//! The bytes of a YAML file as text: PyYAML's reader. It decodes UTF-8, or UTF-16 when the file
//! starts with a byte order mark, keeping the mark as a character as PyYAML does, and refuses a
//! character YAML does not allow. A refusal is a [`Failure`] with the position PyYAML reports:
//! in bytes for text that did not decode, in characters for one that decoded.

use std::borrow::Cow;

use super::{Failure, Mark, Stage};

/// Whether YAML allows the character: tab, line feed, carriage return, the printable ASCII, NEL,
/// and everything from U+00A0 but the surrogates, U+FFFE and U+FFFF.
pub fn printable(c: u32) -> bool {
    matches!(c, 0x09 | 0x0A | 0x0D | 0x20..=0x7E | 0x85 | 0xA0..=0xD7FF | 0xE000..=0xFFFD | 0x10000..=0x10FFFF)
}

/// Whether the byte is ASCII that YAML allows.
const fn ascii_ok(b: u8) -> bool {
    matches!(b, 0x09 | 0x0A | 0x0D | 0x20..=0x7E)
}

/// The index of the first character of `text` YAML does not allow.
pub fn first_unprintable(text: &str) -> Option<(usize, u32)> {
    let bytes = text.as_bytes();
    let mut at = 0;
    let mut index = 0;
    while at < bytes.len() {
        let b = bytes[at];
        if b < 0x80 {
            if !ascii_ok(b) {
                return Some((index, u32::from(b)));
            }
            at += 1;
            index += 1;
        } else {
            let c = text[at..].chars().next()?;
            if !printable(c as u32) {
                return Some((index, c as u32));
            }
            at += c.len_utf8();
            index += 1;
        }
    }
    None
}

/// The line and column of the position after `before`, as yamlio has always reported a reader's
/// refusal: every character counts, a byte order mark too, and the lines are split at CRLF, LF,
/// CR, NEL, LS and PS.
pub fn locate(before: impl Iterator<Item = u32>) -> (usize, usize) {
    let (mut line, mut column) = (0, 0);
    let mut after_cr = false;
    for c in before {
        match c {
            0x0A if after_cr => {}
            0x0A | 0x0D | 0x85 | 0x2028 | 0x2029 => {
                line += 1;
                column = 0;
            }
            _ => column += 1,
        }
        after_cr = c == 0x0D;
    }
    (line, column)
}

fn refusal(character: u32, reason: &str, index: usize, line: usize, column: usize) -> Failure {
    Failure::new(
        Stage::Reader,
        format!("unacceptable character #x{character:04x}: {reason}"),
        Some(Mark {
            index,
            line,
            column,
        }),
    )
}

/// The refusal of the character at `index` of `text`, which decoded but is not allowed.
pub fn unprintable(prefix: impl Iterator<Item = u32>, index: usize, character: u32) -> Failure {
    let (line, column) = locate(prefix);
    refusal(
        character,
        "special characters are not allowed",
        index,
        line,
        column,
    )
}

/// The text of a UTF-8 or UTF-16 file, or the refusal of its first bad byte or character.
pub fn decode(bytes: &[u8]) -> Result<Cow<'_, str>, Failure> {
    let text: Cow<'_, str> = if bytes.starts_with(&[0xFF, 0xFE]) {
        Cow::Owned(utf16(bytes, true)?)
    } else if bytes.starts_with(&[0xFE, 0xFF]) {
        Cow::Owned(utf16(bytes, false)?)
    } else {
        match std::str::from_utf8(bytes) {
            Ok(text) => Cow::Borrowed(text),
            Err(error) => return Err(utf8_refusal(bytes, &error)),
        }
    };
    check(&text)?;
    Ok(text)
}

/// Refuses the first character of `text` that YAML does not allow.
pub fn check(text: &str) -> Result<(), Failure> {
    match first_unprintable(text) {
        None => Ok(()),
        Some((index, character)) => Err(unprintable(
            text.chars().take(index).map(u32::from),
            index,
            character,
        )),
    }
}

/// Why Python's UTF-8 decoder refuses the byte at `valid_up_to` of `bytes`.
fn utf8_reason(bytes: &[u8], error: &std::str::Utf8Error) -> &'static str {
    let byte = bytes[error.valid_up_to()];
    if error.error_len().is_none() {
        "unexpected end of data"
    } else if matches!(byte, 0x80..=0xC1 | 0xF5..=0xFF) {
        "invalid start byte"
    } else {
        "invalid continuation byte"
    }
}

/// What `str(UnicodeDecodeError)` says of the bytes `bytes` that are not UTF-8.
pub fn utf8_message(bytes: &[u8], error: &std::str::Utf8Error) -> String {
    let at = error.valid_up_to();
    let end = at + error.error_len().unwrap_or(bytes.len() - at);
    let reason = utf8_reason(bytes, error);
    if end - at == 1 {
        format!(
            "'utf-8' codec can't decode byte 0x{:02x} in position {at}: {reason}",
            bytes[at]
        )
    } else {
        format!(
            "'utf-8' codec can't decode bytes in position {at}-{}: {reason}",
            end - 1
        )
    }
}

fn utf8_refusal(bytes: &[u8], error: &std::str::Utf8Error) -> Failure {
    let at = error.valid_up_to();
    let byte = bytes[at];
    let reason = utf8_reason(bytes, error);
    // The text before the bad byte decoded, so its characters give the column.
    let valid = std::str::from_utf8(&bytes[..at]).unwrap_or_default();
    let (line, column) = locate(valid.chars().map(u32::from));
    refusal(u32::from(byte), reason, at, line, column)
}

fn utf16(bytes: &[u8], little: bool) -> Result<String, Failure> {
    let unit = |at: usize| -> u16 {
        let pair = [bytes[at], bytes[at + 1]];
        if little {
            u16::from_le_bytes(pair)
        } else {
            u16::from_be_bytes(pair)
        }
    };
    let mut text = String::with_capacity(bytes.len() / 2);
    let mut seen: Vec<u32> = Vec::new();
    let mut at = 0;
    while at < bytes.len() {
        let bad = |reason: &str, seen: &[u32]| {
            let (line, column) = locate(seen.iter().copied());
            refusal(u32::from(bytes[at]), reason, at, line, column)
        };
        if at + 1 >= bytes.len() {
            return Err(bad("truncated data", &seen));
        }
        let first = unit(at);
        let (character, width) = match first {
            0xD800..=0xDBFF => {
                if at + 3 >= bytes.len() {
                    return Err(bad("unexpected end of data", &seen));
                }
                let second = unit(at + 2);
                if !(0xDC00..=0xDFFF).contains(&second) {
                    return Err(bad("illegal UTF-16 surrogate", &seen));
                }
                let high = u32::from(first) - 0xD800;
                let low = u32::from(second) - 0xDC00;
                (0x10000 + (high << 10) + low, 4)
            }
            0xDC00..=0xDFFF => return Err(bad("illegal encoding", &seen)),
            _ => (u32::from(first), 2),
        };
        // A valid scalar value: surrogates were excluded above.
        if let Some(c) = char::from_u32(character) {
            text.push(c);
        }
        seen.push(character);
        at += width;
    }
    Ok(text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_bad_utf8_byte_is_refused_where_pyyaml_refuses_it() {
        let failure = decode(b"a: 1\nb: 2\nc: caf\xe9\n").unwrap_err();
        assert_eq!(
            failure.problem,
            "unacceptable character #x00e9: invalid continuation byte"
        );
        let mark = failure.problem_mark.unwrap();
        assert_eq!((mark.index, mark.line, mark.column), (16, 2, 6));
        let end = decode(b"a: \xe2\x82").unwrap_err();
        assert!(end.problem.ends_with("unexpected end of data"));
        let start = decode(b"\x80").unwrap_err();
        assert!(start.problem.ends_with("invalid start byte"));
    }

    #[test]
    fn an_unprintable_character_is_refused_by_its_index() {
        let failure = decode(b"a: 1\nb: 2\nc: x\x07\n").unwrap_err();
        assert_eq!(
            failure.problem,
            "unacceptable character #x0007: special characters are not allowed"
        );
        let mark = failure.problem_mark.unwrap();
        assert_eq!((mark.index, mark.line, mark.column), (14, 2, 4));
        assert!(check("a\u{85}b\u{a0}\u{fffd}\u{10000}").is_ok());
        assert!(check("\u{fffe}").is_err());
        assert!(check("\u{7f}").is_err());
        assert!(check("\u{86}").is_err());
    }

    #[test]
    fn utf16_with_a_byte_order_mark_decodes_and_keeps_the_mark() {
        let mut bytes = vec![0xFF, 0xFE];
        bytes.extend("a: 1\nb: \u{7}\n".encode_utf16().flat_map(u16::to_le_bytes));
        let failure = decode(&bytes).unwrap_err();
        let mark = failure.problem_mark.unwrap();
        assert_eq!((mark.line, mark.column), (1, 3));
        let mut good = vec![0xFE, 0xFF];
        good.extend(
            "name: caf\u{e9}\n"
                .encode_utf16()
                .flat_map(u16::to_be_bytes),
        );
        assert_eq!(decode(&good).unwrap(), "\u{feff}name: caf\u{e9}\n");
    }

    #[test]
    fn a_lone_utf16_surrogate_is_refused() {
        let bytes = [0xFF, 0xFE, 0x00, 0xDC];
        assert!(
            decode(&bytes)
                .unwrap_err()
                .problem
                .ends_with("illegal encoding")
        );
    }
}
