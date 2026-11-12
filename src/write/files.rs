//! Files put on disk whole: `konstruct._core.write_text` and `real_directory`, which every
//! Konstruct output is written with, and the errors they raise.

use std::io::Write as _;
use std::path::{Path, PathBuf};

use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::PyString;

/// The mode a new file gets when nothing says otherwise: what `open` gives, with the umask.
#[cfg(unix)]
fn new_mode() -> u32 {
    0o666 & !*UMASK.get().expect("read when the module was initialised")
}

#[cfg(unix)]
static UMASK: std::sync::OnceLock<u32> = std::sync::OnceLock::new();

/// Reads the umask, once, as the module is initialised, before any thread of Konstruct runs: it
/// can only be read by setting it, and setting it while another thread creates a file would give
/// that file the wrong mode.
pub(crate) fn read_umask() {
    #[cfg(unix)]
    {
        unsafe extern "C" {
            fn umask(mask: u32) -> u32;
        }
        UMASK.get_or_init(|| {
            // SAFETY: umask has no preconditions; it is set to 0 and put back at once.
            unsafe {
                let mask = umask(0);
                umask(mask);
                mask
            }
        });
    }
}

/// `error` as the `OSError` Python raises for `path`, with its errno and file name.
pub(crate) fn os_error(py: Python<'_>, error: &std::io::Error, path: &Path) -> PyErr {
    let Some(code) = error.raw_os_error() else {
        return PyErr::from(std::io::Error::new(error.kind(), error.to_string()));
    };
    let message = std::io::Error::from_raw_os_error(code).to_string();
    // Rust appends " (os error N)"; Python's message is the C library's alone.
    let message = message.split(" (os error").next().unwrap_or("").to_owned();
    let built = py.import("builtins").and_then(|builtins| {
        builtins
            .getattr("OSError")?
            .call1((code, message, path.to_string_lossy().into_owned()))
    });
    match built {
        Ok(made) => PyErr::from_value(made),
        Err(other) => other,
    }
}

/// The `UnicodeEncodeError` that encoding `text` as UTF-8 raises, which has its position.
pub(crate) fn encode_error(text: &Bound<'_, PyString>) -> PyErr {
    match text.call_method1(pyo3::intern!(text.py(), "encode"), ("utf-8",)) {
        Err(error) => error,
        Ok(_) => PyTypeError::new_err("str is not valid UTF-8"),
    }
}

/// Writes `bytes` to `path` through a new file in the same directory that is then renamed over
/// it, so a reader sees the old file or the new one, never half of one. A symbolic link at
/// `path` is replaced, not followed; a regular file keeps the mode it had. Nothing is synced to
/// disk before the rename (it adds about 30 ms to writing a 23 MB graph.json, over a third), so
/// the rename survives a crash of Konstruct but not of the machine.
pub(crate) fn write_whole(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::fs;
    let parent = match path.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => parent.to_path_buf(),
        _ => PathBuf::from("."),
    };
    fs::create_dir_all(&parent)?;
    #[cfg(unix)]
    let mode = match fs::symlink_metadata(path) {
        Ok(meta) if meta.file_type().is_file() => {
            use std::os::unix::fs::PermissionsExt;
            meta.permissions().mode() & 0o7777
        }
        _ => new_mode(),
    };
    let name = path
        .file_name()
        .ok_or_else(|| std::io::Error::other("a file name is needed"))?;
    let mut temporary = None;
    for attempt in 0u32..100 {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.subsec_nanos());
        let mut candidate = std::ffi::OsString::from(".");
        candidate.push(name);
        candidate.push(format!(".{:x}{:x}{attempt}.tmp", std::process::id(), nanos));
        let candidate = parent.join(candidate);
        let mut options = fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        match options.open(&candidate) {
            Ok(file) => {
                temporary = Some((candidate, file));
                break;
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(error),
        }
    }
    let (temporary, mut file) =
        temporary.ok_or_else(|| std::io::Error::other("no unused temporary file name"))?;
    let result = (|| {
        file.write_all(bytes)?;
        close(file)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&temporary, fs::Permissions::from_mode(mode))?;
        }
        // On Windows a reader holding the file open makes the rename fail for a moment.
        super::retry_busy(if cfg!(windows) { 5 } else { 1 }, 50, || {
            fs::rename(&temporary, path)
        })
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

/// Writes `text` to `path` whole (see [`write_whole`]), creating its directory.
#[pyfunction]
pub(crate) fn write_text(
    py: Python<'_>,
    path: PathBuf,
    text: &Bound<'_, PyString>,
) -> PyResult<()> {
    let bytes = text.to_str().map_err(|_| encode_error(text))?.as_bytes();
    py.detach(|| write_whole(&path, bytes))
        .map_err(|error| os_error(py, &error, &path))
}

/// `base` joined with `parts`, made as real directories: a symbolic link in the way is removed
/// first, so one committed inside an output directory cannot send outputs elsewhere.
#[pyfunction]
#[pyo3(signature = (base, *parts))]
pub(crate) fn real_directory(
    py: Python<'_>,
    base: PathBuf,
    parts: Vec<String>,
) -> PyResult<PathBuf> {
    use std::fs;
    let mut current = base;
    fs::create_dir_all(&current).map_err(|e| os_error(py, &e, &current))?;
    for part in parts {
        current.push(part);
        if fs::symlink_metadata(&current).is_ok_and(|m| m.file_type().is_symlink()) {
            fs::remove_file(&current)
                .or_else(|_| fs::remove_dir(&current))
                .map_err(|e| os_error(py, &e, &current))?;
        }
        match fs::create_dir(&current) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists && current.is_dir() => {
            }
            Err(error) => return Err(os_error(py, &error, &current)),
        }
    }
    Ok(current)
}

/// Closes `file`, reporting what `close` reports: a write the disk refused (a full disk, a
/// network file system) can show up only here, and a truncated file must never be renamed into
/// place.
fn close(file: std::fs::File) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        use std::os::fd::IntoRawFd;
        unsafe extern "C" {
            fn close(fd: i32) -> i32;
        }
        let descriptor = file.into_raw_fd();
        // SAFETY: the descriptor was owned by `file`, which no longer closes it.
        if unsafe { close(descriptor) } != 0 {
            let error = std::io::Error::last_os_error();
            // On Linux an interrupted close has closed the file, and closing again would close
            // another descriptor.
            if error.kind() != std::io::ErrorKind::Interrupted {
                return Err(error);
            }
        }
        Ok(())
    }
    #[cfg(not(unix))]
    {
        drop(file);
        Ok(())
    }
}
