//! `konstruct._core.VectorIndex`: the vector index as Python sees it.

use std::borrow::Cow;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList, PyString};

use crate::graph::EdgeKind;
use crate::python::{Key, SearchIndex, key};

use super::embed::{Link, Node};
use super::fuse;

/// A result as Python receives it: node ordinal, score and what the match rests on.
type HitTuple = (u32, f32, &'static str);

fn threads() -> usize {
    std::thread::available_parallelism().map_or(1, |n| n.get().min(8))
}

fn text<'py>(item: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyString>> {
    item.cast_into::<PyString>().map_err(PyErr::from)
}

/// Search by meaning over a graph's nodes.
#[pyclass(frozen, module = "konstruct._core")]
pub struct VectorIndex {
    inner: super::VectorIndex,
}

#[pymethods]
impl VectorIndex {
    #[new]
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (ids, kinds, standings, names, qualifiers, descriptions, synonyms, expressions, sources, targets, edge_kinds, previous))]
    fn new(
        py: Python<'_>,
        ids: &Bound<'_, PyList>,
        kinds: &Bound<'_, PyList>,
        standings: Vec<u8>,
        names: &Bound<'_, PyList>,
        qualifiers: &Bound<'_, PyList>,
        descriptions: &Bound<'_, PyList>,
        synonyms: &Bound<'_, PyList>,
        expressions: &Bound<'_, PyList>,
        sources: Vec<u32>,
        targets: Vec<u32>,
        edge_kinds: &Bound<'_, PyList>,
        previous: Option<Vec<u8>>,
    ) -> PyResult<Self> {
        let count = ids.len();
        let lists = [
            kinds,
            names,
            qualifiers,
            descriptions,
            synonyms,
            expressions,
        ];
        if standings.len() != count || lists.iter().any(|l| l.len() != count) {
            return Err(PyValueError::new_err(
                "VectorIndex: every node list must have the same length",
            ));
        }
        if sources.len() != targets.len() || edge_kinds.len() != sources.len() {
            return Err(PyValueError::new_err(
                "VectorIndex: every edge list must have the same length",
            ));
        }
        if sources.iter().chain(&targets).any(|&n| n as usize >= count) {
            return Err(PyValueError::new_err(
                "VectorIndex: an edge names a node that is not given",
            ));
        }
        // Python strings stay alive, and their UTF-8 forms with them, for the whole build. Each
        // is read as a key of the indexes, so a lone surrogate is escaped rather than refused.
        let mut held: Vec<Bound<'_, PyString>> = Vec::with_capacity(count * 5);
        for list in [ids, kinds, names, qualifiers, descriptions] {
            for item in list.iter() {
                held.push(text(item)?);
            }
        }
        let mut synonym_lists: Vec<Vec<Bound<'_, PyString>>> = Vec::with_capacity(count);
        for item in synonyms.iter() {
            synonym_lists.push(
                item.cast::<PyList>()?
                    .iter()
                    .map(text)
                    .collect::<PyResult<_>>()?,
            );
        }
        let mut expression_of: Vec<Option<Bound<'_, PyString>>> = Vec::with_capacity(count);
        for item in expressions.iter() {
            expression_of.push(if item.is_none() {
                None
            } else {
                Some(text(item)?)
            });
        }
        let fields: Vec<Cow<'_, str>> = held.iter().map(key).collect::<PyResult<_>>()?;
        let synonym_keys: Vec<Vec<Cow<'_, str>>> = synonym_lists
            .iter()
            .map(|list| list.iter().map(key).collect::<PyResult<_>>())
            .collect::<PyResult<_>>()?;
        let expression_keys: Vec<Option<Cow<'_, str>>> = expression_of
            .iter()
            .map(|e| e.as_ref().map(key).transpose())
            .collect::<PyResult<_>>()?;
        let column = |field: usize, node: usize| &*fields[field * count + node];
        let nodes: Vec<Node<'_>> = (0..count)
            .map(|node| Node {
                id: column(0, node),
                kind: column(1, node),
                name: column(2, node),
                qualifier: column(3, node),
                description: column(4, node),
                synonyms: synonym_keys[node].iter().map(|s| &**s).collect(),
                expression: expression_keys[node].as_deref(),
            })
            .collect();
        let links: Vec<Link> = (0..sources.len())
            .map(|i| {
                let kind = text(edge_kinds.get_item(i)?)?;
                Ok(Link {
                    source: sources[i],
                    target: targets[i],
                    kind: EdgeKind::parse(&key(&kind)?),
                })
            })
            .collect::<PyResult<_>>()?;
        // The &str borrows above are of Python strings, which outlive the build, or of keys
        // owned here; the build itself runs without the interpreter lock.
        let nodes = SendNodes(nodes);
        let inner = py.detach(|| {
            let nodes = nodes;
            super::VectorIndex::new(&nodes.0, &links, &standings, previous.as_deref(), threads())
        });
        Ok(Self { inner })
    }

    /// The nodes that best answer `text`, best first, as (ordinal, score, basis).
    #[pyo3(signature = (text, limit, kinds))]
    fn search(&self, py: Python<'_>, text: Key, limit: usize, kinds: Vec<Key>) -> Vec<HitTuple> {
        let kinds: Vec<&str> = kinds.iter().map(|k| &**k).collect();
        py.detach(|| tuples(self.inner.search(&text, limit, &kinds)))
    }

    /// The nodes most like node `ordinal`, best first, as (ordinal, score, basis).
    #[pyo3(signature = (ordinal, limit, kinds))]
    fn similar(
        &self,
        py: Python<'_>,
        ordinal: u32,
        limit: usize,
        kinds: Vec<Key>,
    ) -> Vec<HitTuple> {
        let kinds: Vec<&str> = kinds.iter().map(|k| &**k).collect();
        py.detach(|| tuples(self.inner.similar(ordinal, limit, &kinds)))
    }

    /// The word search of `words` (given `lowered`, the text in lower case, and `needle`, the
    /// whole query trimmed and lowered) fused with this search of `text`, best first, as
    /// (ordinal, score, basis or None when only the word search found the node).
    #[pyo3(signature = (words, text, lowered, needle, limit, kinds))]
    #[allow(clippy::too_many_arguments)]
    fn hybrid(
        &self,
        py: Python<'_>,
        words: PyRef<'_, SearchIndex>,
        text: Key,
        lowered: Key,
        needle: Key,
        limit: usize,
        kinds: Vec<Key>,
    ) -> Vec<(u32, f64, Option<&'static str>)> {
        let kinds: Vec<&str> = kinds.iter().map(|k| &**k).collect();
        let words = &words.inner;
        py.detach(|| {
            let pool = (2 * limit).max(40);
            let named: Vec<(u32, bool)> = words
                .search(&lowered, &needle, Some(pool), &kinds)
                .into_iter()
                .map(|(ordinal, _)| (ordinal, words.is_named(ordinal, &needle)))
                .collect();
            let meaning: Vec<(u32, &'static str)> = self
                .inner
                .search(&text, pool, &kinds)
                .into_iter()
                .map(|hit| (hit.ordinal, hit.basis.name()))
                .collect();
            let weights = fuse::weights(super::formula::is_formula(&text));
            fuse::fuse(&named, &meaning, weights, limit, |a, b| {
                self.inner.order(a, b)
            })
            .into_iter()
            .map(|f| (f.ordinal, f.score, f.basis))
            .collect()
        })
    }

    /// The index as the bytes of a saved index (``vectors.bin``).
    fn to_bytes<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        let bytes = py.detach(|| self.inner.to_bytes());
        PyBytes::new(py, &Cow::from(bytes))
    }

    /// What reading the saved index did: (kept, added, removed, reused).
    #[getter]
    fn update(&self) -> (usize, usize, usize, bool) {
        let update = self.inner.update();
        (update.kept, update.added, update.removed, update.reused)
    }

    /// True when every node's blocks were read from the saved index rather than computed.
    #[getter]
    fn rows_read(&self) -> bool {
        self.inner.update().rows_read
    }

    fn __len__(&self) -> usize {
        self.inner.len()
    }
}

/// The words of `text` as search by meaning reads them: split where a person reads a word
/// boundary, in lower case, abbreviations expanded, singular, stopwords left out.
#[pyfunction]
pub fn terms(text: Key) -> Vec<String> {
    super::text::terms(&text)
}

/// The concept a word from [`terms`] belongs to (`sale` and `turnover` to revenue), if any.
#[pyfunction]
pub fn concept(word: Key) -> Option<&'static str> {
    super::text::concept(&word)
}

fn tuples(hits: Vec<super::Hit>) -> Vec<HitTuple> {
    hits.into_iter()
        .map(|hit| (hit.ordinal, hit.score, hit.basis.name()))
        .collect()
}

/// Node views of Python strings, moved into a closure that runs without the interpreter lock.
struct SendNodes<'a>(Vec<Node<'a>>);

// SAFETY: the views are of the UTF-8 forms of Python strings, which are immutable and kept
// alive by the caller for the whole of the closure, or of keys the caller owns for as long;
// nothing touches Python objects in it.
unsafe impl Send for SendNodes<'_> {}
