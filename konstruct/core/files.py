"""Whether a file changed: the one rule for every place Konstruct keeps a file to look at again
(the inputs a build read, the SQL files the viewer shows, the files beside an input)."""

from __future__ import annotations

import hashlib
import os
import stat
from array import array
from collections.abc import Iterable, Iterator
from pathlib import Path

BITS = (1 << 64) - 1
"""The low 64 bits of a field, as :func:`listing` hashes it."""


def identity(path: Path) -> list[int] | None:
    """What says a file changed: its size, modification and change times, device and inode, or
    None if it is not a regular file (missing, a directory, a FIFO or a device). The change time
    moves even when a rewrite puts the modification time back, a chmod moves it too, and an
    atomic save gives a new inode."""
    try:
        found = path.stat()
    except (OSError, ValueError):
        return None
    return stated(found) if stat.S_ISREG(found.st_mode) else None


def stated(found: os.stat_result) -> list[int]:
    """:func:`identity` from a regular file's status."""
    return [found.st_size, found.st_mtime_ns, found.st_ctime_ns, found.st_dev, found.st_ino]


def digest(found: list[int]) -> str:
    """A file's :func:`identity` in 16 hexadecimal digits, for a file that is only ever compared
    with itself: BLAKE2b with an 8-byte digest, of its fields in decimal separated by spaces."""
    return hashlib.blake2b(' '.join(map(str, found)).encode(), digest_size=8).hexdigest()


def regular_files(folder: Path, extension: str) -> Iterator[tuple[str, os.stat_result]]:
    """The regular files in ``folder`` whose names end with ``extension``, each with its status,
    in the order the folder lists them. Raises OSError when the folder cannot be listed."""
    with os.scandir(folder) as entries:
        for item in entries:
            name = item.name
            if not name.endswith(extension):
                continue
            try:
                # Build and check use DirEntry.stat(); Windows (inode, device 0) compares size, mtime, ctime.
                found = item.stat()
            except OSError:
                continue
            if stat.S_ISREG(found.st_mode):
                yield name, found


def listing(files: Iterable[tuple[str, os.stat_result]]) -> str:
    """A digest of files as :func:`regular_files` gives them, so a folder whose files are all as
    they were is seen in one hash: BLAKE2b with an 8-byte digest of the names joined by NUL in
    UTF-8, then each file's :func:`identity` as 64-bit integers in the machine's byte order (the
    low 64 bits, two's complement for a time before 1970). The same files listed in another
    order give another digest, which only means they are looked at one by one. Nothing is kept
    of each file, so a folder of thousands costs one pass and one hash."""
    names: list[str] = []
    fields = array('Q')
    add = fields.extend
    for name, found in files:
        names.append(name)
        add(
            (
                found.st_size,
                found.st_mtime_ns & BITS,
                found.st_ctime_ns & BITS,
                found.st_dev,
                found.st_ino & BITS,
            )
        )
    hashed = hashlib.blake2b('\0'.join(names).encode('utf-8', 'surrogateescape'), digest_size=8)
    hashed.update(fields)
    return hashed.hexdigest()
