//! The `konstruct._core` extension module: thin wrappers that convert between Python values and
//! the indexes in this crate. `konstruct/_core.pyi` describes it to type checkers.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::pybacked::PyBackedStr;

use crate::graph::{self, EdgeKind, EdgeSpec, Flow, Visit};
use crate::names::{self, Relation};
use crate::search::{self, Document, Standing};

/// A visit as Python receives it: node ordinal, depth, edge ordinal and previous node ordinal.
type VisitTuple = (u32, u32, Option<u32>, Option<u32>);

fn tuples(visits: Vec<Visit>) -> Vec<VisitTuple> {
    visits
        .into_iter()
        .map(|v| (v.node, v.depth, v.via, v.previous))
        .collect()
}

fn same_length(what: &str, expected: usize, lengths: &[usize]) -> PyResult<()> {
    if lengths.iter().all(|&n| n == expected) {
        Ok(())
    } else {
        Err(PyValueError::new_err(format!(
            "{what}: every list must have the same length"
        )))
    }
}

/// The edges of a graph, indexed for walks and shortest paths. Nodes and edges are numbered in
/// the order they were given.
#[pyclass(frozen, module = "konstruct._core")]
struct GraphIndex {
    inner: graph::GraphIndex,
}

#[pymethods]
impl GraphIndex {
    #[new]
    fn new(
        nodes: Vec<PyBackedStr>,
        sources: Vec<PyBackedStr>,
        targets: Vec<PyBackedStr>,
        kinds: Vec<PyBackedStr>,
        references: Vec<bool>,
    ) -> PyResult<Self> {
        same_length(
            "GraphIndex",
            sources.len(),
            &[targets.len(), kinds.len(), references.len()],
        )?;
        let edges: Vec<EdgeSpec<'_>> = (0..sources.len())
            .map(|i| EdgeSpec {
                source: &sources[i],
                target: &targets[i],
                kind: EdgeKind::parse(&kinds[i]),
                reference: references[i],
            })
            .collect();
        Ok(Self {
            inner: graph::GraphIndex::new(nodes.iter().map(|id| &**id), &edges),
        })
    }

    /// Everything reachable from the nodes `starts`, downstream or upstream, in the order reached.
    #[pyo3(signature = (starts, downstream, max_depth, stop_at_reference))]
    fn walk(
        &self,
        starts: Vec<PyBackedStr>,
        downstream: bool,
        max_depth: Option<i64>,
        stop_at_reference: bool,
    ) -> Vec<VisitTuple> {
        let starts: Vec<u32> = starts.iter().filter_map(|id| self.inner.node(id)).collect();
        let flow = if downstream {
            Flow::Downstream
        } else {
            Flow::Upstream
        };
        tuples(self.inner.walk(&starts, flow, max_depth, stop_at_reference))
    }

    /// The shortest path between two nodes, start first; empty when there is none.
    fn path(&self, start: &str, end: &str) -> Vec<VisitTuple> {
        match (self.inner.node(start), self.inner.node(end)) {
            (Some(start), Some(end)) => tuples(self.inner.path(start, end)),
            _ => Vec::new(),
        }
    }

    /// The ordinals of the edges a walk follows from `node`, downstream or upstream.
    fn neighbours(&self, node: &str, downstream: bool) -> Vec<u32> {
        let flow = if downstream {
            Flow::Downstream
        } else {
            Flow::Upstream
        };
        match self.inner.node(node) {
            Some(node) => self
                .inner
                .neighbours(node, flow)
                .map(|(edge, _)| edge)
                .collect(),
            None => Vec::new(),
        }
    }
}

/// Relations by every name they answer to.
#[pyclass(frozen, module = "konstruct._core")]
struct RelationNames {
    inner: names::RelationNames,
}

#[pymethods]
impl RelationNames {
    #[new]
    fn new(
        names: Vec<PyBackedStr>,
        ids: Vec<PyBackedStr>,
        relations: Vec<Option<PyBackedStr>>,
        outside_warehouse: Vec<bool>,
    ) -> PyResult<Self> {
        same_length(
            "RelationNames",
            names.len(),
            &[ids.len(), relations.len(), outside_warehouse.len()],
        )?;
        let relations = (0..names.len()).map(|i| Relation {
            name: &names[i],
            id: &ids[i],
            relation: relations[i].as_deref(),
            outside_warehouse: outside_warehouse[i],
        });
        Ok(Self {
            inner: names::RelationNames::new(relations),
        })
    }

    /// The ordinals of the relations `needle` (in lower case) names.
    fn find(&self, needle: &str) -> Vec<u32> {
        self.inner.find(needle)
    }
}

/// Ranked word search over a graph's nodes.
#[pyclass(frozen, module = "konstruct._core")]
struct SearchIndex {
    inner: search::SearchIndex,
}

#[pymethods]
impl SearchIndex {
    #[new]
    #[allow(clippy::too_many_arguments)]
    fn new(
        ids: Vec<PyBackedStr>,
        kinds: Vec<PyBackedStr>,
        standings: Vec<u8>,
        names: Vec<PyBackedStr>,
        labels: Vec<PyBackedStr>,
        words: Vec<PyBackedStr>,
        descriptions: Vec<PyBackedStr>,
        synonyms: Vec<Vec<PyBackedStr>>,
    ) -> PyResult<Self> {
        let lengths = [
            kinds.len(),
            standings.len(),
            names.len(),
            labels.len(),
            words.len(),
        ];
        same_length("SearchIndex", ids.len(), &lengths)?;
        same_length(
            "SearchIndex",
            ids.len(),
            &[descriptions.len(), synonyms.len()],
        )?;
        let mut documents = Vec::with_capacity(ids.len());
        for i in 0..ids.len() {
            let standing = match standings[i] {
                0 => Standing::Other,
                1 => Standing::Term,
                2 => Standing::Relation,
                3 => Standing::Test,
                other => {
                    return Err(PyValueError::new_err(format!(
                        "SearchIndex: unknown standing {other}"
                    )));
                }
            };
            documents.push(Document {
                id: &ids[i],
                kind: &kinds[i],
                standing,
                name: &names[i],
                label: &labels[i],
                names: &words[i],
                description: &descriptions[i],
                synonyms: synonyms[i].iter().map(|s| &**s).collect(),
            });
        }
        Ok(Self {
            inner: search::SearchIndex::new(documents),
        })
    }

    /// The nodes matching `text`, best first, as (ordinal, score).
    #[pyo3(signature = (text, needle, limit, kinds))]
    fn search(
        &self,
        py: Python<'_>,
        text: &str,
        needle: &str,
        limit: Option<usize>,
        kinds: Vec<PyBackedStr>,
    ) -> Vec<(u32, f64)> {
        let kinds: Vec<&str> = kinds.iter().map(|k| &**k).collect();
        py.detach(|| self.inner.search(text, needle, limit, &kinds))
    }
}

/// Konstruct's native core: graph traversal, name resolution and search.
#[pymodule(name = "_core")]
mod core_module {
    #[pymodule_export]
    use super::{GraphIndex, RelationNames, SearchIndex};
}
