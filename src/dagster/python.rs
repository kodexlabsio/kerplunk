//! `konstruct.inputs.dagster`: the records the reader returns, and its functions.
//!
//! The records keep Python lists for their collections, so the objects behave as dataclasses do:
//! a list or an object taken from one is the one it holds, and editing it edits the record.
//! `konstruct/_core.pyi` describes them to type checkers.

use std::path::PathBuf;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyModule, PyType};

use crate::pysyntax::python::{
    deep_copy, list_or_new, read_error, reduce, repr_of, same_fields, shallow_copy, strings,
};

use super::{
    AssetInfo as Asset, DagsterFile as File, JobInfo as Job, OpCall as Call, OpInfo as Op,
    SensorInfo as Sensor,
};

/// A copy of a Python list, as `dataclasses.asdict` copies one.
fn copied<'py>(list: &Bound<'py, PyList>) -> PyResult<Bound<'py, PyList>> {
    PyList::new(list.py(), list.iter())
}

/// A list of each of `items` made into an object.
fn objects<'py, T: pyo3::PyClass + Into<pyo3::PyClassInitializer<T>>>(
    py: Python<'py>,
    items: Vec<T>,
) -> PyResult<Bound<'py, PyList>> {
    let made = items
        .into_iter()
        .map(|item| Py::new(py, item))
        .collect::<PyResult<Vec<_>>>()?;
    PyList::new(py, made)
}

/// A list of the dicts of a list of records.
fn dicts<'py, T: pyo3::PyClass<Frozen = pyo3::pyclass::boolean_struct::False>>(
    list: &Bound<'py, PyList>,
    to_dict: impl Fn(&T, Python<'py>) -> PyResult<Bound<'py, PyDict>>,
) -> PyResult<Bound<'py, PyList>> {
    let py = list.py();
    let out = PyList::empty(py);
    for item in list.iter() {
        out.append(to_dict(&*item.cast::<T>()?.borrow(), py)?)?;
    }
    Ok(out)
}

/// An asset, or a dbt selection that builds many.
#[pyclass(module = "konstruct.inputs.dagster")]
pub struct AssetInfo {
    #[pyo3(get, set)]
    key: String,
    #[pyo3(get, set)]
    function: String,
    #[pyo3(get, set)]
    line: usize,
    #[pyo3(get, set)]
    group: Option<String>,
    #[pyo3(get, set)]
    description: Option<String>,
    #[pyo3(get, set)]
    deps: Py<PyList>,
    #[pyo3(get, set)]
    sql: Option<String>,
    #[pyo3(get, set)]
    kinds: Py<PyList>,
    #[pyo3(get, set)]
    dbt_select: Option<Py<PyList>>,
    #[pyo3(get, set)]
    dbt_exclude: Py<PyList>,
}

const ASSET_FIELDS: [&str; 10] = [
    "key",
    "function",
    "line",
    "group",
    "description",
    "deps",
    "sql",
    "kinds",
    "dbt_select",
    "dbt_exclude",
];

impl AssetInfo {
    fn make(py: Python<'_>, found: Asset) -> PyResult<Self> {
        Ok(Self {
            key: found.key,
            function: found.function,
            line: found.line,
            group: found.group,
            description: found.description,
            deps: strings(py, &found.deps)?.unbind(),
            sql: found.sql,
            kinds: strings(py, &found.kinds)?.unbind(),
            dbt_select: found
                .dbt_select
                .map(|s| strings(py, &s).map(Bound::unbind))
                .transpose()?,
            dbt_exclude: strings(py, &found.dbt_exclude)?.unbind(),
        })
    }

    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let out = PyDict::new(py);
        out.set_item("key", &self.key)?;
        out.set_item("function", &self.function)?;
        out.set_item("line", self.line)?;
        out.set_item("group", &self.group)?;
        out.set_item("description", &self.description)?;
        out.set_item("deps", copied(self.deps.bind(py))?)?;
        out.set_item("sql", &self.sql)?;
        out.set_item("kinds", copied(self.kinds.bind(py))?)?;
        match &self.dbt_select {
            Some(select) => out.set_item("dbt_select", copied(select.bind(py))?)?,
            None => out.set_item("dbt_select", py.None())?,
        }
        out.set_item("dbt_exclude", copied(self.dbt_exclude.bind(py))?)?;
        Ok(out)
    }
}

#[pymethods]
impl AssetInfo {
    #[new]
    #[pyo3(signature = (
        key, function, line, group=None, description=None, deps=None, sql=None, kinds=None,
        dbt_select=None, dbt_exclude=None
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        key: String,
        function: String,
        line: usize,
        group: Option<String>,
        description: Option<String>,
        deps: Option<Bound<'_, PyList>>,
        sql: Option<String>,
        kinds: Option<Bound<'_, PyList>>,
        dbt_select: Option<Bound<'_, PyList>>,
        dbt_exclude: Option<Bound<'_, PyList>>,
    ) -> Self {
        Self {
            key,
            function,
            line,
            group,
            description,
            deps: list_or_new(py, deps).unbind(),
            sql,
            kinds: list_or_new(py, kinds).unbind(),
            dbt_select: dbt_select.map(Bound::unbind),
            dbt_exclude: list_or_new(py, dbt_exclude).unbind(),
        }
    }

    fn __eq__(slf: &Bound<'_, Self>, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        same_fields(slf.as_any(), other, &ASSET_FIELDS)
    }

    fn __repr__(slf: &Bound<'_, Self>) -> PyResult<String> {
        repr_of(slf.as_any(), "AssetInfo", &ASSET_FIELDS)
    }

    fn __copy__<'py>(slf: &Bound<'py, Self>) -> PyResult<Bound<'py, PyAny>> {
        shallow_copy(slf.as_any(), &ASSET_FIELDS)
    }

    fn __deepcopy__<'py>(
        slf: &Bound<'py, Self>,
        memo: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        deep_copy(slf.as_any(), &ASSET_FIELDS, memo)
    }

    fn __reduce__<'py>(
        slf: &Bound<'py, Self>,
    ) -> PyResult<(Bound<'py, PyType>, Bound<'py, pyo3::types::PyTuple>)> {
        reduce(slf.as_any(), &ASSET_FIELDS)
    }
}

/// An `@op` definition. It runs once per invocation in a job; see `OpCall`.
#[pyclass(module = "konstruct.inputs.dagster")]
pub struct OpInfo {
    #[pyo3(get, set)]
    name: String,
    #[pyo3(get, set)]
    function: String,
    #[pyo3(get, set)]
    line: usize,
    #[pyo3(get, set)]
    description: Option<String>,
    #[pyo3(get, set)]
    sql: Option<String>,
    #[pyo3(get, set)]
    kinds: Py<PyList>,
    #[pyo3(get, set)]
    dbt_commands: Py<PyList>,
}

const OP_FIELDS: [&str; 7] = [
    "name",
    "function",
    "line",
    "description",
    "sql",
    "kinds",
    "dbt_commands",
];

impl OpInfo {
    fn make(py: Python<'_>, found: Op) -> PyResult<Self> {
        Ok(Self {
            name: found.name,
            function: found.function,
            line: found.line,
            description: found.description,
            sql: found.sql,
            kinds: strings(py, &found.kinds)?.unbind(),
            dbt_commands: strings(py, &found.dbt_commands)?.unbind(),
        })
    }

    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let out = PyDict::new(py);
        out.set_item("name", &self.name)?;
        out.set_item("function", &self.function)?;
        out.set_item("line", self.line)?;
        out.set_item("description", &self.description)?;
        out.set_item("sql", &self.sql)?;
        out.set_item("kinds", copied(self.kinds.bind(py))?)?;
        out.set_item("dbt_commands", copied(self.dbt_commands.bind(py))?)?;
        Ok(out)
    }
}

#[pymethods]
impl OpInfo {
    #[new]
    #[pyo3(signature = (name, function, line, description=None, sql=None, kinds=None, dbt_commands=None))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        name: String,
        function: String,
        line: usize,
        description: Option<String>,
        sql: Option<String>,
        kinds: Option<Bound<'_, PyList>>,
        dbt_commands: Option<Bound<'_, PyList>>,
    ) -> Self {
        Self {
            name,
            function,
            line,
            description,
            sql,
            kinds: list_or_new(py, kinds).unbind(),
            dbt_commands: list_or_new(py, dbt_commands).unbind(),
        }
    }

    fn __eq__(slf: &Bound<'_, Self>, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        same_fields(slf.as_any(), other, &OP_FIELDS)
    }

    fn __repr__(slf: &Bound<'_, Self>) -> PyResult<String> {
        repr_of(slf.as_any(), "OpInfo", &OP_FIELDS)
    }

    fn __copy__<'py>(slf: &Bound<'py, Self>) -> PyResult<Bound<'py, PyAny>> {
        shallow_copy(slf.as_any(), &OP_FIELDS)
    }

    fn __deepcopy__<'py>(
        slf: &Bound<'py, Self>,
        memo: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        deep_copy(slf.as_any(), &OP_FIELDS, memo)
    }

    fn __reduce__<'py>(
        slf: &Bound<'py, Self>,
    ) -> PyResult<(Bound<'py, PyType>, Bound<'py, pyo3::types::PyTuple>)> {
        reduce(slf.as_any(), &OP_FIELDS)
    }
}

/// One invocation of an op in a job, by its handle, and the handles it takes inputs from.
#[pyclass(module = "konstruct.inputs.dagster")]
pub struct OpCall {
    #[pyo3(get, set)]
    handle: String,
    #[pyo3(get, set)]
    op: String,
    #[pyo3(get, set)]
    upstream: Py<PyList>,
}

const CALL_FIELDS: [&str; 3] = ["handle", "op", "upstream"];

impl OpCall {
    fn make(py: Python<'_>, found: Call) -> PyResult<Self> {
        Ok(Self {
            handle: found.handle,
            op: found.op,
            upstream: strings(py, &found.upstream)?.unbind(),
        })
    }

    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let out = PyDict::new(py);
        out.set_item("handle", &self.handle)?;
        out.set_item("op", &self.op)?;
        out.set_item("upstream", copied(self.upstream.bind(py))?)?;
        Ok(out)
    }
}

#[pymethods]
impl OpCall {
    #[new]
    #[pyo3(signature = (handle, op, upstream=None))]
    fn new(
        py: Python<'_>,
        handle: String,
        op: String,
        upstream: Option<Bound<'_, PyList>>,
    ) -> Self {
        Self {
            handle,
            op,
            upstream: list_or_new(py, upstream).unbind(),
        }
    }

    fn __eq__(slf: &Bound<'_, Self>, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        same_fields(slf.as_any(), other, &CALL_FIELDS)
    }

    fn __repr__(slf: &Bound<'_, Self>) -> PyResult<String> {
        repr_of(slf.as_any(), "OpCall", &CALL_FIELDS)
    }

    fn __copy__<'py>(slf: &Bound<'py, Self>) -> PyResult<Bound<'py, PyAny>> {
        shallow_copy(slf.as_any(), &CALL_FIELDS)
    }

    fn __deepcopy__<'py>(
        slf: &Bound<'py, Self>,
        memo: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        deep_copy(slf.as_any(), &CALL_FIELDS, memo)
    }

    fn __reduce__<'py>(
        slf: &Bound<'py, Self>,
    ) -> PyResult<(Bound<'py, PyType>, Bound<'py, pyo3::types::PyTuple>)> {
        reduce(slf.as_any(), &CALL_FIELDS)
    }
}

/// A job: the assets it selects, or the ops it runs.
#[pyclass(module = "konstruct.inputs.dagster")]
pub struct JobInfo {
    #[pyo3(get, set)]
    name: String,
    #[pyo3(get, set)]
    line: usize,
    #[pyo3(get, set)]
    selection: Py<PyList>,
    #[pyo3(get, set)]
    schedule: Option<String>,
    #[pyo3(get, set)]
    description: Option<String>,
    /// For op-based jobs, the op invocations; asset jobs have a selection instead.
    #[pyo3(get, set)]
    ops: Py<PyList>,
}

const JOB_FIELDS: [&str; 6] = [
    "name",
    "line",
    "selection",
    "schedule",
    "description",
    "ops",
];

impl JobInfo {
    fn make(py: Python<'_>, found: Job) -> PyResult<Self> {
        let calls = found
            .ops
            .into_iter()
            .map(|c| OpCall::make(py, c))
            .collect::<PyResult<Vec<_>>>()?;
        Ok(Self {
            name: found.name,
            line: found.line,
            selection: strings(py, &found.selection)?.unbind(),
            schedule: found.schedule,
            description: found.description,
            ops: objects(py, calls)?.unbind(),
        })
    }

    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let out = PyDict::new(py);
        out.set_item("name", &self.name)?;
        out.set_item("line", self.line)?;
        out.set_item("selection", copied(self.selection.bind(py))?)?;
        out.set_item("schedule", &self.schedule)?;
        out.set_item("description", &self.description)?;
        out.set_item("ops", dicts::<OpCall>(self.ops.bind(py), OpCall::to_dict)?)?;
        Ok(out)
    }
}

#[pymethods]
impl JobInfo {
    #[new]
    #[pyo3(signature = (name, line, selection=None, schedule=None, description=None, ops=None))]
    fn new(
        py: Python<'_>,
        name: String,
        line: usize,
        selection: Option<Bound<'_, PyList>>,
        schedule: Option<String>,
        description: Option<String>,
        ops: Option<Bound<'_, PyList>>,
    ) -> Self {
        Self {
            name,
            line,
            selection: list_or_new(py, selection).unbind(),
            schedule,
            description,
            ops: list_or_new(py, ops).unbind(),
        }
    }

    fn __eq__(slf: &Bound<'_, Self>, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        same_fields(slf.as_any(), other, &JOB_FIELDS)
    }

    fn __repr__(slf: &Bound<'_, Self>) -> PyResult<String> {
        repr_of(slf.as_any(), "JobInfo", &JOB_FIELDS)
    }

    fn __copy__<'py>(slf: &Bound<'py, Self>) -> PyResult<Bound<'py, PyAny>> {
        shallow_copy(slf.as_any(), &JOB_FIELDS)
    }

    fn __deepcopy__<'py>(
        slf: &Bound<'py, Self>,
        memo: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        deep_copy(slf.as_any(), &JOB_FIELDS, memo)
    }

    fn __reduce__<'py>(
        slf: &Bound<'py, Self>,
    ) -> PyResult<(Bound<'py, PyType>, Bound<'py, pyo3::types::PyTuple>)> {
        reduce(slf.as_any(), &JOB_FIELDS)
    }
}

/// A sensor, the jobs it starts and what it watches.
#[pyclass(module = "konstruct.inputs.dagster")]
pub struct SensorInfo {
    #[pyo3(get, set)]
    name: String,
    #[pyo3(get, set)]
    kind: String,
    #[pyo3(get, set)]
    line: usize,
    /// The jobs the sensor starts.
    #[pyo3(get, set)]
    jobs: Py<PyList>,
    /// The assets whose materialisations it watches.
    #[pyo3(get, set)]
    assets: Py<PyList>,
    /// For run status sensors, the jobs whose runs it watches.
    #[pyo3(get, set)]
    monitored_jobs: Py<PyList>,
    #[pyo3(get, set)]
    status: Option<String>,
    #[pyo3(get, set)]
    description: Option<String>,
}

const SENSOR_FIELDS: [&str; 8] = [
    "name",
    "kind",
    "line",
    "jobs",
    "assets",
    "monitored_jobs",
    "status",
    "description",
];

impl SensorInfo {
    fn make(py: Python<'_>, found: Sensor) -> PyResult<Self> {
        Ok(Self {
            name: found.name,
            kind: found.kind,
            line: found.line,
            jobs: strings(py, &found.jobs)?.unbind(),
            assets: strings(py, &found.assets)?.unbind(),
            monitored_jobs: strings(py, &found.monitored_jobs)?.unbind(),
            status: found.status,
            description: found.description,
        })
    }

    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let out = PyDict::new(py);
        out.set_item("name", &self.name)?;
        out.set_item("kind", &self.kind)?;
        out.set_item("line", self.line)?;
        out.set_item("jobs", copied(self.jobs.bind(py))?)?;
        out.set_item("assets", copied(self.assets.bind(py))?)?;
        out.set_item("monitored_jobs", copied(self.monitored_jobs.bind(py))?)?;
        out.set_item("status", &self.status)?;
        out.set_item("description", &self.description)?;
        Ok(out)
    }
}

#[pymethods]
impl SensorInfo {
    #[new]
    #[pyo3(signature = (
        name, kind, line, jobs=None, assets=None, monitored_jobs=None, status=None, description=None
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        name: String,
        kind: String,
        line: usize,
        jobs: Option<Bound<'_, PyList>>,
        assets: Option<Bound<'_, PyList>>,
        monitored_jobs: Option<Bound<'_, PyList>>,
        status: Option<String>,
        description: Option<String>,
    ) -> Self {
        Self {
            name,
            kind,
            line,
            jobs: list_or_new(py, jobs).unbind(),
            assets: list_or_new(py, assets).unbind(),
            monitored_jobs: list_or_new(py, monitored_jobs).unbind(),
            status,
            description,
        }
    }

    fn __eq__(slf: &Bound<'_, Self>, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        same_fields(slf.as_any(), other, &SENSOR_FIELDS)
    }

    fn __repr__(slf: &Bound<'_, Self>) -> PyResult<String> {
        repr_of(slf.as_any(), "SensorInfo", &SENSOR_FIELDS)
    }

    fn __copy__<'py>(slf: &Bound<'py, Self>) -> PyResult<Bound<'py, PyAny>> {
        shallow_copy(slf.as_any(), &SENSOR_FIELDS)
    }

    fn __deepcopy__<'py>(
        slf: &Bound<'py, Self>,
        memo: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        deep_copy(slf.as_any(), &SENSOR_FIELDS, memo)
    }

    fn __reduce__<'py>(
        slf: &Bound<'py, Self>,
    ) -> PyResult<(Bound<'py, PyType>, Bound<'py, pyo3::types::PyTuple>)> {
        reduce(slf.as_any(), &SENSOR_FIELDS)
    }
}

/// What one Dagster file declares.
#[pyclass(module = "konstruct.inputs.dagster")]
pub struct DagsterFile {
    #[pyo3(get, set)]
    path: String,
    #[pyo3(get, set)]
    assets: Py<PyList>,
    #[pyo3(get, set)]
    jobs: Py<PyList>,
    #[pyo3(get, set)]
    warnings: Py<PyList>,
    #[pyo3(get, set)]
    ops: Py<PyList>,
    #[pyo3(get, set)]
    sensors: Py<PyList>,
}

const FILE_FIELDS: [&str; 6] = ["path", "assets", "jobs", "warnings", "ops", "sensors"];

impl DagsterFile {
    fn make(py: Python<'_>, found: File) -> PyResult<Self> {
        let assets = found
            .assets
            .into_iter()
            .map(|a| AssetInfo::make(py, a))
            .collect::<PyResult<Vec<_>>>()?;
        let jobs = found
            .jobs
            .into_iter()
            .map(|j| JobInfo::make(py, j))
            .collect::<PyResult<Vec<_>>>()?;
        let ops = found
            .ops
            .into_iter()
            .map(|o| OpInfo::make(py, o))
            .collect::<PyResult<Vec<_>>>()?;
        let sensors = found
            .sensors
            .into_iter()
            .map(|s| SensorInfo::make(py, s))
            .collect::<PyResult<Vec<_>>>()?;
        Ok(Self {
            path: found.path,
            assets: objects(py, assets)?.unbind(),
            jobs: objects(py, jobs)?.unbind(),
            warnings: strings(py, &found.warnings)?.unbind(),
            ops: objects(py, ops)?.unbind(),
            sensors: objects(py, sensors)?.unbind(),
        })
    }
}

/// Each dict of `listed` made into an object of `class`.
fn built<'py>(
    py: Python<'py>,
    listed: Option<Bound<'py, PyAny>>,
    class: &Bound<'py, PyType>,
    inner: impl Fn(&Bound<'py, PyDict>) -> PyResult<()>,
) -> PyResult<Bound<'py, PyList>> {
    let out = PyList::empty(py);
    if let Some(listed) = listed {
        for item in listed.try_iter()? {
            let fields = item?.cast::<PyDict>()?.copy()?;
            inner(&fields)?;
            out.append(class.call((), Some(&fields))?)?;
        }
    }
    Ok(out)
}

#[pymethods]
impl DagsterFile {
    #[new]
    #[pyo3(signature = (path, assets=None, jobs=None, warnings=None, ops=None, sensors=None))]
    fn new(
        py: Python<'_>,
        path: String,
        assets: Option<Bound<'_, PyList>>,
        jobs: Option<Bound<'_, PyList>>,
        warnings: Option<Bound<'_, PyList>>,
        ops: Option<Bound<'_, PyList>>,
        sensors: Option<Bound<'_, PyList>>,
    ) -> Self {
        Self {
            path,
            assets: list_or_new(py, assets).unbind(),
            jobs: list_or_new(py, jobs).unbind(),
            warnings: list_or_new(py, warnings).unbind(),
            ops: list_or_new(py, ops).unbind(),
            sensors: list_or_new(py, sensors).unbind(),
        }
    }

    /// The record as plain dicts and lists, as `dataclasses.asdict` gives it.
    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let out = PyDict::new(py);
        out.set_item("path", &self.path)?;
        out.set_item(
            "assets",
            dicts::<AssetInfo>(self.assets.bind(py), AssetInfo::to_dict)?,
        )?;
        out.set_item(
            "jobs",
            dicts::<JobInfo>(self.jobs.bind(py), JobInfo::to_dict)?,
        )?;
        out.set_item("warnings", copied(self.warnings.bind(py))?)?;
        out.set_item("ops", dicts::<OpInfo>(self.ops.bind(py), OpInfo::to_dict)?)?;
        out.set_item(
            "sensors",
            dicts::<SensorInfo>(self.sensors.bind(py), SensorInfo::to_dict)?,
        )?;
        Ok(out)
    }

    /// The record `to_dict` describes.
    #[classmethod]
    fn from_dict<'py>(
        cls: &Bound<'py, PyType>,
        data: &Bound<'py, PyDict>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = cls.py();
        let calls = py.get_type::<OpCall>();
        let arguments = PyDict::new(py);
        arguments.set_item(
            "path",
            data.get_item("path")?
                .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("path"))?,
        )?;
        arguments.set_item(
            "assets",
            built(
                py,
                data.get_item("assets")?,
                &py.get_type::<AssetInfo>(),
                |_| Ok(()),
            )?,
        )?;
        let jobs = built(
            py,
            data.get_item("jobs")?,
            &py.get_type::<JobInfo>(),
            |fields| {
                let listed = fields.get_item("ops")?;
                fields.set_item("ops", built(py, listed, &calls, |_| Ok(()))?)
            },
        )?;
        arguments.set_item("jobs", jobs)?;
        let warnings = match data.get_item("warnings")? {
            Some(found) => PyList::new(py, found.try_iter()?.collect::<PyResult<Vec<_>>>()?)?,
            None => PyList::empty(py),
        };
        arguments.set_item("warnings", warnings)?;
        arguments.set_item(
            "ops",
            built(py, data.get_item("ops")?, &py.get_type::<OpInfo>(), |_| {
                Ok(())
            })?,
        )?;
        arguments.set_item(
            "sensors",
            built(
                py,
                data.get_item("sensors")?,
                &py.get_type::<SensorInfo>(),
                |_| Ok(()),
            )?,
        )?;
        cls.call((), Some(&arguments))
    }

    fn __eq__(slf: &Bound<'_, Self>, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        same_fields(slf.as_any(), other, &FILE_FIELDS)
    }

    fn __repr__(slf: &Bound<'_, Self>) -> PyResult<String> {
        repr_of(slf.as_any(), "DagsterFile", &FILE_FIELDS)
    }

    fn __copy__<'py>(slf: &Bound<'py, Self>) -> PyResult<Bound<'py, PyAny>> {
        shallow_copy(slf.as_any(), &FILE_FIELDS)
    }

    fn __deepcopy__<'py>(
        slf: &Bound<'py, Self>,
        memo: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        deep_copy(slf.as_any(), &FILE_FIELDS, memo)
    }

    fn __reduce__<'py>(
        slf: &Bound<'py, Self>,
    ) -> PyResult<(Bound<'py, PyType>, Bound<'py, pyo3::types::PyTuple>)> {
        reduce(slf.as_any(), &FILE_FIELDS)
    }
}

/// Whether `text` looks like the file of Dagster definitions.
#[pyfunction]
pub fn looks_like_dagster_file(text: &str) -> bool {
    crate::scan::looks_like_dagster_file(text.as_bytes())
}

/// The assets, jobs, ops and sensors of a Dagster file, read without importing or running it.
/// Raises `SyntaxError` for text that is not Python, or is nested too deeply to read.
#[pyfunction]
pub fn parse_dagster_file(py: Python<'_>, path: PathBuf) -> PyResult<DagsterFile> {
    let shown = path.to_string_lossy().into_owned();
    match py.detach(|| super::parse_file(&shown)) {
        Ok(found) => DagsterFile::make(py, found),
        Err(error) => Err(read_error(py, &shown, error)),
    }
}

/// The definitions of Python `source`, read as if it were the file at `path`.
#[pyfunction]
pub fn parse_dagster_source(py: Python<'_>, path: PathBuf, source: &str) -> PyResult<DagsterFile> {
    let shown = path.to_string_lossy().into_owned();
    match py.detach(|| super::parse_source(&shown, source)) {
        Ok(found) => DagsterFile::make(py, found),
        Err(error) => Err(read_error(py, &shown, error)),
    }
}

/// Adds the tables the reader is built from, which `konstruct.inputs.dagster` has always had.
pub fn add_tables(module: &Bound<'_, PyModule>) -> PyResult<()> {
    use crate::pysyntax::python::add_names;
    add_names(module, "ASSET_DECORATORS", &super::ASSET_DECORATORS)?;
    add_names(module, "SQL_METHODS", &super::SQL_METHODS)?;
    add_names(module, "SENSOR_DECORATORS", &super::SENSOR_DECORATORS)?;
    add_names(module, "OP_WRAPPERS", &super::OP_WRAPPERS)
}
