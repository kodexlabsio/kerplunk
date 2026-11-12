//! Python values as the JSON text of `graph.json`: the values a node, an edge, `meta` or the
//! warnings hold (str, int, float, bool, None, list, tuple and dict), with the layout, escaping and
//! number formatting orjson gave them. What JSON has no form for is a `TypeError`.

use pyo3::exceptions::PyTypeError;
use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

use crate::write::{float, signed, string, unsigned};

/// The deepest nesting written, as orjson allowed it.
const MAX_DEPTH: usize = 254;

/// The graph's values as JSON text indented two spaces, as orjson wrote them.
pub(crate) struct Encoder<'py> {
    pub(crate) out: Vec<u8>,
    /// Set when the fast walk met something that needs Python code to write, so that the value
    /// is written again the safe way.
    fallback: bool,
    /// Writing the safe way: containers are walked through PyO3's reference-counted iteration.
    safe: bool,
    py: std::marker::PhantomData<Python<'py>>,
}

impl<'py> Encoder<'py> {
    pub(crate) fn new() -> Self {
        Self {
            out: Vec::with_capacity(256),
            fallback: false,
            safe: false,
            py: std::marker::PhantomData,
        }
    }

    /// A line break and the indentation of `level`.
    pub(crate) fn line(&mut self, level: usize) {
        self.out.push(b'\n');
        self.out.resize(self.out.len() + 2 * level, b' ');
    }

    /// A key and its separator.
    pub(crate) fn key(&mut self, key: &str) {
        string(&mut self.out, key.as_bytes());
        self.out.extend_from_slice(b": ");
    }

    pub(crate) fn text(&mut self, text: &Bound<'py, PyString>) -> PyResult<()> {
        match text.to_str() {
            Ok(text) => {
                string(&mut self.out, text.as_bytes());
                Ok(())
            }
            Err(_) => Err(PyTypeError::new_err(
                "str is not valid UTF-8: surrogates not allowed",
            )),
        }
    }

    fn integer(&mut self, value: &Bound<'py, PyAny>) -> PyResult<()> {
        if let Ok(n) = value.extract::<i64>() {
            signed(&mut self.out, n);
            Ok(())
        } else if let Ok(n) = value.extract::<u64>() {
            unsigned(&mut self.out, n);
            Ok(())
        } else {
            Err(PyTypeError::new_err("Integer exceeds 64-bit range"))
        }
    }

    /// `value` at nesting `level`.
    pub(crate) fn value(&mut self, value: &Bound<'py, PyAny>, level: usize) -> PyResult<()> {
        if self.safe {
            return self.value_slow(value, level);
        }
        let start = self.out.len();
        // SAFETY: `value` keeps its object alive for the call, the interpreter is attached, and
        // the walk runs no Python code (see `raw`).
        match unsafe { self.raw(value.as_ptr(), level) } {
            Err(_) if self.fallback => {
                // Something in it needs Python code to write (a subclass, an enum, a date, a
                // dataclass), which could change the containers being walked: all of it is
                // written again through iteration that holds its own references.
                self.fallback = false;
                self.out.truncate(start);
                self.safe = true;
                let written = self.value_slow(value, level);
                self.safe = false;
                written
            }
            written => written,
        }
    }

    /// `object`, a borrowed reference, by its exact type, read through the C API with no
    /// reference counts touched: JSON's own types are one pointer comparison each. Anything
    /// else (a subclass, an enum, a dataclass, a date) sets `fallback` and stops, as writing it
    /// runs Python code. The walk itself runs none (no object is released, called or compared
    /// in Python), so nothing it borrows can be changed or freed under it.
    ///
    /// # Safety
    ///
    /// `object` must be a valid object that stays alive for the call, and the interpreter must
    /// be attached.
    unsafe fn raw(&mut self, object: *mut ffi::PyObject, level: usize) -> PyResult<()> {
        // SAFETY: as the caller promises; the statics are the interpreter's own types.
        unsafe {
            let kind = ffi::Py_TYPE(object);
            if kind == &raw mut ffi::PyUnicode_Type {
                return self.raw_str(object);
            }
            if kind == &raw mut ffi::PyLong_Type {
                let mut overflow = 0;
                let n = ffi::PyLong_AsLongLongAndOverflow(object, &mut overflow);
                if overflow == 0 {
                    signed(&mut self.out, n);
                    return Ok(());
                }
                if overflow > 0 {
                    let big = ffi::PyLong_AsUnsignedLongLong(object);
                    if !(big == u64::MAX && !ffi::PyErr_Occurred().is_null()) {
                        unsigned(&mut self.out, big);
                        return Ok(());
                    }
                    ffi::PyErr_Clear();
                }
                return Err(PyTypeError::new_err("Integer exceeds 64-bit range"));
            }
            if kind == &raw mut ffi::PyDict_Type {
                return self.raw_dict(object, level);
            }
            if kind == &raw mut ffi::PyList_Type {
                let len = ffi::PyList_Size(object);
                return self.raw_items(object, len, level, true);
            }
            if kind == &raw mut ffi::PyBool_Type {
                self.out.extend_from_slice(if object == ffi::Py_True() {
                    b"true"
                } else {
                    b"false"
                });
                return Ok(());
            }
            if kind == &raw mut ffi::PyTuple_Type {
                let len = ffi::PyTuple_Size(object);
                return self.raw_items(object, len, level, false);
            }
            if kind == &raw mut ffi::PyFloat_Type {
                float(&mut self.out, ffi::PyFloat_AsDouble(object));
                return Ok(());
            }
            if object == ffi::Py_None() {
                self.out.extend_from_slice(b"null");
                return Ok(());
            }
            self.fallback = true;
            Err(PyTypeError::new_err("needs Python code"))
        }
    }

    unsafe fn raw_str(&mut self, object: *mut ffi::PyObject) -> PyResult<()> {
        let mut len: ffi::Py_ssize_t = 0;
        // SAFETY: `object` is a str, kept alive by the caller; the buffer is Python's own.
        unsafe {
            let data = ffi::PyUnicode_AsUTF8AndSize(object, &mut len);
            if data.is_null() {
                ffi::PyErr_Clear();
                return Err(PyTypeError::new_err(
                    "str is not valid UTF-8: surrogates not allowed",
                ));
            }
            string(
                &mut self.out,
                std::slice::from_raw_parts(data.cast(), len as usize),
            );
        }
        Ok(())
    }

    unsafe fn raw_items(
        &mut self,
        list: *mut ffi::PyObject,
        len: ffi::Py_ssize_t,
        level: usize,
        is_list: bool,
    ) -> PyResult<()> {
        if len == 0 {
            self.out.extend_from_slice(b"[]");
            return Ok(());
        }
        if level >= MAX_DEPTH {
            return Err(PyTypeError::new_err("Recursion limit reached"));
        }
        self.out.push(b'[');
        for n in 0..len {
            if n > 0 {
                self.out.push(b',');
            }
            self.line(level + 1);
            // SAFETY: the index is in range; the item is borrowed and the list holds it.
            let item = unsafe {
                if is_list {
                    ffi::PyList_GetItem(list, n)
                } else {
                    ffi::PyTuple_GetItem(list, n)
                }
            };
            // SAFETY: as above.
            unsafe { self.raw(item, level + 1)? };
        }
        self.line(level);
        self.out.push(b']');
        Ok(())
    }

    /// A dict in insertion order: each key and value is read as it comes, borrowed.
    unsafe fn raw_dict(&mut self, dict: *mut ffi::PyObject, level: usize) -> PyResult<()> {
        // SAFETY: `dict` is a dict kept alive by the caller.
        let size = unsafe { ffi::PyDict_Size(dict) };
        if size == 0 {
            self.out.extend_from_slice(b"{}");
            return Ok(());
        }
        if level >= MAX_DEPTH {
            return Err(PyTypeError::new_err("Recursion limit reached"));
        }
        self.out.push(b'{');
        let (mut position, mut key, mut value) = (0, std::ptr::null_mut(), std::ptr::null_mut());
        let mut first = true;
        // SAFETY: PyDict_Next lends the key and value for as long as the dict is not changed,
        // which writing JSON does not do.
        while unsafe { ffi::PyDict_Next(dict, &mut position, &mut key, &mut value) } != 0 {
            if !std::mem::replace(&mut first, false) {
                self.out.push(b',');
            }
            self.line(level + 1);
            // SAFETY: as above.
            unsafe {
                if ffi::Py_TYPE(key) == &raw mut ffi::PyUnicode_Type {
                    self.raw_str(key)?;
                } else {
                    self.fallback = true;
                    return Err(PyTypeError::new_err("needs Python code"));
                }
            }
            self.out.extend_from_slice(b": ");
            // SAFETY: as above.
            unsafe { self.raw(value, level + 1)? };
        }
        self.line(level);
        self.out.push(b'}');
        Ok(())
    }

    fn value_slow(&mut self, value: &Bound<'py, PyAny>, level: usize) -> PyResult<()> {
        if value.is_none() {
            self.out.extend_from_slice(b"null");
        } else if let Ok(text) = value.cast::<PyString>() {
            self.text(text)?;
        } else if let Ok(flag) = value.cast::<PyBool>() {
            self.out
                .extend_from_slice(if flag.is_true() { b"true" } else { b"false" });
        } else if value.is_instance_of::<PyInt>() && !is_special(value)? {
            self.integer(value)?;
        } else if let Ok(number) = value.cast::<PyFloat>() {
            float(&mut self.out, number.value());
        } else if let Ok(list) = value.cast::<PyList>() {
            self.items(list.iter(), list.len(), level)?;
        } else if let Ok(tuple) = value.cast::<PyTuple>() {
            self.items(tuple.iter(), tuple.len(), level)?;
        } else if let Ok(dict) = value.cast::<PyDict>() {
            self.dict(dict, level)?;
        } else if let Some(inner) = self.special(value)? {
            if level >= MAX_DEPTH {
                return Err(PyTypeError::new_err("Recursion limit reached"));
            }
            self.value(&inner, level)?;
        } else {
            return Err(PyTypeError::new_err(format!(
                "Type is not JSON serializable: {}",
                value.get_type().name()?
            )));
        }
        Ok(())
    }

    fn items(
        &mut self,
        items: impl Iterator<Item = Bound<'py, PyAny>>,
        len: usize,
        level: usize,
    ) -> PyResult<()> {
        if len == 0 {
            self.out.extend_from_slice(b"[]");
            return Ok(());
        }
        if level >= MAX_DEPTH {
            return Err(PyTypeError::new_err("Recursion limit reached"));
        }
        self.out.push(b'[');
        for (n, item) in items.enumerate() {
            if n > 0 {
                self.out.push(b',');
            }
            self.line(level + 1);
            self.value(&item, level + 1)?;
        }
        self.line(level);
        self.out.push(b']');
        Ok(())
    }

    /// A dict that is a subclass of `dict`, in insertion order.
    fn dict(&mut self, dict: &Bound<'py, PyDict>, level: usize) -> PyResult<()> {
        if dict.is_empty() {
            self.out.extend_from_slice(b"{}");
            return Ok(());
        }
        if level >= MAX_DEPTH {
            return Err(PyTypeError::new_err("Recursion limit reached"));
        }
        self.out.push(b'{');
        // A copy of the items first: writing one may run Python code that changes the dict, and
        // iterating a dict that changes size panics.
        let items: Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)> = dict.iter().collect();
        for (n, (key, value)) in items.into_iter().enumerate() {
            if n > 0 {
                self.out.push(b',');
            }
            self.line(level + 1);
            self.out.push(b'"');
            let text = self.key_text(&key)?;
            self.out.extend_from_slice(&text);
            self.out.extend_from_slice(b"\": ");
            self.value(&value, level + 1)?;
        }
        self.line(level);
        self.out.push(b'}');
        Ok(())
    }

    /// A dict key as text, raw (for ordering) and as the inside of a JSON string: a string as it
    /// is, and a number, a boolean or None as the text its value would have.
    fn key_text(&self, key: &Bound<'py, PyAny>) -> PyResult<Vec<u8>> {
        let raw: Vec<u8> = if let Ok(text) = key.cast::<PyString>() {
            let Ok(text) = text.to_str() else {
                return Err(PyTypeError::new_err(
                    "str is not valid UTF-8: surrogates not allowed",
                ));
            };
            text.as_bytes().to_vec()
        } else if key.is_none() {
            b"null".to_vec()
        } else if let Ok(flag) = key.cast::<PyBool>() {
            (if flag.is_true() {
                &b"true"[..]
            } else {
                &b"false"[..]
            })
            .to_vec()
        } else if key.is_instance_of::<PyInt>() && !is_special(key)? {
            let mut scratch = Encoder::new();
            scratch.integer(key)?;
            scratch.out
        } else if let Ok(number) = key.cast::<PyFloat>() {
            let mut scratch = Vec::new();
            float(&mut scratch, number.value());
            scratch
        } else if let Some(inner) = self.special(key)? {
            // An enum member, a date, a time or a UUID is the text of its value.
            return self.key_text(&inner);
        } else {
            return Err(PyTypeError::new_err("Dict key must be str"));
        };
        let mut escaped = Vec::with_capacity(raw.len());
        crate::write::escape(&mut escaped, &raw);
        Ok(escaped)
    }

    /// What orjson wrote beyond JSON's own types, as the plain value that stands for it: an
    /// enum's value, a dataclass's fields, a date or time as ISO 8601 text, a UUID as its text.
    fn special(&self, value: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
        let py = value.py();
        let kinds = special_kinds(py)?;
        if value.is_instance(kinds.get_item(0)?.as_ref())? {
            return Ok(Some(value.getattr(pyo3::intern!(py, "value"))?));
        }
        let class = value.get_type();
        if class.hasattr(pyo3::intern!(py, "__dataclass_fields__"))? {
            let fields = py
                .import(pyo3::intern!(py, "dataclasses"))?
                .getattr(pyo3::intern!(py, "fields"))?
                .call1((value,))?;
            let made = PyDict::new(py);
            for field in fields.try_iter()? {
                let name = field?.getattr(pyo3::intern!(py, "name"))?;
                // A field whose name starts with an underscore is left out.
                if name.cast::<PyString>()?.to_str()?.starts_with('_') {
                    continue;
                }
                made.set_item(&name, value.getattr(name.cast::<PyString>()?)?)?;
            }
            return Ok(Some(made.into_any()));
        }
        if value.is_instance(kinds.get_item(1)?.as_ref())? {
            // The offset is written in whole minutes, rounded as orjson rounds them (minutes can reach 60); isoformat adds seconds.
            let offset = value.call_method0(pyo3::intern!(py, "utcoffset"))?;
            if offset.is_none() {
                return Ok(Some(value.call_method0(pyo3::intern!(py, "isoformat"))?));
            }
            let seconds = offset.call_method0("total_seconds")?.extract::<f64>()? as i64;
            let naive = PyDict::new(py);
            naive.set_item("tzinfo", py.None())?;
            let local = value
                .call_method(pyo3::intern!(py, "replace"), (), Some(&naive))?
                .call_method0(pyo3::intern!(py, "isoformat"))?;
            let (hours, minutes) = (seconds.abs() / 3600, (seconds.abs() % 3600 + 30) / 60);
            let sign = if seconds < 0 { '-' } else { '+' };
            let text = format!("{}{sign}{hours:02}:{minutes:02}", local.str()?);
            return Ok(Some(PyString::new(py, &text).into_any()));
        }
        if value.is_instance(kinds.get_item(2)?.as_ref())? {
            return Ok(Some(value.call_method0(pyo3::intern!(py, "isoformat"))?));
        }
        if value.is_instance(kinds.get_item(3)?.as_ref())? {
            return Ok(Some(value.str()?.into_any()));
        }
        if value.is_instance(kinds.get_item(4)?.as_ref())? {
            if !value.getattr(pyo3::intern!(py, "tzinfo"))?.is_none() {
                return Err(PyTypeError::new_err(
                    "datetime.time must not have tzinfo set",
                ));
            }
            // The microseconds have six digits, but orjson leaves the leading zero off between
            // 10,000 and 99,999 (a time such as 00:00:00.010000 is written .10000).
            let part = |name: &str| -> PyResult<i64> { value.getattr(name)?.extract() };
            let mut text = format!(
                "{:02}:{:02}:{:02}",
                part("hour")?,
                part("minute")?,
                part("second")?
            );
            let micro = part("microsecond")?;
            if micro != 0 {
                if (10_000..100_000).contains(&micro) {
                    text.push_str(&format!(".{micro}"));
                } else {
                    text.push_str(&format!(".{micro:06}"));
                }
            }
            return Ok(Some(PyString::new(py, &text).into_any()));
        }
        Ok(None)
    }
}

/// Whether a Python int is really an enum member, which is written as its value.
fn is_special(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    if value.get_type().is(value.py().get_type::<PyInt>()) {
        return Ok(false);
    }
    let kinds = special_kinds(value.py())?;
    value.is_instance(kinds.get_item(0)?.as_ref())
}

/// `enum.Enum`, `datetime.datetime`, `datetime.date` and `datetime.time`, `uuid.UUID`, in a tuple.
fn special_kinds(py: Python<'_>) -> PyResult<Bound<'_, PyTuple>> {
    use pyo3::sync::PyOnceLock;
    static KINDS: PyOnceLock<Py<PyTuple>> = PyOnceLock::new();
    Ok(KINDS
        .get_or_try_init(py, || -> PyResult<Py<PyTuple>> {
            let datetime = py.import("datetime")?;
            Ok(PyTuple::new(
                py,
                [
                    py.import("enum")?.getattr("Enum")?,
                    datetime.getattr("datetime")?,
                    datetime.getattr("date")?,
                    py.import("uuid")?.getattr("UUID")?,
                    datetime.getattr("time")?,
                ],
            )?
            .unbind())
        })?
        .bind(py)
        .clone())
}
