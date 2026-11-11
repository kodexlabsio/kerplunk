//! The `konstruct._core` extension module: thin wrappers that convert between Python values and
//! the indexes in this crate. `konstruct/_core.pyi` describes it to type checkers.

use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use pyo3::exceptions::{PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::pybacked::PyBackedStr;
use pyo3::types::{PyDict, PyList, PyString};

use crate::graph::{self, EdgeKind, Flow, NodeKind, Visit};
use crate::keys;
use crate::names::{self, Relation};
use crate::search::{self, Document, Standing};
use crate::serve::api::{self, Argument};
use crate::serve::{self, Outcome};

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

/// The items of `list`, each as a Python string.
fn strings<'py>(
    list: &Bound<'py, PyList>,
) -> impl Iterator<Item = PyResult<Bound<'py, PyString>>> + use<'py> {
    list.iter()
        .map(|item| item.cast_into::<PyString>().map_err(PyErr::from))
}

/// `text` as a key of the indexes ([`keys`]), borrowed from Python unless it holds a lone
/// surrogate or the escape character, as almost no string does.
pub(crate) fn key<'a>(text: &'a Bound<'_, PyString>) -> PyResult<std::borrow::Cow<'a, str>> {
    match text.to_str() {
        Ok(utf8) => Ok(keys::from_str(utf8)),
        Err(_) => {
            let py = text.py();
            let encoded = text.call_method1(
                pyo3::intern!(py, "encode"),
                (
                    pyo3::intern!(py, "utf-8"),
                    pyo3::intern!(py, "surrogatepass"),
                ),
            )?;
            let bytes = encoded.cast::<pyo3::types::PyBytes>()?.as_bytes();
            keys::from_generalized_utf8(bytes)
                .map(std::borrow::Cow::Owned)
                .ok_or_else(|| PyValueError::new_err("surrogatepass gave bytes that are not UTF-8"))
        }
    }
}

/// The Python string the key `key` stands for.
fn string<'py>(py: Python<'py>, key: &str) -> PyResult<Bound<'py, PyString>> {
    match keys::to_generalized_utf8(key) {
        std::borrow::Cow::Borrowed(_) => Ok(PyString::new(py, key)),
        std::borrow::Cow::Owned(bytes) => Ok(pyo3::types::PyBytes::new(py, &bytes)
            .call_method1(
                pyo3::intern!(py, "decode"),
                (
                    pyo3::intern!(py, "utf-8"),
                    pyo3::intern!(py, "surrogatepass"),
                ),
            )?
            .cast_into::<PyString>()?),
    }
}

/// A Python string argument as a key of the indexes ([`keys`]).
pub(crate) enum Key {
    /// The string itself, its UTF-8 kept by Python.
    Python(PyBackedStr),
    Escaped(String),
}

impl std::ops::Deref for Key {
    type Target = str;

    fn deref(&self) -> &str {
        match self {
            Self::Python(text) => text,
            Self::Escaped(key) => key,
        }
    }
}

impl FromPyObject<'_, '_> for Key {
    type Error = PyErr;

    fn extract(object: Borrowed<'_, '_, PyAny>) -> PyResult<Self> {
        let text = object.cast::<PyString>()?.to_owned();
        if let Ok(utf8) = text.to_str()
            && !keys::holds_escape(utf8)
        {
            return Ok(Self::Python(PyBackedStr::try_from(text)?));
        }
        Ok(Self::Escaped(key(&text)?.into_owned()))
    }
}

/// The edges of a graph, indexed for walks and shortest paths. Nodes and edges are numbered in
/// the order they were given.
#[pyclass(frozen, module = "konstruct._core")]
struct GraphIndex {
    inner: graph::GraphIndex,
}

impl GraphIndex {
    fn ordinals(&self, ids: &[Key]) -> Vec<u32> {
        ids.iter().filter_map(|id| self.inner.node(id)).collect()
    }
}

#[pymethods]
impl GraphIndex {
    #[new]
    #[allow(clippy::too_many_arguments)]
    fn new(
        nodes: &Bound<'_, PyList>,
        node_kinds: &Bound<'_, PyList>,
        parents: &Bound<'_, PyList>,
        sources: &Bound<'_, PyList>,
        targets: &Bound<'_, PyList>,
        kinds: &Bound<'_, PyList>,
        references: Vec<bool>,
        through: Vec<(u32, Key)>,
    ) -> PyResult<Self> {
        same_length(
            "GraphIndex",
            nodes.len(),
            &[node_kinds.len(), parents.len()],
        )?;
        same_length(
            "GraphIndex",
            sources.len(),
            &[targets.len(), kinds.len(), references.len()],
        )?;
        let mut builder = graph::GraphIndex::builder();
        for (node, kind) in strings(nodes).zip(strings(node_kinds)) {
            builder.node(&key(&node?)?, NodeKind::parse(&key(&kind?)?));
        }
        let edges = strings(sources)
            .zip(strings(targets))
            .zip(strings(kinds))
            .zip(references);
        for (((source, target), kind), reference) in edges {
            let (source, target, kind) = (source?, target?, kind?);
            builder.edge(
                &key(&source)?,
                &key(&target)?,
                EdgeKind::parse(&key(&kind)?),
                reference,
            );
        }
        for (ordinal, parent) in parents.iter().enumerate() {
            if !parent.is_none() {
                builder.parent(
                    graph::ordinal(ordinal),
                    &key(&parent.cast_into::<PyString>()?)?,
                );
            }
        }
        for (edge, task) in &through {
            builder.through(*edge, task);
        }
        Ok(Self {
            inner: builder.build(),
        })
    }

    /// Everything downstream of the nodes `starts`, in the order reached; with `column`, the
    /// consumers of the relations reached too.
    #[pyo3(signature = (starts, max_depth, column))]
    fn impact(&self, starts: Vec<Key>, max_depth: Option<i64>, column: bool) -> Vec<VisitTuple> {
        tuples(
            self.inner
                .impact(&self.ordinals(&starts), max_depth, column),
        )
    }

    /// Everything upstream of the nodes `starts`, in the order reached; with `column`, the tasks
    /// that build the relations on the way too.
    #[pyo3(signature = (starts, max_depth, column))]
    fn trace(&self, starts: Vec<Key>, max_depth: Option<i64>, column: bool) -> Vec<VisitTuple> {
        tuples(self.inner.trace(&self.ordinals(&starts), max_depth, column))
    }

    /// The id of the node or edge endpoint with this ordinal.
    fn id<'py>(&self, py: Python<'py>, ordinal: u32) -> PyResult<Bound<'py, PyString>> {
        string(py, self.inner.id(ordinal))
    }

    /// The shortest path between two nodes, start first; empty when there is none.
    fn path(&self, start: Key, end: Key) -> Vec<VisitTuple> {
        match (self.inner.node(&start), self.inner.node(&end)) {
            (Some(start), Some(end)) => tuples(self.inner.path(start, end)),
            _ => Vec::new(),
        }
    }

    /// Whether `consumer` records which of `relation`'s columns it reads: a column of
    /// `relation` feeds it.
    fn fed_by_columns_of(&self, consumer: Key, relation: Key) -> bool {
        match (self.inner.node(&consumer), self.inner.node(&relation)) {
            (Some(consumer), Some(relation)) => self.inner.fed_by_columns_of(consumer, relation),
            _ => false,
        }
    }

    /// The ordinals of the edges a walk follows from `node`, downstream or upstream.
    fn neighbours(&self, node: Key, downstream: bool) -> Vec<u32> {
        let flow = if downstream {
            Flow::Downstream
        } else {
            Flow::Upstream
        };
        match self.inner.node(&node) {
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
        names: Vec<Key>,
        ids: Vec<Key>,
        relations: Vec<Option<Key>>,
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
    fn find(&self, needle: Key) -> Vec<u32> {
        self.inner.find(&needle)
    }
}

/// Ranked word search over a graph's nodes.
#[pyclass(frozen, module = "konstruct._core")]
pub(crate) struct SearchIndex {
    pub(crate) inner: search::SearchIndex,
}

#[pymethods]
impl SearchIndex {
    #[new]
    #[allow(clippy::too_many_arguments)]
    fn new(
        ids: &Bound<'_, PyList>,
        kinds: &Bound<'_, PyList>,
        standings: Vec<u8>,
        names: &Bound<'_, PyList>,
        labels: &Bound<'_, PyList>,
        words: &Bound<'_, PyList>,
        descriptions: &Bound<'_, PyList>,
        synonyms: &Bound<'_, PyList>,
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
        let mut index = search::SearchIndex::default();
        let fields = strings(ids)
            .zip(strings(kinds))
            .zip(standings)
            .zip(strings(names));
        let more = strings(labels)
            .zip(strings(words))
            .zip(strings(descriptions))
            .zip(synonyms.iter());
        for ((((id, kind), standing), name), (((label, words), description), synonyms)) in
            fields.zip(more)
        {
            let standing = match standing {
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
            let (id, kind, name, label, words, description) =
                (id?, kind?, name?, label?, words?, description?);
            let synonyms: Vec<Bound<'_, PyString>> =
                strings(synonyms.cast::<PyList>()?).collect::<PyResult<_>>()?;
            let synonyms: Vec<_> = synonyms.iter().map(key).collect::<PyResult<_>>()?;
            index.add(Document {
                id: &key(&id)?,
                kind: &key(&kind)?,
                standing,
                name: &key(&name)?,
                label: &key(&label)?,
                names: &key(&words)?,
                description: &key(&description)?,
                synonyms: synonyms.iter().map(|s| &**s).collect(),
            });
        }
        Ok(Self { inner: index })
    }

    /// The nodes matching `text`, best first, as (ordinal, score).
    #[pyo3(signature = (text, needle, limit, kinds))]
    fn search(
        &self,
        py: Python<'_>,
        text: Key,
        needle: Key,
        limit: Option<usize>,
        kinds: Vec<Key>,
    ) -> Vec<(u32, f64)> {
        let kinds: Vec<&str> = kinds.iter().map(|k| &**k).collect();
        py.detach(|| self.inner.search(&text, &needle, limit, &kinds))
    }
}

/// The Python function that answers the API's operations: called with the operation id and its
/// arguments, it returns `("ok", media type, body, graph version)` or
/// `(problem type, "", detail, graph version)`.
struct PythonBackend {
    function: Py<PyAny>,
}

impl serve::Backend for PythonBackend {
    fn answer(&self, operation: &str, arguments: &[(&'static str, Argument)]) -> Outcome {
        // The error is the operator's, on the server's terminal; a client is not told where
        // this machine keeps the project.
        let failed = |error: PyErr| {
            eprintln!("konstruct: {operation} failed: {error}");
            Outcome::Problem {
                kind: "internal-error".to_owned(),
                detail: format!("{operation} failed; the server's output says why"),
            }
        };
        Python::attach(|py| {
            let keywords = PyDict::new(py);
            for (name, argument) in arguments {
                let set = match argument {
                    Argument::Text(text) => keywords.set_item(name, text),
                    Argument::Integer(number) => keywords.set_item(name, number),
                    Argument::Boolean(flag) => keywords.set_item(name, flag),
                    Argument::TextList(items) => keywords.set_item(name, items),
                };
                if let Err(error) = set {
                    return failed(error);
                }
            }
            let result = match self.function.call1(py, (operation, keywords)) {
                Ok(result) => result,
                Err(error) => return failed(error),
            };
            match result.extract::<(String, String, String, String)>(py) {
                Ok((kind, media, body, version)) if kind == "ok" => Outcome::Answer {
                    version,
                    media,
                    body: body.into_bytes(),
                },
                Ok((kind, _, detail, _)) => Outcome::Problem { kind, detail },
                Err(error) => failed(error),
            }
        })
    }
}

/// The version `konstruct serve` gives a graph.json with these bytes.
#[pyfunction]
fn graph_version(data: &[u8]) -> String {
    serve::graph_version(data)
}

/// The OpenAPI 3.1 document of `konstruct serve`'s API, as it is served.
#[pyfunction]
fn openapi() -> String {
    api::openapi_text()
}

/// `konstruct serve`: the viewer and the JSON API over the graph in an output directory.
#[pyclass(frozen, module = "konstruct._core")]
struct Server {
    inner: Mutex<Option<serve::Server>>,
    host: String,
    port: u16,
}

#[pymethods]
impl Server {
    /// Load `out_dir/graph.json` and bind `host:port` (0 for any free port). Raises OSError when
    /// there is no graph or the port is taken.
    #[new]
    #[pyo3(signature = (out_dir, host, port, page, token=None, api=None))]
    fn new(
        py: Python<'_>,
        out_dir: PathBuf,
        host: &str,
        port: u16,
        page: String,
        token: Option<String>,
        api: Option<Py<PyAny>>,
    ) -> PyResult<Self> {
        let mut options = serve::Options::new(out_dir, host, port, page);
        options.token = token;
        options.backend =
            api.map(|function| Arc::new(PythonBackend { function }) as Arc<dyn serve::Backend>);
        let server = py
            .detach(|| serve::Server::bind(options))
            .map_err(|error| PyOSError::new_err(error.to_string()))?;
        let address = server.address();
        Ok(Self {
            inner: Mutex::new(Some(server)),
            host: address.ip().to_string(),
            port: address.port(),
        })
    }

    /// The address bound.
    #[getter]
    fn host(&self) -> &str {
        &self.host
    }

    /// The port bound, which is the one asked for unless that was 0.
    #[getter]
    fn port(&self) -> u16 {
        self.port
    }

    /// Serve until interrupted, without holding the GIL. Signals are checked every 100 ms, so
    /// Ctrl-C raises KeyboardInterrupt here, after open connections have been closed.
    fn run(&self, py: Python<'_>) -> PyResult<()> {
        let server = self
            .inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("the server failed"))?
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("the server has already run"))?;
        let mut interrupted: Option<PyErr> = None;
        let result = py.detach(|| {
            server.run(|| {
                Python::attach(|py| match py.check_signals() {
                    Ok(()) => false,
                    Err(error) => {
                        interrupted = Some(error);
                        true
                    }
                })
            })
        });
        if let Some(error) = interrupted {
            return Err(error);
        }
        result.map_err(|error| PyOSError::new_err(error.to_string()))
    }
}

/// Konstruct's native core: graph traversal, name resolution, search, search by meaning,
/// formula identity and the server.
#[pymodule(name = "_core")]
mod core_module {
    use pyo3::exceptions::PyImportError;
    use pyo3::prelude::*;

    #[pymodule_export]
    use super::{GraphIndex, RelationNames, SearchIndex, Server, graph_version, openapi};
    #[pymodule_export]
    use crate::identity::python::FormulaSet;
    #[pymodule_export]
    use crate::vector::python::{VectorIndex, concept, terms};

    /// Refuses to load on a processor the routines cannot run on, before anything calls them.
    #[pymodule_init]
    fn init(_module: &Bound<'_, PyModule>) -> PyResult<()> {
        match crate::unsupported_processor() {
            Some(reason) => Err(PyImportError::new_err(reason)),
            None => Ok(()),
        }
    }
}
