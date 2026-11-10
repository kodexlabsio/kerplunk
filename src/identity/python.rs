//! The identity test as `konstruct._core.FormulaSet`; `konstruct.analysis.identity` is its
//! caller.

use pyo3::exceptions::{PyIndexError, PyValueError};
use pyo3::prelude::*;

use super::witness::{Input, Kind, Mention, Value};
use super::{Formulas, Witness};

/// An input of a witness: its name, then the definition and label of the expression it is, or
/// None for a column, then its value (None for NULL).
type InputTuple = (String, Option<u32>, Option<u32>, Option<String>);
/// A function's name, and the definition and label of where it is applied.
type MentionTuple = (String, u32, Option<u32>);
/// The kind, the inputs, the rows (None when the formulas aggregate no rows), the two results,
/// the functions, and the expressions only in each formula.
type WitnessTuple = (
    &'static str,
    Vec<InputTuple>,
    Option<Vec<Vec<InputTuple>>>,
    Option<(Option<String>, Option<String>)>,
    Vec<String>,
    Vec<MentionTuple>,
    Vec<MentionTuple>,
);

fn mention(m: &Mention) -> MentionTuple {
    (m.name.clone(), m.definition, m.label)
}

fn text(value: &Value) -> Option<String> {
    value.map(|q| q.to_string())
}

fn inputs(found: &[(Input, Value)]) -> Vec<InputTuple> {
    found
        .iter()
        .map(|(input, value)| match input {
            Input::Column(name) => (name.clone(), None, None, text(value)),
            Input::Expression(m) => (m.name.clone(), Some(m.definition), m.label, text(value)),
        })
        .collect()
}

fn witness(w: &Witness) -> WitnessTuple {
    (
        match w.kind {
            Kind::Values => "values",
            Kind::Functions => "functions",
            Kind::Conditions => "conditions",
        },
        inputs(&w.inputs),
        w.aggregates
            .then(|| w.rows.iter().map(|row| inputs(row)).collect()),
        w.results.map(|(a, b)| (text(&a), text(&b))),
        w.functions.clone(),
        w.only_a.iter().map(mention).collect(),
        w.only_b.iter().map(mention).collect(),
    )
}

/// Formulas to fingerprint and compare, added one at a time.
#[pyclass(module = "konstruct._core")]
pub struct FormulaSet {
    inner: Formulas,
}

impl FormulaSet {
    fn check(&self, definition: u32) -> PyResult<()> {
        if (definition as usize) < self.inner.len() {
            Ok(())
        } else {
            Err(PyIndexError::new_err(format!("no definition {definition}")))
        }
    }
}

#[pymethods]
impl FormulaSet {
    /// Formulas under `key`, or under the fixed key fingerprints use when it is None.
    #[new]
    #[pyo3(signature = (key=None))]
    fn new(key: Option<&[u8]>) -> Self {
        Self {
            inner: key.map_or_else(Formulas::new, Formulas::with_key),
        }
    }

    fn __len__(&self) -> usize {
        self.inner.len()
    }

    /// Adds a definition whose aggregates run under `context`; returns its index.
    fn add(&mut self, text: &str, context: &str) -> PyResult<u32> {
        self.inner
            .add(text, context)
            .map_err(|e| PyValueError::new_err(e.to_string()))
    }

    /// The 128-bit fingerprints of `definitions`.
    fn fingerprints(&mut self, py: Python<'_>, definitions: Vec<u32>) -> PyResult<Vec<u128>> {
        for &d in &definitions {
            self.check(d)?;
        }
        let inner = &mut self.inner;
        Ok(py.detach(|| definitions.iter().map(|&d| inner.fingerprint(d)).collect()))
    }

    /// Whether two definitions are the same, a bound on the error, whether they are the same
    /// without NULLs, and the witness.
    fn compare(&mut self, a: u32, b: u32) -> PyResult<(bool, f64, bool, Option<WitnessTuple>)> {
        self.check(a)?;
        self.check(b)?;
        let verdict = self.inner.compare(a, b);
        Ok((
            verdict.equal,
            verdict.error_bound,
            verdict.equal_without_nulls,
            verdict.witness.as_ref().map(witness),
        ))
    }
}
