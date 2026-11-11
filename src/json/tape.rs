//! The second stage: the JSON grammar over the positions the scan found, into a tape.
//!
//! The tape holds one entry for each value, in document order: an object's or array's entry is
//! followed by its members (key, then value) or items, and records where they end, so a reader
//! skips any value in one step. Nothing is decoded until it is read: a string's entry records
//! where it ends and whether it holds an escape, a number's where it ends and whether it has a
//! fraction or an exponent.
//!
//! What it accepts and rejects is what Konstruct's JSON reader, orjson, accepted and rejected
//! when it read graph.json: RFC 8259 with no byte order mark, a depth of at most 1,024, no lone
//! surrogate in a `\u` escape, and no number that rounds to infinity. An integer outside the
//! 64-bit range is read as the nearest float.

use std::borrow::Cow;

use super::{MAX_LEN, index};

/// One value: where it starts and, by kind, where it ends.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Entry {
    /// The offset of its first byte.
    at: u32,
    /// An object or array: the tape index just past its last member or item. A string: the
    /// offset of its closing quote, with [`FLAG`] when it holds an escape. A number: the offset
    /// just past it, with [`FLAG`] when it has a fraction or exponent. Otherwise 0.
    aux: u32,
}

/// The top bit of [`Entry::aux`], free because documents are shorter than 2^31 bytes.
const FLAG: u32 = 1 << 31;

/// Containers nested deeper than this are refused, as orjson refused them.
pub const MAX_DEPTH: usize = 1024;

/// Why a document is not JSON, and the offset of the byte where that is first clear.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Error {
    pub message: &'static str,
    pub at: usize,
}

/// What a value is.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Object,
    Array,
    String,
    Number,
    True,
    False,
    Null,
}

/// A number as JSON wrote it: an integer if it has no fraction or exponent and fits in 64 bits.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Number {
    Int(i64),
    UInt(u64),
    Float(f64),
}

/// A parsed document's values, without its bytes ([`Tape::doc`] pairs them again).
#[derive(Debug, Default)]
pub struct Tape {
    entries: Vec<Entry>,
}

impl Tape {
    /// Checks that `bytes` are one JSON document and records its values.
    ///
    /// # Errors
    ///
    /// The first problem in the document, by position.
    pub fn parse(bytes: &[u8]) -> Result<Self, Error> {
        if bytes.len() > MAX_LEN {
            return Err(Error {
                message: "the document is larger than 2 GiB",
                at: 0,
            });
        }
        let found = index(bytes);
        let mut parser = Parser {
            bytes,
            positions: &found.positions,
            next: 0,
            entries: Vec::with_capacity(found.positions.len()),
            open: Vec::new(),
        };
        let mut first = parser.document().err();
        let mut consider = |error: Error| {
            if first.is_none_or(|f| error.at < f.at) {
                first = Some(error);
            }
        };
        if let Some(at) = found.control {
            consider(Error {
                message: "unexpected control character",
                at,
            });
        }
        if let Err(error) = std::str::from_utf8(bytes) {
            consider(Error {
                message: "invalid UTF-8",
                at: error.valid_up_to(),
            });
        }
        match first {
            Some(error) => Err(error),
            None => Ok(Self {
                entries: parser.entries,
            }),
        }
    }

    /// The tape with the bytes it was parsed from.
    pub fn doc<'a>(&'a self, bytes: &'a [u8]) -> Doc<'a> {
        Doc {
            bytes,
            entries: &self.entries,
        }
    }

    /// The number of values.
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

/// A tape and its document's bytes: the values, by tape index (the document itself is 0).
#[derive(Clone, Copy, Debug)]
pub struct Doc<'a> {
    bytes: &'a [u8],
    entries: &'a [Entry],
}

impl<'a> Doc<'a> {
    pub fn kind(self, value: usize) -> Kind {
        match self.bytes[self.entries[value].at as usize] {
            b'{' => Kind::Object,
            b'[' => Kind::Array,
            b'"' => Kind::String,
            b't' => Kind::True,
            b'f' => Kind::False,
            b'n' => Kind::Null,
            _ => Kind::Number,
        }
    }

    /// The tape index after `value` and everything in it.
    pub fn next(self, value: usize) -> usize {
        match self.kind(value) {
            Kind::Object | Kind::Array => self.entries[value].aux as usize,
            _ => value + 1,
        }
    }

    /// The (key, value) tape indexes of an object's members, in order.
    pub fn members(self, object: usize) -> impl Iterator<Item = (usize, usize)> + 'a {
        let end = self.entries[object].aux as usize;
        let mut at = object + 1;
        std::iter::from_fn(move || {
            (at < end).then(|| {
                let member = (at, at + 1);
                at = self.next(at + 1);
                member
            })
        })
    }

    /// The tape indexes of an array's items, in order.
    pub fn items(self, array: usize) -> impl Iterator<Item = usize> + 'a {
        let end = self.entries[array].aux as usize;
        let mut at = array + 1;
        std::iter::from_fn(move || {
            (at < end).then(|| {
                let item = at;
                at = self.next(at);
                item
            })
        })
    }

    /// A string's bytes between its quotes, escapes as written.
    pub fn raw(self, string: usize) -> &'a [u8] {
        let entry = self.entries[string];
        &self.bytes[entry.at as usize + 1..(entry.aux & !FLAG) as usize]
    }

    /// Whether a string holds an escape.
    pub fn escaped(self, string: usize) -> bool {
        self.entries[string].aux & FLAG != 0
    }

    /// A string's text.
    pub fn text(self, string: usize) -> Cow<'a, str> {
        let raw = self.raw(string);
        if self.escaped(string) {
            let mut text = String::with_capacity(raw.len());
            unescape(raw, Some(&mut text)).expect("escapes checked when parsed");
            Cow::Owned(text)
        } else {
            // The document is valid UTF-8 and quotes are ASCII, so this is too.
            Cow::Borrowed(std::str::from_utf8(raw).expect("UTF-8 checked when parsed"))
        }
    }

    /// Whether a value is the string `expected`.
    pub fn is(self, value: usize, expected: &str) -> bool {
        if self.kind(value) != Kind::String {
            false
        } else if self.escaped(value) {
            self.text(value) == expected
        } else {
            self.raw(value) == expected.as_bytes()
        }
    }

    pub fn number(self, number: usize) -> Number {
        let entry = self.entries[number];
        let end = (entry.aux & !FLAG) as usize;
        let text = std::str::from_utf8(&self.bytes[entry.at as usize..end]).expect("ASCII digits");
        if entry.aux & FLAG == 0 {
            if let Ok(value) = text.parse() {
                return Number::Int(value);
            }
            if let Ok(value) = text.parse() {
                return Number::UInt(value);
            }
        }
        Number::Float(text.parse().expect("a number checked when parsed"))
    }
}

/// Checks the escapes in a string's `raw` bytes and, given `out`, appends the text they stand
/// for. On a bad escape returns the offset of its backslash.
fn unescape(raw: &[u8], mut out: Option<&mut String>) -> Result<(), usize> {
    fn hex(raw: &[u8], at: usize) -> Option<u32> {
        let digits = raw.get(at..at + 4)?;
        let text = std::str::from_utf8(digits).ok()?;
        if !digits.iter().all(u8::is_ascii_hexdigit) {
            return None;
        }
        u32::from_str_radix(text, 16).ok()
    }
    let mut from = 0;
    while let Some(found) = raw[from..].iter().position(|&b| b == b'\\') {
        let slash = from + found;
        if let Some(out) = out.as_deref_mut() {
            out.push_str(std::str::from_utf8(&raw[from..slash]).map_err(|_| slash)?);
        }
        // A backslash is never last: it would have escaped the closing quote.
        let (character, length) = match raw[slash + 1] {
            b'"' => ('"', 2),
            b'\\' => ('\\', 2),
            b'/' => ('/', 2),
            b'b' => ('\u{8}', 2),
            b'f' => ('\u{c}', 2),
            b'n' => ('\n', 2),
            b'r' => ('\r', 2),
            b't' => ('\t', 2),
            b'u' => {
                let high = hex(raw, slash + 2).ok_or(slash)?;
                match high {
                    0xd800..=0xdbff => {
                        if raw.get(slash + 6..slash + 8) != Some(b"\\u") {
                            return Err(slash);
                        }
                        let low = hex(raw, slash + 8).ok_or(slash)?;
                        if !(0xdc00..=0xdfff).contains(&low) {
                            return Err(slash);
                        }
                        let point = 0x10000 + ((high - 0xd800) << 10) + (low - 0xdc00);
                        (char::from_u32(point).ok_or(slash)?, 12)
                    }
                    0xdc00..=0xdfff => return Err(slash),
                    _ => (char::from_u32(high).ok_or(slash)?, 6),
                }
            }
            _ => return Err(slash),
        };
        if let Some(out) = out.as_deref_mut() {
            out.push(character);
        }
        from = slash + length;
    }
    if let Some(out) = out {
        out.push_str(std::str::from_utf8(&raw[from..]).map_err(|_| from)?);
    }
    Ok(())
}

/// Whether a scalar may end before the byte at `at`: whitespace, a structural character, a
/// quote, a control character (which the scan reports) or the end of the document.
fn delimits(bytes: &[u8], at: usize) -> bool {
    bytes.get(at).is_none_or(|&b| {
        b < 0x20 || matches!(b, b' ' | b'{' | b'}' | b'[' | b']' | b':' | b',' | b'"')
    })
}

struct Parser<'a> {
    bytes: &'a [u8],
    positions: &'a [u32],
    /// The next position to read.
    next: usize,
    entries: Vec<Entry>,
    /// The tape indexes of the objects and arrays not yet closed, innermost last.
    open: Vec<usize>,
}

/// What the grammar expects next.
enum Expect {
    Value,
    Key,
    AfterValue,
}

impl Parser<'_> {
    fn at(&self) -> Option<usize> {
        self.positions.get(self.next).map(|&p| p as usize)
    }

    fn byte(&self) -> Option<u8> {
        self.at().map(|at| self.bytes[at])
    }

    fn at_end(&self, message: &'static str) -> Error {
        Error {
            message,
            at: self.bytes.len(),
        }
    }

    fn push(&mut self, at: usize, aux: usize) {
        self.entries.push(Entry {
            at: at as u32,
            aux: aux as u32,
        });
    }

    fn document(&mut self) -> Result<(), Error> {
        let mut expect = Expect::Value;
        loop {
            expect = match expect {
                Expect::Value => {
                    let at = self.at().ok_or(self.at_end("expected a value"))?;
                    match self.bytes[at] {
                        open @ (b'{' | b'[') => {
                            if self.open.len() == MAX_DEPTH {
                                return Err(Error {
                                    message: "nested too deeply",
                                    at,
                                });
                            }
                            self.open.push(self.entries.len());
                            self.push(at, 0);
                            self.next += 1;
                            let close = if open == b'{' { b'}' } else { b']' };
                            if self.byte() == Some(close) {
                                self.close();
                                Expect::AfterValue
                            } else if open == b'{' {
                                Expect::Key
                            } else {
                                Expect::Value
                            }
                        }
                        b'"' => {
                            self.string(at)?;
                            Expect::AfterValue
                        }
                        b'-' | b'0'..=b'9' => {
                            self.number(at)?;
                            Expect::AfterValue
                        }
                        b't' => self.literal(at, b"true")?,
                        b'f' => self.literal(at, b"false")?,
                        b'n' => self.literal(at, b"null")?,
                        _ => {
                            return Err(Error {
                                message: "expected a value",
                                at,
                            });
                        }
                    }
                }
                Expect::Key => {
                    let at = self.at().ok_or(self.at_end("expected a string key"))?;
                    if self.bytes[at] != b'"' {
                        return Err(Error {
                            message: "expected a string key",
                            at,
                        });
                    }
                    self.string(at)?;
                    match self.at() {
                        Some(colon) if self.bytes[colon] == b':' => self.next += 1,
                        Some(at) => {
                            return Err(Error {
                                message: "expected ':' after a key",
                                at,
                            });
                        }
                        None => return Err(self.at_end("expected ':' after a key")),
                    }
                    Expect::Value
                }
                Expect::AfterValue => {
                    let Some(&open) = self.open.last() else {
                        return match self.at() {
                            None => Ok(()),
                            Some(at) => Err(Error {
                                message: "unexpected content after the document",
                                at,
                            }),
                        };
                    };
                    let object = self.bytes[self.entries[open].at as usize] == b'{';
                    let (close, message) = if object {
                        (b'}', "expected ',' or '}'")
                    } else {
                        (b']', "expected ',' or ']'")
                    };
                    let at = self.at().ok_or(self.at_end(message))?;
                    match self.bytes[at] {
                        b',' => {
                            self.next += 1;
                            if let Some(after) = self.at()
                                && self.bytes[after] == close
                            {
                                return Err(Error {
                                    message: "trailing comma",
                                    at: after,
                                });
                            }
                            if object { Expect::Key } else { Expect::Value }
                        }
                        byte if byte == close => {
                            self.close();
                            Expect::AfterValue
                        }
                        _ => return Err(Error { message, at }),
                    }
                }
            };
        }
    }

    /// Closes the innermost container at the current position, its closing bracket.
    fn close(&mut self) {
        let open = self.open.pop().expect("a container is open");
        self.entries[open].aux = self.entries.len() as u32;
        self.next += 1;
    }

    fn string(&mut self, at: usize) -> Result<(), Error> {
        let Some(&end) = self.positions.get(self.next + 1) else {
            return Err(Error {
                message: "unterminated string",
                at,
            });
        };
        let raw = &self.bytes[at + 1..end as usize];
        let escaped = raw.contains(&b'\\');
        if escaped {
            unescape(raw, None).map_err(|slash| Error {
                message: "invalid escape",
                at: at + 1 + slash,
            })?;
        }
        self.entries.push(Entry {
            at: at as u32,
            aux: end | if escaped { FLAG } else { 0 },
        });
        self.next += 2;
        Ok(())
    }

    fn literal(&mut self, at: usize, word: &[u8]) -> Result<Expect, Error> {
        if !self.bytes[at..].starts_with(word) || !delimits(self.bytes, at + word.len()) {
            return Err(Error {
                message: "expected a value",
                at,
            });
        }
        self.push(at, 0);
        self.next += 1;
        Ok(Expect::AfterValue)
    }

    fn number(&mut self, at: usize) -> Result<(), Error> {
        let bytes = self.bytes;
        let digit = |p: usize| bytes.get(p).is_some_and(u8::is_ascii_digit);
        let digits = |mut p: usize| -> Result<usize, Error> {
            if !digit(p) {
                return Err(Error {
                    message: "expected a digit",
                    at: p,
                });
            }
            while digit(p) {
                p += 1;
            }
            Ok(p)
        };
        let mut p = at + usize::from(bytes[at] == b'-');
        let leading_zero = bytes.get(p) == Some(&b'0');
        p = if leading_zero { p + 1 } else { digits(p)? };
        let mut fraction = false;
        if bytes.get(p) == Some(&b'.') {
            fraction = true;
            p = digits(p + 1)?;
        }
        if matches!(bytes.get(p), Some(b'e' | b'E')) {
            fraction = true;
            p += 1;
            if matches!(bytes.get(p), Some(b'+' | b'-')) {
                p += 1;
            }
            p = digits(p)?;
        }
        if !delimits(bytes, p) {
            let message = if leading_zero && digit(p) {
                "number with a leading zero"
            } else {
                "unexpected character in a number"
            };
            return Err(Error { message, at: p });
        }
        let text = std::str::from_utf8(&bytes[at..p]).expect("ASCII digits");
        let integer = !fraction && (text.parse::<i64>().is_ok() || text.parse::<u64>().is_ok());
        if !integer && text.parse::<f64>().is_ok_and(f64::is_infinite) {
            return Err(Error {
                message: "number out of range",
                at,
            });
        }
        self.push(at, p | if fraction { FLAG as usize } else { 0 });
        self.next += 1;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn error(text: &str) -> Error {
        Tape::parse(text.as_bytes()).expect_err(text)
    }

    #[test]
    fn values() {
        let text = r#"{"a": [1, -2.5e1, "x\u00e9\"", true, false, null, {}], "b": 18446744073709551615, "c": 1e2, "d": 99999999999999999999}"#;
        let tape = Tape::parse(text.as_bytes()).unwrap();
        let doc = tape.doc(text.as_bytes());
        assert_eq!(doc.kind(0), Kind::Object);
        assert_eq!(doc.next(0), tape.len());
        let members: Vec<(String, usize)> = doc
            .members(0)
            .map(|(k, v)| (doc.text(k).into_owned(), v))
            .collect();
        let keys: Vec<&str> = members.iter().map(|(k, _)| k.as_str()).collect();
        assert_eq!(keys, ["a", "b", "c", "d"]);
        let items: Vec<usize> = doc.items(members[0].1).collect();
        assert_eq!(
            items.iter().map(|&i| doc.kind(i)).collect::<Vec<_>>(),
            [
                Kind::Number,
                Kind::Number,
                Kind::String,
                Kind::True,
                Kind::False,
                Kind::Null,
                Kind::Object
            ]
        );
        assert_eq!(doc.number(items[0]), Number::Int(1));
        assert_eq!(doc.number(items[1]), Number::Float(-25.0));
        assert_eq!(doc.text(items[2]), "x\u{e9}\"");
        assert!(doc.is(items[2], "x\u{e9}\""));
        assert!(!doc.is(items[0], "1") && !doc.is(members[0].1, "a"));
        assert_eq!(doc.number(members[1].1), Number::UInt(u64::MAX));
        assert_eq!(doc.number(members[2].1), Number::Float(100.0));
        assert_eq!(doc.number(members[3].1), Number::Float(1e20));
        assert_eq!(doc.members(items[6]).count(), 0);
    }

    #[test]
    fn surrogate_pairs_and_every_simple_escape() {
        let text = r#""\ud83d\ude00 \"\\\/\b\f\n\r\t""#;
        let tape = Tape::parse(text.as_bytes()).unwrap();
        assert_eq!(
            tape.doc(text.as_bytes()).text(0),
            "\u{1f600} \"\\/\u{8}\u{c}\n\r\t"
        );
    }

    #[test]
    fn errors_name_the_first_problem() {
        for (text, message, at) in [
            ("", "expected a value", 0),
            ("  ", "expected a value", 2),
            ("[1, 2,, 3]", "expected a value", 6),
            ("[1,]", "trailing comma", 3),
            ("{\"a\": 1,}", "trailing comma", 8),
            ("{\"a\" 1}", "expected ':' after a key", 5),
            ("{1: 2}", "expected a string key", 1),
            ("[1 2]", "expected ',' or ']'", 3),
            ("{\"a\": 1 \"b\"}", "expected ',' or '}'", 8),
            ("[1}", "expected ',' or ']'", 2),
            ("[1] x", "unexpected content after the document", 4),
            ("\"abc", "unterminated string", 0),
            ("[\"a\\x\"]", "invalid escape", 3),
            ("\"\\ud800\"", "invalid escape", 1),
            ("\"\\udc00\"", "invalid escape", 1),
            ("\"\\ud800\\u0041\"", "invalid escape", 1),
            ("\"\\u12\"", "invalid escape", 1),
            ("01", "number with a leading zero", 1),
            ("1.", "expected a digit", 2),
            ("-", "expected a digit", 1),
            ("1e", "expected a digit", 2),
            ("1x", "unexpected character in a number", 1),
            (".5", "expected a value", 0),
            ("1e400", "number out of range", 0),
            ("nul", "expected a value", 0),
            ("truex", "expected a value", 0),
            ("[\"a\u{1}\"]", "unexpected control character", 3),
            ("\u{c}1", "unexpected control character", 0),
            ("\u{feff}{}", "expected a value", 0),
            ("[1, \\]", "expected a value", 4),
            ("[1, 2, \"a\x01\" , ]", "unexpected control character", 9),
        ] {
            assert_eq!(error(text), Error { message, at }, "{text:?}");
        }
        assert_eq!(
            Tape::parse(b"[\"\xff\"]").unwrap_err(),
            Error {
                message: "invalid UTF-8",
                at: 2
            }
        );
    }

    #[test]
    fn depth_is_limited_as_it_was() {
        let deep = |depth: usize| "[".repeat(depth) + &"]".repeat(depth);
        assert!(Tape::parse(deep(MAX_DEPTH).as_bytes()).is_ok());
        assert_eq!(
            error(&deep(MAX_DEPTH + 1)),
            Error {
                message: "nested too deeply",
                at: MAX_DEPTH
            }
        );
    }

    #[test]
    fn numbers_at_the_edges_of_the_integers() {
        for (text, number) in [
            ("-0", Number::Int(0)),
            ("-0.0", Number::Float(-0.0)),
            ("9223372036854775807", Number::Int(i64::MAX)),
            ("-9223372036854775808", Number::Int(i64::MIN)),
            (
                "-9223372036854775809",
                Number::Float(-9223372036854775809.0),
            ),
            (
                "18446744073709551616",
                Number::Float(18446744073709551616.0),
            ),
            ("1e-400", Number::Float(0.0)),
            ("1E+2", Number::Float(100.0)),
        ] {
            let tape = Tape::parse(text.as_bytes()).unwrap();
            assert_eq!(tape.doc(text.as_bytes()).number(0), number, "{text}");
        }
    }
}
