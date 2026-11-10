//! Arithmetic modulo three primes at once, one per trial ("lane").
//!
//! Each trial evaluates a formula modulo its own prime, drawn at random by the key from the
//! primes between 2^61 - 2^59 and 2^61. A fixed modulus would let a constant wrap: modulo
//! 2^61 - 1, `a * 2^61` is `a`. With a random prime, a nonzero integer of h bits vanishes only
//! if the prime divides it, and it has at most h/60 prime factors in that range, which holds
//! more than 2.8 * 10^15 primes.
//!
//! Elements are kept in Montgomery form (x * 2^64 mod p), so a product needs no division. The
//! Montgomery product itself is hand-written assembly for each architecture Konstruct supports,
//! in this module's folder; addition and subtraction stay here, as compare-and-subtract.

use std::fmt;

use super::oracle::{Oracle, Tag};

/// Independent trials run together.
pub const LANES: usize = 3;

/// The primes are drawn from `LOWEST..2^61`.
pub const LOWEST: u64 = (1 << 61) - (1 << 59);

/// A lower bound on the number of primes in `LOWEST..2^61`. Rosser and Schoenfeld's bounds,
/// pi(x) > x / ln x for x >= 17 and pi(x) < 1.25506 x / ln x for x > 1, give
/// 2^61 / ln 2^61 - 1.25506 * LOWEST / ln LOWEST > 2.8 * 10^15.
pub const PRIMES_IN_RANGE: f64 = 2.8e15;

macro_rules! routine {
    ($file:literal) => {
        #[cfg(any(target_vendor = "apple", target_os = "windows"))]
        core::arch::global_asm!(include_str!($file));
        #[cfg(not(any(target_vendor = "apple", target_os = "windows")))]
        core::arch::global_asm!(
            include_str!($file),
            ".type konstruct_montmul, %function",
            ".size konstruct_montmul, . - konstruct_montmul",
        );
    };
}

#[cfg(target_arch = "x86_64")]
routine!("x86_64.asm");
#[cfg(target_arch = "aarch64")]
routine!("aarch64.asm");
#[cfg(target_arch = "riscv64")]
routine!("riscv64.asm");
#[cfg(all(target_arch = "powerpc64", target_endian = "little"))]
routine!("powerpc64le.asm");
#[cfg(target_arch = "s390x")]
routine!("s390x.asm");
#[cfg(target_arch = "loongarch64")]
routine!("loongarch64.asm");

// System V on x86_64 whatever the operating system, as for the FNV routine.
#[cfg(target_arch = "x86_64")]
unsafe extern "sysv64" {
    #[link_name = "\u{1}konstruct_montmul"]
    fn konstruct_montmul(a: u64, b: u64, p: u64, neg_inverse: u64) -> u64;
}

#[cfg(not(target_arch = "x86_64"))]
unsafe extern "C" {
    #[link_name = "\u{1}konstruct_montmul"]
    fn konstruct_montmul(a: u64, b: u64, p: u64, neg_inverse: u64) -> u64;
}

/// Arithmetic modulo one odd modulus below 2^62, in Montgomery form.
#[derive(Clone, Copy, PartialEq, Eq)]
pub struct Modulus {
    p: u64,
    /// -p^-1 modulo 2^64.
    neg_inverse: u64,
    /// 2^64 mod p: one, in Montgomery form.
    one: u64,
    /// 2^128 mod p, which turns x into its Montgomery form.
    square: u64,
}

impl fmt::Debug for Modulus {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Modulus({})", self.p)
    }
}

impl Modulus {
    /// Arithmetic modulo `p`, which must be odd, above 2 and below 2^62.
    pub fn new(p: u64) -> Modulus {
        assert!(p % 2 == 1 && p > 2 && p < 1 << 62, "modulus out of range");
        // Newton's iteration doubles the correct low bits of an inverse modulo 2^64; p is its
        // own inverse modulo 8, so five steps reach 96 bits.
        let mut inverse = p;
        for _ in 0..5 {
            inverse = inverse.wrapping_mul(2u64.wrapping_sub(p.wrapping_mul(inverse)));
        }
        let one = (u64::MAX % p + 1) % p;
        let mut modulus = Modulus {
            p,
            neg_inverse: inverse.wrapping_neg(),
            one,
            square: 0,
        };
        // 2^128 mod p: 2^64 mod p, doubled 64 times.
        modulus.square = (0..64).fold(one, |x, _| modulus.add(x, x));
        modulus
    }

    pub fn p(&self) -> u64 {
        self.p
    }

    pub fn one(&self) -> u64 {
        self.one
    }

    /// The Montgomery product a * b * 2^-64 mod p, for a and b below p.
    pub fn mul(&self, a: u64, b: u64) -> u64 {
        // SAFETY: the routine reads only its four arguments, writes no memory and follows the
        // declared calling convention; for odd p below 2^62 and a, b below p it returns a
        // value below p.
        unsafe { konstruct_montmul(a, b, self.p, self.neg_inverse) }
    }

    pub fn add(&self, a: u64, b: u64) -> u64 {
        let sum = a + b;
        if sum >= self.p { sum - self.p } else { sum }
    }

    pub fn neg(&self, a: u64) -> u64 {
        if a == 0 { 0 } else { self.p - a }
    }

    pub fn sub(&self, a: u64, b: u64) -> u64 {
        self.add(a, self.neg(b))
    }

    /// The element x (any 64-bit integer), in Montgomery form.
    pub fn from_u64(&self, x: u64) -> u64 {
        self.mul(x % self.p, self.square)
    }

    /// The integer an element stands for, in `0..p`.
    pub fn value(&self, a: u64) -> u64 {
        self.mul(a, 1)
    }

    /// A non-negative integer written in decimal digits; None for anything else or nothing.
    pub fn from_decimal(&self, digits: &str) -> Option<u64> {
        if digits.is_empty() {
            return None;
        }
        let ten = self.from_u64(10);
        digits.bytes().try_fold(0, |acc, b| {
            b.is_ascii_digit()
                .then(|| self.add(self.mul(acc, ten), self.from_u64(u64::from(b - b'0'))))
        })
    }

    pub fn pow(&self, base: u64, mut exponent: u64) -> u64 {
        let (mut base, mut result) = (base, self.one);
        while exponent > 0 {
            if exponent & 1 == 1 {
                result = self.mul(result, base);
            }
            base = self.mul(base, base);
            exponent >>= 1;
        }
        result
    }

    /// The inverse by Fermat's little theorem, for a prime modulus; None for zero.
    pub fn inverse(&self, a: u64) -> Option<u64> {
        (a != 0).then(|| self.pow(a, self.p - 2))
    }
}

/// Whether `n` is prime: Miller-Rabin with the first twelve primes (2 to 37) as bases, which
/// decides every n below psi_12 = 318665857834031151167461, about 3.18 * 10^23, exactly
/// (Sorenson and Webster), far above 2^64.
pub fn is_prime(n: u64) -> bool {
    const BASES: [u64; 12] = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37];
    if n < 2 {
        return false;
    }
    if let Some(&b) = BASES.iter().find(|&&b| n.is_multiple_of(b)) {
        return n == b;
    }
    if n >= 1 << 62 {
        // Beyond what the Montgomery routine takes; the identity test draws nothing this big.
        return false;
    }
    let modulus = Modulus::new(n);
    let (mut d, mut s) = (n - 1, 0);
    while d % 2 == 0 {
        d /= 2;
        s += 1;
    }
    let minus_one = modulus.neg(modulus.one);
    BASES.iter().all(|&b| {
        let mut x = modulus.pow(modulus.from_u64(b), d);
        if x == modulus.one || x == minus_one {
            return true;
        }
        for _ in 1..s {
            x = modulus.mul(x, x);
            if x == minus_one {
                return true;
            }
        }
        false
    })
}

/// A value in every trial, each in Montgomery form for its lane's prime.
#[derive(Clone, Copy, Default, PartialEq, Eq, Hash, PartialOrd, Ord, Debug)]
pub struct Lanes(pub [u64; LANES]);

/// The three primes, and arithmetic on [`Lanes`].
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Field {
    pub moduli: [Modulus; LANES],
}

impl Field {
    /// Three distinct primes from `LOWEST..2^61`, drawn by `oracle`.
    pub fn draw(oracle: &Oracle) -> Field {
        let mut primes: Vec<u64> = Vec::with_capacity(LANES);
        let mut index = 0;
        while primes.len() < LANES {
            let word = oracle.draw(Tag::Prime as u64, index);
            index += 1;
            let candidate = LOWEST | (word & ((1 << 59) - 1)) | 1;
            if !primes.contains(&candidate) && is_prime(candidate) {
                primes.push(candidate);
            }
        }
        Field::of(primes[0], primes[1], primes[2])
    }

    pub fn of(a: u64, b: u64, c: u64) -> Field {
        Field {
            moduli: [Modulus::new(a), Modulus::new(b), Modulus::new(c)],
        }
    }

    /// The smallest of the three primes, which bounds each trial's error.
    pub fn smallest(&self) -> u64 {
        self.moduli
            .iter()
            .map(Modulus::p)
            .min()
            .expect("three primes")
    }

    fn each(&self, mut f: impl FnMut(&Modulus, usize) -> u64) -> Lanes {
        Lanes(std::array::from_fn(|i| f(&self.moduli[i], i)))
    }

    pub fn zero(&self) -> Lanes {
        Lanes([0; LANES])
    }

    pub fn one(&self) -> Lanes {
        self.each(|m, _| m.one())
    }

    pub fn integer(&self, x: u64) -> Lanes {
        self.each(|m, _| m.from_u64(x))
    }

    pub fn add(&self, a: Lanes, b: Lanes) -> Lanes {
        self.each(|m, i| m.add(a.0[i], b.0[i]))
    }

    pub fn sub(&self, a: Lanes, b: Lanes) -> Lanes {
        self.each(|m, i| m.sub(a.0[i], b.0[i]))
    }

    pub fn neg(&self, a: Lanes) -> Lanes {
        self.each(|m, i| m.neg(a.0[i]))
    }

    pub fn mul(&self, a: Lanes, b: Lanes) -> Lanes {
        self.each(|m, i| m.mul(a.0[i], b.0[i]))
    }

    /// The inverse in every lane, and whether some lane was zero (and so has no inverse; it
    /// is given as zero).
    pub fn inverse(&self, a: Lanes) -> (Lanes, bool) {
        let singular = a.0.contains(&0);
        (self.each(|m, i| m.inverse(a.0[i]).unwrap_or(0)), singular)
    }

    /// The rational `numerator / denominator` from decimal digits, the numerator optionally
    /// negative; None if either is malformed or the denominator vanishes in some lane.
    pub fn rational(&self, numerator: &str, denominator: &str) -> Option<Lanes> {
        let (negative, digits) = match numerator.strip_prefix('-') {
            Some(rest) => (true, rest),
            None => (false, numerator),
        };
        let mut lanes = [0; LANES];
        for (i, m) in self.moduli.iter().enumerate() {
            let n = m.from_decimal(digits)?;
            let d = m.inverse(m.from_decimal(denominator)?)?;
            let value = m.mul(n, d);
            lanes[i] = if negative { m.neg(value) } else { value };
        }
        Some(Lanes(lanes))
    }

    /// A uniform element of each lane from 64-bit words: the top 61 bits of a word, rejected
    /// when at or above the lane's prime (at most a quarter of them). `words` must not run out.
    pub fn uniform(&self, mut words: impl Iterator<Item = u64>) -> Lanes {
        self.each(|m, _| {
            loop {
                let candidate = words.next().expect("an endless supply of words") >> 3;
                if candidate < m.p() {
                    // A uniform representative is the Montgomery form of a uniform value.
                    return candidate;
                }
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    const MERSENNE: u64 = (1 << 61) - 1;
    /// The two smallest primes above `LOWEST`, worked out in Python.
    const IN_RANGE: [u64; 2] = [1_729_382_256_910_270_481, 1_729_382_256_910_270_507];

    fn reference_mul(a: u64, b: u64, p: u64) -> u64 {
        ((u128::from(a) * u128::from(b)) % u128::from(p)) as u64
    }

    fn reference_pow(base: u64, mut exponent: u64, p: u64) -> u64 {
        let (mut base, mut result) = (base % p, 1);
        while exponent > 0 {
            if exponent & 1 == 1 {
                result = reference_mul(result, base, p);
            }
            base = reference_mul(base, base, p);
            exponent >>= 1;
        }
        result
    }

    #[test]
    fn primality() {
        assert!(is_prime(MERSENNE));
        assert!(IN_RANGE.iter().all(|&p| is_prime(p)));
        assert!(
            (IN_RANGE[0] + 2..IN_RANGE[1])
                .step_by(2)
                .all(|n| !is_prime(n))
        );
        assert!(is_prime(2) && is_prime(3) && is_prime(37) && is_prime(41));
        assert!(!is_prime(0) && !is_prime(1) && !is_prime(91) && !is_prime(MERSENNE - 2));
        // A strong pseudoprime to the bases 2 to 23 (Jaeschke), which the larger bases catch.
        assert!(!is_prime(3_825_123_056_546_413_051));
        assert!(!is_prime(MERSENNE * 3) && !is_prime(1 << 62 | 1));
        assert_eq!((0..100).filter(|&n| is_prime(n)).count(), 25);
    }

    #[test]
    fn drawn_primes_are_distinct_primes_in_range() {
        let field = Field::draw(&Oracle::default());
        let p: Vec<u64> = field.moduli.iter().map(Modulus::p).collect();
        assert!(
            p.iter()
                .all(|&p| (LOWEST..1 << 61).contains(&p) && is_prime(p))
        );
        assert!(p[0] != p[1] && p[1] != p[2] && p[0] != p[2]);
        assert_eq!(field.smallest(), *p.iter().min().unwrap());
        assert_eq!(field, Field::draw(&Oracle::default()));
        assert_ne!(field, Field::draw(&Oracle::new(b"another key")));
    }

    #[test]
    fn constants_do_not_wrap_at_two_to_the_sixty_one() {
        // Modulo 2^61 - 1, 2^61 is 1 and 2^61 - 1 is 0; modulo the drawn primes they are not.
        let field = Field::draw(&Oracle::default());
        assert_ne!(
            field.rational("2305843009213693952", "1").unwrap(),
            field.one()
        );
        assert_ne!(
            field.rational("2305843009213693951", "1").unwrap(),
            field.zero()
        );
        let (half, singular) = field.inverse(field.integer(2));
        assert!(!singular);
        assert_eq!(
            field.rational("-15", "10").unwrap(),
            field.neg(field.mul(field.integer(3), half))
        );
        assert_eq!(field.rational("1", "0"), None);
        assert_eq!(field.rational("x", "1"), None);
        assert_eq!(field.rational("1", ""), None);
    }

    #[test]
    fn edges_of_one_modulus() {
        let m = Modulus::new(MERSENNE);
        let top = m.from_u64(MERSENNE - 1);
        assert_eq!(m.value(m.mul(top, top)), 1);
        assert_eq!(m.value(m.from_u64(u64::MAX)), u64::MAX % MERSENNE);
        assert_eq!(m.value(m.add(top, m.one())), 0);
        assert_eq!(m.inverse(0), None);
        assert_eq!(m.from_decimal(""), None);
        assert_eq!(m.from_decimal("12a"), None);
        let x = m.from_u64(10u64.pow(15));
        let big = format!("1{}", "0".repeat(30));
        assert_eq!(m.from_decimal(&big), Some(m.mul(x, x)));
        let mut uniform = Field::of(MERSENNE, IN_RANGE[0], IN_RANGE[1]);
        uniform.moduli[0] = m;
        // 61 bits of all ones is p itself for 2^61 - 1, so it is rejected and the next taken.
        let lanes = uniform.uniform([u64::MAX, 8, 16, 24].into_iter());
        assert_eq!(lanes, Lanes([1, 2, 3]));
    }

    proptest! {
        #[test]
        fn montgomery_products_agree_with_integer_arithmetic(
            a in any::<u64>(), b in any::<u64>(), p in (3u64..(1u64 << 62)).prop_map(|p| p | 1),
        ) {
            let m = Modulus::new(p);
            let (x, y) = (m.from_u64(a), m.from_u64(b));
            prop_assert!(x < p && y < p);
            prop_assert_eq!(m.value(m.mul(x, y)), reference_mul(a % p, b % p, p));
            prop_assert_eq!(m.value(m.add(x, y)), ((a % p) + (b % p)) % p);
            prop_assert_eq!(m.value(m.sub(x, y)), ((a % p) + p - (b % p)) % p);
        }

        #[test]
        fn the_product_is_its_definition_at_the_edges(
            p in ((1u64 << 60)..(1u64 << 62)).prop_map(|p| p | 1), which in 0usize..5,
        ) {
            // mul(a, b) = a b 2^-64 mod p, with 2^-64 worked out with 128-bit integers.
            let m = Modulus::new(p);
            let edge = [0, 1, p - 1, p / 2, p - 2][which];
            let two_to_64 = (u128::from(u64::MAX) + 1) % u128::from(p);
            for other in [0, 1, 2, p - 1, p - 2, p / 3] {
                let product = reference_mul(edge, other, p);
                // p may be composite here, so the inverse of 2^64 comes from the definition:
                // the Montgomery product times 2^64 is the plain product.
                let back = reference_mul(m.mul(edge, other), two_to_64 as u64, p);
                prop_assert_eq!(back, product);
                prop_assert!(m.mul(edge, other) < p);
            }
        }

        #[test]
        fn field_laws(a in any::<u64>(), b in any::<u64>(), c in any::<u64>()) {
            let field = Field::of(MERSENNE, IN_RANGE[0], IN_RANGE[1]);
            let (a, b, c) = (field.integer(a), field.integer(b), field.integer(c));
            prop_assert_eq!(field.mul(a, field.add(b, c)), field.add(field.mul(a, b), field.mul(a, c)));
            prop_assert_eq!(field.mul(field.mul(a, b), c), field.mul(a, field.mul(b, c)));
            prop_assert_eq!(field.mul(a, b), field.mul(b, a));
            prop_assert_eq!(field.sub(a, a), field.zero());
            let (inverse, singular) = field.inverse(a);
            if singular {
                prop_assert!(a.0.contains(&0));
            } else {
                prop_assert_eq!(field.mul(a, inverse), field.one());
            }
        }

        #[test]
        fn powers_agree_with_integer_arithmetic(a in any::<u64>(), e in any::<u64>()) {
            let m = Modulus::new(IN_RANGE[0]);
            prop_assert_eq!(m.value(m.pow(m.from_u64(a), e)), reference_pow(a, e, IN_RANGE[0]));
        }
    }
}
