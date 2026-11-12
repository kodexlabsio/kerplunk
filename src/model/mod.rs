//! The graph model as Python sees it: `konstruct._core.Graph`, `Node`, `Edge` and `Step`, the
//! functions that name things in a graph, and `graph.json` read ([`file`]) and written
//! ([`write`]).
//!
//! `konstruct.core.graph` re-exports them, and `konstruct.Graph`, `konstruct.Node` and
//! `konstruct.Edge` are its names; the module docstring there says what each kind of edge means.

mod encode;
pub(crate) mod file;
mod graph;
mod node;
mod write;

use pyo3::prelude::*;
use pyo3::types::PyString;

pub use graph::{Graph, RELATION_KINDS, priority};
pub use node::{Edge, Node, Step, install_dataclass_metadata};

/// An index into a list of at most 2^32 items.
pub(crate) fn ordinal(index: usize) -> u32 {
    u32::try_from(index).expect("fewer than 2^32 nodes and edges")
}

/// The id of the column `column` of the relation `parent`: `<parent>#<column in lower case>`.
pub(crate) fn column_key<'py>(
    parent: &Bound<'py, PyAny>,
    column: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let lower = column.call_method0(pyo3::intern!(column.py(), "lower"))?;
    parent.str()?.add("#")?.add(lower.str()?)
}

/// `FormatError` with `message`.
pub(crate) fn format_error(py: Python<'_>, message: &str) -> PyErr {
    match py
        .import("konstruct.core.formats")
        .and_then(|formats| formats.getattr("FormatError"))
        .and_then(|class| class.call1((message,)))
    {
        Ok(error) => PyErr::from_value(error),
        Err(error) => error,
    }
}

/// Checks that `data` is a graph document this Konstruct reads, called `source`.
pub(crate) fn check_format(py: Python<'_>, data: &Bound<'_, PyAny>, source: &str) -> PyResult<()> {
    py.import("konstruct.core.formats")?
        .getattr("check")?
        .call1((data, crate::load::GRAPH_FORMAT_VERSION, source))?;
    Ok(())
}

/// The id of the column `column` of the relation `parent_id`.
#[pyfunction]
pub(crate) fn column_id<'py>(
    parent_id: &Bound<'py, PyAny>,
    column: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    column_key(parent_id, column)
}

/// Canonical, case-insensitive key for a warehouse relation, e.g. `jaffle.main.orders`.
#[pyfunction]
#[pyo3(signature = (*parts))]
pub(crate) fn relation_key<'py>(
    py: Python<'py>,
    parts: Vec<Option<Bound<'py, PyAny>>>,
) -> PyResult<Bound<'py, PyAny>> {
    let mut kept = Vec::new();
    for part in parts.into_iter().flatten() {
        if !part.is_truthy()? {
            continue;
        }
        let cleaned = part
            .call_method1(pyo3::intern!(py, "strip"), ("\"`[]",))?
            .call_method0(pyo3::intern!(py, "lower"))?;
        if cleaned.is_truthy()? {
            kept.push(cleaned);
        }
    }
    PyString::new(py, ".").call_method1(pyo3::intern!(py, "join"), (kept,))
}

/// How canonical text names one reading of a relation that a query reads more than once: the
/// relation key with each `#` in it doubled, then `#` and the reading (`db.main.orders#2`). A
/// relation read once is its key, escaped the same way, so a relation named `events#2` is never
/// taken for a reading of `events`.
#[pyfunction]
#[pyo3(signature = (key, reading=""))]
pub(crate) fn reading_key(key: &str, reading: &str) -> String {
    let escaped = key.replace('#', "##");
    if reading.is_empty() {
        escaped
    } else {
        format!("{escaped}#{reading}")
    }
}

/// The relation key and the reading a name in canonical text stands for (see `reading_key`):
/// `db.main.orders#2` is reading `2` of `db.main.orders`, and `main.events##2` is the relation
/// `main.events#2`.
#[pyfunction]
pub(crate) fn split_reading(name: &str) -> (String, String) {
    split(name)
}

pub(crate) fn split(name: &str) -> (String, String) {
    if !name.contains('#') {
        return (name.to_owned(), String::new());
    }
    let chars: Vec<char> = name.chars().collect();
    let mut key = String::new();
    let mut index = 0;
    while index < chars.len() {
        let char = chars[index];
        if char == '#' {
            if chars.get(index + 1) != Some(&'#') {
                return (key, chars[index + 1..].iter().collect());
            }
            index += 1;
        }
        key.push(char);
        index += 1;
    }
    (key, String::new())
}
