//! Python's text rules for the strings the orchestrator readers handle: what `str.strip` and
//! `str.split` count as white space, `inspect.cleandoc` for docstrings, and `repr` and `str` of
//! the constants a file can hold, so a value folded from a file reads as Python would print it.

/// Whether `str.isspace` is true of `c`: Unicode white space and the four separators, `\x1c` to
/// `\x1f`, that Python counts and the Unicode White_Space property does not.
pub fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)
}

/// `str.strip()`.
pub fn strip(text: &str) -> &str {
    text.trim_matches(is_space)
}

/// `str.split()`: the runs of characters that are not white space.
pub fn split_whitespace(text: &str) -> impl Iterator<Item = &str> {
    text.split(is_space).filter(|word| !word.is_empty())
}

/// `str.expandtabs()` with the default tab size of 8.
fn expand_tabs(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut column = 0usize;
    for c in text.chars() {
        match c {
            '\t' => {
                let fill = 8 - column % 8;
                out.extend(std::iter::repeat_n(' ', fill));
                column += fill;
            }
            '\n' | '\r' => {
                out.push(c);
                column = 0;
            }
            _ => {
                out.push(c);
                column += 1;
            }
        }
    }
    out
}

/// `inspect.cleandoc`: the indentation common to the lines after the first removed, and leading
/// and trailing blank lines dropped.
pub fn cleandoc(doc: &str) -> String {
    let expanded = expand_tabs(doc);
    let mut lines: Vec<&str> = expanded.split('\n').collect();
    let mut margin = usize::MAX;
    for line in &lines[1..] {
        let content = line.trim_start_matches(' ');
        if !content.is_empty() {
            margin = margin.min(line.chars().count() - content.chars().count());
        }
    }
    lines[0] = lines[0].trim_start_matches(' ');
    if margin < usize::MAX {
        for line in &mut lines[1..] {
            *line = match line.char_indices().nth(margin) {
                Some((at, _)) => &line[at..],
                None => "",
            };
        }
    }
    while lines.last().is_some_and(|line| line.is_empty()) {
        lines.pop();
    }
    let first = lines.iter().position(|line| !line.is_empty());
    lines[first.unwrap_or(lines.len())..].join("\n")
}

/// Python's `repr` of a float: the shortest digits that read back, in positional notation for
/// decimal exponents from -4 to 15 and in scientific notation outside them.
pub fn float_repr(value: f64) -> String {
    if value.is_nan() {
        return "nan".into();
    }
    if value.is_infinite() {
        return if value < 0.0 { "-inf" } else { "inf" }.into();
    }
    if value == 0.0 {
        return if value.is_sign_negative() {
            "-0.0"
        } else {
            "0.0"
        }
        .into();
    }
    let scientific = format!("{:e}", value.abs());
    let (mantissa, exponent) = scientific.split_once('e').unwrap_or((&scientific, "0"));
    let exponent: i32 = exponent.parse().unwrap_or(0);
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let sign = if value < 0.0 { "-" } else { "" };
    let point = exponent + 1;
    if !(-3..=16).contains(&point) {
        let mut out = format!("{sign}{}", &digits[..1]);
        if digits.len() > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        let marker = if exponent < 0 { '-' } else { '+' };
        out.push_str(&format!("e{marker}{:02}", exponent.abs()));
        return out;
    }
    let count = i32::try_from(digits.len()).unwrap_or(i32::MAX);
    let body = if point <= 0 {
        format!(
            "0.{}{digits}",
            "0".repeat(usize::try_from(-point).unwrap_or(0))
        )
    } else if point >= count {
        format!(
            "{digits}{}.0",
            "0".repeat(usize::try_from(point - count).unwrap_or(0))
        )
    } else {
        let at = usize::try_from(point).unwrap_or(0);
        format!("{}.{}", &digits[..at], &digits[at..])
    };
    format!("{sign}{body}")
}

/// Python's `repr` of an imaginary literal such as `2j`.
pub fn imaginary_repr(imag: f64) -> String {
    let text = float_repr(imag);
    format!("{}j", text.strip_suffix(".0").unwrap_or(&text))
}

/// Whether Python's `str.isprintable` holds for `c`. The categories it excludes are those of
/// controls, format characters, surrogates, private use, unassigned code points and separators
/// other than the space; the code points below are the ones of those that a source file is
/// likely to hold.
fn printable(c: char) -> bool {
    match c {
        ' '..='~' => true,
        '\0'..='\u{9f}' | '\u{a0}' | '\u{ad}' => false,
        '\u{600}'..='\u{605}' | '\u{61c}' | '\u{6dd}' | '\u{70f}' | '\u{180e}' => false,
        '\u{1680}' | '\u{2000}'..='\u{200f}' | '\u{2028}'..='\u{202f}' => false,
        '\u{205f}'..='\u{206f}' | '\u{3000}' | '\u{feff}' | '\u{fff9}'..='\u{fffb}' => false,
        '\u{e000}'..='\u{f8ff}' | '\u{f0000}'..='\u{10ffff}' => false,
        '\u{fffe}' | '\u{ffff}' => false,
        _ => true,
    }
}

/// Python's `repr` of a `str`.
pub fn str_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if printable(c) => out.push(c),
            c if (c as u32) < 0x100 => out.push_str(&format!("\\x{:02x}", c as u32)),
            c if (c as u32) < 0x10000 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push_str(&format!("\\U{:08x}", c as u32)),
        }
    }
    out.push(quote);
    out
}

/// Python's `repr` of a `bytes`.
pub fn bytes_repr(bytes: &[u8]) -> String {
    let quote = if bytes.contains(&b'\'') && !bytes.contains(&b'"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(bytes.len() + 3);
    out.push('b');
    out.push(quote);
    for &byte in bytes {
        match byte {
            b'\\' => out.push_str("\\\\"),
            b'\n' => out.push_str("\\n"),
            b'\r' => out.push_str("\\r"),
            b'\t' => out.push_str("\\t"),
            b if b as char == quote => {
                out.push('\\');
                out.push(b as char);
            }
            b' '..=b'~' => out.push(byte as char),
            b => out.push_str(&format!("\\x{b:02x}")),
        }
    }
    out.push(quote);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn floats_read_as_python_prints_them() {
        for (value, text) in [
            (1.0, "1.0"),
            (1.5, "1.5"),
            (0.1, "0.1"),
            (100.0, "100.0"),
            (1e15, "1000000000000000.0"),
            (1e16, "1e+16"),
            (1.5e16, "1.5e+16"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (1.25e-7, "1.25e-07"),
            (123456.789, "123456.789"),
            (f64::INFINITY, "inf"),
            (0.0, "0.0"),
        ] {
            assert_eq!(float_repr(value), text);
        }
        assert_eq!(imaginary_repr(2.0), "2j");
        assert_eq!(imaginary_repr(1.5), "1.5j");
    }

    #[test]
    fn strings_read_as_python_prints_them() {
        assert_eq!(str_repr("a"), "'a'");
        assert_eq!(str_repr("it's"), "\"it's\"");
        assert_eq!(str_repr("it's \"x\""), "'it\\'s \"x\"'");
        assert_eq!(str_repr("a\nb\\"), "'a\\nb\\\\'");
        assert_eq!(str_repr("\u{1}\u{e9}\u{200b}"), "'\\x01\u{e9}\\u200b'");
        assert_eq!(bytes_repr(b"a'\x00"), "b\"a'\\x00\"");
    }

    #[test]
    fn docstrings_are_cleaned_as_inspect_cleans_them() {
        assert_eq!(
            cleandoc("  First.\n    Second.\n      Third.\n  "),
            "First.\nSecond.\n  Third."
        );
        assert_eq!(cleandoc("\n\n  A\n  B\n\n"), "A\nB");
        assert_eq!(cleandoc("Only"), "Only");
        assert_eq!(cleandoc(""), "");
        assert_eq!(cleandoc("a\n\tb\n        c"), "a\nb\nc");
    }

    #[test]
    fn white_space_includes_the_information_separators() {
        assert_eq!(strip("\x1c a \u{a0}\n"), "a");
        assert_eq!(
            split_whitespace("a\x1fb  c").collect::<Vec<_>>(),
            ["a", "b", "c"]
        );
    }
}
