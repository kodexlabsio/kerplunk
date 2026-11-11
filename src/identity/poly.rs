//! Sparse polynomials over atoms, for the argument of an aggregate and for presence.
//!
//! SUM is linear over rows, so SUM(e) is determined by e's coefficients once e is expanded into
//! monomials over the row's atoms (its columns and uninterpreted values). Each monomial is a
//! sorted list of atom identities, so two ways of writing the same expression expand to the
//! same polynomial.
//!
//! An atom is numeric or Boolean. A Boolean atom (a column being present, a CASE condition
//! holding) takes only the values 0 and 1, so it is idempotent: a monomial holds it at most
//! once. Over Boolean atoms alone the polynomial is then multilinear, and every Boolean
//! function has exactly one multilinear polynomial, so presence is compared exactly.
//!
//! Coefficients are integers held in every lane's field; `bits` bounds their size, for the
//! error bound (a coefficient vanishes in a lane only if that lane's prime divides it).

use std::collections::BTreeMap;

use super::field::{Field, Lanes};

/// The most terms an expansion may have. Beyond it, the expression is kept whole as one atom,
/// which is still sound: it stands for exactly that expression, only without linearity.
pub const MAX_TERMS: usize = 256;

/// An atom: its identity, and whether it is Boolean.
pub type Atom = (Lanes, bool);

/// A product of atoms, sorted; a numeric atom repeats for its power, a Boolean one never does.
pub type Monomial = Vec<Atom>;

#[derive(Clone, Debug, Default)]
pub struct Poly {
    terms: BTreeMap<Monomial, Lanes>,
    /// A bound on log2 of the sum of the coefficients' absolute values.
    pub bits: u32,
}

// Two polynomials are equal, and ordered, by their terms alone; `bits` is only a bound.
impl PartialEq for Poly {
    fn eq(&self, other: &Poly) -> bool {
        self.terms == other.terms
    }
}

impl Eq for Poly {}

impl PartialOrd for Poly {
    fn partial_cmp(&self, other: &Poly) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Poly {
    fn cmp(&self, other: &Poly) -> std::cmp::Ordering {
        self.terms.cmp(&other.terms)
    }
}

/// A bound on the bits of `digits`, a decimal integer: 10/3 per digit, plus one.
pub fn decimal_bits(digits: &str) -> u32 {
    (digits.trim_start_matches('-').len() as u32).saturating_mul(10) / 3 + 1
}

impl Poly {
    pub fn zero() -> Poly {
        Poly::default()
    }

    /// The constant `c`, an integer or rational of at most `bits` bits.
    pub fn constant(c: Lanes, bits: u32) -> Poly {
        let mut terms = BTreeMap::new();
        if c != Lanes::default() {
            terms.insert(Vec::new(), c);
        }
        Poly { terms, bits }
    }

    pub fn one(field: &Field) -> Poly {
        Poly::constant(field.one(), 1)
    }

    pub fn atom(field: &Field, identity: Lanes, boolean: bool) -> Poly {
        Poly {
            terms: BTreeMap::from([(vec![(identity, boolean)], field.one())]),
            bits: 1,
        }
    }

    /// The polynomial's value when it is a constant.
    pub fn as_constant(&self) -> Option<Lanes> {
        match self.terms.len() {
            0 => Some(Lanes::default()),
            1 => self.terms.get(&Vec::new()).copied(),
            _ => None,
        }
    }

    pub fn is_zero(&self) -> bool {
        self.terms.is_empty()
    }

    pub fn is_one(&self, field: &Field) -> bool {
        self.as_constant() == Some(field.one())
    }

    pub fn terms(&self) -> impl Iterator<Item = (&Monomial, Lanes)> {
        self.terms.iter().map(|(m, &c)| (m, c))
    }

    pub fn len(&self) -> usize {
        self.terms.len()
    }

    pub fn is_empty(&self) -> bool {
        self.terms.is_empty()
    }

    /// The highest number of atoms in a monomial: the polynomial's degree.
    pub fn degree(&self) -> u32 {
        self.terms.keys().map(|m| m.len() as u32).max().unwrap_or(0)
    }

    /// The polynomial's value when each atom takes the value of its identity.
    pub fn evaluate(&self, field: &Field) -> Lanes {
        self.terms.iter().fold(field.zero(), |sum, (monomial, &c)| {
            let product = monomial.iter().fold(c, |p, (atom, _)| field.mul(p, *atom));
            field.add(sum, product)
        })
    }

    fn accumulate(
        field: &Field,
        terms: &mut BTreeMap<Monomial, Lanes>,
        monomial: Monomial,
        c: Lanes,
    ) {
        let zero = Lanes::default();
        match terms.entry(monomial) {
            std::collections::btree_map::Entry::Occupied(mut found) => {
                let sum = field.add(*found.get(), c);
                if sum == zero {
                    found.remove();
                } else {
                    *found.get_mut() = sum;
                }
            }
            std::collections::btree_map::Entry::Vacant(slot) => {
                if c != zero {
                    slot.insert(c);
                }
            }
        }
    }

    /// The sum, or None when it would have more than [`MAX_TERMS`] terms.
    pub fn add(&self, other: &Poly, field: &Field) -> Option<Poly> {
        let mut terms = self.terms.clone();
        for (m, &c) in &other.terms {
            Poly::accumulate(field, &mut terms, m.clone(), c);
        }
        (terms.len() <= MAX_TERMS).then(|| Poly {
            terms,
            bits: self.bits.max(other.bits).saturating_add(1),
        })
    }

    pub fn scale(&self, c: Lanes, bits: u32, field: &Field) -> Poly {
        if c == Lanes::default() {
            return Poly::zero();
        }
        Poly {
            terms: self
                .terms
                .iter()
                .map(|(m, &k)| (m.clone(), field.mul(k, c)))
                .collect(),
            bits: self.bits.saturating_add(bits),
        }
    }

    pub fn neg(&self, field: &Field) -> Poly {
        Poly {
            terms: self
                .terms
                .iter()
                .map(|(m, &k)| (m.clone(), field.neg(k)))
                .collect(),
            bits: self.bits,
        }
    }

    pub fn sub(&self, other: &Poly, field: &Field) -> Option<Poly> {
        self.add(&other.neg(field), field)
    }

    /// The product, or None when it would have more than [`MAX_TERMS`] terms. A Boolean atom in
    /// both factors appears once in the product.
    pub fn mul(&self, other: &Poly, field: &Field) -> Option<Poly> {
        self.mul_within(other, MAX_TERMS, field)
    }

    /// The product, or None when it would have more than `limit` terms.
    pub fn mul_within(&self, other: &Poly, limit: usize, field: &Field) -> Option<Poly> {
        if self.terms.len().saturating_mul(other.terms.len()) > limit.saturating_mul(16) {
            return None;
        }
        let mut terms = BTreeMap::new();
        for (m1, &c1) in &self.terms {
            for (m2, &c2) in &other.terms {
                let mut monomial = Vec::with_capacity(m1.len() + m2.len());
                monomial.extend_from_slice(m1);
                monomial.extend_from_slice(m2);
                monomial.sort_unstable();
                monomial.dedup_by(|a, b| a.1 && a == b);
                Poly::accumulate(field, &mut terms, monomial, field.mul(c1, c2));
            }
        }
        (terms.len() <= limit).then(|| Poly {
            terms,
            bits: self.bits.saturating_add(other.bits),
        })
    }

    /// 1 - self.
    pub fn complement(&self, field: &Field) -> Option<Poly> {
        Poly::one(field).sub(self, field)
    }

    /// The Boolean atoms in every term, sorted; none for zero.
    pub fn common(&self) -> Monomial {
        let mut monomials = self.terms.keys();
        let mut common = monomials.next().cloned().unwrap_or_default();
        for m in monomials {
            if common.is_empty() {
                break;
            }
            common.retain(|a| m.binary_search(a).is_ok());
        }
        common.retain(|a| a.1);
        common
    }

    /// Divided by `atoms`, sorted Boolean atoms in every term.
    pub fn strip(&self, atoms: &[Atom]) -> Poly {
        Poly {
            terms: self
                .terms
                .iter()
                .map(|(m, &c)| {
                    let rest = m.iter().filter(|a| atoms.binary_search(a).is_err());
                    (rest.copied().collect(), c)
                })
                .collect(),
            bits: self.bits,
        }
    }

    /// With each of `atoms`, sorted Boolean atoms, set to 1; None when no term has one.
    pub fn with_true(&self, atoms: &[Atom], field: &Field) -> Option<Poly> {
        let touches = |m: &Monomial| m.iter().any(|a| atoms.binary_search(a).is_ok());
        if atoms.is_empty() || !self.terms.keys().any(touches) {
            return None;
        }
        let mut terms = BTreeMap::new();
        for (m, &c) in &self.terms {
            let rest = m.iter().filter(|a| atoms.binary_search(a).is_err());
            Poly::accumulate(field, &mut terms, rest.copied().collect(), c);
        }
        // Terms that merge have coefficients no larger in sum: the bound still holds.
        Some(Poly {
            terms,
            bits: self.bits,
        })
    }

    /// Times the product of `atoms`, sorted Boolean atoms.
    pub fn times(&self, atoms: &[Atom], field: &Field) -> Poly {
        let mut terms = BTreeMap::new();
        for (m, &c) in &self.terms {
            let mut monomial = Vec::with_capacity(m.len() + atoms.len());
            monomial.extend_from_slice(m);
            monomial.extend_from_slice(atoms);
            monomial.sort_unstable();
            monomial.dedup_by(|a, b| a.1 && a == b);
            Poly::accumulate(field, &mut terms, monomial, c);
        }
        Poly {
            terms,
            bits: self.bits,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::super::oracle::Oracle;
    use super::*;

    fn field() -> Field {
        Oracle::default().field
    }

    fn atom(field: &Field, i: u64, boolean: bool) -> Poly {
        Poly::atom(field, field.integer(1000 + i), boolean)
    }

    #[test]
    fn expansion_is_canonical() {
        let f = field();
        let (a, b, c) = (atom(&f, 1, false), atom(&f, 2, false), atom(&f, 3, false));
        // (a + b)(a - b) = a^2 - b^2
        let left = a
            .add(&b, &f)
            .unwrap()
            .mul(&a.sub(&b, &f).unwrap(), &f)
            .unwrap();
        let right = a
            .mul(&a, &f)
            .unwrap()
            .sub(&b.mul(&b, &f).unwrap(), &f)
            .unwrap();
        assert!(left.eq(&right));
        // a(b + c) = ab + ca
        let left = a.mul(&b.add(&c, &f).unwrap(), &f).unwrap();
        let right = c
            .mul(&a, &f)
            .unwrap()
            .add(&b.mul(&a, &f).unwrap(), &f)
            .unwrap();
        assert!(left.eq(&right));
        assert!(a.sub(&a, &f).unwrap().is_zero());
        assert_eq!(Poly::zero().as_constant(), Some(Lanes::default()));
        assert!(Poly::one(&f).is_one(&f));
        assert_eq!(a.as_constant(), None);
        assert!(a.scale(Lanes::default(), 0, &f).is_zero());
        assert_eq!(a.mul(&a, &f).unwrap().degree(), 2);
        let value = a.add(&b, &f).unwrap().evaluate(&f);
        assert_eq!(value, f.add(f.integer(1001), f.integer(1002)));
    }

    #[test]
    fn boolean_atoms_are_idempotent() {
        let f = field();
        let (t, u) = (atom(&f, 1, true), atom(&f, 2, true));
        // t * t = t, and t (1 - t) = 0
        assert!(t.mul(&t, &f).unwrap().eq(&t));
        assert!(t.mul(&t.complement(&f).unwrap(), &f).unwrap().is_zero());
        // De Morgan, exactly: 1 - (1 - t)(1 - u) = t + u - tu, whichever way it is built
        let neither = t
            .complement(&f)
            .unwrap()
            .mul(&u.complement(&f).unwrap(), &f)
            .unwrap();
        let either = neither.complement(&f).unwrap();
        let direct = t
            .add(&u, &f)
            .unwrap()
            .sub(&t.mul(&u, &f).unwrap(), &f)
            .unwrap();
        assert!(either.eq(&direct));
        // A numeric atom is not idempotent.
        let a = atom(&f, 3, false);
        assert!(!a.mul(&a, &f).unwrap().eq(&a));
    }

    #[test]
    fn expansions_past_the_limit_are_refused() {
        let f = field();
        let mut sum = Poly::zero();
        for i in 0..20 {
            sum = sum.add(&atom(&f, 100 * i, false), &f).unwrap();
        }
        let square = sum.mul(&sum, &f).unwrap();
        assert_eq!(square.len(), 20 * 21 / 2);
        assert!(square.mul(&sum, &f).is_none());
        assert_eq!(decimal_bits("-1000"), 14);
    }

    #[test]
    fn the_atoms_in_every_term_split_off_and_back() {
        let f = field();
        let (a, b, c, d) = (
            atom(&f, 1, true),
            atom(&f, 2, true),
            atom(&f, 3, true),
            atom(&f, 4, true),
        );
        // (ab)(1 - c + cd): a and b are in every term, c and d are not.
        let either = c
            .complement(&f)
            .unwrap()
            .add(&c.mul(&d, &f).unwrap(), &f)
            .unwrap();
        let whole = a.mul(&b, &f).unwrap().mul(&either, &f).unwrap();
        let common = whole.common();
        assert_eq!(common.len(), 2);
        let rest = whole.strip(&common);
        assert!(rest.eq(&either) && rest.common().is_empty());
        assert!(rest.times(&common, &f).eq(&whole));
        // Where c holds, 1 - c + cd is d; a polynomial without c is untouched.
        let [(c_atom, _)] = c.common()[..] else {
            panic!("one atom")
        };
        assert!(rest.with_true(&[(c_atom, true)], &f).unwrap().eq(&d));
        assert!(d.with_true(&[(c_atom, true)], &f).is_none());
        assert!(Poly::zero().common().is_empty());
        // A numeric atom is not one to split off.
        assert!(atom(&f, 5, false).common().is_empty());
    }
}
