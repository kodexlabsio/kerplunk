//! `Graph`: the nodes and edges of a project and the questions asked of them.
//!
//! Nodes are Python objects in a dict, as callers read and edit them; edges are made objects
//! too, kept in the order they were added, with an index by their ends. A graph read from
//! `graph.json` keeps its edges on the file's tape until something needs them as objects (see
//! [`File`]); anything that changes the graph makes them all first.
//!
//! Walks, shortest paths and name resolution run over an index of the graph, made on first use
//! and again after the graph changes through `add_node`, `add_edge` or `remove_edge`. Code that
//! edits a node's or an edge's fields in place calls `changed()`.
//!
//! A graph is shared between the threads of the MCP server and the HTTP API, so it is `frozen`:
//! no borrow flag to trip over, and its state behind one mutex that is held only for work on
//! Rust data, never while Python runs.

use std::borrow::Cow;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};

use pyo3::exceptions::{PyKeyError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyString, PyTuple, PyType};

use crate::fnv::FnvHashMap;
use crate::graph::{self, EdgeKind, Flow, NodeKind};
use crate::names;
use crate::python::{key, string};

use super::file::{File, OpenError};
use super::node::{Edge, Node, Step};

/// The node kinds that are relations.
pub const RELATION_KINDS: [&str; 5] = ["source", "table", "model", "seed", "snapshot"];

/// When several nodes describe one physical relation (a seed and the dbt source that reads it),
/// SQL that names the relation resolves to the first kind in this order.
pub fn priority(kind: &str) -> u8 {
    match kind {
        "source" => 0,
        "model" => 1,
        "snapshot" => 2,
        "seed" => 3,
        "table" => 4,
        _ => 9,
    }
}

/// What an index is made from, so what makes it stale.
#[derive(Clone, Copy)]
enum Basis {
    Graph = 0,
    Nodes = 1,
    Relations = 2,
}

/// The `str` a field holds, as the text indexes take it, or None for anything else.
fn text<'a>(py: Python<'a>, value: &'a Py<PyAny>) -> Option<Cow<'a, str>> {
    let string = value.bind(py).cast::<PyString>().ok()?;
    key(string).ok()
}

fn is_relation_kind(py: Python<'_>, node: &Node) -> bool {
    text(py, &node.kind).is_some_and(|kind| RELATION_KINDS.contains(&&*kind))
}

fn kind_is(py: Python<'_>, node: &Node, expected: &str) -> bool {
    text(py, &node.kind).is_some_and(|kind| kind == expected)
}

/// `value.lower()`, as Python's string and as the text the indexes take.
fn lowered<'py>(value: &Bound<'py, PyAny>) -> PyResult<(Bound<'py, PyString>, String)> {
    let lower = value
        .call_method0(pyo3::intern!(value.py(), "lower"))?
        .cast_into::<PyString>()?;
    let text = key(&lower)?.into_owned();
    Ok((lower, text))
}

/// What `attrs` holds under `name`, or None: read straight from a plain dict, and asked of
/// anything else (a dict that records what is read from it, say) through its own `get`.
fn attr_get<'py>(attrs: &Bound<'py, PyAny>, name: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
    if attrs.is_exact_instance_of::<PyDict>() {
        return attrs.cast::<PyDict>()?.get_item(name);
    }
    if !attrs.hasattr(pyo3::intern!(attrs.py(), "get"))? {
        return Ok(None);
    }
    let found = attrs.call_method1(pyo3::intern!(attrs.py(), "get"), (name,))?;
    Ok((!found.is_none()).then_some(found))
}

/// Whether `value.lower()` is `needle` (itself lower case, in the text form of the indexes): an
/// ASCII string is compared in place, anything else asks Python.
fn lower_is(value: &Bound<'_, PyAny>, needle: &str) -> PyResult<bool> {
    if let Ok(text) = value.cast::<PyString>()
        && let Ok(utf8) = text.to_str()
        && utf8.is_ascii()
    {
        return Ok(utf8.eq_ignore_ascii_case(needle));
    }
    Ok(lowered(value)?.1 == needle)
}

/// A field's text, or a `TypeError` as Python raises for an attribute that is not a string.
fn required<'a>(py: Python<'a>, value: &'a Py<PyAny>, what: &str) -> PyResult<Cow<'a, str>> {
    text(py, value).ok_or_else(|| PyTypeError::new_err(format!("{what} must be a string")))
}

/// Every edge as a Python object, in order, with an index by their ends.
#[derive(Default)]
struct Made {
    endpoints: FnvHashMap<Box<str>, u32>,
    kinds: FnvHashMap<Box<str>, u32>,
    /// By edge slot: `None` for an edge removed.
    slots: Vec<Option<Py<Edge>>>,
    /// By edge slot: the ends and kind, as numbers.
    triples: Vec<(u32, u32, u32)>,
    lookup: FnvHashMap<(u32, u32, u32), u32>,
    /// By endpoint: the slots of the edges leaving and arriving, in order.
    out: Vec<Vec<u32>>,
    into: Vec<Vec<u32>>,
    live: usize,
}

impl Made {
    fn endpoint(&mut self, id: &str) -> u32 {
        if let Some(&found) = self.endpoints.get(id) {
            return found;
        }
        let number = u32::try_from(self.out.len()).expect("fewer than 2^32 endpoints");
        self.endpoints.insert(id.into(), number);
        self.out.push(Vec::new());
        self.into.push(Vec::new());
        number
    }

    fn kind(&mut self, kind: &str) -> u32 {
        let next = u32::try_from(self.kinds.len()).expect("few kinds");
        *self.kinds.entry(kind.into()).or_insert(next)
    }

    fn find(&self, source: &str, target: &str, kind: &str) -> Option<u32> {
        let triple = (
            *self.endpoints.get(source)?,
            *self.endpoints.get(target)?,
            *self.kinds.get(kind)?,
        );
        self.lookup.get(&triple).copied()
    }

    /// Adds `edge` under its ends and kind, which must not be in the graph yet.
    fn push(&mut self, py: Python<'_>, edge: Py<Edge>) -> PyResult<()> {
        let triple = {
            let this = edge.borrow(py);
            let source = required(py, &this.source, "an edge's source")?;
            let target = required(py, &this.target, "an edge's target")?;
            let kind = required(py, &this.kind, "an edge's kind")?;
            (
                self.endpoint(&source),
                self.endpoint(&target),
                self.kind(&kind),
            )
        };
        let slot = u32::try_from(self.slots.len()).expect("fewer than 2^32 edges");
        self.out[triple.0 as usize].push(slot);
        self.into[triple.1 as usize].push(slot);
        self.lookup.insert(triple, slot);
        self.triples.push(triple);
        self.slots.push(Some(edge));
        self.live += 1;
        Ok(())
    }

    fn live(&self, py: Python<'_>) -> Vec<Py<Edge>> {
        self.slots
            .iter()
            .flatten()
            .map(|edge| edge.clone_ref(py))
            .collect()
    }

    fn adjacent(
        &self,
        py: Python<'_>,
        node: &str,
        kind: Option<&str>,
        outgoing: bool,
    ) -> Vec<Py<Edge>> {
        let Some(&endpoint) = self.endpoints.get(node) else {
            return Vec::new();
        };
        let wanted = match kind {
            None => None,
            Some(kind) => match self.kinds.get(kind) {
                Some(&found) => Some(found),
                None => return Vec::new(),
            },
        };
        let slots = if outgoing {
            &self.out[endpoint as usize]
        } else {
            &self.into[endpoint as usize]
        };
        slots
            .iter()
            .filter(|&&slot| wanted.is_none_or(|k| self.triples[slot as usize].2 == k))
            .filter_map(|&slot| self.slots[slot as usize].as_ref())
            .map(|edge| edge.clone_ref(py))
            .collect()
    }

    /// Removes the edge in `slot` and returns it, for the caller to drop once the lock is let go.
    fn remove(&mut self, slot: u32) -> Option<Py<Edge>> {
        let triple = self.triples[slot as usize];
        let removed = self.slots[slot as usize].take();
        self.lookup.remove(&triple);
        self.out[triple.0 as usize].retain(|&s| s != slot);
        self.into[triple.1 as usize].retain(|&s| s != slot);
        self.live -= 1;
        removed
    }
}

/// Where the graph's edges are.
enum Edges {
    /// Still on the tape of the file the graph was read from.
    File(Arc<File>),
    Made(Made),
}

/// The relations by the name SQL gives them, and the nodes that describe each, the preferred
/// first.
#[derive(Default)]
struct Relations {
    by_key: FnvHashMap<Box<str>, usize>,
    groups: Vec<(Py<PyAny>, Vec<Py<Node>>)>,
}

/// The graph's edges in the native index, with the nodes and edges its ordinals number. It is a
/// Python object so that `Graph._indexes` lists it with the indexes Python code makes.
#[pyclass(frozen, name = "TraversalIndex", module = "konstruct._core")]
struct Traversal {
    core: Arc<graph::GraphIndex>,
    nodes: Vec<Py<Node>>,
    edges: TraversalEdges,
}

enum TraversalEdges {
    File(Arc<File>),
    Made(Vec<Py<Edge>>),
}

/// The relations by every name they answer to.
#[pyclass(frozen, name = "RelationNameIndex", module = "konstruct._core")]
struct RelationNames {
    relations: Vec<Py<Node>>,
    core: names::RelationNames,
}

struct State {
    edges: Edges,
    relations: Relations,
    meta: Py<PyAny>,
    warnings: Py<PyAny>,
    /// For each [`Basis`], a version bumped whenever that part of the graph changes.
    versions: [u64; 3],
    traversal: Option<(u64, Py<Traversal>)>,
    names: Option<(u64, Py<RelationNames>)>,
}

/// The graph.
#[pyclass(frozen, subclass, dict, weakref, module = "konstruct.core.graph")]
pub struct Graph {
    nodes: Py<PyDict>,
    indexes: Py<PyDict>,
    state: Mutex<State>,
}

impl Graph {
    fn empty(py: Python<'_>) -> Self {
        Self::with_nodes(py, PyDict::new(py))
    }

    fn with_nodes(py: Python<'_>, nodes: Bound<'_, PyDict>) -> Self {
        Self {
            nodes: nodes.unbind(),
            indexes: PyDict::new(py).unbind(),
            state: Mutex::new(State {
                edges: Edges::Made(Made::default()),
                relations: Relations::default(),
                meta: PyDict::new(py).into_any().unbind(),
                warnings: PyList::empty(py).into_any().unbind(),
                versions: [0; 3],
                traversal: None,
                names: None,
            }),
        }
    }

    fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn bump(&self, bases: &[Basis]) {
        let mut state = self.lock();
        for &basis in bases {
            state.versions[basis as usize] += 1;
        }
    }

    /// Every edge made a Python object, if they are still in the file.
    ///
    /// The MCP server and the HTTP API answer from threads, so the edges are made with nothing
    /// held, and put in place only if no other thread has put its own there first: a reader sees
    /// the file or every edge, never part of them.
    fn made(&self, py: Python<'_>) -> PyResult<()> {
        let file = match &self.lock().edges {
            Edges::File(file) => Arc::clone(file),
            Edges::Made(_) => return Ok(()),
        };
        let mut made = Made::default();
        for ordinal in 0..file.len() {
            let edge = file.edge(py, super::ordinal(ordinal))?;
            made.push(py, edge.unbind())?;
        }
        // What is replaced, or not needed, is dropped after the lock is let go: a finaliser it
        // runs may call the graph.
        let leftovers = {
            let mut state = self.lock();
            if matches!(state.edges, Edges::File(_)) {
                (
                    Some(std::mem::replace(&mut state.edges, Edges::Made(made))),
                    None,
                )
            } else {
                (None, Some(made))
            }
        };
        drop(leftovers);
        Ok(())
    }

    /// The edges as objects, in order.
    fn all_edges(&self, py: Python<'_>) -> PyResult<Vec<Py<Edge>>> {
        let file = match &self.lock().edges {
            Edges::File(file) => Some(Arc::clone(file)),
            Edges::Made(_) => None,
        };
        if let Some(file) = file {
            return (0..file.len())
                .map(|e| Ok(file.edge(py, super::ordinal(e))?.unbind()))
                .collect();
        }
        match &self.lock().edges {
            Edges::Made(made) => Ok(made.live(py)),
            Edges::File(_) => unreachable!("an edge map is never put back in a file"),
        }
    }

    /// The edges at `node` that leave it or arrive, of kind `kind` if given.
    fn adjacent(
        &self,
        py: Python<'_>,
        node: &Bound<'_, PyAny>,
        kind: Option<&Bound<'_, PyAny>>,
        outgoing: bool,
    ) -> PyResult<Vec<Py<Edge>>> {
        let Ok(node) = node.cast::<PyString>() else {
            return Ok(Vec::new());
        };
        let node = key(node)?;
        let kind = match kind {
            None => None,
            Some(kind) => match kind.cast::<PyString>() {
                Ok(kind) => Some(key(kind)?),
                Err(_) => return Ok(Vec::new()),
            },
        };
        let file = match &self.lock().edges {
            Edges::File(file) => Some(Arc::clone(file)),
            Edges::Made(made) => return Ok(made.adjacent(py, &node, kind.as_deref(), outgoing)),
        };
        let file = file.expect("a file here");
        file.adjacent(&node, kind.as_deref(), outgoing)
            .into_iter()
            .map(|e| Ok(file.edge(py, e)?.unbind()))
            .collect()
    }

    fn node(&self, py: Python<'_>, id: &Bound<'_, PyAny>) -> PyResult<Option<Py<Node>>> {
        Ok(self
            .nodes
            .bind(py)
            .get_item(id)?
            .map(|found| found.cast_into::<Node>())
            .transpose()?
            .map(Bound::unbind))
    }

    /// Every node, in order.
    fn node_list(&self, py: Python<'_>) -> PyResult<Vec<Py<Node>>> {
        self.nodes
            .bind(py)
            .values()
            .iter()
            .map(|node| Ok(node.cast_into::<Node>()?.unbind()))
            .collect()
    }

    fn index_relation(&self, py: Python<'_>, node: &Py<Node>) -> PyResult<()> {
        let (relation, name) = {
            let this = node.borrow(py);
            if !this.relation.bind(py).is_truthy()? || !is_relation_kind(py, &this) {
                return Ok(());
            }
            (
                this.relation.clone_ref(py),
                text(py, &this.relation).map(Cow::into_owned),
            )
        };
        let Some(name) = name else {
            return Ok(());
        };
        let mut state = self.lock();
        let relations = &mut state.relations;
        let index = match relations.by_key.get(name.as_str()) {
            Some(&found) => found,
            None => {
                relations.groups.push((relation, Vec::new()));
                relations
                    .by_key
                    .insert(name.into(), relations.groups.len() - 1);
                relations.groups.len() - 1
            }
        };
        let group = &mut relations.groups[index].1;
        if !group.iter().any(|known| known.is(node)) {
            group.push(node.clone_ref(py));
            group.sort_by_key(|n| text(py, &n.borrow(py).kind).map_or(9, |kind| priority(&kind)));
        }
        Ok(())
    }

    /// `add_node`: adds `node`, or merges it into the node with its id.
    fn merge_node(&self, py: Python<'_>, node: &Bound<'_, Node>) -> PyResult<Py<Node>> {
        // A kind that cannot be hashed is refused, as testing it against the set of relation
        // kinds refuses it.
        node.borrow().kind.bind(py).hash()?;
        let id = node.borrow().id.clone_ref(py);
        let existing = self.node(py, id.bind(py))?;
        {
            let relation_kind = is_relation_kind(py, &node.borrow())
                || existing
                    .as_ref()
                    .is_some_and(|e| is_relation_kind(py, &e.borrow(py)));
            let mut state = self.lock();
            state.versions[Basis::Graph as usize] += 1;
            state.versions[Basis::Nodes as usize] += 1;
            if relation_kind {
                state.versions[Basis::Relations as usize] += 1;
            }
        }
        let Some(existing) = existing else {
            self.nodes.bind(py).set_item(id.bind(py), node)?;
            let node = node.clone().unbind();
            self.index_relation(py, &node)?;
            return Ok(node);
        };
        if existing.is(node) {
            self.index_relation(py, &existing)?;
            return Ok(existing);
        }
        {
            let incoming = node.borrow();
            let mut target = existing.borrow_mut(py);
            for name in ["parent", "relation", "description", "data_type", "path"] {
                if !target.optional(name).bind(py).is_truthy()?
                    && incoming.optional(name).bind(py).is_truthy()?
                {
                    let value = incoming.optional(name).clone_ref(py);
                    target.set_optional(name, value);
                }
            }
            let origins = target.origins.bind(py).clone();
            for origin in incoming.origins.bind(py).try_iter()? {
                let origin = origin?;
                if !origins.contains(&origin)? {
                    origins.call_method1(pyo3::intern!(py, "append"), (origin,))?;
                }
            }
            let attrs = target.attrs.bind(py).clone();
            let items = incoming
                .attrs
                .bind(py)
                .call_method0(pyo3::intern!(py, "items"))?;
            for item in items.try_iter()? {
                let (name, value): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
                attrs.call_method1(pyo3::intern!(py, "setdefault"), (name, value))?;
            }
        }
        self.index_relation(py, &existing)?;
        Ok(existing)
    }

    /// The index of the graph's edges, made if the graph changed since it was.
    fn traversal(&self, py: Python<'_>) -> PyResult<Py<Traversal>> {
        {
            let state = self.lock();
            let version = state.versions[Basis::Graph as usize];
            if let Some((made, index)) = &state.traversal
                && *made == version
            {
                return Ok(index.clone_ref(py));
            }
        }
        let version = self.lock().versions[Basis::Graph as usize];
        let nodes = self.node_list(py)?;
        let mut builder = graph::GraphIndex::builder();
        for (id, node) in self.nodes.bind(py).keys().iter().zip(&nodes) {
            let id = key(id.cast::<PyString>()?)?;
            let kind = text(py, &node.borrow(py).kind)
                .map_or(NodeKind::parse(""), |k| NodeKind::parse(&k));
            builder.node(&id, kind);
        }
        // The index is made from the edges as objects, as the order of each node's adjacency
        // lists is the order they were added in.
        self.made(py)?;
        let edges = self.all_edges(py)?;
        let mut references = Vec::with_capacity(edges.len());
        let mut through: Vec<(u32, String)> = Vec::new();
        for (ordinal, edge) in edges.iter().enumerate() {
            let this = edge.borrow(py);
            let attrs = this.attrs.bind(py);
            let get = |name: &str| attr_get(attrs, name);
            references.push(
                get("role")?
                    .and_then(|role| role.cast_into::<PyString>().ok())
                    .is_some_and(|role| role.to_str().is_ok_and(|r| r == "reference")),
            );
            let ordinal = super::ordinal(ordinal);
            let mut tasks: Vec<Bound<'_, PyAny>> = Vec::new();
            if let Some(via) = get("via")? {
                tasks.push(via);
            }
            if let Some(also) = get("also_via")?
                && let Ok(list) = also.cast_into::<PyList>()
            {
                tasks.extend(list.iter());
            }
            for task in tasks {
                if let Ok(task) = task.cast_into::<PyString>() {
                    through.push((ordinal, key(&task)?.into_owned()));
                }
            }
            let source = key(required_bound(this.source.bind(py))?)?;
            let target = key(required_bound(this.target.bind(py))?)?;
            let kind = key(required_bound(this.kind.bind(py))?)?;
            builder.edge(
                &source,
                &target,
                EdgeKind::parse(&kind),
                *references.last().unwrap_or(&false),
            );
        }
        for (ordinal, node) in nodes.iter().enumerate() {
            let parent = node.borrow(py).parent.clone_ref(py);
            if !parent.bind(py).is_none() {
                let parent = parent.bind(py).cast::<PyString>()?.clone();
                builder.parent(super::ordinal(ordinal), &key(&parent)?);
            }
        }
        for (edge, task) in &through {
            builder.through(*edge, task);
        }
        let made = Py::new(
            py,
            Traversal {
                core: Arc::new(builder.build()),
                nodes,
                edges: TraversalEdges::Made(edges),
            },
        )?;
        self.indexes
            .bind(py)
            .set_item("traversal", (version, made.clone_ref(py)))?;
        let old = self.lock().traversal.replace((version, made.clone_ref(py)));
        drop(old);
        Ok(made)
    }

    /// The relations by every name they answer to.
    fn relation_names(&self, py: Python<'_>) -> PyResult<Py<RelationNames>> {
        let version = {
            let state = self.lock();
            let version = state.versions[Basis::Relations as usize];
            if let Some((made, index)) = &state.names
                && *made == version
            {
                return Ok(index.clone_ref(py));
            }
            version
        };
        let mut relations = Vec::new();
        for node in self.node_list(py)? {
            if is_relation_kind(py, &node.borrow(py)) {
                relations.push(node);
            }
        }
        let mut names_lower = Vec::with_capacity(relations.len());
        let mut ids_lower = Vec::with_capacity(relations.len());
        let mut kept = Vec::with_capacity(relations.len());
        let mut outside = Vec::with_capacity(relations.len());
        for node in &relations {
            let this = node.borrow(py);
            names_lower.push(lowered(this.name.bind(py))?.1);
            ids_lower.push(lowered(this.id.bind(py))?.1);
            kept.push(text(py, &this.relation).map(Cow::into_owned));
            let attrs = this.attrs.bind(py);
            outside.push(
                attr_get(attrs, "outside_warehouse")?
                    .map(|v| v.is_truthy())
                    .transpose()?
                    .unwrap_or(false),
            );
        }
        let core = names::RelationNames::new((0..relations.len()).map(|i| names::Relation {
            name: &names_lower[i],
            id: &ids_lower[i],
            relation: kept[i].as_deref(),
            outside_warehouse: outside[i],
        }));
        let made = Py::new(py, RelationNames { relations, core })?;
        self.indexes
            .bind(py)
            .set_item("relation names", (version, made.clone_ref(py)))?;
        let old = self.lock().names.replace((version, made.clone_ref(py)));
        drop(old);
        Ok(made)
    }

    fn steps<'py>(
        &self,
        py: Python<'py>,
        traversal: &Traversal,
        visits: Vec<graph::Visit>,
    ) -> PyResult<Bound<'py, PyList>> {
        let count = traversal.nodes.len();
        let steps = PyList::empty(py);
        for visit in visits {
            let node = traversal.nodes[visit.node as usize].clone_ref(py);
            let via = match visit.via {
                None => py.None(),
                Some(via) => match &traversal.edges {
                    TraversalEdges::File(file) => file.edge(py, via)?.into_any().unbind(),
                    TraversalEdges::Made(edges) => edges[via as usize].clone_ref(py).into_any(),
                },
            };
            let previous = match visit.previous {
                None => py.None(),
                Some(previous) if (previous as usize) < count => traversal.nodes[previous as usize]
                    .borrow(py)
                    .id
                    .clone_ref(py),
                Some(previous) => string(py, traversal.core.id(previous))?.into_any().unbind(),
            };
            let step = Step {
                node: node.into_any(),
                depth: visit.depth.into_pyobject(py)?.into_any().unbind(),
                via,
                previous,
            };
            steps.append(Py::new(py, step)?)?;
        }
        Ok(steps)
    }

    fn match_relations(&self, py: Python<'_>, needle: &str) -> PyResult<Vec<Py<Node>>> {
        let names = self.relation_names(py)?;
        let names = names.get();
        let found = names.core.find(needle);
        let matches: Vec<Py<Node>> = found
            .iter()
            .map(|&i| names.relations[i as usize].clone_ref(py))
            .collect();
        // Nodes for one physical relation (a seed and the source over it) are one answer.
        let relations: Vec<Option<String>> = matches
            .iter()
            .map(|n| text(py, &n.borrow(py).relation).map(Cow::into_owned))
            .collect();
        if matches.len() > 1
            && let Some(Some(first)) = relations.first()
            && relations
                .iter()
                .all(|r| r.as_deref() == Some(first.as_str()))
        {
            return Ok(match self.preferred(py, first)? {
                Some(preferred) => vec![preferred],
                None => matches,
            });
        }
        Ok(matches)
    }

    fn preferred(&self, py: Python<'_>, relation: &str) -> PyResult<Option<Py<Node>>> {
        let state = self.lock();
        Ok(state
            .relations
            .by_key
            .get(relation)
            .and_then(|&i| state.relations.groups[i].1.first())
            .map(|n| n.clone_ref(py)))
    }

    fn label_of<'py>(
        &self,
        py: Python<'py>,
        node: &Bound<'py, Node>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let this = node.borrow();
        let name = this.name.bind(py).clone();
        if kind_is(py, &this, "column") {
            return match self.parent(py, &this)? {
                Some(parent) => self.label_of(py, parent.bind(py))?.add(".")?.add(name),
                None => Ok(name),
            };
        }
        let attrs = this.attrs.bind(py);
        let attr = |key: &str| -> PyResult<Option<Bound<'py, PyAny>>> {
            match attr_get(attrs, key)? {
                Some(value) if value.is_truthy()? => Ok(Some(value)),
                _ => Ok(None),
            }
        };
        let prefix = if kind_is(py, &this, "source") {
            attr("source_name")?
        } else if kind_is(py, &this, "task") {
            attr("dag")?
        } else {
            None
        };
        match prefix {
            Some(prefix) => prefix.str()?.add(".")?.add(name.str()?),
            None => Ok(name),
        }
    }

    fn parent(&self, py: Python<'_>, node: &Node) -> PyResult<Option<Py<Node>>> {
        if node.parent.bind(py).is_truthy()? {
            self.node(py, node.parent.bind(py))
        } else {
            Ok(None)
        }
    }

    fn column_node(
        &self,
        py: Python<'_>,
        parent: &Bound<'_, PyAny>,
        name: &Bound<'_, PyAny>,
    ) -> PyResult<Option<Py<Node>>> {
        self.node(py, &super::column_key(parent, name)?)
    }
}

/// `value`, or `empty` when it is None.
fn or_empty<'py>(value: Bound<'py, PyAny>, empty: Bound<'py, PyAny>) -> Bound<'py, PyAny> {
    if value.is_none() { empty } else { value }
}

fn required_bound<'a, 'py>(value: &'a Bound<'py, PyAny>) -> PyResult<&'a Bound<'py, PyString>> {
    Ok(value.cast::<PyString>()?)
}

fn native_depth(max_depth: Option<i128>) -> Option<i64> {
    // Any limit beyond the signed 64-bit range is as good as none, and a negative one stops at
    // the start, as 0 does.
    max_depth.map(|depth| i64::try_from(depth.clamp(0, i128::from(i64::MAX))).unwrap_or(i64::MAX))
}

#[pymethods]
impl Graph {
    #[new]
    #[pyo3(signature = (*args, **kwargs), text_signature = "()")]
    fn new(py: Python<'_>, args: &Bound<'_, PyTuple>, kwargs: Option<&Bound<'_, PyDict>>) -> Self {
        // A subclass's `__init__` has arguments of its own; `Graph.__init__` takes none.
        let _ = (args, kwargs);
        Self::empty(py)
    }

    #[pyo3(name = "__init__", signature = (*args, **kwargs))]
    fn init(&self, args: &Bound<'_, PyTuple>, kwargs: Option<&Bound<'_, PyDict>>) -> PyResult<()> {
        if !args.is_empty() || kwargs.is_some_and(|k| !k.is_empty()) {
            return Err(pyo3::exceptions::PyTypeError::new_err(
                "Graph() takes no arguments",
            ));
        }
        Ok(())
    }

    /// The nodes by id, in the order they were added.
    #[getter]
    fn nodes<'py>(&self, py: Python<'py>) -> Bound<'py, PyDict> {
        self.nodes.bind(py).clone()
    }

    #[setter(nodes)]
    fn set_nodes(&self, py: Python<'_>, value: &Bound<'_, PyDict>) -> PyResult<()> {
        let held = self.nodes.bind(py);
        if !value.is(held) {
            let copy = value.copy()?;
            held.clear();
            held.update(copy.as_mapping())?;
        }
        self.bump(&[Basis::Graph, Basis::Nodes, Basis::Relations]);
        Ok(())
    }

    /// Notes about what the build could not read or resolve.
    #[getter]
    fn warnings(&self, py: Python<'_>) -> Py<PyAny> {
        self.lock().warnings.clone_ref(py)
    }

    #[setter(warnings)]
    fn set_warnings(&self, value: Py<PyAny>) {
        let old = std::mem::replace(&mut self.lock().warnings, value);
        drop(old);
    }

    /// What the build records about the project: its name, the inputs it read and so on.
    #[getter]
    fn meta(&self, py: Python<'_>) -> Py<PyAny> {
        self.lock().meta.clone_ref(py)
    }

    #[setter(meta)]
    fn set_meta(&self, value: Py<PyAny>) {
        let old = std::mem::replace(&mut self.lock().meta, value);
        drop(old);
    }

    /// For each basis an index can have, a version bumped whenever that part of the graph
    /// changes; each cached index records the version it was made from.
    #[getter(_versions)]
    fn versions<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let versions = self.lock().versions;
        let found = PyDict::new(py);
        for (name, version) in ["graph", "nodes", "relations"].into_iter().zip(versions) {
            found.set_item(name, version)?;
        }
        Ok(found)
    }

    /// Whether the edges are still in the file the graph was read from, not yet Python objects.
    #[getter(_in_file)]
    fn in_file(&self) -> bool {
        matches!(self.lock().edges, Edges::File(_))
    }

    #[getter(_indexes)]
    fn indexes<'py>(&self, py: Python<'py>) -> Bound<'py, PyDict> {
        self.indexes.bind(py).clone()
    }

    // Construction

    /// Add a node, or merge it into an existing node with the same id.
    fn add_node(&self, py: Python<'_>, node: &Bound<'_, Node>) -> PyResult<Py<Node>> {
        self.merge_node(py, node)
    }

    #[pyo3(signature = (parent, name, *, data_type=None, description=None, origin=None, attrs=None))]
    #[allow(clippy::too_many_arguments)]
    fn add_column(
        slf: &Bound<'_, Self>,
        parent: &Bound<'_, Node>,
        name: &Bound<'_, PyString>,
        data_type: Option<Py<PyAny>>,
        description: Option<Py<PyAny>>,
        origin: Option<Py<PyAny>>,
        attrs: Option<Bound<'_, PyAny>>,
    ) -> PyResult<Py<Node>> {
        let py = slf.py();
        let this = slf.get();
        let parent_id = parent.borrow().id.clone_ref(py);
        let id = super::column_key(parent_id.bind(py), name.as_any())?;
        let origins = PyList::empty(py);
        if let Some(origin) = origin
            && origin.bind(py).is_truthy()?
        {
            origins.append(origin)?;
        }
        let attrs = match attrs {
            Some(attrs) if attrs.is_truthy()? => attrs.unbind(),
            _ => PyDict::new(py).into_any().unbind(),
        };
        let node = Bound::new(
            py,
            Node {
                id: id.unbind(),
                kind: PyString::new(py, "column").into_any().unbind(),
                name: name.clone().into_any().unbind(),
                parent: parent_id.clone_ref(py),
                relation: py.None(),
                description: description.unwrap_or_else(|| py.None()),
                data_type: data_type.unwrap_or_else(|| py.None()),
                path: py.None(),
                origins: origins.into_any().unbind(),
                attrs,
            },
        )?;
        if !Self::is_exact(slf) {
            // A subclass that overrides add_node or add_edge is asked, as it always was.
            let node = slf
                .call_method1("add_node", (node,))?
                .cast_into::<Node>()?
                .unbind();
            let id = node.borrow(py).id.clone_ref(py);
            slf.call_method1("add_edge", (parent_id, id, "contains"))?;
            return Ok(node);
        }
        let node = this.merge_node(py, &node)?;
        this.add_edge(
            py,
            parent_id.bind(py).cast::<PyString>()?,
            node.borrow(py).id.bind(py).cast::<PyString>()?,
            PyString::new(py, "contains").as_any(),
            None,
        )?;
        Ok(node)
    }

    /// Add an edge, or merge `attrs` into an existing one of the same kind between the same
    /// nodes. Self-loops are ignored and return None.
    #[pyo3(signature = (source, target, kind, **attrs))]
    fn add_edge(
        &self,
        py: Python<'_>,
        source: &Bound<'_, PyString>,
        target: &Bound<'_, PyString>,
        kind: &Bound<'_, PyAny>,
        attrs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Option<Py<Edge>>> {
        if source.as_any().eq(target)? {
            return Ok(None);
        }
        let kind = kind.cast::<PyString>()?;
        self.made(py)?;
        self.bump(&[Basis::Graph]);
        let (source_key, target_key, kind_key) = (key(source)?, key(target)?, key(kind)?);
        let existing = match &self.lock().edges {
            Edges::Made(made) => made
                .find(&source_key, &target_key, &kind_key)
                .and_then(|slot| made.slots[slot as usize].as_ref())
                .map(|edge| edge.clone_ref(py)),
            Edges::File(_) => unreachable!("made above"),
        };
        if let Some(existing) = existing {
            if let Some(attrs) = attrs {
                let held = existing.borrow(py).attrs.bind(py).clone();
                for (name, value) in attrs.iter() {
                    held.call_method1(pyo3::intern!(py, "setdefault"), (name, value))?;
                }
            }
            return Ok(Some(existing));
        }
        let held = match attrs {
            Some(attrs) => attrs.copy()?,
            None => PyDict::new(py),
        };
        let edge = Py::new(
            py,
            Edge {
                source: source.clone().into_any().unbind(),
                target: target.clone().into_any().unbind(),
                kind: kind.clone().into_any().unbind(),
                attrs: held.into_any().unbind(),
            },
        )?;
        match &mut self.lock().edges {
            Edges::Made(made) => made.push(py, edge.clone_ref(py))?,
            Edges::File(_) => unreachable!("made above"),
        }
        Ok(Some(edge))
    }

    fn remove_edge(&self, py: Python<'_>, edge: &Bound<'_, Edge>) -> PyResult<()> {
        self.made(py)?;
        let (source, target, kind) = {
            let this = edge.borrow();
            (
                this.source.bind(py).cast::<PyString>()?.clone(),
                this.target.bind(py).cast::<PyString>()?.clone(),
                this.kind.bind(py).cast::<PyString>()?.clone(),
            )
        };
        let removed = {
            let mut state = self.lock();
            let Edges::Made(made) = &mut state.edges else {
                unreachable!("made above");
            };
            let removed = match made.find(&key(&source)?, &key(&target)?, &key(&kind)?) {
                Some(slot) => made.remove(slot),
                None => None,
            };
            if removed.is_some() {
                state.versions[Basis::Graph as usize] += 1;
            }
            removed
        };
        drop(removed);
        Ok(())
    }

    /// Note that nodes or edges were edited in place (a name, a relation key, an edge's
    /// `role`), so the indexes queries use are made again.
    fn changed(&self) {
        self.bump(&[Basis::Graph, Basis::Nodes, Basis::Relations]);
    }

    /// The index `name` that `build` makes from the graph, kept until what it is made from
    /// changes: anything in the graph (`'graph'`), a node (`'nodes'`, for an index that reads
    /// no edges) or a relation (`'relations'`).
    #[pyo3(signature = (name, build, *, basis="graph"))]
    fn indexed<'py>(
        slf: &Bound<'py, Self>,
        name: &str,
        build: &Bound<'py, PyAny>,
        basis: &str,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = slf.py();
        let this = slf.get();
        let basis = match basis {
            "graph" => Basis::Graph,
            "nodes" => Basis::Nodes,
            "relations" => Basis::Relations,
            other => return Err(PyKeyError::new_err(other.to_owned())),
        };
        let version = this.lock().versions[basis as usize];
        if let Some(cached) = this.indexes.bind(py).get_item(name)? {
            let (made, value): (u64, Bound<'py, PyAny>) = cached.extract()?;
            if made == version {
                return Ok(value);
            }
        }
        let value = build.call1((slf,))?;
        this.indexes
            .bind(py)
            .set_item(name, (version, value.clone()))?;
        Ok(value)
    }

    fn warn(&self, py: Python<'_>, message: &Bound<'_, PyAny>) -> PyResult<()> {
        let warnings = self.lock().warnings.clone_ref(py);
        let warnings = warnings.bind(py);
        if !warnings.contains(message)? {
            warnings.call_method1(pyo3::intern!(py, "append"), (message,))?;
        }
        Ok(())
    }

    // Lookup

    /// Every edge, in the order they were added.
    #[getter]
    fn edges<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        self.made(py)?;
        PyList::new(py, self.all_edges(py)?)
    }

    /// Edges leaving `node_id`, optionally only of one kind.
    #[pyo3(signature = (node_id, kind=None))]
    fn out_edges<'py>(
        &self,
        py: Python<'py>,
        node_id: &Bound<'py, PyAny>,
        kind: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyList>> {
        PyList::new(py, self.adjacent(py, node_id, kind, true)?)
    }

    /// Edges arriving at `node_id`, optionally only of one kind.
    #[pyo3(signature = (node_id, kind=None))]
    fn in_edges<'py>(
        &self,
        py: Python<'py>,
        node_id: &Bound<'py, PyAny>,
        kind: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyList>> {
        PyList::new(py, self.adjacent(py, node_id, kind, false)?)
    }

    /// The node SQL naming this relation refers to (see `RELATION_PRIORITY`).
    fn node_for_relation(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyString>,
    ) -> PyResult<Option<Py<Node>>> {
        self.preferred(py, &lowered(key.as_any())?.1)
    }

    /// Every node that describes this relation, the preferred one first.
    fn nodes_for_relation<'py>(
        &self,
        py: Python<'py>,
        key: &Bound<'py, PyString>,
    ) -> PyResult<Bound<'py, PyList>> {
        let wanted = lowered(key.as_any())?.1;
        let nodes: Vec<Py<Node>> = {
            let state = self.lock();
            state
                .relations
                .by_key
                .get(wanted.as_str())
                .map(|&i| {
                    state.relations.groups[i]
                        .1
                        .iter()
                        .map(|n| n.clone_ref(py))
                        .collect()
                })
                .unwrap_or_default()
        };
        PyList::new(py, nodes)
    }

    /// The relation a name in canonical text stands for, whether the relation itself or one
    /// reading of it (see `split_reading`).
    fn node_for_reading(&self, py: Python<'_>, name: &str) -> PyResult<Option<Py<Node>>> {
        let key = PyString::new(py, &super::split(name).0);
        self.node_for_relation(py, &key)
    }

    fn shared_relations<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let shared: Vec<(Py<PyAny>, Vec<Py<Node>>)> = self
            .lock()
            .relations
            .groups
            .iter()
            .filter(|(_, nodes)| nodes.len() > 1)
            .map(|(key, nodes)| {
                (
                    key.clone_ref(py),
                    nodes.iter().map(|n| n.clone_ref(py)).collect(),
                )
            })
            .collect();
        let items = shared
            .into_iter()
            .map(|(key, nodes)| Ok((key, PyList::new(py, nodes)?)))
            .collect::<PyResult<Vec<_>>>()?;
        PyList::new(py, items)?.try_iter().map(Bound::into_any)
    }

    fn relations<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let mut found = Vec::new();
        for node in self.node_list(py)? {
            if is_relation_kind(py, &node.borrow(py)) {
                found.push(node);
            }
        }
        PyList::new(py, found)?.try_iter().map(Bound::into_any)
    }

    /// The relations whose columns in the graph may not be all they have: the build marks a
    /// relation `columns_guessed` unless the warehouse or a dbt catalog lists its columns, or
    /// SQL that writes it selects them without a star it cannot expand.
    fn guessed_relations<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let found = pyo3::types::PySet::empty(py)?;
        for node in self.node_list(py)? {
            let this = node.borrow(py);
            if !is_relation_kind(py, &this) || !this.relation.bind(py).is_truthy()? {
                continue;
            }
            let guessed = attr_get(this.attrs.bind(py), "columns_guessed")?
                .map(|v| v.is_truthy())
                .transpose()?
                .unwrap_or(false);
            if guessed {
                found.add(this.relation.bind(py))?;
            }
        }
        py.import(pyo3::intern!(py, "builtins"))?
            .getattr(pyo3::intern!(py, "sorted"))?
            .call1((found,))
    }

    /// Whether something in the graph builds `relation`: a model, seed or snapshot, a file of
    /// its own, a task, or SQL that gives its columns expressions. A source, or a table the
    /// graph only reads, is not built.
    fn built(&self, py: Python<'_>, relation: &Bound<'_, Node>) -> PyResult<bool> {
        let this = relation.borrow();
        if !is_relation_kind(py, &this) || kind_is(py, &this, "source") {
            return Ok(false);
        }
        if ["model", "seed", "snapshot"]
            .iter()
            .any(|kind| kind_is(py, &this, kind))
            || this.path.bind(py).is_truthy()?
        {
            return Ok(true);
        }
        let id = this.id.clone_ref(py);
        for edge in self.adjacent(
            py,
            id.bind(py),
            Some(PyString::new(py, "feeds").as_any()),
            false,
        )? {
            let source = edge.borrow(py).source.clone_ref(py);
            if let Some(node) = self.node(py, source.bind(py))?
                && kind_is(py, &node.borrow(py), "task")
            {
                return Ok(true);
            }
        }
        for column in self.columns(py, id.bind(py))? {
            let attrs = column.borrow(py).attrs.clone_ref(py);
            if attrs.bind(py).contains("expression")? {
                return Ok(true);
            }
        }
        Ok(false)
    }

    /// The columns of a relation, in their order.
    fn columns_of<'py>(
        &self,
        py: Python<'py>,
        node_id: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyList>> {
        PyList::new(py, self.columns(py, node_id)?)
    }

    /// The column `name` of the relation `parent_id`, or None.
    fn column(
        &self,
        py: Python<'_>,
        parent_id: &Bound<'_, PyAny>,
        name: &Bound<'_, PyAny>,
    ) -> PyResult<Option<Py<Node>>> {
        self.column_node(py, parent_id, name)
    }

    /// A column's relation, or None for anything else.
    fn parent_of(&self, py: Python<'_>, node: &Bound<'_, Node>) -> PyResult<Option<Py<Node>>> {
        self.parent(py, &node.borrow())
    }

    /// Short human label: `orders.amount` for a column, `orders` for a relation.
    fn label<'py>(&self, py: Python<'py>, node: &Bound<'py, Node>) -> PyResult<Bound<'py, PyAny>> {
        self.label_of(py, node)
    }

    // Name resolution

    /// Find the nodes a user reference such as `orders.amount` or `stg_orders` names.
    fn resolve<'py>(
        &self,
        py: Python<'py>,
        r#ref: &Bound<'py, PyString>,
    ) -> PyResult<Bound<'py, PyList>> {
        if let Some(found) = self.nodes.bind(py).get_item(r#ref)? {
            return PyList::new(py, [found]);
        }
        let stripped = r#ref.call_method0(pyo3::intern!(py, "strip"))?;
        let (needle_py, needle) = lowered(&stripped)?;
        if needle_py.len()? == 0 {
            return Ok(PyList::empty(py));
        }
        let relations = self.match_relations(py, &needle)?;
        if !relations.is_empty() {
            return PyList::new(py, relations);
        }
        let parts: Vec<&str> = needle.split('.').collect();
        // Try every split into relation and column, the longest relation first, so that nested
        // columns (orders.customer.country) resolve as well as qualified tables.
        for cut in (1..parts.len()).rev() {
            let (relation, column) = (parts[..cut].join("."), parts[cut..].join("."));
            let column = string(py, &column)?;
            let mut columns = Vec::new();
            for relation in self.match_relations(py, &relation)? {
                let id = relation.borrow(py).id.clone_ref(py);
                if let Some(column) = self.column_node(py, id.bind(py), column.as_any())? {
                    columns.push(column);
                }
            }
            if !columns.is_empty() {
                return PyList::new(py, columns);
            }
        }
        let nodes = self.node_list(py)?;
        let mut others = Vec::new();
        for node in &nodes {
            let bound = node.bind(py);
            let this = bound.borrow();
            if is_relation_kind(py, &this) || kind_is(py, &this, "column") {
                continue;
            }
            // A label is the name unless a source or a task has a prefix.
            let prefixed = kind_is(py, &this, "source") || kind_is(py, &this, "task");
            let mut hit = lower_is(this.name.bind(py), &needle)?
                || (prefixed && lower_is(&self.label_of(py, bound)?, &needle)?);
            if !hit && kind_is(py, &this, "term") {
                let attrs = this.attrs.bind(py);
                if let Some(synonyms) = attr_get(attrs, "synonyms")? {
                    for synonym in synonyms.try_iter()? {
                        if lower_is(&synonym?, &needle)? {
                            hit = true;
                            break;
                        }
                    }
                }
            }
            if hit {
                others.push(node.clone_ref(py));
            }
        }
        if !others.is_empty() {
            return PyList::new(py, others);
        }
        let mut by_id = Vec::new();
        for node in &nodes {
            if lower_is(node.borrow(py).id.bind(py), &needle)? {
                by_id.push(node.clone_ref(py));
            }
        }
        PyList::new(py, by_id)
    }

    // Traversal

    /// Edges along which a change to `node_id` propagates, with the node they reach: `feeds`
    /// forwards, and `derived_from`, `tests` and `runs` backwards.
    fn downstream_edges<'py>(
        &self,
        py: Python<'py>,
        node_id: &Bound<'py, PyString>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.neighbours(py, node_id, true)
    }

    /// Edges along which `node_id` depends on something, with the node they reach: `feeds` and
    /// `runs` backwards, and `derived_from` forwards.
    fn upstream_edges<'py>(
        &self,
        py: Python<'py>,
        node_id: &Bound<'py, PyString>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.neighbours(py, node_id, false)
    }

    /// A relation stands for itself and all its columns; anything else for itself.
    fn expand<'py>(
        &self,
        py: Python<'py>,
        node: &Bound<'py, Node>,
    ) -> PyResult<Bound<'py, PyList>> {
        let this = node.borrow();
        let id = this.id.clone_ref(py);
        let mut ids = vec![id.clone_ref(py)];
        if is_relation_kind(py, &this) {
            for column in self.columns(py, id.bind(py))? {
                ids.push(column.borrow(py).id.clone_ref(py));
            }
        }
        PyList::new(py, ids)
    }

    /// Everything downstream of `node`.
    ///
    /// A column reached through a join, filter or group-by (`role = "reference"`) is reported,
    /// but the walk does not continue from the whole relation it reached, so one join key does
    /// not mark every downstream column as affected. For a column, the exposures that use and
    /// the tasks that read or run each affected relation are reported too, and so are the tasks
    /// whose SQL an edge on the way came from (an Airflow SQL task, a Dagster asset). Tests and
    /// the tasks that run a node are reported, not walked through.
    #[pyo3(signature = (node, max_depth=None))]
    fn impact<'py>(
        &self,
        py: Python<'py>,
        node: &Bound<'py, Node>,
        max_depth: Option<i128>,
    ) -> PyResult<Bound<'py, PyList>> {
        self.walk(py, node, max_depth, true)
    }

    /// Everything upstream of `node`. For a column, the tasks that build each relation on the
    /// way (including the column's own) are reported too.
    #[pyo3(signature = (node, max_depth=None))]
    fn trace<'py>(
        &self,
        py: Python<'py>,
        node: &Bound<'py, Node>,
        max_depth: Option<i128>,
    ) -> PyResult<Bound<'py, PyList>> {
        self.walk(py, node, max_depth, false)
    }

    /// Whether `consumer` (a Looker view, say) records which of `relation`'s columns it reads,
    /// so a column reaches it only through its own edge, and walks do not pass it every column
    /// of `relation`.
    fn fed_by_columns_of(
        &self,
        py: Python<'_>,
        consumer: &Bound<'_, PyString>,
        relation: &Bound<'_, PyString>,
    ) -> PyResult<bool> {
        let traversal = self.traversal(py)?;
        let traversal = traversal.get();
        let (consumer, relation) = (key(consumer)?, key(relation)?);
        Ok(
            match (
                traversal.core.node(&consumer),
                traversal.core.node(&relation),
            ) {
                (Some(consumer), Some(relation)) => {
                    traversal.core.fed_by_columns_of(consumer, relation)
                }
                _ => false,
            },
        )
    }

    /// Shortest path from `start` to `end`, following data flow in either direction: downstream
    /// if there is a path that way, else upstream, else either way through `contains` and
    /// `precedes` too. Of several paths of the same length, each step takes the neighbour whose
    /// id sorts first, so the same path is found whatever order the build added the edges in.
    fn path<'py>(
        &self,
        py: Python<'py>,
        start: &Bound<'py, Node>,
        end: &Bound<'py, Node>,
    ) -> PyResult<Bound<'py, PyList>> {
        let traversal = self.traversal(py)?;
        let traversal = traversal.get();
        let (start, end) = (
            start.borrow().id.clone_ref(py),
            end.borrow().id.clone_ref(py),
        );
        let (start, end) = (
            key(start.bind(py).cast::<PyString>()?)?.into_owned(),
            key(end.bind(py).cast::<PyString>()?)?.into_owned(),
        );
        let visits = match (traversal.core.node(&start), traversal.core.node(&end)) {
            (Some(start), Some(end)) => traversal.core.path(start, end),
            _ => Vec::new(),
        };
        self.steps(py, traversal, visits)
    }

    // Serialisation

    /// The graph as plain data: `format_version`, `meta`, `nodes`, `edges` and `warnings`.
    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let data = PyDict::new(py);
        data.set_item("format_version", crate::load::GRAPH_FORMAT_VERSION)?;
        data.set_item("meta", self.lock().meta.clone_ref(py))?;
        let mut nodes = self.sorted_nodes(py)?;
        let nodes_data = PyList::empty(py);
        for node in nodes.drain(..) {
            nodes_data.append(node.bind(py).call_method0(pyo3::intern!(py, "to_dict"))?)?;
        }
        data.set_item("nodes", nodes_data)?;
        let edges = PyList::empty(py);
        self.made(py)?;
        for edge in self.all_edges(py)? {
            edges.append(edge.bind(py).call_method0(pyo3::intern!(py, "to_dict"))?)?;
        }
        data.set_item("edges", edges)?;
        data.set_item("warnings", self.lock().warnings.clone_ref(py))?;
        Ok(data)
    }

    /// The graph in `data`, as `to_dict` writes it in this or any earlier format.
    ///
    /// Raises:
    ///     konstruct.FormatError: A newer Konstruct wrote `data` (`source` names it).
    #[classmethod]
    #[pyo3(signature = (data, source="the graph"))]
    fn from_dict(
        cls: &Bound<'_, PyType>,
        data: &Bound<'_, PyAny>,
        source: &str,
    ) -> PyResult<Py<Self>> {
        if Self::is_subclass(cls) {
            return Self::from_data_for(cls, data, source);
        }
        Self::from_data(data.py(), data, source)
    }

    /// Write the graph to `path` as JSON (`graph.json`).
    fn write(slf: &Bound<'_, Self>, path: PathBuf) -> PyResult<()> {
        let py = slf.py();
        let bytes = if Self::overrides_to_dict(slf)? {
            // A subclass of the graph, of nodes or of edges may write its own `to_dict`, and
            // graph.json says what `to_dict` says.
            let document = slf.call_method0("to_dict")?;
            let mut encoder = super::encode::Encoder::new();
            encoder.value(&document, 0)?;
            encoder.out.push(b'\n');
            encoder.out
        } else {
            slf.get().json(py)?
        };
        py.detach(|| crate::write::files::write_whole(&path, &bytes))
            .map_err(|error| crate::write::files::os_error(py, &error, &path))
    }

    /// Load a graph from a `graph.json` file, written by this or any earlier Konstruct.
    ///
    /// Raises:
    ///     json.JSONDecodeError: The file is not JSON; the message says where and why.
    ///     konstruct.FormatError: A newer Konstruct wrote it, it is not a graph, or it is larger
    ///         than 2 GiB.
    #[classmethod]
    fn read(cls: &Bound<'_, PyType>, path: PathBuf) -> PyResult<Py<Self>> {
        let py = cls.py();
        let data = py
            .detach(|| std::fs::read(&path))
            .map_err(|error| crate::write::files::os_error(py, &error, &path))?;
        let source = path.to_string_lossy().into_owned();
        Self::from_bytes(cls, PyBytes::new(py, &data), &source)
    }

    /// The graph in `data`, the bytes of a `graph.json`, as `read` reads a file.
    ///
    /// Raises:
    ///     json.JSONDecodeError: `data` is not JSON; the message says where and why.
    ///     konstruct.FormatError: A newer Konstruct wrote it, it is not a graph, or it is larger
    ///         than 2 GiB (`source` names it).
    #[classmethod]
    #[pyo3(signature = (data, source="the graph"))]
    fn from_json(
        cls: &Bound<'_, PyType>,
        data: Bound<'_, PyBytes>,
        source: &str,
    ) -> PyResult<Py<Self>> {
        Self::from_bytes(cls, data, source)
    }

    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let counts = PyDict::new(py);
        let bump = |name: Bound<'py, PyAny>| -> PyResult<()> {
            let now: i64 = match counts.get_item(&name)? {
                Some(n) => n.extract()?,
                None => 0,
            };
            counts.set_item(name, now + 1)
        };
        for node in self.node_list(py)? {
            bump(node.borrow(py).kind.bind(py).clone())?;
        }
        for edge in self.all_edges(py)? {
            let kind = edge.borrow(py).kind.bind(py).str()?.to_string();
            bump(PyString::new(py, &format!("edge:{kind}")).into_any())?;
        }
        Ok(counts)
    }

    /// What `copy` and `pickle` keep: every edge as a Python object, and no index, as each is
    /// made again when a query needs it.
    fn __reduce__<'py>(
        slf: &Bound<'py, Self>,
    ) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyTuple>, Bound<'py, PyDict>)> {
        let py = slf.py();
        let this = slf.get();
        let state = PyDict::new(py);
        state.set_item("nodes", this.nodes.bind(py))?;
        state.set_item("edges", this.edges(py)?)?;
        {
            let held = this.lock();
            state.set_item("meta", held.meta.bind(py))?;
            state.set_item("warnings", held.warnings.bind(py))?;
        }
        // The instance's own attributes go with it, as a pickle of its `__dict__` kept them.
        if let Ok(own) = slf.getattr("__dict__")
            && let Ok(own) = own.cast::<PyDict>()
        {
            state.update(own.as_mapping())?;
        }
        let new = py.import("copyreg")?.getattr("__newobj__")?;
        Ok((new, PyTuple::new(py, [slf.get_type().into_any()])?, state))
    }

    fn __setstate__(slf: &Bound<'_, Self>, state: &Bound<'_, PyDict>) -> PyResult<()> {
        let py = slf.py();
        let this = slf.get();
        let nodes = this.nodes.bind(py);
        nodes.clear();
        let mut relations = Relations::default();
        std::mem::swap(&mut this.lock().relations, &mut relations);
        if let Some(found) = state.get_item("nodes")? {
            for (id, node) in found.cast::<PyDict>()?.iter() {
                nodes.set_item(id, &node)?;
                this.index_relation(py, &node.cast_into::<Node>()?.unbind())?;
            }
        }
        let mut made = Made::default();
        // A pickle made before the graph was native held its edges as a dict by key (`_edges`).
        let edges = match state.get_item("edges")? {
            Some(found) => Some(found),
            None => match state.get_item("_edges")? {
                Some(found) => Some(found.cast::<PyDict>()?.values().into_any()),
                None => None,
            },
        };
        if let Some(found) = edges {
            for edge in found.try_iter()? {
                made.push(py, edge?.cast_into::<Edge>()?.unbind())?;
            }
        }
        let meta = state.get_item("meta")?.map(Bound::unbind);
        let warnings = state.get_item("warnings")?.map(Bound::unbind);
        // What is replaced is dropped after the lock is let go.
        let replaced = {
            let mut held = this.lock();
            let edges = std::mem::replace(&mut held.edges, Edges::Made(made));
            let meta = meta.map(|m| std::mem::replace(&mut held.meta, m));
            let warnings = warnings.map(|w| std::mem::replace(&mut held.warnings, w));
            for version in &mut held.versions {
                *version += 1;
            }
            (edges, meta, warnings)
        };
        drop(replaced);
        const KNOWN: [&str; 10] = [
            "nodes",
            "edges",
            "meta",
            "warnings",
            "_edges",
            "_out",
            "_in",
            "_relations",
            "_versions",
            "_indexes",
        ];
        for (name, value) in state.iter() {
            let name = name.cast_into::<PyString>()?;
            if !KNOWN.contains(&name.to_str()?) && name.to_str()? != "_file" {
                slf.setattr(name, value)?;
            }
        }
        Ok(())
    }
}

impl Graph {
    /// Whether `slf` is a `Graph` itself, not an instance of a subclass.
    fn is_exact(slf: &Bound<'_, Self>) -> bool {
        slf.get_type().is(slf.py().get_type::<Self>())
    }

    /// Whether the graph, a node or an edge is of a subclass, which may have its own `to_dict`.
    fn overrides_to_dict(slf: &Bound<'_, Self>) -> PyResult<bool> {
        let py = slf.py();
        if !Self::is_exact(slf) {
            return Ok(true);
        }
        let this = slf.get();
        for node in this.nodes.bind(py).values().iter() {
            if !node.is_exact_instance_of::<Node>() {
                return Ok(true);
            }
        }
        for edge in this.all_edges(py)? {
            if !edge.bind(py).is_exact_instance_of::<Edge>() {
                return Ok(true);
            }
        }
        Ok(false)
    }

    /// Whether `cls` is a subclass of `Graph`, not `Graph` itself.
    fn is_subclass(cls: &Bound<'_, PyType>) -> bool {
        !cls.is(cls.py().get_type::<Self>())
    }

    /// The graph in `data`, made by the subclass `cls` as `from_dict` always made it: an empty
    /// one, then `add_node` and `add_edge` for each node and edge, so that a subclass that
    /// overrides them is asked.
    fn from_data_for(
        cls: &Bound<'_, PyType>,
        data: &Bound<'_, PyAny>,
        source: &str,
    ) -> PyResult<Py<Self>> {
        let py = cls.py();
        super::check_format(py, data, source)?;
        let graph = cls.call0()?;
        let get = |name: &str| -> PyResult<Bound<'_, PyAny>> {
            data.call_method1(pyo3::intern!(py, "get"), (name, py.None()))
        };
        let builtins = py.import(pyo3::intern!(py, "builtins"))?;
        let dict = builtins.getattr(pyo3::intern!(py, "dict"))?;
        let list = builtins.getattr(pyo3::intern!(py, "list"))?;
        graph.setattr(
            "meta",
            dict.call1((or_empty(get("meta")?, PyDict::new(py).into_any()),))?,
        )?;
        let exported = py.import("konstruct.core.graph")?;
        let (node_type, edge_type) = (exported.getattr("Node")?, exported.getattr("Edge")?);
        for item in or_empty(get("nodes")?, PyList::empty(py).into_any()).try_iter()? {
            let node = node_type.call_method1(pyo3::intern!(py, "from_dict"), (item?,))?;
            graph.call_method1("add_node", (node,))?;
        }
        for item in or_empty(get("edges")?, PyList::empty(py).into_any()).try_iter()? {
            let edge = edge_type.call_method1(pyo3::intern!(py, "from_dict"), (item?,))?;
            let attrs = edge.getattr("attrs")?;
            graph.call_method(
                "add_edge",
                (
                    edge.getattr("source")?,
                    edge.getattr("target")?,
                    edge.getattr("kind")?,
                ),
                attrs.cast::<PyDict>().ok(),
            )?;
        }
        graph.setattr(
            "warnings",
            list.call1((or_empty(get("warnings")?, PyList::empty(py).into_any()),))?,
        )?;
        Ok(graph.cast_into::<Self>()?.unbind())
    }

    fn columns(&self, py: Python<'_>, node_id: &Bound<'_, PyAny>) -> PyResult<Vec<Py<Node>>> {
        let contains = PyString::new(py, "contains");
        let mut columns = Vec::new();
        for edge in self.adjacent(py, node_id, Some(contains.as_any()), true)? {
            let target = edge.borrow(py).target.clone_ref(py);
            match self.node(py, target.bind(py))? {
                Some(node) => columns.push(node),
                None => return Err(PyKeyError::new_err(target)),
            }
        }
        Ok(columns)
    }

    fn neighbours<'py>(
        &self,
        py: Python<'py>,
        node_id: &Bound<'py, PyString>,
        downstream: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let traversal = self.traversal(py)?;
        let traversal = traversal.get();
        let ordinals: Vec<u32> = match traversal.core.node(&key(node_id)?) {
            Some(node) => traversal
                .core
                .neighbours(
                    node,
                    if downstream {
                        Flow::Downstream
                    } else {
                        Flow::Upstream
                    },
                )
                .map(|(edge, _)| edge)
                .collect(),
            None => Vec::new(),
        };
        let mut pairs = Vec::with_capacity(ordinals.len());
        for ordinal in ordinals {
            let edge = match &traversal.edges {
                TraversalEdges::File(file) => file.edge(py, ordinal)?,
                TraversalEdges::Made(edges) => edges[ordinal as usize].bind(py).clone(),
            };
            let this = edge.borrow();
            let other = if this.source.bind(py).eq(node_id)? {
                this.target.clone_ref(py)
            } else {
                this.source.clone_ref(py)
            };
            drop(this);
            pairs.push((edge, other));
        }
        PyList::new(py, pairs)?.try_iter().map(Bound::into_any)
    }

    fn walk<'py>(
        &self,
        py: Python<'py>,
        node: &Bound<'py, Node>,
        max_depth: Option<i128>,
        impact: bool,
    ) -> PyResult<Bound<'py, PyList>> {
        let traversal = self.traversal(py)?;
        let traversal = traversal.get();
        let starts: Vec<String> = self
            .expand(py, node)?
            .iter()
            .map(|id| Ok(key(id.cast::<PyString>()?)?.into_owned()))
            .collect::<PyResult<_>>()?;
        let column = kind_is(py, &node.borrow(), "column");
        let ordinals: Vec<u32> = starts
            .iter()
            .filter_map(|id| traversal.core.node(id))
            .collect();
        let depth = native_depth(max_depth);
        let core = Arc::clone(&traversal.core);
        let visits = py.detach(|| {
            if impact {
                core.impact(&ordinals, depth, column)
            } else {
                core.trace(&ordinals, depth, column)
            }
        });
        self.steps(py, traversal, visits)
    }

    fn sorted_nodes(&self, py: Python<'_>) -> PyResult<Vec<Py<Node>>> {
        let nodes = self.node_list(py)?;
        let mut keyed = Vec::with_capacity(nodes.len());
        for node in &nodes {
            let id = node.borrow(py).id.clone_ref(py);
            match id.bind(py).cast::<PyString>() {
                Ok(text) => keyed.push((key(text)?.into_owned(), node.clone_ref(py))),
                Err(_) => {
                    // An id that is not a string (a hand-edited document): Python sorts them.
                    let options = PyDict::new(py);
                    options.set_item(
                        "key",
                        py.import("operator")?
                            .getattr("attrgetter")?
                            .call1(("id",))?,
                    )?;
                    let sorted = py.import("builtins")?.getattr("sorted")?.call(
                        (PyList::new(py, nodes.iter().map(|n| n.clone_ref(py)))?,),
                        Some(&options),
                    )?;
                    return sorted
                        .try_iter()?
                        .map(|n| Ok(n?.cast_into::<Node>()?.unbind()))
                        .collect();
                }
            }
        }
        keyed.sort_by(|a, b| a.0.cmp(&b.0));
        Ok(keyed.into_iter().map(|(_, node)| node).collect())
    }

    fn from_data(py: Python<'_>, data: &Bound<'_, PyAny>, source: &str) -> PyResult<Py<Self>> {
        super::check_format(py, data, source)?;
        let graph = Self::empty(py);
        let get = |name: &str| -> PyResult<Bound<'_, PyAny>> {
            data.call_method1(pyo3::intern!(py, "get"), (name, py.None()))
        };
        let builtins = py.import(pyo3::intern!(py, "builtins"))?;
        let dict = builtins.getattr(pyo3::intern!(py, "dict"))?;
        let list = builtins.getattr(pyo3::intern!(py, "list"))?;
        graph.lock().meta = dict
            .call1((or_empty(get("meta")?, PyDict::new(py).into_any()),))?
            .unbind();
        // The classes `konstruct.core.graph` exports, so that a subclass installed there is what
        // `from_dict` makes (a graph read from graph.json holds the native classes themselves).
        let exported = py.import("konstruct.core.graph")?;
        let node_type = exported.getattr("Node")?;
        for item in or_empty(get("nodes")?, PyList::empty(py).into_any()).try_iter()? {
            let node = node_type.call_method1(pyo3::intern!(py, "from_dict"), (item?,))?;
            graph.merge_node(py, node.cast::<Node>()?)?;
        }
        let edge_type = exported.getattr("Edge")?;
        for item in or_empty(get("edges")?, PyList::empty(py).into_any()).try_iter()? {
            let edge = edge_type
                .call_method1(pyo3::intern!(py, "from_dict"), (item?,))?
                .cast_into::<Edge>()?;
            let (s, t, k, attrs) = {
                let this = edge.borrow();
                (
                    this.source.clone_ref(py),
                    this.target.clone_ref(py),
                    this.kind.clone_ref(py),
                    this.attrs.clone_ref(py),
                )
            };
            graph.add_edge(
                py,
                s.bind(py).cast::<PyString>()?,
                t.bind(py).cast::<PyString>()?,
                k.bind(py),
                attrs.bind(py).cast::<PyDict>().ok(),
            )?;
        }
        graph.lock().warnings = list
            .call1((or_empty(get("warnings")?, PyList::empty(py).into_any()),))?
            .unbind();
        Py::new(py, graph)
    }

    fn from_bytes(
        cls: &Bound<'_, PyType>,
        data: Bound<'_, PyBytes>,
        source: &str,
    ) -> PyResult<Py<Self>> {
        let py = cls.py();
        let bytes = data.clone();
        let mut file = match File::open(py, data) {
            Ok(file) => file,
            Err(OpenError::TooLarge) => {
                return Err(super::format_error(
                    py,
                    &format!(
                        "{source} cannot be read: it is larger than the 2 GiB Konstruct reads"
                    ),
                ));
            }
            Err(OpenError::Syntax { message, at }) => {
                return Err(crate::model::file::decode_error(
                    py,
                    bytes.as_bytes(),
                    &format!("{source} is not valid JSON: {message}"),
                    at,
                ));
            }
        };
        let header = file.header(py)?;
        super::check_format(py, &header, source)?;
        if Self::is_subclass(cls) {
            return Self::from_data_for(cls, &file.document(py)?, source);
        }
        let nodes = match file.load(py) {
            Ok(nodes) => nodes,
            // Not a graph as Konstruct writes one (a node id that is a number, say): from_dict
            // gives it the meaning it has always had, errors included.
            Err(error) if error.is_instance_of::<pyo3::exceptions::PyValueError>(py) => {
                return Self::from_data(py, &file.document(py)?, source);
            }
            Err(error) => return Err(error),
        };
        let file = Arc::new(file);
        let graph = Self::with_nodes(py, nodes.by_id);
        let values = graph.node_list(py)?;
        {
            let mut state = graph.lock();
            state.meta = match header.cast::<PyDict>() {
                Ok(header) => match header.get_item("meta")? {
                    Some(meta) => py
                        .import(pyo3::intern!(py, "builtins"))?
                        .getattr(pyo3::intern!(py, "dict"))?
                        .call1((meta,))?
                        .unbind(),
                    None => PyDict::new(py).into_any().unbind(),
                },
                Err(_) => PyDict::new(py).into_any().unbind(),
            };
            // The index of a file's edges is made with it, for as long as the graph is as read.
            let traversal = Py::new(
                py,
                Traversal {
                    core: file.index(),
                    nodes: values.iter().map(|n| n.clone_ref(py)).collect(),
                    edges: TraversalEdges::File(Arc::clone(&file)),
                },
            )?;
            graph
                .indexes
                .bind(py)
                .set_item("traversal", (0u64, traversal.clone_ref(py)))?;
            state.traversal = Some((0, traversal));
            state.edges = Edges::File(Arc::clone(&file));
        }
        // Relations are indexed, and a node whose id came before merged, in the file's order, as
        // `add_node` does them.
        let mut indexed = 0;
        let mut later = nodes.later.into_iter();
        loop {
            let next = later.next();
            let before = next.as_ref().map_or(values.len(), |(before, _)| *before);
            for relation in &values[indexed..before] {
                graph.index_relation(py, relation)?;
            }
            indexed = before;
            match next {
                Some((_, node)) => {
                    graph.merge_node(py, node.bind(py))?;
                }
                None => break,
            }
        }
        if let Ok(header) = header.cast::<PyDict>()
            && let Some(warnings) = header.get_item("warnings")?
        {
            graph.lock().warnings = py
                .import(pyo3::intern!(py, "builtins"))?
                .getattr(pyo3::intern!(py, "list"))?
                .call1((warnings,))?
                .unbind();
        }
        Py::new(py, graph)
    }

    /// The graph as the bytes of its `graph.json`.
    pub(crate) fn json(&self, py: Python<'_>) -> PyResult<Vec<u8>> {
        super::write::graph_json(py, self)
    }

    pub(crate) fn meta_and_warnings(&self, py: Python<'_>) -> (Py<PyAny>, Py<PyAny>) {
        let state = self.lock();
        (state.meta.clone_ref(py), state.warnings.clone_ref(py))
    }

    pub(crate) fn nodes_sorted(&self, py: Python<'_>) -> PyResult<Vec<Py<Node>>> {
        self.sorted_nodes(py)
    }

    pub(crate) fn edge_list(&self, py: Python<'_>) -> PyResult<Vec<Py<Edge>>> {
        self.all_edges(py)
    }
}
