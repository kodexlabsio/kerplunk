//! Formulas as a straight-line program: one definition per formula, each a tree of operations
//! over source columns and earlier definitions.
//!
//! A definition is written as a prefix expression. Each node is `(op args...)`, with an optional
//! `#n` after the operation naming the node's label (an index the caller keeps, to show the
//! node's SQL in a counterexample). Strings are double-quoted, with `\"`, `\\` and `\1` (the
//! byte 1) as escapes.
//!
//! | Node | Meaning |
//! |------|---------|
//! | `(col "name")` | a source column: a variable, which may be NULL |
//! | `(present x)` | x, known not to be NULL (a column with a `not_null` test) |
//! | `(outer "key" x)` | x read from the side of an outer join that may have no matching row: NULL also when it has none, a Boolean atom keyed by `key` |
//! | `(num "n" "d")` | the rational n/d (decimal integers, d positive) |
//! | `(lit "kind" "text")` | another constant (a string, a date), opaque; of kind `null`, NULL |
//! | `(ref k)` | the value of definition k, which must come earlier |
//! | `(add x...)`, `(mul x...)`, `(sub x y)`, `(neg x)`, `(div x y)`, `(pow x k)` | arithmetic |
//! | `(fn "name" x...)` | an uninterpreted function of ordered arguments |
//! | `(sym "name" x...)` | an uninterpreted function of its arguments in any order |
//! | `(case c1 v1 ... cn vn e)` | the first `v` whose condition is true, else `e` |
//! | `(coalesce x...)` | the first argument that is not NULL |
//! | `(sum x)`, `(avg x)`, `(count)`, `(countof x)` | aggregates read by linearity |
//! | `(agg "name" x...)` | an uninterpreted aggregate (MIN, MAX, COUNT DISTINCT, ...) |
//! | `(win "name" x...)` | an uninterpreted window function, over the rows of its context |
//!
//! Sharing is what makes the program straight-line: a formula built from others refers to their
//! definitions instead of copying them, so a column's whole lineage costs one evaluation per
//! column however often it is reused downstream.

use std::fmt;
use std::ops::Range;

use super::rational::Q;

/// The deepest nesting a definition may have. Callers flatten associative chains, so real
/// formulas stay far below it; the limit keeps evaluation's recursion bounded.
pub const MAX_NESTING: usize = 200;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Op {
    Column(Box<str>),
    Present,
    /// Its argument, NULL also when the row of an outer join's side it is read from is missing.
    Outer(Box<str>),
    /// The rational's numerator and denominator as decimal digits, which each lane reduces by
    /// its own prime, and the rational exactly when it fits in 128 bits.
    Number(Box<str>, Box<str>, Option<Q>),
    Literal(Box<str>, Box<str>),
    Ref(u32),
    Add,
    Sub,
    Neg,
    Mul,
    Div,
    Pow(u32),
    Function(Box<str>),
    Symmetric(Box<str>),
    Case,
    Coalesce,
    Sum,
    Avg,
    Count,
    CountOf,
    Aggregate(Box<str>),
    Window(Box<str>),
}

#[derive(Clone, Debug)]
pub struct Node {
    pub op: Op,
    children: Range<u32>,
    /// The definition the node belongs to.
    pub definition: u32,
    pub label: Option<u32>,
}

#[derive(Clone, Debug)]
pub struct Definition {
    pub root: u32,
    /// Index into the program's contexts: the conditions this definition's aggregates run under.
    pub context: u32,
}

/// Why a definition could not be read.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ParseError {
    pub at: usize,
    pub message: String,
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} at byte {}", self.message, self.at)
    }
}

impl std::error::Error for ParseError {}

/// Formulas as definitions, added one at a time.
#[derive(Clone, Debug, Default)]
pub struct Program {
    pub nodes: Vec<Node>,
    edges: Vec<u32>,
    pub definitions: Vec<Definition>,
    /// Distinct contexts, as given.
    pub contexts: Vec<Box<str>>,
}

impl Program {
    pub fn new() -> Program {
        Program::default()
    }

    pub fn children(&self, node: u32) -> &[u32] {
        let range = &self.nodes[node as usize].children;
        &self.edges[range.start as usize..range.end as usize]
    }

    /// Adds a definition, written as the module describes, whose aggregates run under
    /// `context`; returns its index. Nothing is added when `text` is malformed.
    pub fn add(&mut self, text: &str, context: &str) -> Result<u32, ParseError> {
        let definition = self.definitions.len() as u32;
        let (nodes, edges) = (self.nodes.len(), self.edges.len());
        let mut parser = Parser {
            text: text.as_bytes(),
            at: 0,
            program: self,
            definition,
        };
        let result = parser.node(0).and_then(|root| {
            parser.space();
            if parser.at == parser.text.len() {
                Ok(root)
            } else {
                Err(parser.error("text after the formula"))
            }
        });
        match result {
            Ok(root) => {
                let context = match self.contexts.iter().position(|c| &**c == context) {
                    Some(found) => found as u32,
                    None => {
                        self.contexts.push(context.into());
                        (self.contexts.len() - 1) as u32
                    }
                };
                self.definitions.push(Definition { root, context });
                Ok(definition)
            }
            Err(error) => {
                self.nodes.truncate(nodes);
                self.edges.truncate(edges);
                Err(error)
            }
        }
    }
}

struct Parser<'a> {
    text: &'a [u8],
    at: usize,
    program: &'a mut Program,
    definition: u32,
}

impl Parser<'_> {
    fn error(&self, message: &str) -> ParseError {
        ParseError {
            at: self.at,
            message: message.to_string(),
        }
    }

    fn space(&mut self) {
        while self.at < self.text.len() && self.text[self.at].is_ascii_whitespace() {
            self.at += 1;
        }
    }

    fn peek(&mut self) -> Option<u8> {
        self.space();
        self.text.get(self.at).copied()
    }

    fn expect(&mut self, byte: u8) -> Result<(), ParseError> {
        if self.peek() == Some(byte) {
            self.at += 1;
            Ok(())
        } else {
            Err(self.error(&format!("expected {:?}", byte as char)))
        }
    }

    /// A bare word: an operation, a label or an integer.
    fn word(&mut self) -> &str {
        self.space();
        let start = self.at;
        while self.at < self.text.len()
            && (self.text[self.at].is_ascii_alphanumeric() || b"#-_".contains(&self.text[self.at]))
        {
            self.at += 1;
        }
        // Only ASCII bytes were taken, so the slice is valid UTF-8.
        std::str::from_utf8(&self.text[start..self.at]).unwrap_or_default()
    }

    fn string(&mut self) -> Result<String, ParseError> {
        self.expect(b'"')?;
        let mut bytes = Vec::new();
        loop {
            match self.text.get(self.at) {
                None => return Err(self.error("unterminated string")),
                Some(b'"') => {
                    self.at += 1;
                    break;
                }
                Some(b'\\') => {
                    match self.text.get(self.at + 1) {
                        Some(&c @ (b'"' | b'\\')) => bytes.push(c),
                        Some(b'1') => bytes.push(1),
                        _ => return Err(self.error("unknown escape")),
                    }
                    self.at += 2;
                }
                Some(&c) => {
                    bytes.push(c);
                    self.at += 1;
                }
            }
        }
        String::from_utf8(bytes).map_err(|_| self.error("string is not UTF-8"))
    }

    fn integer(&mut self) -> Result<u32, ParseError> {
        let word = self.word().to_string();
        word.parse()
            .map_err(|_| self.error(&format!("expected an integer, found {word:?}")))
    }

    fn node(&mut self, depth: usize) -> Result<u32, ParseError> {
        if depth > MAX_NESTING {
            return Err(self.error("formula nested too deeply"));
        }
        self.expect(b'(')?;
        let head = self.word().to_string();
        let (name, label) = match head.split_once('#') {
            Some((name, label)) => (
                name.to_string(),
                Some(label.parse().map_err(|_| self.error("bad label"))?),
            ),
            None => (head, None),
        };
        let mut children = Vec::new();
        let op = match name.as_str() {
            "col" => Op::Column(self.string()?.into()),
            "num" => {
                let (n, d) = (self.string()?, self.string()?);
                number(&n, &d).ok_or_else(|| self.error("bad number"))?
            }
            "present" => Op::Present,
            "outer" => Op::Outer(self.string()?.into()),
            "coalesce" => Op::Coalesce,
            "win" => Op::Window(self.string()?.into()),
            "lit" => Op::Literal(self.string()?.into(), self.string()?.into()),
            "ref" => {
                let target = self.integer()?;
                if target >= self.definition {
                    return Err(self.error("a reference must be to an earlier definition"));
                }
                Op::Ref(target)
            }
            "pow" => {
                children.push(self.node(depth + 1)?);
                let exponent = self.integer()?;
                if exponent > 64 {
                    return Err(self.error("exponent above 64"));
                }
                Op::Pow(exponent)
            }
            "add" => Op::Add,
            "sub" => Op::Sub,
            "neg" => Op::Neg,
            "mul" => Op::Mul,
            "div" => Op::Div,
            "fn" => Op::Function(self.string()?.into()),
            "sym" => Op::Symmetric(self.string()?.into()),
            "agg" => Op::Aggregate(self.string()?.into()),
            "case" => Op::Case,
            "sum" => Op::Sum,
            "avg" => Op::Avg,
            "count" => Op::Count,
            "countof" => Op::CountOf,
            _ => return Err(self.error(&format!("unknown operation {name:?}"))),
        };
        if !matches!(op, Op::Pow(_)) {
            while self.peek() == Some(b'(') {
                children.push(self.node(depth + 1)?);
            }
        }
        self.expect(b')')?;
        let count = children.len();
        let arity_ok = match op {
            Op::Column(_) | Op::Number(..) | Op::Literal(..) | Op::Ref(_) | Op::Count => count == 0,
            Op::Neg | Op::Pow(_) | Op::Sum | Op::Avg | Op::CountOf | Op::Present | Op::Outer(_) => {
                count == 1
            }
            Op::Sub | Op::Div => count == 2,
            Op::Add | Op::Mul | Op::Coalesce => count >= 1,
            Op::Case => count >= 3 && count % 2 == 1,
            Op::Function(_) | Op::Symmetric(_) | Op::Aggregate(_) | Op::Window(_) => true,
        };
        if !arity_ok {
            return Err(self.error(&format!("wrong number of arguments for {name:?}")));
        }
        let start = self.program.edges.len() as u32;
        self.program.edges.extend(children);
        self.program.nodes.push(Node {
            op,
            children: start..self.program.edges.len() as u32,
            definition: self.definition,
            label,
        });
        Ok((self.program.nodes.len() - 1) as u32)
    }
}

/// The rational `n/d` from decimal digits, `n` optionally negative and `d` positive.
fn number(n: &str, d: &str) -> Option<Op> {
    let digits = n.strip_prefix('-').unwrap_or(n);
    let decimal = |text: &str| !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit());
    if !decimal(digits) || !decimal(d) || d.bytes().all(|b| b == b'0') {
        return None;
    }
    let exact = match (n.parse::<i128>(), d.parse::<i128>()) {
        (Ok(n), Ok(d)) => Q::new(n, d),
        _ => None,
    };
    Some(Op::Number(n.into(), d.into(), exact))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_every_operation() {
        let mut program = Program::new();
        let first = program
            .add(
                r#"(add (present (col "a")) (num "-3" "2") (lit "str" "x \"y\" \\ \1"))"#,
                "",
            )
            .unwrap();
        assert_eq!(first, 0);
        let text = r#"(case#4 (sym#1 "EQ" (col "a") (ref 0)) (sum (mul (col "a") (col "b")))
            (avg (div (col "a") (pow (col "b") 2))))"#;
        assert_eq!(program.add(text, "WHERE x").unwrap(), 1);
        let text = r#"(agg "MIN" (coalesce (col "a") (count) (countof (neg (col "b"))))
            (win "LAG" (outer "j" (col "a"))))"#;
        program.add(text, "WHERE x").unwrap();
        assert_eq!(program.contexts.len(), 2);
        assert_eq!(program.definitions[2].context, 1);
        let root = program.definitions[1].root;
        assert_eq!(program.nodes[root as usize].label, Some(4));
        assert_eq!(program.children(root).len(), 3);
        let literal = program.children(program.definitions[0].root)[2];
        assert!(matches!(
            program.nodes[literal as usize].op,
            Op::Literal(ref k, ref t) if &**k == "str" && &**t == "x \"y\" \\ \u{1}"
        ));
    }

    #[test]
    fn rejects_malformed_definitions_and_keeps_the_program_as_it_was() {
        let mut program = Program::new();
        program.add(r#"(col "a")"#, "").unwrap();
        let nodes = program.nodes.len();
        for bad in [
            r#"(col "a""#,
            r#"(ref 1)"#,
            r#"(sub (col "a"))"#,
            r#"(case (col "a") (col "b"))"#,
            r#"(num "1" "0")"#,
            r#"(num "x" "1")"#,
            r#"(wat)"#,
            r#"(col "a") (col "b")"#,
            r#"(col "a\q")"#,
            r#"(pow (col "a") 65)"#,
            r#"(add#x (col "a"))"#,
            r#"(present (col "a") (col "b"))"#,
            r#"(outer "k")"#,
            r#"(outer (col "a"))"#,
            r#"(coalesce)"#,
        ] {
            assert!(program.add(bad, "").is_err(), "{bad}");
            assert_eq!(program.nodes.len(), nodes);
        }
        assert_eq!(program.definitions.len(), 1);
        let deep = format!(
            "{}(col \"a\"){}",
            "(neg ".repeat(MAX_NESTING + 2),
            ")".repeat(MAX_NESTING + 2)
        );
        assert!(program.add(&deep, "").is_err());
        assert!(
            program
                .add("", "")
                .unwrap_err()
                .to_string()
                .contains("at byte 0")
        );
    }

    #[test]
    fn numbers_are_kept_exactly() {
        match number("-15", "10") {
            Some(Op::Number(n, d, Some(exact))) => {
                assert_eq!((&*n, &*d), ("-15", "10"));
                assert_eq!(exact, Q::new(-3, 2).unwrap());
            }
            other => panic!("{other:?}"),
        }
        // Too large for 128 bits: kept as digits, exact in the fields.
        let big = format!("1{}", "0".repeat(40));
        assert!(matches!(number(&big, "1"), Some(Op::Number(_, _, None))));
        assert_eq!(number("1", "000"), None);
        assert_eq!(number("--1", "1"), None);
        assert_eq!(number("1", "-2"), None);
        assert_eq!(number("", "1"), None);
    }
}
