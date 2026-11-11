//! Reading JSON in two stages, as simdjson does: a scan that finds where every token starts, then
//! a parse that follows those positions instead of reading every byte again.
//!
//! `graph.json` is read on every command, and at 2,000 models it is 30 MB, so the reader is
//! built for speed: the scan ([`index`]) classifies the bytes of the document 64 at a time with
//! the vector instructions each architecture's baseline guarantees, and the parse
//! ([`tape::parse`]) checks the grammar over the positions it found and records each value in a
//! flat tape that the graph loader (`crate::load`) walks.
//!
//! The scan is hand-written assembly for each architecture Konstruct supports, in `src/json/`:
//!
//! - x86_64: SSE2. Each 16 bytes are compared with the bytes that matter and `pmovmskb` turns
//!   each comparison into 16 bits of a 64-bit mask.
//! - aarch64: NEON. Each comparison is ANDed with the bit each byte stands for and three rounds
//!   of `addp` gather 64 comparisons into a 64-bit mask.
//! - powerpc64le: VMX and VSX as POWER8 has them; `vbpermq` gathers sixteen comparisons into a
//!   mask.
//! - loongarch64: LSX; `vmskltz.b` gathers sixteen comparisons into a mask.
//! - s390x: the vector facility of z13, which has no instruction that gathers a mask, so the
//!   scan instead jumps from token to token with `vstrc` (VECTOR STRING RANGE COMPARE), which
//!   finds the first of sixteen bytes in or out of a set of ranges: past whitespace, to the end
//!   of a number or word, or to the next quote, backslash or control character in a string.
//! - riscv64: scalar RV64GC, a byte at a time, as its baseline has no vector extension.
//!
//! What every one of them computes is defined, a byte at a time, by the reference in this
//! module's tests, and they are tested against it.
//!
//! # What the scan finds
//!
//! A backslash escapes the byte after it unless it is itself escaped, wherever it is (outside a
//! string a backslash is an error, so what it escapes there does not matter). A quote that is
//! not escaped opens a string, and the next one closes it. Outside strings the scan reports the
//! position of every structural character (`{ } [ ] : ,`), every quote (opening and closing)
//! and the first byte of every other run of bytes that are not whitespace (space, tab, line
//! feed, carriage return) or structural: a number, `true`, `false`, `null` or something that is
//! not JSON, which the parse rejects. It also notes the first control character (below 0x20)
//! inside a string, or outside one that is not whitespace, which JSON does not allow.

pub mod tape;

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
routine!(include_str!("x86_64.asm"), "konstruct_json_scan");

#[cfg(target_arch = "aarch64")]
routine!(include_str!("aarch64.asm"), "konstruct_json_scan");

#[cfg(target_arch = "riscv64")]
routine!(include_str!("riscv64.asm"), "konstruct_json_scan");

#[cfg(all(target_arch = "powerpc64", target_endian = "little"))]
routine!(include_str!("powerpc64le.asm"), "konstruct_json_scan");

#[cfg(target_arch = "s390x")]
routine!(include_str!("s390x.asm"), "konstruct_json_scan");

#[cfg(target_arch = "loongarch64")]
routine!(include_str!("loongarch64.asm"), "konstruct_json_scan");

/// What the scan carries from one call to the next, and the memory a call works in. The
/// routines read and write it at these offsets, so its layout is fixed.
#[repr(C)]
#[derive(Debug)]
struct Scan {
    /// 0: all ones when the last byte scanned was inside a string, else 0.
    in_string: u64,
    /// 8: 1 when the next byte is escaped (the last scanned ended an odd run of backslashes).
    escaped: u64,
    /// 16: 1 when the last byte scanned was part of a number or word, else 0.
    scalar: u64,
    /// 24: the position of the first control character JSON does not allow, `u64::MAX` until
    /// one is seen.
    control: u64,
    /// 32 to 103: the routine's own: what it keeps while it runs, such as registers the
    /// calling convention says it must preserve.
    scratch: [u64; 9],
    /// 104 to 167: where a call copies the last bytes it is given when they are fewer than 64,
    /// padded with spaces, so that it never reads past them.
    tail: [u8; 64],
}

// The offsets the routines hard-code: the state at 0 to 31, the nine 8-byte slots they keep
// registers in at 32 to 103 (x86_64 and powerpc64le use every one), and the tail copy at 104.
const _: () = {
    use std::mem::{align_of, offset_of, size_of};
    assert!(offset_of!(Scan, in_string) == 0);
    assert!(offset_of!(Scan, escaped) == 8);
    assert!(offset_of!(Scan, scalar) == 16);
    assert!(offset_of!(Scan, control) == 24);
    assert!(offset_of!(Scan, scratch) == 32 && size_of::<[u64; 9]>() == 72);
    assert!(offset_of!(Scan, tail) == 104);
    assert!(size_of::<Scan>() == 168 && align_of::<Scan>() == 8);
};

impl Scan {
    fn new() -> Self {
        Self {
            in_string: 0,
            escaped: 0,
            scalar: 0,
            control: u64::MAX,
            scratch: [0; 9],
            tail: [0; 64],
        }
    }
}

// System V on x86_64 whatever the operating system, so one routine serves Windows as well.
#[cfg(target_arch = "x86_64")]
unsafe extern "sysv64" {
    #[link_name = "\u{1}konstruct_json_scan"]
    fn konstruct_json_scan(
        data: *const u8,
        len: usize,
        base: usize,
        scan: *mut Scan,
        positions: *mut u32,
    ) -> usize;
}

// Every other architecture has one C calling convention on every operating system it runs.
#[cfg(not(target_arch = "x86_64"))]
unsafe extern "C" {
    #[link_name = "\u{1}konstruct_json_scan"]
    fn konstruct_json_scan(
        data: *const u8,
        len: usize,
        base: usize,
        scan: *mut Scan,
        positions: *mut u32,
    ) -> usize;
}

/// The bytes scanned in one call. A multiple of 64, so only the last call has a partial block;
/// small enough that the positions vector grows with what it holds rather than reserving room
/// for a position at every byte of the document.
const CHUNK: usize = 1 << 16;

/// The largest document the reader takes: positions are 32-bit, and the tape keeps a flag in the
/// top bit of some.
pub const MAX_LEN: usize = (1 << 31) - 1;

/// Where a document's tokens start, as the scan finds them.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Index {
    /// The positions, in order (see the module documentation).
    pub positions: Vec<u32>,
    /// Whether the document ends inside a string.
    pub unterminated: bool,
    /// The position of the first control character JSON does not allow, if there is one.
    pub control: Option<usize>,
}

/// Scans `bytes`, which must be at most [`MAX_LEN`] long.
///
/// # Panics
///
/// When `bytes` is longer than [`MAX_LEN`].
pub fn index(bytes: &[u8]) -> Index {
    assert!(bytes.len() <= MAX_LEN, "a document of at most 2 GiB");
    let mut scan = Scan::new();
    let mut positions: Vec<u32> = Vec::new();
    for (number, chunk) in bytes.chunks(CHUNK).enumerate() {
        positions.reserve(chunk.len());
        let filled = positions.len();
        // SAFETY: the routine reads exactly `chunk.len()` bytes from `chunk.as_ptr()`, all inside
        // the slice, and `scan`, which it owns for the call; it writes at most `chunk.len()`
        // positions from the end of `positions`, which the reservation above leaves room for,
        // and returns how many it wrote. Every chunk but the last is a multiple of 64 bytes
        // long, as the routine requires of any call but the last. It writes no other memory
        // and follows the declared calling convention.
        unsafe {
            let count = konstruct_json_scan(
                chunk.as_ptr(),
                chunk.len(),
                number * CHUNK,
                &raw mut scan,
                positions.as_mut_ptr().add(filled),
            );
            positions.set_len(filled + count);
        }
    }
    Index {
        positions,
        unterminated: scan.in_string != 0,
        control: usize::try_from(scan.control)
            .ok()
            .filter(|_| scan.control != u64::MAX),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testing::Rng;
    use proptest::prelude::*;

    /// The scan, a byte at a time: what every routine must compute.
    fn reference(bytes: &[u8]) -> Index {
        let mut found = Index::default();
        let (mut in_string, mut escaped, mut scalar) = (false, false, false);
        for (at, &byte) in bytes.iter().enumerate() {
            let escaped_now = escaped;
            escaped = byte == b'\\' && !escaped_now;
            let control = byte < 0x20;
            if byte == b'"' && !escaped_now {
                found.positions.push(at as u32);
                in_string = !in_string;
                scalar = false;
            } else if in_string {
                if control {
                    found.control.get_or_insert(at);
                }
                scalar = false;
            } else if matches!(byte, b'{' | b'}' | b'[' | b']' | b':' | b',') {
                found.positions.push(at as u32);
                scalar = false;
            } else if matches!(byte, b' ' | b'\t' | b'\n' | b'\r') {
                scalar = false;
            } else if control {
                found.control.get_or_insert(at);
                scalar = false;
            } else {
                if !scalar {
                    found.positions.push(at as u32);
                }
                scalar = true;
            }
        }
        found.unterminated = in_string;
        found
    }

    /// Bytes the scan tells apart, with letters and digits so that runs form.
    const JSON: &[u8] = b"{}[]:,\"\\ \t\n\rab01-.eE+\x00\x1f\x7f\x80\xc3\xa9/u";

    fn check_aligned(bytes: &[u8], alignments: std::ops::Range<usize>) {
        let expected = reference(bytes);
        let mut buffer = vec![0u8; bytes.len() + alignments.end];
        for offset in alignments {
            buffer[offset..offset + bytes.len()].copy_from_slice(bytes);
            let at = &buffer[offset..offset + bytes.len()];
            assert_eq!(index(at), expected, "{bytes:?} at offset {offset}");
        }
    }

    #[test]
    fn examples() {
        let found = index(br#"{"a": [1, true, "x\"y"], "b\\": null}"#);
        assert_eq!(
            found.positions,
            [
                0, 1, 3, 4, 6, 7, 8, 10, 14, 16, 21, 22, 23, 25, 29, 30, 32, 36
            ]
        );
        assert!(!found.unterminated);
        assert_eq!(found.control, None);
        assert_eq!(index(b""), Index::default());
        assert!(index(b"\"abc").unterminated);
        assert_eq!(index(b"[\"a\x01\"]").control, Some(3));
        assert_eq!(index(b"\x0c1").control, Some(0));
        assert_eq!(index(b"\t\n\r 1").positions, [4]);
    }

    #[test]
    fn every_string_of_up_to_two_bytes() {
        for first in 0..=255u8 {
            assert_eq!(index(&[first]), reference(&[first]), "{first:#04x}");
            for second in 0..=255u8 {
                let bytes = [first, second];
                assert_eq!(index(&bytes), reference(&bytes), "{bytes:?}");
            }
        }
    }

    #[test]
    fn every_byte_value_at_every_position_of_a_block() {
        for byte in 0..=255u8 {
            for background in [b'a', b' ', b'"', b'\\', 0xc3] {
                for len in [1, 63, 64, 65, 127, 128, 129] {
                    check_aligned(&vec![byte; len], 0..1);
                    for at in (0..len).step_by(7).chain([len - 1]) {
                        let mut bytes = vec![background; len];
                        bytes[at] = byte;
                        check_aligned(&bytes, 0..2);
                    }
                }
            }
        }
    }

    #[test]
    fn backslash_runs_of_every_length_across_block_boundaries() {
        for run in 0..=140 {
            for start in [0, 1, 30, 60, 62, 63, 64, 65, 120] {
                for after in *b"\"a\\{" {
                    let mut bytes = vec![b' '; start];
                    bytes.push(b'"');
                    bytes.extend(std::iter::repeat_n(b'\\', run));
                    bytes.push(after);
                    bytes.extend_from_slice(b"\"]");
                    check_aligned(&bytes, 0..1);
                }
            }
        }
    }

    #[test]
    fn every_length_to_300_at_every_alignment() {
        let mut rng = Rng(0x15_0e);
        for len in 0..=300 {
            for bytes in [
                rng.bytes(JSON, len),
                rng.bytes(b"\"\\a", len),
                rng.bytes(b"\"a ,", len),
                vec![b'"'; len],
                vec![b'\\'; len],
                vec![b'1'; len],
                (0..len)
                    .map(|i| if i % 3 == 0 { b'"' } else { 0xe9 })
                    .collect(),
            ] {
                check_aligned(&bytes, 0..64);
            }
        }
    }

    #[test]
    fn documents_longer_than_a_chunk() {
        let mut rng = Rng(99);
        for len in [CHUNK - 1, CHUNK, CHUNK + 1, 3 * CHUNK + 17] {
            for bytes in [rng.bytes(JSON, len), rng.bytes(b"\"\\ab", len)] {
                assert_eq!(index(&bytes), reference(&bytes));
            }
        }
        // A string, a backslash run and a number that each cross a chunk boundary.
        let mut bytes = vec![b' '; CHUNK - 3];
        bytes.extend_from_slice(b"\"\\\\\\\"x\" 12345 [");
        assert_eq!(index(&bytes), reference(&bytes));
    }

    #[test]
    fn a_pretty_printed_document() {
        let text = "{\n  \"nodes\": [\n    {\n      \"id\": \"model.shop.orders#amount\",\n      \
                    \"attrs\": {\"expression\": \"sum(\\\"o\\\".\\\"amount\\\")\", \"n\": -1.5e3}\n    }\n  ],\n  \
                    \"warnings\": [\"caf\u{e9} \\u00e9 \u{1f600}\"]\n}\n";
        check_aligned(text.as_bytes(), 0..64);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn reads_nothing_past_the_end() {
        let mut rng = Rng(7);
        let mut guarded = crate::testing::Guarded::new(512);
        for len in 0..=300 {
            for bytes in [
                rng.bytes(JSON, len),
                vec![b'"'; len],
                vec![b'\\'; len],
                vec![b'a'; len],
            ] {
                let at_end = guarded.at_end(&bytes);
                assert_eq!(index(at_end), reference(&bytes), "{bytes:?}");
            }
        }
    }

    proptest! {
        #[test]
        fn matches_the_byte_scan(bytes in prop::collection::vec(prop::sample::select(JSON), 0..700), offset in 0usize..64) {
            let mut buffer = vec![b'a'; offset];
            buffer.extend_from_slice(&bytes);
            prop_assert_eq!(index(&buffer[offset..]), reference(&bytes));
        }

        #[test]
        fn matches_the_byte_scan_on_any_bytes(bytes in prop::collection::vec(any::<u8>(), 0..400)) {
            prop_assert_eq!(index(&bytes), reference(&bytes));
        }

        #[test]
        fn matches_the_byte_scan_on_json_text(text in "[\\[\\]{}:,\" \\\\a1é😀\n]{0,300}") {
            prop_assert_eq!(index(text.as_bytes()), reference(text.as_bytes()));
        }
    }
}
