//! The tokens of a YAML text: PyYAML's scanner, rule for rule, so that what is a token, where it
//! is, and what is refused are what PyYAML's are. This is YAML 1.1, as PyYAML reads it, which
//! differs from the 1.2 that other scanners read: NEL, LS and PS break lines, a tab cannot start
//! a token, a quoted scalar or a flow collection may continue at any indentation, and `?` and `:`
//! mean what PyYAML says they mean in a flow collection.
//!
//! The text is a `&str` and a position in it, so a token that is a stretch of the text, as nearly
//! every scalar is, borrows it. Marks are counted as PyYAML counts them (characters, lines from
//! zero, a byte order mark not counted in a column) because they reach the errors callers read.

use std::borrow::Cow;
use std::collections::VecDeque;

use super::lines::{break_len, find_break};
use super::{Failure, Mark, Stage, repr};

pub type Scan<T> = Result<T, Failure>;

/// A directive, `%YAML 1.1` or `%TAG ! tag:example.com,2000:`.
#[derive(Debug)]
pub enum Directive<'a> {
    Yaml {
        major: u64,
    },
    Tag {
        handle: Cow<'a, str>,
        prefix: Cow<'a, str>,
    },
    Other,
}

#[derive(Debug)]
pub enum Tok<'a> {
    StreamStart,
    StreamEnd,
    Directive(Directive<'a>),
    DocumentStart,
    DocumentEnd,
    BlockSequenceStart,
    BlockMappingStart,
    BlockEnd,
    FlowSequenceStart,
    FlowMappingStart,
    FlowSequenceEnd,
    FlowMappingEnd,
    BlockEntry,
    FlowEntry,
    Key,
    Value,
    Alias(Cow<'a, str>),
    Anchor(Cow<'a, str>),
    /// The handle (None for a verbatim tag or the non-specific `!`) and the suffix.
    Tag(Option<Cow<'a, str>>, Cow<'a, str>),
    Scalar {
        value: Cow<'a, str>,
        plain: bool,
        /// Whether the value holds an escaped surrogate that no pair completes, which a reader
        /// cannot use: the text has U+FFFD in its place.
        lone: bool,
    },
}

#[derive(Debug)]
pub struct Token<'a> {
    pub tok: Tok<'a>,
    pub start: Mark,
    pub end: Mark,
}

struct SimpleKey {
    token_number: usize,
    required: bool,
    mark: Mark,
}

const BOM: char = '\u{FEFF}';

fn is_break(c: char) -> bool {
    matches!(c, '\r' | '\n' | '\u{85}' | '\u{2028}' | '\u{2029}')
}

/// `'\0 \t\r\n\x85  '`: the end of the text, a blank or a break.
fn is_blankz(c: char) -> bool {
    matches!(c, '\0' | ' ' | '\t') || is_break(c)
}

fn is_breakz(c: char) -> bool {
    c == '\0' || is_break(c)
}

fn is_alnum(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '-' | '_')
}

fn is_hex(c: char) -> bool {
    c.is_ascii_hexdigit()
}

/// A scalar being built: a borrowed stretch of the text until something else is added to it.
enum Chunks<'a> {
    Empty,
    Borrowed(&'a str),
    Owned(String),
}

impl<'a> Chunks<'a> {
    fn push(&mut self, piece: &'a str) {
        match self {
            Chunks::Empty => *self = Chunks::Borrowed(piece),
            Chunks::Borrowed(first) => {
                let mut owned = String::with_capacity(first.len() + piece.len() + 16);
                owned.push_str(first);
                owned.push_str(piece);
                *self = Chunks::Owned(owned);
            }
            Chunks::Owned(text) => text.push_str(piece),
        }
    }

    fn push_owned(&mut self, piece: &str) {
        match self {
            Chunks::Empty => *self = Chunks::Owned(piece.to_owned()),
            Chunks::Borrowed(first) => {
                let mut owned = String::with_capacity(first.len() + piece.len() + 16);
                owned.push_str(first);
                owned.push_str(piece);
                *self = Chunks::Owned(owned);
            }
            Chunks::Owned(text) => text.push_str(piece),
        }
    }

    fn push_char(&mut self, c: char) {
        let mut buffer = [0u8; 4];
        self.push_owned(c.encode_utf8(&mut buffer));
    }

    fn finish(self) -> Cow<'a, str> {
        match self {
            Chunks::Empty => Cow::Borrowed(""),
            Chunks::Borrowed(text) => Cow::Borrowed(text),
            Chunks::Owned(text) => Cow::Owned(text),
        }
    }
}

/// What a plain scalar's line break and the blank lines after it say.
enum Spaces<'a> {
    /// A document separator follows, or nothing separates: the scalar ends.
    End,
    Blanks(&'a str),
    Folded(String),
}

pub struct Scanner<'a> {
    /// The most digits of an integer a directive may write, Python's limit on converting one.
    digits: usize,
    text: &'a str,
    bytes: &'a [u8],
    pos: usize,
    index: usize,
    line: usize,
    column: usize,
    done: bool,
    flow_level: usize,
    tokens: VecDeque<Token<'a>>,
    tokens_taken: usize,
    indent: isize,
    indents: Vec<isize>,
    allow_simple_key: bool,
    /// The possible simple key of each flow level, in the order they were saved.
    possible: Vec<(usize, SimpleKey)>,
}

impl<'a> Scanner<'a> {
    pub fn new(text: &'a str, digits: usize) -> Scanner<'a> {
        let mut scanner = Scanner {
            digits,
            text,
            bytes: text.as_bytes(),
            pos: 0,
            index: 0,
            line: 0,
            column: 0,
            done: false,
            flow_level: 0,
            tokens: VecDeque::new(),
            tokens_taken: 0,
            indent: -1,
            indents: Vec::new(),
            allow_simple_key: true,
            possible: Vec::new(),
        };
        let mark = scanner.mark();
        scanner.tokens.push_back(Token {
            tok: Tok::StreamStart,
            start: mark,
            end: mark,
        });
        scanner
    }

    // The reader's side: positions and characters.

    pub fn mark(&self) -> Mark {
        Mark {
            index: self.index,
            line: self.line,
            column: self.column,
        }
    }

    fn char_at(&self, at: usize) -> char {
        match self.bytes.get(at) {
            None => '\0',
            Some(&b) if b < 0x80 => char::from(b),
            Some(_) => self.text[at..].chars().next().unwrap_or('\0'),
        }
    }

    fn peek(&self) -> char {
        self.char_at(self.pos)
    }

    /// The character `n` characters on.
    fn peek_n(&self, n: usize) -> char {
        let mut at = self.pos;
        for _ in 0..n {
            if at >= self.bytes.len() {
                return '\0';
            }
            at += self.char_at(at).len_utf8();
        }
        self.char_at(at)
    }

    /// Moves past `n` characters, counting lines and columns as PyYAML's reader does.
    fn forward(&mut self, n: usize) {
        for _ in 0..n {
            if self.pos >= self.bytes.len() {
                return;
            }
            let c = self.char_at(self.pos);
            self.pos += c.len_utf8();
            self.index += 1;
            if matches!(c, '\n' | '\u{85}' | '\u{2028}' | '\u{2029}')
                || (c == '\r' && self.char_at(self.pos) != '\n')
            {
                self.line += 1;
                self.column = 0;
            } else if c != BOM {
                self.column += 1;
            }
        }
    }

    /// Moves past `len` bytes of the text that hold no line break.
    fn skip(&mut self, len: usize) {
        let run = &self.bytes[self.pos..self.pos + len];
        if run.is_ascii() {
            self.index += len;
            self.column += len;
        } else {
            for c in self.text[self.pos..self.pos + len].chars() {
                self.index += 1;
                if c != BOM {
                    self.column += 1;
                }
            }
        }
        self.pos += len;
    }

    /// How many bytes from the position are spaces.
    fn spaces(&self) -> usize {
        self.bytes[self.pos..]
            .iter()
            .take_while(|&&b| b == b' ')
            .count()
    }

    /// Moves past `len` bytes of ASCII that are not line breaks.
    fn skip_ascii(&mut self, len: usize) {
        self.pos += len;
        self.index += len;
        self.column += len;
    }

    /// How many bytes from the position are made of characters `keep` accepts.
    fn run(&self, keep: impl Fn(char) -> bool) -> usize {
        let mut at = self.pos;
        loop {
            let c = self.char_at(at);
            if at >= self.bytes.len() || !keep(c) {
                return at - self.pos;
            }
            at += c.len_utf8();
        }
    }

    fn slice(&self, len: usize) -> &'a str {
        &self.text[self.pos..self.pos + len]
    }

    fn error<T>(&self, context: Option<(&str, Mark)>, problem: String) -> Scan<T> {
        Err(match context {
            Some((context, mark)) => {
                Failure::within(Stage::Scanner, context, mark, problem, self.mark())
            }
            None => Failure::new(Stage::Scanner, problem, Some(self.mark())),
        })
    }

    // Tokens.

    /// The next token, not taken.
    pub fn peek_token(&mut self) -> Scan<&Token<'a>> {
        while self.need_more_tokens()? {
            self.fetch_more_tokens()?;
        }
        self.tokens
            .front()
            .ok_or_else(|| Failure::new(Stage::Scanner, "the stream has ended", Some(self.mark())))
    }

    pub fn get_token(&mut self) -> Scan<Token<'a>> {
        while self.need_more_tokens()? {
            self.fetch_more_tokens()?;
        }
        self.tokens_taken += 1;
        self.tokens
            .pop_front()
            .ok_or_else(|| Failure::new(Stage::Scanner, "the stream has ended", Some(self.mark())))
    }

    fn need_more_tokens(&mut self) -> Scan<bool> {
        if self.done {
            return Ok(false);
        }
        if self.tokens.is_empty() {
            return Ok(true);
        }
        // The current token may be a possible simple key, so we need to look further.
        self.stale_possible_simple_keys()?;
        Ok(self.next_possible_simple_key() == Some(self.tokens_taken))
    }

    fn push(&mut self, tok: Tok<'a>, start: Mark, end: Mark) {
        self.tokens.push_back(Token { tok, start, end });
    }

    fn fetch_more_tokens(&mut self) -> Scan<()> {
        self.scan_to_next_token();
        self.stale_possible_simple_keys()?;
        self.unwind_indent(self.column as isize);
        let ch = self.peek();
        if self.pos >= self.bytes.len() {
            return self.fetch_stream_end();
        }
        if ch == '%' && self.column == 0 {
            return self.fetch_directive();
        }
        if ch == '-' && self.check_document_indicator("---") {
            return self.fetch_document_indicator(Tok::DocumentStart);
        }
        if ch == '.' && self.check_document_indicator("...") {
            return self.fetch_document_indicator(Tok::DocumentEnd);
        }
        match ch {
            '[' => return self.fetch_flow_collection_start(Tok::FlowSequenceStart),
            '{' => return self.fetch_flow_collection_start(Tok::FlowMappingStart),
            ']' => return self.fetch_flow_collection_end(Tok::FlowSequenceEnd),
            '}' => return self.fetch_flow_collection_end(Tok::FlowMappingEnd),
            ',' => return self.fetch_flow_entry(),
            _ => {}
        }
        if ch == '-' && is_blankz(self.peek_n(1)) {
            return self.fetch_block_entry();
        }
        if ch == '?' && (self.flow_level > 0 || is_blankz(self.peek_n(1))) {
            return self.fetch_key();
        }
        if ch == ':' && (self.flow_level > 0 || is_blankz(self.peek_n(1))) {
            return self.fetch_value();
        }
        match ch {
            '*' => return self.fetch_alias(true),
            '&' => return self.fetch_alias(false),
            '!' => return self.fetch_tag(),
            '|' if self.flow_level == 0 => return self.fetch_block_scalar(false),
            '>' if self.flow_level == 0 => return self.fetch_block_scalar(true),
            '\'' => return self.fetch_flow_scalar(false),
            '"' => return self.fetch_flow_scalar(true),
            _ => {}
        }
        if self.check_plain() {
            return self.fetch_plain();
        }
        Err(Failure::during(
            Stage::Scanner,
            "while scanning for the next token",
            format!(
                "found character {} that cannot start any token",
                repr(&ch.to_string())
            ),
            self.mark(),
        ))
    }

    // Simple keys.

    fn next_possible_simple_key(&self) -> Option<usize> {
        self.possible.iter().map(|(_, key)| key.token_number).min()
    }

    fn stale_possible_simple_keys(&mut self) -> Scan<()> {
        let mut at = 0;
        while at < self.possible.len() {
            let key = &self.possible[at].1;
            if key.mark.line != self.line || self.index - key.mark.index > 1024 {
                if key.required {
                    return Err(Failure::within(
                        Stage::Scanner,
                        "while scanning a simple key",
                        key.mark,
                        "could not find expected ':'",
                        self.mark(),
                    ));
                }
                self.possible.remove(at);
            } else {
                at += 1;
            }
        }
        Ok(())
    }

    fn save_possible_simple_key(&mut self) -> Scan<()> {
        let required = self.flow_level == 0 && self.indent == self.column as isize;
        if self.allow_simple_key {
            self.remove_possible_simple_key()?;
            self.possible.push((
                self.flow_level,
                SimpleKey {
                    token_number: self.tokens_taken + self.tokens.len(),
                    required,
                    mark: self.mark(),
                },
            ));
        }
        Ok(())
    }

    fn remove_possible_simple_key(&mut self) -> Scan<()> {
        if let Some(at) = self
            .possible
            .iter()
            .position(|(level, _)| *level == self.flow_level)
        {
            let key = &self.possible[at].1;
            if key.required {
                return Err(Failure::within(
                    Stage::Scanner,
                    "while scanning a simple key",
                    key.mark,
                    "could not find expected ':'",
                    self.mark(),
                ));
            }
            self.possible.remove(at);
        }
        Ok(())
    }

    // Indentation.

    fn unwind_indent(&mut self, column: isize) {
        // In the flow context indentation is ignored.
        if self.flow_level > 0 {
            return;
        }
        while self.indent > column {
            let mark = self.mark();
            self.indent = self.indents.pop().unwrap_or(-1);
            self.push(Tok::BlockEnd, mark, mark);
        }
    }

    fn add_indent(&mut self, column: usize) -> bool {
        if self.indent < column as isize {
            self.indents.push(self.indent);
            self.indent = column as isize;
            return true;
        }
        false
    }

    // Fetchers.

    fn fetch_stream_end(&mut self) -> Scan<()> {
        self.unwind_indent(-1);
        self.remove_possible_simple_key()?;
        self.allow_simple_key = false;
        self.possible.clear();
        let mark = self.mark();
        self.push(Tok::StreamEnd, mark, mark);
        self.done = true;
        Ok(())
    }

    fn fetch_directive(&mut self) -> Scan<()> {
        self.unwind_indent(-1);
        self.remove_possible_simple_key()?;
        self.allow_simple_key = false;
        let token = self.scan_directive()?;
        self.tokens.push_back(token);
        Ok(())
    }

    fn check_document_indicator(&self, indicator: &str) -> bool {
        self.column == 0
            && self.text[self.pos..].starts_with(indicator)
            && is_blankz(self.char_at(self.pos + 3))
    }

    fn fetch_document_indicator(&mut self, tok: Tok<'a>) -> Scan<()> {
        self.unwind_indent(-1);
        self.remove_possible_simple_key()?;
        self.allow_simple_key = false;
        let start = self.mark();
        self.forward(3);
        let end = self.mark();
        self.push(tok, start, end);
        Ok(())
    }

    fn fetch_flow_collection_start(&mut self, tok: Tok<'a>) -> Scan<()> {
        self.save_possible_simple_key()?;
        self.flow_level += 1;
        self.allow_simple_key = true;
        let start = self.mark();
        self.forward(1);
        let end = self.mark();
        self.push(tok, start, end);
        Ok(())
    }

    fn fetch_flow_collection_end(&mut self, tok: Tok<'a>) -> Scan<()> {
        self.remove_possible_simple_key()?;
        // A closing bracket without an opening one leaves the level where it is, as PyYAML's
        // counter goes below zero and the parser refuses the token.
        self.flow_level = self.flow_level.saturating_sub(1);
        self.allow_simple_key = false;
        let start = self.mark();
        self.forward(1);
        let end = self.mark();
        self.push(tok, start, end);
        Ok(())
    }

    fn fetch_flow_entry(&mut self) -> Scan<()> {
        self.allow_simple_key = true;
        self.remove_possible_simple_key()?;
        let start = self.mark();
        self.forward(1);
        let end = self.mark();
        self.push(Tok::FlowEntry, start, end);
        Ok(())
    }

    fn fetch_block_entry(&mut self) -> Scan<()> {
        if self.flow_level == 0 {
            if !self.allow_simple_key {
                return self.error(None, "sequence entries are not allowed here".into());
            }
            if self.add_indent(self.column) {
                let mark = self.mark();
                self.push(Tok::BlockSequenceStart, mark, mark);
            }
        }
        self.allow_simple_key = true;
        self.remove_possible_simple_key()?;
        let start = self.mark();
        self.forward(1);
        let end = self.mark();
        self.push(Tok::BlockEntry, start, end);
        Ok(())
    }

    fn fetch_key(&mut self) -> Scan<()> {
        if self.flow_level == 0 {
            if !self.allow_simple_key {
                return self.error(None, "mapping keys are not allowed here".into());
            }
            if self.add_indent(self.column) {
                let mark = self.mark();
                self.push(Tok::BlockMappingStart, mark, mark);
            }
        }
        self.allow_simple_key = self.flow_level == 0;
        self.remove_possible_simple_key()?;
        let start = self.mark();
        self.forward(1);
        let end = self.mark();
        self.push(Tok::Key, start, end);
        Ok(())
    }

    fn fetch_value(&mut self) -> Scan<()> {
        if let Some(at) = self
            .possible
            .iter()
            .position(|(level, _)| *level == self.flow_level)
        {
            let (_, key) = self.possible.remove(at);
            let place = key.token_number - self.tokens_taken;
            self.tokens.insert(
                place,
                Token {
                    tok: Tok::Key,
                    start: key.mark,
                    end: key.mark,
                },
            );
            if self.flow_level == 0 && self.add_indent(key.mark.column) {
                self.tokens.insert(
                    place,
                    Token {
                        tok: Tok::BlockMappingStart,
                        start: key.mark,
                        end: key.mark,
                    },
                );
            }
            self.allow_simple_key = false;
        } else {
            if self.flow_level == 0 {
                if !self.allow_simple_key {
                    return self.error(None, "mapping values are not allowed here".into());
                }
                if self.add_indent(self.column) {
                    let mark = self.mark();
                    self.push(Tok::BlockMappingStart, mark, mark);
                }
            }
            self.allow_simple_key = self.flow_level == 0;
            self.remove_possible_simple_key()?;
        }
        let start = self.mark();
        self.forward(1);
        let end = self.mark();
        self.push(Tok::Value, start, end);
        Ok(())
    }

    fn fetch_alias(&mut self, alias: bool) -> Scan<()> {
        self.save_possible_simple_key()?;
        self.allow_simple_key = false;
        let token = self.scan_anchor(alias)?;
        self.tokens.push_back(token);
        Ok(())
    }

    fn fetch_tag(&mut self) -> Scan<()> {
        self.save_possible_simple_key()?;
        self.allow_simple_key = false;
        let token = self.scan_tag()?;
        self.tokens.push_back(token);
        Ok(())
    }

    fn fetch_block_scalar(&mut self, folded: bool) -> Scan<()> {
        self.allow_simple_key = true;
        self.remove_possible_simple_key()?;
        let token = self.scan_block_scalar(folded)?;
        self.tokens.push_back(token);
        Ok(())
    }

    fn fetch_flow_scalar(&mut self, double: bool) -> Scan<()> {
        self.save_possible_simple_key()?;
        self.allow_simple_key = false;
        let token = self.scan_flow_scalar(double)?;
        self.tokens.push_back(token);
        Ok(())
    }

    fn fetch_plain(&mut self) -> Scan<()> {
        self.save_possible_simple_key()?;
        self.allow_simple_key = false;
        let token = self.scan_plain()?;
        self.tokens.push_back(token);
        Ok(())
    }

    fn check_plain(&self) -> bool {
        let ch = self.peek();
        !(is_blankz(ch)
            || matches!(
                ch,
                '-' | '?'
                    | ':'
                    | ','
                    | '['
                    | ']'
                    | '{'
                    | '}'
                    | '#'
                    | '&'
                    | '*'
                    | '!'
                    | '|'
                    | '>'
                    | '\''
                    | '"'
                    | '%'
                    | '@'
                    | '`'
            ))
            || (!is_blankz(self.peek_n(1))
                && (ch == '-' || (self.flow_level == 0 && (ch == '?' || ch == ':'))))
    }

    // Scanners.

    /// Skips spaces, comments and line breaks. A line break in the block context allows a simple
    /// key. The byte order mark is stripped if it is the first character of the stream.
    fn scan_to_next_token(&mut self) {
        if self.index == 0 && self.peek() == BOM {
            self.forward(1);
        }
        loop {
            let spaces = self.spaces();
            self.skip_ascii(spaces);
            if self.peek() == '#' {
                let comment = find_break(self.bytes, self.pos) - self.pos;
                self.skip(comment);
            }
            if self.scan_line_break().is_some() {
                if self.flow_level == 0 {
                    self.allow_simple_key = true;
                }
            } else {
                return;
            }
        }
    }

    fn scan_directive(&mut self) -> Scan<Token<'a>> {
        let start = self.mark();
        self.forward(1);
        let name = self.scan_directive_name(start)?;
        let directive;
        let end;
        if name == "YAML" {
            directive = self.scan_yaml_directive_value(start)?;
            end = self.mark();
        } else if name == "TAG" {
            directive = self.scan_tag_directive_value(start)?;
            end = self.mark();
        } else {
            end = self.mark();
            let rest = self.run(|c| !is_breakz(c));
            self.skip(rest);
            directive = Directive::Other;
        }
        self.scan_directive_ignored_line(start)?;
        Ok(Token {
            tok: Tok::Directive(directive),
            start,
            end,
        })
    }

    fn scan_directive_name(&mut self, start: Mark) -> Scan<&'a str> {
        let length = self.run(is_alnum);
        if length == 0 {
            return self.error(
                Some(("while scanning a directive", start)),
                format!(
                    "expected alphabetic or numeric character, but found {}",
                    repr(&self.peek().to_string())
                ),
            );
        }
        let value = self.slice(length);
        self.skip(length);
        let ch = self.peek();
        if !(is_breakz(ch) || ch == ' ') {
            return self.error(
                Some(("while scanning a directive", start)),
                format!(
                    "expected alphabetic or numeric character, but found {}",
                    repr(&ch.to_string())
                ),
            );
        }
        Ok(value)
    }

    fn scan_yaml_directive_value(&mut self, start: Mark) -> Scan<Directive<'a>> {
        let spaces = self.run(|c| c == ' ');
        self.skip(spaces);
        let major = self.scan_yaml_directive_number(start)?;
        if self.peek() != '.' {
            return self.error(
                Some(("while scanning a directive", start)),
                format!(
                    "expected a digit or '.', but found {}",
                    repr(&self.peek().to_string())
                ),
            );
        }
        self.forward(1);
        self.scan_yaml_directive_number(start)?;
        let ch = self.peek();
        if !(is_breakz(ch) || ch == ' ') {
            return self.error(
                Some(("while scanning a directive", start)),
                format!(
                    "expected a digit or ' ', but found {}",
                    repr(&ch.to_string())
                ),
            );
        }
        Ok(Directive::Yaml { major })
    }

    fn scan_yaml_directive_number(&mut self, start: Mark) -> Scan<u64> {
        let ch = self.peek();
        if !ch.is_ascii_digit() {
            return self.error(
                Some(("while scanning a directive", start)),
                format!("expected a digit, but found {}", repr(&ch.to_string())),
            );
        }
        let length = self.run(|c| c.is_ascii_digit());
        if length > self.digits {
            // `int()` of the digits refuses them, so the text is not read.
            return Err(Failure::new(
                Stage::Unreadable,
                format!(
                    "Exceeds the limit ({} digits) for integer string conversion: value has {length} digits; use sys.set_int_max_str_digits() to increase the limit",
                    self.digits
                ),
                Some(self.mark()),
            ));
        }
        let value = self.slice(length).parse::<u64>().unwrap_or(u64::MAX);
        self.skip(length);
        Ok(value)
    }

    fn scan_tag_directive_value(&mut self, start: Mark) -> Scan<Directive<'a>> {
        let spaces = self.run(|c| c == ' ');
        self.skip(spaces);
        let handle = self.scan_tag_handle("directive", start)?;
        let ch = self.peek();
        if ch != ' ' {
            return self.error(
                Some(("while scanning a directive", start)),
                format!("expected ' ', but found {}", repr(&ch.to_string())),
            );
        }
        let spaces = self.run(|c| c == ' ');
        self.skip(spaces);
        let prefix = self.scan_tag_uri("directive", start)?;
        let ch = self.peek();
        if !(is_breakz(ch) || ch == ' ') {
            return self.error(
                Some(("while scanning a directive", start)),
                format!("expected ' ', but found {}", repr(&ch.to_string())),
            );
        }
        Ok(Directive::Tag {
            handle: Cow::Borrowed(handle),
            prefix,
        })
    }

    fn scan_directive_ignored_line(&mut self, start: Mark) -> Scan<()> {
        let spaces = self.run(|c| c == ' ');
        self.skip(spaces);
        if self.peek() == '#' {
            let comment = self.run(|c| !is_breakz(c));
            self.skip(comment);
        }
        let ch = self.peek();
        if !is_breakz(ch) {
            return self.error(
                Some(("while scanning a directive", start)),
                format!(
                    "expected a comment or a line break, but found {}",
                    repr(&ch.to_string())
                ),
            );
        }
        self.scan_line_break();
        Ok(())
    }

    fn scan_anchor(&mut self, alias: bool) -> Scan<Token<'a>> {
        let start = self.mark();
        let name = if alias { "alias" } else { "anchor" };
        self.forward(1);
        let length = self.run(is_alnum);
        if length == 0 {
            return self.error(
                Some((&format!("while scanning an {name}"), start)),
                format!(
                    "expected alphabetic or numeric character, but found {}",
                    repr(&self.peek().to_string())
                ),
            );
        }
        let value = self.slice(length);
        self.skip(length);
        let ch = self.peek();
        if !(is_blankz(ch) || "?:,]}%@`".contains(ch)) {
            return self.error(
                Some((&format!("while scanning an {name}"), start)),
                format!(
                    "expected alphabetic or numeric character, but found {}",
                    repr(&ch.to_string())
                ),
            );
        }
        let end = self.mark();
        let value = Cow::Borrowed(value);
        Ok(Token {
            tok: if alias {
                Tok::Alias(value)
            } else {
                Tok::Anchor(value)
            },
            start,
            end,
        })
    }

    fn scan_tag(&mut self) -> Scan<Token<'a>> {
        let start = self.mark();
        let ch = self.peek_n(1);
        let handle: Option<Cow<'a, str>>;
        let suffix: Cow<'a, str>;
        if ch == '<' {
            handle = None;
            self.forward(2);
            suffix = self.scan_tag_uri("tag", start)?;
            if self.peek() != '>' {
                return self.error(
                    Some(("while parsing a tag", start)),
                    format!("expected '>', but found {}", repr(&self.peek().to_string())),
                );
            }
            self.forward(1);
        } else if is_blankz(ch) {
            handle = None;
            suffix = Cow::Borrowed("!");
            self.forward(1);
        } else {
            let mut at = self.pos + 1;
            let mut use_handle = false;
            loop {
                let ch = self.char_at(at);
                if at >= self.bytes.len() || is_breakz(ch) || ch == ' ' {
                    break;
                }
                if ch == '!' {
                    use_handle = true;
                    break;
                }
                at += ch.len_utf8();
            }
            if use_handle {
                handle = Some(Cow::Borrowed(self.scan_tag_handle("tag", start)?));
            } else {
                handle = Some(Cow::Borrowed("!"));
                self.forward(1);
            }
            suffix = self.scan_tag_uri("tag", start)?;
        }
        let ch = self.peek();
        if !(is_breakz(ch) || ch == ' ') {
            return self.error(
                Some(("while scanning a tag", start)),
                format!("expected ' ', but found {}", repr(&ch.to_string())),
            );
        }
        let end = self.mark();
        Ok(Token {
            tok: Tok::Tag(handle, suffix),
            start,
            end,
        })
    }

    fn scan_block_scalar(&mut self, folded: bool) -> Scan<Token<'a>> {
        let mut chunks = String::new();
        let start = self.mark();
        self.forward(1);
        let (chomping, increment) = self.scan_block_scalar_indicators(start)?;
        self.scan_block_scalar_ignored_line(start)?;
        let min_indent = (self.indent + 1).max(1);
        let mut breaks = String::new();
        let mut end;
        let indent: isize;
        if let Some(increment) = increment {
            indent = min_indent + increment as isize - 1;
            end = self.scan_block_scalar_breaks(indent, &mut breaks);
        } else {
            let (max_indent, mark) = self.scan_block_scalar_indentation(&mut breaks);
            end = mark;
            indent = min_indent.max(max_indent as isize);
        }
        let mut line_break: Option<char> = None;
        while self.column as isize == indent && self.pos < self.bytes.len() {
            chunks.push_str(&breaks);
            let leading_non_space = !matches!(self.peek(), ' ' | '\t');
            let length = find_break(self.bytes, self.pos) - self.pos;
            chunks.push_str(self.slice(length));
            self.skip(length);
            line_break = self.scan_line_break();
            breaks.clear();
            end = self.scan_block_scalar_breaks(indent, &mut breaks);
            if self.column as isize == indent && self.pos < self.bytes.len() {
                // Folding rules are ambiguous; this is the specification's.
                if folded
                    && line_break == Some('\n')
                    && leading_non_space
                    && !matches!(self.peek(), ' ' | '\t')
                {
                    if breaks.is_empty() {
                        chunks.push(' ');
                    }
                } else if let Some(c) = line_break {
                    chunks.push(c);
                }
            } else {
                break;
            }
        }
        if chomping != Some(false)
            && let Some(c) = line_break
        {
            chunks.push(c);
        }
        if chomping == Some(true) {
            chunks.push_str(&breaks);
        }
        Ok(Token {
            tok: Tok::Scalar {
                value: Cow::Owned(chunks),
                plain: false,
                lone: false,
            },
            start,
            end,
        })
    }

    fn scan_block_scalar_indicators(&mut self, start: Mark) -> Scan<(Option<bool>, Option<usize>)> {
        let mut chomping = None;
        let mut increment = None;
        let zero = |scanner: &Scanner<'a>| {
            scanner.error::<()>(
                Some(("while scanning a block scalar", start)),
                "expected indentation indicator in the range 1-9, but found 0".into(),
            )
        };
        let mut ch = self.peek();
        if ch == '+' || ch == '-' {
            chomping = Some(ch == '+');
            self.forward(1);
            ch = self.peek();
            if ch.is_ascii_digit() {
                let digit = ch as usize - '0' as usize;
                if digit == 0 {
                    zero(self)?;
                }
                increment = Some(digit);
                self.forward(1);
            }
        } else if ch.is_ascii_digit() {
            let digit = ch as usize - '0' as usize;
            if digit == 0 {
                zero(self)?;
            }
            increment = Some(digit);
            self.forward(1);
            ch = self.peek();
            if ch == '+' || ch == '-' {
                chomping = Some(ch == '+');
                self.forward(1);
            }
        }
        let ch = self.peek();
        if !(is_breakz(ch) || ch == ' ') {
            return self.error(
                Some(("while scanning a block scalar", start)),
                format!(
                    "expected chomping or indentation indicators, but found {}",
                    repr(&ch.to_string())
                ),
            );
        }
        Ok((chomping, increment))
    }

    fn scan_block_scalar_ignored_line(&mut self, start: Mark) -> Scan<()> {
        let spaces = self.run(|c| c == ' ');
        self.skip(spaces);
        if self.peek() == '#' {
            let comment = self.run(|c| !is_breakz(c));
            self.skip(comment);
        }
        let ch = self.peek();
        if !is_breakz(ch) {
            return self.error(
                Some(("while scanning a block scalar", start)),
                format!(
                    "expected a comment or a line break, but found {}",
                    repr(&ch.to_string())
                ),
            );
        }
        self.scan_line_break();
        Ok(())
    }

    fn scan_block_scalar_indentation(&mut self, chunks: &mut String) -> (usize, Mark) {
        let mut max_indent = 0;
        let mut end = self.mark();
        loop {
            let spaces = self.spaces();
            if spaces > 0 {
                self.skip_ascii(spaces);
                max_indent = max_indent.max(self.column);
            }
            match self.scan_line_break() {
                Some(c) => {
                    chunks.push(c);
                    end = self.mark();
                }
                None => return (max_indent, end),
            }
        }
    }

    /// Skips up to `indent` columns of spaces, then the blank lines, adding their breaks to
    /// `chunks`; the mark is the end of the last break.
    fn scan_block_scalar_breaks(&mut self, indent: isize, chunks: &mut String) -> Mark {
        let mut end = self.mark();
        self.skip_indent(indent);
        while let Some(c) = self.scan_line_break() {
            chunks.push(c);
            end = self.mark();
            self.skip_indent(indent);
        }
        end
    }

    /// Skips spaces while the column is under `indent`.
    fn skip_indent(&mut self, indent: isize) {
        let room = indent - self.column as isize;
        if room > 0 {
            let spaces = self.spaces().min(room as usize);
            self.skip_ascii(spaces);
        }
    }

    fn scan_flow_scalar(&mut self, double: bool) -> Scan<Token<'a>> {
        let mut chunks = Chunks::Empty;
        let mut lone = false;
        let start = self.mark();
        let quote = self.peek();
        self.forward(1);
        self.scan_flow_scalar_non_spaces(double, start, &mut chunks, &mut lone)?;
        while self.peek() != quote {
            self.scan_flow_scalar_spaces(double, start, &mut chunks)?;
            self.scan_flow_scalar_non_spaces(double, start, &mut chunks, &mut lone)?;
        }
        self.forward(1);
        let end = self.mark();
        Ok(Token {
            tok: Tok::Scalar {
                value: chunks.finish(),
                plain: false,
                lone,
            },
            start,
            end,
        })
    }

    /// How many bytes from the position are text in a quoted scalar: up to a quote, a backslash,
    /// a blank, a line break or the end, and whether they are all ASCII.
    fn quoted_run(&self) -> (usize, bool) {
        let bytes = self.bytes;
        let mut at = self.pos;
        let mut ascii = true;
        while at < bytes.len() {
            let b = bytes[at];
            match b {
                b'\'' | b'"' | b'\\' | b' ' | b'\t' | b'\r' | b'\n' => break,
                0..=0x7F => at += 1,
                0xC2 | 0xE2 if break_len(bytes, at) > 0 => break,
                _ => {
                    ascii = false;
                    at += if b >= 0xF0 {
                        4
                    } else if b >= 0xE0 {
                        3
                    } else {
                        2
                    };
                }
            }
        }
        (at - self.pos, ascii)
    }

    fn scan_flow_scalar_non_spaces(
        &mut self,
        double: bool,
        start: Mark,
        chunks: &mut Chunks<'a>,
        lone: &mut bool,
    ) -> Scan<()> {
        // A high surrogate escaped as \uD83D waits here for the low one that completes it.
        let mut high: Option<u32> = None;
        let flush = |chunks: &mut Chunks<'a>, high: &mut Option<u32>, lone: &mut bool| {
            if high.take().is_some() {
                chunks.push_char('\u{FFFD}');
                *lone = true;
            }
        };
        loop {
            let (length, ascii) = self.quoted_run();
            if length > 0 {
                flush(chunks, &mut high, lone);
                chunks.push(self.slice(length));
                if ascii {
                    self.skip_ascii(length);
                } else {
                    self.skip(length);
                }
            }
            let ch = self.peek();
            if !double && ch == '\'' && self.peek_n(1) == '\'' {
                flush(chunks, &mut high, lone);
                chunks.push_owned("'");
                self.forward(2);
            } else if (double && ch == '\'') || (!double && (ch == '"' || ch == '\\')) {
                flush(chunks, &mut high, lone);
                chunks.push_char(ch);
                self.forward(1);
            } else if double && ch == '\\' {
                self.forward(1);
                let ch = self.peek();
                let replacement = match ch {
                    '0' => Some('\0'),
                    'a' => Some('\x07'),
                    'b' => Some('\x08'),
                    't' | '\t' => Some('\t'),
                    'n' => Some('\n'),
                    'v' => Some('\x0B'),
                    'f' => Some('\x0C'),
                    'r' => Some('\r'),
                    'e' => Some('\x1B'),
                    ' ' => Some(' '),
                    '"' => Some('"'),
                    '\\' => Some('\\'),
                    '/' => Some('/'),
                    'N' => Some('\u{85}'),
                    '_' => Some('\u{A0}'),
                    'L' => Some('\u{2028}'),
                    'P' => Some('\u{2029}'),
                    _ => None,
                };
                if let Some(replacement) = replacement {
                    flush(chunks, &mut high, lone);
                    chunks.push_char(replacement);
                    self.forward(1);
                } else if let Some(length) = match ch {
                    'x' => Some(2),
                    'u' => Some(4),
                    'U' => Some(8),
                    _ => None,
                } {
                    self.forward(1);
                    for k in 0..length {
                        if !is_hex(self.peek_n(k)) {
                            return self.error(
                                Some(("while scanning a double-quoted scalar", start)),
                                format!(
                                    "expected escape sequence of {length} hexadecimal numbers, but found {}",
                                    repr(&self.peek_n(k).to_string())
                                ),
                            );
                        }
                    }
                    let code = u64::from_str_radix(self.slice(length), 16).unwrap_or(u64::MAX);
                    if code > 0x10FFFF {
                        return Err(Failure::new(
                            Stage::Unreadable,
                            "chr() arg not in range(0x110000)",
                            Some(self.mark()),
                        ));
                    }
                    let code = code as u32;
                    match char::from_u32(code) {
                        Some(c) => {
                            flush(chunks, &mut high, lone);
                            chunks.push_char(c);
                        }
                        None if (0xD800..0xDC00).contains(&code) => {
                            flush(chunks, &mut high, lone);
                            high = Some(code);
                        }
                        None => match high.take() {
                            Some(first) => {
                                let joined = 0x10000 + ((first - 0xD800) << 10) + (code - 0xDC00);
                                chunks.push_char(char::from_u32(joined).unwrap_or('\u{FFFD}'));
                            }
                            None => {
                                chunks.push_char('\u{FFFD}');
                                *lone = true;
                            }
                        },
                    }
                    self.forward(length);
                } else if is_break(ch) {
                    self.scan_line_break();
                    let mut breaks = String::new();
                    self.scan_flow_scalar_breaks(start, &mut breaks)?;
                    if !breaks.is_empty() {
                        // Blank lines are text between the halves of a pair.
                        flush(chunks, &mut high, lone);
                    }
                    chunks.push_owned(&breaks);
                } else {
                    return self.error(
                        Some(("while scanning a double-quoted scalar", start)),
                        format!("found unknown escape character {}", repr(&ch.to_string())),
                    );
                }
            } else {
                flush(chunks, &mut high, lone);
                return Ok(());
            }
        }
    }

    fn scan_flow_scalar_spaces(
        &mut self,
        _double: bool,
        start: Mark,
        chunks: &mut Chunks<'a>,
    ) -> Scan<()> {
        let length = self.bytes[self.pos..]
            .iter()
            .take_while(|&&b| b == b' ' || b == b'\t')
            .count();
        let whitespaces = self.slice(length);
        self.skip_ascii(length);
        if self.pos >= self.bytes.len() {
            return self.error(
                Some(("while scanning a quoted scalar", start)),
                "found unexpected end of stream".into(),
            );
        } else if let Some(line_break) = self.scan_line_break() {
            let mut breaks = String::new();
            self.scan_flow_scalar_breaks(start, &mut breaks)?;
            if line_break != '\n' {
                chunks.push_char(line_break);
            } else if breaks.is_empty() {
                chunks.push_owned(" ");
            }
            chunks.push_owned(&breaks);
        } else {
            chunks.push(whitespaces);
        }
        Ok(())
    }

    /// The line breaks after a line break in a quoted scalar, which are its blank lines, added to
    /// `breaks`.
    fn scan_flow_scalar_breaks(&mut self, start: Mark, breaks: &mut String) -> Scan<()> {
        loop {
            // Instead of checking indentation, we check for document separators.
            if self.at_document_indicator() {
                return self.error(
                    Some(("while scanning a quoted scalar", start)),
                    "found unexpected document separator".into(),
                );
            }
            let blanks = self.bytes[self.pos..]
                .iter()
                .take_while(|&&b| b == b' ' || b == b'\t')
                .count();
            self.skip_ascii(blanks);
            match self.scan_line_break() {
                Some(c) => breaks.push(c),
                None => return Ok(()),
            }
        }
    }

    /// How many bytes from the position a plain scalar's stretch of text reaches, and whether
    /// they are all ASCII: it ends at a blank, a line break, the end of the text, a `:` that a
    /// blank follows (or a flow indicator, in a flow collection) and, in a flow collection, at
    /// `,`, `?`, `[`, `]`, `{` and `}`.
    fn plain_run(&self, flow: bool) -> (usize, bool) {
        let bytes = self.bytes;
        let mut at = self.pos;
        let mut ascii = true;
        while at < bytes.len() {
            let b = bytes[at];
            match b {
                b' ' | b'\t' | b'\r' | b'\n' => break,
                b':' => {
                    let next = bytes.get(at + 1).copied();
                    let blank = match next {
                        None | Some(b' ' | b'\t' | b'\r' | b'\n') => true,
                        Some(b',' | b'[' | b']' | b'{' | b'}') => flow,
                        Some(0xC2 | 0xE2) => break_len(bytes, at + 1) > 0,
                        _ => false,
                    };
                    if blank {
                        break;
                    }
                    at += 1;
                }
                b',' | b'?' | b'[' | b']' | b'{' | b'}' => {
                    if flow {
                        break;
                    }
                    at += 1;
                }
                0..=0x7F => at += 1,
                0xC2 | 0xE2 if break_len(bytes, at) > 0 => break,
                _ => {
                    ascii = false;
                    at += if b >= 0xF0 {
                        4
                    } else if b >= 0xE0 {
                        3
                    } else {
                        2
                    };
                }
            }
        }
        (at - self.pos, ascii)
    }

    fn scan_plain(&mut self) -> Scan<Token<'a>> {
        let mut chunks = Chunks::Empty;
        let start = self.mark();
        let mut end = start;
        let indent = self.indent + 1;
        let flow = self.flow_level > 0;
        let mut spaces: Option<Spaces<'a>> = None;
        loop {
            if self.peek() == '#' {
                break;
            }
            let (length, ascii) = self.plain_run(flow);
            if length == 0 {
                break;
            }
            self.allow_simple_key = false;
            match spaces.take() {
                Some(Spaces::Blanks(blanks)) => chunks.push(blanks),
                Some(Spaces::Folded(folded)) => chunks.push_owned(&folded),
                Some(Spaces::End) | None => {}
            }
            chunks.push(self.slice(length));
            if ascii {
                self.skip_ascii(length);
            } else {
                self.skip(length);
            }
            end = self.mark();
            let next = self.scan_plain_spaces();
            let stop = matches!(next, Spaces::End)
                || self.peek() == '#'
                || (!flow && (self.column as isize) < indent);
            spaces = Some(next);
            if stop {
                break;
            }
        }
        Ok(Token {
            tok: Tok::Scalar {
                value: chunks.finish(),
                plain: true,
                lone: false,
            },
            start,
            end,
        })
    }

    /// What follows a stretch of a plain scalar: blanks, or a line break and the blank lines
    /// after it, folded; or the end of the scalar.
    fn scan_plain_spaces(&mut self) -> Spaces<'a> {
        let length = self.spaces();
        let whitespaces = self.slice(length);
        self.skip_ascii(length);
        if let Some(line_break) = self.scan_line_break() {
            self.allow_simple_key = true;
            if self.at_document_indicator() {
                return Spaces::End;
            }
            let mut breaks = String::new();
            loop {
                let spaces = self.spaces();
                self.skip_ascii(spaces);
                match self.scan_line_break() {
                    Some(c) => {
                        breaks.push(c);
                        if self.at_document_indicator() {
                            return Spaces::End;
                        }
                    }
                    None => break,
                }
            }
            let mut folded = String::new();
            if line_break != '\n' {
                folded.push(line_break);
            } else if breaks.is_empty() {
                folded.push(' ');
            }
            folded.push_str(&breaks);
            Spaces::Folded(folded)
        } else if length > 0 {
            Spaces::Blanks(whitespaces)
        } else {
            Spaces::End
        }
    }

    /// Whether `---` or `...` and a blank follow, wherever they are.
    fn at_document_indicator(&self) -> bool {
        let rest = &self.text[self.pos..];
        (rest.starts_with("---") || rest.starts_with("..."))
            && is_blankz(self.char_at(self.pos + 3))
    }

    fn scan_tag_handle(&mut self, name: &str, start: Mark) -> Scan<&'a str> {
        let ch = self.peek();
        if ch != '!' {
            return self.error(
                Some((&format!("while scanning a {name}"), start)),
                format!("expected '!', but found {}", repr(&ch.to_string())),
            );
        }
        let mut at = self.pos + 1;
        let mut ch = self.char_at(at);
        if ch != ' ' {
            while is_alnum(ch) {
                at += 1;
                ch = self.char_at(at);
            }
            if ch != '!' {
                self.forward(at - self.pos);
                return self.error(
                    Some((&format!("while scanning a {name}"), start)),
                    format!("expected '!', but found {}", repr(&ch.to_string())),
                );
            }
            at += 1;
        }
        let value = self.slice(at - self.pos);
        self.skip_ascii(at - self.pos);
        Ok(value)
    }

    fn scan_tag_uri(&mut self, name: &str, start: Mark) -> Scan<Cow<'a, str>> {
        let allowed = |c: char| c.is_ascii_alphanumeric() || "-;/?:@&=+$,_.!~*'()[]%".contains(c);
        let mut chunks = Chunks::Empty;
        let mut any = false;
        loop {
            let length = self.run(|c| allowed(c) && c != '%');
            if length > 0 {
                chunks.push(self.slice(length));
                self.skip(length);
                any = true;
            }
            if self.peek() == '%' {
                let decoded = self.scan_uri_escapes(name, start)?;
                chunks.push_owned(&decoded);
                any = true;
            } else {
                break;
            }
        }
        if !any {
            return self.error(
                Some((&format!("while parsing a {name}"), start)),
                format!("expected URI, but found {}", repr(&self.peek().to_string())),
            );
        }
        Ok(chunks.finish())
    }

    fn scan_uri_escapes(&mut self, name: &str, start: Mark) -> Scan<String> {
        let mut codes: Vec<u8> = Vec::new();
        let mark = self.mark();
        while self.peek() == '%' {
            self.forward(1);
            for k in 0..2 {
                if !is_hex(self.peek_n(k)) {
                    return self.error(
                        Some((&format!("while scanning a {name}"), start)),
                        format!(
                            "expected URI escape sequence of 2 hexadecimal numbers, but found {}",
                            repr(&self.peek_n(k).to_string())
                        ),
                    );
                }
            }
            codes.push(u8::from_str_radix(self.slice(2), 16).unwrap_or(0));
            self.forward(2);
        }
        String::from_utf8(codes).map_err(|error| {
            Failure::within(
                Stage::Scanner,
                format!("while scanning a {name}"),
                start,
                super::reader::utf8_message(error.as_bytes(), &error.utf8_error()),
                mark,
            )
        })
    }

    /// Takes a line break, as `\n` for CRLF, CR, LF and NEL, and as itself for LS and PS.
    fn scan_line_break(&mut self) -> Option<char> {
        let width = break_len(self.bytes, self.pos);
        if width == 0 {
            return None;
        }
        let first = self.bytes[self.pos];
        self.line += 1;
        self.column = 0;
        if first == b'\r' && self.bytes.get(self.pos + 1) == Some(&b'\n') {
            self.pos += 2;
            self.index += 2;
            return Some('\n');
        }
        self.pos += width;
        self.index += 1;
        Some(match width {
            3 => {
                if self.bytes[self.pos - 1] == 0xA8 {
                    '\u{2028}'
                } else {
                    '\u{2029}'
                }
            }
            _ => '\n',
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::yaml::Limits;

    fn tokens(text: &str) -> Vec<String> {
        let mut scanner = Scanner::new(text, Limits::DEFAULT.digits);
        let mut out = Vec::new();
        loop {
            let token = scanner.get_token().expect("a token");
            let done = matches!(token.tok, Tok::StreamEnd);
            out.push(match token.tok {
                Tok::Scalar { value, plain, .. } => {
                    format!("{}{value}", if plain { "=" } else { "\"" })
                }
                other => format!("{other:?}"),
            });
            if done {
                return out;
            }
        }
    }

    #[test]
    fn a_mapping_is_a_block_start_and_keys_with_values() {
        assert_eq!(
            tokens("a: 1\n"),
            [
                "StreamStart",
                "BlockMappingStart",
                "Key",
                "=a",
                "Value",
                "=1",
                "BlockEnd",
                "StreamEnd"
            ]
        );
    }

    #[test]
    fn a_flow_collection_may_continue_at_the_start_of_a_line() {
        assert_eq!(
            tokens("[1,\n2]"),
            [
                "StreamStart",
                "FlowSequenceStart",
                "=1",
                "FlowEntry",
                "=2",
                "FlowSequenceEnd",
                "StreamEnd"
            ]
        );
        assert_eq!(tokens("\"a\nb\"")[1], "\"a b");
    }

    #[test]
    fn nel_ls_and_ps_break_a_line_and_a_tab_starts_nothing() {
        let mut scanner = Scanner::new("a\u{2028}b", Limits::DEFAULT.digits);
        let first = scanner.get_token().unwrap();
        assert!(matches!(first.tok, Tok::StreamStart));
        assert_eq!(tokens("a\u{85}b")[1], "=a b");
        let mut scanner = Scanner::new("\ta", Limits::DEFAULT.digits);
        let refused = loop {
            match scanner.get_token() {
                Ok(_) => {}
                Err(failure) => break failure,
            }
        };
        assert_eq!(
            crate::yaml::plain(&refused.problem),
            "found character '\\t' that cannot start any token"
        );
    }

    #[test]
    fn a_scalar_that_is_a_stretch_of_the_text_borrows_it() {
        let mut scanner = Scanner::new("key: value", Limits::DEFAULT.digits);
        let mut borrowed = 0;
        while let Ok(token) = scanner.get_token() {
            if let Tok::Scalar { value, .. } = &token.tok {
                assert!(matches!(value, Cow::Borrowed(_)));
                borrowed += 1;
            }
            if matches!(token.tok, Tok::StreamEnd) {
                break;
            }
        }
        assert_eq!(borrowed, 2);
    }

    #[test]
    fn an_escaped_surrogate_pair_is_its_character_and_a_lone_half_is_marked() {
        let scalar = |text: &str| {
            let mut scanner = Scanner::new(text, Limits::DEFAULT.digits);
            loop {
                let token = scanner.get_token().expect("a token");
                if let Tok::Scalar { value, lone, .. } = token.tok {
                    return (value.into_owned(), lone);
                }
            }
        };
        assert_eq!(
            scalar("\"\\ud83d\\udce6\""),
            ("\u{1F4E6}".to_owned(), false)
        );
        assert!(scalar("\"\\ud83d\"").1);
        assert!(scalar("\"\\udce6\\ud83d\"").1);
        assert_eq!(
            scalar("\"a\\u00e9\\U0001F4E6\""),
            ("a\u{e9}\u{1F4E6}".to_owned(), false)
        );
    }
}
