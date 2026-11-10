//! Python strings as the keys of this crate's indexes, lone surrogates included.
//!
//! A Python string can hold a lone surrogate (U+D800 to U+DFFF): a file name that is not valid
//! UTF-8, decoded with `surrogateescape`, holds one for each stray byte. A Rust `str` cannot, so
//! such a string is stored escaped. U+D7FF, a code point Unicode leaves unassigned, starts each
//! escape, and one character from the supplementary private use area follows it: U+F0000 for
//! U+D7FF itself, and U+F0001 onwards for U+D800 onwards. Any other string, which is every string
//! a project normally holds, is its own key.
//!
//! The escape keeps distinct strings distinct, orders keys as Python orders the strings (code
//! point by code point: each escape sorts after U+D7FE and before U+E000, as what it stands for
//! does) and adds no ASCII, so ids intern, sort and split into words as they would in Python.

use std::borrow::Cow;

/// The character that starts an escape.
pub const ESCAPE: char = '\u{D7FF}';

/// The character after [`ESCAPE`] that stands for U+D7FF; the surrogates follow it in order.
const ESCAPED: u32 = 0xF_0000;

const SURROGATES: std::ops::RangeInclusive<u32> = 0xD800..=0xDFFF;

fn push_escape(key: &mut String, point: u32) {
    let offset = if point == u32::from(ESCAPE) {
        0
    } else {
        point - SURROGATES.start() + 1
    };
    key.push(ESCAPE);
    key.push(char::from_u32(ESCAPED + offset).expect("a private use character"));
}

fn push_escaped(key: &mut String, text: &str) {
    for (at, piece) in text.split(ESCAPE).enumerate() {
        if at > 0 {
            push_escape(key, u32::from(ESCAPE));
        }
        key.push_str(piece);
    }
}

/// The key of a string with no lone surrogates: the string itself unless it holds [`ESCAPE`].
pub fn from_str(text: &str) -> Cow<'_, str> {
    if text.contains(ESCAPE) {
        let mut key = String::with_capacity(text.len() + 4);
        push_escaped(&mut key, text);
        Cow::Owned(key)
    } else {
        Cow::Borrowed(text)
    }
}

/// The key of a string given in generalized UTF-8, as Python's
/// `str.encode('utf-8', 'surrogatepass')` writes it: UTF-8 in which each lone surrogate is the
/// three bytes UTF-8 would give its code point. `None` when `bytes` is not that.
pub fn from_generalized_utf8(bytes: &[u8]) -> Option<String> {
    let mut key = String::with_capacity(bytes.len() + 4);
    let mut rest = bytes;
    loop {
        match std::str::from_utf8(rest) {
            Ok(valid) => {
                push_escaped(&mut key, valid);
                return Some(key);
            }
            Err(error) => {
                let (valid, after) = rest.split_at(error.valid_up_to());
                push_escaped(&mut key, std::str::from_utf8(valid).ok()?);
                // UTF-8 rejects these three bytes only because they encode a surrogate.
                let &[0xED, second @ 0xA0..=0xBF, third @ 0x80..=0xBF, ..] = after else {
                    return None;
                };
                let point = 0xD000 | (u32::from(second & 0x3F) << 6) | u32::from(third & 0x3F);
                push_escape(&mut key, point);
                rest = &after[3..];
            }
        }
    }
}

/// The string the key `key` stands for, in generalized UTF-8: the key's own bytes unless it
/// holds an escape. (Given a string that is not a key, an [`ESCAPE`] that starts no escape is
/// kept as it is.)
pub fn to_generalized_utf8(key: &str) -> Cow<'_, [u8]> {
    if !key.contains(ESCAPE) {
        return Cow::Borrowed(key.as_bytes());
    }
    let mut bytes = Vec::with_capacity(key.len());
    let mut characters = key.chars().peekable();
    while let Some(character) = characters.next() {
        let escaped = characters
            .peek()
            .and_then(|&next| u32::from(next).checked_sub(ESCAPED))
            .filter(|&offset| {
                character == ESCAPE && offset <= SURROGATES.end() - SURROGATES.start() + 1
            });
        let Some(offset) = escaped else {
            bytes.extend_from_slice(character.encode_utf8(&mut [0; 4]).as_bytes());
            continue;
        };
        characters.next();
        let point = match offset {
            0 => u32::from(ESCAPE),
            _ => SURROGATES.start() + offset - 1,
        };
        // UTF-8's three-byte form, which generalized UTF-8 gives a surrogate too.
        bytes.extend_from_slice(&[
            0xE0 | (point >> 12) as u8,
            0x80 | ((point >> 6) & 0x3F) as u8,
            0x80 | (point & 0x3F) as u8,
        ]);
    }
    Cow::Owned(bytes)
}

/// The length of the string `key` stands for, in code points, as Python's `len` counts it.
pub fn length(key: &str) -> usize {
    key.chars().filter(|&c| c != ESCAPE).count()
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    /// A Python string's code points in generalized UTF-8, as `surrogatepass` encodes them.
    fn encode(points: &[u32]) -> Vec<u8> {
        let mut bytes = Vec::new();
        for &point in points {
            match char::from_u32(point) {
                Some(c) => bytes.extend_from_slice(c.encode_utf8(&mut [0; 4]).as_bytes()),
                None => bytes.extend_from_slice(&[
                    0xE0 | (point >> 12) as u8,
                    0x80 | ((point >> 6) & 0x3F) as u8,
                    0x80 | (point & 0x3F) as u8,
                ]),
            }
        }
        bytes
    }

    fn key(points: &[u32]) -> String {
        from_generalized_utf8(&encode(points)).expect("generalized UTF-8")
    }

    /// Code points as a Python string can hold them, weighted towards the ones near the escape.
    fn point() -> impl Strategy<Value = u32> {
        prop_oneof![
            3 => 0x20u32..0x7F,
            2 => SURROGATES,
            1 => Just(u32::from(ESCAPE)),
            1 => 0xD7F0u32..0xD7FF,
            1 => 0xE000u32..0xE010,
            1 => ESCAPED..ESCAPED + 0x900,
            1 => 0u32..=0x10_FFFF,
        ]
    }

    #[test]
    fn ordinary_strings_are_their_own_keys() {
        for text in ["", "model.shop.orders", "résumé#montant", "表.列", "😀"] {
            assert!(matches!(from_str(text), Cow::Borrowed(t) if t == text));
            assert_eq!(
                from_generalized_utf8(text.as_bytes()).as_deref(),
                Some(text)
            );
            assert!(matches!(to_generalized_utf8(text), Cow::Borrowed(b) if b == text.as_bytes()));
        }
    }

    #[test]
    fn surrogates_and_the_escape_are_escaped() {
        assert_eq!(key(&[0x61, 0xDC80]), "a\u{D7FF}\u{F0481}");
        assert_eq!(key(&[0xD800]), "\u{D7FF}\u{F0001}");
        assert_eq!(key(&[0xD7FF, 0x62]), "\u{D7FF}\u{F0000}b");
        assert_eq!(from_str("\u{D7FF}"), "\u{D7FF}\u{F0000}");
        // A pair of surrogates stays two code points, not the character they would encode.
        assert_ne!(key(&[0xD83D, 0xDE00]), "😀");
    }

    #[test]
    fn an_escape_character_that_starts_no_escape_is_kept() {
        assert_eq!(&*to_generalized_utf8("\u{D7FF}x"), "\u{D7FF}x".as_bytes());
        assert_eq!(&*to_generalized_utf8("a\u{D7FF}"), "a\u{D7FF}".as_bytes());
    }

    #[test]
    fn bytes_that_are_not_generalized_utf8_are_refused() {
        for bytes in [&b"\xff"[..], b"a\xed\xa0", b"\xed\x9f", b"\xc3"] {
            assert_eq!(from_generalized_utf8(bytes), None, "{bytes:?}");
        }
    }

    proptest! {
        #[test]
        fn keys_round_trip(points in prop::collection::vec(point(), 0..12)) {
            let key = key(&points);
            prop_assert_eq!(&*to_generalized_utf8(&key), &encode(&points)[..]);
            prop_assert_eq!(length(&key), points.len());
        }

        #[test]
        fn keys_order_as_python_orders_strings(
            a in prop::collection::vec(point(), 0..8),
            b in prop::collection::vec(point(), 0..8),
        ) {
            // Equal keys only for equal strings, and otherwise the order Python gives them.
            prop_assert_eq!(key(&a).cmp(&key(&b)), a.cmp(&b));
        }

        #[test]
        fn keys_hold_the_same_ascii(points in prop::collection::vec(point(), 0..12)) {
            let ascii: Vec<u8> = encode(&points).into_iter().filter(u8::is_ascii).collect();
            let key_ascii: Vec<u8> = key(&points).bytes().filter(u8::is_ascii).collect();
            prop_assert_eq!(key_ascii, ascii);
        }

        #[test]
        fn a_valid_string_has_one_key(text in "\\PC{0,12}|[a\u{D7FF}]{0,6}") {
            prop_assert_eq!(from_str(&text), from_generalized_utf8(text.as_bytes()).unwrap());
        }
    }
}
