//! Evaluating definitions at the random point of every trial.
//!
//! A formula's value is a rational function of its source columns, of the values of its
//! uninterpreted functions and of the row sums its aggregates stand for; whether it is NULL is
//! a Boolean function of whether its columns and uninterpreted values are. Evaluating the value
//! at a point drawn uniformly from each lane's field gives that lane of its fingerprint; by the
//! Schwartz-Zippel lemma two different rational functions of degree at most d agree at such a
//! point with probability at most d/p. Presence is kept exactly, as a multilinear polynomial
//! over Boolean atoms (see [`super::poly`]).
//!
//! What "the same" means, precisely: two formulas are the same when, for every assignment to
//! the source columns of a number or NULL, and over every table of rows (an empty one
//! included), they are both NULL or both the same number, wherever no divisor is zero, for
//! every interpretation of the uninterpreted functions. In detail:
//!
//! * `+`, `-`, `*` and `/` are exact (rational) arithmetic, and NULL if an operand is.
//! * Every other function, comparison and constant is uninterpreted: its value is a keyed hash
//!   of its name and its arguments' values and presence, a fresh random value for each distinct
//!   argument, and whether it is NULL is a fresh Boolean atom of the same. Equal fingerprints
//!   therefore mean equal formulas for every interpretation of those functions, as long as one
//!   name always means one function (congruence, as in Ackermann's reduction). `sym` functions
//!   hash their arguments as a multiset.
//! * `CASE WHEN c THEN a ELSE b END` is `t*a + (1 - t)*b`, with `t` a Boolean atom standing for
//!   "c is true", and NULL exactly when the branch it takes is. A WHEN that repeats an earlier
//!   condition is never taken. A branch that is always NULL gives no value, as the CASE is NULL
//!   wherever it is taken: the branches after it give the value there, and an ELSE that is
//!   always NULL leaves it to the last branch that is not. So `CASE WHEN c THEN a END + 1` and
//!   `CASE WHEN c THEN a + 1 END` have the same value, `a + 1`, and the same presence.
//!   `COALESCE` is the first argument that is not NULL.
//! * `SUM(e)` expands `e`, times its presence, into monomials over the row's atoms, and
//!   replaces each monomial `m` by a random row sum `S_ctx(m)`, keyed by the context (the
//!   conditions and grain the aggregate runs under). This is exactly linearity over the rows
//!   where `e` is not NULL: SUM(3a) = 3 SUM(a), while SUM(a)SUM(b) and SUM(ab) stay apart.
//!   `COUNT(e)` is the row sum of e's presence and `COUNT(*)` that of 1; `AVG(e)` is SUM(e)
//!   over COUNT(e). SUM and AVG are NULL when no row has e: the Boolean atom "the table has
//!   rows" times one of COUNT(e), as the second implies the first. MIN,
//!   MAX, COUNT DISTINCT, window functions and every other aggregate are uninterpreted, keyed
//!   by the context too.
//!
//! Without NULLs (the second verdict [`super::Formulas::compare`] gives) every source column is
//! taken as present and every group as having rows; a NULL a CASE gives is still NULL.
//!
//! A wrong "same" needs every lane to fail. In a lane, a nonzero polynomial vanishes at the
//! point with probability at most D/p, or vanishes modulo p because p divides each of its
//! integer coefficients, which for coefficients below 2^H happens for at most H/60 of the more
//! than 2.8 * 10^15 primes the lane's prime is drawn from. With n uninterpreted values whose
//! arguments could collide, the bound is (1 + n(n-1)/2)(D/p + (H/60 + 1)/(2.8 * 10^15))^3.
//! Formulas that do not compare the same are not shown to be the same, which is not a proof
//! that they differ. Lanes that differ are a point where the two values differ as polynomials,
//! but a Boolean atom is evaluated at a random point rather than at 0 or 1, so SQL can still
//! make them equal: `t * t` and `t` differ as polynomials, and so `CASE WHEN c THEN 1 ELSE 0
//! END` squared and `CASE WHEN c THEN 1 ELSE 0 END` are not shown to be the same. A witness of
//! values (see [`super::witness`]) is an assignment at which they differ, worked out exactly,
//! and a proof when it assigns source columns alone: a value it gives an uninterpreted
//! expression may be one the expression never takes.

use std::collections::HashSet;
use std::hash::BuildHasherDefault;
use std::sync::Arc;

use super::field::{Field, LANES, Lanes, PRIMES_IN_RANGE};
use super::oracle::{Feed, Oracle, Tag};
use super::poly::{Poly, decimal_bits};
use super::program::{Op, Program};
use crate::fnv::{Fnv1a, FnvHashMap};

type FnvHashSet<T> = HashSet<T, BuildHasherDefault<Fnv1a>>;

/// The most terms a presence is kept with. An OR of k atoms has 2^k - 1 terms, so a long chain
/// of COALESCE would otherwise grow to the expansion limit; past this one a presence is a
/// Boolean atom standing for exactly itself, keyed by the presences it was made from.
const PRESENCE_TERMS: usize = 8;

fn small(p: Poly) -> Option<Poly> {
    (p.len() <= PRESENCE_TERMS).then_some(p)
}

/// A definition's value at the trial points, its presence, and what bounds the error of
/// comparing it.
#[derive(Clone, Debug)]
pub struct Val {
    pub lanes: Lanes,
    /// When it is not NULL, as a multilinear polynomial over Boolean atoms.
    pub presence: Arc<Poly>,
    /// A bound on the degree of the numerator of the value as a rational function.
    pub num: u32,
    /// A bound on the degree of its denominator.
    pub den: u32,
    /// A bound on the bits of the numerator's and denominator's integer coefficients.
    pub bits: u32,
    /// The highest degree among the arguments of the uninterpreted values beneath.
    pub reach: u32,
    /// The most coefficient bits among those arguments.
    pub reach_bits: u32,
    /// How many uninterpreted values are beneath, counted with repetition.
    pub apps: u32,
    /// A divisor was zero at some trial's point.
    pub singular: bool,
}

impl Val {
    /// Whether two values are the same: the same in every lane, and NULL at the same times.
    pub fn same(&self, other: &Val) -> bool {
        self.lanes == other.lanes && self.presence == other.presence
    }

    fn joined(&self, other: &Val, lanes: Lanes, num: u32, den: u32, bits: u32) -> Val {
        Val {
            lanes,
            presence: self.presence.clone(),
            num,
            den,
            bits,
            reach: self.reach.max(other.reach),
            reach_bits: self.reach_bits.max(other.reach_bits),
            apps: self.apps.saturating_add(other.apps),
            singular: self.singular || other.singular,
        }
    }

    fn reach_of(&self) -> (u32, u32) {
        let degree = self
            .num
            .saturating_add(self.den)
            .max(self.presence.degree())
            .max(self.reach);
        let bits = self.bits.max(self.presence.bits).max(self.reach_bits);
        (degree, bits)
    }
}

/// Feeds a value and its presence to a draw: what an uninterpreted function sees of it.
fn feed_val(f: &mut Feed, v: &Val) {
    f.lanes(&v.lanes);
    feed_poly(f, &v.presence);
}

fn feed_poly(f: &mut Feed, p: &Poly) {
    f.number(p.len() as u64);
    for (monomial, c) in p.terms() {
        f.number(monomial.len() as u64);
        for (atom, boolean) in monomial {
            f.lanes(atom).number(u64::from(*boolean));
        }
        f.lanes(&c);
    }
}

/// Values, expansions and the constant presences of one evaluation.
pub struct Memo {
    values: Vec<Option<Val>>,
    polys: FnvHashMap<u32, Arc<Poly>>,
    one: Arc<Poly>,
    zero: Arc<Poly>,
}

impl Memo {
    pub fn new(field: &Field) -> Memo {
        Memo {
            values: Vec::new(),
            polys: FnvHashMap::default(),
            one: Arc::new(Poly::one(field)),
            zero: Arc::new(Poly::zero()),
        }
    }
}

/// One evaluation of a program: its memo, the contexts its aggregates are keyed by, and whether
/// source values may be NULL.
pub struct Eval<'a> {
    pub program: &'a Program,
    pub oracle: &'a Oracle,
    pub contexts: &'a [[u8; 16]],
    pub memo: &'a mut Memo,
    pub nulls: bool,
}

impl Eval<'_> {
    fn field(&self) -> &Field {
        &self.oracle.field
    }

    fn context(&self, node: u32) -> [u8; 16] {
        let definition = self.program.nodes[node as usize].definition;
        self.contexts[self.program.definitions[definition as usize].context as usize]
    }

    fn constant(&self, lanes: Lanes, bits: u32) -> Val {
        Val {
            lanes,
            presence: self.memo.one.clone(),
            num: 0,
            den: 0,
            bits,
            reach: 0,
            reach_bits: 0,
            apps: 0,
            singular: false,
        }
    }

    /// A fresh value, the result of an uninterpreted function of `args`, with `presence`.
    fn atom(&self, lanes: Lanes, args: &[Val], presence: Arc<Poly>) -> Val {
        let (reach, reach_bits) = args
            .iter()
            .map(Val::reach_of)
            .fold((0, 0), |(d, b), (d2, b2)| (d.max(d2), b.max(b2)));
        let value = Val {
            lanes,
            presence,
            num: 1,
            den: 0,
            bits: 1,
            reach,
            reach_bits,
            apps: args.iter().fold(1u32, |n, a| n.saturating_add(a.apps)),
            singular: args.iter().any(|a| a.singular),
        };
        self.normalised(value)
    }

    /// A value that is always NULL is the NULL value: its lanes no longer matter.
    fn normalised(&self, mut value: Val) -> Val {
        if value.presence.is_zero() {
            value.lanes = Lanes::default();
            value.num = 0;
            value.den = 0;
        }
        value
    }

    /// A Boolean atom drawn for the input `feed` writes.
    fn boolean(&self, tag: Tag, feed: impl FnOnce(&mut Feed)) -> Arc<Poly> {
        let identity = self.oracle.lanes(tag, feed);
        Arc::new(Poly::atom(self.field(), identity, true))
    }

    /// Presence when both are present: the product, or past the limit an atom standing for it.
    fn both(&self, a: &Arc<Poly>, b: &Arc<Poly>) -> Arc<Poly> {
        let field = self.field();
        if a.is_one(field) || b.is_zero() {
            return b.clone();
        }
        if b.is_one(field) || a.is_zero() {
            return a.clone();
        }
        match a.mul(b, field).and_then(small) {
            Some(product) => Arc::new(product),
            None => self.boolean(Tag::BothPresence, |f| {
                feed_poly(f, a);
                feed_poly(f, b);
            }),
        }
    }

    fn add(&self, a: &Val, b: &Val) -> Val {
        let field = self.field();
        let num = (a.num.saturating_add(b.den)).max(b.num.saturating_add(a.den));
        let bits = a.bits.saturating_add(b.bits).saturating_add(1);
        let mut value = a.joined(
            b,
            field.add(a.lanes, b.lanes),
            num,
            a.den.saturating_add(b.den),
            bits,
        );
        value.presence = self.both(&a.presence, &b.presence);
        self.normalised(value)
    }

    fn neg(&self, a: &Val) -> Val {
        Val {
            lanes: self.field().neg(a.lanes),
            ..a.clone()
        }
    }

    fn mul(&self, a: &Val, b: &Val) -> Val {
        let field = self.field();
        let mut value = a.joined(
            b,
            field.mul(a.lanes, b.lanes),
            a.num.saturating_add(b.num),
            a.den.saturating_add(b.den),
            a.bits.saturating_add(b.bits),
        );
        value.presence = self.both(&a.presence, &b.presence);
        self.normalised(value)
    }

    fn div(&self, a: &Val, b: &Val) -> Val {
        let field = self.field();
        let (inverse, singular) = field.inverse(b.lanes);
        let mut value = a.joined(
            b,
            field.mul(a.lanes, inverse),
            a.num.saturating_add(b.den),
            a.den.saturating_add(b.num),
            a.bits.saturating_add(b.bits),
        );
        value.presence = self.both(&a.presence, &b.presence);
        // A NULL divisor's value does not matter; a zero one that is present does.
        value.singular |= singular && !b.presence.is_zero();
        self.normalised(value)
    }

    /// The value alone, taken as present: the arithmetic that picks a branch works on values,
    /// and the presence of the result is worked out on its own.
    fn bare(&self, v: &Val) -> Val {
        Val {
            presence: self.memo.one.clone(),
            ..v.clone()
        }
    }

    /// A presence as a number, for arithmetic that branches on it.
    fn presence_val(&self, p: &Arc<Poly>) -> Val {
        let mut value = self.constant(p.evaluate(self.field()), p.bits);
        value.num = p.degree();
        value
    }

    /// Evaluates the definitions `definition` refers to, directly or not, earliest first, so
    /// that evaluating it recurses only through its own tree, never down a long lineage.
    fn ensure(&mut self, definition: u32) {
        if self.memo.values.len() < self.program.nodes.len() {
            self.memo.values.resize(self.program.nodes.len(), None);
        }
        let root = self.program.definitions[definition as usize].root;
        if self.memo.values[root as usize].is_some() {
            return;
        }
        let mut pending = vec![definition];
        let mut seen = FnvHashSet::default();
        seen.insert(definition);
        while let Some(d) = pending.pop() {
            for node in self.span(d) {
                if let Op::Ref(target) = self.program.nodes[node as usize].op {
                    let target_root = self.program.definitions[target as usize].root;
                    if self.memo.values[target_root as usize].is_none() && seen.insert(target) {
                        pending.push(target);
                    }
                }
            }
        }
        let mut order: Vec<u32> = seen.into_iter().collect();
        order.sort_unstable();
        for d in order {
            let root = self.program.definitions[d as usize].root;
            self.value(root);
        }
    }

    /// The nodes of a definition: they are added together, its root last.
    fn span(&self, definition: u32) -> std::ops::RangeInclusive<u32> {
        let end = self.program.definitions[definition as usize].root;
        let start = match definition {
            0 => 0,
            d => self.program.definitions[d as usize - 1].root + 1,
        };
        start..=end
    }

    /// The value of a definition's formula.
    pub fn definition(&mut self, definition: u32) -> Val {
        self.ensure(definition);
        self.value(self.program.definitions[definition as usize].root)
    }

    pub fn value(&mut self, node: u32) -> Val {
        if let Some(Some(found)) = self.memo.values.get(node as usize) {
            return found.clone();
        }
        let value = self.compute(node);
        if self.memo.values.len() <= node as usize {
            self.memo.values.resize(self.program.nodes.len(), None);
        }
        self.memo.values[node as usize] = Some(value.clone());
        value
    }

    fn args(&mut self, node: u32) -> Vec<Val> {
        let children = self.program.children(node).to_vec();
        children.into_iter().map(|c| self.value(c)).collect()
    }

    /// The Boolean atom "the row of the outer join's side that `node` reads matched".
    pub fn matched(&self, node: u32) -> Lanes {
        match &self.program.nodes[node as usize].op {
            Op::Outer(key) => self.oracle.lanes(Tag::Outer, |f| {
                f.text(key);
            }),
            _ => Lanes::default(),
        }
    }

    /// The Boolean atom "the condition holds": a fresh one for each distinct condition.
    pub fn indicator(&mut self, condition: u32) -> Lanes {
        let c = self.value(condition);
        self.oracle.lanes(Tag::Indicator, |f| feed_val(f, &c))
    }

    /// The (condition, branch) pairs of a CASE with these children that can be taken, with
    /// each condition's indicator. A WHEN whose condition repeats an earlier one never is:
    /// when it holds, the earlier one was chosen already.
    pub fn kept(&mut self, children: &[u32]) -> Vec<(u32, u32, Lanes)> {
        let mut seen = FnvHashSet::default();
        let mut kept = Vec::new();
        for pair in children[..children.len() - 1].as_chunks::<2>().0 {
            let indicator = self.indicator(pair[0]);
            if seen.insert(indicator) {
                kept.push((pair[0], pair[1], indicator));
            }
        }
        kept
    }

    fn uninterpreted(
        &mut self,
        node: u32,
        tag: Tag,
        presence_tag: Tag,
        name: &str,
        context: bool,
    ) -> Val {
        let mut args = self.args(node);
        if tag == Tag::Symmetric {
            args.sort_by(|x, y| (x.lanes, &*x.presence).cmp(&(y.lanes, &*y.presence)));
        }
        let context = context.then(|| self.context(node));
        let feed = |f: &mut Feed| {
            if let Some(context) = &context {
                f.bytes(context);
            }
            f.text(name).number(args.len() as u64);
            args.iter().for_each(|a| feed_val(f, a));
        };
        let lanes = self.oracle.lanes(tag, feed);
        // Without NULLs an aggregate of arguments that are never NULL is not NULL either.
        let present = !self.nulls
            && tag == Tag::Aggregate
            && args.iter().all(|a| a.presence.is_one(self.field()));
        let presence = if present {
            self.memo.one.clone()
        } else {
            self.boolean(presence_tag, feed)
        };
        self.atom(lanes, &args, presence)
    }

    fn compute(&mut self, node: u32) -> Val {
        let program = self.program;
        let oracle = self.oracle;
        match &program.nodes[node as usize].op {
            Op::Column(name) => {
                let lanes = oracle.lanes(Tag::Column, |f| {
                    f.text(name);
                });
                let presence = if self.nulls {
                    self.boolean(Tag::Presence, |f| {
                        f.text(name);
                    })
                } else {
                    self.memo.one.clone()
                };
                self.atom(lanes, &[], presence)
            }
            Op::Present => {
                let mut value = self.args(node).remove(0);
                value.presence = self.memo.one.clone();
                value
            }
            Op::Outer(_) => {
                let mut value = self.args(node).remove(0);
                if self.nulls {
                    let matched = Arc::new(Poly::atom(self.field(), self.matched(node), true));
                    value.presence = self.both(&value.presence, &matched);
                }
                self.normalised(value)
            }
            Op::Number(n, d, _) => match self.field().rational(n, d) {
                Some(lanes) => self.constant(lanes, decimal_bits(n).max(decimal_bits(d))),
                None => {
                    // A denominator that one of the drawn primes divides: no guarantee.
                    let mut value = self.constant(Lanes::default(), 0);
                    value.singular = true;
                    value
                }
            },
            Op::Literal(kind, _) if &**kind == "null" => {
                let mut value = self.constant(Lanes::default(), 0);
                value.presence = self.memo.zero.clone();
                value
            }
            Op::Literal(kind, text) => {
                let lanes = oracle.lanes(Tag::Literal, |f| {
                    f.text(kind).text(text);
                });
                self.atom(lanes, &[], self.memo.one.clone())
            }
            Op::Ref(target) => self.value(program.definitions[*target as usize].root),
            Op::Add => {
                let args = self.args(node);
                args[1..]
                    .iter()
                    .fold(args[0].clone(), |sum, a| self.add(&sum, a))
            }
            Op::Mul => {
                let args = self.args(node);
                args[1..]
                    .iter()
                    .fold(args[0].clone(), |product, a| self.mul(&product, a))
            }
            Op::Sub => {
                let args = self.args(node);
                self.add(&args[0], &self.neg(&args[1]))
            }
            Op::Neg => {
                let args = self.args(node);
                self.neg(&args[0])
            }
            Op::Div => {
                let args = self.args(node);
                self.div(&args[0], &args[1])
            }
            Op::Pow(exponent) => {
                let base = self.args(node).remove(0);
                let mut one = self.constant(self.field().one(), 1);
                one.presence = base.presence.clone();
                (0..*exponent).fold(one, |product, _| self.mul(&product, &base))
            }
            Op::Function(name) => {
                self.uninterpreted(node, Tag::Function, Tag::FunctionPresence, name, false)
            }
            Op::Symmetric(name) => {
                self.uninterpreted(node, Tag::Symmetric, Tag::SymmetricPresence, name, false)
            }
            Op::Aggregate(name) => {
                self.uninterpreted(node, Tag::Aggregate, Tag::AggregatePresence, name, true)
            }
            Op::Window(name) => {
                self.uninterpreted(node, Tag::Window, Tag::WindowPresence, name, true)
            }
            Op::Case => self.case(node),
            Op::Coalesce => self.coalesce(node),
            Op::Sum | Op::Avg => {
                let child = program.children(node)[0];
                let argument = self.value(child);
                let (total, rows) = self.sums(node, child, &argument);
                let mut value = if matches!(program.nodes[node as usize].op, Op::Sum) {
                    total
                } else {
                    let count = self.row_sum(node, &argument.presence, &argument);
                    self.div(&total, &count)
                };
                value.presence = rows;
                self.normalised(value)
            }
            Op::Count => {
                let one = self.memo.one.clone();
                let argument = self.constant(self.field().one(), 1);
                self.row_sum(node, &one, &argument)
            }
            Op::CountOf => {
                let child = program.children(node)[0];
                let argument = self.value(child);
                self.row_sum(node, &argument.presence.clone(), &argument)
            }
        }
    }

    fn case(&mut self, node: u32) -> Val {
        let field = *self.field();
        let children = self.program.children(node).to_vec();
        let kept = self.kept(&children);
        let otherwise = self.value(*children.last().expect("an ELSE"));
        let branches: Vec<Val> = kept
            .iter()
            .map(|&(_, branch, _)| self.value(branch))
            .collect();
        let conditions: Vec<Val> = kept.iter().map(|&(c, _, _)| self.value(c)).collect();
        // The value, t*a + (1 - t)*rest from the last WHEN back, over the branches that can
        // give one: where an always NULL branch is taken, the value does not matter.
        let valued: Vec<usize> = (0..kept.len())
            .filter(|&i| !branches[i].presence.is_zero())
            .collect();
        let (mut value, earlier) = match valued.split_last() {
            Some((&last, earlier)) if otherwise.presence.is_zero() => {
                (self.bare(&branches[last]), earlier)
            }
            _ => (self.bare(&otherwise), &valued[..]),
        };
        for &i in earlier.iter().rev() {
            let indicator = kept[i].2;
            let t = self.atom(
                indicator,
                std::slice::from_ref(&conditions[i]),
                self.memo.one.clone(),
            );
            let rest = self.add(&self.constant(field.one(), 1), &self.neg(&t));
            let chosen = self.mul(&t, &self.bare(&branches[i]));
            value = self.add(&chosen, &self.mul(&rest, &value));
        }
        // The presence, sum over branches of "this branch is taken" times its presence.
        let mut taken = Poly::one(&field);
        let mut presence = Some(Poly::zero());
        for (i, &(_, _, indicator)) in kept.iter().enumerate() {
            let t = Poly::atom(&field, indicator, true);
            presence = presence.and_then(|p| {
                let here = taken.mul(&t, &field)?.mul(&branches[i].presence, &field)?;
                small(p.add(&here, &field)?)
            });
            taken = match t
                .complement(&field)
                .and_then(|c| small(taken.mul(&c, &field)?))
            {
                Some(rest) => rest,
                None => {
                    presence = None;
                    break;
                }
            };
        }
        let presence = presence
            .and_then(|p| p.add(&taken.mul(&otherwise.presence, &field)?, &field))
            .and_then(small);
        value.presence = match presence {
            Some(p) => Arc::new(p),
            None => self.boolean(Tag::CasePresence, |f| {
                f.number(kept.len() as u64);
                for (i, &(_, _, indicator)) in kept.iter().enumerate() {
                    f.lanes(&indicator);
                    feed_poly(f, &branches[i].presence);
                }
                feed_poly(f, &otherwise.presence);
            }),
        };
        self.normalised(value)
    }

    fn coalesce(&mut self, node: u32) -> Val {
        let field = *self.field();
        let args = self.args(node);
        // The value: each argument where it is the first present one, from the last back.
        let mut value = self.bare(args.last().expect("an argument"));
        for arg in args[..args.len() - 1].iter().rev() {
            let present = self.presence_val(&arg.presence);
            let absent = self.add(&self.constant(field.one(), 1), &self.neg(&present));
            let chosen = self.mul(&present, &self.bare(arg));
            value = self.add(&chosen, &self.mul(&absent, &value));
        }
        // Present unless every argument is absent.
        let mut absent = Some(Poly::one(&field));
        for arg in &args {
            absent = absent.and_then(|a| small(a.mul(&arg.presence.complement(&field)?, &field)?));
        }
        value.presence = match absent.and_then(|a| a.complement(&field)) {
            Some(p) => Arc::new(p),
            None => self.boolean(Tag::CoalescePresence, |f| {
                f.number(args.len() as u64);
                args.iter().for_each(|a| feed_poly(f, &a.presence))
            }),
        };
        self.normalised(value)
    }

    /// SUM of `child` at `node`: the row sum of its value where it is present, and when the
    /// sum is not NULL.
    fn sums(&mut self, node: u32, child: u32, argument: &Val) -> (Val, Arc<Poly>) {
        let field = *self.field();
        self.ensure_polys(child);
        let expansion = self.poly(child);
        let weighted = match expansion.mul(&argument.presence, &field) {
            Some(w) => w,
            None => Poly::atom(
                &field,
                self.oracle.lanes(Tag::Weighted, |f| feed_val(f, argument)),
                false,
            ),
        };
        let total = self.row_sum(node, &weighted, argument);
        // NULL unless some row has the argument: "the table has rows" (N) times "a row has
        // it" (R), as R implies N. Written so, the product with another sum's presence is
        // exact: SUM(b) - SUM(0) is NULL exactly when SUM(b) is.
        let context = self.context(node);
        let nonempty = if self.nulls {
            let all = Poly::one(&field);
            let count = self.row_sum(node, &all, argument);
            self.boolean(Tag::Rows, |f| {
                f.bytes(&context).lanes(&count.lanes);
            })
        } else {
            self.memo.one.clone()
        };
        let rows = if argument.presence.is_one(&field) {
            nonempty
        } else {
            let count = self.row_sum(node, &argument.presence.clone(), argument);
            let some = self.boolean(Tag::Rows, |f| {
                f.bytes(&context).lanes(&count.lanes);
            });
            self.both(&nonempty, &some)
        };
        (total, rows)
    }

    /// The row sums of `poly`'s monomials under `node`'s context, weighted by its coefficients.
    fn row_sum(&mut self, node: u32, poly: &Poly, argument: &Val) -> Val {
        let field = *self.field();
        let context = self.context(node);
        let mut lanes = field.zero();
        let mut monomials = 0u32;
        for (monomial, c) in poly.terms() {
            let sum = self.oracle.lanes(Tag::RowSum, |f| {
                f.bytes(&context).number(monomial.len() as u64);
                for (atom, boolean) in monomial {
                    f.lanes(atom).number(u64::from(*boolean));
                }
            });
            lanes = field.add(lanes, field.mul(c, sum));
            monomials = monomials.saturating_add(1);
        }
        let mut value = self.atom(lanes, std::slice::from_ref(argument), self.memo.one.clone());
        value.apps = value.apps.saturating_add(monomials);
        value.bits = poly.bits;
        if poly.is_empty() {
            value.num = 0;
        }
        value
    }

    /// Fills the expansions of the definitions that `node`'s expansion reaches, earliest first,
    /// so that expanding it never recurses down a long lineage.
    fn ensure_polys(&mut self, node: u32) {
        let mut stack = vec![node];
        let mut found = FnvHashSet::default();
        while let Some(n) = stack.pop() {
            match &self.program.nodes[n as usize].op {
                Op::Ref(target) => {
                    let root = self.program.definitions[*target as usize].root;
                    if !self.memo.polys.contains_key(&root) && found.insert(*target) {
                        stack.push(root);
                    }
                }
                Op::Add
                | Op::Sub
                | Op::Neg
                | Op::Mul
                | Op::Div
                | Op::Pow(_)
                | Op::Case
                | Op::Coalesce
                | Op::Present
                | Op::Outer(_) => {
                    stack.extend_from_slice(self.program.children(n));
                }
                _ => {}
            }
        }
        let mut order: Vec<u32> = found.into_iter().collect();
        order.sort_unstable();
        for definition in order {
            let root = self.program.definitions[definition as usize].root;
            self.poly(root);
        }
    }

    /// The expansion of a row-level expression's value into monomials over its atoms.
    pub fn poly(&mut self, node: u32) -> Arc<Poly> {
        if let Some(found) = self.memo.polys.get(&node) {
            return found.clone();
        }
        let poly = match self.expand(node) {
            Some(p) => Arc::new(p),
            None => Arc::new(self.opaque(node, Tag::Opaque)),
        };
        self.memo.polys.insert(node, poly.clone());
        poly
    }

    /// An expression kept whole, as one numeric atom standing for exactly itself.
    fn opaque(&mut self, node: u32, tag: Tag) -> Poly {
        let value = self.value(node);
        let identity = self.oracle.lanes(tag, |f| feed_val(f, &value));
        Poly::atom(self.field(), identity, false)
    }

    fn expand(&mut self, node: u32) -> Option<Poly> {
        let program = self.program;
        let field = *self.field();
        let children = program.children(node).to_vec();
        Some(match &program.nodes[node as usize].op {
            Op::Number(..) => {
                let value = self.value(node);
                Poly::constant(value.lanes, value.bits)
            }
            Op::Literal(kind, _) if &**kind == "null" => Poly::zero(),
            Op::Column(_)
            | Op::Literal(..)
            | Op::Function(_)
            | Op::Symmetric(_)
            | Op::Window(_) => Poly::atom(&field, self.value(node).lanes, false),
            Op::Present | Op::Outer(_) => (*self.poly(children[0])).clone(),
            Op::Ref(target) => (*self.poly(program.definitions[*target as usize].root)).clone(),
            Op::Add => {
                let mut sum = Poly::zero();
                for c in children {
                    sum = sum.add(&self.poly(c), &field)?;
                }
                sum
            }
            Op::Mul => {
                let mut product = Poly::one(&field);
                for c in children {
                    product = product.mul(&self.poly(c), &field)?;
                }
                product
            }
            Op::Sub => self
                .poly(children[0])
                .sub(&self.poly(children[1]), &field)?,
            Op::Neg => self.poly(children[0]).neg(&field),
            Op::Pow(exponent) => {
                let base = self.poly(children[0]);
                let mut product = Poly::one(&field);
                for _ in 0..*exponent {
                    product = product.mul(&base, &field)?;
                }
                product
            }
            Op::Div => {
                let numerator = self.poly(children[0]);
                let divisor = self.poly(children[1]);
                let constant = divisor
                    .as_constant()
                    .filter(|c| !c.0.contains(&0))
                    .map(|c| field.inverse(c).0);
                match constant {
                    Some(inverse) => numerator.scale(inverse, divisor.bits, &field),
                    // 1/x for a non-constant x is an atom: SUM(a/x) is linear in a only.
                    None => {
                        let inverse = self.opaque(children[1], Tag::Inverse);
                        numerator.mul(&inverse, &field)?
                    }
                }
            }
            Op::Case => {
                let kept = self.kept(&children);
                let mut result = (*self.poly(*children.last().expect("an ELSE"))).clone();
                for &(_, branch, indicator) in kept.iter().rev() {
                    let t = Poly::atom(&field, indicator, true);
                    let rest = t.complement(&field)?.mul(&result, &field)?;
                    result = t.mul(&self.poly(branch), &field)?.add(&rest, &field)?;
                }
                result
            }
            Op::Coalesce => {
                let mut result = (*self.poly(*children.last().expect("an argument"))).clone();
                for &c in children[..children.len() - 1].iter().rev() {
                    let present = self.value(c).presence;
                    let rest = present.complement(&field)?.mul(&result, &field)?;
                    result = present.mul(&self.poly(c), &field)?.add(&rest, &field)?;
                }
                result
            }
            // An aggregate inside an aggregate is one value of the outer rows.
            Op::Sum | Op::Avg | Op::Count | Op::CountOf | Op::Aggregate(_) => {
                self.opaque(node, Tag::Nested)
            }
        })
    }
}

/// A bound on the probability that two values compare the same although the formulas differ;
/// 1, no guarantee, when a divisor was zero at some trial's point. See the module.
pub fn error_bound(a: &Val, b: &Val, field: &Field) -> f64 {
    if a.singular || b.singular {
        return 1.0;
    }
    let (da, ba) = a.reach_of();
    let (db, bb) = b.reach_of();
    let degree = a
        .num
        .saturating_add(a.den)
        .saturating_add(b.num)
        .saturating_add(b.den)
        .max(da)
        .max(db)
        .max(1);
    let bits = a
        .bits
        .saturating_add(b.bits)
        .saturating_add(1)
        .max(ba)
        .max(bb)
        .max(
            a.presence
                .bits
                .saturating_add(b.presence.bits)
                .saturating_add(1),
        );
    let per_lane = f64::from(degree) / field.smallest() as f64
        + (f64::from(bits) / 60.0 + 1.0) / (PRIMES_IN_RANGE - 2.0);
    let n = f64::from(a.apps) + f64::from(b.apps);
    let pairs = 1.0 + n * (n - 1.0).max(0.0) / 2.0;
    (pairs * per_lane.powi(LANES as i32)).min(1.0)
}
