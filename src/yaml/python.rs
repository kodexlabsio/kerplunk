//! `konstruct._core.yaml_load` and `yaml_load_all`: the Python values of a YAML text, built from
//! the nodes the way PyYAML's `SafeConstructor` builds them, so a caller gets what `safe_load`
//! gave. A text that cannot be read raises `YamlFailure`, whose arguments describe the problem
//! for `konstruct.core.yamlio` to raise as the PyYAML exception it always was.

use std::borrow::Cow;
use std::collections::HashMap;
use std::rc::Rc;

use pyo3::exceptions::{PyException, PyTypeError};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PySet, PyString, PyTuple};

use super::compose::{Body, Composer, Document, Node, NodeId};
use super::reader;
use super::resolve::Tag;
use super::{Failure, Limits, Mark, Stage, grouped, repr};

pyo3::create_exception!(
    _core,
    YamlFailure,
    PyException,
    "A YAML text that cannot be read. The arguments are the stage that refused it, its context \
     and the mark of it, the problem and its mark; a mark is (index, line, column)."
);

/// Adds the exception to the module.
pub fn add_exceptions(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("YamlFailure", module.py().get_type::<YamlFailure>())
}

type MarkTuple = (usize, usize, usize);

fn mark_tuple(mark: Mark) -> MarkTuple {
    (mark.index, mark.line, mark.column)
}

fn raise(failure: Failure) -> PyErr {
    let failure = failure.into_parts();
    // The names in a message are quoted as Python quotes them.
    let quote = |text: &str| {
        Python::attach(|py| {
            PyString::new(py, text).repr().map_or_else(
                |_| format!("'{text}'"),
                |r| r.to_string_lossy().into_owned(),
            )
        })
    };
    let problem = super::resolve_reprs(&failure.problem, quote);
    let context = failure
        .context
        .as_deref()
        .map(|c| super::resolve_reprs(c, quote));
    let stage = match failure.stage {
        Stage::Reader => "reader",
        Stage::Scanner => "scanner",
        Stage::Parser => "parser",
        Stage::Composer => "composer",
        Stage::Constructor => "constructor",
        Stage::Unreadable => "unreadable",
        Stage::Limit => "limit",
    };
    YamlFailure::new_err((
        stage,
        context,
        failure.context_mark.map(mark_tuple),
        problem,
        failure.problem_mark.map(mark_tuple),
    ))
}

/// What can go wrong while building values: the text is refused, or Python itself fails.
enum Error {
    Yaml(Failure),
    Python(PyErr),
}

impl From<Failure> for Error {
    fn from(failure: Failure) -> Error {
        Error::Yaml(failure)
    }
}

impl From<PyErr> for Error {
    fn from(error: PyErr) -> Error {
        Error::Python(error)
    }
}

impl From<Error> for PyErr {
    fn from(error: Error) -> PyErr {
        match error {
            Error::Yaml(failure) => raise(failure),
            Error::Python(error) => error,
        }
    }
}

type Built<'py> = Result<Bound<'py, PyAny>, Error>;

/// The text of a `str` or `bytes` source, or why it cannot be read.
fn text_of<'a>(source: &'a Bound<'_, PyAny>) -> PyResult<Result<Cow<'a, str>, Failure>> {
    if let Ok(bytes) = source.cast::<PyBytes>() {
        return Ok(reader::decode(bytes.as_bytes()));
    }
    if let Ok(text) = source.cast::<PyString>() {
        return Ok(match text.to_str() {
            Ok(text) => reader::check(text).map(|()| Cow::Borrowed(text)),
            Err(_) => Err(lone_surrogate(text)?),
        });
    }
    Err(PyTypeError::new_err("YAML is read from str or bytes"))
}

/// The refusal of a `str` that holds a lone surrogate, which Rust cannot see as text.
fn lone_surrogate(text: &Bound<'_, PyString>) -> PyResult<Failure> {
    let py = text.py();
    let encoded = text.call_method1(
        pyo3::intern!(py, "encode"),
        (
            pyo3::intern!(py, "utf-32-le"),
            pyo3::intern!(py, "surrogatepass"),
        ),
    )?;
    let bytes = encoded.cast::<PyBytes>()?.as_bytes();
    let points: Vec<u32> = bytes
        .as_chunks::<4>()
        .0
        .iter()
        .map(|chunk| u32::from_le_bytes(*chunk))
        .collect();
    let at = points
        .iter()
        .position(|&c| !reader::printable(c))
        .unwrap_or(0);
    Ok(reader::unprintable(
        points[..at].iter().copied(),
        at,
        points[at],
    ))
}

/// The value of a YAML document, None when it is empty.
///
/// `lines` makes every mapping a dict of string keys with the line it starts on as `__line__`.
#[pyfunction]
#[pyo3(signature = (source, lines=false, max_values=Limits::DEFAULT.max_values, max_depth=Limits::DEFAULT.max_depth, digits=Limits::DEFAULT.digits))]
pub fn yaml_load<'py>(
    py: Python<'py>,
    source: &Bound<'py, PyAny>,
    lines: bool,
    max_values: u64,
    max_depth: usize,
    digits: usize,
) -> PyResult<Bound<'py, PyAny>> {
    let limits = Limits {
        max_values,
        max_depth,
        digits,
    };
    let text = text_of(source)?.map_err(raise)?;
    let document = py
        .detach(|| Composer::new(&text, limits, false).single())
        .map_err(raise)?;
    match document {
        None => Ok(py.None().into_bound(py)),
        Some(document) => {
            let values = values_of(py, std::slice::from_ref(&document), limits, lines)?;
            Ok(values
                .into_iter()
                .next()
                .unwrap_or_else(|| py.None().into_bound(py)))
        }
    }
}

/// The value of each document of a stream, an empty document left out. The values aliases add
/// are one budget for the whole stream.
#[pyfunction]
#[pyo3(signature = (source, lines=false, max_values=Limits::DEFAULT.max_values, max_depth=Limits::DEFAULT.max_depth, digits=Limits::DEFAULT.digits))]
pub fn yaml_load_all<'py>(
    py: Python<'py>,
    source: &Bound<'py, PyAny>,
    lines: bool,
    max_values: u64,
    max_depth: usize,
    digits: usize,
) -> PyResult<Bound<'py, PyList>> {
    let limits = Limits {
        max_values,
        max_depth,
        digits,
    };
    let text = text_of(source)?.map_err(raise)?;
    let documents = py
        .detach(|| -> Result<Vec<Document>, Failure> {
            let mut composer = Composer::new(&text, limits, true);
            let mut documents = Vec::new();
            while let Some(document) = composer.next_document()? {
                documents.push(document);
            }
            Ok(documents)
        })
        .map_err(raise)?;
    let values = PyList::empty(py);
    for value in values_of(py, &documents, limits, lines)? {
        if !value.is_none() {
            values.append(value)?;
        }
    }
    Ok(values)
}

/// The value of each document, built one after another.
fn values_of<'py>(
    py: Python<'py>,
    documents: &[Document<'_>],
    limits: Limits,
    lines: bool,
) -> Result<Vec<Bound<'py, PyAny>>, Error> {
    documents
        .iter()
        .map(|document| Builder::new(py, document, limits, lines).value())
        .collect()
}

/// The pairs of a merging mapping flattened, or the merged mappings to flatten first, in order.
type Merged<'a, 't> = Result<Vec<(NodeId, NodeId)>, Vec<&'a Node<'t>>>;

/// The pairs of a mapping, with its merge keys flattened when it has any.
enum Flat<'a> {
    Own(&'a [(NodeId, NodeId)]),
    Shared(Rc<Vec<(NodeId, NodeId)>>),
}

impl std::ops::Deref for Flat<'_> {
    type Target = [(NodeId, NodeId)];

    fn deref(&self) -> &[(NodeId, NodeId)] {
        match self {
            Flat::Own(pairs) => pairs,
            Flat::Shared(pairs) => pairs,
        }
    }
}

struct Builder<'a, 't, 'py> {
    py: Python<'py>,
    document: &'a Document<'t>,
    limits: Limits,
    lines: bool,
    /// The value built for each node an alias refers to, so that every alias is the same object.
    shared: HashMap<NodeId, Py<PyAny>>,
    /// The collections made and not yet filled: PyYAML's constructors make a collection first and
    /// fill it later, in rounds, so a document with several faults reports the one PyYAML did.
    pending: Vec<(&'a Node<'t>, Bound<'py, PyAny>)>,
    /// The pairs of each mapping with merge keys, flattened, by the address of its node.
    flattened: HashMap<usize, Rc<Vec<(NodeId, NodeId)>>>,
    /// A collection made to be filled at once, which the loop that fills them takes next.
    spawned: Option<Frame<'a, 't, 'py>>,
}

/// A collection being filled, and where it is: PyYAML's constructors read a collection with a
/// generator, and this is that generator's state, so that no depth of nesting costs the stack.
struct Frame<'a, 't, 'py> {
    node: &'a Node<'t>,
    target: Bound<'py, PyAny>,
    /// Whether its members are filled at once, as the line loader reads them, or in the next round.
    deep: bool,
    at: usize,
    stage: u8,
    flat: Option<Flat<'a>>,
    /// The key of an ordered map's item, while its value is read.
    stash: Option<Bound<'py, PyAny>>,
    /// What to refuse once a collection used as a key is filled.
    after: Option<Error>,
    /// For a line mapping: the dict its members go into, before they are given as strings.
    scratch: Option<Bound<'py, PyDict>>,
}

impl<'a, 't, 'py> Frame<'a, 't, 'py> {
    fn new(node: &'a Node<'t>, target: Bound<'py, PyAny>, deep: bool) -> Self {
        Frame {
            node,
            target,
            deep,
            at: 0,
            stage: 0,
            flat: None,
            stash: None,
            after: None,
            scratch: None,
        }
    }
}

impl<'a, 't, 'py> Builder<'a, 't, 'py> {
    fn new(py: Python<'py>, document: &'a Document<'t>, limits: Limits, lines: bool) -> Self {
        Builder {
            py,
            document,
            limits,
            lines,
            shared: HashMap::new(),
            pending: Vec::new(),
            flattened: HashMap::new(),
            spawned: None,
        }
    }

    /// The value of the document.
    fn value(&mut self) -> Built<'py> {
        let root = self.build(self.document.root, false)?;
        if let Some(frame) = self.spawned.take() {
            self.run(frame)?;
        }
        while !self.pending.is_empty() {
            for (node, target) in std::mem::take(&mut self.pending) {
                self.run(Frame::new(node, target, false))?;
            }
        }
        Ok(root)
    }

    fn node(&self, id: NodeId) -> &'a Node<'t> {
        &self.document.nodes[id as usize]
    }

    /// The value of a node. A collection is made empty and filled later unless `deep`, which is
    /// how PyYAML's constructors read the members of a mapping the line loader reads.
    fn build(&mut self, id: NodeId, deep: bool) -> Built<'py> {
        let node = self.node(id);
        if node.shared
            && let Some(value) = self.shared.get(&id)
        {
            return Ok(value.bind(self.py).clone());
        }
        let value = self.construct(node, deep)?;
        if node.shared {
            self.shared.insert(id, value.clone().unbind());
        }
        Ok(value)
    }

    fn refuse<T>(&self, problem: impl Into<String>, mark: Mark) -> Result<T, Error> {
        Err(Failure::new(Stage::Constructor, problem, Some(mark)).into())
    }

    fn construct(&mut self, node: &'a Node<'t>, deep: bool) -> Built<'py> {
        let py = self.py;
        if node.tag == Tag::Int
            && let Body::Scalar(text) = &node.body
            && text.len() > self.limits.digits
        {
            return self.too_many_digits(node);
        }
        match &node.tag {
            Tag::Str => {
                let text = self.scalar(node)?;
                if node.lone {
                    return self.refuse("a lone surrogate is not allowed", node.mark);
                }
                Ok(PyString::new(py, text).into_any())
            }
            Tag::Null => {
                self.scalar(node)?;
                Ok(py.None().into_bound(py))
            }
            Tag::Bool => {
                let text = self.scalar(node)?;
                match bool_of(text) {
                    Some(value) => Ok(PyBool::new(py, value).to_owned().into_any()),
                    None => self.unreadable(node, Unread::Other),
                }
            }
            Tag::Int => {
                let text = self.scalar(node)?;
                match self.integer(text) {
                    Ok(value) => self.printable(node, value),
                    Err(error) => self.unreadable(node, error),
                }
            }
            Tag::Float => {
                let text = self.scalar(node)?;
                match self.float(text) {
                    Ok(value) => Ok(value),
                    Err(error) => self.unreadable(node, error),
                }
            }
            Tag::Timestamp => {
                let text = self.scalar(node)?;
                match self.timestamp(text) {
                    Ok(value) => Ok(value),
                    Err(error) => self.unreadable(node, error),
                }
            }
            Tag::Binary => self.binary(node),
            Tag::Seq | Tag::Omap | Tag::Pairs => {
                self.collection(node, PyList::empty(py).into_any(), deep)
            }
            Tag::Map if self.lines => {
                // The line loader reads a mapping at once, with everything in it.
                let target = PyDict::new(py);
                let mut frame = Frame::new(node, target.clone().into_any(), true);
                frame.scratch = Some(PyDict::new(py));
                self.spawned = Some(frame);
                Ok(target.into_any())
            }
            Tag::Map => self.collection(node, PyDict::new(py).into_any(), deep),
            Tag::Set => self.collection(node, PySet::empty(py)?.into_any(), deep),
            Tag::Merge | Tag::Value | Tag::Yaml | Tag::Other(_) => self.refuse(
                format!(
                    "could not determine a constructor for the tag {}",
                    repr(&node.tag.uri())
                ),
                node.mark,
            ),
        }
    }

    fn too_many_digits<T>(&self, node: &Node<'_>) -> Result<T, Error> {
        self.refuse(
            format!(
                "an integer of more than {} digits",
                grouped(self.limits.digits as u64)
            ),
            node.mark,
        )
    }

    /// The text of a scalar node: PyYAML's `construct_scalar`, which also reads a mapping whose
    /// key is `=` as the scalar that key stands for.
    fn scalar(&self, node: &'a Node<'t>) -> Result<&'a str, Error> {
        let mut node = node;
        loop {
            match &node.body {
                Body::Scalar(text) => return Ok(text),
                Body::Mapping(pairs) => {
                    if let Some(&(_, value)) = pairs
                        .iter()
                        .find(|&&(key, _)| self.node(key).tag == Tag::Value)
                    {
                        node = self.node(value);
                        continue;
                    }
                }
                Body::Sequence(_) => {}
            }
            return self.refuse(
                format!("expected a scalar node, but found {}", node.body.id()),
                node.mark,
            );
        }
    }

    /// A value PyYAML's constructor could not make from the text: what PyYAML raised, besides
    /// its own errors, is reported as a problem at the node.
    fn unreadable<T>(&self, node: &Node<'_>, error: Unread) -> Result<T, Error> {
        let kind = node.tag.short();
        let problem = match &node.body {
            Body::Scalar(text) => {
                let shown = if text.chars().count() <= 40 {
                    text.to_string()
                } else {
                    format!("{}...", text.chars().take(40).collect::<String>())
                };
                let detail = match error {
                    Unread::Value(message) if !message.is_empty() => format!(": {message}"),
                    _ => String::new(),
                };
                format!("{} is not a valid {kind}{detail}", repr(&shown))
            }
            _ => format!("cannot read this {kind}"),
        };
        self.refuse(problem, node.mark)
    }

    /// An integer, refused when its decimal text would be past the limit.
    fn printable(&self, node: &Node<'_>, value: Bound<'py, PyAny>) -> Built<'py> {
        let digits = self.limits.digits;
        if let Ok(bits) = value
            .call_method0("bit_length")
            .and_then(|bits| bits.extract::<usize>())
            && bits > 3 * digits
            && value.str().is_err()
        {
            return self.too_many_digits(node);
        }
        Ok(value)
    }

    /// PyYAML's `construct_yaml_int`.
    fn integer(&self, text: &str) -> Result<Bound<'py, PyAny>, Unread> {
        let py = self.py;
        let cleaned: String = text.chars().filter(|&c| c != '_').collect();
        let (negative, digits) = match cleaned.strip_prefix('-') {
            Some(rest) => (true, rest),
            None => (false, cleaned.strip_prefix('+').unwrap_or(&cleaned)),
        };
        let magnitude: Bound<'py, PyAny> = if digits.is_empty() {
            return Err(Unread::Other);
        } else if digits == "0" {
            return Ok(PyInt::new(py, 0).into_any());
        } else if let Some(rest) = digits.strip_prefix("0b") {
            self.parse_int(rest, 2)?
        } else if let Some(rest) = digits.strip_prefix("0x") {
            self.parse_int(rest, 16)?
        } else if digits.starts_with('0') {
            self.parse_int(digits, 8)?
        } else if digits.contains(':') {
            self.sexagesimal_int(digits)?
        } else {
            self.parse_int(digits, 10)?
        };
        if negative {
            magnitude.neg().map_err(|_| Unread::Other)
        } else {
            Ok(magnitude)
        }
    }

    /// Python's `int(text, base)`, with the common case done without a call.
    fn parse_int(&self, text: &str, base: u32) -> Result<Bound<'py, PyAny>, Unread> {
        let py = self.py;
        let plain = !text.is_empty()
            && text.len() <= 15
            && text.bytes().all(|b| char::from(b).is_digit(base));
        if plain && let Ok(value) = u64::from_str_radix(text, base) {
            return Ok(PyInt::new(py, value).into_any());
        }
        py.get_type::<PyInt>()
            .call1((text, base))
            .map_err(|error| unread(py, &error))
    }

    fn sexagesimal_int(&self, text: &str) -> Result<Bound<'py, PyAny>, Unread> {
        let py = self.py;
        let mut total = PyInt::new(py, 0).into_any();
        for part in text.split(':') {
            let digit = self.parse_int(part, 10)?;
            total = total
                .mul(60)
                .and_then(|scaled| scaled.add(digit))
                .map_err(|error| unread(py, &error))?;
        }
        Ok(total)
    }

    /// PyYAML's `construct_yaml_float`.
    fn float(&self, text: &str) -> Result<Bound<'py, PyAny>, Unread> {
        let py = self.py;
        let cleaned: String = text
            .chars()
            .filter(|&c| c != '_')
            .collect::<String>()
            .to_lowercase();
        let (negative, digits) = match cleaned.strip_prefix('-') {
            Some(rest) => (true, rest),
            None => (false, cleaned.strip_prefix('+').unwrap_or(&cleaned)),
        };
        if digits.is_empty() {
            return Err(Unread::Other);
        }
        let signed =
            |value: f64| PyFloat::new(py, if negative { -value } else { value }).into_any();
        if digits == ".inf" {
            return Ok(signed(f64::INFINITY));
        }
        if digits == ".nan" {
            // One object for every `.nan`, as PyYAML's class attribute is, so that two of them
            // as keys of a mapping are one key, which is how a dict compares a NaN with itself.
            static NAN: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
            let nan = NAN.get_or_init(py, || PyFloat::new(py, f64::NAN).into_any().unbind());
            return Ok(nan.bind(py).clone());
        }
        if digits.contains(':') {
            let mut total = PyFloat::new(py, 0.0).into_any();
            // Every part is read first, from the left, so that the first bad one is named.
            let mut parts = digits
                .split(':')
                .map(|part| self.parse_float(part))
                .collect::<Result<Vec<_>, _>>()?;
            parts.reverse();
            let mut base = PyInt::new(py, 1).into_any();
            for digit in parts {
                total = digit
                    .mul(&base)
                    .and_then(|scaled| total.add(scaled))
                    .map_err(|error| unread(py, &error))?;
                base = base.mul(60).map_err(|error| unread(py, &error))?;
            }
            return if negative {
                total.neg().map_err(|error| unread(py, &error))
            } else {
                Ok(total)
            };
        }
        let value = self.parse_float(digits)?;
        if negative {
            value.neg().map_err(|error| unread(py, &error))
        } else {
            Ok(value)
        }
    }

    /// Python's `float(text)`, with the common case done without a call.
    fn parse_float(&self, text: &str) -> Result<Bound<'py, PyAny>, Unread> {
        let py = self.py;
        let plain = !text.is_empty()
            && text
                .bytes()
                .all(|b| b.is_ascii_digit() || matches!(b, b'.' | b'e' | b'+' | b'-'));
        if plain && let Ok(value) = text.parse::<f64>() {
            return Ok(PyFloat::new(py, value).into_any());
        }
        py.get_type::<PyFloat>()
            .call1((text,))
            .map_err(|error| unread(py, &error))
    }

    /// PyYAML's `construct_yaml_timestamp`.
    fn timestamp(&self, text: &str) -> Result<Bound<'py, PyAny>, Unread> {
        let py = self.py;
        let fields =
            Timestamp::parse(text.strip_suffix('\n').unwrap_or(text)).ok_or(Unread::Other)?;
        let datetime = py
            .import(pyo3::intern!(py, "datetime"))
            .map_err(|error| unread(py, &error))?;
        let call =
            |name: &str, arguments: Bound<'py, PyTuple>, keywords: Option<&Bound<'py, PyDict>>| {
                datetime
                    .getattr(name)
                    .and_then(|class| class.call(arguments, keywords))
                    .map_err(|error| unread(py, &error))
            };
        let Some(time) = fields.time else {
            return call(
                "date",
                (fields.year, fields.month, fields.day)
                    .into_pyobject(py)
                    .map_err(|e| unread(py, &e))?,
                None,
            );
        };
        let zone = match fields.zone {
            Zone::None => py.None().into_bound(py),
            Zone::Utc => datetime
                .getattr("timezone")
                .and_then(|class| class.getattr("utc"))
                .map_err(|error| unread(py, &error))?,
            Zone::Offset {
                negative,
                hours,
                minutes,
            } => {
                let delta = call(
                    "timedelta",
                    PyTuple::empty(py),
                    Some(&{
                        let keywords = PyDict::new(py);
                        keywords
                            .set_item("hours", hours)
                            .map_err(|e| unread(py, &e))?;
                        keywords
                            .set_item("minutes", minutes)
                            .map_err(|e| unread(py, &e))?;
                        keywords
                    }),
                )?;
                let delta = if negative {
                    delta.neg().map_err(|e| unread(py, &e))?
                } else {
                    delta
                };
                call(
                    "timezone",
                    PyTuple::new(py, [delta]).map_err(|e| unread(py, &e))?,
                    None,
                )?
            }
        };
        let keywords = PyDict::new(py);
        keywords
            .set_item("tzinfo", zone)
            .map_err(|e| unread(py, &e))?;
        call(
            "datetime",
            (
                fields.year,
                fields.month,
                fields.day,
                time.hour,
                time.minute,
                time.second,
                time.micro,
            )
                .into_pyobject(py)
                .map_err(|e| unread(py, &e))?,
            Some(&keywords),
        )
    }

    /// PyYAML's `construct_yaml_binary`.
    fn binary(&self, node: &'a Node<'t>) -> Built<'py> {
        let py = self.py;
        let text = PyString::new(py, self.scalar(node)?);
        let ascii =
            match text.call_method1(pyo3::intern!(py, "encode"), (pyo3::intern!(py, "ascii"),)) {
                Ok(ascii) => ascii,
                Err(error) => {
                    return self.refuse(
                        format!(
                            "failed to convert base64 data into ascii: {}",
                            message(py, &error)
                        ),
                        node.mark,
                    );
                }
            };
        let base64 = py.import(pyo3::intern!(py, "base64"))?;
        match base64.call_method1(pyo3::intern!(py, "decodebytes"), (ascii,)) {
            Ok(bytes) => Ok(bytes),
            Err(error) => self.refuse(
                format!("failed to decode base64 data: {}", message(py, &error)),
                node.mark,
            ),
        }
    }

    /// A collection, made empty: filled at once when `deep`, and in the next round otherwise.
    fn collection(
        &mut self,
        node: &'a Node<'t>,
        target: Bound<'py, PyAny>,
        deep: bool,
    ) -> Built<'py> {
        if deep {
            self.spawned = Some(Frame::new(node, target.clone(), true));
        } else {
            self.pending.push((node, target.clone()));
        }
        Ok(target)
    }

    /// Fills `first` and every collection filled at once inside it, depth first, as PyYAML's
    /// constructors do, on a stack of frames rather than the call stack.
    fn run(&mut self, first: Frame<'a, 't, 'py>) -> Result<(), Error> {
        let mut stack = vec![first];
        while let Some(mut frame) = stack.pop() {
            if !self.advance(&mut frame)? {
                stack.push(frame);
            }
            if let Some(child) = self.spawned.take() {
                stack.push(child);
            }
        }
        Ok(())
    }

    /// One step of filling a collection: its next member, or its end. True at the end.
    fn advance(&mut self, f: &mut Frame<'a, 't, 'py>) -> Result<bool, Error> {
        if let Some(error) = f.after.take() {
            return Err(error);
        }
        let node = f.node;
        match &node.tag {
            Tag::Seq => {
                let Body::Sequence(items) = &node.body else {
                    return self.refuse(
                        format!("expected a sequence node, but found {}", node.body.id()),
                        node.mark,
                    );
                };
                let Some(&item) = items.get(f.at) else {
                    return Ok(true);
                };
                f.at += 1;
                let value = self.build(item, f.deep)?;
                f.target
                    .cast::<PyList>()
                    .map_err(PyErr::from)?
                    .append(value)?;
                Ok(false)
            }
            Tag::Map | Tag::Set => self.advance_pairs(f),
            Tag::Omap => self.advance_items(f, "while constructing an ordered map"),
            Tag::Pairs => self.advance_items(f, "while constructing pairs"),
            _ => Ok(true),
        }
    }

    /// PyYAML's `construct_mapping`: the pairs of a mapping, merge keys flattened, in a dict (the
    /// last of a repeated key wins) or a set.
    fn advance_pairs(&mut self, f: &mut Frame<'a, 't, 'py>) -> Result<bool, Error> {
        let node = f.node;
        if f.flat.is_none() {
            f.flat = Some(self.flatten(node)?);
        }
        let pair = f.flat.as_ref().and_then(|pairs| pairs.get(f.at)).copied();
        let Some((key_id, value_id)) = pair else {
            if let Some(scratch) = &f.scratch {
                // The line loader gives the keys as strings, and the line the mapping is on.
                let lined = f.target.cast::<PyDict>().map_err(PyErr::from)?;
                for (key, value) in scratch.iter() {
                    lined.set_item(key.str()?, value)?;
                }
                lined.set_item(pyo3::intern!(self.py, "__line__"), node.mark.line + 1)?;
            }
            return Ok(true);
        };
        f.at += 1;
        let key = self.key(key_id, f.deep)?;
        if let Err(refusal) = self.hashable(node, key_id, &key) {
            if self.spawned.is_some() {
                // A collection used as a key is filled first, and any fault in it comes first.
                f.after = Some(refusal);
                return Ok(false);
            }
            return Err(refusal);
        }
        let value = self.build(value_id, f.deep)?;
        if node.tag == Tag::Set {
            f.target.cast::<PySet>().map_err(PyErr::from)?.add(key)?;
        } else if let Some(scratch) = &f.scratch {
            scratch.set_item(key, value)?;
        } else {
            f.target
                .cast::<PyDict>()
                .map_err(PyErr::from)?
                .set_item(key, value)?;
        }
        Ok(false)
    }

    /// An ordered map or pairs: a list of (key, value) tuples, one for each one-item mapping.
    fn advance_items(&mut self, f: &mut Frame<'a, 't, 'py>, context: &str) -> Result<bool, Error> {
        let node = f.node;
        let Body::Sequence(items) = &node.body else {
            return Err(Failure::within(
                Stage::Constructor,
                context,
                node.mark,
                format!("expected a sequence, but found {}", node.body.id()),
                node.mark,
            )
            .into());
        };
        let Some(&item) = items.get(f.at) else {
            return Ok(true);
        };
        let sub = self.node(item);
        let Body::Mapping(pairs) = &sub.body else {
            return Err(Failure::within(
                Stage::Constructor,
                context,
                node.mark,
                format!(
                    "expected a mapping of length 1, but found {}",
                    sub.body.id()
                ),
                sub.mark,
            )
            .into());
        };
        let [(key_id, value_id)] = pairs.as_slice() else {
            return Err(Failure::within(
                Stage::Constructor,
                context,
                node.mark,
                format!(
                    "expected a single mapping item, but found {} items",
                    pairs.len()
                ),
                sub.mark,
            )
            .into());
        };
        if f.stage == 0 {
            f.stash = Some(self.build(*key_id, f.deep)?);
            f.stage = 1;
            return Ok(false);
        }
        let value = self.build(*value_id, f.deep)?;
        let key = f
            .stash
            .take()
            .ok_or_else(|| Failure::new(Stage::Constructor, "no key", None))?;
        f.target
            .cast::<PyList>()
            .map_err(PyErr::from)?
            .append(PyTuple::new(self.py, [key, value])?)?;
        f.stage = 0;
        f.at += 1;
        Ok(false)
    }

    /// The pairs of a mapping with its merge keys flattened, PyYAML's `flatten_mapping`: what
    /// the merged mappings give first, the later of a sequence of them first, then its own. A
    /// mapping that merges others waits on a stack until they are flattened, so a chain of
    /// merges as long as the limits allow costs no depth.
    fn flatten(&mut self, root: &'a Node<'t>) -> Result<Flat<'a>, Error> {
        let Body::Mapping(pairs) = &root.body else {
            return self.refuse(
                format!("expected a mapping node, but found {}", root.body.id()),
                root.mark,
            );
        };
        if !self.merges(pairs) {
            return Ok(Flat::Own(pairs));
        }
        let mut stack = vec![root];
        while let Some(&node) = stack.last() {
            let at = std::ptr::from_ref(node) as usize;
            if self.flattened.contains_key(&at) {
                stack.pop();
                continue;
            }
            match self.merge(node)? {
                Ok(merged) => {
                    self.flattened.insert(at, Rc::new(merged));
                    stack.pop();
                }
                Err(needed) => stack.extend(needed.into_iter().rev()),
            }
        }
        let at = std::ptr::from_ref(root) as usize;
        Ok(Flat::Shared(Rc::clone(&self.flattened[&at])))
    }

    fn merges(&self, pairs: &[(NodeId, NodeId)]) -> bool {
        pairs
            .iter()
            .any(|&(key, _)| self.node(key).tag == Tag::Merge)
    }

    /// The pairs of `node` flattened, or a merged mapping that is to be flattened first.
    fn merge(&self, node: &'a Node<'t>) -> Result<Merged<'a, 't>, Error> {
        let Body::Mapping(pairs) = &node.body else {
            return Ok(Ok(Vec::new()));
        };
        // What a merged mapping stands for, if it is known now.
        let known = |this: &Self, target: &'a Node<'t>| -> Option<Flat<'a>> {
            let Body::Mapping(inner) = &target.body else {
                return None;
            };
            if !this.merges(inner) {
                return Some(Flat::Own(inner));
            }
            let at = std::ptr::from_ref(target) as usize;
            this.flattened
                .get(&at)
                .map(|done| Flat::Shared(Rc::clone(done)))
        };
        let mut merged: Vec<(NodeId, NodeId)> = Vec::new();
        let mut own: Vec<(NodeId, NodeId)> = Vec::new();
        // Every merged mapping still to flatten, found in one pass up to a fault in the structure,
        // so that each is flattened once and this one is read again once: the faults of the ones
        // before it come first, as PyYAML visits them.
        let mut needed: Vec<&'a Node<'t>> = Vec::new();
        for &(key, value) in pairs {
            if self.node(key).tag != Tag::Merge {
                own.push((key, value));
                continue;
            }
            let target = self.node(value);
            match &target.body {
                Body::Mapping(_) => match known(self, target) {
                    Some(inner) => merged.extend_from_slice(&inner),
                    None => needed.push(target),
                },
                Body::Sequence(items) => {
                    let mut each: Vec<Flat<'a>> = Vec::new();
                    for &item in items {
                        let sub = self.node(item);
                        if !matches!(sub.body, Body::Mapping(_)) {
                            if !needed.is_empty() {
                                return Ok(Err(needed));
                            }
                            return Err(Failure::within(
                                Stage::Constructor,
                                "while constructing a mapping",
                                node.mark,
                                format!(
                                    "expected a mapping for merging, but found {}",
                                    sub.body.id()
                                ),
                                sub.mark,
                            )
                            .into());
                        }
                        match known(self, sub) {
                            Some(inner) => each.push(inner),
                            None => needed.push(sub),
                        }
                    }
                    for inner in each.iter().rev() {
                        merged.extend_from_slice(inner);
                    }
                }
                Body::Scalar(_) => {
                    if !needed.is_empty() {
                        return Ok(Err(needed));
                    }
                    return Err(Failure::within(
                        Stage::Constructor,
                        "while constructing a mapping",
                        node.mark,
                        format!(
                            "expected a mapping or list of mappings for merging, but found {}",
                            target.body.id()
                        ),
                        target.mark,
                    )
                    .into());
                }
            }
        }
        if !needed.is_empty() {
            return Ok(Err(needed));
        }
        merged.extend(own);
        Ok(Ok(merged))
    }

    /// The key of a pair: a key written `=` is a string.
    fn key(&mut self, id: NodeId, deep: bool) -> Built<'py> {
        let node = self.node(id);
        if node.tag == Tag::Value
            && let Body::Scalar(text) = &node.body
        {
            if node.lone {
                return self.refuse("a lone surrogate is not allowed", node.mark);
            }
            return Ok(PyString::new(self.py, text).into_any());
        }
        self.build(id, deep)
    }

    /// Refuses a key that is a list, a dict or a set, as PyYAML does.
    fn hashable(
        &self,
        node: &Node<'_>,
        key_id: NodeId,
        key: &Bound<'py, PyAny>,
    ) -> Result<(), Error> {
        if key.is_instance_of::<PyList>()
            || key.is_instance_of::<PyDict>()
            || key.is_instance_of::<PySet>()
        {
            return Err(Failure::within(
                Stage::Constructor,
                "while constructing a mapping",
                node.mark,
                "found unhashable key",
                self.node(key_id).mark,
            )
            .into());
        }
        Ok(())
    }
}

/// Why a scalar's text is not a value of its tag: Python's `ValueError`, whose message a problem
/// repeats, or another error, which it does not.
enum Unread {
    Value(String),
    Other,
}

fn unread(py: Python<'_>, error: &PyErr) -> Unread {
    if error.is_instance_of::<pyo3::exceptions::PyValueError>(py) {
        Unread::Value(message(py, error))
    } else {
        Unread::Other
    }
}

fn message(py: Python<'_>, error: &PyErr) -> String {
    error
        .value(py)
        .str()
        .map(|text| text.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// `yes`, `no`, `true`, `false`, `on` and `off` in any case, as PyYAML's `bool_values` reads them.
fn bool_of(text: &str) -> Option<bool> {
    match text.to_lowercase().as_str() {
        "yes" | "true" | "on" => Some(true),
        "no" | "false" | "off" => Some(false),
        _ => None,
    }
}

struct Time {
    hour: u32,
    minute: u32,
    second: u32,
    micro: u32,
}

enum Zone {
    None,
    Utc,
    Offset {
        negative: bool,
        hours: u32,
        minutes: u32,
    },
}

/// The fields of a timestamp, as PyYAML's `timestamp_regexp` reads them.
struct Timestamp {
    year: u32,
    month: u32,
    day: u32,
    time: Option<Time>,
    zone: Zone,
}

impl Timestamp {
    fn parse(text: &str) -> Option<Timestamp> {
        let v = text.as_bytes();
        let number = |at: usize, most: usize| -> Option<(u32, usize)> {
            let width = v[at.min(v.len())..]
                .iter()
                .take(most)
                .take_while(|b| b.is_ascii_digit())
                .count();
            if width == 0 {
                return None;
            }
            let value = text[at..at + width].parse().ok()?;
            Some((value, at + width))
        };
        let exactly = |at: usize, count: usize| -> Option<(u32, usize)> {
            let (value, end) = number(at, count)?;
            (end == at + count).then_some((value, end))
        };
        let (year, at) = exactly(0, 4)?;
        if v.get(at) != Some(&b'-') {
            return None;
        }
        let (month, at) = number(at + 1, 2)?;
        if v.get(at) != Some(&b'-') {
            return None;
        }
        let (day, at) = number(at + 1, 2)?;
        if at == v.len() {
            return Some(Timestamp {
                year,
                month,
                day,
                time: None,
                zone: Zone::None,
            });
        }
        let mut at = at;
        match v.get(at) {
            Some(b'T' | b't') => at += 1,
            Some(b' ' | b'\t') => {
                while matches!(v.get(at), Some(b' ' | b'\t')) {
                    at += 1;
                }
            }
            _ => return None,
        }
        let (hour, next) = number(at, 2)?;
        if v.get(next) != Some(&b':') {
            return None;
        }
        let (minute, next) = exactly(next + 1, 2)?;
        if v.get(next) != Some(&b':') {
            return None;
        }
        let (second, mut at) = exactly(next + 1, 2)?;
        let mut micro = 0;
        if v.get(at) == Some(&b'.') {
            let width = v[at + 1..]
                .iter()
                .take_while(|b| b.is_ascii_digit())
                .count();
            let fraction = &text[at + 1..at + 1 + width];
            if !fraction.is_empty() {
                let mut padded: String = fraction.chars().take(6).collect();
                while padded.len() < 6 {
                    padded.push('0');
                }
                micro = padded.parse().ok()?;
            }
            at += 1 + width;
        }
        let time = Some(Time {
            hour,
            minute,
            second,
            micro,
        });
        if at == v.len() {
            return Some(Timestamp {
                year,
                month,
                day,
                time,
                zone: Zone::None,
            });
        }
        while matches!(v.get(at), Some(b' ' | b'\t')) {
            at += 1;
        }
        let zone = match v.get(at) {
            Some(b'Z') if at + 1 == v.len() => Zone::Utc,
            Some(sign @ (b'-' | b'+')) => {
                let (hours, next) = number(at + 1, 2)?;
                let (minutes, next) = if v.get(next) == Some(&b':') {
                    exactly(next + 1, 2)?
                } else {
                    (0, next)
                };
                if next != v.len() {
                    return None;
                }
                Zone::Offset {
                    negative: *sign == b'-',
                    hours,
                    minutes,
                }
            }
            _ => return None,
        };
        Some(Timestamp {
            year,
            month,
            day,
            time,
            zone,
        })
    }
}
