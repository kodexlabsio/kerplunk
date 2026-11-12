//! YAML, read as PyYAML's safe loader reads it, by the core: the single YAML reader of Konstruct.
//!
//! Every YAML file Konstruct reads goes through `yaml_load` (in `python`), so one reader decides
//! what a text means. It keeps the semantics callers had from PyYAML's `safe_load`, YAML 1.1
//! rather than 1.2: `yes` and `off` are booleans, `017` is an octal integer, `1:30` is an integer
//! of base 60, a date is a `datetime.date`, merge keys `<<` flatten, and a repeated key is the
//! last one. The pieces:
//!
//! - [`reader`] decodes the bytes (UTF-8, or UTF-16 with a byte order mark) and refuses a
//!   character YAML does not allow, with PyYAML's positions.
//! - [`scanner`] is PyYAML's scanner: the tokens of the text, where each is, and what is refused;
//!   [`lines`] finds where a line ends for it.
//! - [`parser`] turns the tokens into events by PyYAML's state machine, which keeps its tags,
//!   anchors, and the error at the token PyYAML reports.
//! - [`resolve`] is PyYAML's implicit resolver: the tag of a plain scalar.
//! - [`compose`] builds the nodes of a document (aliases share one node) and enforces the limits
//!   that make an untrusted file safe to read: nesting depth, and the values aliases add, counted
//!   for a whole stream.
//! - `python` builds the Python values from those nodes, as PyYAML's `SafeConstructor` does, in
//!   the order it visits them: a collection is made first and filled in the next round, so a
//!   document with several faults reports the one PyYAML did.
//!
//! The whole of it follows PyYAML rule for rule, because a scanner for YAML 1.2, such as the
//! `yaml-rust2` crate's, reads some of what callers have written differently: it refuses a quoted
//! scalar or a flow collection that continues at the start of a line, and reads NEL, LS and PS as
//! text and a tab as a space.

pub mod compose;
pub mod lines;
pub mod parser;
pub mod reader;
pub mod resolve;
pub mod scanner;

#[cfg(feature = "python")]
pub mod python;

/// A position in the text: PyYAML's `Mark`, with the line and the column counted from zero.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Mark {
    /// The character index, counting a byte order mark.
    pub index: usize,
    pub line: usize,
    pub column: usize,
}

/// Which stage of reading refused a text. The Python surface raises the PyYAML exception of the
/// same name, so a caller that caught one still does.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Stage {
    /// The text is not valid UTF-8 or UTF-16, or holds a character YAML does not allow.
    Reader,
    Scanner,
    Parser,
    Composer,
    Constructor,
    /// What PyYAML raised besides its own errors: a character past Unicode in an escape.
    Unreadable,
    /// A limit of this reader: nesting depth, the values aliases add, a number's digits.
    Limit,
}

/// What is known of a text that cannot be read: PyYAML's `MarkedYAMLError`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Parts {
    pub stage: Stage,
    pub context: Option<String>,
    pub context_mark: Option<Mark>,
    pub problem: String,
    pub problem_mark: Option<Mark>,
}

/// Why a text cannot be read. It is one pointer, so that the results of the scanner and the
/// parser, which are returned for every token, stay small.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Failure(Box<Parts>);

impl std::ops::Deref for Failure {
    type Target = Parts;

    fn deref(&self) -> &Parts {
        &self.0
    }
}

impl Failure {
    pub fn new(stage: Stage, problem: impl Into<String>, mark: Option<Mark>) -> Failure {
        Failure(Box::new(Parts {
            stage,
            context: None,
            context_mark: None,
            problem: problem.into(),
            problem_mark: mark,
        }))
    }

    /// A failure that says what it was doing, without where it began.
    pub fn during(
        stage: Stage,
        context: impl Into<String>,
        problem: impl Into<String>,
        mark: Mark,
    ) -> Failure {
        Failure(Box::new(Parts {
            stage,
            context: Some(context.into()),
            context_mark: None,
            problem: problem.into(),
            problem_mark: Some(mark),
        }))
    }

    /// A failure that says what it was doing and where it began.
    pub fn within(
        stage: Stage,
        context: impl Into<String>,
        context_mark: Mark,
        problem: impl Into<String>,
        mark: Mark,
    ) -> Failure {
        Failure(Box::new(Parts {
            stage,
            context: Some(context.into()),
            context_mark: Some(context_mark),
            problem: problem.into(),
            problem_mark: Some(mark),
        }))
    }

    pub fn into_parts(self) -> Parts {
        *self.0
    }
}

/// What a read refuses, besides what is not YAML.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Limits {
    /// The most values the aliases of a stream may add, once expanded. Values written out are
    /// not counted, so a large file without aliases is read whole.
    pub max_values: u64,
    /// The deepest nesting of collections, aliases expanded.
    pub max_depth: usize,
    /// The most digits an integer may have.
    pub digits: usize,
}

impl Limits {
    /// One million values from aliases, 250 levels (PyYAML's own recursion gave out between 300
    /// and 400), and Python's limit on the digits of an integer.
    pub const DEFAULT: Limits = Limits {
        max_values: 1_000_000,
        max_depth: 250,
        digits: 4300,
    };
}

/// Whether Python's `repr` writes the character as an escape: the control characters, and the
/// blanks, separators and format characters that `str.isprintable` refuses.
fn escaped(c: char) -> bool {
    c.is_control()
        || matches!(
            c,
            '\u{a0}'
                | '\u{ad}'
                | '\u{1680}'
                | '\u{2000}'..='\u{200f}'
                | '\u{2028}'..='\u{202f}'
                | '\u{205f}'..='\u{2064}'
                | '\u{3000}'
                | '\u{feff}'
                | '\u{e000}'..='\u{f8ff}'
        )
}

/// The marks around a quoted name in a message: Python writes the quotes, as `repr` does, when
/// the message is raised (see [`resolve_reprs`]).
const OPEN: char = '\u{1}';
const CLOSE: char = '\u{2}';

/// `text` as PyYAML's messages quote a name: marked, for [`resolve_reprs`] to give it Python's
/// own `repr`, which no table of the characters Python prints can match.
pub fn repr(text: &str) -> String {
    if text.contains([OPEN, CLOSE]) {
        return approximate_repr(text);
    }
    format!("{OPEN}{text}{CLOSE}")
}

/// `message` with each marked name as `quote` writes it.
pub fn resolve_reprs(message: &str, quote: impl Fn(&str) -> String) -> String {
    let mut out = String::with_capacity(message.len());
    let mut rest = message;
    while let Some(open) = rest.find(OPEN) {
        let Some(close) = rest[open..].find(CLOSE) else {
            break;
        };
        out.push_str(&rest[..open]);
        out.push_str(&quote(&rest[open + 1..open + close]));
        rest = &rest[open + close + 1..];
    }
    out.push_str(rest);
    out
}

/// `message` with its names quoted by [`approximate_repr`], for a test of the words.
#[cfg(test)]
pub fn plain(message: &str) -> String {
    resolve_reprs(message, approximate_repr)
}

/// `text` as Python's `repr` writes a string, as far as the standard library can tell which
/// characters Python prints.
fn approximate_repr(text: &str) -> String {
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
            c if escaped(c) => {
                let code = c as u32;
                if code <= 0xFF {
                    out.push_str(&format!("\\x{code:02x}"));
                } else if code <= 0xFFFF {
                    out.push_str(&format!("\\u{code:04x}"));
                } else {
                    out.push_str(&format!("\\U{code:08x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// `number` with its thousands grouped by commas, as `f'{number:,}'` writes it.
pub fn grouped(number: u64) -> String {
    let digits = number.to_string();
    let mut out = String::with_capacity(digits.len() + digits.len() / 3);
    for (at, digit) in digits.chars().enumerate() {
        if at > 0 && (digits.len() - at).is_multiple_of(3) {
            out.push(',');
        }
        out.push(digit);
    }
    out
}
