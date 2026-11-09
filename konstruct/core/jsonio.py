"""JSON in and out, through orjson: graph.json, the build cache and the viewer's data are written
on every build, and the standard library's encoder was a measurable share of build time."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import orjson


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
