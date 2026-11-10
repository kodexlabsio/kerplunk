//! Where the words of a text start and end: the runs of ASCII lower-case letters and digits.
//!
//! The search index splits every node's names and description into words, and every query, so
//! this is the loop that building the index spends most of its time in. The scan is hand-written
//! assembly for each architecture Konstruct supports, in `src/words/`, each using the vector
//! instructions its baseline guarantees, so one routine serves every machine of that architecture:
//!
//! - x86_64: SSE2, part of x86_64 itself. Sixteen bytes are classified at once and `pmovmskb`
//!   turns them into a bit mask.
//! - aarch64: NEON (Advanced SIMD), mandatory in ARMv8-A. A narrowing shift turns sixteen
//!   comparisons into a 64-bit mask of four bits per byte.
//! - riscv64: scalar RV64GC. The vector extension is not part of RV64GC, so a riscv64 machine
//!   need not have it, and Zbb's bit counting is not part of it either.
//! - powerpc64le: VMX and VSX as POWER8 has them, the oldest processor little-endian Power
//!   Linux runs on. `vbpermq` gathers sixteen comparisons into a mask.
//! - s390x: the vector facility of z13. The s390x wheel is built for manylinux_2_28, the
//!   AlmaLinux 8 platform, whose userland is built for z13 and later; the extension refuses to
//!   load on an older machine ([`crate::unsupported_processor`]). `vstrc` finds the first byte in
//!   or out of the two ranges, and `vll` loads the last bytes of a text without reading past it.
//! - loongarch64: LSX, part of the la64v1.0 profile that Linux distributions build for and that
//!   Rust's target enables. `vmskltz.b` turns sixteen comparisons into a mask.
//!
//! Text arrives folded to lower case by Python's `str.lower()`, so upper-case ASCII letters are
//! separators here, as every other byte is. Bytes of other characters are never ASCII in UTF-8,
//! so scanning bytes splits a `str` exactly where a scan of its characters would, and every
//! boundary is a character boundary.

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
routine!(include_str!("x86_64.asm"), "konstruct_word_bounds");

#[cfg(target_arch = "aarch64")]
routine!(include_str!("aarch64.asm"), "konstruct_word_bounds");

#[cfg(target_arch = "riscv64")]
routine!(include_str!("riscv64.asm"), "konstruct_word_bounds");

#[cfg(all(target_arch = "powerpc64", target_endian = "little"))]
routine!(include_str!("powerpc64le.asm"), "konstruct_word_bounds");

#[cfg(target_arch = "s390x")]
routine!(include_str!("s390x.asm"), "konstruct_word_bounds");

#[cfg(target_arch = "loongarch64")]
routine!(include_str!("loongarch64.asm"), "konstruct_word_bounds");

// System V on x86_64 whatever the operating system, so one routine serves Windows as well.
#[cfg(target_arch = "x86_64")]
unsafe extern "sysv64" {
    #[link_name = "\u{1}konstruct_word_bounds"]
    fn konstruct_word_bounds(data: *const u8, len: usize, bounds: *mut usize) -> usize;
}

// Every other architecture has one C calling convention on every operating system it runs.
#[cfg(not(target_arch = "x86_64"))]
unsafe extern "C" {
    #[link_name = "\u{1}konstruct_word_bounds"]
    fn konstruct_word_bounds(data: *const u8, len: usize, bounds: *mut usize) -> usize;
}

/// Sets `bounds` to the offsets in `bytes` where each word starts and ends, in order: a word's
/// start, then the offset just past its end, then the next word's start. A word is a run of
/// bytes `a` to `z` and `0` to `9` that no other such byte adjoins.
pub fn bounds(bytes: &[u8], bounds: &mut Vec<usize>) {
    bounds.clear();
    // Words and separators alternate, so there are at most `len + 1` offsets.
    bounds.reserve(bytes.len() + 1);
    // SAFETY: the routine reads exactly `bytes.len()` bytes from `bytes.as_ptr()`, all inside
    // the slice, and writes at most `bytes.len() + 1` offsets from `bounds.as_mut_ptr()`, which
    // the reservation above leaves room for, then returns how many it wrote. It writes no other
    // memory and follows the declared calling convention.
    unsafe {
        let count = konstruct_word_bounds(bytes.as_ptr(), bytes.len(), bounds.as_mut_ptr());
        bounds.set_len(count);
    }
}

/// A text's words, through a buffer of boundaries reused from one text to the next.
#[derive(Debug, Default)]
pub struct Words {
    bounds: Vec<usize>,
}

impl Words {
    /// The runs of ASCII lower-case letters and digits in `text`, in order.
    pub fn of<'t>(&mut self, text: &'t str) -> impl Iterator<Item = &'t str> {
        bounds(text.as_bytes(), &mut self.bounds);
        self.bounds
            .as_chunks::<2>()
            .0
            .iter()
            .map(move |pair| &text[pair[0]..pair[1]])
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testing::{MIXED, Rng};
    use proptest::prelude::*;

    fn word_byte(byte: u8) -> bool {
        byte.is_ascii_lowercase() || byte.is_ascii_digit()
    }

    /// The boundaries by looking at one byte at a time.
    fn reference(bytes: &[u8]) -> Vec<usize> {
        let mut found = Vec::new();
        let mut inside = false;
        for (at, &byte) in bytes.iter().enumerate() {
            if word_byte(byte) != inside {
                found.push(at);
                inside = !inside;
            }
        }
        if inside {
            found.push(bytes.len());
        }
        found
    }

    fn scanned(bytes: &[u8]) -> Vec<usize> {
        let mut found = vec![usize::MAX; 3];
        bounds(bytes, &mut found);
        found
    }

    /// Checks `bytes` at every offset in `alignments` of a buffer.
    fn check_aligned(bytes: &[u8], alignments: std::ops::Range<usize>) {
        let expected = reference(bytes);
        let mut buffer = vec![0u8; bytes.len() + alignments.end];
        for offset in alignments {
            buffer[offset..offset + bytes.len()].copy_from_slice(bytes);
            let at = &buffer[offset..offset + bytes.len()];
            assert_eq!(scanned(at), expected, "{bytes:?} at offset {offset}");
        }
    }

    #[test]
    fn examples() {
        assert_eq!(scanned(b""), [0usize; 0]);
        assert_eq!(scanned(b"orders.amount_usd"), [0, 6, 7, 13, 14, 17]);
        assert_eq!(scanned(b"  2nd-x"), [2, 5, 6, 7]);
        assert_eq!(scanned(b"Order"), [1, 5]);
        let text = "orders.amount_usd, 2nd-é x";
        let found: Vec<&str> = Words::default().of(text).collect();
        assert_eq!(found, ["orders", "amount", "usd", "2nd", "x"]);
    }

    #[test]
    fn every_string_of_up_to_two_bytes() {
        assert_eq!(scanned(&[]), reference(&[]));
        for first in 0..=255u8 {
            assert_eq!(scanned(&[first]), reference(&[first]), "{first:#04x}");
            for second in 0..=255u8 {
                let bytes = [first, second];
                assert_eq!(scanned(&bytes), reference(&bytes), "{bytes:?}");
            }
        }
    }

    #[test]
    fn every_byte_value_at_every_position_of_a_block() {
        for byte in 0..=255u8 {
            for background in [b'x', b'.', 0xc3] {
                for len in [1, 15, 16, 17, 31, 32, 33, 48] {
                    check_aligned(&vec![byte; len], 0..1);
                    for at in 0..len {
                        let mut bytes = vec![background; len];
                        bytes[at] = byte;
                        check_aligned(&bytes, 0..2);
                    }
                }
            }
        }
    }

    #[test]
    fn every_length_to_300_at_every_alignment() {
        let mut rng = Rng(0x5eed);
        for len in 0..=300 {
            for bytes in [
                rng.bytes(MIXED, len),
                rng.bytes(b"ab.", len),
                vec![b'a'; len],
                vec![b'-'; len],
                (0..len)
                    .map(|i| if i % 2 == 0 { b'q' } else { 0xe9 })
                    .collect(),
            ] {
                check_aligned(&bytes, 0..64);
            }
        }
    }

    #[test]
    fn large_inputs() {
        let mut rng = Rng(42);
        for bytes in [
            rng.bytes(MIXED, 1 << 20),
            rng.bytes(b"abcdefghij.", (1 << 20) + 7),
            vec![b'z'; (1 << 20) + 3],
            vec![0xff; 1 << 20],
        ] {
            check_aligned(&bytes, 0..3);
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn reads_nothing_past_the_end() {
        let mut rng = Rng(7);
        let mut guarded = crate::testing::Guarded::new(512);
        for len in 0..=300 {
            for bytes in [rng.bytes(MIXED, len), vec![b'a'; len], vec![b' '; len]] {
                let at_end = guarded.at_end(&bytes);
                assert_eq!(scanned(at_end), reference(&bytes), "{bytes:?}");
            }
        }
    }

    proptest! {
        #[test]
        fn matches_the_byte_scan(bytes in prop::collection::vec(prop::sample::select(MIXED), 0..700), offset in 0usize..32) {
            let mut buffer = vec![b'a'; offset];
            buffer.extend_from_slice(&bytes);
            prop_assert_eq!(scanned(&buffer[offset..]), reference(&bytes));
        }

        #[test]
        fn matches_the_byte_scan_on_any_bytes(bytes in prop::collection::vec(any::<u8>(), 0..400)) {
            prop_assert_eq!(scanned(&bytes), reference(&bytes));
        }

        #[test]
        fn splits_a_str_where_its_characters_split(text in "[a-z0-9 ._A-Zé表😀\u{D7FF}]{0,80}|\\PC{0,40}") {
            let expected: Vec<&str> = text
                .split(|c: char| !(c.is_ascii_lowercase() || c.is_ascii_digit()))
                .filter(|w| !w.is_empty())
                .collect();
            let found: Vec<&str> = Words::default().of(&text).collect();
            prop_assert_eq!(found, expected);
        }
    }
}
