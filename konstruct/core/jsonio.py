"""JSON in and out, through orjson: graph.json, the build cache and the viewer's data are written
on every build, and the standard library's encoder was a measurable share of build time."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
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


def write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a new file in the same directory that is then renamed
    over it. A symbolic link at ``path`` is replaced, not followed, so links a repository commits
    in its output directory cannot send Konstruct's files elsewhere, and a reader never sees a
    half-written file."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)
        # mkstemp makes the file private; outputs get the permissions a plain write would give.
        mask = os.umask(0o022)
        os.umask(mask)
        Path(temporary).chmod(0o666 & ~mask)
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
