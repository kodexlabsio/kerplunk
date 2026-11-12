"""Which of the files a build read have changed since it read them.

Every query command asks this before it answers, so it lives apart from the build, whose imports
(sqlglot's dialects, Jinja, the inputs) a command that only reads the graph does not need. The
files are walked and identified by the native core.
"""

from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from konstruct._core import file_identity as identity
from konstruct._core import files_under, folder_digest, folder_files
from konstruct.core.graph import Graph

IDENTITY = ('size', 'mtime_ns', 'ctime_ns', 'device', 'inode')
"""The fields of each ``watched_files`` entry besides its path, in the order of
:func:`konstruct._core.file_identity`."""


def watched_files(seen: dict[str, list[int]]) -> list[dict[str, Any]]:
    """``watched_files`` as graph.json holds it: each file, as the build found it."""
    return [{'path': path, **dict(zip(IDENTITY, found, strict=True))} for path, found in sorted(seen.items())]


def seen_files(meta: dict[str, Any]) -> dict[str, list[Any]] | None:
    """The files a graph's build found, by path, as :func:`konstruct._core.file_identity` gives
    them; None for a graph from before builds recorded them."""
    found = meta.get('watched_files')
    if not isinstance(found, list):
        return None
    return {str(f.get('path')): [f.get(k) for k in IDENTITY] for f in found if isinstance(f, dict)}


Verdicts = dict[str, tuple[list[int], bool]]
"""Each file in a folder beside an input that is no longer as the build found it, by its name: the
file as it was judged (:func:`konstruct._core.file_identity`) and whether it would now be an
input."""

Judged = dict[tuple[str, str, str, tuple[str, ...], tuple[str, ...], str, str], tuple[str, Verdicts]]
"""What :func:`changed_inputs` concluded of the folders beside an input whose files are not all
as the build found them: by the folder, the extension, the test, its patterns, the exclude
patterns, the repository's root and the digest the build recorded of the folder's listing, the
digest of the listing when it was judged (:func:`konstruct._core.folder_files`) and the
:data:`Verdicts` on its changed files. A folder listed as it was then is not looked at file by
file again, and a file as it was then is not read again, so files that are no inputs and start no
build (helper modules a branch switch rewrote) cost one hash of their folder at each check, as
they would unchanged. A folder whose files are all as the build found them adds nothing."""


def _new_siblings(meta: dict[str, Any], root: Path, out_dir: Path, judged: Judged) -> list[str]:
    """The files beside those a build read, with their extension, that discovery would now take
    and that are new or no longer as the build found them: judged by the rule discovery found
    their neighbour by (:data:`konstruct.engine.config.FINDS`), under that repository's own
    exclude patterns if discovery applied them, and never in the output folder. A folder is
    looked at file by file only when its listing's digest is neither the one the build recorded
    nor the one ``judged`` holds, and a file is read only when ``judged`` does not hold it as it
    is now; ``judged`` keeps what this check found, and nothing else."""
    new = []
    kept: Judged = {}
    for entry in meta.get('watched_siblings', []):
        if not isinstance(entry, dict):
            continue
        folder, extension = root / str(entry.get('folder')), str(entry.get('extension'))
        if folder.resolve() == out_dir:
            continue
        try:
            listed = folder_digest(str(folder), extension)
        except OSError:
            continue
        recorded = str(entry.get('digest'))
        if listed == recorded:
            continue  # every file there as the build found it
        # Only now: discovery's rules bring in the inputs' readers, which a check that finds
        # every folder as the build found it does not need.
        from konstruct.engine.config import FINDS, relative_text

        if entry.get('test') not in FINDS:
            continue
        name = str(entry['test'])
        base = root / str(entry.get('root', '.'))
        patterns = tuple(str(p) for p in entry.get('patterns', []))
        exclude = tuple(str(p) for p in entry.get('exclude', []))
        key = (str(folder), extension, name, patterns, exclude, str(base), recorded)
        before = judged.get(key)
        if before is not None and before[0] == listed:
            verdicts = before[1]
        else:
            try:
                _, files = folder_files(str(folder), extension)
            except OSError:
                continue
            known = dict(zip(entry.get('names', []), entry.get('found', []), strict=False))
            earlier = before[1] if before is not None else {}
            verdicts = {}
            for file_name, now, hashed in files:
                verdict = earlier.get(file_name)
                if verdict is None or verdict[0] != now:
                    if known.get(file_name) == hashed:
                        continue
                    file = folder / file_name
                    relative = relative_text(file, base)
                    excluded = any(fnmatch(relative, pattern) for pattern in exclude)
                    verdict = (now, not excluded and FINDS[name](file, relative, patterns))
                verdicts[file_name] = verdict
        kept[key] = (listed, verdicts)
        new += [os.path.relpath(folder / n, root) for n, (_, taken) in verdicts.items() if taken]
    judged.clear()
    judged.update(kept)
    return new


def changed_inputs(graph_path: Path, graph: Graph, judged: Judged | None = None) -> list[str]:
    """Files the last build read that have changed since it read them, relative to the project
    root: each file it recorded (``watched_files`` in the graph's meta) that is no longer as it
    was just before it was read (:func:`konstruct._core.file_identity`: its size, times, device
    or inode differ, or it is gone), each file it did not find in a directory it read, and each
    file beside a file it read with that file's extension (``watched_siblings``), new or no
    longer as the build found it, that discovery would now take (not excluded, not in the output
    folder): a new SQL file or DAG beside one it read, or a helper rewritten as a DAG. A file
    changed while the build ran, after it was read, is one the graph does not reflect, whenever
    graph.json was written. A graph from before builds recorded what they found is compared with
    graph.json's own time, as it always was.

    ``judged`` is kept from one check to the next by a caller that checks again (see
    :data:`Judged`), so a file beside an input is read once for each change to it."""
    try:
        written = graph_path.stat().st_mtime_ns
    except OSError:
        return []
    seen = seen_files(graph.meta)
    root = Path(str(graph.meta.get('root', graph_path.parent.parent)))
    changed = []
    if seen is not None:
        changed = [name for name, found in seen.items() if identity(root / name) != found]
        changed += _new_siblings(
            graph.meta, root, graph_path.parent.resolve(), {} if judged is None else judged
        )
    for name in graph.meta.get('watched', []):
        path = root / name
        found_here = (
            [(os.path.relpath(path / relative, root), found) for relative, found in files_under(path)]
            if path.is_dir()
            else [(os.path.relpath(path, root), identity(path))]
        )
        for relative, found in found_here:
            if found is None:
                continue
            if seen is not None:
                if relative not in seen:
                    changed.append(relative)
            elif found[1] > written:
                changed.append(relative)
    return sorted(set(changed))
