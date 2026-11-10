//! Exact rationals over 128-bit integers, for counterexamples: the values a witness shows are
//! worked out exactly, not in the field. Every operation is checked and gives None on
//! overflow, so a witness is never shown with a wrong value.

use std::fmt;

/// A rational in lowest terms with a positive denominator.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct Q {
    n: i128,
    d: i128,
}

fn gcd(mut a: i128, mut b: i128) -> i128 {
    while b != 0 {
        (a, b) = (b, a % b);
    }
    a.abs()
}

impl Q {
    pub const ZERO: Q = Q { n: 0, d: 1 };
    pub const ONE: Q = Q { n: 1, d: 1 };

    /// `n/d` in lowest terms; None when `d` is zero or a value overflows.
    pub fn new(n: i128, d: i128) -> Option<Q> {
        if d == 0 {
            return None;
        }
        let g = gcd(n, d);
        let (mut n, mut d) = (n / g, d / g);
        if d < 0 {
            (n, d) = (n.checked_neg()?, d.checked_neg()?);
        }
        Some(Q { n, d })
    }

    pub fn integer(n: i64) -> Q {
        Q {
            n: i128::from(n),
            d: 1,
        }
    }

    pub fn numerator(self) -> i128 {
        self.n
    }

    pub fn denominator(self) -> i128 {
        self.d
    }

    pub fn checked_add(self, other: Q) -> Option<Q> {
        let g = gcd(self.d, other.d);
        let n = self
            .n
            .checked_mul(other.d / g)?
            .checked_add(other.n.checked_mul(self.d / g)?)?;
        Q::new(n, self.d.checked_mul(other.d / g)?)
    }

    pub fn checked_neg(self) -> Option<Q> {
        Some(Q {
            n: self.n.checked_neg()?,
            d: self.d,
        })
    }

    pub fn checked_sub(self, other: Q) -> Option<Q> {
        self.checked_add(other.checked_neg()?)
    }

    pub fn checked_mul(self, other: Q) -> Option<Q> {
        let (g1, g2) = (gcd(self.n, other.d).max(1), gcd(other.n, self.d).max(1));
        Q::new(
            (self.n / g1).checked_mul(other.n / g2)?,
            (self.d / g2).checked_mul(other.d / g1)?,
        )
    }

    /// None when `other` is zero, as well as on overflow.
    pub fn checked_div(self, other: Q) -> Option<Q> {
        if other.n == 0 {
            return None;
        }
        self.checked_mul(Q::new(other.d, other.n)?)
    }

    pub fn checked_pow(self, exponent: u32) -> Option<Q> {
        Q::new(self.n.checked_pow(exponent)?, self.d.checked_pow(exponent)?)
    }
}

impl fmt::Display for Q {
    /// An integer, a terminating decimal, or a fraction when the decimal would not end.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if self.d == 1 {
            return write!(f, "{}", self.n);
        }
        let mut d = self.d;
        let (mut twos, mut fives) = (0u32, 0u32);
        while d % 2 == 0 {
            d /= 2;
            twos += 1;
        }
        while d % 5 == 0 {
            d /= 5;
            fives += 1;
        }
        let places = twos.max(fives);
        if d != 1 || places > 30 {
            return write!(f, "{}/{}", self.n, self.d);
        }
        // n/d = n * (10^places / d) / 10^places, and 10^places / d is an integer.
        let scale = 10i128.pow(places) / self.d;
        match self.n.checked_mul(scale) {
            Some(scaled) => {
                let sign = if scaled < 0 { "-" } else { "" };
                let digits = format!(
                    "{:0>width$}",
                    scaled.unsigned_abs(),
                    width = places as usize + 1
                );
                let (whole, fraction) = digits.split_at(digits.len() - places as usize);
                write!(f, "{sign}{whole}.{fraction}")
            }
            None => write!(f, "{}/{}", self.n, self.d),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn q(n: i128, d: i128) -> Q {
        Q::new(n, d).unwrap()
    }

    #[test]
    fn lowest_terms_and_arithmetic() {
        assert_eq!(q(6, -4), q(-3, 2));
        assert_eq!(q(1, 3).checked_add(q(1, 6)), Some(q(1, 2)));
        assert_eq!(q(1, 3).checked_sub(q(1, 3)), Some(Q::ZERO));
        assert_eq!(q(2, 3).checked_mul(q(9, 4)), Some(q(3, 2)));
        assert_eq!(q(2, 3).checked_div(q(4, 3)), Some(q(1, 2)));
        assert_eq!(q(2, 3).checked_div(Q::ZERO), None);
        assert_eq!(q(-2, 3).checked_pow(3), Some(q(-8, 27)));
        assert_eq!(Q::new(1, 0), None);
        assert_eq!(Q::integer(i64::MAX).checked_pow(5), None);
        assert_eq!(q(i128::MAX, 1).checked_add(Q::ONE), None);
    }

    #[test]
    fn shown_as_people_write_numbers() {
        assert_eq!(q(7, 1).to_string(), "7");
        assert_eq!(q(-5, 2).to_string(), "-2.5");
        assert_eq!(q(1, 8).to_string(), "0.125");
        assert_eq!(q(-3, 40).to_string(), "-0.075");
        assert_eq!(q(7, 3).to_string(), "7/3");
        assert_eq!(q(-1, 6).to_string(), "-1/6");
    }
}
