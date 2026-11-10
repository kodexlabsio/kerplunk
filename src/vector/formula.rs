//! The structure of a formula, as canonical tokens.
//!
//! A column's expression (SQL over physical columns, as the build records it) is read as a
//! stream of tokens: the columns it reads, by the words of their names; functions, under one
//! name for each family (`nvl`, `ifnull` and `coalesce` are one); keywords, operators, numbers
//! and string literals. Runs of three tokens, with every column, number and string replaced by
//! a placeholder, make shapes: `$c * $n` is the same shape whichever column is scaled, so two
//! formulas that do the same thing to different columns share features.
//!
//! The reader is a tokeniser, not a parser. It accepts every dialect's quoting and never fails,
//! because a formula it half understands still says something about the column.

use super::text::{self, Feature, Space, feature};

/// How much each kind of formula token weighs.
const COLUMN: f32 = 1.0;
const COLUMN_CONCEPT: f32 = 0.8;
const FUNCTION: f32 = 0.9;
const KEYWORD: f32 = 0.45;
const OPERATOR: f32 = 0.6;
const NUMBER: f32 = 0.4;
const LITERAL: f32 = 0.5;
/// The weight of the formula's shapes together.
const SHAPES: f32 = 0.9;
/// In a question, how much the columns it names weigh against its operators and functions:
/// less, because what a question in SQL asks for is the computation.
const QUESTION_COLUMNS: f32 = 0.5;

const KEYWORDS: &[&str] = &[
    "and",
    "between",
    "case",
    "distinct",
    "else",
    "end",
    "filter",
    "in",
    "interval",
    "is",
    "like",
    "not",
    "null",
    "or",
    "over",
    "partition",
    "then",
    "when",
];

/// Functions that do the same thing under different names in different dialects.
const ALIASES: &[(&str, &str)] = &[
    ("ceiling", "ceil"),
    ("char_length", "length"),
    ("character_length", "length"),
    ("date_part", "extract"),
    ("datepart", "extract"),
    ("ifnull", "coalesce"),
    ("isnull", "coalesce"),
    ("len", "length"),
    ("nvl", "coalesce"),
    ("pow", "power"),
    ("substr", "substring"),
    ("to_char", "format"),
    ("try_cast", "cast"),
];

#[derive(Clone, Debug, PartialEq)]
pub enum Token {
    /// A column, by the segments of its qualified name.
    Column(Vec<String>),
    Function(String),
    Keyword(&'static str),
    Operator(&'static str),
    Number(String),
    Literal(String),
}

const OPERATORS: &[&str] = &[
    "->>", "->", "::", "||", "<=", ">=", "<>", "!=", "==", "+", "-", "*", "/", "%", "=", "<", ">",
    "(", ")", ",",
];

/// The tokens of a SQL expression.
pub fn tokens(sql: &str) -> Vec<Token> {
    let bytes = sql.as_bytes();
    let mut out = Vec::new();
    let mut at = 0;
    while at < bytes.len() {
        let c = bytes[at];
        if c.is_ascii_whitespace() {
            at += 1;
        } else if c == b'\'' {
            let (text, next) = quoted(sql, at, b'\'');
            out.push(Token::Literal(text));
            at = next;
        } else if c.is_ascii_digit()
            || (c == b'.' && bytes.get(at + 1).is_some_and(u8::is_ascii_digit))
        {
            let start = at;
            while at < bytes.len() && (bytes[at].is_ascii_digit() || bytes[at] == b'.') {
                at += 1;
            }
            let number = sql[start..at].trim_end_matches('.');
            let number = if number.contains('.') {
                number.trim_end_matches('0').trim_end_matches('.')
            } else {
                number
            };
            out.push(Token::Number(number.to_owned()));
        } else if is_name_start(sql, at) {
            let mut segments = Vec::new();
            loop {
                let (segment, next) = name(sql, at);
                segments.push(segment);
                at = next;
                if bytes.get(at) == Some(&b'.')
                    && at + 1 < bytes.len()
                    && is_name_start(sql, at + 1)
                {
                    at += 1;
                } else {
                    break;
                }
            }
            let mut ahead = at;
            while ahead < bytes.len() && bytes[ahead].is_ascii_whitespace() {
                ahead += 1;
            }
            let last = segments.last().map(String::as_str).unwrap_or_default();
            if bytes.get(ahead) == Some(&b'(') {
                let lower = last.to_lowercase();
                let canonical = ALIASES
                    .binary_search_by(|(alias, _)| alias.cmp(&lower.as_str()))
                    .map_or(lower.clone(), |i| ALIASES[i].1.to_owned());
                out.push(Token::Function(canonical));
            } else if let Some(keyword) = (segments.len() == 1)
                .then(|| KEYWORDS.iter().find(|k| k.eq_ignore_ascii_case(last)))
                .flatten()
            {
                out.push(Token::Keyword(keyword));
            } else if segments.len() == 1 && last.eq_ignore_ascii_case("as") {
                // An alias's name follows; it names the result, not an input.
            } else {
                out.push(Token::Column(segments));
            }
        } else if let Some(op) = OPERATORS.iter().find(|op| sql[at..].starts_with(*op)) {
            out.push(Token::Operator(op));
            at += op.len();
        } else {
            at += sql[at..].chars().next().map_or(1, char::len_utf8);
        }
    }
    out
}

fn is_name_start(sql: &str, at: usize) -> bool {
    match sql[at..].chars().next() {
        Some('"' | '`' | '[') => true,
        Some(c) => c.is_alphabetic() || c == '_',
        None => false,
    }
}

/// A name, bare or quoted, and where it ends.
fn name(sql: &str, at: usize) -> (String, usize) {
    match sql.as_bytes()[at] {
        b'"' => quoted(sql, at, b'"'),
        b'`' => quoted(sql, at, b'`'),
        b'[' => quoted(sql, at, b']'),
        _ => {
            let end = sql[at..]
                .char_indices()
                .find(|(_, c)| !(c.is_alphanumeric() || *c == '_' || *c == '$'))
                .map_or(sql.len(), |(i, _)| at + i);
            (sql[at..end].to_owned(), end)
        }
    }
}

/// The text between a quote at `at` and its closing `close`, with doubled quotes undone.
fn quoted(sql: &str, at: usize, close: u8) -> (String, usize) {
    let bytes = sql.as_bytes();
    let mut text = Vec::new();
    let mut i = at + 1;
    while i < bytes.len() {
        if bytes[i] == close {
            if bytes.get(i + 1) == Some(&close) && close != b']' {
                text.push(close);
                i += 2;
                continue;
            }
            return (String::from_utf8_lossy(&text).into_owned(), i + 1);
        }
        text.push(bytes[i]);
        i += 1;
    }
    (String::from_utf8_lossy(&text).into_owned(), bytes.len())
}

/// Whether a question reads as a formula: it calls a function (a name straight before `(`) or
/// uses an arithmetic or comparison operator. A hyphen counts only beside a space, so
/// `year-to-date` stays words.
pub fn is_formula(text: &str) -> bool {
    let chars: Vec<char> = text.chars().collect();
    chars.iter().enumerate().any(|(at, &c)| {
        let before = at.checked_sub(1).map(|i| chars[i]);
        let after = chars.get(at + 1).copied();
        match c {
            '*' | '/' | '%' | '=' | '<' | '>' | '+' | '|' => true,
            '-' => {
                before.is_some_and(char::is_whitespace) || after.is_some_and(char::is_whitespace)
            }
            '(' => before.is_some_and(|b| b.is_alphanumeric() || b == '_'),
            _ => false,
        }
    })
}

/// The features of a formula, weighed by `weight`. With `question`, the text is a person's
/// question rather than SQL: keywords that are also ordinary words (`in`, `and`) are dropped.
pub fn features(sql: &str, weight: f32, question: bool, out: &mut Vec<Feature>) {
    let tokens = tokens(sql);
    let mut shape: Vec<String> = Vec::with_capacity(tokens.len());
    for token in &tokens {
        match token {
            Token::Column(segments) => {
                let name = segments.last().map(String::as_str).unwrap_or_default();
                let weight = if question {
                    QUESTION_COLUMNS * weight
                } else {
                    weight
                };
                for word in text::terms(name) {
                    out.push((feature(Space::Column, &[&word]), COLUMN * weight));
                    if let Some(concept) = text::concept(&word) {
                        out.push((
                            feature(Space::ColumnConcept, &[concept]),
                            COLUMN_CONCEPT * weight,
                        ));
                    }
                }
                shape.push("$c".into());
            }
            Token::Function(name) => {
                out.push((feature(Space::Function, &[name]), FUNCTION * weight));
                shape.push(name.clone());
            }
            Token::Keyword(keyword) => {
                if !question {
                    out.push((feature(Space::Keyword, &[keyword]), KEYWORD * weight));
                    shape.push((*keyword).into());
                }
            }
            Token::Operator(op) => {
                if !matches!(*op, "(" | ")" | ",") {
                    out.push((feature(Space::Operator, &[op]), OPERATOR * weight));
                }
                shape.push((*op).into());
            }
            Token::Number(number) => {
                out.push((feature(Space::Number, &[number]), NUMBER * weight));
                shape.push("$n".into());
            }
            Token::Literal(literal) => {
                for word in text::terms(literal) {
                    out.push((feature(Space::Literal, &[&word]), LITERAL * weight));
                }
                shape.push("$s".into());
            }
        }
    }
    // A question's words are columns side by side, whose shapes say nothing.
    let shapes: Vec<&[String]> = shape
        .windows(3)
        .filter(|w| !(question && w.iter().all(|t| t == "$c")))
        .collect();
    if !shapes.is_empty() {
        let each = SHAPES * weight / (shapes.len() as f32).sqrt();
        for window in shapes {
            out.push((
                feature(Space::Shape, &[&window[0], &window[1], &window[2]]),
                each,
            ));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tables_are_sorted() {
        assert!(KEYWORDS.windows(2).all(|pair| pair[0] < pair[1]));
        assert!(ALIASES.windows(2).all(|pair| pair[0].0 < pair[1].0));
    }

    #[test]
    fn expressions_become_canonical_tokens() {
        let found = tokens(
            r#"CASE WHEN "shop"."main"."orders".status = 'it''s' THEN NVL(o.amount, 0) / 100.50 ELSE -1 END"#,
        );
        assert_eq!(
            found,
            [
                Token::Keyword("case"),
                Token::Keyword("when"),
                Token::Column(vec![
                    "shop".into(),
                    "main".into(),
                    "orders".into(),
                    "status".into()
                ]),
                Token::Operator("="),
                Token::Literal("it's".into()),
                Token::Keyword("then"),
                Token::Function("coalesce".into()),
                Token::Operator("("),
                Token::Column(vec!["o".into(), "amount".into()]),
                Token::Operator(","),
                Token::Number("0".into()),
                Token::Operator(")"),
                Token::Operator("/"),
                Token::Number("100.5".into()),
                Token::Keyword("else"),
                Token::Operator("-"),
                Token::Number("1".into()),
                Token::Keyword("end"),
            ]
        );
        assert_eq!(
            tokens("sum(x) as total_x, [a b].`c` :: int"),
            [
                Token::Function("sum".into()),
                Token::Operator("("),
                Token::Column(vec!["x".into()]),
                Token::Operator(")"),
                Token::Column(vec!["total_x".into()]),
                Token::Operator(","),
                Token::Column(vec!["a b".into(), "c".into()]),
                Token::Operator("::"),
                Token::Column(vec!["int".into()]),
            ]
        );
        // Unterminated quotes and stray characters are read as far as they go.
        assert_eq!(tokens("'abc"), [Token::Literal("abc".into())]);
        assert_eq!(tokens("§ ~ 1."), [Token::Number("1".into())]);
    }

    #[test]
    fn the_same_shape_over_other_columns_shares_features() {
        let shapes = |sql: &str| {
            let mut out = Vec::new();
            features(sql, 1.0, false, &mut out);
            out.into_iter()
                .filter(|(h, _)| {
                    *h == feature(Space::Shape, &["$c", "*", "$n"])
                        || *h == feature(Space::Operator, &["*"])
                })
                .count()
        };
        assert_eq!(shapes("a.price * 2"), 2);
        assert_eq!(shapes("b.quantity * 3"), 2);
        assert_eq!(shapes("a.price + b.quantity"), 0);
        assert!(is_formula("amount / 100") && is_formula("coalesce(a, b)"));
        assert!(is_formula("price - cost") && is_formula("a.x*2"));
        assert!(!is_formula("refund amount in pounds") && !is_formula("orders (daily)"));
        assert!(!is_formula("year-to-date revenue"));
        let mut question = Vec::new();
        features("revenue in pounds", 1.0, true, &mut question);
        assert!(
            question
                .iter()
                .all(|(h, _)| *h != feature(Space::Keyword, &["in"]))
        );
        assert!(question.contains(&(
            feature(Space::ColumnConcept, &["pound"]),
            COLUMN_CONCEPT * QUESTION_COLUMNS
        )));
    }
}
