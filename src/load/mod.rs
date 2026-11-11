//! Reading `graph.json` into the core: the JSON reader's tape ([`crate::json`]), then the graph's
//! shape over it, with the index walks and paths use made straight from the file.
//!
//! A command asks a few questions of a graph of tens of thousands of nodes and a hundred
//! thousand edges, so the graph is not turned into Python objects up front. The nodes are,
//! because Python code reads them all (`Graph.nodes` is a dictionary), but the edges stay here:
//! the walks run on [`GraphIndex`], and an edge becomes a Python object only when something
//! reads it (`src/load/python.rs`).
//!
//! The shape follows `Graph.from_dict`: the last of two members with the same key wins, as in a
//! Python dictionary; a node whose id came before is merged into the first by Python (see
//! [`NodeFields::first`]); an edge from a node to itself is dropped; and an edge with the
//! source, target and kind of an earlier one is that edge, which takes the attributes it does
//! not have yet from each later one ([`Shape::attrs`]).
//!
//! It reads a graph as Konstruct writes one: string ids, kinds, sources, targets and edge kinds,
//! a string or null parent and relation, a list of origins and an object of attributes. A
//! document with anything else in those places (an id that is a number, origins that are a
//! string) is not what any Konstruct wrote, and [`Shape::read`] says why; `Graph.read` then
//! reads the whole document into `Graph.from_dict`, which gives it the meaning it always had.

#[cfg(feature = "python")]
pub(crate) mod python;

use crate::fnv::FnvHashMap;
use crate::graph::{EdgeKind, GraphIndex, NodeKind};
use crate::json::tape::{Doc, Kind};
use crate::keys;

/// The newest `graph.json` format this Konstruct reads and writes (`konstruct.core.formats`
/// says when it changes); every earlier one is read.
pub const GRAPH_FORMAT_VERSION: i64 = 1;

/// A field a node or edge does not have.
pub const NONE: u32 = u32::MAX;

/// Where a node's fields are on the tape, each [`NONE`] when the node has no such member.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NodeFields {
    pub id: u32,
    pub kind: u32,
    pub name: u32,
    pub parent: u32,
    pub relation: u32,
    pub description: u32,
    pub data_type: u32,
    pub path: u32,
    pub origins: u32,
    pub attrs: u32,
    /// Whether no node before it has its id. A later one is merged into the first by
    /// `Graph.add_node`, as `Graph.from_dict` merges it.
    pub first: bool,
}

/// An edge of the graph, by its kind and attributes on the tape.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EdgeFields {
    /// Its kind, as an index into [`Shape::kinds`].
    pub kind: u16,
    /// Its `attrs` object, or [`NONE`].
    pub attrs: u32,
}

/// A graph document's nodes and edges, as the tape holds them, and its index.
#[derive(Debug)]
pub struct Shape {
    /// Every node in `nodes`, in order.
    pub nodes: Vec<NodeFields>,
    /// Each edge once, numbered as [`GraphIndex`] numbers them.
    pub edges: Vec<EdgeFields>,
    /// The `attrs` objects of the later edges that are the same edge as an earlier one.
    pub more_attrs: FnvHashMap<u32, Vec<u32>>,
    /// The edges' kinds, each once.
    pub kinds: Vec<Box<str>>,
    pub index: GraphIndex,
}

/// Why a document that is JSON is not a graph as Konstruct writes one.
pub type ShapeError = String;

/// The members of the object at `object` with the key `name`: the value of the last, as a
/// Python dictionary keeps it.
pub fn member(doc: Doc<'_>, object: usize, name: &str) -> Option<usize> {
    doc.members(object)
        .filter(|&(key, _)| doc.is(key, name))
        .last()
        .map(|(_, value)| value)
}

/// A member's key, as bytes: as written unless it holds an escape, which only a hand-written
/// file would use.
fn key<'a>(doc: Doc<'a>, key: usize) -> std::borrow::Cow<'a, [u8]> {
    if doc.escaped(key) {
        std::borrow::Cow::Owned(doc.text(key).into_owned().into_bytes())
    } else {
        std::borrow::Cow::Borrowed(doc.raw(key))
    }
}

fn ordinal(value: usize) -> u32 {
    u32::try_from(value).expect("a tape of fewer than 2^31 values")
}

impl Shape {
    /// The shape of the graph document `doc`, whose root must be an object.
    ///
    /// # Errors
    ///
    /// When a node or edge is not an object, misses a field it must have, or has one of a kind
    /// Konstruct cannot use (an id that is not a string, say).
    pub fn read(doc: Doc<'_>) -> Result<Self, ShapeError> {
        let mut builder = GraphIndex::builder();
        let nodes = match member(doc, 0, "nodes") {
            Some(list) => Self::nodes(doc, list, &mut builder)?,
            None => Vec::new(),
        };
        let mut shape = Self {
            nodes,
            edges: Vec::new(),
            more_attrs: FnvHashMap::default(),
            kinds: Vec::new(),
            index: GraphIndex::builder().build(),
        };
        // (source, target, kind) of each edge, by ordinal.
        let mut ends: Vec<(u32, u32, EdgeKind)> = Vec::new();
        if let Some(list) = member(doc, 0, "edges") {
            shape.edges(doc, list, &mut builder, &mut ends)?;
        }
        for (edge, &(source, target, kind)) in ends.iter().enumerate() {
            let reference = shape
                .attr(doc, ordinal(edge), "role")
                .is_some_and(|role| doc.is(role, "reference"));
            builder.edge_between(source, target, kind, reference);
        }
        // The first of each id is a node, numbered in order.
        for (node, fields) in shape.nodes.iter().filter(|f| f.first).enumerate() {
            if fields.parent != NONE && doc.kind(fields.parent as usize) == Kind::String {
                let parent = doc.text(fields.parent as usize);
                if !parent.is_empty() {
                    builder.parent(ordinal(node), &keys::from_str(&parent));
                }
            }
        }
        for edge in 0..shape.edges.len() {
            let edge = ordinal(edge);
            let via = shape
                .attr(doc, edge, "via")
                .filter(|&v| doc.kind(v) == Kind::String);
            let also = shape
                .attr(doc, edge, "also_via")
                .filter(|&v| doc.kind(v) == Kind::Array);
            let tasks = via
                .into_iter()
                .chain(also.into_iter().flat_map(|list| doc.items(list)));
            for task in tasks.filter(|&t| doc.kind(t) == Kind::String) {
                builder.through(edge, &keys::from_str(&doc.text(task)));
            }
        }
        shape.index = builder.build();
        Ok(shape)
    }

    fn nodes(
        doc: Doc<'_>,
        list: usize,
        builder: &mut crate::graph::GraphIndexBuilder,
    ) -> Result<Vec<NodeFields>, ShapeError> {
        if doc.kind(list) != Kind::Array {
            return Err("its nodes are not a list".into());
        }
        let mut nodes = Vec::new();
        for (number, item) in doc.items(list).enumerate() {
            if doc.kind(item) != Kind::Object {
                return Err(format!("node {number} is not an object"));
            }
            let mut fields = NodeFields {
                id: NONE,
                kind: NONE,
                name: NONE,
                parent: NONE,
                relation: NONE,
                description: NONE,
                data_type: NONE,
                path: NONE,
                origins: NONE,
                attrs: NONE,
                first: false,
            };
            for (name, value) in doc.members(item) {
                let slot = match &*key(doc, name) {
                    b"id" => &mut fields.id,
                    b"kind" => &mut fields.kind,
                    b"name" => &mut fields.name,
                    b"parent" => &mut fields.parent,
                    b"relation" => &mut fields.relation,
                    b"description" => &mut fields.description,
                    b"data_type" => &mut fields.data_type,
                    b"path" => &mut fields.path,
                    b"origins" => &mut fields.origins,
                    b"attrs" => &mut fields.attrs,
                    _ => continue,
                };
                *slot = ordinal(value);
            }
            let is = |field: u32, kinds: &[Kind]| {
                field != NONE && kinds.contains(&doc.kind(field as usize))
            };
            for (field, name) in [(fields.id, "id"), (fields.kind, "kind")] {
                if !is(field, &[Kind::String]) {
                    return Err(format!("node {number} has no {name} that is a string"));
                }
            }
            if fields.name == NONE {
                return Err(format!("node {number} has no name"));
            }
            for (field, name) in [(fields.parent, "parent"), (fields.relation, "relation")] {
                if field != NONE && !is(field, &[Kind::String, Kind::Null]) {
                    return Err(format!("node {number} has a {name} that is not a string"));
                }
            }
            if fields.origins != NONE && !is(fields.origins, &[Kind::Array]) {
                return Err(format!("node {number} has origins that are not a list"));
            }
            if fields.attrs != NONE && !is(fields.attrs, &[Kind::Object]) {
                return Err(format!("node {number} has attrs that are not an object"));
            }
            let id = doc.text(fields.id as usize);
            let kind = NodeKind::parse(&doc.text(fields.kind as usize));
            fields.first = builder.node(&keys::from_str(&id), kind);
            nodes.push(fields);
        }
        Ok(nodes)
    }

    fn edges(
        &mut self,
        doc: Doc<'_>,
        list: usize,
        builder: &mut crate::graph::GraphIndexBuilder,
        ends: &mut Vec<(u32, u32, EdgeKind)>,
    ) -> Result<(), ShapeError> {
        if doc.kind(list) != Kind::Array {
            return Err("its edges are not a list".into());
        }
        let mut seen: FnvHashMap<(u32, u32, u16), u32> = FnvHashMap::default();
        for (number, item) in doc.items(list).enumerate() {
            if doc.kind(item) != Kind::Object {
                return Err(format!("edge {number} is not an object"));
            }
            let (mut source, mut target, mut kind, mut attrs) = (NONE, NONE, NONE, NONE);
            for (name, value) in doc.members(item) {
                let slot = match &*key(doc, name) {
                    b"source" => &mut source,
                    b"target" => &mut target,
                    b"kind" => &mut kind,
                    b"attrs" => &mut attrs,
                    _ => continue,
                };
                *slot = ordinal(value);
            }
            for (field, name) in [(source, "source"), (target, "target"), (kind, "kind")] {
                if field == NONE || doc.kind(field as usize) != Kind::String {
                    return Err(format!("edge {number} has no {name} that is a string"));
                }
            }
            if attrs != NONE && doc.kind(attrs as usize) != Kind::Object {
                return Err(format!("edge {number} has attrs that are not an object"));
            }
            let (from, to) = (doc.text(source as usize), doc.text(target as usize));
            if from == to {
                continue;
            }
            let from = builder.endpoint(&keys::from_str(&from));
            let to = builder.endpoint(&keys::from_str(&to));
            let name = doc.text(kind as usize);
            let kind = match self.kinds.iter().position(|k| **k == *name) {
                Some(found) => found,
                None => {
                    self.kinds.push(name.as_ref().into());
                    self.kinds.len() - 1
                }
            };
            let kind = u16::try_from(kind).map_err(|_| "its edges have more than 65,536 kinds")?;
            match seen.entry((from, to, kind)) {
                std::collections::hash_map::Entry::Occupied(earlier) => {
                    if attrs != NONE {
                        self.more_attrs
                            .entry(*earlier.get())
                            .or_default()
                            .push(attrs);
                    }
                }
                std::collections::hash_map::Entry::Vacant(slot) => {
                    slot.insert(ordinal(self.edges.len()));
                    self.edges.push(EdgeFields { kind, attrs });
                    ends.push((from, to, EdgeKind::parse(&self.kinds[kind as usize])));
                }
            }
        }
        Ok(())
    }

    /// The `attrs` objects of edge `edge`: its own, then those of the later edges that are the
    /// same edge, in order.
    pub fn attrs(&self, edge: u32) -> impl Iterator<Item = usize> + '_ {
        let own = self.edges[edge as usize].attrs;
        (own != NONE).then_some(own as usize).into_iter().chain(
            self.more_attrs
                .get(&edge)
                .into_iter()
                .flatten()
                .map(|&a| a as usize),
        )
    }

    /// The attribute `name` of edge `edge` as the merged attributes hold it: from the first of
    /// its `attrs` objects that has it.
    pub fn attr(&self, doc: Doc<'_>, edge: u32, name: &str) -> Option<usize> {
        self.attrs(edge)
            .find_map(|object| member(doc, object, name))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::Flow;
    use crate::json::tape::Tape;

    fn shape(text: &str) -> Result<Shape, ShapeError> {
        let tape = Tape::parse(text.as_bytes()).expect("JSON");
        Shape::read(tape.doc(text.as_bytes()))
    }

    #[test]
    fn nodes_edges_and_the_index() {
        let text = r#"{"nodes": [
            {"id": "model.a", "kind": "model", "name": "a"},
            {"id": "model.a#x", "kind": "column", "name": "x", "parent": "model.a"},
            {"id": "model.b", "kind": "model", "name": "b"},
            {"id": "model.b#y", "kind": "column", "name": "y", "parent": "model.b"},
            {"id": "model.a", "kind": "model", "name": "a", "path": "a.sql"}
        ], "edges": [
            {"source": "model.a", "target": "model.a#x", "kind": "contains"},
            {"source": "model.b", "target": "model.b#y", "kind": "contains"},
            {"source": "model.b#y", "target": "model.a#x", "kind": "derived_from", "attrs": {"transform": "direct"}},
            {"source": "model.a", "target": "model.a", "kind": "feeds"},
            {"source": "model.b#y", "target": "model.a#x", "kind": "derived_from", "attrs": {"role": "reference", "transform": "x"}},
            {"source": "model.a", "target": "model.b", "kind": "feeds", "attrs": {"via": "task.d.t"}},
            {"source": "task.d.t", "target": "model.b", "kind": "feeds"}
        ]}"#;
        let shape = shape(text).unwrap();
        assert_eq!(
            shape.nodes.iter().map(|n| n.first).collect::<Vec<_>>(),
            [true, true, true, true, false]
        );
        assert_eq!(shape.edges.len(), 5);
        assert_eq!(
            &*shape.kinds,
            ["contains".into(), "derived_from".into(), "feeds".into()]
        );
        assert_eq!(shape.more_attrs.get(&2).map(Vec::len), Some(1));
        let index = &shape.index;
        assert_eq!(index.node_count(), 4);
        assert!(index.edge(2).reference);
        let a = index.node("model.a#x").unwrap();
        let downstream: Vec<u32> = index
            .neighbours(a, Flow::Downstream)
            .map(|(e, _)| e)
            .collect();
        assert_eq!(downstream, [2]);
        assert_eq!(index.id(index.edge(4).source), "task.d.t");
        let tape = Tape::parse(text.as_bytes()).unwrap();
        let doc = tape.doc(text.as_bytes());
        let transform = shape.attr(doc, 2, "transform").unwrap();
        assert_eq!(doc.text(transform), "direct");
        assert_eq!(
            shape.attr(doc, 2, "role").map(|r| doc.text(r)),
            Some("reference".into())
        );
    }

    #[test]
    fn a_name_may_be_any_value() {
        let shape = shape(
            r#"{"nodes": [{"id": "a", "kind": "model", "name": null}, {"id": "b", "kind": "model", "name": 3}],
                "edges": [{"source": "a", "target": "a", "kind": "feeds"}, {"source": "a", "target": "b", "kind": "feeds"},
                          {"source": "a", "target": "b", "kind": "feeds"}]}"#,
        )
        .unwrap();
        assert_eq!((shape.nodes.len(), shape.edges.len()), (2, 1));
    }

    #[test]
    fn a_document_without_nodes_or_edges_is_an_empty_graph() {
        let shape = shape(r#"{"format_version": 1}"#).unwrap();
        assert!(shape.nodes.is_empty() && shape.edges.is_empty());
    }

    #[test]
    fn shapes_konstruct_does_not_write_are_told_apart() {
        for (text, message) in [
            (r#"{"nodes": {}}"#, "its nodes are not a list"),
            (r#"{"nodes": [1]}"#, "node 0 is not an object"),
            (
                r#"{"nodes": [{"kind": "model", "name": "a"}]}"#,
                "node 0 has no id that is a string",
            ),
            (
                r#"{"nodes": [{"id": 1, "kind": "model", "name": "a"}]}"#,
                "node 0 has no id that is a string",
            ),
            (
                r#"{"nodes": [{"id": "a", "kind": "model"}]}"#,
                "node 0 has no name",
            ),
            (
                r#"{"nodes": [{"id": "a", "kind": "model", "name": "a", "parent": 2}]}"#,
                "node 0 has a parent that is not a string",
            ),
            (
                r#"{"nodes": [{"id": "a", "kind": "model", "name": "a", "origins": "x"}]}"#,
                "node 0 has origins that are not a list",
            ),
            (
                r#"{"nodes": [{"id": "a", "kind": "model", "name": "a", "attrs": []}]}"#,
                "node 0 has attrs that are not an object",
            ),
            (r#"{"edges": "x"}"#, "its edges are not a list"),
            (
                r#"{"edges": [{"source": "a", "kind": "feeds"}]}"#,
                "edge 0 has no target that is a string",
            ),
            (
                r#"{"edges": [{"source": "a", "target": "b", "kind": "feeds", "attrs": 1}]}"#,
                "edge 0 has attrs that are not an object",
            ),
        ] {
            assert_eq!(shape(text).unwrap_err(), message, "{text}");
        }
    }
}
