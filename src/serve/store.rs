//! A `graph.json` as the server holds it: the file's bytes, checked to be a Konstruct graph before
//! it replaces the one being served, so a file that does not parse leaves the graph as it was.
//!
//! The server answers nothing from the graph itself: every answer comes from the Python API
//! (`konstruct.interfaces.http_api`), so this reads only what checking the file and the size the
//! event stream reports need.

use std::borrow::Cow;

use serde::Deserialize;
use serde_json::value::RawValue;

/// The fields every node has; reading them is the check. A string with an escape (a quoted
/// identifier's `"`, a backslash, a tab) cannot be borrowed from the file, so each is a `Cow`:
/// borrowed when it can be, copied when it must.
#[derive(Deserialize)]
#[allow(dead_code, reason = "deserialised to check the entry, never read")]
struct NodeEntry<'a> {
    #[serde(borrow)]
    id: Cow<'a, str>,
    #[serde(borrow)]
    kind: Cow<'a, str>,
}

/// The fields every edge has; reading them is the check.
#[derive(Deserialize)]
#[allow(dead_code, reason = "deserialised to check the entry, never read")]
struct EdgeEntry<'a> {
    #[serde(borrow)]
    source: Cow<'a, str>,
    #[serde(borrow)]
    target: Cow<'a, str>,
}

#[derive(Deserialize)]
struct File<'a> {
    #[serde(borrow, default)]
    nodes: Vec<&'a RawValue>,
    #[serde(borrow, default)]
    edges: Vec<&'a RawValue>,
}

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

impl GraphStore {
    /// Check the bytes of a `graph.json`: UTF-8, an object whose `nodes` have an id and a kind
    /// and whose `edges` have a source and a target.
    pub fn load(bytes: Vec<u8>) -> Result<Self, LoadError> {
        let source = std::str::from_utf8(&bytes)
            .map_err(|e| LoadError(format!("graph.json is not UTF-8: {e}")))?;
        let file: File<'_> = serde_json::from_str(source)
            .map_err(|e| LoadError(format!("graph.json is not a Konstruct graph: {e}")))?;
        for raw in &file.nodes {
            serde_json::from_str::<NodeEntry<'_>>(raw.get())
                .map_err(|e| LoadError(format!("graph.json has a node it cannot read: {e}")))?;
        }
        for raw in &file.edges {
            serde_json::from_str::<EdgeEntry<'_>>(raw.get())
                .map_err(|e| LoadError(format!("graph.json has an edge it cannot read: {e}")))?;
        }
        let (nodes, edges) = (file.nodes.len(), file.edges.len());
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
