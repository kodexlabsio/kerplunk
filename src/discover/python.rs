//! `konstruct._core.Tree` and the file identity functions: thin wrappers that release the GIL
//! for the walk and the questions, and give paths back as Python strings (a name that is not
//! valid text comes back with escapes, as `os.fsdecode` makes it).

use std::collections::HashSet;
use std::ffi::OsString;
use std::path::{Path, PathBuf};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use super::files::{self, Identity, digest_of};
use super::glob::Pattern;
use super::{Names, Tree as Walked};

/// An `identity` as Python holds it: size, modification and change times, device and inode.
type Fields = Vec<i128>;

/// A file in a folder as Python gets it: its name, identity and the digest of it.
type Listed = (OsString, Fields, String);

fn fields(id: &Identity) -> Fields {
    vec![
        i128::from(id.size),
        id.mtime_ns,
        id.ctime_ns,
        i128::from(id.device),
        i128::from(id.inode),
    ]
}

/// The files under a root, walked once.
#[pyclass(frozen, module = "konstruct._core")]
pub struct Tree {
    inner: Walked,
    /// Whether file names do not distinguish case (Windows), so `exclude` patterns do not either.
    fold_paths: bool,
}

#[pymethods]
impl Tree {
    /// Walks `root`, entering no directory named in `skip` or beginning with a `.` (unless
    /// `enter_hidden`). With `fold_paths`, `exclude` patterns ignore case, as `fnmatch` does on a system whose file
    /// names do.
    #[new]
    #[pyo3(signature = (root, skip, fold_paths=false, enter_hidden=false))]
    fn new(
        py: Python<'_>,
        root: PathBuf,
        skip: Vec<OsString>,
        fold_paths: bool,
        enter_hidden: bool,
    ) -> Self {
        let skip: HashSet<OsString> = skip.into_iter().collect();
        Self {
            inner: py.detach(|| Walked::walk_with(&root, &skip, enter_hidden)),
            fold_paths,
        }
    }

    /// The directories, as `os.walk` yields them.
    fn directories(&self) -> Vec<OsString> {
        self.inner
            .directories()
            .iter()
            .map(|d| self.inner.full(d).into_os_string())
            .collect()
    }

    /// The files whose names end with one of `suffixes` (whatever the case, with `fold`) or match
    /// one of `patterns`, as `(path, is a link)` in walk order, except those of the directory
    /// `skip` (from the root) and those whose path from the root matches one of `exclude`.
    #[pyo3(signature = (*, suffixes=None, patterns=None, fold=false, exclude=Vec::new(), skip=None))]
    fn files(
        &self,
        py: Python<'_>,
        suffixes: Option<Vec<String>>,
        patterns: Option<Vec<String>>,
        fold: bool,
        exclude: Vec<String>,
        skip: Option<PathBuf>,
    ) -> PyResult<Vec<(OsString, bool)>> {
        let names = names_of(suffixes, patterns, fold)?;
        let exclude: Vec<Pattern> = exclude
            .iter()
            .map(|p| Pattern::new(p, self.fold_paths))
            .collect();
        Ok(py.detach(|| {
            self.inner
                .files(&names, &exclude, skip.as_deref())
                .into_iter()
                .map(|(path, link)| (path.into_os_string(), link))
                .collect()
        }))
    }
}

/// What a question asks of a name: its suffixes or its patterns, and not both.
fn names_of(
    suffixes: Option<Vec<String>>,
    patterns: Option<Vec<String>>,
    fold: bool,
) -> PyResult<Names> {
    match (suffixes, patterns) {
        (Some(suffixes), None) => Ok(Names::Suffix {
            suffixes: if fold {
                suffixes.iter().map(|s| s.to_lowercase()).collect()
            } else {
                suffixes
            },
            fold,
        }),
        (None, Some(patterns)) => Ok(Names::Glob(
            patterns.iter().map(|p| Pattern::new(p, false)).collect(),
        )),
        _ => Err(PyValueError::new_err(
            "give suffixes or patterns, and not both",
        )),
    }
}

/// Whether a file name is one [`Tree::files`] would take for these suffixes or patterns: the one
/// rule for names, for a walk and for a file looked at on its own.
#[pyfunction]
#[pyo3(signature = (name, *, suffixes=None, patterns=None, fold=false))]
pub fn name_accepts(
    name: OsString,
    suffixes: Option<Vec<String>>,
    patterns: Option<Vec<String>>,
    fold: bool,
) -> PyResult<bool> {
    let names = names_of(suffixes, patterns, fold)?;
    Ok(super::accepts(&names, &name.to_string_lossy()))
}

/// What says the file at `path` changed (size, modification and change times in nanoseconds,
/// device, inode), or None when it is not a regular file.
#[pyfunction]
pub fn file_identity(py: Python<'_>, path: PathBuf) -> Option<Fields> {
    py.detach(|| files::identity(&path).as_ref().map(fields))
}

/// An identity in 16 hexadecimal digits, for a file only ever compared with itself.
#[pyfunction]
pub fn file_digest(found: Fields) -> String {
    digest_of(&found)
}

/// The regular files in `folder` whose names end with `extension`, each with its identity and
/// the digest of it, in the order the folder lists them; and a digest of them all. Raises
/// `OSError` when the folder cannot be listed.
#[pyfunction]
pub fn folder_files(
    py: Python<'_>,
    folder: PathBuf,
    extension: &str,
) -> PyResult<(String, Vec<Listed>)> {
    let ext = extension.as_bytes();
    let (whole, entries) = py.detach(|| {
        let entries = files::regular_files(&folder, ext)?;
        let whole = files::listing(&entries);
        std::io::Result::Ok((whole, entries))
    })?;
    Ok((
        whole,
        entries
            .into_iter()
            .map(|e| (e.name, fields(&e.identity), e.identity.digest()))
            .collect(),
    ))
}

/// The digest of the files [`folder_files`] would give, alone.
#[pyfunction]
pub fn folder_digest(py: Python<'_>, folder: PathBuf, extension: &str) -> PyResult<String> {
    let ext = extension.as_bytes();
    Ok(py.detach(|| files::regular_files(&folder, ext).map(|e| files::listing(&e)))?)
}

/// Every regular file under `folder`, with its path from `folder` and its identity.
#[pyfunction]
pub fn files_under(py: Python<'_>, folder: PathBuf) -> Vec<(OsString, Fields)> {
    py.detach(|| {
        files::under(Path::new(&folder))
            .into_iter()
            .map(|(path, id)| (path.into_os_string(), fields(&id)))
            .collect()
    })
}
