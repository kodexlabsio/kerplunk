//! The tag of a plain scalar: PyYAML's implicit resolver for YAML 1.1, written as code rather
//! than as regular expressions. Each function accepts exactly the strings one of its patterns
//! matches in full (a pattern's `$` also matches before a final line feed, so one is allowed).

use std::borrow::Cow;

/// The tags `SafeConstructor` builds a value for, and any other as its text.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Tag {
    Null,
    Bool,
    Int,
    Float,
    Timestamp,
    Merge,
    Value,
    /// `!`, `&` and `*`: PyYAML's resolver names them and no constructor builds them.
    Yaml,
    Str,
    Binary,
    Set,
    Omap,
    Pairs,
    Seq,
    Map,
    Other(String),
}

impl Tag {
    /// The tag written in the document, as the full URI the parser resolved it to.
    pub fn from_uri(uri: &str) -> Tag {
        match uri.strip_prefix("tag:yaml.org,2002:") {
            Some("null") => Tag::Null,
            Some("bool") => Tag::Bool,
            Some("int") => Tag::Int,
            Some("float") => Tag::Float,
            Some("timestamp") => Tag::Timestamp,
            Some("merge") => Tag::Merge,
            Some("value") => Tag::Value,
            Some("yaml") => Tag::Yaml,
            Some("str") => Tag::Str,
            Some("binary") => Tag::Binary,
            Some("set") => Tag::Set,
            Some("omap") => Tag::Omap,
            Some("pairs") => Tag::Pairs,
            Some("seq") => Tag::Seq,
            Some("map") => Tag::Map,
            _ => Tag::Other(uri.to_owned()),
        }
    }

    /// The tag as a URI.
    pub fn uri(&self) -> Cow<'_, str> {
        let short = match self {
            Tag::Null => "null",
            Tag::Bool => "bool",
            Tag::Int => "int",
            Tag::Float => "float",
            Tag::Timestamp => "timestamp",
            Tag::Merge => "merge",
            Tag::Value => "value",
            Tag::Yaml => "yaml",
            Tag::Str => "str",
            Tag::Binary => "binary",
            Tag::Set => "set",
            Tag::Omap => "omap",
            Tag::Pairs => "pairs",
            Tag::Seq => "seq",
            Tag::Map => "map",
            Tag::Other(uri) => return Cow::Borrowed(uri),
        };
        Cow::Owned(format!("tag:yaml.org,2002:{short}"))
    }

    /// What a message calls the tag: the text after its last colon.
    pub fn short(&self) -> String {
        let uri = self.uri();
        uri.rsplit(':').next().unwrap_or_default().to_owned()
    }
}

/// The tag a plain scalar has when it carries none.
pub fn resolve(value: &str) -> Tag {
    let Some(&first) = value.as_bytes().first() else {
        return Tag::Null;
    };
    let value = value.strip_suffix('\n').unwrap_or(value);
    match first {
        b'y' | b'Y' | b't' | b'T' | b'f' | b'F' | b'o' | b'O' => {
            if boolean(value) {
                Tag::Bool
            } else {
                Tag::Str
            }
        }
        b'n' | b'N' => {
            if boolean(value) {
                Tag::Bool
            } else if matches!(value, "null" | "Null" | "NULL") {
                Tag::Null
            } else {
                Tag::Str
            }
        }
        b'-' | b'+' | b'.' => {
            if float(value) {
                Tag::Float
            } else if integer(value) {
                Tag::Int
            } else {
                Tag::Str
            }
        }
        b'0'..=b'9' => {
            if float(value) {
                Tag::Float
            } else if integer(value) {
                Tag::Int
            } else if timestamp(value) {
                Tag::Timestamp
            } else {
                Tag::Str
            }
        }
        b'<' if value == "<<" => Tag::Merge,
        b'~' if value == "~" => Tag::Null,
        b'=' if value == "=" => Tag::Value,
        b'!' | b'&' | b'*' if value.len() == 1 => Tag::Yaml,
        _ => Tag::Str,
    }
}

fn boolean(v: &str) -> bool {
    matches!(
        v,
        "yes"
            | "Yes"
            | "YES"
            | "no"
            | "No"
            | "NO"
            | "true"
            | "True"
            | "TRUE"
            | "false"
            | "False"
            | "FALSE"
            | "on"
            | "On"
            | "ON"
            | "off"
            | "Off"
            | "OFF"
    )
}

fn unsigned(v: &[u8]) -> &[u8] {
    match v.first() {
        Some(b'-' | b'+') => &v[1..],
        _ => v,
    }
}

fn all(v: &[u8], allowed: impl Fn(u8) -> bool) -> bool {
    v.iter().all(|&b| allowed(b))
}

/// `[0-9][0-9_]*` followed by nothing: the length of the digits and underscores at the start.
fn run(v: &[u8], allowed: impl Fn(u8) -> bool) -> usize {
    v.iter().take_while(|&&b| allowed(b)).count()
}

/// `(?::[0-5]?[0-9])+` to the end of `v`, or up to a `.` when `dot` is true: how far it reaches.
fn sexagesimal(v: &[u8]) -> Option<usize> {
    let mut at = 0;
    let mut groups = 0;
    while v.get(at) == Some(&b':') {
        at += 1;
        let first = *v.get(at)?;
        if !first.is_ascii_digit() {
            return None;
        }
        at += 1;
        if v.get(at).is_some_and(u8::is_ascii_digit) {
            if !(b'0'..=b'5').contains(&first) {
                return None;
            }
            at += 1;
        }
        groups += 1;
    }
    (groups > 0).then_some(at)
}

/// `(?:[eE][-+][0-9]+)?` then the end.
fn exponent_to_end(v: &[u8]) -> bool {
    if v.is_empty() {
        return true;
    }
    matches!(v[0], b'e' | b'E')
        && matches!(v.get(1), Some(b'-' | b'+'))
        && v.len() > 2
        && all(&v[2..], |b| b.is_ascii_digit())
}

fn float(text: &str) -> bool {
    let all_bytes = text.as_bytes();
    let v = unsigned(all_bytes);
    let signed = v.len() != all_bytes.len();
    // [-+]?[0-9][0-9_]*\.[0-9_]*(?:[eE][-+][0-9]+)?
    // [-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*
    if v.first().is_some_and(u8::is_ascii_digit) {
        let digits = run(v, |b| b.is_ascii_digit() || b == b'_');
        let rest = &v[digits..];
        if rest.first() == Some(&b'.') {
            let fraction = 1 + run(&rest[1..], |b| b.is_ascii_digit() || b == b'_');
            if exponent_to_end(&rest[fraction..]) {
                return true;
            }
        }
        if let Some(reach) = sexagesimal(rest) {
            let after = &rest[reach..];
            if after.first() == Some(&b'.') && all(&after[1..], |b| b.is_ascii_digit() || b == b'_')
            {
                return true;
            }
        }
        return false;
    }
    // \.[0-9][0-9_]*(?:[eE][-+][0-9]+)?
    if !signed && v.len() > 1 && v[0] == b'.' && v[1].is_ascii_digit() {
        let digits = run(&v[1..], |b| b.is_ascii_digit() || b == b'_');
        if exponent_to_end(&v[1 + digits..]) {
            return true;
        }
    }
    // [-+]?\.(?:inf|Inf|INF)  and  \.(?:nan|NaN|NAN)
    matches!(v, b".inf" | b".Inf" | b".INF")
        || (!signed && matches!(v, b".nan" | b".NaN" | b".NAN"))
}

fn integer(text: &str) -> bool {
    let v = unsigned(text.as_bytes());
    match v {
        [] => false,
        [b'0'] => true,
        [b'0', b'b', rest @ ..] => {
            !rest.is_empty() && all(rest, |b| matches!(b, b'0' | b'1' | b'_'))
        }
        [b'0', b'x', rest @ ..] => {
            !rest.is_empty() && all(rest, |b| b.is_ascii_hexdigit() || b == b'_')
        }
        [b'0', rest @ ..] => all(rest, |b| matches!(b, b'0'..=b'7' | b'_')),
        [b'1'..=b'9', ..] => {
            let digits = run(v, |b| b.is_ascii_digit() || b == b'_');
            digits == v.len() || sexagesimal(&v[digits..]) == Some(v.len() - digits)
        }
        _ => false,
    }
}

fn digits(v: &[u8], at: usize, count: usize) -> bool {
    v.len() >= at + count && all(&v[at..at + count], |b| b.is_ascii_digit())
}

/// A `[0-9][0-9]?` at `at`: how many digits, one or two.
fn one_or_two(v: &[u8], at: usize) -> Option<usize> {
    if !v.get(at).is_some_and(u8::is_ascii_digit) {
        return None;
    }
    Some(if v.get(at + 1).is_some_and(u8::is_ascii_digit) {
        2
    } else {
        1
    })
}

fn timestamp(text: &str) -> bool {
    let v = text.as_bytes();
    // [0-9]{4}-[0-9]{2}-[0-9]{2}
    if v.len() == 10
        && digits(v, 0, 4)
        && v[4] == b'-'
        && digits(v, 5, 2)
        && v[7] == b'-'
        && digits(v, 8, 2)
    {
        return true;
    }
    if !(digits(v, 0, 4) && v.get(4) == Some(&b'-')) {
        return false;
    }
    let mut at = 5;
    for separator in [b'-', 0] {
        let Some(width) = one_or_two(v, at) else {
            return false;
        };
        at += width;
        if separator == b'-' {
            if v.get(at) != Some(&b'-') {
                return false;
            }
            at += 1;
        }
    }
    // (?:[Tt]|[ \t]+)
    match v.get(at) {
        Some(b'T' | b't') => at += 1,
        Some(b' ' | b'\t') => {
            while matches!(v.get(at), Some(b' ' | b'\t')) {
                at += 1;
            }
        }
        _ => return false,
    }
    // [0-9][0-9]?:[0-9][0-9]:[0-9][0-9]
    let Some(width) = one_or_two(v, at) else {
        return false;
    };
    at += width;
    if !(v.get(at) == Some(&b':')
        && digits(v, at + 1, 2)
        && v.get(at + 3) == Some(&b':')
        && digits(v, at + 4, 2))
    {
        return false;
    }
    at += 6;
    // (?:\.[0-9]*)?
    if v.get(at) == Some(&b'.') {
        at += 1 + run(&v[at + 1..], |b| b.is_ascii_digit());
    }
    // (?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?
    if at == v.len() {
        return true;
    }
    while matches!(v.get(at), Some(b' ' | b'\t')) {
        at += 1;
    }
    match v.get(at) {
        Some(b'Z') => at + 1 == v.len(),
        Some(b'-' | b'+') => {
            let Some(width) = one_or_two(v, at + 1) else {
                return false;
            };
            at += 1 + width;
            if at == v.len() {
                return true;
            }
            v.get(at) == Some(&b':') && digits(v, at + 1, 2) && at + 3 == v.len()
        }
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_yaml_1_1_scalars_resolve_as_pyyaml_resolves_them() {
        let cases = [
            ("yes", Tag::Bool),
            ("Off", Tag::Bool),
            ("y", Tag::Str),
            ("n", Tag::Str),
            ("yEs", Tag::Str),
            ("~", Tag::Null),
            ("", Tag::Null),
            ("NULL", Tag::Null),
            ("0", Tag::Int),
            ("-12", Tag::Int),
            ("0b1_0", Tag::Int),
            ("0x_f", Tag::Int),
            ("017", Tag::Int),
            ("08", Tag::Str),
            ("0_", Tag::Int),
            ("1_000", Tag::Int),
            ("1:30", Tag::Int),
            ("1:60", Tag::Str),
            ("0:30", Tag::Str),
            ("190:20:30", Tag::Int),
            ("1.5", Tag::Float),
            ("1.", Tag::Float),
            (".5", Tag::Float),
            ("-.inf", Tag::Float),
            (".NaN", Tag::Float),
            ("+.nan", Tag::Str),
            ("1.e+5", Tag::Float),
            ("1e5", Tag::Str),
            ("1.5e5", Tag::Str),
            ("1:30.5", Tag::Float),
            ("2026-11-12", Tag::Timestamp),
            ("2026-1-2", Tag::Str),
            ("2001-12-14t21:59:43.10-05:00", Tag::Timestamp),
            ("2001-12-14 21:59:43.10 -5", Tag::Timestamp),
            ("2001-12-14 21:59:43 Z", Tag::Timestamp),
            ("2001-12-14 21:59", Tag::Str),
            ("<<", Tag::Merge),
            ("=", Tag::Value),
            ("!", Tag::Yaml),
            ("&", Tag::Yaml),
            ("*", Tag::Yaml),
            ("!!", Tag::Str),
            ("hello", Tag::Str),
        ];
        for (text, tag) in cases {
            assert_eq!(resolve(text), tag, "{text:?}");
        }
    }
}
