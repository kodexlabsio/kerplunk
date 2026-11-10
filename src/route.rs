//! Topic routing as Kafka Connect's `RegexRouter` and Debezium's `ByLogicalTableRouter` do it:
//! a topic that the whole expression matches is replaced, with Java's `$1` and `${name}` group
//! references, and any other topic is left as it is.
//!
//! The expressions come from connector configuration in a repository, which anyone can write,
//! so they run on the `regex` crate's finite automata: the time is linear in the topic, whatever
//! the expression. The crate has no backreferences or lookaround, so an expression that uses them
//! is refused rather than run on a backtracking matcher.
//!
//! As in Java, where `\w`, `\d`, `\s` and `(?i)` are ASCII unless a flag says otherwise, the
//! expressions match bytes without Unicode classes, which also keeps compiling one cheap: a
//! Unicode class takes milliseconds to compile, and any file can hold thousands.

use regex::bytes::{Regex, RegexBuilder};

/// The largest compiled expression accepted, in bytes; a bounded repeat such as `a{0,249}`
/// compiles to one state per repetition, and compiling stops once the limit is passed.
const SIZE_LIMIT: usize = 64 << 10;

/// One piece of a replacement.
#[derive(Debug, PartialEq, Eq)]
enum Piece {
    Text(String),
    Group(usize),
    Named(String),
}

/// A compiled routing transformation.
#[derive(Debug)]
pub struct Router {
    regex: Regex,
    replacement: Vec<Piece>,
}

impl Router {
    /// The transformation for `pattern` and `replacement`, or why it cannot be run, as a
    /// clause (`it uses a backreference, which Konstruct does not run`): an expression the
    /// crate cannot run as Java would (a backreference, lookaround, a possessive repeat), one
    /// that is not valid or too large, or a replacement that is not Java's syntax.
    pub fn new(pattern: &str, replacement: &str) -> Result<Self, String> {
        use regex_syntax::ast::ErrorKind;
        let ast = regex_syntax::ast::parse::Parser::new()
            .parse(pattern)
            .map_err(|error| match error.kind() {
                ErrorKind::UnsupportedBackreference => {
                    "it uses a backreference, which Konstruct does not run".to_owned()
                }
                ErrorKind::UnsupportedLookAround => {
                    "it uses lookaround, which Konstruct does not run".to_owned()
                }
                kind => format!("its expression is not valid: {kind}"),
            })?;
        if possessive(&ast) {
            return Err("it uses a possessive repeat, which Konstruct does not run".to_owned());
        }
        let regex = RegexBuilder::new(&format!(r"\A(?:{pattern})\z"))
            .unicode(false)
            .size_limit(SIZE_LIMIT)
            .build()
            .map_err(|error| match error {
                regex::Error::CompiledTooBig(_) => "its expression is too large".to_owned(),
                regex::Error::Syntax(text) => format!(
                    "its expression is not valid: {}",
                    text.lines()
                        .last()
                        .unwrap_or("")
                        .trim_start_matches("error: ")
                ),
                _ => "its expression is not valid".to_owned(),
            })?;
        let replacement = pieces(replacement, &regex)?;
        Ok(Self { regex, replacement })
    }

    /// The topic after routing: replaced when the whole expression matches it, else as it was;
    /// None when the replacement splits a character, as an expression on bytes can.
    pub fn route(&self, topic: &str) -> Option<String> {
        let Some(groups) = self.regex.captures(topic.as_bytes()) else {
            return Some(topic.to_owned());
        };
        let mut routed = Vec::new();
        for piece in &self.replacement {
            match piece {
                Piece::Text(text) => routed.extend_from_slice(text.as_bytes()),
                Piece::Group(number) => {
                    routed.extend_from_slice(groups.get(*number).map_or(&[][..], |m| m.as_bytes()));
                }
                Piece::Named(name) => {
                    routed.extend_from_slice(groups.name(name).map_or(&[][..], |m| m.as_bytes()));
                }
            }
        }
        String::from_utf8(routed).ok()
    }
}

/// Whether an expression repeats a repeat directly (`a*+`, `a++`, `a{2}+`): a possessive repeat
/// in Java, which matches less than the repeat of a repeat the `regex` crate reads it as.
fn possessive(ast: &regex_syntax::ast::Ast) -> bool {
    use regex_syntax::ast::Ast;
    match ast {
        Ast::Repetition(repetition) => {
            matches!(*repetition.ast, Ast::Repetition(_)) || possessive(&repetition.ast)
        }
        Ast::Group(group) => possessive(&group.ast),
        Ast::Alternation(alternation) => alternation.asts.iter().any(possessive),
        Ast::Concat(concat) => concat.asts.iter().any(possessive),
        _ => false,
    }
}

/// A replacement in Java's syntax (`java.util.regex.Matcher.appendReplacement`): `\` takes the
/// next character literally, `${name}` is a named group, and `$` with digits a numbered group,
/// taking each further digit while the number is still a group of the expression.
fn pieces(replacement: &str, regex: &Regex) -> Result<Vec<Piece>, String> {
    let groups = regex.captures_len() - 1;
    let mut out = Vec::new();
    let mut text = String::new();
    let mut chars = replacement.chars().peekable();
    while let Some(char) = chars.next() {
        match char {
            '\\' => text.push(
                chars
                    .next()
                    .ok_or("its replacement ends with a backslash")?,
            ),
            '$' => {
                if !text.is_empty() {
                    out.push(Piece::Text(std::mem::take(&mut text)));
                }
                if chars.next_if_eq(&'{').is_some() {
                    let name: String = chars.by_ref().take_while(|&c| c != '}').collect();
                    if regex.capture_names().flatten().all(|n| n != name) {
                        return Err(format!("its replacement names no group {name}"));
                    }
                    out.push(Piece::Named(name));
                    continue;
                }
                let first = chars
                    .next()
                    .and_then(|c| c.to_digit(10))
                    .ok_or("its replacement has a $ that names no group")?;
                let mut number = first as usize;
                if number > groups {
                    return Err(format!(
                        "its replacement names group {number}, which the expression lacks"
                    ));
                }
                while let Some(digit) = chars.peek().and_then(|c| c.to_digit(10)) {
                    let longer = number * 10 + digit as usize;
                    if longer > groups {
                        break;
                    }
                    number = longer;
                    chars.next();
                }
                out.push(Piece::Group(number));
            }
            other => text.push(other),
        }
    }
    if !text.is_empty() {
        out.push(Piece::Text(text));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn route(pattern: &str, replacement: &str, topic: &str) -> String {
        Router::new(pattern, replacement)
            .unwrap()
            .route(topic)
            .unwrap()
    }

    #[test]
    fn a_whole_match_is_replaced_and_anything_else_kept() {
        assert_eq!(
            route(r"app\.public\.(.*)", "$1", "app.public.orders"),
            "orders"
        );
        assert_eq!(
            route(r"app\.public\.(.*)", "$1", "app.sales.orders"),
            "app.sales.orders"
        );
        // The whole topic must match, as Java's Matcher.matches() requires.
        assert_eq!(
            route("public", "x", "app.public.orders"),
            "app.public.orders"
        );
        assert_eq!(route("a|ab", "x", "ab"), "x");
    }

    #[test]
    fn java_replacement_syntax() {
        assert_eq!(route(r"(\w+)\.(\w+)", "$2_$1", "a.b"), "b_a");
        assert_eq!(
            route(r"(?<schema>\w+)\.(\w+)", "${schema}-$2", "a.b"),
            "a-b"
        );
        assert_eq!(route(r"(\w+)", r"\$1-$1", "a"), "$1-a");
        // $12 is group 1 then a literal 2 when there is no group 12.
        assert_eq!(route(r"(\w+)", "$12", "a"), "a2");
        // A group that takes no part in the match is empty.
        assert_eq!(route(r"(x)?(\w+)", "[$1]$2", "a"), "[]a");
    }

    #[test]
    fn what_cannot_be_run_is_refused() {
        for pattern in [r"(a)\1", "a(?=b)", "(?<=a)b", "a*+"] {
            let error = Router::new(pattern, "x").unwrap_err();
            assert!(
                error.ends_with("which Konstruct does not run"),
                "{pattern}: {error}"
            );
        }
        assert_eq!(
            Router::new(&"a{0,249}".repeat(64), "x").unwrap_err(),
            "its expression is too large"
        );
        assert!(Router::new("(a)", "$2").is_err());
        assert!(Router::new("(a)", "$x").is_err());
        assert!(Router::new("(a)", "x\\").is_err());
        assert!(Router::new("(a)", "${nope}").is_err());
    }

    #[test]
    fn classes_and_case_are_ascii_as_in_java() {
        assert_eq!(route(r"\w+", "x", "orders"), "x");
        assert_eq!(route(r"\w+", "x", "caf\u{e9}"), "caf\u{e9}");
        assert_eq!(route("(?i)orders", "x", "ORDERS"), "x");
        assert!(Router::new(r"\pL+", "x").is_err());
        // A group that ends inside a character gives no topic.
        assert_eq!(
            Router::new("(.)(.*)", "$1").unwrap().route("\u{e9}t\u{e9}"),
            None
        );
    }

    #[test]
    fn compiling_is_cheap() {
        // With Unicode classes each of these took about 30 ms to compile.
        let started = std::time::Instant::now();
        for i in 0..100 {
            let pattern = format!(r"[a-z]{{0,40}}\w{{0,40}}(\d|\s){{1,20}}x{i}");
            Router::new(&pattern, "y").unwrap();
        }
        assert!(started.elapsed().as_secs_f64() < 2.0);
        assert!(Router::new(&"[a-z]{0,249}".repeat(3), "y").is_err());
        assert!(Router::new("([a-z]{1,40}){1,40}b", "y").is_err());
    }

    #[test]
    fn expressions_that_backtrack_run_in_linear_time() {
        let topic = format!("{}-", "a".repeat(10_000));
        for pattern in ["(a+)+$", "(a|aa)+", ".*.*.*.*b", "(?x)(a+) +b"] {
            let started = std::time::Instant::now();
            assert_eq!(route(pattern, "x", &topic), topic);
            assert!(started.elapsed().as_secs_f64() < 1.0, "{pattern}");
        }
    }
}
