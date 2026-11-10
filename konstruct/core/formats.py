"""Format versions of the documents Konstruct writes for programs to read.

Scripts, agents and CI read ``graph.json``, the ``--json`` output of every command, the MCP tool
results and the JSON files in ``konstruct-out``. Each document carries ``format_version`` at its top
level: an integer that changes only with a breaking change to that document's shape (a field
removed, renamed or given another meaning). Adding a field leaves it as it is, so a reader that
knows format ``n`` can read any later document of format ``n``.

Every document is format 1 until its shape first breaks. Then the result type that writes it
declares ``FORMAT_VERSION`` beside the ``to_dict`` that defines the new shape, and
:func:`document` writes that instead. :func:`document` adds the field where a result is written
out, so a result nested in another (an impact inside a diff) stays plain data.
"""

from __future__ import annotations

from typing import Any, Protocol

FIELD = 'format_version'
"""The key every document carries its format version under."""

FIRST = 1
"""The format of every document whose shape has not broken since documents carried a version."""


class FormatError(Exception):
    """A document whose format this Konstruct cannot read: written by a newer Konstruct, or not
    a Konstruct document at all."""


class Result(Protocol):
    def to_dict(self) -> dict[str, Any]: ...


def document(result: Result) -> dict[str, Any]:
    """``result.to_dict()`` with its ``format_version`` first, as written for programs to read:
    the result type's ``FORMAT_VERSION`` if it declares one, else :data:`FIRST`."""
    return {FIELD: getattr(type(result), 'FORMAT_VERSION', FIRST), **result.to_dict()}


def check(data: Any, supported: int, what: str) -> int:
    """The format version of ``data``, a document called ``what``, once it is known to be readable.

    A document without the field is format 1, which is how documents were written before they
    carried one.

    Raises:
        FormatError: ``data`` is not a document, or a newer Konstruct wrote it.
    """
    from konstruct import __version__

    if not isinstance(data, dict):
        raise FormatError(f'{what} is not a Konstruct document (expected a JSON object)')
    version = data.get(FIELD, FIRST)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise FormatError(f'{what} has an unknown {FIELD} {version!r}')
    if version > supported:
        raise FormatError(
            f'{what} was written by a newer Konstruct (format {version}; Konstruct {__version__} reads '
            f'formats 1 to {supported}); upgrade Konstruct to read it'
        )
    return version
