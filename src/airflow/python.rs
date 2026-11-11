//! `konstruct.inputs.airflow`: the records the reader returns, and its functions.
//!
//! The records keep Python lists and dicts for their collections, so the objects behave as
//! dataclasses do: a list or an object taken from one is the one it holds, and editing it edits
//! the record. `konstruct/_core.pyi` describes them to type checkers.

use std::path::PathBuf;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyModule, PyType};

use crate::pysyntax::python::{
    deep_copy, dict_or_new, list_or_new, pairs, read_error, reduce, repr_of, same_fields,
    shallow_copy, strings,
};

use super::{DagInfo as Dag, DbtInvocation as Invocation, TaskInfo as Task};

/// A copy of a Python list, as `dataclasses.asdict` copies one.
fn copied<'py>(list: &Bound<'py, PyList>) -> PyResult<Bound<'py, PyList>> {
    PyList::new(list.py(), list.iter())
}

/// One `dbt <command>` a task runs, and what it selects.
#[pyclass(module = "konstruct.inputs.airflow")]
pub struct DbtInvocation {
    #[pyo3(get, set)]
    command: String,
    #[pyo3(get, set)]
    selectors: Py<PyList>,
    #[pyo3(get, set)]
    excludes: Py<PyList>,
    #[pyo3(get, set)]
    project_dir: Option<String>,
    #[pyo3(get, set)]
    unsupported: Py<PyList>,
}

const INVOCATION_FIELDS: [&str; 5] = [
    "command",
    "selectors",
    "excludes",
    "project_dir",
    "unsupported",
];

impl DbtInvocation {
    fn make(py: Python<'_>, found: &Invocation) -> PyResult<Self> {
        Ok(Self {
            command: found.command.clone(),
            selectors: strings(py, &found.selectors)?.unbind(),
            excludes: strings(py, &found.excludes)?.unbind(),
            project_dir: found.project_dir.clone(),
            unsupported: strings(py, &found.unsupported)?.unbind(),
        })
    }

    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let out = PyDict::new(py);
        out.set_item("command", &self.command)?;
        out.set_item("selectors", copied(self.selectors.bind(py))?)?;
        out.set_item("excludes", copied(self.excludes.bind(py))?)?;
        out.set_item("project_dir", &self.project_dir)?;
        out.set_item("unsupported", copied(self.unsupported.bind(py))?)?;
        Ok(out)
    }
}

#[pymethods]
impl DbtInvocation {
    #[new]
    #[pyo3(signature = (command, selectors=None, excludes=None, project_dir=None, unsupported=None))]
    fn new(
        py: Python<'_>,
        command: String,
        selectors: Option<Bound<'_, PyList>>,
        excludes: Option<Bound<'_, PyList>>,
        project_dir: Option<String>,
        unsupported: Option<Bound<'_, PyList>>,
    ) -> Self {
        Self {
            command,
            selectors: list_or_new(py, selectors).unbind(),
            excludes: list_or_new(py, excludes).unbind(),
            project_dir,
            unsupported: list_or_new(py, unsupported).unbind(),
        }
    }

    fn __eq__(slf: &Bound<'_, Self>, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        same_fields(slf.as_any(), other, &INVOCATION_FIELDS)
    }

    fn __repr__(slf: &Bound<'_, Self>) -> PyResult<String> {
        repr_of(slf.as_any(), "DbtInvocation", &INVOCATION_FIELDS)
    }

    fn __copy__<'py>(slf: &Bound<'py, Self>) -> PyResult<Bound<'py, PyAny>> {
        shallow_copy(slf.as_any(), &INVOCATION_FIELDS)
    }

    fn __deepcopy__<'py>(
        slf: &Bound<'py, Self>,
        memo: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        deep_copy(slf.as_any(), &INVOCATION_FIELDS, memo)
    }

    fn __reduce__<'py>(
        slf: &Bound<'py, Self>,
    ) -> PyResult<(Bound<'py, PyType>, Bound<'py, pyo3::types::PyTuple>)> {
        reduce(slf.as_any(), &INVOCATION_FIELDS)
    }
}

/// One task of a DAG.
#[pyclass(module = "konstruct.inputs.airflow")]
pub struct TaskInfo {
    #[pyo3(get, set)]
    task_id: String,
    #[pyo3(get, set)]
    operator: String,
    #[pyo3(get, set)]
    line: usize,
    #[pyo3(get, set)]
    doc: Option<String>,
    #[pyo3(get, set)]
    bash_command: Option<String>,
    #[pyo3(get, set)]
    sql: Option<String>,
    #[pyo3(get, set)]
    sql_path: Option<String>,
    #[pyo3(get, set)]
    dbt: Py<PyList>,
    #[pyo3(get, set)]
    reads: Py<PyList>,
    #[pyo3(get, set)]
    writes: Py<PyList>,
    #[pyo3(get, set)]
    inlets: Py<PyList>,
    #[pyo3(get, set)]
    outlets: Py<PyList>,
}

const TASK_FIELDS: [&str; 12] = [
    "task_id",
    "operator",
    "line",
    "doc",
    "bash_command",
    "sql",
    "sql_path",
    "dbt",
    "reads",
    "writes",
    "inlets",
    "outlets",
];

impl TaskInfo {
    fn make(py: Python<'_>, found: Task) -> PyResult<Self> {
        let invocations = found
            .dbt
            .iter()
            .map(|i| Py::new(py, DbtInvocation::make(py, i)?))
            .collect::<PyResult<Vec<_>>>()?;
        Ok(Self {
            task_id: found.task_id,
            operator: found.operator,
            line: found.line,
            doc: found.doc,
            bash_command: found.bash_command,
            sql: found.sql,
            sql_path: found.sql_path,
            dbt: PyList::new(py, invocations)?.unbind(),
            reads: strings(py, &found.reads)?.unbind(),
            writes: strings(py, &found.writes)?.unbind(),
            inlets: strings(py, &found.inlets)?.unbind(),
            outlets: strings(py, &found.outlets)?.unbind(),
        })
    }

    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let out = PyDict::new(py);
        out.set_item("task_id", &self.task_id)?;
        out.set_item("operator", &self.operator)?;
        out.set_item("line", self.line)?;
        out.set_item("doc", &self.doc)?;
        out.set_item("bash_command", &self.bash_command)?;
        out.set_item("sql", &self.sql)?;
        out.set_item("sql_path", &self.sql_path)?;
        let dbt = PyList::empty(py);
        for item in self.dbt.bind(py).iter() {
            dbt.append(item.cast::<DbtInvocation>()?.borrow().to_dict(py)?)?;
        }
        out.set_item("dbt", dbt)?;
        out.set_item("reads", copied(self.reads.bind(py))?)?;
        out.set_item("writes", copied(self.writes.bind(py))?)?;
        out.set_item("inlets", copied(self.inlets.bind(py))?)?;
        out.set_item("outlets", copied(self.outlets.bind(py))?)?;
        Ok(out)
    }
}

#[pymethods]
impl TaskInfo {
    #[new]
    #[pyo3(signature = (
        task_id, operator, line, doc=None, bash_command=None, sql=None, sql_path=None, dbt=None,
        reads=None, writes=None, inlets=None, outlets=None
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        task_id: String,
        operator: String,
        line: usize,
        doc: Option<String>,
        bash_command: Option<String>,
        sql: Option<String>,
        sql_path: Option<String>,
        dbt: Option<Bound<'_, PyList>>,
        reads: Option<Bound<'_, PyList>>,
        writes: Option<Bound<'_, PyList>>,
        inlets: Option<Bound<'_, PyList>>,
        outlets: Option<Bound<'_, PyList>>,
    ) -> Self {
        Self {
            task_id,
            operator,
            line,
            doc,
            bash_command,
            sql,
            sql_path,
            dbt: list_or_new(py, dbt).unbind(),
            reads: list_or_new(py, reads).unbind(),
            writes: list_or_new(py, writes).unbind(),
            inlets: list_or_new(py, inlets).unbind(),
            outlets: list_or_new(py, outlets).unbind(),
        }
    }

    fn __eq__(slf: &Bound<'_, Self>, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        same_fields(slf.as_any(), other, &TASK_FIELDS)
    }

    fn __repr__(slf: &Bound<'_, Self>) -> PyResult<String> {
        repr_of(slf.as_any(), "TaskInfo", &TASK_FIELDS)
    }

    fn __copy__<'py>(slf: &Bound<'py, Self>) -> PyResult<Bound<'py, PyAny>> {
        shallow_copy(slf.as_any(), &TASK_FIELDS)
    }

    fn __deepcopy__<'py>(
        slf: &Bound<'py, Self>,
        memo: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        deep_copy(slf.as_any(), &TASK_FIELDS, memo)
    }

    fn __reduce__<'py>(
        slf: &Bound<'py, Self>,
    ) -> PyResult<(Bound<'py, PyType>, Bound<'py, pyo3::types::PyTuple>)> {
        reduce(slf.as_any(), &TASK_FIELDS)
    }
}

/// One DAG of a file.
#[pyclass(module = "konstruct.inputs.airflow")]
pub struct DagInfo {
    #[pyo3(get, set)]
    dag_id: String,
    #[pyo3(get, set)]
    path: String,
    #[pyo3(get, set)]
    line: usize,
    #[pyo3(get, set)]
    schedule: Option<String>,
    #[pyo3(get, set)]
    description: Option<String>,
    #[pyo3(get, set)]
    tags: Py<PyList>,
    #[pyo3(get, set)]
    tasks: Py<PyDict>,
    #[pyo3(get, set)]
    dependencies: Py<PyList>,
    #[pyo3(get, set)]
    warnings: Py<PyList>,
}

const DAG_FIELDS: [&str; 9] = [
    "dag_id",
    "path",
    "line",
    "schedule",
    "description",
    "tags",
    "tasks",
    "dependencies",
    "warnings",
];

impl DagInfo {
    fn make(py: Python<'_>, found: Dag) -> PyResult<Self> {
        let tasks = PyDict::new(py);
        for task in found.tasks.into_vec() {
            let id = task.task_id.clone();
            tasks.set_item(id, Py::new(py, TaskInfo::make(py, task)?)?)?;
        }
        Ok(Self {
            dag_id: found.dag_id,
            path: found.path,
            line: found.line,
            schedule: found.schedule,
            description: found.description,
            tags: strings(py, &found.tags)?.unbind(),
            tasks: tasks.unbind(),
            dependencies: pairs(py, &found.dependencies)?.unbind(),
            warnings: strings(py, &found.warnings)?.unbind(),
        })
    }
}

#[pymethods]
impl DagInfo {
    #[new]
    #[pyo3(signature = (
        dag_id, path, line, schedule=None, description=None, tags=None, tasks=None,
        dependencies=None, warnings=None
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        dag_id: String,
        path: String,
        line: usize,
        schedule: Option<String>,
        description: Option<String>,
        tags: Option<Bound<'_, PyList>>,
        tasks: Option<Bound<'_, PyDict>>,
        dependencies: Option<Bound<'_, PyList>>,
        warnings: Option<Bound<'_, PyList>>,
    ) -> Self {
        Self {
            dag_id,
            path,
            line,
            schedule,
            description,
            tags: list_or_new(py, tags).unbind(),
            tasks: dict_or_new(py, tasks).unbind(),
            dependencies: list_or_new(py, dependencies).unbind(),
            warnings: list_or_new(py, warnings).unbind(),
        }
    }

    /// The record as plain dicts, lists and tuples, as `dataclasses.asdict` gives it.
    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let out = PyDict::new(py);
        out.set_item("dag_id", &self.dag_id)?;
        out.set_item("path", &self.path)?;
        out.set_item("line", self.line)?;
        out.set_item("schedule", &self.schedule)?;
        out.set_item("description", &self.description)?;
        out.set_item("tags", copied(self.tags.bind(py))?)?;
        let tasks = PyDict::new(py);
        for (key, task) in self.tasks.bind(py).iter() {
            tasks.set_item(key, task.cast::<TaskInfo>()?.borrow().to_dict(py)?)?;
        }
        out.set_item("tasks", tasks)?;
        out.set_item("dependencies", copied(self.dependencies.bind(py))?)?;
        out.set_item("warnings", copied(self.warnings.bind(py))?)?;
        Ok(out)
    }

    /// The record `to_dict` describes.
    #[classmethod]
    fn from_dict<'py>(
        cls: &Bound<'py, PyType>,
        data: &Bound<'py, PyDict>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = cls.py();
        let task_class = py.get_type::<TaskInfo>();
        let invocation_class = py.get_type::<DbtInvocation>();
        let arguments = data.copy()?;
        let tasks = PyDict::new(py);
        if let Some(found) = data.get_item("tasks")? {
            for (key, value) in found.cast::<PyDict>()?.iter() {
                let fields = value.cast::<PyDict>()?.copy()?;
                let invocations = PyList::empty(py);
                if let Some(listed) = fields.get_item("dbt")? {
                    for item in listed.try_iter()? {
                        invocations
                            .append(invocation_class.call((), Some(item?.cast::<PyDict>()?))?)?;
                    }
                }
                fields.set_item("dbt", invocations)?;
                tasks.set_item(key, task_class.call((), Some(&fields))?)?;
            }
        }
        arguments.set_item("tasks", tasks)?;
        let dependencies = PyList::empty(py);
        if let Some(found) = data.get_item("dependencies")? {
            for pair in found.try_iter()? {
                let ends = pair?.try_iter()?.collect::<PyResult<Vec<_>>>()?;
                if ends.len() != 2 {
                    return Err(pyo3::exceptions::PyValueError::new_err(
                        "a dependency has an upstream and a downstream task",
                    ));
                }
                dependencies.append(pyo3::types::PyTuple::new(py, ends)?)?;
            }
        }
        arguments.set_item("dependencies", dependencies)?;
        cls.call((), Some(&arguments))
    }

    fn __eq__(slf: &Bound<'_, Self>, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        same_fields(slf.as_any(), other, &DAG_FIELDS)
    }

    fn __repr__(slf: &Bound<'_, Self>) -> PyResult<String> {
        repr_of(slf.as_any(), "DagInfo", &DAG_FIELDS)
    }

    fn __copy__<'py>(slf: &Bound<'py, Self>) -> PyResult<Bound<'py, PyAny>> {
        shallow_copy(slf.as_any(), &DAG_FIELDS)
    }

    fn __deepcopy__<'py>(
        slf: &Bound<'py, Self>,
        memo: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        deep_copy(slf.as_any(), &DAG_FIELDS, memo)
    }

    fn __reduce__<'py>(
        slf: &Bound<'py, Self>,
    ) -> PyResult<(Bound<'py, PyType>, Bound<'py, pyo3::types::PyTuple>)> {
        reduce(slf.as_any(), &DAG_FIELDS)
    }
}

/// Whether `text` looks like the file of a DAG.
#[pyfunction]
pub fn looks_like_dag_file(text: &str) -> bool {
    crate::scan::looks_like_dag_file(text.as_bytes())
}

/// The DAGs of an Airflow file, read without importing or running it. Raises `SyntaxError` for
/// text that is not Python, or is nested too deeply to read.
#[pyfunction]
pub fn parse_dag_file(py: Python<'_>, path: PathBuf) -> PyResult<Vec<DagInfo>> {
    let shown = path.to_string_lossy().into_owned();
    let read = py.detach(|| super::parse_file(&shown));
    dags(py, &shown, read)
}

/// The DAGs of Python `source`, read as if it were the file at `path`.
#[pyfunction]
pub fn parse_dag_source(py: Python<'_>, path: PathBuf, source: &str) -> PyResult<Vec<DagInfo>> {
    let shown = path.to_string_lossy().into_owned();
    let read = py.detach(|| super::parse_source(&shown, source));
    dags(py, &shown, read)
}

fn dags(
    py: Python<'_>,
    path: &str,
    read: Result<Vec<Dag>, super::Error>,
) -> PyResult<Vec<DagInfo>> {
    match read {
        Ok(found) => found
            .into_iter()
            .map(|dag| DagInfo::make(py, dag))
            .collect(),
        Err(error) => Err(read_error(py, path, error)),
    }
}

/// The `dbt <command>` invocations in a shell command, with their selection flags.
#[pyfunction]
pub fn parse_dbt_commands(py: Python<'_>, command: &str) -> PyResult<Vec<DbtInvocation>> {
    super::parse_dbt_commands(command)
        .iter()
        .map(|found| DbtInvocation::make(py, found))
        .collect()
}

/// Adds the tables the reader is built from, which `konstruct.inputs.airflow` has always had.
pub fn add_tables(module: &Bound<'_, PyModule>) -> PyResult<()> {
    use crate::pysyntax::python::{add_names, add_tuple};
    add_names(module, "DAG_CLASSES", &super::DAG_CLASSES)?;
    add_names(module, "GROUP_CLASSES", &super::GROUP_CLASSES)?;
    add_names(module, "COSMOS_CLASSES", &super::COSMOS_CLASSES)?;
    add_tuple(module, "SQL_KWARGS", &super::SQL_KWARGS)?;
    add_tuple(module, "WRITE_KWARGS", &super::WRITE_KWARGS)?;
    add_tuple(module, "READ_KWARGS", &super::READ_KWARGS)?;
    add_names(module, "DBT_COMMANDS", &super::dbt::DBT_COMMANDS)?;
    module.add("UNRESOLVED", super::UNRESOLVED)?;
    module.add("GROUP", super::GROUP)
}
