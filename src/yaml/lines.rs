//! Where a line ends: YAML 1.1 breaks a line at CR, LF, NEL, LS and PS, and the scanner asks it of
//! the bytes of the text at every comment, block scalar line and line break.

/// The length in bytes of the line break at `at`, 0 when there is none: CR and LF are one byte,
/// NEL (U+0085) is two, and LS and PS (U+2028 and U+2029) are three.
#[inline]
pub fn break_len(bytes: &[u8], at: usize) -> usize {
    match bytes.get(at) {
        Some(b'\n' | b'\r') => 1,
        Some(0xC2) if bytes.get(at + 1) == Some(&0x85) => 2,
        Some(0xE2)
            if bytes.get(at + 1) == Some(&0x80)
                && matches!(bytes.get(at + 2), Some(0xA8 | 0xA9)) =>
        {
            3
        }
        _ => 0,
    }
}

/// The index of the first line break at or after `from`, or the length of `bytes`.
#[inline]
pub fn find_break(bytes: &[u8], from: usize) -> usize {
    let mut at = from;
    while at < bytes.len() {
        match bytes[at] {
            b'\n' | b'\r' => return at,
            0xC2 | 0xE2 if break_len(bytes, at) > 0 => return at,
            _ => at += 1,
        }
    }
    bytes.len()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_break_ends_a_line_and_nothing_else_does() {
        for (text, at) in [
            ("ab\ncd", 2),
            ("ab\rcd", 2),
            ("ab\r\ncd", 2),
            ("ab\u{85}cd", 2),
            ("ab\u{2028}cd", 2),
            ("ab\u{2029}cd", 2),
            ("\u{e9}\u{2022}\u{c3}x\n", 8),
            ("abc", 3),
            ("", 0),
        ] {
            assert_eq!(find_break(text.as_bytes(), 0), at, "{text:?}");
        }
        assert_eq!(break_len("\u{85}".as_bytes(), 0), 2);
        assert_eq!(break_len("\u{2028}".as_bytes(), 0), 3);
        assert_eq!(break_len("\u{2029}".as_bytes(), 0), 3);
        assert_eq!(break_len("\u{a0}".as_bytes(), 0), 0);
        assert_eq!(break_len("\u{2022}".as_bytes(), 0), 0);
    }
}
