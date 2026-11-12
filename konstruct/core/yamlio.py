"""YAML in, through the core. Every YAML file Konstruct reads, the repository's or its own, goes
through :func:`read`, :func:`load` or :func:`load_all` (the documents of a stream), and the core
(``src/yaml/``) reads it as PyYAML's safe loader did: YAML 1.1, so a reader handles one error,
:class:`yaml.YAMLError`, for any text it cannot read. PyYAML is imported for its exception
classes, which callers catch, and for the writers that emit YAML; it reads nothing."""

from __future__ import annotations

import codecs
import sys
from pathlib import Path
from typing import Any, NoReturn

import yaml
from yaml.error import Mark

from konstruct import _core

MAX_VALUES = 1_000_000
"""The most values a YAML document's aliases may add once they are expanded: a few hundred bytes
of aliases to aliases stand for millions, which a reader walking the value would expand. Values
written out are not counted, so a large file without aliases is read whole."""
LOAD_DEPTH = 250
"""The deepest nesting of collections :func:`load` reads, counted through aliases: PyYAML's own
recursion gave out between 300 and 400 levels, and no document a person writes comes near either."""
LOAD_ALL_DEPTH = 100
"""The deepest nesting :func:`load_all` reads, aliases expanded. It is lower than
:data:`LOAD_DEPTH` because its documents feed engines that read 128 levels of JSON, so a
document that goes deeper would be read here and refused there."""
DIGITS = sys.get_int_max_str_digits() or sys.int_info.default_max_str_digits
"""The most digits an integer may have: Python's limit for converting one to text, which a reader
does with each value it reports. A sexagesimal integer (``1:30:59``) is read in linear time, and
held to the limit too."""

_ERRORS: dict[str, type[yaml.MarkedYAMLError]] = {
    'scanner': yaml.scanner.ScannerError,
    'parser': yaml.parser.ParserError,
    'composer': yaml.composer.ComposerError,
    'constructor': yaml.constructor.ConstructorError,
}


def read(path: Path, loader: type | None = None, *, lines: bool = False) -> Any:
    """The value of the YAML file at ``path``, as :func:`load` reads it. The core reads the file's
    bytes: a byte order mark, UTF-8 or UTF-16, is understood, and text that is neither raises a
    :class:`yaml.YAMLError`. Raises OSError if the file cannot be read."""
    return load(path.read_bytes(), loader, lines=lines)


_LOADERS = tuple(c for c in (yaml.SafeLoader, getattr(yaml, 'CSafeLoader', None)) if c is not None)


def _check(text: object, loader: type | None) -> None:
    """Refuses what the core does not read: a loader other than PyYAML's two safe loaders, since
    a subclass can change what is read by overriding a method or setting an attribute, which the
    core would ignore; and a source that is not text, which is a :class:`yaml.YAMLError` as it was
    when PyYAML read it."""
    if loader is not None and loader not in _LOADERS:
        raise TypeError(
            'the YAML reader is the core, which reads as yaml.SafeLoader and yaml.CSafeLoader do: '
            f'{loader!r} is neither, and a subclass of one is not read either'
        )
    if not isinstance(text, (str, bytes)):
        raise yaml.MarkedYAMLError(problem=f'YAML is read from str or bytes, not {type(text).__name__}')


def load(text: str | bytes, loader: type | None = None, *, lines: bool = False) -> Any:
    """The value of the YAML document ``text``, None for an empty one. Raises
    :class:`yaml.YAMLError` for any text it cannot read: not YAML, a value that has no meaning
    (a date that does not exist, ``!!bool maybe``), an unpaired surrogate, an integer of more than
    :data:`DIGITS` digits, nesting past :data:`LOAD_DEPTH`, or aliases that add more than
    :data:`MAX_VALUES` values, which is refused before anything expands them. The error's
    ``problem_mark``, when it has one, is the value at fault, or where reading stopped.

    ``loader`` is accepted for callers that passed one: ``yaml.SafeLoader``, ``yaml.CSafeLoader`` or
    None, all read by the core as PyYAML's safe loader reads. Any other loader, a subclass too,
    raises TypeError.

    With ``lines`` every mapping is a dict of string keys and holds the line it starts on, from
    one, as ``__line__``."""
    _check(text, loader)
    try:
        return _core.yaml_load(text, lines=lines, max_values=MAX_VALUES, max_depth=LOAD_DEPTH, digits=DIGITS)
    except _core.YamlFailure as failure:
        _raise(failure, text)


def load_all(text: str | bytes, loader: type | None = None, *, lines: bool = False) -> list[Any]:
    """The value of every document of ``text``, a stream of ``---`` separated documents, empty
    documents left out. Each is read as :func:`load` reads one, with its limits and its errors,
    and nesting past :data:`LOAD_ALL_DEPTH` is refused as well, through aliases too. The values that
    aliases add are one budget of :data:`MAX_VALUES` for the whole stream, so that many small
    bombs are no more than one large one."""
    _check(text, loader)
    try:
        return _core.yaml_load_all(
            text, lines=lines, max_values=MAX_VALUES, max_depth=LOAD_ALL_DEPTH, digits=DIGITS
        )
    except _core.YamlFailure as failure:
        _raise(failure, text)


def _raise(failure: _core.YamlFailure, source: str | bytes) -> NoReturn:
    """The PyYAML exception that describes ``failure``: the class of the stage that refused the
    text, so a reader that caught one still does, with the marks and the problem it carried."""
    stage, context, context_mark, problem, problem_mark = failure.args
    # A mark gets the text it points into, as PyYAML's do, so that a message shows the line.
    # What the reader refused is a byte or a character it could not use, and has none.
    buffer = None if stage == 'reader' else _decode(source) + '\0'
    name = '<byte string>' if isinstance(source, bytes) else '<unicode string>'

    def mark(place: tuple[int, int, int] | None) -> Mark | None:
        if place is None:
            return None
        index, line, column = place
        return Mark(name, index, line, column, buffer, 0 if buffer is None else index)

    error = _ERRORS.get(stage, yaml.MarkedYAMLError)
    raise error(context, mark(context_mark), problem, mark(problem_mark)) from None


def _decode(source: str | bytes) -> str:
    """The characters PyYAML's reader indexes: the text of ``source`` as it decodes, with a byte
    order mark kept as a character."""
    if isinstance(source, str):
        return source
    codec = {codecs.BOM_UTF16_LE: 'utf-16-le', codecs.BOM_UTF16_BE: 'utf-16-be'}.get(source[:2], 'utf-8')
    return source.decode(codec, 'replace')
