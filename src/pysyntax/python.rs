//! What the Python classes of the readers share: errors as Python raises them, and lists and
//! dicts that stay Python objects, so that code which edits a record's list sees its edit.

use pyo3::exceptions::{PySyntaxError, PyUnicodeDecodeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PyList, PyModule, PyTuple, PyType};

use super::Failure;

/// A file's failure as the `SyntaxError` Python's `ast.parse` raises for it.
pub fn syntax_error(py: Python<'_>, path: &str, failure: Failure) -> PyErr {
    let details = (path.to_owned(), failure.line, 1, None::<String>);
    if failure.indentation {
        // pyo3 has no type for IndentationError, a subclass of SyntaxError.
        let made = py
            .import("builtins")
            .and_then(|builtins| builtins.getattr("IndentationError"))
            .and_then(|class| class.call1((failure.message, details)));
        return match made {
            Ok(error) => PyErr::from_value(error),
            Err(failed) => failed,
        };
    }
    PySyntaxError::new_err((failure.message, details))
}

/// What reading `path` raised: a `SyntaxError`, an `OSError` or a `UnicodeDecodeError`.
pub fn read_error(py: Python<'_>, path: &str, error: crate::airflow::Error) -> PyErr {
    use crate::airflow::Error;
    match error {
        Error::Syntax(failure) => syntax_error(py, path, failure),
        Error::Io(error) => error.into(),
        Error::NotUtf8 { bytes, error } => {
            match PyUnicodeDecodeError::new_utf8(py, &bytes, error) {
                Ok(raised) => PyErr::from_value(raised.into_any()),
                Err(failed) => failed,
            }
        }
    }
}

/// The list given, or a new empty one.
pub fn list_or_new<'py>(py: Python<'py>, given: Option<Bound<'py, PyList>>) -> Bound<'py, PyList> {
    given.unwrap_or_else(|| PyList::empty(py))
}

/// The dict given, or a new empty one.
pub fn dict_or_new<'py>(py: Python<'py>, given: Option<Bound<'py, PyDict>>) -> Bound<'py, PyDict> {
    given.unwrap_or_else(|| PyDict::new(py))
}

/// A list of strings.
pub fn strings<'py>(py: Python<'py>, items: &[String]) -> PyResult<Bound<'py, PyList>> {
    PyList::new(py, items)
}

/// A list of pairs of strings, as the tuples a dataclass's list of pairs holds.
pub fn pairs<'py>(py: Python<'py>, items: &[(String, String)]) -> PyResult<Bound<'py, PyList>> {
    let tuples = items
        .iter()
        .map(|(a, b)| PyTuple::new(py, [a, b]))
        .collect::<PyResult<Vec<_>>>()?;
    PyList::new(py, tuples)
}

/// `Name(field=value, ...)`, the way a dataclass prints itself.
pub fn repr_of(object: &Bound<'_, PyAny>, name: &str, fields: &[&str]) -> PyResult<String> {
    let mut parts = Vec::with_capacity(fields.len());
    for field in fields {
        parts.push(format!("{field}={}", object.getattr(*field)?.repr()?));
    }
    Ok(format!("{name}({})", parts.join(", ")))
}

/// Whether `other` has each of `fields` equal to `this`'s, and is of `this`'s class.
pub fn same_fields(
    this: &Bound<'_, PyAny>,
    other: &Bound<'_, PyAny>,
    fields: &[&str],
) -> PyResult<bool> {
    if !this.get_type().is(other.get_type()) {
        return Ok(false);
    }
    for field in fields {
        if !this.getattr(*field)?.eq(other.getattr(*field)?)? {
            return Ok(false);
        }
    }
    Ok(true)
}

/// The fields of a record, in the order its constructor takes them.
fn values<'py>(object: &Bound<'py, PyAny>, fields: &[&str]) -> PyResult<Bound<'py, PyTuple>> {
    let found = fields
        .iter()
        .map(|f| object.getattr(*f))
        .collect::<PyResult<Vec<_>>>()?;
    PyTuple::new(object.py(), found)
}

/// `copy.copy` of a record: another with the same fields, lists included.
pub fn shallow_copy<'py>(
    object: &Bound<'py, PyAny>,
    fields: &[&str],
) -> PyResult<Bound<'py, PyAny>> {
    object.get_type().call1(values(object, fields)?)
}

/// `copy.deepcopy` of a record: another with copies of its fields.
pub fn deep_copy<'py>(
    object: &Bound<'py, PyAny>,
    fields: &[&str],
    memo: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let copy = object.py().import("copy")?;
    let copied = copy.call_method1("deepcopy", (values(object, fields)?, memo))?;
    object.get_type().call1(copied.cast_into::<PyTuple>()?)
}

/// What `pickle` needs of a record: its class and the fields its constructor takes.
pub fn reduce<'py>(
    object: &Bound<'py, PyAny>,
    fields: &[&str],
) -> PyResult<(Bound<'py, PyType>, Bound<'py, PyTuple>)> {
    Ok((object.get_type(), values(object, fields)?))
}

/// Adds a module-level frozenset of names.
pub fn add_names(module: &Bound<'_, PyModule>, name: &str, names: &[&str]) -> PyResult<()> {
    module.add(name, PyFrozenSet::new(module.py(), names.iter().copied())?)
}

/// Adds a module-level tuple of names.
pub fn add_tuple(module: &Bound<'_, PyModule>, name: &str, names: &[&str]) -> PyResult<()> {
    module.add(name, PyTuple::new(module.py(), names.iter().copied())?)
}
