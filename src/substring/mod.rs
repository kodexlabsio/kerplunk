//! Whether one byte string occurs in another.
//!
//! A search word that names no node scores the nodes whose label holds it, so a search tests
//! every node's label against each such word: tens of thousands of short haystacks per query.
//! The test is hand-written assembly for each architecture Konstruct supports, in
//! `src/substring/`, in the style of memchr's `memmem`: sixteen positions at a time are compared
//! with the needle's first byte and, at the same offsets plus the needle's length less one, with
//! its last, and only a position where both match is compared in full. Each architecture uses
//! the vector instructions its baseline guarantees, as `src/words/` does and for the same reasons:
//!
//! - x86_64: SSE2, with `pmovmskb` for the mask of candidates.
//! - aarch64: NEON, with a narrowing shift for a mask of four bits per position.
//! - riscv64: scalar RV64GC, comparing the first and last bytes one position at a time.
//! - powerpc64le: POWER8's VMX and VSX, with `vbpermq` for the mask.
//! - s390x: the vector facility of z13, with `vfene` for the first candidate and `vll` for the
//!   last positions.
//! - loongarch64: LSX, with `vmskltz.b` for the mask.
//!
//! Text is UTF-8 and so is the needle, so a match of the needle's bytes always starts and ends
//! on character boundaries, and the answer is the one `str::contains` gives.

// The routines take 64-bit pointers and lengths, so a 32-bit ABI on one of these architectures
// (x86_64's x32, aarch64's ILP32) is refused as well: it would pass 32-bit ones.
#[cfg(not(all(
    target_pointer_width = "64",
    any(
        target_arch = "x86_64",
        target_arch = "aarch64",
        target_arch = "riscv64",
        all(target_arch = "powerpc64", target_endian = "little"),
        target_arch = "s390x",
        target_arch = "loongarch64",
    ),
)))]
compile_error!(
    "Konstruct supports x86_64, aarch64, riscv64, powerpc64le, s390x and loongarch64, with 64-bit pointers"
);

#[cfg(target_arch = "x86_64")]
routine!(include_str!("x86_64.asm"), "konstruct_contains");

#[cfg(target_arch = "aarch64")]
routine!(include_str!("aarch64.asm"), "konstruct_contains");

#[cfg(target_arch = "riscv64")]
routine!(include_str!("riscv64.asm"), "konstruct_contains");

#[cfg(all(target_arch = "powerpc64", target_endian = "little"))]
routine!(include_str!("powerpc64le.asm"), "konstruct_contains");

#[cfg(target_arch = "s390x")]
routine!(include_str!("s390x.asm"), "konstruct_contains");

#[cfg(target_arch = "loongarch64")]
routine!(include_str!("loongarch64.asm"), "konstruct_contains");

// System V on x86_64 whatever the operating system, so one routine serves Windows as well.
#[cfg(target_arch = "x86_64")]
unsafe extern "sysv64" {
    #[link_name = "\u{1}konstruct_contains"]
    fn konstruct_contains(
        haystack: *const u8,
        haystack_len: usize,
        needle: *const u8,
        needle_len: usize,
    ) -> usize;
}

// Every other architecture has one C calling convention on every operating system it runs.
#[cfg(not(target_arch = "x86_64"))]
unsafe extern "C" {
    #[link_name = "\u{1}konstruct_contains"]
    fn konstruct_contains(
        haystack: *const u8,
        haystack_len: usize,
        needle: *const u8,
        needle_len: usize,
    ) -> usize;
}

/// Whether `needle` occurs in `haystack`; an empty needle occurs in every haystack.
pub fn contains(haystack: &[u8], needle: &[u8]) -> bool {
    // SAFETY: the routine reads only bytes inside the two slices, writes no memory and follows
    // the declared calling convention; it returns 1 or 0.
    unsafe {
        konstruct_contains(
            haystack.as_ptr(),
            haystack.len(),
            needle.as_ptr(),
            needle.len(),
        ) != 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testing::{MIXED, Rng};
    use proptest::prelude::*;

    /// Every position, compared in full.
    fn reference(haystack: &[u8], needle: &[u8]) -> bool {
        needle.is_empty() || haystack.windows(needle.len()).any(|w| w == needle)
    }

    /// Checks `needle` in `haystack` with the haystack at every offset in `alignments` of a
    /// buffer, and the needle at an offset of its own.
    fn check(haystack: &[u8], needle: &[u8], alignments: std::ops::Range<usize>) {
        let expected = reference(haystack, needle);
        let mut buffer = vec![0u8; haystack.len() + alignments.end];
        let mut own = vec![0u8; needle.len() + 7];
        for offset in alignments {
            buffer[offset..offset + haystack.len()].copy_from_slice(haystack);
            let at = &buffer[offset..offset + haystack.len()];
            let shift = offset % 7;
            own[shift..shift + needle.len()].copy_from_slice(needle);
            let wanted = &own[shift..shift + needle.len()];
            assert_eq!(
                contains(at, wanted),
                expected,
                "{needle:?} in {haystack:?} at offset {offset}"
            );
        }
    }

    #[test]
    fn examples() {
        assert!(contains(b"", b""));
        assert!(contains(b"orders", b""));
        assert!(!contains(b"", b"o"));
        assert!(contains(b"stg_orders.amount", b"orders"));
        assert!(contains(b"stg_orders.amount", b"amount"));
        assert!(!contains(b"stg_orders.amount", b"amounts"));
        assert!(contains(b"orders", b"orders"));
        assert!(!contains(b"order", b"orders"));
        assert!(contains("résumé".as_bytes(), "é".as_bytes()));
        let long = b"customer_lifetime_value_in_the_reporting_currency";
        assert!(contains(long, b"currency"));
        assert!(!contains(long, b"currencies"));
    }

    #[test]
    fn every_short_haystack_and_needle_over_three_bytes() {
        // Every haystack of up to six bytes and needle of up to three, over bytes that tell
        // the routines' comparisons apart.
        let alphabet = [b'a', 0x80, 0xff];
        let strings = |longest: u32| -> Vec<Vec<u8>> {
            (0..=longest)
                .flat_map(|len| {
                    (0..3usize.pow(len)).map(move |mut n| {
                        (0..len)
                            .map(|_| {
                                let byte = alphabet[n % 3];
                                n /= 3;
                                byte
                            })
                            .collect()
                    })
                })
                .collect()
        };
        let needles = strings(3);
        for haystack in strings(6) {
            for needle in &needles {
                assert_eq!(
                    contains(&haystack, needle),
                    reference(&haystack, needle),
                    "{needle:?} in {haystack:?}"
                );
            }
        }
    }

    #[test]
    fn every_byte_value_at_every_position() {
        for byte in 0..=255u8 {
            for len in [1, 2, 15, 16, 17, 18, 31, 33, 40] {
                for at in 0..len {
                    let mut haystack = vec![b'.'; len];
                    haystack[at] = byte;
                    check(&haystack, &[byte], 0..2);
                    check(&haystack, &[b'.', byte], 0..1);
                    check(&haystack, &[byte, b'.'], 0..1);
                    check(&haystack, &[byte, byte], 0..1);
                }
            }
            // A needle with the byte in the middle: first and last bytes match everywhere.
            let haystack: Vec<u8> = (0..=255u8).chain(0..=255u8).collect();
            let needle = [b'x', byte, b'x'];
            check(&haystack, &needle, 0..1);
            check(
                &haystack,
                &[byte.wrapping_sub(1), byte, byte.wrapping_add(1)],
                0..1,
            );
        }
    }

    #[test]
    fn every_length_to_300_at_every_alignment() {
        let mut rng = Rng(0xfeed);
        for len in 0..=300 {
            let haystacks = [
                rng.bytes(MIXED, len),
                rng.bytes(b"ab", len),
                vec![b'a'; len],
            ];
            for haystack in haystacks {
                let mut needles = vec![Vec::new(), b"a".to_vec(), b"aab".to_vec()];
                if len > 0 {
                    let start = rng.below(len);
                    let size = 1 + rng.below((len - start).min(40));
                    let present = haystack[start..start + size].to_vec();
                    let mut changed = present.clone();
                    let last = changed.len() - 1;
                    changed[rng.below(size)] ^= 1;
                    let mut longer = present.clone();
                    longer.push(present[last]);
                    needles.extend([present, changed, longer]);
                }
                let size = 1 + rng.below(20);
                needles.push(rng.bytes(MIXED, size));
                for needle in &needles {
                    check(&haystack, needle, 0..16);
                }
            }
        }
        // Every alignment to 64, on fewer lengths.
        for len in [0, 1, 15, 16, 17, 31, 32, 63, 64, 65, 200, 300] {
            let haystack = rng.bytes(b"abc", len);
            for needle in [&b"ab"[..], b"cab", b"abcabca", b"c"] {
                check(&haystack, needle, 0..64);
            }
        }
    }

    #[test]
    fn large_inputs() {
        let mut rng = Rng(3);
        let haystack = rng.bytes(b"ab", 1 << 20);
        // A long run of near misses: every position matches the first and last bytes.
        let near = vec![b'a'; (1 << 20) + 5];
        let mut needle = vec![b'a'; 300];
        needle[150] = b'b';
        check(&near, &needle, 0..2);
        check(&near, &near[..1000], 0..2);
        let mut found = near.clone();
        found[(1 << 20) - 10..(1 << 20) + 5].copy_from_slice(b"aaaaabaaaaaaaaa");
        check(&found, &needle[145..160], 0..2);
        check(&haystack, &haystack[(1 << 20) - 64..], 0..2);
        check(&haystack, &rng.bytes(b"ab", 24), 0..2);
        check(&haystack, &haystack, 0..1);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn reads_nothing_past_either_end() {
        let mut rng = Rng(11);
        let mut haystacks = crate::testing::Guarded::new(512);
        let mut needles = crate::testing::Guarded::new(64);
        for len in 0..=300 {
            let bytes = rng.bytes(b"abc", len);
            let size = rng.below(len.min(40) + 1);
            let start = rng.below(len - size + 1);
            for needle in [bytes[start..start + size].to_vec(), rng.bytes(b"abc", size)] {
                let expected = reference(&bytes, &needle);
                let haystack = haystacks.at_end(&bytes);
                let wanted = needles.at_end(&needle);
                assert_eq!(
                    contains(haystack, wanted),
                    expected,
                    "{needle:?} in {bytes:?}"
                );
            }
        }
    }

    proptest! {
        #[test]
        fn matches_the_comparison_of_every_position(
            haystack in prop::collection::vec(prop::sample::select(&b"abc\x80"[..]), 0..200),
            needle in prop::collection::vec(prop::sample::select(&b"abc\x80"[..]), 0..6),
        ) {
            prop_assert_eq!(contains(&haystack, &needle), reference(&haystack, &needle));
        }

        #[test]
        fn finds_any_part_of_a_haystack(haystack in prop::collection::vec(any::<u8>(), 1..300), a in any::<usize>(), b in any::<usize>()) {
            let (a, b) = (a % haystack.len(), b % haystack.len());
            let part = &haystack[a.min(b)..=a.max(b)];
            prop_assert!(contains(&haystack, part));
        }

        #[test]
        fn agrees_with_str_contains(haystack in "[a-c_.é😀]{0,60}", needle in "[a-c_.é😀]{0,4}") {
            prop_assert_eq!(contains(haystack.as_bytes(), needle.as_bytes()), haystack.contains(&needle));
        }
    }
}
