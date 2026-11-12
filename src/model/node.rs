//! `Node`, `Edge` and `Step`: what a graph holds and what a walk reports.
//!
//! They are plain mutable records, as the dataclasses they replace were: code that reads a graph
//! also edits a node's fields and an edge's `attrs` in place, and tells the graph with
//! `Graph.changed()` when an edit changes what an index is made from. Every field holds the
//! Python object it was given, so a node's `attrs` is the same dict whoever reached it.

use pyo3::basic::CompareOp;
use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyModule, PyString, PyTuple, PyType};

/// The fields of a node that are text or None, in the order `to_dict` writes them.
const OPTIONAL: [&str; 5] = ["parent", "relation", "description", "data_type", "path"];

/// A node of the graph.
#[pyclass(subclass, dict, weakref, module = "konstruct.core.graph")]
pub struct Node {
    /// A unique id, such as `model.<package>.<name>`, `table.<relation>`,
    /// `<relation id>#<column>`, `task.<dag>.<task>`, `looker.view.<view>`,
    /// `powerbi.table.<model>.<table>` or `term.<slug>`.
    #[pyo3(get, set)]
    pub id: Py<PyAny>,
    /// What the node is.
    #[pyo3(get, set)]
    pub kind: Py<PyAny>,
    /// Its own name.
    #[pyo3(get, set)]
    pub name: Py<PyAny>,
    /// For a column, its relation's id.
    #[pyo3(get, set)]
    pub parent: Py<PyAny>,
    /// For a relation, its lower-case warehouse name, such as `shop.main.orders`.
    #[pyo3(get, set)]
    pub relation: Py<PyAny>,
    /// Description from dbt, the warehouse or definitions.yml.
    #[pyo3(get, set)]
    pub description: Py<PyAny>,
    /// Column type.
    #[pyo3(get, set)]
    pub data_type: Py<PyAny>,
    /// The file that defines it, relative to the project.
    #[pyo3(get, set)]
    pub path: Py<PyAny>,
    /// Inputs it came from.
    #[pyo3(get, set)]
    pub origins: Py<PyAny>,
    /// Everything else, such as expressions, conditions, schedules, usage and term fields.
    #[pyo3(get, set)]
    pub attrs: Py<PyAny>,
}

impl Node {
    /// The fields, in declaration order.
    fn fields<'py>(&self, py: Python<'py>) -> [Bound<'py, PyAny>; 10] {
        [
            self.id.bind(py).clone(),
            self.kind.bind(py).clone(),
            self.name.bind(py).clone(),
            self.parent.bind(py).clone(),
            self.relation.bind(py).clone(),
            self.description.bind(py).clone(),
            self.data_type.bind(py).clone(),
            self.path.bind(py).clone(),
            self.origins.bind(py).clone(),
            self.attrs.bind(py).clone(),
        ]
    }

    /// The optional text field `name` ("parent", "relation", ...).
    pub fn optional<'a>(&'a self, name: &str) -> &'a Py<PyAny> {
        match name {
            "parent" => &self.parent,
            "relation" => &self.relation,
            "description" => &self.description,
            "data_type" => &self.data_type,
            _ => &self.path,
        }
    }

    pub fn set_optional(&mut self, name: &str, value: Py<PyAny>) {
        match name {
            "parent" => self.parent = value,
            "relation" => self.relation = value,
            "description" => self.description = value,
            "data_type" => self.data_type = value,
            _ => self.path = value,
        }
    }
}

#[pymethods]
impl Node {
    #[new]
    #[pyo3(signature = (*args, **kwargs), text_signature = "(id, kind, name, parent=None, relation=None, description=None, data_type=None, path=None, origins=None, attrs=None)")]
    fn new(
        py: Python<'_>,
        args: &Bound<'_, PyTuple>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
        // The arguments belong to `__init__`, which a subclass may have its own of.
        let _ = (args, kwargs);
        Ok(Self::empty(py))
    }

    /// Sets the fields, as the dataclass's `__init__` did: for a subclass's `super().__init__()`.
    #[pyo3(name = "__init__", signature = (*args, **kwargs))]
    fn init(
        &mut self,
        py: Python<'_>,
        args: &Bound<'_, PyTuple>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<()> {
        *self = Self::from_args(py, args, kwargs)?;
        Ok(())
    }

    /// A copy with some fields changed (`copy.replace`).
    #[pyo3(signature = (**changes))]
    fn __replace__<'py>(
        slf: &Bound<'py, Self>,
        changes: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        replaced(slf.as_any(), &NODE_FIELDS, changes)
    }

    /// The node as plain data: `id`, `kind` and `name`, the fields that are set, its origins in
    /// order, and `attrs` when there are any.
    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let data = PyDict::new(py);
        data.set_item(pyo3::intern!(py, "id"), self.id.bind(py))?;
        data.set_item(pyo3::intern!(py, "kind"), self.kind.bind(py))?;
        data.set_item(pyo3::intern!(py, "name"), self.name.bind(py))?;
        for key in OPTIONAL {
            let value = self.optional(key).bind(py);
            if value.is_truthy()? {
                data.set_item(key, value)?;
            }
        }
        let origins = self.origins.bind(py);
        if origins.is_truthy()? {
            let sorted = py
                .import(pyo3::intern!(py, "builtins"))?
                .getattr(pyo3::intern!(py, "sorted"))?
                .call1((origins,))?;
            data.set_item(pyo3::intern!(py, "origins"), sorted)?;
        }
        let attrs = self.attrs.bind(py);
        if attrs.is_truthy()? {
            data.set_item(pyo3::intern!(py, "attrs"), attrs)?;
        }
        Ok(data)
    }

    /// The node `data` describes, as `to_dict` writes it.
    #[classmethod]
    fn from_dict<'py>(
        cls: &Bound<'py, PyType>,
        data: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = cls.py();
        let get = |key: &str| -> PyResult<Option<Bound<'py, PyAny>>> {
            let found = data.call_method1(pyo3::intern!(py, "get"), (key,))?;
            Ok((!found.is_none()).then_some(found))
        };
        let builtins = py.import(pyo3::intern!(py, "builtins"))?;
        let list = builtins.getattr(pyo3::intern!(py, "list"))?;
        let dict = builtins.getattr(pyo3::intern!(py, "dict"))?;
        let origins = match get("origins")? {
            Some(value) => list.call1((value,))?,
            None => PyList::empty(py).into_any(),
        };
        let attrs = match get("attrs")? {
            Some(value) => dict.call1((value,))?,
            None => PyDict::new(py).into_any(),
        };
        cls.call1((
            data.get_item("id")?,
            data.get_item("kind")?,
            data.get_item("name")?,
            data.call_method1(pyo3::intern!(py, "get"), ("parent",))?,
            data.call_method1(pyo3::intern!(py, "get"), ("relation",))?,
            data.call_method1(pyo3::intern!(py, "get"), ("description",))?,
            data.call_method1(pyo3::intern!(py, "get"), ("data_type",))?,
            data.call_method1(pyo3::intern!(py, "get"), ("path",))?,
            origins,
            attrs,
        ))
    }

    fn __repr__(&self, py: Python<'_>) -> PyResult<String> {
        const NAMES: [&str; 10] = [
            "id",
            "kind",
            "name",
            "parent",
            "relation",
            "description",
            "data_type",
            "path",
            "origins",
            "attrs",
        ];
        let mut parts = Vec::with_capacity(10);
        for (name, value) in NAMES.iter().zip(self.fields(py)) {
            parts.push(format!("{name}={}", value.repr()?));
        }
        Ok(format!("Node({})", parts.join(", ")))
    }

    fn __richcmp__<'py>(
        &self,
        py: Python<'py>,
        other: &Bound<'py, PyAny>,
        op: CompareOp,
    ) -> PyResult<Py<PyAny>> {
        let Ok(other) = other.cast::<Self>() else {
            return Ok(py.NotImplemented());
        };
        let same =
            PyTuple::new(py, self.fields(py))?.eq(PyTuple::new(py, other.borrow().fields(py))?)?;
        match op {
            CompareOp::Eq => Ok(same.into_pyobject(py)?.to_owned().into_any().unbind()),
            CompareOp::Ne => Ok((!same).into_pyobject(py)?.to_owned().into_any().unbind()),
            _ => Ok(py.NotImplemented()),
        }
    }

    fn __reduce__<'py>(
        slf: &Bound<'py, Self>,
    ) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyTuple>, Bound<'py, PyDict>)> {
        reduced(slf.as_any(), &NODE_FIELDS)
    }

    /// Sets the fields and any other attribute from `state`: what `__reduce__` gives, and what a
    /// dataclass's pickle held (its fields as a dict).
    fn __setstate__(slf: &Bound<'_, Self>, state: &Bound<'_, PyDict>) -> PyResult<()> {
        for (name, value) in state.iter() {
            slf.setattr(name.cast::<PyString>()?, value)?;
        }
        Ok(())
    }
}

/// An edge of the graph: `source -> target`, with the meaning its `kind` gives (see
/// `konstruct.core.graph`).
#[pyclass(subclass, dict, weakref, module = "konstruct.core.graph")]
pub struct Edge {
    /// Source node id.
    #[pyo3(get, set)]
    pub source: Py<PyAny>,
    /// Target node id.
    #[pyo3(get, set)]
    pub target: Py<PyAny>,
    /// Edge kind.
    #[pyo3(get, set)]
    pub kind: Py<PyAny>,
    /// Details such as `transform`, `role`, `verified` and `via`.
    #[pyo3(get, set)]
    pub attrs: Py<PyAny>,
}

#[pymethods]
impl Edge {
    #[new]
    #[pyo3(signature = (*args, **kwargs), text_signature = "(source, target, kind, attrs=None)")]
    fn new(
        py: Python<'_>,
        args: &Bound<'_, PyTuple>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
        // The arguments belong to `__init__`, which a subclass may have its own of.
        let _ = (args, kwargs);
        Ok(Self::empty(py))
    }

    #[pyo3(name = "__init__", signature = (*args, **kwargs))]
    fn init(
        &mut self,
        py: Python<'_>,
        args: &Bound<'_, PyTuple>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<()> {
        *self = Self::from_args(py, args, kwargs)?;
        Ok(())
    }

    #[pyo3(signature = (**changes))]
    fn __replace__<'py>(
        slf: &Bound<'py, Self>,
        changes: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        replaced(slf.as_any(), &EDGE_FIELDS, changes)
    }

    /// `(source, target, kind)`, which names an edge in its graph.
    #[getter]
    fn key<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
        PyTuple::new(
            py,
            [
                self.source.bind(py),
                self.target.bind(py),
                self.kind.bind(py),
            ],
        )
    }

    /// The edge as plain data: `source`, `target` and `kind`, and `attrs` when there are any.
    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let data = PyDict::new(py);
        data.set_item(pyo3::intern!(py, "source"), self.source.bind(py))?;
        data.set_item(pyo3::intern!(py, "target"), self.target.bind(py))?;
        data.set_item(pyo3::intern!(py, "kind"), self.kind.bind(py))?;
        let attrs = self.attrs.bind(py);
        if attrs.is_truthy()? {
            data.set_item(pyo3::intern!(py, "attrs"), attrs)?;
        }
        Ok(data)
    }

    /// The edge `data` describes, as `to_dict` writes it.
    #[classmethod]
    fn from_dict<'py>(
        cls: &Bound<'py, PyType>,
        data: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = cls.py();
        let attrs = data.call_method1(pyo3::intern!(py, "get"), ("attrs", PyDict::new(py)))?;
        let attrs = py
            .import(pyo3::intern!(py, "builtins"))?
            .getattr(pyo3::intern!(py, "dict"))?
            .call1((attrs,))?;
        cls.call1((
            data.get_item("source")?,
            data.get_item("target")?,
            data.get_item("kind")?,
            attrs,
        ))
    }

    fn __repr__(&self, py: Python<'_>) -> PyResult<String> {
        Ok(format!(
            "Edge(source={}, target={}, kind={}, attrs={})",
            self.source.bind(py).repr()?,
            self.target.bind(py).repr()?,
            self.kind.bind(py).repr()?,
            self.attrs.bind(py).repr()?
        ))
    }

    fn __richcmp__<'py>(
        &self,
        py: Python<'py>,
        other: &Bound<'py, PyAny>,
        op: CompareOp,
    ) -> PyResult<Py<PyAny>> {
        let Ok(other) = other.cast::<Self>() else {
            return Ok(py.NotImplemented());
        };
        let other = other.borrow();
        let fields = |edge: &Self| {
            PyTuple::new(
                py,
                [
                    edge.source.bind(py),
                    edge.target.bind(py),
                    edge.kind.bind(py),
                    edge.attrs.bind(py),
                ],
            )
        };
        let same = fields(self)?.eq(fields(&other)?)?;
        match op {
            CompareOp::Eq => Ok(same.into_pyobject(py)?.to_owned().into_any().unbind()),
            CompareOp::Ne => Ok((!same).into_pyobject(py)?.to_owned().into_any().unbind()),
            _ => Ok(py.NotImplemented()),
        }
    }

    fn __reduce__<'py>(
        slf: &Bound<'py, Self>,
    ) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyTuple>, Bound<'py, PyDict>)> {
        reduced(slf.as_any(), &EDGE_FIELDS)
    }

    /// Sets the fields and any other attribute from `state`: what `__reduce__` gives, and what a
    /// dataclass's pickle held (its fields as a dict).
    fn __setstate__(slf: &Bound<'_, Self>, state: &Bound<'_, PyDict>) -> PyResult<()> {
        for (name, value) in state.iter() {
            slf.setattr(name.cast::<PyString>()?, value)?;
        }
        Ok(())
    }
}

/// One node reached by a traversal, with the edge that reached it.
#[pyclass(subclass, dict, weakref, module = "konstruct.core.graph")]
pub struct Step {
    /// The node reached.
    #[pyo3(get, set)]
    pub node: Py<PyAny>,
    /// Steps from the start.
    #[pyo3(get, set)]
    pub depth: Py<PyAny>,
    /// The edge that reached it.
    #[pyo3(get, set)]
    pub via: Py<PyAny>,
    /// The id of the node it was reached from.
    #[pyo3(get, set)]
    pub previous: Py<PyAny>,
}

#[pymethods]
impl Step {
    #[new]
    #[pyo3(signature = (*args, **kwargs), text_signature = "(node, depth, via, previous)")]
    fn new(
        py: Python<'_>,
        args: &Bound<'_, PyTuple>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
        // The arguments belong to `__init__`, which a subclass may have its own of.
        let _ = (args, kwargs);
        Ok(Self::empty(py))
    }

    #[pyo3(name = "__init__", signature = (*args, **kwargs))]
    fn init(
        &mut self,
        py: Python<'_>,
        args: &Bound<'_, PyTuple>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<()> {
        *self = Self::from_args(py, args, kwargs)?;
        Ok(())
    }

    #[pyo3(signature = (**changes))]
    fn __replace__<'py>(
        slf: &Bound<'py, Self>,
        changes: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        replaced(slf.as_any(), &STEP_FIELDS, changes)
    }

    fn __repr__(&self, py: Python<'_>) -> PyResult<String> {
        Ok(format!(
            "Step(node={}, depth={}, via={}, previous={})",
            self.node.bind(py).repr()?,
            self.depth.bind(py).repr()?,
            self.via.bind(py).repr()?,
            self.previous.bind(py).repr()?
        ))
    }

    fn __richcmp__<'py>(
        &self,
        py: Python<'py>,
        other: &Bound<'py, PyAny>,
        op: CompareOp,
    ) -> PyResult<Py<PyAny>> {
        let Ok(other) = other.cast::<Self>() else {
            return Ok(py.NotImplemented());
        };
        let other = other.borrow();
        let fields = |step: &Self| {
            PyTuple::new(
                py,
                [
                    step.node.bind(py),
                    step.depth.bind(py),
                    step.via.bind(py),
                    step.previous.bind(py),
                ],
            )
        };
        let same = fields(self)?.eq(fields(&other)?)?;
        match op {
            CompareOp::Eq => Ok(same.into_pyobject(py)?.to_owned().into_any().unbind()),
            CompareOp::Ne => Ok((!same).into_pyobject(py)?.to_owned().into_any().unbind()),
            _ => Ok(py.NotImplemented()),
        }
    }

    fn __reduce__<'py>(
        slf: &Bound<'py, Self>,
    ) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyTuple>, Bound<'py, PyDict>)> {
        reduced(slf.as_any(), &STEP_FIELDS)
    }

    /// Sets the fields and any other attribute from `state`: what `__reduce__` gives, and what a
    /// dataclass's pickle held (its fields as a dict).
    fn __setstate__(slf: &Bound<'_, Self>, state: &Bound<'_, PyDict>) -> PyResult<()> {
        for (name, value) in state.iter() {
            slf.setattr(name.cast::<PyString>()?, value)?;
        }
        Ok(())
    }
}

const NODE_FIELDS: [&str; 10] = [
    "id",
    "kind",
    "name",
    "parent",
    "relation",
    "description",
    "data_type",
    "path",
    "origins",
    "attrs",
];
const EDGE_FIELDS: [&str; 4] = ["source", "target", "kind", "attrs"];
const STEP_FIELDS: [&str; 4] = ["node", "depth", "via", "previous"];

/// The arguments of a call bound to `names`, the first `required` of which must be given.
fn bind<const N: usize>(
    class: &str,
    names: [&str; N],
    required: usize,
    args: &Bound<'_, PyTuple>,
    kwargs: Option<&Bound<'_, PyDict>>,
) -> PyResult<[Option<Py<PyAny>>; N]> {
    let mut found: [Option<Py<PyAny>>; N] = std::array::from_fn(|_| None);
    if args.len() > N {
        return Err(PyTypeError::new_err(format!(
            "{class}() takes at most {N} arguments ({} given)",
            args.len()
        )));
    }
    for (slot, value) in found.iter_mut().zip(args.iter()) {
        *slot = Some(value.unbind());
    }
    if let Some(kwargs) = kwargs {
        for (key, value) in kwargs.iter() {
            let key: String = key.extract()?;
            let Some(at) = names.iter().position(|n| *n == key) else {
                return Err(PyTypeError::new_err(format!(
                    "{class}() got an unexpected keyword argument '{key}'"
                )));
            };
            if found[at].is_some() {
                return Err(PyTypeError::new_err(format!(
                    "{class}() got multiple values for argument '{key}'"
                )));
            }
            found[at] = Some(value.unbind());
        }
    }
    if let Some(at) = found[..required].iter().position(Option::is_none) {
        return Err(PyTypeError::new_err(format!(
            "{class}() missing required argument: '{}'",
            names[at]
        )));
    }
    Ok(found)
}

/// `type(obj)(**{every field, then the changes})`, as `dataclasses.replace` makes a copy.
fn replaced<'py>(
    obj: &Bound<'py, PyAny>,
    names: &[&str],
    changes: Option<&Bound<'py, PyDict>>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = obj.py();
    let all = PyDict::new(py);
    for name in names {
        all.set_item(name, obj.getattr(*name)?)?;
    }
    if let Some(changes) = changes {
        all.update(changes.as_mapping())?;
    }
    obj.get_type().call((), Some(&all))
}

impl Node {
    fn empty(py: Python<'_>) -> Self {
        Self::build(py, std::array::from_fn(|_| None))
    }

    fn build(py: Python<'_>, found: [Option<Py<PyAny>>; 10]) -> Self {
        let [
            id,
            kind,
            name,
            parent,
            relation,
            description,
            data_type,
            path,
            origins,
            attrs,
        ] = found;
        let none = || py.None();
        Self {
            id: id.unwrap_or_else(none),
            kind: kind.unwrap_or_else(none),
            name: name.unwrap_or_else(none),
            parent: parent.unwrap_or_else(none),
            relation: relation.unwrap_or_else(none),
            description: description.unwrap_or_else(none),
            data_type: data_type.unwrap_or_else(none),
            path: path.unwrap_or_else(none),
            origins: origins.unwrap_or_else(|| PyList::empty(py).into_any().unbind()),
            attrs: attrs.unwrap_or_else(|| PyDict::new(py).into_any().unbind()),
        }
    }

    fn from_args(
        py: Python<'_>,
        args: &Bound<'_, PyTuple>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
        Ok(Self::build(py, bind("Node", NODE_FIELDS, 3, args, kwargs)?))
    }
}

impl Edge {
    fn empty(py: Python<'_>) -> Self {
        Self::build(py, std::array::from_fn(|_| None))
    }

    fn build(py: Python<'_>, found: [Option<Py<PyAny>>; 4]) -> Self {
        let [source, target, kind, attrs] = found;
        let none = || py.None();
        Self {
            source: source.unwrap_or_else(none),
            target: target.unwrap_or_else(none),
            kind: kind.unwrap_or_else(none),
            attrs: attrs.unwrap_or_else(|| PyDict::new(py).into_any().unbind()),
        }
    }

    fn from_args(
        py: Python<'_>,
        args: &Bound<'_, PyTuple>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
        Ok(Self::build(py, bind("Edge", EDGE_FIELDS, 3, args, kwargs)?))
    }
}

impl Step {
    fn empty(py: Python<'_>) -> Self {
        let none = || py.None();
        Self {
            node: none(),
            depth: none(),
            via: none(),
            previous: none(),
        }
    }

    fn from_args(
        py: Python<'_>,
        args: &Bound<'_, PyTuple>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
        let [node, depth, via, previous] = bind("Step", STEP_FIELDS, 4, args, kwargs)?;
        let none = || py.None();
        Ok(Self {
            node: node.unwrap_or_else(none),
            depth: depth.unwrap_or_else(none),
            via: via.unwrap_or_else(none),
            previous: previous.unwrap_or_else(none),
        })
    }
}

/// Gives `Node`, `Edge` and `Step` what `@dataclass` gave them, so that `dataclasses.fields`,
/// `asdict`, `replace` and `is_dataclass`, and pattern matching, work on them as they always did:
/// the metadata of a throwaway dataclass with the same fields is copied onto each class.
pub fn install_dataclass_metadata<'py>(
    py: Python<'py>,
    module: &Bound<'py, PyModule>,
) -> PyResult<()> {
    let dataclasses = py.import("dataclasses")?;
    let field = dataclasses.getattr("field")?;
    let list = py.get_type::<PyList>();
    let dict = py.get_type::<PyDict>();
    let none = py.None().into_bound(py);
    let spec = |name: &str, kind: &str, default: Option<&Bound<'py, PyAny>>| {
        spec(py, &field, &list, &dict, name, kind, default)
    };
    let classes: [(&str, Vec<Bound<'_, PyTuple>>); 3] = [
        (
            "Node",
            vec![
                spec("id", "str", None)?,
                spec("kind", "NodeKind", None)?,
                spec("name", "str", None)?,
                spec("parent", "str | None", Some(&none))?,
                spec("relation", "str | None", Some(&none))?,
                spec("description", "str | None", Some(&none))?,
                spec("data_type", "str | None", Some(&none))?,
                spec("path", "str | None", Some(&none))?,
                spec("origins", "list[str]", Some(list.as_any()))?,
                spec("attrs", "dict[str, Any]", Some(dict.as_any()))?,
            ],
        ),
        (
            "Edge",
            vec![
                spec("source", "str", None)?,
                spec("target", "str", None)?,
                spec("kind", "EdgeKind", None)?,
                spec("attrs", "dict[str, Any]", Some(dict.as_any()))?,
            ],
        ),
        (
            "Step",
            vec![
                spec("node", "Node", None)?,
                spec("depth", "int", None)?,
                spec("via", "Edge | None", None)?,
                spec("previous", "str | None", None)?,
            ],
        ),
    ];
    for (name, fields) in classes {
        let made = dataclasses
            .getattr("make_dataclass")?
            .call1((name, fields))?;
        let ours = module.getattr(name)?;
        for attribute in [
            "__dataclass_fields__",
            "__dataclass_params__",
            "__match_args__",
            "__annotations__",
        ] {
            ours.setattr(attribute, made.getattr(attribute)?)?;
        }
    }
    Ok(())
}

/// One field of the throwaway dataclass: `(name, type)`, with a default or a default factory.
fn spec<'py>(
    py: Python<'py>,
    field: &Bound<'py, PyAny>,
    list: &Bound<'py, PyType>,
    dict: &Bound<'py, PyType>,
    name: &str,
    kind: &str,
    default: Option<&Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyTuple>> {
    let mut items = vec![
        PyString::new(py, name).into_any(),
        PyString::new(py, kind).into_any(),
    ];
    if let Some(default) = default {
        if default.is(list) || default.is(dict) {
            let options = PyDict::new(py);
            options.set_item("default_factory", default)?;
            items.push(field.call((), Some(&options))?);
        } else {
            items.push(default.clone());
        }
    }
    PyTuple::new(py, items)
}

/// `copyreg.__newobj__(type(obj))` and a state of the fields and the instance's own attributes, so
/// that copying and pickling a subclass with an `__init__` of its own never calls it.
pub(crate) fn reduced<'py>(
    obj: &Bound<'py, PyAny>,
    names: &[&str],
) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyTuple>, Bound<'py, PyDict>)> {
    let py = obj.py();
    let state = PyDict::new(py);
    for name in names {
        state.set_item(name, obj.getattr(*name)?)?;
    }
    if let Ok(own) = obj.getattr("__dict__")
        && let Ok(own) = own.cast::<PyDict>()
    {
        state.update(own.as_mapping())?;
    }
    let new = py.import("copyreg")?.getattr("__newobj__")?;
    Ok((new, PyTuple::new(py, [obj.get_type().into_any()])?, state))
}
