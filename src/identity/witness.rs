//! Counterexamples: why two formulas differ, shown so that a person can check it.
//!
//! When the difference lies in arithmetic or in NULLs, the witness is a small assignment to
//! the source columns of integers or NULL (and, for aggregates, a table of a few rows, perhaps
//! none) at which the two formulas, worked out exactly over the rationals with SQL's NULLs,
//! give different results. It may also give values to the uninterpreted expressions both
//! formulas share (a CASE condition holding, a function's result), chosen as freely as a
//! column's; the expression may never take them, so such a witness shows a difference only if
//! it can. When one formula applies an uninterpreted function the other does
//! not, no assignment can be checked without knowing the function, so the witness names it.
//!
//! The NULL literal is never an input: it is NULL, not a value to choose.

use std::collections::{BTreeMap, BTreeSet};

use super::eval::Eval;
use super::field::Lanes;
use super::oracle::fixed_draw;
use super::program::Op;
use super::rational::Q;
use crate::fnv::FnvHashMap;

/// Attempts without NULLs, then with NULLs and empty tables, each drawing a fresh assignment.
const PLAIN_ATTEMPTS: u64 = 64;
const ATTEMPTS: u64 = 160;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    /// Inputs at which the formulas give different results.
    Values,
    /// The formulas apply different uninterpreted functions, or apply them to different
    /// arguments.
    Functions,
    /// The formulas are the same but their aggregates run under different conditions.
    Conditions,
}

/// A node of one of the formulas compared: its function's name, and where its label is.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Mention {
    pub definition: u32,
    pub label: Option<u32>,
    pub name: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Input {
    Column(String),
    /// The value of an uninterpreted expression, or for a CASE condition 1 when it holds and
    /// 0 when it does not.
    Expression(Mention),
}

/// A value of SQL: a number, or NULL (None).
pub type Value = Option<Q>;

#[derive(Clone, Debug, PartialEq)]
pub struct Witness {
    pub kind: Kind,
    /// The inputs outside any aggregate.
    pub inputs: Vec<(Input, Value)>,
    /// The rows the aggregates read, each with its inputs; whether there are any at all is
    /// [`Witness::aggregates`].
    pub rows: Vec<Vec<(Input, Value)>>,
    /// Whether the formulas aggregate rows, so that `rows` is the table (even when empty).
    pub aggregates: bool,
    /// The two formulas' results at the inputs.
    pub results: Option<(Value, Value)>,
    /// The functions the difference comes from.
    pub functions: Vec<String>,
    /// The innermost uninterpreted expressions only the first formula has.
    pub only_a: Vec<Mention>,
    pub only_b: Vec<Mention>,
}

impl Witness {
    fn empty(kind: Kind) -> Witness {
        Witness {
            kind,
            inputs: Vec::new(),
            rows: Vec::new(),
            aggregates: false,
            results: None,
            functions: Vec::new(),
            only_a: Vec::new(),
            only_b: Vec::new(),
        }
    }

    pub fn conditions() -> Witness {
        Witness::empty(Kind::Conditions)
    }
}

/// An uninterpreted value: whether it is read per row (inside an aggregate), and its identity.
type AtomKey = (bool, Lanes);

#[derive(Debug)]
struct Atom {
    mention: Mention,
    indicator: bool,
    /// Whether its value is read, not only hashed as another's argument: only those are
    /// inputs of a witness.
    read: bool,
    /// The uninterpreted values its arguments depend on.
    beneath: BTreeSet<AtomKey>,
}

/// Where a node is read.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
struct Place {
    /// Per row, inside an aggregate.
    inside: bool,
    /// Beneath an uninterpreted value.
    opaque: bool,
    /// Directly beneath `present`: a column there is never NULL.
    present: bool,
}

/// What one formula reads: its uninterpreted values and its columns, outside aggregates and
/// per row, with whether each column may be NULL.
#[derive(Default)]
struct Reading {
    atoms: BTreeMap<AtomKey, Atom>,
    columns: [BTreeMap<String, bool>; 2],
    /// Whether it aggregates rows, so that a witness must say how many it reads.
    aggregates: bool,
    seen: FnvHashMap<(u32, Place), BTreeSet<AtomKey>>,
}

impl Reading {
    fn of(eval: &mut Eval<'_>, definition: u32) -> Reading {
        let mut reading = Reading::default();
        let root = eval.program.definitions[definition as usize].root;
        let place = Place {
            inside: false,
            opaque: false,
            present: false,
        };
        reading.collect(eval, root, place);
        reading
    }

    fn add(
        &mut self,
        key: AtomKey,
        mention: Mention,
        place: Place,
        indicator: bool,
        beneath: BTreeSet<AtomKey>,
    ) {
        let atom = self.atoms.entry(key).or_insert(Atom {
            mention,
            indicator,
            read: false,
            beneath: BTreeSet::new(),
        });
        atom.read |= !place.opaque;
        atom.beneath.extend(beneath);
    }

    /// Every uninterpreted value in `node`'s subtree, itself included, recording each.
    fn collect(&mut self, eval: &mut Eval<'_>, node: u32, place: Place) -> BTreeSet<AtomKey> {
        if let Some(found) = self.seen.get(&(node, place)) {
            return found.clone();
        }
        let program = eval.program;
        let children = program.children(node).to_vec();
        let at = &program.nodes[node as usize];
        let mention = |name: &str| Mention {
            definition: at.definition,
            label: at.label,
            name: name.to_string(),
        };
        let inside = place.inside;
        let mut below = BTreeSet::new();
        let mut own: Option<&str> = None;
        // Arguments of an uninterpreted value: their columns are not inputs of a witness,
        // which gives the value itself.
        let argument = Place {
            opaque: true,
            present: false,
            ..place
        };
        let arithmetic = Place {
            present: false,
            ..place
        };
        match &at.op {
            Op::Column(name) => {
                if !place.opaque {
                    let nullable = self.columns[usize::from(inside)]
                        .entry(name.to_string())
                        .or_insert(true);
                    *nullable &= !place.present;
                }
            }
            Op::Present => {
                let present = Place {
                    present: true,
                    ..place
                };
                below = self.collect(eval, children[0], present);
            }
            Op::Outer(name) => {
                // Whether the row matched is an input, as a CASE condition holding is.
                let key = (inside, eval.matched(node));
                self.add(key, mention(name), place, true, BTreeSet::new());
                below = self.collect(eval, children[0], arithmetic);
                below.insert(key);
            }
            Op::Number(..) => {}
            Op::Ref(target) => {
                below = self.collect(eval, program.definitions[*target as usize].root, place);
            }
            Op::Literal(kind, _) if &**kind == "null" => {}
            Op::Literal(kind, _) => own = Some(kind),
            Op::Function(name) | Op::Symmetric(name) | Op::Window(name) => {
                for c in children {
                    below.extend(self.collect(eval, c, argument));
                }
                own = Some(name);
            }
            Op::Case => {
                for (condition, branch, indicator) in eval.kept(&children) {
                    let beneath = self.collect(eval, condition, argument);
                    let at = &program.nodes[condition as usize];
                    let key = (inside, indicator);
                    let mention = Mention {
                        definition: at.definition,
                        label: at.label,
                        name: "CASE".to_string(),
                    };
                    self.add(key, mention, place, true, beneath.clone());
                    below.insert(key);
                    below.extend(beneath);
                    below.extend(self.collect(eval, branch, arithmetic));
                }
                let otherwise = *children.last().expect("an ELSE");
                below.extend(self.collect(eval, otherwise, arithmetic));
            }
            op @ (Op::Sum | Op::Avg | Op::Count | Op::CountOf | Op::Aggregate(_)) => {
                self.aggregates = true;
                let atom = inside || matches!(op, Op::Aggregate(_));
                let rows = Place {
                    inside: true,
                    opaque: place.opaque || atom,
                    present: false,
                };
                for c in &children {
                    below.extend(self.collect(eval, *c, rows));
                }
                if atom {
                    own = Some(match op {
                        Op::Sum => "SUM",
                        Op::Avg => "AVG",
                        Op::Aggregate(name) => name,
                        _ => "COUNT",
                    });
                }
            }
            Op::Add | Op::Sub | Op::Neg | Op::Mul | Op::Div | Op::Pow(_) | Op::Coalesce => {
                for c in children {
                    below.extend(self.collect(eval, c, arithmetic));
                }
            }
        }
        if let Some(name) = own {
            let key = (inside, eval.value(node).lanes);
            self.add(key, mention(name), place, false, below.clone());
            below.insert(key);
        }
        self.seen.insert((node, place), below.clone());
        below
    }

    /// The innermost of `keys`: those none of whose arguments depend on another of `keys`.
    fn innermost(&self, keys: &BTreeSet<AtomKey>) -> Vec<Mention> {
        let mut found: Vec<Mention> = keys
            .iter()
            .filter(|k| self.atoms[k].beneath.is_disjoint(keys))
            .map(|k| self.atoms[k].mention.clone())
            .collect();
        found.sort();
        found.dedup();
        found
    }

    /// Those of `keys` whose values are read, not only hashed as another's argument.
    fn read(&self, keys: &BTreeSet<AtomKey>) -> Vec<&Atom> {
        keys.iter()
            .map(|k| &self.atoms[k])
            .filter(|atom| atom.read)
            .collect()
    }

    fn names(&self) -> BTreeMap<&str, usize> {
        let mut names = BTreeMap::new();
        for atom in self.atoms.values() {
            *names.entry(atom.mention.name.as_str()).or_insert(0) += 1;
        }
        names
    }
}

/// Values for every input of one attempt.
struct Env {
    rows: usize,
    columns: FnvHashMap<String, Value>,
    atoms: FnvHashMap<Lanes, Q>,
    row_columns: Vec<FnvHashMap<String, Value>>,
    row_atoms: Vec<FnvHashMap<Lanes, Q>>,
}

/// Exact evaluation over the rationals, with SQL's NULLs, under an assignment. None is a
/// result that cannot be worked out (an overflow or a division by zero).
struct Exact<'a, 'e> {
    eval: &'a mut Eval<'e>,
    env: &'a Env,
    memo: FnvHashMap<(u32, usize), Option<Value>>,
}

const OUTSIDE: usize = usize::MAX;

fn strict(values: &[Value], f: impl FnOnce(&[Q]) -> Option<Q>) -> Option<Value> {
    let present: Option<Vec<Q>> = values.iter().copied().collect();
    match present {
        Some(numbers) => Some(Some(f(&numbers)?)),
        None => Some(None),
    }
}

impl Exact<'_, '_> {
    fn atom(&self, key: Lanes, row: usize) -> Option<Value> {
        let found = if row == OUTSIDE {
            self.env.atoms.get(&key)
        } else {
            self.env.row_atoms[row].get(&key)
        };
        found.map(|q| Some(*q))
    }

    fn value(&mut self, node: u32, row: usize) -> Option<Value> {
        if let Some(found) = self.memo.get(&(node, row)) {
            return *found;
        }
        let value = self.compute(node, row);
        self.memo.insert((node, row), value);
        value
    }

    fn values(&mut self, children: &[u32], row: usize) -> Option<Vec<Value>> {
        children.iter().map(|&c| self.value(c, row)).collect()
    }

    fn compute(&mut self, node: u32, row: usize) -> Option<Value> {
        let program = self.eval.program;
        let children = program.children(node).to_vec();
        let op = &program.nodes[node as usize].op;
        match op {
            Op::Column(name) => {
                let columns = if row == OUTSIDE {
                    &self.env.columns
                } else {
                    &self.env.row_columns[row]
                };
                columns.get(&**name).copied()
            }
            Op::Present => self.value(children[0], row),
            Op::Outer(_) => {
                if self.atom(self.eval.matched(node), row)?? == Q::ZERO {
                    return Some(None);
                }
                self.value(children[0], row)
            }
            Op::Number(_, _, exact) => Some(Some((*exact)?)),
            Op::Ref(target) => self.value(program.definitions[*target as usize].root, row),
            Op::Literal(kind, _) if &**kind == "null" => Some(None),
            Op::Literal(..) | Op::Function(_) | Op::Symmetric(_) | Op::Window(_) => {
                let key = self.eval.value(node).lanes;
                self.atom(key, row)
            }
            Op::Add => strict(&self.values(&children, row)?, |v| {
                v.iter().try_fold(Q::ZERO, |sum, &q| sum.checked_add(q))
            }),
            Op::Mul => strict(&self.values(&children, row)?, |v| {
                v.iter()
                    .try_fold(Q::ONE, |product, &q| product.checked_mul(q))
            }),
            Op::Sub => strict(&self.values(&children, row)?, |v| v[0].checked_sub(v[1])),
            Op::Neg => strict(&self.values(&children, row)?, |v| v[0].checked_neg()),
            Op::Div => strict(&self.values(&children, row)?, |v| v[0].checked_div(v[1])),
            Op::Pow(exponent) => strict(&self.values(&children, row)?, |v| {
                v[0].checked_pow(*exponent)
            }),
            Op::Case => {
                for (condition, branch, indicator) in self.eval.kept(&children) {
                    let _ = condition;
                    if self.atom(indicator, row)?? != Q::ZERO {
                        return self.value(branch, row);
                    }
                }
                self.value(*children.last()?, row)
            }
            Op::Coalesce => {
                for &c in &children {
                    if let Some(q) = self.value(c, row)? {
                        return Some(Some(q));
                    }
                }
                Some(None)
            }
            _ if row != OUTSIDE => {
                // An aggregate inside an aggregate: one uninterpreted value per outer row.
                let key = self.eval.value(node).lanes;
                self.atom(key, row)
            }
            Op::Sum => {
                let (total, count) = self.totals(children[0])?;
                Some((count != 0).then_some(total))
            }
            Op::Avg => {
                let (total, count) = self.totals(children[0])?;
                if count == 0 {
                    return Some(None);
                }
                Some(Some(total.checked_div(Q::integer(count as i64))?))
            }
            Op::Count => Some(Some(Q::integer(self.env.rows as i64))),
            Op::CountOf => Some(Some(Q::integer(self.totals(children[0])?.1 as i64))),
            Op::Aggregate(_) => {
                let key = self.eval.value(node).lanes;
                self.atom(key, OUTSIDE)
            }
        }
    }

    /// The sum of an aggregate's argument over the rows where it is not NULL, and how many
    /// those are.
    fn totals(&mut self, node: u32) -> Option<(Q, usize)> {
        let (mut total, mut count) = (Q::ZERO, 0);
        for row in 0..self.env.rows {
            if let Some(value) = self.value(node, row)? {
                total = total.checked_add(value)?;
                count += 1;
            }
        }
        Some((total, count))
    }
}

/// Why definitions `a` and `b`, which differ, are different: see the module.
pub fn explain(eval: &mut Eval<'_>, a: u32, b: u32) -> Option<Witness> {
    let first = Reading::of(eval, a);
    let second = Reading::of(eval, b);
    let keys_a: BTreeSet<AtomKey> = first.atoms.keys().copied().collect();
    let keys_b: BTreeSet<AtomKey> = second.atoms.keys().copied().collect();
    if keys_a != keys_b {
        let only_a: BTreeSet<AtomKey> = keys_a.difference(&keys_b).copied().collect();
        let only_b: BTreeSet<AtomKey> = keys_b.difference(&keys_a).copied().collect();
        // When only one formula reads CASE conditions the other does not, values still show
        // the difference: such a condition holding or not is an input like a column. With
        // conditions only in each, the two sets may be related (x > 0 and NOT x > 0), and
        // values chosen independently might be impossible, so the conditions are named.
        let (extra_a, extra_b) = (first.read(&only_a), second.read(&only_b));
        let one_sided = extra_a.is_empty() || extra_b.is_empty();
        if one_sided
            && extra_a.iter().chain(&extra_b).all(|atom| atom.indicator)
            && let Some(found) = search(eval, a, b, &first, &second)
        {
            return Some(found);
        }
        let (names_a, names_b) = (first.names(), second.names());
        let mut functions: Vec<String> = names_a
            .keys()
            .chain(names_b.keys())
            .filter(|name| names_a.get(*name) != names_b.get(*name))
            .map(|name| name.to_string())
            .collect();
        let (inner_a, inner_b) = (first.innermost(&only_a), second.innermost(&only_b));
        if functions.is_empty() {
            functions = inner_a
                .iter()
                .chain(&inner_b)
                .map(|m| m.name.clone())
                .collect();
        }
        functions.sort();
        functions.dedup();
        let mut witness = Witness::empty(Kind::Functions);
        witness.functions = functions;
        witness.only_a = inner_a;
        witness.only_b = inner_b;
        return Some(witness);
    }
    search(eval, a, b, &first, &second)
}

/// Draws small assignments until the formulas give different exact results: first integers
/// only, over tables of one to three rows; then with NULLs for the columns that may be NULL
/// and tables of none to three rows.
fn search(
    eval: &mut Eval<'_>,
    a: u32,
    b: u32,
    first: &Reading,
    second: &Reading,
) -> Option<Witness> {
    let columns = |level: usize| -> Vec<(String, bool)> {
        let mut merged = first.columns[level].clone();
        for (name, nullable) in &second.columns[level] {
            *merged.entry(name.clone()).or_insert(true) &= *nullable;
        }
        merged.into_iter().collect()
    };
    let (outer_columns, row_columns) = (columns(0), columns(1));
    let mut union: BTreeMap<&AtomKey, &Atom> = BTreeMap::new();
    for (key, atom) in first.atoms.iter().chain(&second.atoms) {
        if atom.read {
            union.entry(key).or_insert(atom);
        }
    }
    let mut atoms: Vec<(&AtomKey, &Atom)> = union.into_iter().collect();
    atoms.sort_by(|x, y| x.1.mention.cmp(&y.1.mention));
    let aggregates = first.aggregates || second.aggregates;
    let (root_a, root_b) = (
        eval.program.definitions[a as usize].root,
        eval.program.definitions[b as usize].root,
    );
    for attempt in 0..ATTEMPTS {
        let nulls = attempt >= PLAIN_ATTEMPTS;
        let rows = if nulls { attempt % 4 } else { 1 + attempt % 3 } as usize;
        let range = 2 + (attempt % PLAIN_ATTEMPTS) / 4;
        let mut index = 0;
        // The fixed key's stream, so that the witness is the same whatever key evaluated.
        let mut next = |indicator: bool, nullable: bool| -> Value {
            let word = fixed_draw(attempt, index);
            index += 1;
            if indicator {
                return Some(Q::integer((word & 1) as i64));
            }
            if nulls && nullable && (word >> 8).is_multiple_of(4) {
                return None;
            }
            let magnitude = 1 + (word >> 1) % range;
            let negative = attempt >= PLAIN_ATTEMPTS / 2 && word & 1 == 1;
            Some(Q::integer(if negative {
                -(magnitude as i64)
            } else {
                magnitude as i64
            }))
        };
        let mut env = Env {
            rows,
            columns: FnvHashMap::default(),
            atoms: FnvHashMap::default(),
            row_columns: vec![FnvHashMap::default(); rows],
            row_atoms: vec![FnvHashMap::default(); rows],
        };
        for (name, nullable) in &outer_columns {
            env.columns.insert(name.clone(), next(false, *nullable));
        }
        for (key, atom) in &atoms {
            if !key.0 {
                env.atoms
                    .insert(key.1, next(atom.indicator, false).expect("not NULL"));
            }
        }
        for row in 0..rows {
            for (name, nullable) in &row_columns {
                env.row_columns[row].insert(name.clone(), next(false, *nullable));
            }
            for (key, atom) in &atoms {
                if key.0 {
                    env.row_atoms[row]
                        .insert(key.1, next(atom.indicator, false).expect("not NULL"));
                }
            }
        }
        let mut exact = Exact {
            eval,
            env: &env,
            memo: FnvHashMap::default(),
        };
        let (Some(x), Some(y)) = (exact.value(root_a, OUTSIDE), exact.value(root_b, OUTSIDE))
        else {
            continue;
        };
        if x == y {
            continue;
        }
        let inputs =
            |columns: &FnvHashMap<String, Value>, values: &FnvHashMap<Lanes, Q>, inside: bool| {
                let mut found: Vec<(Input, Value)> = columns
                    .iter()
                    .map(|(name, q)| (Input::Column(name.clone()), *q))
                    .collect();
                found.sort_by(|p, q| match (&p.0, &q.0) {
                    (Input::Column(x), Input::Column(y)) => x.cmp(y),
                    _ => std::cmp::Ordering::Equal,
                });
                for (key, atom) in &atoms {
                    if key.0 == inside {
                        found.push((
                            Input::Expression(atom.mention.clone()),
                            Some(values[&key.1]),
                        ));
                    }
                }
                found
            };
        let mut witness = Witness::empty(Kind::Values);
        witness.inputs = inputs(&env.columns, &env.atoms, false);
        if aggregates {
            witness.rows = (0..rows)
                .map(|r| inputs(&env.row_columns[r], &env.row_atoms[r], true))
                .collect();
        }
        witness.aggregates = aggregates;
        witness.results = Some((x, y));
        return Some(witness);
    }
    None
}
