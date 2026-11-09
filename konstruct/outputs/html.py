"""Write graph.html: one self-contained, offline, interactive viewer of the graph.

The viewer is a Svelte and TypeScript app in ``viewer/`` at the root of the repository. Its build
(``pnpm build``) writes ``konstruct/outputs/viewer.html``: one file with the script, the styles and the
Manrope and JetBrains Mono fonts (SIL Open Font License; the licences are in ``licenses/``)
inlined. Konstruct fills its two placeholders with
``Graph.to_dict()`` and with what the viewer shows beside the graph: each column's formula and each
relation's conditions from :mod:`konstruct.analysis.explain`, the SQL files that define models and
tasks (and dbt's compiled SQL when ``target/compiled`` has it), the ontology from
:mod:`konstruct.semantics.ontology` and the conflicts from :mod:`konstruct.analysis.conflicts`.
The page loads nothing from the network.
"""

from __future__ import annotations

import functools
import os
import stat
from dataclasses import asdict
from functools import cache
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from konstruct.core import jsonio
from konstruct.core.graph import Graph, Node

if TYPE_CHECKING:
    from konstruct.semantics.ontology import Ontology

_DATA_PLACEHOLDER = '__KONSTRUCT_DATA__'
_EXPLAIN_PLACEHOLDER = '__KONSTRUCT_EXPLAIN__'

# Inside <script type="application/json"> the HTML parser only looks for "</script"; escaping
# "<", ">" and "&" rules that out entirely, and U+2028/U+2029 are escaped for older JS parsers.
_JSON_ESCAPES = (
    ('<', '\\u003c'),
    ('>', '\\u003e'),
    ('&', '\\u0026'),
    ('\u2028', '\\u2028'),
    ('\u2029', '\\u2029'),
)

VIEWER_FORMULA_LIMIT = 800
# A file longer than this is cut, with a note; the viewer is for reading SQL, not for archiving it.
VIEWER_SQL_LIMIT = 100_000
# Beyond this much SQL in all, the page would be slow to open; later files are left out.
VIEWER_SQL_TOTAL = 24_000_000
VIEWER_CONFLICT_LIMIT = 100
_CODE_KINDS = frozenset({'model', 'snapshot', 'seed', 'test'})
_CODE_SUFFIXES = ('.sql', '.py')


@cache
def template() -> str:
    """The built viewer page, with the data placeholders still open."""
    page = resources.files('konstruct.outputs').joinpath('viewer.html').read_text(encoding='utf-8')
    if _DATA_PLACEHOLDER not in page or _EXPLAIN_PLACEHOLDER not in page:
        raise RuntimeError('konstruct/outputs/viewer.html is not a viewer build; run `pnpm build` in viewer/')
    return page


def _embed_json(data: Any) -> str:
    # A pass of str.replace per character runs in C; str.translate with a mapping calls back
    # into Python for every character, over 1 s for a 2,000-model graph. No replacement holds a
    # character another one replaces, so the order does not matter.
    text = jsonio.dumps(data)
    for character, escape in _JSON_ESCAPES:
        text = text.replace(character, escape)
    return text


def explanations(graph: Graph, cache: Any = None) -> dict[str, Any]:
    """Column formulas and relation conditions for the viewer, or empty if they cannot be made.
    ``cache`` is the build cache, so an update recomputes only the formulas that changed."""
    from konstruct.analysis.explain import Explainer

    try:
        # A formula longer than this is not readable at a glance; the panel points to
        # `konstruct explain` for the full composition.
        return Explainer(graph, max_composed=VIEWER_FORMULA_LIMIT).summaries(cache=cache)
    except RecursionError:
        return {}


def _read(root: Path, relative: str) -> str | None:
    """A file under ``root`` (a resolved path), or None if it is missing, unreadable or outside
    the project, through a symbolic link or otherwise."""
    try:
        path = root / relative
        # Most of dbt's compiled paths tried are missing, and a stat is cheaper than resolving.
        if not path.is_file():
            return None
        path = path.resolve()
        # A string comparison: Path.is_relative_to costs more than reading the file.
        if not str(path).startswith(str(root).rstrip(os.sep) + os.sep) or not path.is_file():
            return None
        return path.read_text(encoding='utf-8', errors='replace')
    except (OSError, ValueError):
        return None


def _stat(root: Path, relative: str) -> list[int] | None:
    """What says a file changed: its size, modification and change times, device and inode, or
    None if it is not a file. The change time moves even when a rewrite puts the modification time
    back."""
    try:
        found = (root / relative).stat()
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(found.st_mode):
        return None
    return [found.st_size, found.st_mtime_ns, found.st_ctime_ns, found.st_dev, found.st_ino]


def _compiled_path(node: Node, path: str) -> list[str]:
    """Where dbt writes a node's compiled SQL: ``target/compiled/<package>/<path>`` beside the dbt
    project, which may be any folder above the file."""
    package = node.attrs.get('package')
    if not isinstance(package, str) or not package:
        return []
    parts = path.split('/')
    return [
        '/'.join([*parts[:i], 'target', 'compiled', package, *parts[i:]])
        for i in range(len(parts) - 1, -1, -1)
    ]


def _code_path(node: Node) -> str | None:
    if node.kind in _CODE_KINDS and node.path and node.path.endswith(_CODE_SUFFIXES):
        return node.path
    if node.kind == 'task' and isinstance(node.attrs.get('sql_path'), str):
        return str(node.attrs['sql_path'])
    return None


def _code_entry(root: Path, path: str, compiled: list[str]) -> dict[str, Any] | None:
    source = _read(root, path)
    if source is None:
        return None
    entry: dict[str, Any] = {}
    for key, text in (('s', source), ('c', _first(root, compiled))):
        if text is None or (key == 'c' and text == source):
            continue
        if len(text) > VIEWER_SQL_LIMIT:
            text = text[:VIEWER_SQL_LIMIT]
            entry['t'] = 1
        entry[key] = text
    return entry


def code(graph: Graph, cache: Any = None) -> dict[str, dict[str, Any]]:
    """The SQL behind each model, snapshot, singular test and task, read from the project: ``s`` is
    the file as written, ``c`` dbt's compiled SQL when ``target/compiled`` has it, ``t`` is set when
    a file was cut at :data:`VIEWER_SQL_LIMIT` characters. Nothing is run; files outside the project
    root are not read. With ``cache`` (the build cache), a file is read again only when its size,
    modification or change time, device or inode changes, or its compiled copy appears, goes
    or changes."""
    root_text = graph.meta.get('root')
    if not isinstance(root_text, str) or not root_text:
        return {}
    try:
        root = Path(root_text).resolve()
    except OSError:
        return {}
    if not root.is_dir():
        return {}
    found: dict[str, dict[str, Any]] = {}
    total = 0
    # Most of the places dbt's compiled SQL might be are in folders that do not exist; each folder
    # is looked for once, not once for every file.
    folders: dict[str, bool] = {}

    def present(relative: str) -> bool:
        folder = relative.rpartition('/')[0]
        if folder not in folders:
            folders[folder] = (root / folder).is_dir()
        return folders[folder]

    for node in sorted(graph.nodes.values(), key=lambda n: n.id):
        path = _code_path(node)
        if path is None:
            continue
        compiled = [candidate for candidate in _compiled_path(node, path) if present(candidate)]
        compute = functools.partial(_code_entry, root, path, compiled)
        if cache is None:
            entry = compute()
        else:
            # A stat costs less than resolving and reading a file; at 2,000 models the reads took
            # seconds of every build.
            copies = [[candidate, _stat(root, candidate)] for candidate in compiled]
            key = jsonio.dumps(['viewer-sql', VIEWER_SQL_LIMIT, str(root), path, _stat(root, path), copies])
            entry = cache.get_or_compute(key, compute)
        if entry is None:
            continue
        total += len(entry.get('s', '')) + len(entry.get('c', ''))
        if total > VIEWER_SQL_TOTAL:
            break
        found[node.id] = entry
    return found


def _first(root: Path, candidates: list[str]) -> str | None:
    for candidate in candidates:
        text = _read(root, candidate)
        if text is not None:
            return text
    return None


def _plain(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v not in (None, [], '', ())}


def ontology_data(graph: Graph, ontology: Ontology | None = None) -> dict[str, Any]:
    """The ontology as the viewer reads it: ``ontology.yml`` with the graph ids of key and measure
    columns, so the page can link to them."""
    from konstruct.semantics.ontology import derive

    found = ontology if ontology is not None else derive(graph)
    return {
        'entities': [_plain(asdict(e)) for e in found.entities],
        'measures': [
            {
                **_plain({k: v for k, v in asdict(m).items() if k != 'columns'}),
                'columns': [_plain(asdict(c)) for c in m.columns],
            }
            for m in found.measures
        ],
        'relationships': [_plain(asdict(r)) for r in found.relationships],
        'concepts': [_plain(asdict(c)) for c in found.concepts],
        'rules': [_plain(asdict(r)) for r in found.rules],
    }


def conflicts_data(graph: Graph) -> dict[str, Any]:
    """The most consequential conflicting definitions, as ``konstruct conflicts --json`` gives them."""
    from konstruct.analysis.conflicts import find_conflicts

    try:
        return find_conflicts(graph, VIEWER_CONFLICT_LIMIT).to_dict()
    except RecursionError:
        return {}


def viewer_data(graph: Graph, cache: Any = None, ontology: Ontology | None = None) -> dict[str, Any]:
    """Everything the page shows beside ``Graph.to_dict()``, in its second placeholder."""
    data = explanations(graph, cache)
    data['sql'] = code(graph, cache)
    data['ontology'] = ontology_data(graph, ontology)
    data['conflicts'] = conflicts_data(graph)
    return data


def write_html(graph: Graph, path: Path, cache: Any = None, ontology: Ontology | None = None) -> Path:
    """Write the interactive viewer for ``graph`` to ``path`` and return ``path``. ``ontology`` is
    the build's, when it has already derived one."""
    page = (
        template()
        .replace(_EXPLAIN_PLACEHOLDER, _embed_json(viewer_data(graph, cache, ontology)), 1)
        .replace(_DATA_PLACEHOLDER, _embed_json(graph.to_dict()), 1)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_text(path, page)
    return path
