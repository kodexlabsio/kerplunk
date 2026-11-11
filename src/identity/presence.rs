//! Presence: when a value is not NULL, a Boolean function of Boolean atoms (a column being
//! present, a CASE condition holding), kept as its unique multilinear polynomial while that is
//! small, and past that as a canonical form of what it is made of.
//!
//! A presence that is not a product (a column's, a CASE's, a COALESCE's) is a leaf: the atoms
//! in every term of it, each a leaf of its own, and the rest. A product of presences (a value
//! that is NULL when any of its operands is) is the set of its operands' leaves, without
//! repeats, as a Boolean function times itself is itself. Whatever the order and grouping of
//! the operands, the same leaves make the same presence, worked out from them alone:
//!
//! * Leaves that share an atom, directly or through other leaves, are one class; classes share
//!   no atom, so each is worked out on its own and the presence is their product.
//! * In a class, the implied atoms are the least set that holds the atom leaves, the atoms in
//!   every term of a factor, and the atoms in every term of a part's product; each factor is a
//!   leaf's rest with the implied atoms set to 1; a part is factors that share atoms; and a
//!   part's product is worked out when the product of its factors' numbers of terms is at most
//!   [`ATTEMPT`], so that every step of it has at most that many terms in any order, and kept
//!   when it has at most [`MAX_TERMS`]. Setting an atom to 1 never adds terms, so a part only
//!   comes within these limits as atoms are found, and the least set is one set.
//! * A class is multiplied out when its parts are and their product has at most [`MAX_TERMS`]
//!   terms; the presence is multiplied out when its classes are and theirs has.
//!
//! So `a + COALESCE(a, b)` is present exactly where `a` is, and `a + CASE WHEN c > 0 THEN a ELSE
//! 0 END` where `a` is, whatever the order. An atom that only a product past the limits would
//! show is not found, the same way however the operands are arranged. Multiplied out, two
//! presences are the same exactly when their polynomials are; past the limit, when their atoms
//! and their classes that are more than atoms are, compared by the count of those classes and
//! the sum of their identities, lane by lane, wrapping. Under a fresh key each identity is uniform in its lane, so two
//! different sets of classes have the same sums with probability at most about 2^-183, far
//! within the error a verdict reports. Under the fixed key of fingerprints, sums can be forged
//! with a few thousand classes in about 2^30 steps by Wagner's k-tree algorithm, which is one
//! more reason a fixed-key fingerprint, as [`super::oracle`] says, is no guarantee against a
//! formula built to collide.
//!
//! Classes are kept in persistent maps (see [`super::pmap`]): a product that adds a leaf
//! sharing no atom with a presence of many shares all of theirs, so a lineage of n such products
//! costs n log n. A leaf that shares atoms with classes works them out again, from their leaves.

use std::sync::{Arc, OnceLock};

use super::field::{Field, Lanes};
use super::oracle::{Feed, Oracle, Tag};
use super::pmap::Map;
use super::poly::{Atom, MAX_TERMS, Monomial, Poly};
use crate::fnv::FnvHashMap;

/// The most terms a step of a part's product may have; see the module.
pub const ATTEMPT: usize = 4096;

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

fn feed_atoms(f: &mut Feed, atoms: &[Atom]) {
    f.number(atoms.len() as u64);
    for (atom, boolean) in atoms {
        f.lanes(atom).number(u64::from(*boolean));
    }
}

/// A polynomial with its identity, drawn once for whatever presences share it.
#[derive(Debug)]
struct Rest {
    poly: Arc<Poly>,
    identity: OnceLock<Lanes>,
}

impl Rest {
    fn new(poly: Poly) -> Arc<Rest> {
        Arc::new(Rest {
            poly: Arc::new(poly),
            identity: OnceLock::new(),
        })
    }

    fn identity(&self, oracle: &Oracle) -> Lanes {
        *self
            .identity
            .get_or_init(|| oracle.lanes(Tag::Rest, |f| feed_poly(f, &self.poly)))
    }
}

/// The rest of a leaf: no atom in every term of it.
#[derive(Debug)]
struct Leaf {
    rest: Poly,
    /// Every atom in it, sorted.
    atoms: Vec<Lanes>,
    /// Drawn when the leaf first shares a class with another.
    identity: OnceLock<Lanes>,
}

impl Leaf {
    fn new(rest: Poly) -> Arc<Leaf> {
        Arc::new(Leaf {
            atoms: support(&rest),
            identity: OnceLock::new(),
            rest,
        })
    }

    fn identity(&self, oracle: &Oracle) -> Lanes {
        *self
            .identity
            .get_or_init(|| oracle.lanes(Tag::Leaf, |f| feed_poly(f, &self.rest)))
    }
}

fn support(poly: &Poly) -> Vec<Lanes> {
    let mut atoms: Vec<Lanes> = poly
        .terms()
        .flat_map(|(m, _)| m.iter().map(|(atom, _)| *atom))
        .collect();
    atoms.sort_unstable();
    atoms.dedup();
    atoms
}

/// Leaves that share atoms, directly or through one another, worked out.
#[derive(Debug)]
struct Class {
    /// Sorted by identity, without repeats.
    leaves: Vec<Arc<Leaf>>,
    /// The atoms that are leaves of their own, each in some leaf's rest, sorted.
    atom_leaves: Monomial,
    /// Every atom in the leaves, sorted.
    atoms: Vec<Lanes>,
    /// The implied atoms, sorted.
    common: Monomial,
    /// The parts multiplied out, while that has at most [`MAX_TERMS`] terms.
    whole: Option<Arc<Rest>>,
    identity: Lanes,
}

impl Class {
    fn trivial(&self) -> bool {
        self.whole
            .as_ref()
            .is_some_and(|w| w.poly.as_constant().is_some())
    }
}

/// The class of these leaves, or None when it is never present.
fn class(oracle: &Oracle, mut leaves: Vec<Arc<Leaf>>, atom_leaves: Monomial) -> Option<Arc<Class>> {
    let field = oracle.field;
    if leaves.len() > 1 {
        leaves.sort_by_cached_key(|l| l.identity(oracle));
        leaves.dedup_by_key(|l| l.identity(oracle));
    }
    let atoms = leaves
        .iter()
        .fold(Vec::new(), |all, l| union(&all, &l.atoms));
    let mut common = atom_leaves.clone();
    let (factors, parts) = loop {
        let mut grown = false;
        let mut factors = Vec::new();
        for leaf in &leaves {
            let factor = leaf
                .rest
                .with_true(&common, &field)
                .unwrap_or_else(|| leaf.rest.clone());
            if factor.is_zero() {
                return None;
            }
            let more = factor.common();
            if !more.is_empty() {
                common = union(&common, &more);
                grown = true;
            }
            if !grown {
                let factor = if more.is_empty() {
                    factor
                } else {
                    factor.strip(&more)
                };
                if !factor.is_one(&field) {
                    factors.push(factor);
                }
            }
        }
        if grown {
            continue;
        }
        let mut parts: Vec<(Vec<usize>, Option<Poly>)> = Vec::new();
        for members in connected(&factors) {
            let terms = members
                .iter()
                .try_fold(1usize, |n, &i| n.checked_mul(factors[i].len()))
                .filter(|&n| n <= ATTEMPT);
            let product = terms.and_then(|_| {
                members.iter().try_fold(Poly::one(&field), |p, &i| {
                    p.mul_within(&factors[i], ATTEMPT, &field)
                })
            });
            let product = product.filter(|p| p.len() <= MAX_TERMS);
            if let Some(p) = &product {
                if p.is_zero() {
                    return None;
                }
                let more = p.common();
                if !more.is_empty() {
                    common = union(&common, &more);
                    grown = true;
                }
            }
            parts.push((members, product));
        }
        if !grown {
            break (factors, parts);
        }
    };
    // Parts share no atom: their product has as many terms as theirs multiplied, and every step
    // of it fewer, in any order.
    let terms = parts.iter().try_fold(1usize, |n, (_, p)| {
        p.as_ref().and_then(|p| n.checked_mul(p.len()))
    });
    let whole = match terms {
        Some(n) if n <= MAX_TERMS => Some(Rest::new(
            parts
                .iter()
                .filter_map(|(_, p)| p.as_ref())
                .try_fold(Poly::one(&field), |p, q| p.mul(q, &field))
                .expect("a product within the limit"),
        )),
        _ => None,
    };
    let identity = oracle.lanes(Tag::Factor, |f| {
        feed_atoms(f, &common);
        match &whole {
            Some(w) => {
                f.number(0);
                feed_poly(f, &w.poly);
            }
            None => {
                // Past the limit, the parts as a set: a sum of their identities.
                let sum = parts
                    .iter()
                    .fold(Lanes::default(), |sum, (members, product)| {
                        let identity = match product {
                            Some(p) => oracle.lanes(Tag::Rest, |g| feed_poly(g, p)),
                            None => {
                                let each = members.iter().fold(Lanes::default(), |s, &i| {
                                    plus(s, oracle.lanes(Tag::Rest, |g| feed_poly(g, &factors[i])))
                                });
                                oracle.lanes(Tag::BothPresence, |g| {
                                    g.number(members.len() as u64).lanes(&each);
                                })
                            }
                        };
                        plus(sum, identity)
                    });
                f.number(1).number(parts.len() as u64).lanes(&sum);
            }
        }
    });
    Some(Arc::new(Class {
        leaves,
        atom_leaves,
        atoms,
        common,
        whole,
        identity,
    }))
}

/// The factors that share atoms, directly or through one another, as lists of their indices.
fn connected(factors: &[Poly]) -> Vec<Vec<usize>> {
    if factors.len() < 2 {
        return (0..factors.len()).map(|i| vec![i]).collect();
    }
    let mut parent: Vec<usize> = (0..factors.len()).collect();
    fn root(parent: &mut [usize], mut i: usize) -> usize {
        while parent[i] != i {
            parent[i] = parent[parent[i]];
            i = parent[i];
        }
        i
    }
    let mut first: FnvHashMap<Lanes, usize> = FnvHashMap::default();
    for (i, factor) in factors.iter().enumerate() {
        for atom in support(factor) {
            match first.get(&atom) {
                Some(&j) => {
                    let (a, b) = (root(&mut parent, i), root(&mut parent, j));
                    parent[a] = b;
                }
                None => {
                    first.insert(atom, i);
                }
            }
        }
    }
    let mut groups: FnvHashMap<usize, Vec<usize>> = FnvHashMap::default();
    for i in 0..factors.len() {
        let r = root(&mut parent, i);
        groups.entry(r).or_default().push(i);
    }
    let mut groups: Vec<Vec<usize>> = groups.into_values().collect();
    groups.sort_unstable();
    groups
}

#[derive(Clone, Debug)]
pub struct Presence(Arc<Shape>);

#[derive(Debug)]
struct Shape {
    /// The atoms in every term, sorted: atom leaves in no class, and each class's implied atoms.
    common: Monomial,
    classes: Map<Arc<Class>>,
    /// The classes that are not multiplied out to a constant, which make up the polynomial.
    rests: Map<Arc<Class>>,
    /// The class each atom of a class's leaves is in.
    index: Map<Lanes>,
    /// The identities of the classes in `rests` summed lane by lane, wrapping: a class that is
    /// 1 but for its atoms is no more than those, which `common` holds.
    sum: Lanes,
    /// The classes multiplied out, while that has at most [`MAX_TERMS`] terms.
    whole: Option<Arc<Rest>>,
    /// Past the limit, a Boolean atom standing for exactly the classes' product.
    stand_in: OnceLock<Lanes>,
}

fn plus(a: Lanes, b: Lanes) -> Lanes {
    Lanes(std::array::from_fn(|i| a.0[i].wrapping_add(b.0[i])))
}

fn minus(a: Lanes, b: Lanes) -> Lanes {
    Lanes(std::array::from_fn(|i| a.0[i].wrapping_sub(b.0[i])))
}

/// The union of two sorted lists without repeats.
fn union<T: Ord + Copy>(a: &[T], b: &[T]) -> Vec<T> {
    let mut out = Vec::with_capacity(a.len() + b.len());
    let (mut i, mut j) = (0, 0);
    while i < a.len() && j < b.len() {
        match a[i].cmp(&b[j]) {
            std::cmp::Ordering::Less => {
                out.push(a[i]);
                i += 1;
            }
            std::cmp::Ordering::Greater => {
                out.push(b[j]);
                j += 1;
            }
            std::cmp::Ordering::Equal => {
                out.push(a[i]);
                i += 1;
                j += 1;
            }
        }
    }
    out.extend_from_slice(&a[i..]);
    out.extend_from_slice(&b[j..]);
    out
}

/// What is in `a` and not in `b`, both sorted.
fn without<T: Ord + Copy>(a: &[T], b: &[T]) -> Vec<T> {
    a.iter()
        .filter(|x| b.binary_search(x).is_err())
        .copied()
        .collect()
}

impl Presence {
    /// The presence that is a constant polynomial, 0 or 1.
    pub fn constant(poly: Poly) -> Presence {
        Presence(Arc::new(Shape {
            common: Monomial::new(),
            classes: Map::default(),
            rests: Map::default(),
            index: Map::default(),
            sum: Lanes::default(),
            whole: Some(Rest::new(poly)),
            stand_in: OnceLock::new(),
        }))
    }

    /// The presence that is one Boolean atom; `one` is the presence 1.
    pub fn atom(identity: Lanes, one: &Presence) -> Presence {
        Presence(Arc::new(Shape {
            common: vec![(identity, true)],
            classes: Map::default(),
            rests: Map::default(),
            index: Map::default(),
            sum: Lanes::default(),
            whole: one.0.whole.clone(),
            stand_in: OnceLock::new(),
        }))
    }

    /// The presence `poly` is, a polynomial over Boolean atoms: a leaf.
    pub fn of(poly: Poly, oracle: &Oracle, one: &Presence, zero: &Presence) -> Presence {
        if poly.is_zero() {
            return zero.clone();
        }
        let common = poly.common();
        let rest = if common.is_empty() {
            poly
        } else {
            poly.strip(&common)
        };
        let mut builder = Builder::new(oracle, &one.0);
        builder.common = common;
        if !rest.is_one(&oracle.field) {
            let made = class(oracle, vec![Leaf::new(rest)], Monomial::new());
            match made {
                Some(made) => builder.put(made),
                None => return zero.clone(),
            }
        }
        builder.finish(&one.0, one, zero)
    }

    /// The presence of a value that is NULL when either of two is: their product.
    pub fn and(
        &self,
        other: &Presence,
        oracle: &Oracle,
        one: &Presence,
        zero: &Presence,
    ) -> Presence {
        let field = &oracle.field;
        if self.is_zero() || other.is_one(field) {
            return self.clone();
        }
        if other.is_zero() || self.is_one(field) {
            return other.clone();
        }
        // The one with more classes is the base: the other's leaves are added to it.
        let (base, more) = if self.0.classes.len() >= other.0.classes.len() {
            (self, other)
        } else {
            (other, self)
        };
        let mut builder = Builder::new(oracle, &base.0);
        for (atom, _) in &more.0.common {
            if more.0.index.get(atom).is_none() {
                builder.atom(*atom);
            }
        }
        more.0.classes.for_each(|_, c| builder.class(c));
        builder.finish(&base.0, one, zero)
    }

    pub fn is_zero(&self) -> bool {
        self.0.whole.as_ref().is_some_and(|w| w.poly.is_zero())
    }

    pub fn is_one(&self, field: &Field) -> bool {
        self.0.common.is_empty() && self.0.whole.as_ref().is_some_and(|w| w.poly.is_one(field))
    }

    /// The degree of its polynomial, as a consumer sees it.
    pub fn degree(&self) -> u32 {
        let rest = self.0.whole.as_ref().map_or(1, |w| w.poly.degree());
        (self.0.common.len() as u32).saturating_add(rest)
    }

    /// A bound on the bits of its coefficients.
    pub fn bits(&self) -> u32 {
        self.0.whole.as_ref().map_or(1, |w| w.poly.bits)
    }

    fn stand_in(&self, oracle: &Oracle) -> Lanes {
        *self.0.stand_in.get_or_init(|| {
            oracle.lanes(Tag::Product, |f| {
                f.number(self.0.rests.len() as u64).lanes(&self.0.sum);
            })
        })
    }

    /// Its polynomial: multiplied out, or past the limit its atoms times the atom standing for
    /// its classes.
    pub fn poly(&self, oracle: &Oracle) -> Arc<Poly> {
        let field = &oracle.field;
        let common = &self.0.common;
        match &self.0.whole {
            Some(w) if common.is_empty() => w.poly.clone(),
            Some(w) => Arc::new(w.poly.times(common, field)),
            None => Arc::new(Poly::atom(field, self.stand_in(oracle), true).times(common, field)),
        }
    }

    /// The value of that polynomial when each atom takes the value of its identity.
    pub fn evaluate(&self, oracle: &Oracle) -> Lanes {
        let field = &oracle.field;
        let rest = match &self.0.whole {
            Some(w) => w.poly.evaluate(field),
            None => self.stand_in(oracle),
        };
        self.0
            .common
            .iter()
            .fold(rest, |p, (atom, _)| field.mul(p, *atom))
    }

    /// Feeds it to a draw as what it is, not how it was made.
    pub fn feed(&self, oracle: &Oracle, f: &mut Feed) {
        feed_atoms(f, &self.0.common);
        match &self.0.whole {
            Some(w) => f.number(0).lanes(&w.identity(oracle)),
            None => f.number(1).lanes(&self.stand_in(oracle)),
        };
    }

    /// What it is compared by: multiplied out, its polynomial alone, however its leaves make it
    /// up; past the limit, its classes.
    fn key(&self) -> (&Monomial, Option<&Poly>, usize, Lanes) {
        let shape = &self.0;
        match &shape.whole {
            Some(w) => (&shape.common, Some(&*w.poly), 0, Lanes::default()),
            None => (&shape.common, None, shape.rests.len(), shape.sum),
        }
    }
}

impl PartialEq for Presence {
    fn eq(&self, other: &Presence) -> bool {
        Arc::ptr_eq(&self.0, &other.0) || self.key() == other.key()
    }
}

impl Eq for Presence {}

impl PartialOrd for Presence {
    fn partial_cmp(&self, other: &Presence) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Presence {
    fn cmp(&self, other: &Presence) -> std::cmp::Ordering {
        self.key().cmp(&other.key())
    }
}

/// A presence being made from another, its maps shared until they change.
struct Builder<'o> {
    oracle: &'o Oracle,
    common: Monomial,
    classes: Map<Arc<Class>>,
    rests: Map<Arc<Class>>,
    index: Map<Lanes>,
    sum: Lanes,
    zero: bool,
}

impl<'o> Builder<'o> {
    fn new(oracle: &'o Oracle, shape: &Shape) -> Builder<'o> {
        Builder {
            oracle,
            common: shape.common.clone(),
            classes: shape.classes.clone(),
            rests: shape.rests.clone(),
            index: shape.index.clone(),
            sum: shape.sum,
            zero: false,
        }
    }

    fn take(&mut self, id: &Lanes) -> Arc<Class> {
        let taken = self.classes.remove(id).expect("an indexed class");
        if self.rests.remove(id).is_some() {
            self.sum = minus(self.sum, taken.identity);
        }
        for atom in &taken.atoms {
            self.index.remove(atom);
        }
        self.common = without(&self.common, &taken.common);
        taken
    }

    fn put(&mut self, class: Arc<Class>) {
        for atom in &class.atoms {
            self.index.insert(*atom, class.identity);
        }
        self.common = union(&self.common, &class.common);
        if !class.trivial() {
            self.sum = plus(self.sum, class.identity);
            self.rests.insert(class.identity, class.clone());
        }
        self.classes.insert(class.identity, class);
    }

    /// Works out again, with more leaves, the classes these atoms are in.
    fn merge(&mut self, atoms: &[Lanes], mut leaves: Vec<Arc<Leaf>>, mut atom_leaves: Monomial) {
        for atom in atoms {
            if let Some(id) = self.index.get(atom).copied() {
                let taken = self.take(&id);
                leaves.extend(taken.leaves.iter().cloned());
                atom_leaves = union(&atom_leaves, &taken.atom_leaves);
            } else if self.common.binary_search(&(*atom, true)).is_ok() {
                // An atom leaf in no class, which this one's leaves have.
                atom_leaves = union(&atom_leaves, &[(*atom, true)]);
            }
        }
        match class(self.oracle, leaves, atom_leaves) {
            Some(made) => self.put(made),
            None => self.zero = true,
        }
    }

    /// An atom that is a leaf of another presence, in none of its classes.
    fn atom(&mut self, atom: Lanes) {
        match self.index.get(&atom).copied() {
            Some(id) => {
                let found = self.classes.get(&id).expect("an indexed class");
                if found.atom_leaves.binary_search(&(atom, true)).is_err() {
                    let taken = self.take(&id);
                    let atom_leaves = union(&taken.atom_leaves, &[(atom, true)]);
                    match class(self.oracle, taken.leaves.clone(), atom_leaves) {
                        Some(made) => self.put(made),
                        None => self.zero = true,
                    }
                }
            }
            None => self.common = union(&self.common, &[(atom, true)]),
        }
    }

    /// A class of another presence: kept whole when it shares no atom with this one.
    fn class(&mut self, class: &Arc<Class>) {
        let apart = class.atoms.iter().all(|a| {
            self.index.get(a).is_none() && self.common.binary_search(&(*a, true)).is_err()
        });
        if apart {
            self.put(class.clone());
        } else {
            self.merge(
                &class.atoms,
                class.leaves.clone(),
                class.atom_leaves.clone(),
            );
        }
    }

    fn finish(self, base: &Shape, one: &Presence, zero: &Presence) -> Presence {
        if self.zero {
            return zero.clone();
        }
        let field = self.oracle.field;
        let whole = if self.rests.is_empty() {
            one.0.whole.clone()
        } else if self.rests.same(&base.rests) {
            base.whole.clone()
        } else {
            // Classes share no atom: their product has as many terms as theirs multiplied, at
            // least two each, so more than eight of them are past the limit.
            let mut parts = Vec::new();
            if self.rests.len() <= 8 {
                self.rests.for_each(|_, c| parts.push(c.clone()));
            }
            let terms = parts.iter().try_fold(1usize, |n, c| {
                let whole = c.whole.as_ref()?;
                n.checked_mul(whole.poly.len())
            });
            match (terms, &parts[..]) {
                (Some(_), [only]) => only.whole.clone(),
                (Some(n), _) if n <= MAX_TERMS && !parts.is_empty() => parts
                    .iter()
                    .filter_map(|c| c.whole.as_ref())
                    .try_fold(Poly::one(&field), |p, r| p.mul(&r.poly, &field))
                    .map(Rest::new),
                _ => None,
            }
        };
        Presence(Arc::new(Shape {
            common: self.common,
            classes: self.classes,
            rests: self.rests,
            index: self.index,
            sum: self.sum,
            whole,
            stand_in: OnceLock::new(),
        }))
    }
}
