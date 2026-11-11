//! `konstruct._core.scan_python_files`: which Python files declare Airflow DAGs or Dagster
//! definitions.

use std::path::PathBuf;

use pyo3::prelude::*;

/// For each path, whether the file looks like an Airflow DAG file and whether it looks like a
/// Dagster file, decided from its bytes, with the GIL released and the files read in parallel. A
/// file that cannot be read is neither.
#[pyfunction]
pub fn scan_python_files(py: Python<'_>, paths: Vec<PathBuf>) -> Vec<(bool, bool)> {
    py.detach(|| super::classify_files(&paths))
        .into_iter()
        .map(|bits| (bits & super::DAG_FILE != 0, bits & super::DAGSTER_FILE != 0))
        .collect()
}
