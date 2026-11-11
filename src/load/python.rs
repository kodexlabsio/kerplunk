//! `konstruct._core.GraphFile`: `graph.json` as `Graph.read` reads it.
//!
//! The file is parsed once ([`Tape::parse`]); `Graph.read` checks its format from the header,
//! then [`GraphFile::load`] makes the nodes Python objects and the index from the shape. The
//! edges stay on the tape until Python reads one: each is made once, when first asked for, and
//! the same object is returned from then on, so code that edits an edge's attributes sees its
//! edits whichever way it reaches the edge again.

use std::sync::{Arc, Mutex};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyList, PyString, PyType};

use crate::fnv::FnvHashMap;
use crate::graph;
use crate::json::tape::{Doc, Kind, Number, Tape};
use crate::python::{GraphIndex, string};

use super::{NONE, Shape};

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
            // SAFETY: as in `new`; the value lives only within `GraphFile::load`, which holds
            // the interpreter throughout.
            unsafe {
                pyo3::ffi::PyGC_Enable();
            }
        }
    }
}

/// What [`GraphFile::load`] makes, and what the edges are made from.
struct Loaded {
    shape: Shape,
    index: Arc<graph::GraphIndex>,
    edge_type: Py<PyType>,
    new: Py<PyAny>,
    /// The id of each node and edge endpoint, by ordinal: the node's own string for a node.
    names: Vec<PyOnceLock<Py<PyString>>>,
    /// Each edge kind, by its index in `shape.kinds`.
    kinds: Vec<Py<PyString>>,
    edges: Vec<PyOnceLock<Py<PyAny>>>,
    shared: Mutex<Shared>,
}

/// A parsed `graph.json`.
#[pyclass(frozen, sequence, module = "konstruct._core")]
pub struct GraphFile {
    bytes: Py<PyBytes>,
    tape: Tape,
    loaded: PyOnceLock<Loaded>,
}

impl GraphFile {
    fn doc<'a>(&'a self, py: Python<'a>) -> Doc<'a> {
        self.tape.doc(self.bytes.as_bytes(py))
    }

    fn loaded(&self, py: Python<'_>) -> PyResult<&Loaded> {
        self.loaded
            .get(py)
            .ok_or_else(|| PyValueError::new_err("the graph is not loaded yet"))
    }

    fn name<'py>(
        &self,
        py: Python<'py>,
        loaded: &Loaded,
        ordinal: u32,
    ) -> PyResult<Bound<'py, PyString>> {
        made_once(py, &loaded.names[ordinal as usize], || {
            string(py, loaded.index.id(ordinal))
        })
    }

    fn edge<'py>(&self, py: Python<'py>, ordinal: u32) -> PyResult<Bound<'py, PyAny>> {
        let loaded = self.loaded(py)?;
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
            let edge = loaded.new.bind(py).call1((loaded.edge_type.bind(py),))?;
            edge.setattr(
                pyo3::intern!(py, "source"),
                self.name(py, loaded, ends.source)?,
            )?;
            edge.setattr(
                pyo3::intern!(py, "target"),
                self.name(py, loaded, ends.target)?,
            )?;
            let kind = loaded.shape.edges[ordinal as usize].kind as usize;
            edge.setattr(pyo3::intern!(py, "kind"), loaded.kinds[kind].bind(py))?;
            edge.setattr(pyo3::intern!(py, "attrs"), attrs)?;
            Ok(edge)
        })
    }

    /// The edges at the node or endpoint `node` that `adjacent` lists, of kind `kind` if given.
    fn adjacent<'py>(
        &self,
        py: Python<'py>,
        node: &Bound<'py, PyAny>,
        kind: Option<&Bound<'py, PyAny>>,
        outgoing: bool,
    ) -> PyResult<Bound<'py, PyList>> {
        let loaded = self.loaded(py)?;
        let empty = || PyList::empty(py);
        let Ok(node) = node.cast::<PyString>() else {
            return Ok(empty());
        };
        let Some(ordinal) = loaded.index.ordinal(&crate::python::key(node)?) else {
            return Ok(empty());
        };
        let wanted = match kind {
            None => None,
            Some(kind) => {
                let Ok(kind) = kind.cast::<PyString>() else {
                    return Ok(empty());
                };
                let kind = crate::python::key(kind)?;
                match loaded.shape.kinds.iter().position(|k| **k == *kind) {
                    Some(found) => Some(found as u16),
                    None => return Ok(empty()),
                }
            }
        };
        let edges = if outgoing {
            loaded.index.outgoing(ordinal)
        } else {
            loaded.index.incoming(ordinal)
        };
        let made = edges
            .iter()
            .filter(|&&e| wanted.is_none_or(|k| loaded.shape.edges[e as usize].kind == k))
            .map(|&e| self.edge(py, e))
            .collect::<PyResult<Vec<_>>>()?;
        PyList::new(py, made)
    }
}

#[pymethods]
impl GraphFile {
    /// Parses `data`.
    ///
    /// Raises `ValueError(message, offset)` when it is not JSON, with the byte offset where
    /// that first shows, and `OverflowError` when it is longer than the reader takes.
    #[new]
    fn new(py: Python<'_>, data: Bound<'_, PyBytes>) -> PyResult<Self> {
        let bytes = data.as_bytes();
        if bytes.len() > crate::json::MAX_LEN {
            return Err(pyo3::exceptions::PyOverflowError::new_err(
                "it is larger than the 2 GiB Konstruct reads",
            ));
        }
        let tape = py
            .detach(|| Tape::parse(bytes))
            .map_err(|error| PyValueError::new_err((error.message, error.at)))?;
        Ok(Self {
            bytes: data.unbind(),
            tape,
            loaded: PyOnceLock::new(),
        })
    }

    /// The whole document as Python values, as `Graph.from_dict` takes it: for a document that
    /// is not a graph as Konstruct writes one, which `load` refuses.
    fn document<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
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
    fn header<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
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

    /// The nodes, as instances of `node_type` by id, the first of each id, and the later ones
    /// with an id that came before, for `Graph.add_node` to merge, each with the number of
    /// first nodes before it; edges will be instances of `edge_type`. A file is loaded once.
    ///
    /// Raises `ValueError(message)` when the document is not a graph as Konstruct writes one:
    /// `document` gives it to `Graph.from_dict` instead.
    fn load<'py>(
        &self,
        py: Python<'py>,
        node_type: Bound<'py, PyType>,
        edge_type: Bound<'py, PyType>,
    ) -> PyResult<(Bound<'py, PyDict>, Bound<'py, PyList>)> {
        if self.loaded.get(py).is_some() {
            return Err(PyValueError::new_err("the graph is loaded already"));
        }
        let doc = self.doc(py);
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
        let new = node_type.getattr(pyo3::intern!(py, "__new__"))?;
        let nodes = PyDict::new(py);
        let later = PyList::empty(py);
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
                let node = new.call1((&node_type,))?;
                let id = PyString::new(py, &doc.text(fields.id as usize));
                node.setattr(pyo3::intern!(py, "id"), &id)?;
                node.setattr(
                    pyo3::intern!(py, "kind"),
                    convert.string(fields.kind as usize),
                )?;
                node.setattr(
                    pyo3::intern!(py, "name"),
                    convert.value(fields.name as usize)?,
                )?;
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
                node.setattr(pyo3::intern!(py, "parent"), parent)?;
                for (name, field) in [
                    (pyo3::intern!(py, "relation"), fields.relation),
                    (pyo3::intern!(py, "description"), fields.description),
                    (pyo3::intern!(py, "data_type"), fields.data_type),
                    (pyo3::intern!(py, "path"), fields.path),
                ] {
                    node.setattr(name, convert.field(field, none)?)?;
                }
                let origins = convert.field(fields.origins, |py| PyList::empty(py).into_any())?;
                node.setattr(pyo3::intern!(py, "origins"), origins)?;
                let attrs = convert.field(fields.attrs, |py| PyDict::new(py).into_any())?;
                node.setattr(pyo3::intern!(py, "attrs"), attrs)?;
                if fields.first {
                    names[ordinal]
                        .set(py, id.clone().unbind())
                        .expect("each node is made once");
                    nodes.set_item(id, node)?;
                    ordinal += 1;
                } else {
                    // With the number of nodes before it, so Python merges it where it was.
                    later.append((ordinal, node))?;
                }
            }
        }
        let kinds = shape
            .kinds
            .iter()
            .map(|kind| PyString::new(py, kind).unbind())
            .collect();
        let edges = (0..shape.edges.len()).map(|_| PyOnceLock::new()).collect();
        let loaded = Loaded {
            shape,
            index,
            edge_type: edge_type.clone().unbind(),
            new: edge_type.getattr(pyo3::intern!(py, "__new__"))?.unbind(),
            names,
            kinds,
            edges,
            shared: Mutex::new(shared),
        };
        if self.loaded.set(py, loaded).is_err() {
            return Err(PyValueError::new_err("the graph is loaded already"));
        }
        Ok((nodes, later))
    }

    /// The index of the graph's edges, for walks and paths.
    fn index(&self, py: Python<'_>) -> PyResult<GraphIndex> {
        Ok(GraphIndex::shared(Arc::clone(&self.loaded(py)?.index)))
    }

    /// The number of edges.
    fn __len__(&self, py: Python<'_>) -> PyResult<usize> {
        Ok(self.loaded(py)?.edges.len())
    }

    /// The edge with this ordinal, made when first asked for.
    fn __getitem__<'py>(&self, py: Python<'py>, ordinal: usize) -> PyResult<Bound<'py, PyAny>> {
        let count = self.loaded(py)?.edges.len();
        match u32::try_from(ordinal) {
            Ok(ordinal) if (ordinal as usize) < count => self.edge(py, ordinal),
            _ => Err(pyo3::exceptions::PyIndexError::new_err(
                "no edge with that ordinal",
            )),
        }
    }

    /// Every edge, in order.
    fn edges<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let count = self.loaded(py)?.edges.len();
        let made = (0..count)
            .map(|e| self.edge(py, graph::ordinal(e)))
            .collect::<PyResult<Vec<_>>>()?;
        PyList::new(py, made)
    }

    /// The edges leaving `node`, of kind `kind` if given, in order.
    #[pyo3(signature = (node, kind=None))]
    fn out_edges<'py>(
        &self,
        py: Python<'py>,
        node: &Bound<'py, PyAny>,
        kind: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyList>> {
        self.adjacent(py, node, kind, true)
    }

    /// The edges arriving at `node`, of kind `kind` if given, in order.
    #[pyo3(signature = (node, kind=None))]
    fn in_edges<'py>(
        &self,
        py: Python<'py>,
        node: &Bound<'py, PyAny>,
        kind: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyList>> {
        self.adjacent(py, node, kind, false)
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
