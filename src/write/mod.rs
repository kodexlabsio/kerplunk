//! JSON text for `graph.json` (strings, numbers) and files put on disk whole ([`files`]).
//!
//! String escaping is hand-written assembly for each architecture Konstruct supports, in
//! `src/write/`, and so are the digits of an integer, in `src/write/digits/`; both work on a
//! 64-bit word at a time, except riscv64, which goes a byte at a time. Only the graph writer uses
//! them (`src/model/write.rs`); every other JSON file goes through orjson. The text is what orjson
//! wrote, which the golden files and the compatibility corpus pin down: two-space indentation,
//! non-ASCII text kept as it is, and floats as the shortest digits that read back the same.

pub mod digits;
#[cfg(feature = "python")]
pub(crate) mod files;

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
routine!(include_str!("x86_64.asm"), "konstruct_json_escape");

#[cfg(target_arch = "aarch64")]
routine!(include_str!("aarch64.asm"), "konstruct_json_escape");

#[cfg(target_arch = "riscv64")]
routine!(include_str!("riscv64.asm"), "konstruct_json_escape");

#[cfg(all(target_arch = "powerpc64", target_endian = "little"))]
routine!(include_str!("powerpc64le.asm"), "konstruct_json_escape");

#[cfg(target_arch = "s390x")]
routine!(include_str!("s390x.asm"), "konstruct_json_escape");

#[cfg(target_arch = "loongarch64")]
routine!(include_str!("loongarch64.asm"), "konstruct_json_escape");

// System V on x86_64 whatever the operating system, so one routine serves Windows as well.
#[cfg(target_arch = "x86_64")]
unsafe extern "sysv64" {
    #[link_name = "\u{1}konstruct_json_escape"]
    fn konstruct_json_escape(src: *const u8, len: usize, dst: *mut u8) -> *mut u8;
}

// Every other architecture has one C calling convention on every operating system it runs.
#[cfg(not(target_arch = "x86_64"))]
unsafe extern "C" {
    #[link_name = "\u{1}konstruct_json_escape"]
    fn konstruct_json_escape(src: *const u8, len: usize, dst: *mut u8) -> *mut u8;
}

/// The most input escaped in one call, so that the room reserved for the worst case (six bytes
/// for each) stays small however long a string is.
const CHUNK: usize = 1 << 14;

/// Appends `bytes`, escaped as the inside of a JSON string, to `out`.
pub fn escape(out: &mut Vec<u8>, bytes: &[u8]) {
    for chunk in bytes.chunks(CHUNK) {
        out.reserve(chunk.len() * 6);
        // SAFETY: the routine reads exactly `chunk.len()` bytes from `chunk.as_ptr()`, all inside
        // the slice, and writes at most six bytes for each from the end of `out`, which the
        // reservation above leaves room for; it returns the address after the last one written,
        // in the same allocation. It writes no other memory and follows the declared calling
        // convention.
        unsafe {
            let end = out.as_mut_ptr().add(out.len());
            let after = konstruct_json_escape(chunk.as_ptr(), chunk.len(), end);
            out.set_len(out.len() + after.offset_from(end) as usize);
        }
    }
}

/// Appends `text` as a JSON string, quotes included.
pub fn string(out: &mut Vec<u8>, text: &[u8]) {
    if text.len() > CHUNK {
        out.push(b'"');
        escape(out, text);
        out.push(b'"');
        return;
    }
    // One reservation for the quotes and the worst case, and the routine writes between them.
    out.reserve(text.len() * 6 + 2);
    // SAFETY: the reservation leaves room for the opening quote, at most six bytes for each byte
    // of `text` (which the routine reads exactly, from the slice) and the closing quote; the
    // routine returns the address after the last byte it wrote, in the same allocation.
    unsafe {
        let start = out.as_mut_ptr().add(out.len());
        *start = b'"';
        let end = konstruct_json_escape(text.as_ptr(), text.len(), start.add(1));
        *end = b'"';
        out.set_len(out.len() + end.offset_from(start) as usize + 1);
    }
}

/// Appends an unsigned integer in decimal.
pub fn unsigned(out: &mut Vec<u8>, value: u64) {
    let mut buffer = [0u8; 20];
    let n = digits::of(value, &mut buffer);
    out.extend_from_slice(&buffer[20 - n..]);
}

/// Appends a signed integer in decimal.
pub fn signed(out: &mut Vec<u8>, value: i64) {
    if value < 0 {
        out.push(b'-');
    }
    unsigned(out, value.unsigned_abs());
}

/// The integer in an exponent such as `-7` or `16`.
fn parse_exponent(text: &[u8]) -> i32 {
    let (negative, digits) = match text.split_first() {
        Some((b'-', rest)) => (true, rest),
        _ => (false, text),
    };
    let n = digits
        .iter()
        .fold(0i32, |n, &b| n * 10 + i32::from(b - b'0'));
    if negative { -n } else { n }
}

/// Appends a float as orjson wrote it: the shortest digits that read back the same, in decimal
/// from 1e-5 up to but not including 1e16 and with an exponent beyond (`1e-7`, `1.5e+300`), `.0`
/// after a whole number, and `null` for a value JSON has no number for.
pub fn float(out: &mut Vec<u8>, value: f64) {
    if !value.is_finite() {
        out.extend_from_slice(b"null");
        return;
    }
    // ryu gives the shortest digits that read back, the closest to the value, ties to even, as
    // `repr` does; its layout (`1e16`, `0.00001`, `123.45`) is taken apart into digits and
    // exponent and written as the files have it.
    let mut buffer = ryu::Buffer::new();
    let shown = buffer.format_finite(value).as_bytes();
    let (sign, shown) = match shown.split_first() {
        Some((b'-', rest)) => (&b"-"[..], rest),
        _ => (&b""[..], shown),
    };
    let (mantissa, power) = match shown.iter().position(|&b| b == b'e') {
        Some(at) => (&shown[..at], parse_exponent(&shown[at + 1..])),
        None => (shown, 0),
    };
    // The digits of the mantissa with the point taken out, on the stack.
    let mut all = [0u8; 24];
    let mut count = 0;
    let mut whole = mantissa.len();
    for (at, &b) in mantissa.iter().enumerate() {
        if b == b'.' {
            whole = at;
        } else {
            all[count] = b;
            count += 1;
        }
    }
    let leading = all[..count].iter().take_while(|&&b| b == b'0').count();
    let mut end = count;
    while end > leading && all[end - 1] == b'0' {
        end -= 1;
    }
    let (digits, exponent) = if end == leading {
        (&b"0"[..], 0)
    } else {
        (
            &all[leading..end],
            whole as i32 - 1 - leading as i32 + power,
        )
    };
    out.extend_from_slice(sign);
    if !(-5..16).contains(&exponent) {
        out.extend_from_slice(&digits[..1]);
        if digits.len() > 1 {
            out.push(b'.');
            out.extend_from_slice(&digits[1..]);
        }
        out.push(b'e');
        out.push(if exponent < 0 { b'-' } else { b'+' });
        unsigned(out, u64::from(exponent.unsigned_abs()));
    } else if exponent < 0 {
        out.extend_from_slice(b"0.");
        out.resize(out.len() + (-exponent - 1) as usize, b'0');
        out.extend_from_slice(digits);
    } else {
        let whole = exponent as usize + 1;
        if digits.len() <= whole {
            out.extend_from_slice(digits);
            out.resize(out.len() + whole - digits.len(), b'0');
            out.extend_from_slice(b".0");
        } else {
            out.extend_from_slice(&digits[..whole]);
            out.push(b'.');
            out.extend_from_slice(&digits[whole..]);
        }
    }
}

/// Whether an operating system error is a file another process holds for the moment, which
/// Windows reports while a reader has it open: access denied (5) or a sharing violation (32).
pub fn is_busy(error: &std::io::Error) -> bool {
    matches!(error.raw_os_error(), Some(5 | 32))
}

/// `operation`, tried again after a pause that doubles (`pause` milliseconds first) while it
/// fails with [`is_busy`], up to `tries` times in all.
pub fn retry_busy<T>(
    tries: u32,
    pause: u64,
    mut operation: impl FnMut() -> std::io::Result<T>,
) -> std::io::Result<T> {
    let mut attempt = 0;
    loop {
        match operation() {
            Err(error) if is_busy(&error) && attempt + 1 < tries => {
                std::thread::sleep(std::time::Duration::from_millis(pause << attempt));
                attempt += 1;
            }
            other => return other,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testing::{MIXED, Rng};

    /// What escaping should do, a byte at a time.
    fn specification(bytes: &[u8]) -> Vec<u8> {
        let mut out = Vec::new();
        for &b in bytes {
            match b {
                b'"' => out.extend_from_slice(b"\\\""),
                b'\\' => out.extend_from_slice(b"\\\\"),
                8 => out.extend_from_slice(b"\\b"),
                9 => out.extend_from_slice(b"\\t"),
                10 => out.extend_from_slice(b"\\n"),
                12 => out.extend_from_slice(b"\\f"),
                13 => out.extend_from_slice(b"\\r"),
                0..=31 => out.extend_from_slice(format!("\\u{b:04x}").as_bytes()),
                _ => out.push(b),
            }
        }
        out
    }

    fn escaped(bytes: &[u8]) -> Vec<u8> {
        let mut out = Vec::new();
        escape(&mut out, bytes);
        out
    }

    #[test]
    fn every_byte_alone_and_among_clean_bytes() {
        for byte in 0..=255u8 {
            for (before, after) in [(0, 0), (3, 9), (8, 0), (0, 8), (16, 16), (7, 1)] {
                let mut input = vec![b'a'; before];
                input.push(byte);
                input.resize(input.len() + after, b'z');
                assert_eq!(
                    escaped(&input),
                    specification(&input),
                    "{byte:#x} {before} {after}"
                );
            }
        }
    }

    #[test]
    fn mixed_bytes_of_every_length() {
        let mut rng = Rng(0x1234_5678_9abc_def1);
        for len in 0..200 {
            for _ in 0..20 {
                let input = rng.bytes(MIXED, len);
                assert_eq!(escaped(&input), specification(&input), "{input:?}");
            }
        }
    }

    #[test]
    fn clean_text_is_copied_whole() {
        let text = "résumé \u{2028} \u{1f600} model.shop.orders#amount".repeat(50);
        assert_eq!(escaped(text.as_bytes()), text.as_bytes());
    }

    #[test]
    fn a_long_string_crosses_chunks() {
        let mut rng = Rng(7);
        let input = rng.bytes(MIXED, CHUNK * 2 + 77);
        assert_eq!(escaped(&input), specification(&input));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn nothing_is_read_past_the_end_of_the_bytes() {
        let mut guarded = crate::testing::Guarded::new(64);
        for len in 0..40 {
            let input = Rng(len as u64 + 1).bytes(MIXED, len);
            assert_eq!(escaped(guarded.at_end(&input)), specification(&input));
        }
    }

    #[test]
    fn the_output_is_the_json_of_the_text() {
        let mut out = Vec::new();
        string(&mut out, b"a\"b\\c\n\x01\x7f\xc3\xa9");
        assert_eq!(out, b"\"a\\\"b\\\\c\\n\\u0001\x7f\xc3\xa9\"");
    }

    #[test]
    fn integers() {
        let mut rng = Rng(99);
        let mut values = vec![
            0,
            1,
            9,
            10,
            99,
            100,
            u64::MAX,
            u64::MAX - 1,
            1 << 63,
            10_000_000_000,
        ];
        values.extend((0..2000).map(|_| rng.next() >> (rng.next() % 64)));
        for value in values {
            let mut out = Vec::new();
            unsigned(&mut out, value);
            assert_eq!(out, value.to_string().as_bytes());
        }
        for value in [i64::MIN, i64::MAX, -1, 0, -10] {
            let mut out = Vec::new();
            signed(&mut out, value);
            assert_eq!(out, value.to_string().as_bytes());
        }
    }

    #[test]
    fn floats() {
        // Each as orjson writes it.
        for (value, text) in [
            (1.0, "1.0"),
            (-0.0, "-0.0"),
            (100.0, "100.0"),
            (123_456_789.123, "123456789.123"),
            (1e15, "1000000000000000.0"),
            (9.99e15, "9990000000000000.0"),
            (1e16, "1e+16"),
            (1.2345e16, "1.2345e+16"),
            (1.5e300, "1.5e+300"),
            (1e21, "1e+21"),
            (5e-324, "5e-324"),
            (1e-5, "0.00001"),
            (1.234e-5, "0.00001234"),
            (9e-6, "9e-6"),
            (1e-6, "1e-6"),
            (1.234e-6, "1.234e-6"),
            (0.0001, "0.0001"),
            (0.1 + 0.2, "0.30000000000000004"),
            (0.5, "0.5"),
            (-2.5, "-2.5"),
            (f64::NAN, "null"),
            (f64::INFINITY, "null"),
        ] {
            let mut out = Vec::new();
            float(&mut out, value);
            assert_eq!(String::from_utf8(out).unwrap(), text, "{value:e}");
        }
    }

    #[test]
    fn a_busy_file_is_tried_again_and_anything_else_is_not() {
        let mut calls = 0;
        let result = retry_busy(5, 0, || {
            calls += 1;
            if calls < 3 {
                Err(std::io::Error::from_raw_os_error(if calls == 1 {
                    5
                } else {
                    32
                }))
            } else {
                Ok(calls)
            }
        });
        assert_eq!(result.unwrap(), 3);
        let mut calls = 0;
        let result: std::io::Result<()> = retry_busy(3, 0, || {
            calls += 1;
            Err(std::io::Error::from_raw_os_error(5))
        });
        assert!(result.is_err() && calls == 3);
        let mut calls = 0;
        let result: std::io::Result<()> = retry_busy(5, 0, || {
            calls += 1;
            Err(std::io::Error::from_raw_os_error(2))
        });
        assert!(result.is_err() && calls == 1);
    }
}
