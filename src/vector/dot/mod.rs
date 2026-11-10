//! Inner products of quantised vectors.
//!
//! Vectors are stored as `i8`, so an inner product is a sum of integer products: exact,
//! whatever order the lanes add up in, which is what lets the index use each machine's SIMD and
//! still build the same graph on every machine.
//!
//! The kernel is hand-written assembly, one routine for each architecture Konstruct supports,
//! beside this file in `src/vector/dot/`, each using the SIMD its baseline guarantees: SSE2 on x86_64, NEON
//! on aarch64, VSX on powerpc64le, the vector facility on s390x and LSX on loongarch64; riscv64
//! is scalar, as its baseline has no vector extension. Each file says why its choice holds on
//! every machine the wheels install on. There is one implementation per architecture and no
//! dispatch: LLVM assembles the routine through `global_asm!` on every operating system, as it
//! does the FNV-1a routines (see [`crate::fnv`]).

#[cfg(target_arch = "x86_64")]
routine!(include_str!("x86_64.asm"), "konstruct_dot_i8");

#[cfg(target_arch = "aarch64")]
routine!(include_str!("aarch64.asm"), "konstruct_dot_i8");

#[cfg(target_arch = "riscv64")]
routine!(include_str!("riscv64.asm"), "konstruct_dot_i8");

#[cfg(all(target_arch = "powerpc64", target_endian = "little"))]
routine!(include_str!("powerpc64le.asm"), "konstruct_dot_i8");

#[cfg(target_arch = "s390x")]
routine!(include_str!("s390x.asm"), "konstruct_dot_i8");

#[cfg(target_arch = "loongarch64")]
routine!(include_str!("loongarch64.asm"), "konstruct_dot_i8");

// System V on x86_64 whatever the operating system, so one routine serves Windows as well.
#[cfg(target_arch = "x86_64")]
unsafe extern "sysv64" {
    #[link_name = "\u{1}konstruct_dot_i8"]
    fn konstruct_dot_i8(a: *const i8, b: *const i8, len: usize) -> i32;
}

// Every other architecture has one C calling convention on every operating system it runs.
#[cfg(not(target_arch = "x86_64"))]
unsafe extern "C" {
    #[link_name = "\u{1}konstruct_dot_i8"]
    fn konstruct_dot_i8(a: *const i8, b: *const i8, len: usize) -> i32;
}

/// The longest vectors whose inner product cannot wrap: each product is at most 2^14.
pub const MAX_LENGTH: usize = (1 << 17) - 1;

/// The inner product of two vectors of equal length, at most [`MAX_LENGTH`].
pub fn dot(a: &[i8], b: &[i8]) -> i32 {
    assert_eq!(a.len(), b.len(), "vectors of different lengths");
    assert!(a.len() <= MAX_LENGTH, "a vector too long to sum exactly");
    // SAFETY: the routine reads exactly `len` bytes from each pointer, all inside the two
    // slices, writes no memory and follows the declared calling convention.
    unsafe { konstruct_dot_i8(a.as_ptr(), b.as_ptr(), a.len()) }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    /// The inner product in exact `f64` arithmetic, the reference the kernel is tested
    /// against (every product and every partial sum here is an integer below 2^53).
    fn reference(a: &[i8], b: &[i8]) -> f64 {
        a.iter()
            .zip(b)
            .map(|(x, y)| f64::from(*x) * f64::from(*y))
            .sum()
    }

    #[test]
    fn computed_vectors() {
        assert_eq!(dot(&[], &[]), 0);
        assert_eq!(dot(&[3], &[-4]), -12);
        assert_eq!(dot(&[1, 2, 3], &[4, 5, 6]), 32);
        // The extremes: -128 squared, and -128 times 127, over a whole block and a tail.
        for length in [1, 15, 16, 17, 31, 32, 33, 255, 256, 257, 1000] {
            let low = vec![-128i8; length];
            let high = vec![127i8; length];
            assert_eq!(dot(&low, &low), 16384 * length as i32, "{length}");
            assert_eq!(dot(&low, &high), -16256 * length as i32, "{length}");
            assert_eq!(dot(&high, &high), 16129 * length as i32, "{length}");
        }
        // 0, 1, ..., 99 against the same: the sum of squares, 99 * 100 * 199 / 6, with signs.
        let up: Vec<i8> = (0..100).collect();
        let down: Vec<i8> = (0..100).map(|x: i8| -x).collect();
        assert_eq!(dot(&up, &up), 328_350);
        assert_eq!(dot(&up, &down), -328_350);
        let longest = vec![-128i8; MAX_LENGTH];
        assert_eq!(
            i64::from(dot(&longest, &longest)),
            16384 * MAX_LENGTH as i64
        );
    }

    #[test]
    fn unaligned_slices_and_every_tail() {
        let a: Vec<i8> = (0..300)
            .map(|i| ((i * 37 + 11) % 256) as u8 as i8)
            .collect();
        let b: Vec<i8> = (0..300)
            .map(|i| ((i * 101 + 7) % 256) as u8 as i8)
            .collect();
        for start in 0..17 {
            for length in 0..(300 - start).min(80) {
                let (x, y) = (&a[start..start + length], &b[start..start + length]);
                assert_eq!(f64::from(dot(x, y)), reference(x, y), "{start} {length}");
                // The two vectors at different offsets from alignment.
                let y = &b[(start + 3) % 17..(start + 3) % 17 + length];
                assert_eq!(
                    f64::from(dot(x, y)),
                    reference(x, y),
                    "{start} {length} shifted"
                );
            }
        }
    }

    #[test]
    #[should_panic(expected = "different lengths")]
    fn lengths_must_match() {
        dot(&[1, 2], &[1]);
    }

    proptest! {
        #[test]
        fn matches_the_exact_reference(
            pairs in prop::collection::vec((any::<i8>(), any::<i8>()), 0..2000),
            skip in 0usize..16,
        ) {
            let (a, b): (Vec<i8>, Vec<i8>) = pairs.into_iter().unzip();
            let skip = skip.min(a.len());
            let (a, b) = (&a[skip..], &b[skip..]);
            prop_assert_eq!(f64::from(dot(a, b)), reference(a, b));
        }
    }
}
