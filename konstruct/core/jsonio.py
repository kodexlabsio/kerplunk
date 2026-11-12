"""JSON in and out, through orjson: the build cache, the viewer's data and the commands' output are
written on every build, and the standard library's encoder was a measurable share of build time
(graph.json is written by the graph itself). The build's files are written whole, by the native core,
through a renamed temporary file."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import orjson

from konstruct._core import real_directory, write_text


def dumps(
    value: Any,
    *,
    indent: bool = False,
    sort_keys: bool = False,
    default: Callable[[Any], Any] | None = None,
) -> str:
    """``value`` as JSON text: compact unless ``indent`` (two spaces), non-ASCII text kept as is."""
    option = orjson.OPT_NON_STR_KEYS
    if indent:
        option |= orjson.OPT_INDENT_2
    if sort_keys:
        option |= orjson.OPT_SORT_KEYS
    return orjson.dumps(value, default=default, option=option).decode('utf-8')


def loads(text: str | bytes) -> Any:
    """Parse JSON text."""
    return orjson.loads(text)


def read_jsonl(path: Path) -> list[Any]:
    """The values of a JSON Lines file, one per line that is not blank."""
    return [orjson.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


__all__ = ['dumps', 'loads', 'read_jsonl', 'real_directory', 'write_text']
