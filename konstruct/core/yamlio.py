"""YAML in, through PyYAML's safe loader. Every YAML file Konstruct reads, the repository's or its own,
goes through :func:`read` or :func:`load`, so a reader handles one error, :class:`yaml.YAMLError`,
for any text it cannot read, rather than its own list of what PyYAML raises."""

from __future__ import annotations

import codecs
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.error import Mark
from yaml.reader import ReaderError

MAX_VALUES = 1_000_000
"""The most values a YAML document's aliases may add once they are expanded: a few hundred bytes
of aliases to aliases stand for millions, which a reader walking the value would expand. Values
written out are not counted, so a large file without aliases is read whole."""
DIGITS = sys.get_int_max_str_digits() or sys.int_info.default_max_str_digits
"""The most digits an integer may have: Python's limit for converting one to text, which a reader
does with each value it reports. PyYAML computes a sexagesimal integer (``1:30:59``) in time
quadratic in its length, so the literal is held to the limit too."""

_UNREADABLE = (ValueError, OverflowError, LookupError, AttributeError, TypeError, RecursionError)
"""What PyYAML raises, besides its own errors, on text it cannot read: ``chr()`` of an escape past
Unicode (ValueError) or past a C int (OverflowError) in the scanner; in the constructors, a date
that does not exist (ValueError), ``!!bool maybe`` (KeyError), ``!!timestamp foo``
(AttributeError) and an empty ``!!int`` or ``!!float`` (IndexError); and nesting past Python's
recursion limit."""
_INT_TAG = 'tag:yaml.org,2002:int'
_SURROGATE = re.compile('[\ud800-\udfff]')


def read(path: Path, loader: type[yaml.SafeLoader] = yaml.SafeLoader) -> Any:
    """The value of the YAML file at ``path``, as :func:`load` reads it. The file's bytes go to
    PyYAML, which reads a byte order mark and refuses text that is not UTF-8 or UTF-16 with a
    :class:`yaml.YAMLError`. Raises OSError if the file cannot be read."""
    return load(path.read_bytes(), loader)


def load(text: str | bytes, loader: type[yaml.SafeLoader] = yaml.SafeLoader) -> Any:
    """The value of the YAML document ``text``, None for an empty one, read by ``loader`` (a
    :class:`yaml.SafeLoader` or a subclass). Raises :class:`yaml.YAMLError` for any text it
    cannot read: not YAML, a value PyYAML cannot construct, an unpaired surrogate, an integer of
    more than :data:`DIGITS` digits, nesting too deep, or aliases that add more than
    :data:`MAX_VALUES` values, which is refused before anything expands them. The error's
    ``problem_mark``, when it has one, is the value at fault, or where reading stopped."""
    reader = None
    try:
        reader = _guarded(loader)(text)
        node = reader.get_single_node()
        if node is None:
            return None
        if reader.aliases:
            _check_aliases(node, reader.aliases)
        return reader.construct_document(node)
    except ReaderError as error:
        raise yaml.MarkedYAMLError(
            problem=str(error).splitlines()[0], problem_mark=_reader_mark(text, error)
        ) from error
    except _UNREADABLE as error:
        problem = 'nested too deeply to read' if isinstance(error, RecursionError) else str(error)
        mark = reader.get_mark() if reader is not None else None
        raise yaml.MarkedYAMLError(problem=problem or type(error).__name__, problem_mark=mark) from error
    finally:
        if reader is not None:
            reader.dispose()


class _Guard(yaml.SafeLoader):
    """Placed before a loader in its method order: it records each alias as it is composed, and
    refuses, at the node at fault, a value its constructor cannot build or a reader cannot use."""

    def __init__(self, stream: str | bytes) -> None:
        super().__init__(stream)
        self.aliases: list[tuple[yaml.Node, Mark]] = []

    def compose_node(self, parent: yaml.Node | None, index: int) -> yaml.Node | None:
        if not self.check_event(yaml.AliasEvent):
            return super().compose_node(parent, index)
        mark: Mark = self.peek_event().start_mark  # type: ignore[no-untyped-call]
        node = super().compose_node(parent, index)
        if node is not None:
            self.aliases.append((node, mark))
        return node

    def construct_object(self, node: yaml.Node, deep: bool = False) -> Any:
        if node in self.constructed_objects:
            return super().construct_object(node, deep)
        if node.tag == _INT_TAG and isinstance(node.value, str) and len(node.value) > DIGITS:
            raise ConstructorError(None, None, f'an integer of more than {DIGITS:,} digits', node.start_mark)
        try:
            data = super().construct_object(node, deep)
        except _UNREADABLE as error:
            raise ConstructorError(None, None, _problem(node, error), node.start_mark) from error
        if isinstance(data, str) and _SURROGATE.search(data):
            data = self.constructed_objects[node] = _paired(data, node)
        if type(data) is int and not _printable(data):
            raise ConstructorError(None, None, f'an integer of more than {DIGITS:,} digits', node.start_mark)
        return data


_GUARDED: dict[type[yaml.SafeLoader], type[_Guard]] = {}


def _guarded(loader: type[yaml.SafeLoader]) -> type[_Guard]:
    """``loader`` with :class:`_Guard` before it, made once for each loader."""
    if issubclass(loader, _Guard):
        return loader
    if loader not in _GUARDED:
        _GUARDED[loader] = type(f'Guarded{loader.__name__}', (_Guard, loader), {})
    return _GUARDED[loader]


def _paired(text: str, node: yaml.Node) -> str:
    """``text`` with each escaped surrogate pair (``"\\ud83d\\udce6"``, as JSON writes a character
    past the Basic Multilingual Plane) joined into its character. A half left unpaired is refused:
    no encoding can write it, so a reader would fail on it later."""
    try:
        return text.encode('utf-16', 'surrogatepass').decode('utf-16')
    except UnicodeDecodeError:
        raise ConstructorError(None, None, 'a lone surrogate is not allowed', node.start_mark) from None


def _problem(node: yaml.Node, error: BaseException) -> str:
    if isinstance(error, RecursionError):
        return 'nested too deeply to read'
    kind = str(node.tag).rsplit(':', 1)[-1]
    if not isinstance(node, yaml.ScalarNode):
        return f'cannot read this {kind}: {error}'
    value = node.value if len(node.value) <= 40 else node.value[:40] + '...'
    detail = f': {error}' if isinstance(error, ValueError) and str(error) else ''
    return f'{value!r} is not a valid {kind}{detail}'


_BREAKS = re.compile('\r\n|[\n\r\x85\u2028\u2029]')


def _reader_mark(text: str | bytes, error: ReaderError) -> Mark:
    """Where the byte or character PyYAML's reader refused is: its position counts characters
    when the text decoded and it is one YAML does not allow, and bytes when it did not decode."""
    if isinstance(text, bytes):
        if error.encoding == 'unicode':
            # As PyYAML decodes it, keeping a byte order mark, which its position counts.
            codec = {codecs.BOM_UTF16_LE: 'utf-16-le', codecs.BOM_UTF16_BE: 'utf-16-be'}.get(
                text[:2], 'utf-8'
            )
            before = text.decode(codec, 'replace')[: error.position]
        else:
            before = text[: error.position].decode(error.encoding, 'replace')
    else:
        before = text[: error.position]
    lines = _BREAKS.split(before)
    return Mark(error.name, error.position, len(lines) - 1, len(lines[-1]), None, 0)


def _printable(number: int) -> bool:
    """Whether ``number`` has at most :data:`DIGITS` decimal digits. A number of at most three
    bits for each digit always has; nearer the limit, the conversion itself decides."""
    if number.bit_length() <= 3 * DIGITS:
        return True
    try:
        return len(str(abs(number))) <= DIGITS
    except ValueError:
        return False


def _check_aliases(root: yaml.Node, aliases: list[tuple[yaml.Node, Mark]]) -> None:
    """Refuse a document whose aliases add more than :data:`MAX_VALUES` values: the values it
    stands for with each alias expanded, less the nodes written out. The error is at the alias
    that adds the most."""
    limit = MAX_VALUES + _distinct(root)
    sizes: dict[int, int] = {}
    if _expanded(root, sizes, limit) <= limit:
        return
    _, mark = max(aliases, key=lambda alias: sizes.get(id(alias[0]), 0))
    raise yaml.MarkedYAMLError(
        problem=f'its aliases stand for more than {MAX_VALUES:,} values', problem_mark=mark
    )


def _distinct(root: yaml.Node) -> int:
    """How many nodes a document has, each written out once."""
    seen = {id(root)}
    stack = [root]
    while stack:
        node = stack.pop()
        children = (
            node.value
            if isinstance(node, yaml.SequenceNode)
            else [part for pair in node.value for part in pair]
            if isinstance(node, yaml.MappingNode)
            else []
        )
        for child in children:
            if id(child) not in seen:
                seen.add(id(child))
                stack.append(child)
    return len(seen)


def _expanded(node: yaml.Node, sizes: dict[int, int], limit: int) -> int:
    """How many values a YAML node stands for with its aliases expanded, up to ``limit`` and one
    more, each shared node counted once and remembered; a node that contains itself stands for
    too many."""
    known = sizes.get(id(node))
    if known is not None:
        return known
    sizes[id(node)] = limit + 1
    total = 1
    if isinstance(node, yaml.SequenceNode):
        total += sum(_expanded(item, sizes, limit) for item in node.value)
    elif isinstance(node, yaml.MappingNode):
        total += sum(
            _expanded(key, sizes, limit) + _expanded(value, sizes, limit) for key, value in node.value
        )
    sizes[id(node)] = min(total, limit + 1)
    return sizes[id(node)]
