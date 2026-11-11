//! A `graph.json` as the server holds it: the file's bytes, checked to be a graph this Konstruct
//! reads before it replaces the one being served, so a file that does not parse leaves the graph
//! as it was.
//!
//! The server answers nothing from the graph itself: every answer comes from the Python API
//! (`konstruct.interfaces.http_api`), which reads the file with `Graph.read`'s reader. So this
//! decides only what that reader decides before anything else, with the same JSON reader
//! ([`crate::json`]): JSON, an object, and a format this Konstruct reads. What the nodes and
//! edges hold is left to `Graph.read`, so the server serves exactly the graphs it reads.

use crate::json::tape::{Doc, Kind, Number, Tape};
use crate::load::{GRAPH_FORMAT_VERSION, member};

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
    /// How many node entries the file has, for the event stream.
    pub nodes: usize,
    /// How many edge entries the file has, for the event stream.
    pub edges: usize,
}

/// How many entries the graph's list `name` has: as a Python dictionary keeps it, the last
/// member of that name; none when it is not a list.
fn entries(doc: Doc<'_>, name: &str) -> usize {
    member(doc, 0, name)
        .filter(|&list| doc.kind(list) == Kind::Array)
        .map_or(0, |list| doc.items(list).count())
}

impl GraphStore {
    /// Check the bytes of a `graph.json` as `Graph.read` does first (`konstruct.core.formats`):
    /// JSON, an object, and a `format_version`, the last member of that name, that is absent
    /// (format 1) or an integer from 1 to [`GRAPH_FORMAT_VERSION`].
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
                "graph.json is not a Konstruct document (expected a JSON object)".into(),
            ));
        }
        if let Some(version) = member(doc, 0, "format_version") {
            let newer = |format: &dyn std::fmt::Display| {
                LoadError(format!(
                    "graph.json was written by a newer Konstruct (format {format}; this one reads \
                     formats 1 to {GRAPH_FORMAT_VERSION}); upgrade Konstruct to read it"
                ))
            };
            // An integer, as Python's int: not a bool, and not a float such as 1.0.
            let number = (doc.kind(version) == Kind::Number).then(|| doc.number(version));
            match number {
                Some(Number::Int(n)) if (1..=GRAPH_FORMAT_VERSION).contains(&n) => {}
                Some(Number::Int(n)) if n > GRAPH_FORMAT_VERSION => return Err(newer(&n)),
                Some(Number::UInt(n)) => return Err(newer(&n)),
                _ => return Err(LoadError("graph.json has an unknown format_version".into())),
            }
        }
        let (nodes, edges) = (entries(doc, "nodes"), entries(doc, "edges"));
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
    fn what_graph_read_reads_is_served() {
        // Graph.read gives these the meaning Graph.from_dict always gave them, or answers with
        // its own error; the server leaves them to it.
        for (text, counts) in [
            (
                r#"{"nodes": [{"id": 1, "kind": "model", "name": "a"}]}"#,
                (1, 0),
            ),
            (r#"{"nodes": [{"id": "a", "kind": "model"}]}"#, (1, 0)),
            (
                r#"{"nodes": [{"id": "a", "kind": 3, "attrs": [], "origins": "abc"}],
                    "edges": [{"source": "a", "target": "b", "kind": 2}, {"source": 1}]}"#,
                (1, 2),
            ),
            (r#"{"nodes": {"a": 1}, "edges": "x"}"#, (0, 0)),
            (r#"{"format_version": 1, "nodes": []}"#, (0, 0)),
        ] {
            let graph = GraphStore::load(text.as_bytes().to_vec()).expect(text);
            assert_eq!((graph.nodes, graph.edges), counts, "{text}");
        }
    }

    #[test]
    fn a_newer_or_unknown_format_is_refused() {
        for (text, message) in [
            (r#"{"format_version": 2}"#, "newer Konstruct (format 2"),
            (
                r#"{"format_version": 18446744073709551615}"#,
                "newer Konstruct",
            ),
            (r#"{"format_version": 0}"#, "unknown format_version"),
            (r#"{"format_version": 1.0}"#, "unknown format_version"),
            (r#"{"format_version": true}"#, "unknown format_version"),
            (r#"{"format_version": "1"}"#, "unknown format_version"),
            ("[1]", "not a Konstruct document"),
        ] {
            let error = GraphStore::load(text.as_bytes().to_vec()).unwrap_err();
            assert!(error.0.contains(message), "{text}: {error}");
        }
    }

    #[test]
    fn the_last_of_two_members_with_the_same_key_counts() {
        // As in a Python dictionary, so as Graph.read reads the file.
        let newer = r#"{"format_version": 1, "format_version": 2}"#;
        assert!(GraphStore::load(newer.as_bytes().to_vec()).is_err());
        let older = r#"{"format_version": 2, "format_version": 1, "nodes": [1, 2], "nodes": [3]}"#;
        let graph = GraphStore::load(older.as_bytes().to_vec()).unwrap();
        assert_eq!(graph.nodes, 1);
    }

    #[test]
    fn bad_files_are_errors() {
        assert!(GraphStore::load(b"not json".to_vec()).is_err());
        assert!(GraphStore::load(vec![0xff, 0xfe]).is_err());
        assert!(GraphStore::load(br#"{"nodes": [1,]}"#.to_vec()).is_err());
        let empty = GraphStore::load(b"{}".to_vec()).unwrap();
        assert_eq!(empty.nodes, 0);
    }
}
