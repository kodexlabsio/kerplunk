//! A `graph.json` as the server holds it: the file's bytes, checked to be a Konstruct graph before
//! it replaces the one being served, so a file that does not parse leaves the graph as it was.
//!
//! The server answers nothing from the graph itself: every answer comes from the Python API
//! (`konstruct.interfaces.http_api`), so this reads only what checking the file and the size the
//! event stream reports need, with the JSON reader `Graph.read` uses ([`crate::json`]).

use crate::json::tape::{Doc, Kind, Tape};
use crate::load::member;

/// Why a graph could not be read.
#[derive(Debug)]
pub struct LoadError(pub String);

impl std::fmt::Display for LoadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for LoadError {}

/// One version of the graph, as the bytes of its `graph.json`.
#[derive(Debug)]
pub struct GraphStore {
    /// The file as read.
    pub bytes: bytes::Bytes,
    /// How many node entries the file has.
    pub nodes: usize,
    /// How many edge entries the file has.
    pub edges: usize,
}

/// How many entries the list `name` of the graph has, each an object whose `fields` are strings;
/// none when it has no such list.
fn entries(doc: Doc<'_>, name: &str, fields: [&str; 2]) -> Result<usize, LoadError> {
    let Some(list) = member(doc, 0, name) else {
        return Ok(0);
    };
    if doc.kind(list) != Kind::Array {
        return Err(LoadError(format!(
            "graph.json is not a Konstruct graph: its {name} are not a list"
        )));
    }
    let mut count = 0;
    for (number, item) in doc.items(list).enumerate() {
        for field in fields {
            let value = (doc.kind(item) == Kind::Object)
                .then(|| member(doc, item, field))
                .flatten();
            if value.is_none_or(|v| doc.kind(v) != Kind::String) {
                return Err(LoadError(format!(
                    "graph.json has an entry {number} in its {name} without a {field} that is a string"
                )));
            }
        }
        count += 1;
    }
    Ok(count)
}

impl GraphStore {
    /// Check the bytes of a `graph.json` with the reader `Graph.read` uses: JSON, and an object
    /// whose `nodes` have a string id and kind and whose `edges` have a string source and
    /// target.
    pub fn load(bytes: Vec<u8>) -> Result<Self, LoadError> {
        let tape = Tape::parse(&bytes).map_err(|e| {
            LoadError(format!(
                "graph.json is not valid JSON: {} at byte {}",
                e.message, e.at
            ))
        })?;
        let doc = tape.doc(&bytes);
        if doc.kind(0) != Kind::Object {
            return Err(LoadError(
                "graph.json is not a Konstruct graph: it is not an object".into(),
            ));
        }
        let nodes = entries(doc, "nodes", ["id", "kind"])?;
        let edges = entries(doc, "edges", ["source", "target"])?;
        Ok(Self {
            bytes: bytes::Bytes::from(bytes),
            nodes,
            edges,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_graph_is_counted() {
        let text = r#"{"format_version": 1, "nodes": [
            {"id": "model.a.orders", "kind": "model", "name": "orders"},
            {"id": "model.a.orders#id", "kind": "column", "name": "id", "parent": "model.a.orders"}
        ], "edges": [{"source": "model.a.orders", "target": "model.a.orders#id", "kind": "contains"}]}"#;
        let graph = GraphStore::load(text.as_bytes().to_vec()).unwrap();
        assert_eq!((graph.nodes, graph.edges), (2, 1));
        assert_eq!(&graph.bytes[..], text.as_bytes());
    }

    #[test]
    fn escaped_ids_are_read() {
        // A quoted identifier such as "a""b", a backslash or a tab is escaped in the file.
        let text = r#"{"nodes": [
            {"id": "table.main.t", "kind": "table", "name": "t"},
            {"id": "table.main.t#a\"b", "kind": "column", "name": "a\"b"},
            {"id": "table.main.t#c\\d\te", "kind": "column", "name": "c\\d\te"},
            {"id": "table.main.t#\u00e9", "kind": "col\u0075mn", "name": "\u00e9"}
        ], "edges": [{"source": "table.main.t", "target": "table.main.t#a\"b", "kind": "contains"}]}"#;
        let graph = GraphStore::load(text.as_bytes().to_vec()).unwrap();
        assert_eq!((graph.nodes, graph.edges), (4, 1));
    }

    #[test]
    fn values_konstruct_does_not_write_are_served() {
        // Graph.read gives these the meaning Graph.from_dict always gave them.
        let text = r#"{"nodes": [{"id": "a", "kind": "model", "attrs": [], "origins": "abc"}],
            "edges": [{"source": "a", "target": "b", "kind": 2}, {"source": "a", "target": "a"}]}"#;
        let graph = GraphStore::load(text.as_bytes().to_vec()).unwrap();
        assert_eq!((graph.nodes, graph.edges), (1, 2));
    }

    #[test]
    fn bad_files_are_errors() {
        assert!(GraphStore::load(b"not json".to_vec()).is_err());
        assert!(GraphStore::load(vec![0xff, 0xfe]).is_err());
        assert!(GraphStore::load(br#"{"nodes": [{"id": 1}]}"#.to_vec()).is_err());
        assert!(
            GraphStore::load(br#"{"nodes": [], "edges": [{"source": "a"}]}"#.to_vec()).is_err()
        );
        assert!(GraphStore::load(br#"{"nodes": {}}"#.to_vec()).is_err());
        let empty = GraphStore::load(b"{}".to_vec()).unwrap();
        assert_eq!(empty.nodes, 0);
    }
}
