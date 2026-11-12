"""Write graph.html: one self-contained, offline, interactive viewer of the graph.

The viewer is a Svelte and TypeScript app in ``viewer/`` at the root of the repository. Its build
(``pnpm build``) writes ``konstruct/outputs/viewer.html``: one file with the script, the styles and the
Manrope and JetBrains Mono fonts (SIL Open Font License; the licences are ``licenses/manrope.txt`` and
``licenses/jetbrains-mono.txt``) inlined. Konstruct fills its two placeholders with
``Graph.to_dict()`` and with what the viewer shows beside the graph: each column's formula and each
relation's conditions from :mod:`konstruct.analysis.explain`, the SQL files that define models and
tasks (and dbt's compiled SQL when ``target/compiled`` has it), the ontology from
:mod:`konstruct.semantics.ontology`, the metrics of :mod:`konstruct.semantics.world` and the
conflicts from :mod:`konstruct.analysis.conflicts`.
The page loads nothing from the network.

``konstruct serve`` sends the same page unchanged but for one element in its head, which makes it
load ``graph.json`` and ``explain.json`` from the server instead; the build writes
``explain.json``, the object the page embeds (:func:`viewer_data`), beside ``graph.json`` for it.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
from dataclasses import asdict
from functools import cache
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from konstruct._core import file_identity as identity
from konstruct.core import jsonio
from konstruct.core.graph import Graph, Node

if TYPE_CHECKING:
    from konstruct.semantics.ontology import Ontology

log = logging.getLogger(__name__)

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
        log.warning('graph.html: the lineage is too deep to compose formulas; the page shows none')
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
    """Whether ``relative`` changed, by the rule every kept file follows (:func:`identity`)."""
    return identity(root / relative)


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


def world_data(graph: Graph, ontology: Ontology | None = None) -> dict[str, Any]:
    """The world model's metrics as the viewer reads them: every metric defined and proposed, as
    :func:`konstruct.semantics.world.all_metrics` finds them, with the graph ids of its columns in
    place of their formulas, which the page has already. A proposal leaves out what the page
    writes for itself from its source and columns: its evidence, and its grain, one row per its
    entity."""
    from konstruct.semantics.ontology import derive
    from konstruct.semantics.world import all_metrics

    found = ontology if ontology is not None else derive(graph)
    metrics = []
    for metric in all_metrics(graph, found, compose=False):
        entry = {k: v for k, v in metric.to_dict().items() if k != 'columns'}
        if metric.proposed:
            for key in ('evidence', 'grain', 'grain_confidence'):
                entry.pop(key, None)
        ids = [c.id for c in metric.columns if c.id]
        if ids:
            entry['columns'] = ids
        metrics.append(entry)
    return {'metrics': metrics}


def graph_fingerprint(graph: Graph, data: dict[str, Any] | None = None) -> str:
    """A hash of the graph's nodes and edges, not its meta (which records when the build ran), so
    a result computed from one graph can be recognised as still true of the next. ``data`` is
    ``graph.to_dict()``, when the caller has made it already."""
    if data is None:
        data = graph.to_dict()
    content = {'nodes': data['nodes'], 'edges': data['edges'], 'dialect': graph.meta.get('dialect')}
    return hashlib.sha256(jsonio.dumps(content).encode('utf-8')).hexdigest()


def conflicts_data(graph: Graph, directory: Path, graph_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """The conflicts ``konstruct conflicts`` last wrote to ``conflicts.json`` beside the graph, if
    they were found in this same graph; otherwise none. Finding conflicts is a question asked on
    demand: a build does not spend its time on it. ``graph_data`` is ``graph.to_dict()``."""
    path = directory / 'conflicts.json'
    try:
        data = jsonio.loads(path.read_bytes())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get('graph') != graph_fingerprint(graph, graph_data):
        return {}
    found = {k: v for k, v in data.items() if k != 'graph'}
    if isinstance(found.get('conflicts'), list):
        found['conflicts'] = found['conflicts'][:VIEWER_CONFLICT_LIMIT]
    return found


def viewer_data(
    graph: Graph,
    directory: Path,
    cache: Any = None,
    ontology: Ontology | None = None,
    graph_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Everything the page shows beside ``Graph.to_dict()`` (``graph_data``, when it is made
    already), in its second placeholder; ``directory`` is where the page is written, beside
    graph.json and conflicts.json."""
    from konstruct.semantics.ontology import derive

    data = explanations(graph, cache)
    data['sql'] = code(graph, cache)
    found = ontology if ontology is not None else derive(graph)
    data['ontology'] = ontology_data(graph, found)
    data['world'] = world_data(graph, found)
    data['conflicts'] = conflicts_data(graph, directory, graph_data)
    # The page's language follows the reader's browser; KONSTRUCT_LOCALE (such as de or pt-BR)
    # sets the one to use when the reader has not chosen one.
    locale = os.environ.get('KONSTRUCT_LOCALE', '').strip()
    if locale:
        data['locale'] = locale
    return data


def write_html(
    graph: Graph,
    path: Path,
    cache: Any = None,
    ontology: Ontology | None = None,
    data: dict[str, Any] | None = None,
    graph_data: dict[str, Any] | None = None,
) -> Path:
    """Write the interactive viewer for ``graph`` to ``path`` and return ``path``. ``ontology`` is
    the build's, when it has already derived one; ``data`` is what :func:`viewer_data` returns and
    ``graph_data`` is ``graph.to_dict()``, when they have been made already."""
    # Made once: the page embeds it, and the conflicts' fingerprint is a hash of it.
    if graph_data is None:
        graph_data = graph.to_dict()
    if data is None:
        data = viewer_data(graph, path.parent, cache, ontology, graph_data)
    page = (
        template()
        .replace(_EXPLAIN_PLACEHOLDER, _embed_json(data), 1)
        .replace(_DATA_PLACEHOLDER, _embed_json(graph_data), 1)
    )
    jsonio.write_text(path, page)
    return path


def write_viewer_data(data: dict[str, Any], path: Path) -> Path:
    """Write :func:`viewer_data` to ``path`` (``explain.json``), which the viewer loads when
    ``konstruct serve`` serves it, and return ``path``."""
    jsonio.write_text(path, jsonio.dumps(data))
    return path


SERVED = '<meta name="konstruct-serve" content="">'
"""The element that tells the viewer it is served (``viewer/src/lib/source.ts``): its content is
the path the server's files are under, here the root, so the page loads ``/graph.json`` and
``/explain.json`` and follows ``/events``, all on its own origin."""

SERVED_SELECTOR = 'meta[name="konstruct-serve"]'
"""What a viewer build that can be served looks for; a build without it cannot load a graph from
a server."""


def served_page() -> str:
    """The viewer page ``konstruct serve`` sends: the built page unchanged except for the
    :data:`SERVED` element at the start of its head."""
    return template().replace('<head>', f'<head>{SERVED}', 1)
