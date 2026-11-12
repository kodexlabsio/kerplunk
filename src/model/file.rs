//! A `graph.json` as `Graph.read` reads it.
//!
//! The file is parsed once ([`Tape::parse`]); `Graph.read` checks its format from the header,
//! then [`File::load`] makes the nodes Python objects and the index from the shape. The edges
//! stay on the tape until Python reads one: each is made once, when first asked for, and the
//! same object is returned from then on, so code that edits an edge's attributes sees its edits
//! whichever way it reaches the edge again.

use std::sync::{Arc, Mutex};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyList, PyString};

use crate::fnv::FnvHashMap;
use crate::graph;
use crate::json::tape::{Doc, Kind, Number, Tape};
use crate::load::{NONE, Shape};
use crate::python::string;

use super::node::{Edge, Node};

/// Strings up to this many bytes are made once per file and shared: kinds, types, origins,
/// attribute keys and values such as `direct`.
const SHORT: usize = 24;

/// The short strings already made, by their bytes in the file.
type Shared = FnvHashMap<Box<[u8]>, Py<PyString>>;

/// Values on the tape as Python objects.
struct Convert<'py, 'a> {
    py: Python<'py>,
    doc: Doc<'a>,
    shared: &'a mut Shared,
}

impl<'py> Convert<'py, '_> {
    fn string(&mut self, value: usize) -> Bound<'py, PyString> {
        let raw = self.doc.raw(value);
        if raw.len() > SHORT || self.doc.escaped(value) {
            return PyString::new(self.py, &self.doc.text(value));
        }
        if let Some(made) = self.shared.get(raw) {
            return made.bind(self.py).clone();
        }
        let made = PyString::new(self.py, &self.doc.text(value));
        self.shared.insert(raw.into(), made.clone().unbind());
        made
    }

    fn value(&mut self, value: usize) -> PyResult<Bound<'py, PyAny>> {
        let py = self.py;
        Ok(match self.doc.kind(value) {
            Kind::Object => self.object(value)?.into_any(),
            Kind::Array => self.list(value)?.into_any(),
            Kind::String => self.string(value).into_any(),
            Kind::Number => match self.doc.number(value) {
                Number::Int(n) => n.into_pyobject(py)?.into_any(),
                Number::UInt(n) => n.into_pyobject(py)?.into_any(),
                Number::Float(n) => PyFloat::new(py, n).into_any(),
            },
            Kind::True => PyBool::new(py, true).to_owned().into_any(),
            Kind::False => PyBool::new(py, false).to_owned().into_any(),
            Kind::Null => py.None().into_bound(py),
        })
    }

    fn object(&mut self, object: usize) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(self.py);
        for (key, value) in self.doc.members(object) {
            dict.set_item(self.string(key), self.value(value)?)?;
        }
        Ok(dict)
    }

    fn list(&mut self, array: usize) -> PyResult<Bound<'py, PyList>> {
        let items = self
            .doc
            .items(array)
            .map(|item| self.value(item))
            .collect::<PyResult<Vec<_>>>()?;
        PyList::new(self.py, items)
    }

    /// The value of a field, or `absent` when there is none.
    fn field(
        &mut self,
        field: u32,
        absent: impl FnOnce(Python<'py>) -> Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        if field == NONE {
            Ok(absent(self.py))
        } else {
            self.value(field as usize)
        }
    }
}

/// `json.JSONDecodeError` for `message` at byte `at` of `bytes`: the position is counted in
/// characters, as the standard library's decoder counts it.
pub(crate) fn decode_error(py: Python<'_>, bytes: &[u8], message: &str, at: usize) -> PyErr {
    let text = String::from_utf8_lossy(bytes);
    let position = String::from_utf8_lossy(&bytes[..at.min(bytes.len())])
        .chars()
        .count();
    let made = py
        .import("json")
        .and_then(|json| json.getattr("JSONDecodeError"))
        .and_then(|class| class.call1((message, text.as_ref(), position)));
    match made {
        Ok(error) => PyErr::from_value(error),
        Err(error) => error,
    }
}

/// Python's cyclic garbage collector, paused while the nodes are made: they are new objects
/// that hold no cycles, and a collection every few thousand of them would walk every object
/// made so far again.
struct Paused(bool);

impl Paused {
    fn new(_py: Python<'_>) -> Self {
        // SAFETY: called with the interpreter attached (the `Python` token proves it); it only
        // switches the collector off and reports whether it was on.
        Self(unsafe { pyo3::ffi::PyGC_Disable() } != 0)
    }
}

impl Drop for Paused {
    fn drop(&mut self) {
        if self.0 {
            // SAFETY: as in `new`; the value lives only within `File::load`, which holds
            // the interpreter throughout.
            unsafe {
                pyo3::ffi::PyGC_Enable();
            }
        }
    }
}

/// What [`File::load`] makes, and what the edges are made from.
struct Loaded {
    shape: Shape,
    index: Arc<graph::GraphIndex>,
    /// The id of each node and edge endpoint, by ordinal: the node's own string for a node.
    names: Vec<PyOnceLock<Py<PyString>>>,
    /// Each edge kind, by its index in `shape.kinds`.
    kinds: Vec<Py<PyString>>,
    edges: Vec<PyOnceLock<Py<Edge>>>,
    shared: Mutex<Shared>,
}

/// Why bytes are not a JSON document the reader takes.
pub(crate) enum OpenError {
    /// Longer than the 2 GiB the reader takes.
    TooLarge,
    /// Not JSON: what is wrong, and the byte offset where that first shows.
    Syntax { message: &'static str, at: usize },
}

/// The nodes of a loaded file, as `Graph.read` needs them.
pub(crate) struct Nodes<'py> {
    /// By id, the first node of each id.
    pub(crate) by_id: Bound<'py, PyDict>,
    /// The later nodes with an id that came before, each with the number of first nodes before
    /// it, for `Graph.add_node` to merge.
    pub(crate) later: Vec<(usize, Py<Node>)>,
}

/// A parsed `graph.json`.
pub(crate) struct File {
    bytes: Py<PyBytes>,
    tape: Tape,
    loaded: Option<Loaded>,
}

impl File {
    /// Parses `data`.
    pub(crate) fn open(py: Python<'_>, data: Bound<'_, PyBytes>) -> Result<Self, OpenError> {
        let bytes = data.as_bytes();
        if bytes.len() > crate::json::MAX_LEN {
            return Err(OpenError::TooLarge);
        }
        let tape = py
            .detach(|| Tape::parse(bytes))
            .map_err(|error| OpenError::Syntax {
                message: error.message,
                at: error.at,
            })?;
        Ok(Self {
            bytes: data.unbind(),
            tape,
            loaded: None,
        })
    }

    fn doc<'a>(&'a self, py: Python<'a>) -> Doc<'a> {
        self.tape.doc(self.bytes.as_bytes(py))
    }

    fn loaded(&self) -> &Loaded {
        self.loaded.as_ref().expect("the file is loaded")
    }

    /// The whole document as Python values, as `Graph.from_dict` takes it: for a document that
    /// is not a graph as Konstruct writes one, which `load` refuses.
    pub(crate) fn document<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let _paused = Paused::new(py);
        let mut shared = Shared::default();
        Convert {
            py,
            doc: self.doc(py),
            shared: &mut shared,
        }
        .value(0)
    }

    /// The document's members other than `nodes` and `edges`, or the document itself when it
    /// is not an object.
    pub(crate) fn header<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let doc = self.doc(py);
        let mut shared = Shared::default();
        let mut convert = Convert {
            py,
            doc,
            shared: &mut shared,
        };
        if doc.kind(0) != Kind::Object {
            return convert.value(0);
        }
        let header = PyDict::new(py);
        for (key, value) in doc.members(0) {
            if !(doc.is(key, "nodes") || doc.is(key, "edges")) {
                header.set_item(convert.string(key), convert.value(value)?)?;
            }
        }
        Ok(header.into_any())
    }

    /// Makes the nodes Python objects and the index from the document's shape; the edges stay on
    /// the tape.
    ///
    /// Raises `ValueError(message)` when the document is not a graph as Konstruct writes one:
    /// `document` gives it to `Graph.from_dict` instead.
    pub(crate) fn load<'py>(&mut self, py: Python<'py>) -> PyResult<Nodes<'py>> {
        let doc = self.tape.doc(self.bytes.as_bytes(py));
        let mut shape = py
            .detach(|| Shape::read(doc))
            .map_err(PyValueError::new_err)?;
        let index = Arc::new(std::mem::replace(
            &mut shape.index,
            graph::GraphIndex::builder().build(),
        ));
        let names: Vec<PyOnceLock<Py<PyString>>> = (0..index.endpoint_count())
            .map(|_| PyOnceLock::new())
            .collect();
        let nodes = PyDict::new(py);
        let mut later = Vec::new();
        let mut shared = Shared::default();
        {
            let _paused = Paused::new(py);
            let mut convert = Convert {
                py,
                doc,
                shared: &mut shared,
            };
            let none = |py: Python<'py>| py.None().into_bound(py);
            let mut ordinal = 0usize;
            let mut last_parent: Option<(&[u8], Bound<'py, PyAny>)> = None;
            for fields in &shape.nodes {
                let id = PyString::new(py, &doc.text(fields.id as usize));
                let kind = convert.string(fields.kind as usize);
                let name = convert.value(fields.name as usize)?;
                // A column's parent is a node, so its string is the parent's own id; the
                // columns of a relation come together, so it is usually the last one's.
                let parent = match fields.parent {
                    NONE => none(py),
                    parent if doc.kind(parent as usize) == Kind::String => {
                        let raw = doc.raw(parent as usize);
                        match &last_parent {
                            Some((bytes, made)) if *bytes == raw => made.clone(),
                            _ => {
                                let text = doc.text(parent as usize);
                                let made = match index.node(&crate::keys::from_str(&text)) {
                                    Some(known) if (known as usize) < ordinal => {
                                        self_name(py, &names, known)?.into_any()
                                    }
                                    _ => PyString::new(py, &text).into_any(),
                                };
                                last_parent = Some((raw, made.clone()));
                                made
                            }
                        }
                    }
                    _ => none(py),
                };
                let node = Node {
                    id: id.clone().into_any().unbind(),
                    kind: kind.into_any().unbind(),
                    name: name.unbind(),
                    parent: parent.unbind(),
                    relation: convert.field(fields.relation, none)?.unbind(),
                    description: convert.field(fields.description, none)?.unbind(),
                    data_type: convert.field(fields.data_type, none)?.unbind(),
                    path: convert.field(fields.path, none)?.unbind(),
                    origins: convert
                        .field(fields.origins, |py| PyList::empty(py).into_any())?
                        .unbind(),
                    attrs: convert
                        .field(fields.attrs, |py| PyDict::new(py).into_any())?
                        .unbind(),
                };
                let node = Py::new(py, node)?;
                if fields.first {
                    names[ordinal]
                        .set(py, id.clone().unbind())
                        .expect("each node is made once");
                    nodes.set_item(id, node)?;
                    ordinal += 1;
                } else {
                    // With the number of nodes before it, so Python merges it where it was.
                    later.push((ordinal, node));
                }
            }
        }
        let kinds = shape
            .kinds
            .iter()
            .map(|kind| PyString::new(py, kind).unbind())
            .collect();
        let edges = (0..shape.edges.len()).map(|_| PyOnceLock::new()).collect();
        self.loaded = Some(Loaded {
            shape,
            index,
            names,
            kinds,
            edges,
            shared: Mutex::new(shared),
        });
        Ok(Nodes {
            by_id: nodes,
            later,
        })
    }

    /// The index of the graph's edges, for walks and paths.
    pub(crate) fn index(&self) -> Arc<graph::GraphIndex> {
        Arc::clone(&self.loaded().index)
    }

    /// The number of edges.
    pub(crate) fn len(&self) -> usize {
        self.loaded().edges.len()
    }

    fn name<'py>(&self, py: Python<'py>, ordinal: u32) -> PyResult<Bound<'py, PyString>> {
        let loaded = self.loaded();
        made_once(py, &loaded.names[ordinal as usize], || {
            string(py, loaded.index.id(ordinal))
        })
    }

    /// The edge with this ordinal, made when first asked for.
    pub(crate) fn edge<'py>(&self, py: Python<'py>, ordinal: u32) -> PyResult<Bound<'py, Edge>> {
        let loaded = self.loaded();
        made_once(py, &loaded.edges[ordinal as usize], || {
            let ends = loaded.index.edge(ordinal);
            // The short strings made so far, unless another call holds them (a thread, or a
            // finaliser run by the collector while this one makes objects): then this edge
            // makes its own, so nothing ever waits for the table.
            let mut own = Shared::default();
            let mut held = loaded.shared.try_lock().ok();
            let mut convert = Convert {
                py,
                doc: self.doc(py),
                shared: held.as_deref_mut().unwrap_or(&mut own),
            };
            let attrs = PyDict::new(py);
            for (number, object) in loaded.shape.attrs(ordinal).enumerate() {
                let more = convert.object(object)?;
                if number == 0 {
                    attrs.update(more.as_mapping())?;
                } else {
                    // A later edge that is the same edge adds what this one lacks.
                    for (key, value) in more.iter() {
                        if !attrs.contains(&key)? {
                            attrs.set_item(key, value)?;
                        }
                    }
                }
            }
            drop(held);
            let kind = loaded.shape.edges[ordinal as usize].kind as usize;
            Py::new(
                py,
                Edge {
                    source: self.name(py, ends.source)?.into_any().unbind(),
                    target: self.name(py, ends.target)?.into_any().unbind(),
                    kind: loaded.kinds[kind].clone_ref(py).into_any(),
                    attrs: attrs.into_any().unbind(),
                },
            )
            .map(|edge| edge.into_bound(py))
        })
    }

    /// The edge ordinals at `node` that `outgoing` or incoming lists, of kind `kind` if given.
    pub(crate) fn adjacent(&self, node: &str, kind: Option<&str>, outgoing: bool) -> Vec<u32> {
        let loaded = self.loaded();
        let Some(ordinal) = loaded.index.ordinal(node) else {
            return Vec::new();
        };
        let wanted = match kind {
            None => None,
            Some(kind) => match loaded.shape.kinds.iter().position(|k| **k == *kind) {
                Some(found) => Some(found as u16),
                None => return Vec::new(),
            },
        };
        let edges = if outgoing {
            loaded.index.outgoing(ordinal)
        } else {
            loaded.index.incoming(ordinal)
        };
        edges
            .iter()
            .copied()
            .filter(|&e| wanted.is_none_or(|k| loaded.shape.edges[e as usize].kind == k))
            .collect()
    }
}

/// The object in `cell`, made by `make` the first time it is asked for. Nothing is held while
/// `make` runs Python, so a thread, or a finaliser the collector runs meanwhile, that asks for
/// the same object makes one too rather than waiting; the first to be stored is the one every
/// caller gets from then on.
fn made_once<'py, T>(
    py: Python<'py>,
    cell: &PyOnceLock<Py<T>>,
    make: impl FnOnce() -> PyResult<Bound<'py, T>>,
) -> PyResult<Bound<'py, T>> {
    if let Some(made) = cell.get(py) {
        return Ok(made.bind(py).clone());
    }
    let made = make()?.unbind();
    // Another caller may have stored one first; then this one is dropped.
    let _ = cell.set(py, made);
    Ok(cell.get(py).expect("set above or before").bind(py).clone())
}

/// The string `names` holds for `ordinal`, set when its node was made.
fn self_name<'py>(
    py: Python<'py>,
    names: &[PyOnceLock<Py<PyString>>],
    ordinal: u32,
) -> PyResult<Bound<'py, PyString>> {
    names[ordinal as usize]
        .get(py)
        .map(|name| name.bind(py).clone())
        .ok_or_else(|| PyValueError::new_err("a node is named before it is made"))
}
