"""The operations of ``konstruct serve``'s HTTP API.

The server (``konstruct._core.Server``) routes every request and checks its parameters against
the API's route table (``src/serve/api.rs``). It then calls :class:`Api` with the operation's id
and its checked arguments, on a thread of its own, and keeps the answer until the graph changes
when the operation depends only on the graph. Each operation is the Python API's own function, so
the CLI, the MCP server and the HTTP API give one answer from one implementation. None of them
writes a file, runs SQL or reads a warehouse.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from konstruct import _core
from konstruct.analysis import query
from konstruct.analysis.query import QueryError, item
from konstruct.core import jsonio
from konstruct.core.graph import Graph, Node
from konstruct.interfaces.project import Project

log = logging.getLogger(__name__)

JSON = 'application/json'

EXPORT_MEDIA = {
    'openlineage': 'application/x-ndjson',
    'mermaid': 'text/plain; charset=utf-8',
    'dot': 'text/vnd.graphviz; charset=utf-8',
    'csv': 'text/csv; charset=utf-8',
    'ossie': 'application/yaml',
}


def _node(project: Project, node_id: str) -> Node:
    """The node with this id; the routes that take an id take nothing else."""
    node = project.graph.nodes.get(node_id)
    if node is None:
        raise QueryError(
            f'nothing in the graph matches {node_id!r}: no node has this id; '
            '/api/v1/search?q=<words> and /api/v1/resolve?name=<name> find ids'
        )
    return node


def _entry(project: Project, id: str) -> dict[str, Any]:
    """The node as graph.json holds it, its label, and the edges into and out of it."""
    graph = project.graph
    node = _node(project, id)
    return {
        'node': node.to_dict(),
        'label': graph.label(node),
        'incoming': [edge.to_dict() for edge in graph.in_edges(node.id)],
        'outgoing': [edge.to_dict() for edge in graph.out_edges(node.id)],
    }


def _walk(project: Project, direction: str, id: str, depth: int | None = None) -> dict[str, Any]:
    walk = query.impact if direction == 'downstream' else query.trace
    return walk(project.graph, _node(project, id).id, depth).to_dict()


def _path(project: Project, **ends: str) -> dict[str, Any]:
    start, end = (_node(project, ends[name]).id for name in ('from', 'to'))
    return query.path(project.graph, start, end).to_dict()


def _search(project: Project, q: str, kind: list[str] | None = None) -> dict[str, Any]:
    # Every match, ranked once: the server cuts the pages from this one answer.
    graph = project.graph
    return query.search(graph, q, limit=len(graph.nodes), kinds=kind).to_dict()


def _resolve(project: Project, name: str) -> dict[str, Any]:
    graph = project.graph
    return {'name': name, 'nodes': [item(graph, node).to_dict() for node in graph.resolve(name)]}


def _status(project: Project) -> dict[str, Any]:
    from konstruct.interfaces.status import inspect

    return inspect(project.path, project.out_dir).to_dict()


def _report(project: Project) -> dict[str, Any]:
    """What REPORT.md summarises, as data."""
    from konstruct.outputs.report import analyse

    graph = project.graph
    findings = analyse(graph)
    stats = graph.stats()

    def items(nodes: list[Any]) -> list[dict[str, Any]]:
        return [item(graph, node).to_dict() for node in nodes]

    return {
        'nodes': {k: v for k, v in stats.items() if not k.startswith('edge:')},
        'edges': {k[5:]: v for k, v in stats.items() if k.startswith('edge:')},
        'lineage': {'covered': findings.lineage_covered, 'columns': findings.lineage_total},
        'hubs': [{**item(graph, node).to_dict(), 'downstream': count} for node, count in findings.hubs],
        'orphans': items(findings.orphans),
        'untested': items(findings.untested),
        'no_lineage': items(findings.no_lineage),
        'documented_missing': items(findings.documented_missing),
        'warnings': list(graph.warnings),
    }


def _export(
    project: Project,
    format: str,
    select: str | None = None,
    columns: bool = False,
    namespace: str | None = None,
) -> tuple[str, str]:
    text = project.export(format, select=select, columns=columns, namespace=namespace)
    return EXPORT_MEDIA[format], text


# An absolute POSIX or Windows path, not one inside a word, a number or a relative path.
_ABSOLUTE_PATH = re.compile(r'(?<![\w.\\/])(?:[A-Za-z]:)?(?:[\\/][^\s\'"\\/:,;()\[\]{}<>|]+)+[\\/]?')


def without_paths(text: str) -> str:
    """``text`` with each absolute path cut to its last part, so an error a client is sent says
    which file failed without saying where this machine keeps it."""
    return _ABSOLUTE_PATH.sub(lambda found: re.split(r'[\\/]', found.group(0).rstrip('\\/'))[-1], text)


Answer = dict[str, Any] | tuple[str, str]

OPERATIONS: dict[str, Callable[..., Answer]] = {
    'getNode': _entry,
    'impact': lambda p, id, depth=None: _walk(p, 'downstream', id, depth),
    'trace': lambda p, id, depth=None: _walk(p, 'upstream', id, depth),
    'path': _path,
    'search': _search,
    'listNodes': lambda p, kind=None: p.list(kind).to_dict(),
    'resolve': _resolve,
    'describe': lambda p, ref: p.describe(ref).to_dict(),
    'explain': lambda p, ref: p.explain(ref).to_dict(),
    'define': lambda p, ref: p.define(ref).to_dict(),
    'listTerms': lambda p: p.list(['term']).to_dict(),
    'ontology': lambda p: p.ontology().to_dict(),
    'conflicts': lambda p: p.conflicts().to_dict(),
    'query': lambda p, q, depth=1: p.query(q, depth=depth).to_dict(),
    'history': lambda p, ref, limit=50: p.history(ref, limit=limit).to_dict(),
    'diff': lambda p, base=None, head=None: p.diff(base=base, head=head).to_dict(),
    # The served graph as it is: an update would write it.
    'gate': lambda p, base=None: p.gate(base, update=False).to_dict(),
    'status': _status,
    'report': _report,
    'export': _export,
}
"""Each Python operation of the API by its OpenAPI ``operationId``."""


class _Served(Project):
    """A project whose graph is the one :class:`Api` read, never reread behind its back, so an
    answer and the version it is reported with come from the same bytes."""

    def __init__(self, root: Path, out_dir: Path, plugins: bool) -> None:
        super().__init__(root, out_dir=out_dir, plugins=plugins)
        self.served: Graph | None = None

    @property
    def graph(self) -> Graph:
        if self.served is None:
            raise QueryError(f'no graph at {self.graph_path}; run `konstruct build` first')
        return self.served


class Api:
    """Answers the API's operations for the graph in ``out_dir``, one at a time.

    Called as ``api(operation, arguments)``, it returns ``('ok', media type, body, version)``, or
    a problem type of the API and what went wrong, as ``(type, '', detail, version)``. ``version``
    is the :func:`konstruct._core.graph_version` of the ``graph.json`` the answer came from: a
    build can replace the file after the server last looked, and the server keeps the answer
    under the graph it is true of.
    """

    def __init__(self, project_root: Path, out_dir: Path, plugins: bool = True) -> None:
        # Its own Project, so a rebuild by `--watch` never changes the graph under an answer.
        self.project = _Served(project_root, out_dir, plugins)
        self._lock = threading.Lock()
        self._stamp: tuple[int, int, int] | None = None
        self.version = ''

    def _load(self) -> None:
        """Read graph.json again if it was replaced since the last answer, and its version from
        the same bytes."""
        try:
            stat = self.project.graph_path.stat()
        except OSError:
            return
        stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        if stamp == self._stamp and self.project.served is not None:
            return
        data = self.project.graph_path.read_bytes()
        self.project.served = Graph.from_json(data, str(self.project.graph_path))
        self.version = _core.graph_version(data)
        # Taken before the read: a file replaced during it is read again next time.
        self._stamp = stamp

    def __call__(self, operation: str, arguments: dict[str, Any]) -> tuple[str, str, str, str]:
        with self._lock:
            kind, media, body = self._answer(operation, arguments)
            return kind, media, body, self.version

    def _answer(self, operation: str, arguments: dict[str, Any]) -> tuple[str, str, str]:
        from konstruct.analysis.diff import DiffError
        from konstruct.outputs.export import ExportError

        handler = OPERATIONS.get(operation)
        if handler is None:
            return ('internal-error', '', f'{operation} has no Python answer')
        try:
            self._load()
            answer = handler(self.project, **arguments)
        except QueryError as error:
            missing = str(error).startswith('nothing in the graph matches')
            return ('no-such-node' if missing else 'not-answerable', '', str(error))
        except (DiffError, ExportError) as error:
            return ('not-answerable', '', str(error))
        except Exception as error:  # reported to the client, and the server keeps serving
            log.error('%s failed', operation, exc_info=error)
            return ('internal-error', '', without_paths(f'{type(error).__name__}: {error}'))
        if isinstance(answer, tuple):
            media, text = answer
            return ('ok', media, text)
        return ('ok', JSON, jsonio.dumps(answer))
