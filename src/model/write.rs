//! A graph as the bytes of its `graph.json`: two-space indentation, nodes in the order of their
//! ids, edges in the order they were added (which keeps a relation's columns in table order),
//! and attributes as the values they hold. The layout is the one the build has always written.

use pyo3::prelude::*;
use pyo3::types::{PyList, PyString};

use super::encode::Encoder;
use crate::write::unsigned;

use super::graph::Graph;

/// Writes `value` under `name` as a member of an object at nesting `level`, after the member
/// before it when `more`.
fn member<'py>(
    out: &mut Encoder<'py>,
    more: &mut bool,
    level: usize,
    name: &str,
    value: &Bound<'py, PyAny>,
) -> PyResult<()> {
    if std::mem::replace(more, true) {
        out.out.push(b',');
    }
    out.line(level);
    out.key(name);
    out.value(value, level)
}

/// The origins of a node, in order: a list of strings is sorted here, anything else by Python.
fn sorted<'py>(py: Python<'py>, origins: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    if let Ok(list) = origins.cast::<PyList>() {
        let mut texts = Vec::with_capacity(list.len());
        for item in list.iter() {
            match item.cast_into::<PyString>() {
                Ok(text) => texts.push(text),
                Err(_) => {
                    return py
                        .import(pyo3::intern!(py, "builtins"))?
                        .getattr(pyo3::intern!(py, "sorted"))?
                        .call1((origins,));
                }
            }
        }
        let mut keyed = Vec::with_capacity(texts.len());
        for text in texts {
            match text.to_str() {
                Ok(utf8) => keyed.push((utf8.as_bytes().to_vec(), text)),
                Err(_) => {
                    return py
                        .import(pyo3::intern!(py, "builtins"))?
                        .getattr(pyo3::intern!(py, "sorted"))?
                        .call1((origins,));
                }
            }
        }
        keyed.sort_by(|a, b| a.0.cmp(&b.0));
        return PyList::new(py, keyed.into_iter().map(|(_, text)| text)).map(Bound::into_any);
    }
    py.import(pyo3::intern!(py, "builtins"))?
        .getattr(pyo3::intern!(py, "sorted"))?
        .call1((origins,))
}

pub(super) fn graph_json(py: Python<'_>, graph: &Graph) -> PyResult<Vec<u8>> {
    let mut out = Encoder::new();
    let (meta, warnings) = graph.meta_and_warnings(py);
    out.out.push(b'{');
    out.line(1);
    out.key("format_version");
    unsigned(&mut out.out, crate::load::GRAPH_FORMAT_VERSION as u64);
    out.out.push(b',');
    out.line(1);
    out.key("meta");
    out.value(meta.bind(py), 1)?;
    out.out.push(b',');
    out.line(1);
    out.key("nodes");
    let nodes = graph.nodes_sorted(py)?;
    if nodes.is_empty() {
        out.out.extend_from_slice(b"[]");
    } else {
        out.out.push(b'[');
        for (n, node) in nodes.iter().enumerate() {
            if n > 0 {
                out.out.push(b',');
            }
            out.line(2);
            out.out.push(b'{');
            let node = node.borrow(py);
            let mut more = false;
            member(&mut out, &mut more, 3, "id", node.id.bind(py))?;
            member(&mut out, &mut more, 3, "kind", node.kind.bind(py))?;
            member(&mut out, &mut more, 3, "name", node.name.bind(py))?;
            for name in ["parent", "relation", "description", "data_type", "path"] {
                let value = node.optional(name).bind(py);
                if value.is_truthy()? {
                    member(&mut out, &mut more, 3, name, value)?;
                }
            }
            let origins = node.origins.bind(py);
            if origins.is_truthy()? {
                member(&mut out, &mut more, 3, "origins", &sorted(py, origins)?)?;
            }
            let attrs = node.attrs.bind(py);
            if attrs.is_truthy()? {
                member(&mut out, &mut more, 3, "attrs", attrs)?;
            }
            out.line(2);
            out.out.push(b'}');
        }
        out.line(1);
        out.out.push(b']');
    }
    out.out.push(b',');
    out.line(1);
    out.key("edges");
    let edges = graph.edge_list(py)?;
    if edges.is_empty() {
        out.out.extend_from_slice(b"[]");
    } else {
        out.out.push(b'[');
        for (n, edge) in edges.iter().enumerate() {
            if n > 0 {
                out.out.push(b',');
            }
            out.line(2);
            out.out.push(b'{');
            let edge = edge.borrow(py);
            let mut more = false;
            member(&mut out, &mut more, 3, "source", edge.source.bind(py))?;
            member(&mut out, &mut more, 3, "target", edge.target.bind(py))?;
            member(&mut out, &mut more, 3, "kind", edge.kind.bind(py))?;
            let attrs = edge.attrs.bind(py);
            if attrs.is_truthy()? {
                member(&mut out, &mut more, 3, "attrs", attrs)?;
            }
            out.line(2);
            out.out.push(b'}');
        }
        out.line(1);
        out.out.push(b']');
    }
    out.out.push(b',');
    out.line(1);
    out.key("warnings");
    out.value(warnings.bind(py), 1)?;
    out.line(0);
    out.out.push(b'}');
    out.out.push(b'\n');
    Ok(out.out)
}
