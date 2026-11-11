//! What the readers ask of a node: the name a call or an attribute ends in, a docstring, a
//! literal's value and the line a node starts on, each as Python's `ast` shows it.

use ruff_python_ast::{Expr, Number, Stmt, StmtFunctionDef};
use ruff_text_size::{Ranged, TextSize};

use super::Source;
use super::text;
use super::value::Value;

/// The name a call, a name or an attribute ends in: `_call_name` of the readers. A call is seen
/// through to what it calls.
pub fn call_name(node: &Expr) -> Option<&str> {
    let node = match node {
        Expr::Call(call) => &*call.func,
        other => other,
    };
    match node {
        Expr::Name(name) => Some(name.id.as_str()),
        Expr::Attribute(attribute) => Some(attribute.attr.as_str()),
        _ => None,
    }
}

/// The dotted name of a name or a chain of attributes (`airflow.decorators.dag`), or `None` for
/// anything else.
pub fn dotted(node: &Expr) -> Option<String> {
    match node {
        Expr::Name(name) => Some(name.id.to_string()),
        Expr::Attribute(attribute) => match dotted(&attribute.value) {
            Some(base) if !base.is_empty() => Some(format!("{base}.{}", attribute.attr.as_str())),
            _ => Some(attribute.attr.to_string()),
        },
        _ => None,
    }
}

/// The docstring of a body, cleaned as `ast.get_docstring` cleans it, or `None` when the first
/// statement is not a string.
pub fn docstring(body: &[Stmt]) -> Option<String> {
    let Some(Stmt::Expr(first)) = body.first() else {
        return None;
    };
    let Expr::StringLiteral(literal) = &*first.value else {
        return None;
    };
    Some(text::cleandoc(&literal.value.to_string()))
}

/// The value of a literal: a string, a number, a boolean, `None` or `...`.
pub fn literal(node: &Expr) -> Option<Value> {
    Some(match node {
        Expr::StringLiteral(s) => Value::Str(s.value.to_string()),
        Expr::BytesLiteral(b) => Value::Bytes(
            b.value
                .as_slice()
                .iter()
                .flat_map(|part| part.iter().copied())
                .collect(),
        ),
        Expr::NumberLiteral(n) => match &n.value {
            Number::Int(i) => Value::Int(i.to_string()),
            Number::Float(f) => Value::Float(*f),
            Number::Complex { imag, .. } => Value::Imaginary(*imag),
        },
        Expr::BooleanLiteral(b) => Value::Bool(b.value),
        Expr::NoneLiteral(_) => Value::None,
        Expr::EllipsisLiteral(_) => Value::Ellipsis,
        _ => return None,
    })
}

/// The line a function's `def` (or `async`) is on: Python's `lineno` of the definition, where
/// Ruff's range starts at its first decorator.
pub fn definition_line(source: &Source, function: &StmtFunctionDef) -> usize {
    let Some(last) = function.decorator_list.last() else {
        return source.line(function.range.start());
    };
    let text = source.text().as_bytes();
    let mut at = usize::from(last.range.end());
    while let Some(&byte) = text.get(at) {
        match byte {
            b' ' | b'\t' | b'\n' | b'\x0c' => at += 1,
            b'\\' => at += 1,
            b'#' => {
                while text.get(at).is_some_and(|&b| b != b'\n') {
                    at += 1;
                }
            }
            _ => break,
        }
    }
    source.line(TextSize::try_from(at).unwrap_or_default())
}

/// The line `node` starts on.
pub fn line(source: &Source, node: &impl Ranged) -> usize {
    source.line(node.start())
}

#[cfg(test)]
mod tests {
    use super::*;
    use ruff_python_parser::parse_module;

    fn first_function(source: &str) -> usize {
        let source = Source::new(source);
        let parsed = parse_module(source.text()).unwrap();
        let Stmt::FunctionDef(function) = &parsed.syntax().body[0] else {
            panic!("not a function");
        };
        definition_line(&source, function)
    }

    #[test]
    fn a_definition_starts_at_its_def_not_its_decorators() {
        assert_eq!(first_function("def f():\n    pass\n"), 1);
        assert_eq!(
            first_function("@a\n@b(\n  1,\n)\n# note\n\nasync def f():\n    pass\n"),
            7
        );
    }

    #[test]
    fn names_are_read_through_calls_and_attributes() {
        let parsed = parse_module("a.b.c(1)\nd\n").unwrap();
        let body = &parsed.syntax().body;
        let Stmt::Expr(first) = &body[0] else {
            panic!()
        };
        assert_eq!(call_name(&first.value), Some("c"));
        assert_eq!(dotted(&first.value), None);
        let Expr::Call(call) = &*first.value else {
            panic!()
        };
        assert_eq!(dotted(&call.func).as_deref(), Some("a.b.c"));
    }
}
