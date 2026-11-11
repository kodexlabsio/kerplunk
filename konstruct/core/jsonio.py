"""JSON in and out, through orjson: graph.json, the build cache and the viewer's data are written
on every build, and the standard library's encoder was a measurable share of build time. The
build's files are written whole, through a renamed temporary file."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import time
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


def _umask() -> int:
    # The umask can only be read by setting it; do so once, at import, before any thread starts.
    mask = os.umask(0)
    os.umask(mask)
    return mask


_UMASK = _umask()
# On Windows a reader holding the file open makes the rename fail for a moment.
_REPLACE_TRIES = 5 if sys.platform == 'win32' else 1


def write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a new file in the same directory that is then renamed
    over it, so a reader (an MCP server, or a hook run at the same time) sees the old file or the
    new one, never half of one. A symbolic link at ``path`` is replaced, not followed, so links a
    repository commits in its output directory cannot send Konstruct's files elsewhere. A regular
    file keeps the mode it had; a new one gets the mode an ordinary write would give it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # lstat, so a link's target is neither followed nor allowed to set the mode.
        current = path.lstat()
        mode = stat.S_IMODE(current.st_mode) if stat.S_ISREG(current.st_mode) else 0o666 & ~_UMASK
    except OSError:
        mode = 0o666 & ~_UMASK
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)
        # mkstemp makes the file private; outputs get the mode chosen above.
        Path(temporary).chmod(mode)
        for attempt in range(_REPLACE_TRIES):
            try:
                Path(temporary).replace(path)
                break
            except PermissionError:
                if attempt == _REPLACE_TRIES - 1:
                    raise
                time.sleep(0.05 * 2**attempt)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def real_directory(base: Path, *parts: str) -> Path:
    """``base`` joined with ``parts``, made as real directories: a symbolic link in the way is
    removed first, so one committed inside the output directory cannot send outputs elsewhere."""
    current = base
    current.mkdir(parents=True, exist_ok=True)
    for part in parts:
        current = current / part
        if current.is_symlink():
            current.unlink()
        current.mkdir(exist_ok=True)
    return current
