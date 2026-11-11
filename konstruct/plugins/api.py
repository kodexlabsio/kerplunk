"""The plugin API: what a plugin provides for each entry-point group, and what Konstruct gives it.

Every type here is part of the versioned plugin API (:data:`API_VERSION`). A plugin object declares
the version it targets in ``api_version``; Konstruct refuses one it does not support rather than
run it against types it was not written for.
"""

from __future__ import annotations

import math
import re
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypeGuard

if TYPE_CHECKING:
    from konstruct.analysis.conflicts import Conflicts
    from konstruct.analysis.diff import Diff
    from konstruct.analysis.laws import Laws
    from konstruct.core.graph import Graph, Node
    from konstruct.interfaces.project import Project

API_VERSION: tuple[int, int] = (1, 2)
"""The version of the plugin API this release of Konstruct runs, as ``(major, minor)``. The minor
grows when the API gains something a plugin may use; the major only when a type or protocol
here changes incompatibly. Konstruct runs a plugin that targets the same major and a minor no
higher than its own; a plugin's ``api_version = 1`` means ``(1, 0)``."""

ApiVersion = tuple[int, int]
"""A plugin API version, ``(major, minor)``."""


def api_version(value: Any) -> ApiVersion | None:
    """``value`` as a plugin API version: an int ``1`` is ``(1, 0)``, and a pair or a ``"1.0"``
    string is read as it is; None when it is neither."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return (value, 0)
    if isinstance(value, str) and re.fullmatch(r'\d+\.\d+', value):
        major, minor = value.split('.')
        return (int(major), int(minor))
    if (
        isinstance(value, tuple | list)
        and len(value) == 2
        and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
    ):
        return (value[0], value[1])
    return None


def supported(version: ApiVersion) -> bool:
    """Whether this Konstruct runs a plugin that targets ``version``: the same major, and a minor
    no higher than its own."""
    return version[0] == API_VERSION[0] and version[1] <= API_VERSION[1]


def shown(version: ApiVersion) -> str:
    """``(1, 0)`` as ``1.0``."""
    return f'{version[0]}.{version[1]}'


# Every attribute name Konstruct reads from a node or an edge, in the package or the viewer: a
# plugin that set one would change what Konstruct claims, or hand a reader a value of a type it
# does not expect. Kept in step with the code by tests/test_plugins.py, which finds each name read
# with a string key, a module-level string constant or a loop over strings, from ``.attrs``, from
# a name assigned it, or from ``dict(...)``, ``{**...}``, ``getattr`` or ``... or {}`` of it.
_KONSTRUCT_ATTRS = frozenset(
    {
        'also_via', 'approved', 'bash_command', 'broader', 'checksum', 'column', 'columns', 'columns_guessed', 'command',
        'conditions', 'connection', 'currency', 'dag', 'dbt_name', 'declared_loader', 'definition', 'documented_only',
        'entities', 'exceptions', 'expression', 'fields', 'filters', 'formula', 'formula_dialect', 'fqn', 'grain',
        'identifier_fields', 'in_warehouse', 'incremental_strategy', 'inferred', 'iri', 'kind', 'label',
        'line', 'lineage', 'loaded', 'loaded_by', 'loader', 'loader_role', 'masking_policy', 'materialized', 'materialized_view', 'maturity', 'measure_filters', 'measures', 'model',
        'nested', 'nullable', 'operator', 'orchestrator', 'ossie_authored', 'ossie_columns',
        'ossie_dataset', 'ossie_expression', 'ossie_key', 'ossie_metric', 'ossie_reads',
        'outside_warehouse', 'owner', 'package', 'partitioned_by', 'parts', 'pipeline', 'primary_key',
        'primary_key_enforced', 'read_before', 'registry', 'related', 'repository', 'role',
        'same_relation', 'schedule', 'sensitivity', 'sensors', 'severity', 'source_name', 'sql_path',
        'star_reads', 'status', 'synonyms', 'table_type', 'tags', 'test', 'transform', 'type', 'unique_key',
        'unit', 'url', 'usage', 'verified', 'via', 'view_sql', 'warehouse_tags', 'where',
    }
)  # fmt: skip

PLUGIN_NODE_ATTRS = frozenset(
    {
        'owner', 'test', 'severity', 'schedule', 'operator', 'orchestrator', 'bash_command', 'type',
        'maturity', 'label', 'url',
    }
)  # fmt: skip
"""The attribute names Konstruct reads that a plugin's node may set, each a string with the meaning
Konstruct gives it, which are ``test`` and ``severity`` on a test node, naming the test (such as
``unique`` or ``not_null``) and how much a failure counts, ``owner``, who owns a DAG or task, and
those the graph viewer shows: ``schedule``, ``operator``, ``orchestrator`` and ``bash_command`` for
a DAG or task, and ``type``, ``maturity``, ``label`` and ``url`` for an exposure."""

PLUGIN_LIST_ATTRS = frozenset({'fields'})
"""The attribute names Konstruct reads that a plugin's node or edge may set as a list of strings, with
the meaning Konstruct gives it: ``fields``, the fields a BI view or explore shows, which the Looker
input sets and search by meaning reads."""

PLUGIN_MAP_ATTRS = frozenset({'measures', 'measure_filters'})
"""The attribute names Konstruct reads that a plugin's node may set as a map from strings to
strings, with the meaning Konstruct gives them: ``measures``, each measure a BI view or model
defines with its formula, and ``measure_filters``, a measure's filters as the tool writes them,
which every answer from it applies. The Looker input sets both and the ontology's metrics read
them. Added in plugin API 1.1."""

RESERVED_NODE_ATTRS = _KONSTRUCT_ATTRS - PLUGIN_NODE_ATTRS - PLUGIN_LIST_ATTRS - PLUGIN_MAP_ATTRS
"""The attribute names a plugin's node may not set, which are every name Konstruct reads from
attributes bar :data:`PLUGIN_NODE_ATTRS`; ``sorted(RESERVED_NODE_ATTRS)`` lists them."""

RESERVED_EDGE_ATTRS = (_KONSTRUCT_ATTRS - PLUGIN_LIST_ATTRS) | {'origin', 'source', 'target', 'kind'}
"""The attribute names a plugin's edge may not set, which are those Konstruct sets on every edge
(``origin``, ``source``, ``target``, ``kind``) and every name Konstruct reads from attributes,
such as ``verified``, ``transform``, ``expression``, ``via`` and ``role``, which carry its own
lineage claims; ``sorted(RESERVED_EDGE_ATTRS)`` lists them."""


def _map_problems(where: str, attrs: Mapping[str, Any]) -> list[str]:
    return [
        f'{where}: {key} must be a map of strings to strings'
        for key in sorted(PLUGIN_MAP_ATTRS & set(attrs))
        if type(attrs[key]) is not dict
        or not all(type(k) is str and type(v) is str for k, v in attrs[key].items())
    ]


def _list_problems(where: str, attrs: Mapping[str, Any]) -> list[str]:
    return [
        f'{where}: {key} must be a list of strings'
        for key in sorted(PLUGIN_LIST_ATTRS & set(attrs))
        if type(attrs[key]) is not list or not all(type(item) is str for item in attrs[key])
    ]


MAX_DEPTH = 64
"""How deeply a node's or edge's attribute values, or a cached value, may nest lists and dicts."""

PLUGIN_NODE_KINDS = frozenset({'dag', 'task', 'exposure', 'test'})
"""The kinds of node an input plugin may add. Relations and their columns are named through
:class:`Relation` and :class:`Column`, so that they resolve to the nodes the rest of the build
made for the same warehouse objects."""

PLUGIN_EDGE_KINDS = frozenset({'feeds', 'derived_from', 'runs', 'precedes', 'tests'})
"""The kinds of edge an input plugin may add (see :mod:`konstruct.core.graph` for directions)."""


# Inputs


@dataclass(frozen=True)
class InputFile:
    """One file discovery found for an input."""

    path: str
    """The file, relative to the project root, as node ``path`` values use it."""
    content: bytes
    """Its bytes."""

    @property
    def text(self) -> str:
        """The content as UTF-8 text, with undecodable bytes replaced."""
        return self.content.decode('utf-8', errors='replace')


@dataclass(frozen=True)
class Relation:
    """A warehouse relation named by its key (``schema.table`` or ``catalog.schema.table``), as SQL
    names it. It resolves to the node the build already has for that relation, or to a new
    inferred table."""

    key: str
    """The relation's name, such as ``analytics.orders``."""


@dataclass(frozen=True)
class Column:
    """A column of a warehouse relation. When the build knows the relation's columns from
    something that defines it (the warehouse, dbt, or SQL that writes it) and this column is not
    one of them, the edge is left out with a warning; otherwise the column is created."""

    relation: str
    """The relation's name, as for :class:`Relation`."""
    name: str
    """The column's name."""


Endpoint = str | Relation | Column
"""An edge end: the ``id`` of a node in the same fragment, a relation or a column."""


@dataclass
class PluginNode:
    """A node an input adds. Its id in the graph is the input's name, a dot and ``id``."""

    id: str
    """An id unique within the fragment, such as ``pipeline.daily_revenue``; no ``#`` or line break."""
    kind: str
    """One of :data:`PLUGIN_NODE_KINDS`."""
    name: str
    """Its own name, as people know it."""
    path: str | None = None
    """The file that defines it, relative to the project."""
    description: str | None = None
    """What it is."""
    attrs: dict[str, Any] = field(default_factory=dict)
    """Anything else, as JSON-ready data, under names not in :data:`RESERVED_NODE_ATTRS`."""


@dataclass
class PluginEdge:
    """An edge an input adds, in the graph's directions (``source -> target``)."""

    source: Endpoint
    """Where it starts."""
    target: Endpoint
    """Where it ends."""
    kind: str
    """One of :data:`PLUGIN_EDGE_KINDS`; ``derived_from`` joins two :class:`Column` ends."""
    attrs: dict[str, Any] = field(default_factory=dict)
    """Details, as JSON-ready data, under names not in :data:`RESERVED_EDGE_ATTRS`; ``origin`` is
    set by Konstruct."""
    missing: str | None = None
    """The warning to give, instead of the default, when a :class:`Column` end is not a known
    column."""


@dataclass
class Sql:
    """SQL that Konstruct analyses for column lineage, as it does SQL files."""

    sql: str
    """One or more statements."""
    path: str
    """The file it comes from, relative to the project, for warnings and ``via``."""
    task: str | None = None
    """The ``id`` of the fragment's task node that runs it: the task then feeds the tables the
    SQL writes, and the tables it reads feed the task."""
    dialect: str | None = None
    """Its SQL dialect, when it is not the project's."""


@dataclass
class Source:
    """One line of the build summary and of ``graph.json``'s ``meta.sources``."""

    path: str
    """The file read."""
    detail: str
    """What was found in it."""


@dataclass
class Fragment:
    """What an input read: nodes, edges and SQL to merge into the graph. Konstruct validates the
    whole fragment first and merges nothing from one that is not valid."""

    nodes: list[PluginNode] = field(default_factory=list)
    """Nodes to add."""
    edges: list[PluginEdge] = field(default_factory=list)
    """Edges to add, in order."""
    sql: list[Sql] = field(default_factory=list)
    """SQL to analyse for lineage."""
    warnings: list[str] = field(default_factory=list)
    """Problems with the input, given as build warnings with the input's name in front."""
    sources: list[Source] = field(default_factory=list)
    """What each file held, for the build summary."""
    counts: dict[str, int] = field(default_factory=dict)
    """Counts for :attr:`konstruct.engine.build.BuildResult.inputs`, each a 64-bit integer named
    ``<input>_<key>``."""

    def problems(self) -> list[str]:
        """Everything that stops the fragment being merged; empty when it is valid. Each node,
        edge, SQL statement, source and warning is checked as graph.json would hold it, so what
        passes can be written there and read back unchanged."""
        # A plugin's fragment is checked as data: its types are what the plugin claims, not facts.
        fields: dict[str, Any] = {name: getattr(self, name) for name in _FIELDS}
        found = [
            f'{name} must be a {kind}'
            for name, (cls, kind) in _FIELDS.items()
            if not isinstance(fields[name], cls)
        ]
        if found:
            # A generator would be used up here and merge as nothing: check nothing more.
            return found
        # Each node id with its kind, for edges and SQL that name them.
        ids: dict[str, str] = {}
        for node in fields['nodes']:
            found += _node_problems(node, ids)
        for edge in fields['edges']:
            found += _edge_problems(edge, ids)
        for item in fields['sql']:
            found += _sql_problems(item, ids)
        for warning in fields['warnings']:
            if not _text(warning):
                found.append('warnings must be a list of strings')
                break
            found += _encoding_problems(f'warning {briefly(warning)}', warning)
        for source in fields['sources']:
            if not (isinstance(source, Source) and _text(source.path) and _text(source.detail)):
                found.append('sources must be Source objects with path and detail strings')
                break
            found += _encoding_problems(
                f'source {briefly(source.path)}', {'path': source.path, 'detail': source.detail}
            )
        counts = fields['counts']
        # Count names are shown in REPORT.md's table and the build summary.
        if not all(_text(k) and _COUNT_NAME.fullmatch(k) and _is_count(v) for k, v in counts.items()):
            found.append('counts must map names of lower-case letters, digits and _ to 64-bit integers')
        else:
            found += _encoding_problems('counts', counts, joined=True)
        return found


_FIELDS: dict[str, tuple[type, str]] = {
    'nodes': (list, 'list'),
    'edges': (list, 'list'),
    'sql': (list, 'list'),
    'warnings': (list, 'list of strings'),
    'sources': (list, 'list'),
    'counts': (dict, 'dict'),
}


def _listed(values: frozenset[str]) -> str:
    return ', '.join(sorted(values))


def briefly(value: Any) -> str:
    """``value`` as a message names it: its repr, which escapes what JSON cannot hold, cut short."""
    text = repr(value)
    return text if len(text) <= 80 else f'{text[:77]}...'


def one_line(text: str) -> str:
    """``text`` on one line: what a plugin writes is shown in headings, table rows and diagram
    labels, where a line break would end them."""
    return ' '.join(text.splitlines())


def _breaks(text: str) -> bool:
    return text != one_line(text)


def _text(value: Any) -> TypeGuard[str]:
    """Whether ``value`` is a str itself: a subclass, such as a str enum, can print, compare and
    hash as something else, and every text a plugin gives is shown or stored as it prints."""
    return type(value) is str


_COUNT_NAME = re.compile(r'[a-z0-9_]+')


def _is_count(value: Any) -> bool:
    # bool is an int, and graph.json would write it as true.
    return type(value) is int and -(2**63) <= value < 2**63


def _node_problems(node: Any, ids: dict[str, str]) -> list[str]:
    if not isinstance(node, PluginNode):
        return [f'{briefly(node)} is not a PluginNode']
    found: list[str] = []
    where = f'node {briefly(node.id)}'
    if not _text(node.id) or not node.id or '#' in node.id or _breaks(node.id):
        found.append(f'node id {briefly(node.id)} must be a non-empty string without # or a line break')
    elif node.id in ids:
        found.append(f'node id {briefly(node.id)} is used twice')
    else:
        ids[node.id] = str(node.kind)
    if not _text(node.kind) or node.kind not in PLUGIN_NODE_KINDS:
        found.append(f'{where}: kind {briefly(node.kind)} is not one of {_listed(PLUGIN_NODE_KINDS)}')
    if not _text(node.name) or not node.name:
        found.append(f'{where}: name must be a non-empty string')
    for key in ('path', 'description'):
        if getattr(node, key) is not None and not _text(getattr(node, key)):
            found.append(f'{where}: {key} must be a string')
    if _text(node.path) and _breaks(node.path):
        found.append(f'{where}: path may not contain a line break')
    attrs = _attrs_problems(where, node.attrs, RESERVED_NODE_ATTRS)
    if not attrs:
        attrs = (
            [
                f'{where}: {key} must be a string'
                for key in sorted(PLUGIN_NODE_ATTRS & set(node.attrs))
                if type(node.attrs[key]) is not str
            ]
            + _list_problems(where, node.attrs)
            + _map_problems(where, node.attrs)
        )
    if attrs:
        return found + attrs
    # As graph.json nests it: the node's own fields with its attributes inside.
    plain = {
        'id': node.id,
        'kind': node.kind,
        'name': node.name,
        'description': node.description,
        'path': node.path,
        'attrs': node.attrs,
    }
    return found + _encoding_problems(where, plain)


def _edge_problems(edge: Any, ids: dict[str, str]) -> list[str]:
    if not isinstance(edge, PluginEdge):
        return [f'{briefly(edge)} is not a PluginEdge']
    found: list[str] = []
    where = f'edge {briefly(edge.source)} -> {briefly(edge.target)}'
    if not _text(edge.kind) or edge.kind not in PLUGIN_EDGE_KINDS:
        found.append(f'{where}: kind {briefly(edge.kind)} is not one of {_listed(PLUGIN_EDGE_KINDS)}')
    for end in (edge.source, edge.target):
        found += [f'{where}: {p}' for p in _endpoint_problems(end, ids)]
    if edge.kind == 'derived_from' and not (
        isinstance(edge.source, Column) and isinstance(edge.target, Column)
    ):
        found.append(f'{where}: derived_from joins two columns')
    missing: Any = edge.missing
    if missing is not None and not _text(missing):
        found.append(f'{where}: missing must be a string')
    attrs = _attrs_problems(where, edge.attrs, RESERVED_EDGE_ATTRS) or _list_problems(where, edge.attrs)
    if attrs:
        return found + attrs
    plain = {
        'source': _plain(edge.source),
        'target': _plain(edge.target),
        'kind': edge.kind,
        'attrs': edge.attrs,
        'missing': edge.missing,
    }
    return found + _encoding_problems(where, plain)


def _sql_problems(item: Any, ids: dict[str, str]) -> list[str]:
    if not isinstance(item, Sql) or not _text(item.sql) or not _text(item.path):
        return [f'{briefly(item)} is not Sql with sql and path strings']
    found: list[str] = []
    where = f'sql in {briefly(item.path)}'
    if _breaks(item.path):
        # The path names the SQL in warnings and in what a column is derived via.
        found.append(f'{where}: path may not contain a line break')
    if item.task is not None and (not _text(item.task) or item.task not in ids):
        found.append(f'{where}: task {briefly(item.task)} is not a node of the fragment')
    elif item.task is not None and ids[item.task] != 'task':
        found.append(f'{where}: task {briefly(item.task)} is {_a(ids[item.task])}, not a task')
    if item.dialect is not None and not _is_dialect(item.dialect):
        found.append(f'{where}: {briefly(item.dialect)} is not a SQL dialect Konstruct knows')
    plain = {'sql': item.sql, 'path': item.path, 'task': item.task, 'dialect': item.dialect}
    return found + _encoding_problems(where, plain)


def _plain(end: Any) -> Any:
    """An edge end as plain data, for the encoding check; None for one that is not an end, which
    has its own problem."""
    if isinstance(end, Relation):
        return end.key
    if isinstance(end, Column):
        return [end.relation, end.name]
    return end if _text(end) else None


def _endpoint_problems(end: Any, ids: dict[str, str]) -> list[str]:
    if _text(end):
        return [] if end in ids else [f'{briefly(end)} is not a node of the fragment']
    if isinstance(end, Relation):
        if not _text(end.key) or not _names_relation(end.key):
            return ['a Relation needs a key']
        # A # separates a relation from its column in node ids: it would name a column node.
        if '#' in end.key:
            return ['a Relation key may not contain #']
        return ['a Relation key may not contain a line break'] if _breaks(end.key) else []
    if isinstance(end, Column):
        valid = _text(end.relation) and _names_relation(end.relation) and _text(end.name)
        if not valid or not end.name:
            return ['a Column needs a relation and a name']
        if '#' in end.relation + end.name:
            return ["a Column's relation and name may not contain #"]
        return (
            ["a Column's relation and name may not contain a line break"]
            if _breaks(end.relation + end.name)
            else []
        )
    return [f'{briefly(end)} is not a node id, Relation or Column']


def _names_relation(key: str) -> bool:
    """Whether ``key`` names a relation once Konstruct normalises it, as the merge does."""
    from konstruct.core.graph import relation_key
    from konstruct.core.sql import PLACEHOLDER

    return bool(relation_key(*[p for p in key.split('.') if p != PLACEHOLDER]))


def _attrs_problems(where: str, attrs: Any, reserved: frozenset[str]) -> list[str]:
    """What is wrong with a node's or edge's attributes before they are encoded: their shape, the
    names Konstruct reserves, and what JSON cannot hold that orjson would write anyway."""
    if not isinstance(attrs, dict) or not all(_text(k) for k in attrs):
        return [f'{where}: attrs must be a dict with string keys']
    taken = sorted(reserved & set(attrs))
    if taken:
        return [f'{where}: attrs may not set {", ".join(taken)}']
    problem = _shape_problem(attrs, plural=True)
    return [f'{where}: attrs {problem}'] if problem else []


def _shape_problem(value: Any, *, plural: bool = False) -> str | None:
    """What makes ``value`` unfit for graph.json beyond what the encoder refuses: anything but
    dicts with string keys, lists, tuples, strings, integers, floats, booleans and None, which the
    encoder would write in a form that does not read back as it is; nesting deeper than
    :data:`MAX_DEPTH`, which the encoder refuses only near its own limit, once graph.json's own
    levels are added; and a float JSON has no number for, which the encoder writes as null."""
    # Breadth first, so the problem named is the first in the order the value is written.
    hold = 'hold' if plural else 'holds'
    queue: deque[tuple[Any, int]] = deque([(value, 0)])
    while queue:
        item, depth = queue.popleft()
        kind = type(item)
        if kind in (dict, list, tuple):
            if depth > MAX_DEPTH:
                return f'{"nest" if plural else "nests"} more than {MAX_DEPTH} levels deep'
            if kind is dict:
                bad = next((k for k in item if type(k) is not str), None)
                if bad is not None:
                    # The encoder would write it as a string, so it would not read back as it is.
                    return f'{hold} a dict whose key {briefly(bad)} is not a string'
            queue.extend((v, depth + 1) for v in (item.values() if kind is dict else item))
        elif kind not in _SCALARS:
            # The encoder writes dataclasses, dates, enums and UUIDs, and subclasses of str, int,
            # dict and list, its own way, and looks inside none of them for depth or NaN.
            return f'{hold} {_a(kind.__name__)}, which is not JSON data'
        elif kind is float and not math.isfinite(item):
            return f'{hold} {item!r}, which JSON has no number for'
    return None


_SCALARS = (str, int, float, bool, type(None))


def _a(name: str) -> str:
    """``name`` with its indefinite article: an exposure, a UUID."""
    return f'{"an" if name[:1].lower() in "aeio" else "a"} {name}'


def _encoding_problems(where: str, value: Any, *, joined: bool = False) -> list[str]:
    from konstruct.core import jsonio

    try:
        # The encoder graph.json is written with, so what passes here can be written there.
        jsonio.dumps(value)
    except TypeError as error:
        return [f'{where} {"are" if joined else "is"} not JSON-ready data ({error})']
    return []


def json_problem(value: Any) -> str | None:
    """What stops ``value`` being written to graph.json or the build cache and read back as it
    is, such as ``is not JSON-ready data (...)``; None when nothing does."""
    problem = _shape_problem(value)
    if problem is not None:
        return problem
    found = _encoding_problems('', value)
    return found[0].strip() if found else None


def _is_dialect(name: Any) -> bool:
    from sqlglot.dialects.dialect import Dialect

    if not _text(name):
        return False
    try:
        Dialect.get_or_raise(name)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class InputContext:
    """What Konstruct gives an input besides its files."""

    root: Path
    """The project root, for reference; read files from :class:`InputFile`, not from here."""
    dialect: str | None
    """The project's SQL dialect, when one is known."""
    options: Mapping[str, Any]
    """The input's own table in ``konstruct.toml`` (``[<name>]``), or an empty mapping."""
    cached: Callable[[tuple[Any, ...], Callable[[], Any]], Any]
    """Called as ``cached(key, compute)``, gives the JSON-ready value ``compute()`` returns, kept
    for ``konstruct build --update`` while ``key`` (a tuple of JSON-ready parts, such as a hash
    of a file's content) and the plugin's version are unchanged. A value that is not JSON-ready,
    or nests deeper than :data:`MAX_DEPTH`, raises an error and is not kept."""


class InputPlugin(Protocol):
    """``konstruct.inputs``: a reader for a kind of file (an orchestrator, BI tool or loader)."""

    api_version: ApiVersion | int
    """The plugin API version the input targets."""
    patterns: Sequence[str]
    """File-name patterns to discover, such as ``*.pipeline.yml``; ``[<name>] paths`` in
    ``konstruct.toml`` replaces discovery. With no patterns, :meth:`read` is called once with no
    files (an input that reads from elsewhere)."""

    def read(self, files: Sequence[InputFile], context: InputContext) -> Fragment:
        """What the files hold, as a fragment of the graph."""
        ...


# Exports


@dataclass(frozen=True)
class ExportRequest:
    """The options of one ``konstruct export``."""

    select: Node | None = None
    """The node given with ``--select``, if any."""
    keep: frozenset[str] | None = None
    """With ``select``, the ids of the relations and tasks in its lineage, both directions."""
    columns: bool = False
    """``--columns``: draw columns as well as relations."""
    namespace: str | None = None
    """``--namespace``: a dataset namespace."""
    job_namespace: str = 'konstruct'
    """The namespace of jobs, for formats that have one."""
    event_time: str | None = None
    """A fixed event time, for formats that have one."""


class ExportPlugin(Protocol):
    """``konstruct.exports``: a format for ``konstruct export``, named by the entry point."""

    api_version: ApiVersion | int
    """The plugin API version the format targets."""
    description: str
    """One line: what the format is for."""

    def export(self, graph: Graph, request: ExportRequest) -> str:
        """The graph (or the lineage ``request.keep`` names) in the format."""
        ...


# Checks


@dataclass
class Finding:
    """One thing that stops a change, from a gate check."""

    rule: str
    """The rule: ``breaking``, ``approved-definition-changed``, ``conflicting-definition``,
    ``data-law-broken`` or ``consumer-pin-changed`` for Konstruct's own checks, ``<plugin>.<rule>``
    for a plugin's."""
    message: str
    """What is wrong."""
    path: str | None = None
    """The file to change, when one is known, relative to the project."""
    fix: str = ''
    """What to do about it."""


def finding_problem(finding: Any) -> str | None:
    """What makes a plugin check's ``finding`` unfit for the gate's text, markdown and JSON: one
    that is not a :class:`Finding` with a non-empty string ``rule``, ``message`` and ``fix`` and a
    string or None ``path``, all text JSON can hold; None when nothing does."""
    if not isinstance(finding, Finding):
        return f'{briefly(finding)} is not a Finding'
    texts = (finding.rule, finding.message, finding.fix)
    if not all(type(t) is str and t for t in texts) or not (
        finding.path is None or type(finding.path) is str
    ):
        return f'{briefly(finding)} needs a rule, message and fix that are strings, and a string path or None'
    found = _encoding_problems('finding', [*texts, finding.path])
    return found[0] if found else None


@dataclass(frozen=True)
class CheckContext:
    """What a gate check sees: the graph at the head and what the change does."""

    graph: Graph
    """The graph, up to date with the change."""
    diff: Diff
    """The change, as ``konstruct diff`` describes it."""
    conflicts: Conflicts
    """The conflicting definitions at the head, as ``konstruct conflicts`` finds them."""
    laws: Laws | None = None
    """The data laws as recorded before the change: the ``laws.json`` that ``konstruct verify
    --laws`` writes, as committed at the base, or, with no git base to read, as it is in the
    output directory; None when there is none. Since plugin API 1.2."""


class CheckPlugin(Protocol):
    """``konstruct.checks``: a rule of ``konstruct gate``. Each :class:`Finding` it returns fails
    the gate; a third-party plugin's rules are named ``<entry point>.<rule>``."""

    api_version: ApiVersion | int
    """The plugin API version the check targets."""
    description: str
    """One line: what the check stops."""

    def check(self, context: CheckContext) -> list[Finding]:
        """What stops the change, each finding with its fix."""
        ...


# MCP tools


class ToolsPlugin(Protocol):
    """``konstruct.mcp_tools``: tools for ``konstruct mcp``, each named ``<entry point>_<tool>``."""

    api_version: ApiVersion | int
    """The plugin API version the tools target."""

    def tools(self, project: Project) -> Mapping[str, Callable[..., Any]]:
        """Each tool's short name with its function, bound to ``project``. The function's typed
        parameters are the tool's input and its docstring the description agents read; it
        returns JSON-ready data."""
        ...


__all__ = [
    'API_VERSION',
    'MAX_DEPTH',
    'PLUGIN_EDGE_KINDS',
    'PLUGIN_LIST_ATTRS',
    'PLUGIN_MAP_ATTRS',
    'PLUGIN_NODE_ATTRS',
    'PLUGIN_NODE_KINDS',
    'RESERVED_EDGE_ATTRS',
    'RESERVED_NODE_ATTRS',
    'CheckContext',
    'CheckPlugin',
    'Column',
    'Endpoint',
    'ExportPlugin',
    'ExportRequest',
    'Finding',
    'Fragment',
    'InputContext',
    'InputFile',
    'InputPlugin',
    'PluginEdge',
    'PluginNode',
    'Relation',
    'Source',
    'Sql',
    'ToolsPlugin',
]
