"""Where landed tables come from, read from the files that configure the loaders.

Each format gives :class:`Feed` records: a loader's task (a pipeline, connection or connector),
the source system's table it reads (``postgres.app.public.orders``), the table it lands, and the
columns it copies when the file names them. Nothing is imported, run or connected to: Python is
parsed with :mod:`ast`, YAML with a safe loader, JSON with :mod:`json` and Terraform with
:mod:`konstruct.inputs.hcl`. A stream between two loaders, a Kafka topic or an Estuary
collection, is a node of its own outside the warehouse, so a Debezium connector and the sink that
reads its topics make one chain.

:func:`kind_of` says from a file's name and a cheap look at its text which format it is, so
discovery reads nothing else; :func:`read_files` turns one file, or for Terraform and Octavia one
directory's files, into feeds and problems.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

import yaml

from konstruct import _core
from konstruct.inputs import hcl

MAX_BYTES = 2_000_000
"""Files larger than this are not configuration, and are not read."""
SUFFIXES = ('.py', '.yml', '.yaml', '.json', '.tf', '.properties')
"""The file names discovery looks at."""
MALFORMED = (RecursionError, MemoryError, ValueError, TypeError, AttributeError, KeyError, IndexError)
"""What a reader raises on a file of a shape it does not expect: nesting past Python's recursion
limit or its parser's memory, or a number where a list or mapping belongs. Parse errors (JSON's,
``ast``'s) are ValueErrors too."""
MAX_YAML_VALUES = 1_000_000
"""The most values a YAML file may stand for once its aliases are expanded: a few hundred bytes
of aliases to aliases stand for millions, which reading would expand."""
MAX_NAME = 1_024
"""The longest table, schema or database name a feed may have; warehouses allow at most this."""
_PLACEHOLDER = re.compile(r'\$\{|(?<![\w$])\$[A-Za-z_]|Ref\(')
"""A value filled in when the loader runs: ``${VAR}``, ``$VAR``, or a Terraform expression."""
LOADER_OF = {
    'dlt': 'dlt',
    'dlt_schema': 'dlt',
    'meltano': 'meltano',
    'singer_catalog': 'singer',
    'singer_state': 'singer',
    'airbyte_octavia': 'airbyte',
    'airbyte_api': 'airbyte',
    'terraform': 'terraform',
    'kafka_connect': 'kafka_connect',
    'estuary': 'estuary',
    'sling': 'sling',
    'lakeflow': 'lakeflow',
    'dms': 'dms',
}
"""The loader each format configures (a Terraform module can configure several)."""
COMPANIONS = {'meltano': ('plugins/extractors/*.lock', 'plugins/loaders/*.lock')}
"""Files read with one of a format's, found by these globs from its directory: the Meltano Hub
definitions ``meltano lock`` writes beside meltano.yml, which hold the defaults of the plugins
it uses (target-postgres's schema is the extractor's load_schema)."""
GROUPED = {'terraform': 1, 'airbyte_octavia': 3}
"""Formats read a directory at a time, as resources in one file refer to those in another: a
Terraform module's directory, and an Octavia project (``connections/<name>/configuration.yaml``
beside ``sources/`` and ``destinations/``)."""


@dataclass(frozen=True)
class Feed:
    """A loader task reading one source table and landing it."""

    loader: str
    """The loader, as :data:`konstruct.inputs.loaders.LOADERS` names it."""
    task: str
    """The pipeline, connection or connector, unique within its loader."""
    source: tuple[str, ...]
    """The source table, its system first: ``('postgres', 'app', 'public', 'orders')``."""
    target: tuple[str, ...]
    """The landed table, with as many of its database and schema as the file says."""
    columns: tuple[tuple[str, str, str], ...] = ()
    """Columns the file says it copies: the landed column, the source column, and ``direct``, or
    ``transformed`` when the loader hashes it."""
    stream: bool = False
    """Whether the target is a stream outside the warehouse (a Kafka topic, an Estuary
    collection) rather than a table in it."""
    line: int = 0
    path: str = ''
    """The file it comes from, relative to the project."""

    def to_dict(self) -> dict[str, Any]:
        return {
            'loader': self.loader,
            'task': self.task,
            'source': list(self.source),
            'target': list(self.target),
            'columns': [list(c) for c in self.columns],
            'stream': self.stream,
            'line': self.line,
            'path': self.path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Feed:
        return cls(
            str(data['loader']),
            str(data['task']),
            tuple(data['source']),
            tuple(data['target']),
            tuple((str(a), str(b), str(c)) for a, b, c in data.get('columns', [])),
            bool(data.get('stream')),
            int(data.get('line', 0)),
            str(data.get('path', '')),
        )


@dataclass
class Reading:
    """What one configuration file gave."""

    feeds: list[Feed] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    """``path:line: message`` for what could not be followed."""

    def to_dict(self) -> dict[str, Any]:
        return {'feeds': [f.to_dict() for f in self.feeds], 'problems': self.problems}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Reading:
        return cls([Feed.from_dict(f) for f in data.get('feeds', [])], list(data.get('problems', [])))


class _Context:
    """Collects one file's feeds and problems."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.reading = Reading()

    def feed(
        self,
        loader: str,
        task: str,
        source: Iterable[object],
        target: Iterable[object],
        *,
        columns: Iterable[tuple[str, str, str]] = (),
        stream: bool = False,
        line: int = 0,
    ) -> None:
        """A feed from a source table to a target, each a sequence of names, of which None and
        empty ones are left out. A name that is not a literal (a number, a list, an environment
        variable, a Terraform expression) is known only when the loader runs: the feed is left
        out with a problem, rather than invent a table by that name."""
        source_given, target_given = [p for p in source if p], [p for p in target if p]
        for part in (*source_given, *target_given):
            if isinstance(part, str) and len(part) > MAX_NAME:
                self.problem(
                    line,
                    f'{task}: a name of {len(part):,} characters is not a table name, so the feed is left out',
                )
                return
            if not isinstance(part, str) or _PLACEHOLDER.search(part):
                shown = part if isinstance(part, str) else getattr(part, 'text', type(part).__name__)
                self.problem(line, f'{task}: {shown} is not a literal name, so the feed is left out')
                return
        source_parts = tuple(str(p) for p in source_given)
        target_parts = tuple(str(p) for p in target_given)
        if len(source_parts) < 2 or not target_parts:
            return
        self.reading.feeds.append(
            Feed(loader, task, source_parts, target_parts, tuple(columns), stream, line, self.path)
        )

    def problem(self, line: int, message: str) -> None:
        self.reading.problems.append(f'{self.path}:{line}: {message}' if line else f'{self.path}: {message}')

    @contextmanager
    def reading_file(self, path: str) -> Iterator[None]:
        """Reads one file: if its reader fails on a shape it does not expect, the file gives a
        problem and none of its feeds, and the other files are still read."""
        self.path = path
        before = len(self.reading.feeds)
        try:
            yield
        except MALFORMED as error:
            del self.reading.feeds[before:]
            reason = 'it is nested too deeply' if isinstance(error, RecursionError) else error
            self.problem(0, f'cannot read: {reason or type(error).__name__}')


def _direct(columns: Iterable[str]) -> list[tuple[str, str, str]]:
    return [(c, c, 'direct') for c in columns]


def _slug(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', text.lower()).strip('_')


def _url_system(url: str) -> tuple[str | None, str | None]:
    """The database type and name a connection URL names (``postgresql://h/app`` is
    ``postgres``, ``app``), or Nones for anything else."""
    parsed = urlparse(url)
    if not parsed.scheme or '://' not in url:
        return None, None
    scheme = parsed.scheme.split('+')[0].lower()
    kind = {'postgresql': 'postgres', 'postgres': 'postgres', 'mysql': 'mysql', 'mssql': 'sqlserver'}.get(
        scheme, scheme
    )
    database = parsed.path.strip('/').split('/')[0] or None
    return kind, database


# Formats


_SLING_REQUIRED = frozenset({'source', 'target', 'streams'})
_SLING_KEYS = _SLING_REQUIRED | {'defaults', 'env', 'hooks'}
"""The top-level keys of a Sling replication
(https://docs.slingdata.io/concepts/replication/structure); its streams name an ``object``."""


def kind_of(path: str, text: str) -> str | None:
    """The configuration format of a file, from its path in the project and a look at its text,
    or None."""
    lower = PurePosixPath(path).name.lower()
    if lower.endswith('.py'):
        return (
            'dlt'
            if re.search(r'^[ \t]*(import dlt\b|from dlt\b)', text, re.MULTILINE) and 'pipeline(' in text
            else None
        )
    if lower.endswith(('.schema.yaml', '.schema.yml')):
        return 'dlt_schema' if 'engine_version' in text and 'tables:' in text else None
    if lower in ('meltano.yml', 'meltano.yaml'):
        return 'meltano'
    if lower.endswith('.tf'):
        return 'terraform' if re.search(r'resource\s+"(fivetran|airbyte)_', text) else None
    if lower.endswith('.properties'):
        return 'kafka_connect' if re.search(r'^[ \t]*connector\.class[ \t]*=', text, re.MULTILINE) else None
    if (lower in ('flow.yaml', 'flow.yml') or lower.endswith(('.flow.yaml', '.flow.yml'))) and re.search(
        r'^(captures|materializations):', text, re.MULTILINE
    ):
        return 'estuary'
    if lower.endswith(('.yaml', '.yml')) and 'ingestion_definition:' in text:
        return 'lakeflow'
    if (
        lower.endswith(('.yaml', '.yml'))
        and _SLING_REQUIRED
        <= set(re.findall(r'^([A-Za-z_][\w-]*)[ \t]*:', text, re.MULTILINE))
        <= _SLING_KEYS
        and re.search(r'^[ \t]+object[ \t]*:', text, re.MULTILINE)
    ):
        return 'sling'
    if lower.endswith(('.yaml', '.yml')) and re.search(
        r'^definition_type:\s*(connection|source|destination)\b', text, re.MULTILINE
    ):
        return 'airbyte_octavia'
    if lower.endswith('.json'):
        if '"connector.class"' in text:
            return 'kafka_connect'
        if '"rule-type"' in text and '"object-locator"' in text:
            return 'dms'
        if '"syncCatalog"' in text or all(
            f'"{key}"' in text for key in ('sourceId', 'destinationId', 'configurations')
        ):
            return 'airbyte_api'
        if '"tap_stream_id"' in text and '"streams"' in text:
            return 'singer_catalog'
        if 'state' in lower and '"bookmarks"' in text:
            try:
                data = json.loads(text)
            except (ValueError, RecursionError):
                return None
            return 'singer_state' if _singer_bookmarks(path, data) is not None else None
    return None


# dlt


_DLT_SOURCES = {'sql_database', 'sql_table', 'rest_api_source', 'rest_api_resources', 'filesystem'}


def _literal(node: ast.AST | None) -> Any:
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None


def _keyword(call: ast.Call, name: str, position: int | None = None) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    if position is not None and len(call.args) > position:
        return call.args[position]
    return None


def _called(node: ast.AST) -> str | None:
    """The last name of what a call calls: ``dlt.pipeline(...)`` is ``pipeline``."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


@dataclass
class _Resource:
    """A dlt resource: the table it lands and the system it reads."""

    table: str
    source: tuple[str, ...]
    columns: list[str] = field(default_factory=list)
    line: int = 0


def _dlt_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> ast.expr | None:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr == name:
            return decorator
        if isinstance(target, ast.Name) and target.id == name:
            return decorator
    return None


def _read_dlt(text: str, context: _Context) -> None:
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        context.problem(error.lineno or 0, f'cannot parse: {error.msg}')
        return
    resources: dict[str, list[_Resource]] = {}
    sources: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        decorator = _dlt_decorator(node, 'resource')
        if decorator is not None:
            call = decorator if isinstance(decorator, ast.Call) else None
            table = _literal(_keyword(call, 'table_name')) if call else None
            name = _literal(_keyword(call, 'name')) if call else None
            columns = _literal(_keyword(call, 'columns')) if call else None
            resource = _Resource(
                str(table or name or node.name),
                ('dlt', str(name or node.name)),
                [str(c) for c in columns] if isinstance(columns, dict | list) else [],
                node.lineno,
            )
            resources[node.name] = [resource]
            continue
        if _dlt_decorator(node, 'source') is not None:
            names: list[str] = []
            for inner in ast.walk(node):
                if isinstance(inner, ast.Return | ast.Yield | ast.YieldFrom) and inner.value is not None:
                    values = (
                        inner.value.elts if isinstance(inner.value, ast.Tuple | ast.List) else [inner.value]
                    )
                    for value in values:
                        called = value.func if isinstance(value, ast.Call) else value
                        if isinstance(called, ast.Name):
                            names.append(called.id)
            sources[node.name] = names
    for source_name, members in sources.items():
        found = [r for member in members for r in resources.get(member, [])]
        resources[source_name] = [
            _Resource(r.table, ('dlt', source_name, r.source[-1]), r.columns, r.line) for r in found
        ]

    pipelines: dict[str, tuple[str, str, int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _called(node.value) == 'pipeline':
            created = node.value
            assert isinstance(created, ast.Call)
            name = _literal(_keyword(created, 'pipeline_name', 0))
            given = _keyword(created, 'dataset_name')
            dataset = _literal(given)
            if not isinstance(name, str):
                context.problem(node.lineno, 'a dlt pipeline whose name is not a literal')
                continue
            if given is not None and not isinstance(dataset, str):
                context.problem(
                    node.lineno,
                    f'dlt pipeline {name}: its dataset_name is not a literal, so its tables are not known',
                )
                continue
            dataset = dataset if isinstance(dataset, str) else f'{name}_dataset'
            for target in node.targets:
                if isinstance(target, ast.Name):
                    pipelines[target.id] = (name, dataset, node.lineno)

    assigned: dict[str, ast.expr] = {
        t.id: n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ('run', 'extract')
        ):
            continue
        owner = node.func.value
        if not (isinstance(owner, ast.Name) and owner.id in pipelines):
            continue
        name, dataset, _ = pipelines[owner.id]
        data = _keyword(node, 'data', 0)
        if isinstance(data, ast.Name) and data.id in assigned:
            data = assigned[data.id]
        table_override = _literal(_keyword(node, 'table_name'))
        for resource in _dlt_resources(data, resources, context, node.lineno):
            table = str(table_override) if isinstance(table_override, str) else resource.table
            context.feed(
                'dlt',
                name,
                resource.source,
                (dataset, table),
                columns=_direct(resource.columns),
                line=node.lineno,
            )


def _dlt_resources(
    data: ast.expr | None, resources: dict[str, list[_Resource]], context: _Context, line: int
) -> list[_Resource]:
    """The resources a pipeline's ``run`` loads, from the expression passed to it."""
    if data is None:
        return []
    if isinstance(data, ast.List | ast.Tuple):
        return [r for item in data.elts for r in _dlt_resources(item, resources, context, line)]
    if (
        isinstance(data, ast.Call)
        and isinstance(data.func, ast.Attribute)
        and data.func.attr == 'with_resources'
    ):
        wanted = {str(v) for v in (_literal(a) for a in data.args) if isinstance(v, str)}
        return [
            r
            for r in _dlt_resources(data.func.value, resources, context, line)
            if r.source[-1] in wanted or r.table in wanted
        ]
    called = _called(data)
    if called in resources:
        return resources[called]
    if isinstance(data, ast.Name) and data.id in resources:
        return resources[data.id]
    if called in ('sql_database', 'sql_table'):
        assert isinstance(data, ast.Call)
        credentials = _literal(_keyword(data, 'credentials', 0))
        kind, database = _url_system(credentials) if isinstance(credentials, str) else (None, None)
        schema = _literal(_keyword(data, 'schema'))
        tables = (
            [_literal(_keyword(data, 'table', 1))]
            if called == 'sql_table'
            else _literal(_keyword(data, 'table_names'))
        )
        if not isinstance(tables, list) or not all(isinstance(t, str) for t in tables):
            context.problem(line, f'{called} without literal table names: its tables are not known')
            return []
        system = (kind, database or '') if kind else ('sql_database',)
        prefix = (*system, schema) if isinstance(schema, str) else system
        return [_Resource(t, tuple(p for p in (*prefix, t) if p)) for t in tables]
    if called in ('rest_api_source', 'rest_api_resources'):
        assert isinstance(data, ast.Call)
        config = _literal(data.args[0]) if data.args else None
        if not isinstance(config, dict):
            context.problem(line, f'{called} with a configuration that is not a literal')
            return []
        base = str((config.get('client') or {}).get('base_url') or '')
        host = urlparse(base).hostname or 'rest_api'
        found = []
        for item in config.get('resources') or []:
            resource_name = (
                item
                if isinstance(item, str)
                else (item or {}).get('name')
                if isinstance(item, dict)
                else None
            )
            if resource_name:
                table = (item.get('table_name') if isinstance(item, dict) else None) or resource_name
                found.append(_Resource(str(table), (host, str(resource_name))))
        return found
    if called is not None:
        context.problem(
            line, f'the data passed to run() comes from {called}(), which Konstruct does not read'
        )
    return []


def _yaml(text: str, context: _Context) -> Any:
    """A YAML file's value, ``{}`` for an empty one, or None after a problem when it cannot be
    read. The document is composed first and its values counted with each alias expanded, so a
    file whose aliases stand for more than :data:`MAX_YAML_VALUES` values is refused before
    anything expands them."""
    try:
        loader = yaml.SafeLoader(text)
    except yaml.YAMLError as error:
        context.problem(0, f'cannot read YAML: {error}')
        return None
    try:
        node = loader.get_single_node()
        if node is None:
            return {}
        if _expanded(node, {}) > MAX_YAML_VALUES:
            context.problem(
                0, f'cannot read YAML: its aliases stand for more than {MAX_YAML_VALUES:,} values'
            )
            return None
        data = loader.construct_document(node)
    except yaml.YAMLError as error:
        context.problem(0, f'cannot read YAML: {error}')
        return None
    finally:
        loader.dispose()
    return {} if data is None else data


def _expanded(node: yaml.Node, sizes: dict[int, int]) -> int:
    """How many values a YAML node stands for with its aliases expanded, each shared node counted
    once and remembered; a node that contains itself stands for too many."""
    known = sizes.get(id(node))
    if known is not None:
        return known
    sizes[id(node)] = MAX_YAML_VALUES + 1
    total = 1
    if isinstance(node, yaml.SequenceNode):
        total += sum(_expanded(item, sizes) for item in node.value)
    elif isinstance(node, yaml.MappingNode):
        total += sum(_expanded(key, sizes) + _expanded(value, sizes) for key, value in node.value)
    sizes[id(node)] = min(total, MAX_YAML_VALUES + 1)
    return sizes[id(node)]


def _strings(value: Any) -> list[str]:
    """The strings a list holds, or a single string; anything else holds none."""
    if isinstance(value, str):
        return [value]
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _read_dlt_schema(text: str, context: _Context) -> None:
    """A schema dlt exported (``export_schema_path``): its tables, the resource each comes
    from, and their columns. The dataset is not in the file, so tables are found by name."""
    data = _yaml(text, context)
    if data is None:
        return
    given = data.get('name')
    name = given if isinstance(given, str) else PurePosixPath(context.path).name.split('.')[0]
    for table, spec in (data.get('tables') or {}).items():
        if not isinstance(spec, dict) or str(table).startswith('_dlt_'):
            continue
        resource = spec.get('resource')
        if spec.get('parent') or not resource:
            continue
        context.feed('dlt', name, ('dlt', name, resource), (table,))


# Singer and Meltano


def _singer_table(stream: str) -> tuple[str | None, str]:
    """The schema and table a Meltano SDK target lands a stream in: ``public-orders`` is
    ``public`` and ``orders``, when no default target schema is set."""
    parts = stream.split('-')
    return (parts[-2] if len(parts) in (2, 3) else None), parts[-1]


def _catalog_streams(data: Any) -> Iterator[tuple[str, str | None, str, list[str], bool]]:
    """Each stream of a Singer catalog: its id, source schema, source table, selected columns
    and whether it is selected."""
    for stream in (data or {}).get('streams') or []:
        if not isinstance(stream, dict):
            continue
        stream_id = str(stream.get('tap_stream_id') or stream.get('stream') or '')
        root: dict[str, Any] = {}
        properties: dict[str, dict[str, Any]] = {}
        for entry in stream.get('metadata') or []:
            breadcrumb = entry.get('breadcrumb') or []
            meta = entry.get('metadata') or {}
            if not breadcrumb:
                root = meta
            elif len(breadcrumb) == 2 and breadcrumb[0] == 'properties':
                properties[str(breadcrumb[1])] = meta
        selected = bool(root.get('selected', stream.get('selected', False)))
        columns = [
            name
            for name in ((stream.get('schema') or {}).get('properties') or {})
            if properties.get(name, {}).get('inclusion') == 'automatic'
            or properties.get(name, {}).get(
                'selected', properties.get(name, {}).get('selected-by-default', True)
            )
        ]
        table = str(stream.get('table_name') or stream.get('stream') or stream_id)
        schema = root.get('schema-name') or root.get('database-name')
        yield stream_id, str(schema) if schema else None, table, columns, selected


def _tap(path: str) -> str:
    """The tap a catalog or state file belongs to, from a ``tap-<name>`` in its path, else the
    file's own name."""
    for part in reversed(PurePosixPath(path).parts):
        found = re.search(r'tap-[a-z0-9_-]+?(?=$|[._])', part.lower())
        if found:
            return found.group(0)
    return PurePosixPath(path).name.split('.')[0]


def _read_singer_catalog(text: str, context: _Context) -> None:
    try:
        data = json.loads(text)
    except ValueError as error:
        context.problem(0, f'cannot read JSON: {error}')
        return
    tap = _tap(context.path)
    for stream_id, schema, table, columns, selected in _catalog_streams(data):
        if not selected:
            continue
        target_schema, target_table = _singer_table(stream_id)
        context.feed(
            'singer',
            tap,
            (re.sub(r'^tap-', '', tap), schema, table),
            (target_schema, target_table),
            columns=_direct(columns),
        )


_SINGER_BOOKMARK = frozenset({'replication_key', 'replication_key_value', 'lsn'})
"""Keys of the bookmarks Singer taps write for a stream (``version`` alone is too common)."""


_TAP = re.compile(r'tap-[a-z0-9][a-z0-9_-]*')


def _singer_bookmarks(path: str, data: Any) -> dict[str, Any] | None:
    """A Singer state's bookmarks, by stream, or None when the JSON is not a tap's state. Other
    programs keep bookmarks in a state.json, so a mapping of streams to objects is not enough: the
    state must also be a runner's (``singer_state``, ``currently_syncing``), sit in a
    ``tap-<name>`` directory or be named ``tap-<name>.<...>``, or hold the bookmarks taps write."""
    if not isinstance(data, dict):
        return None
    state = data
    # Meltano keeps a tap's state under "singer_state"; older runners under "value".
    for key in ('singer_state', 'value'):
        if isinstance(state.get(key), dict):
            state = state[key]
    bookmarks = state.get('bookmarks')
    if not isinstance(bookmarks, dict) or not all(isinstance(b, dict) for b in bookmarks.values()):
        return None
    if (
        'singer_state' in data
        or 'currently_syncing' in state
        or _TAP.fullmatch(PurePosixPath(path.lower()).parent.name)
        or _TAP.match(PurePosixPath(path.lower()).name.split('.')[0])
        or any(_SINGER_BOOKMARK & b.keys() for b in bookmarks.values())
    ):
        return bookmarks
    return None


def _read_singer_state(text: str, context: _Context) -> None:
    try:
        data = json.loads(text)
    except ValueError as error:
        context.problem(0, f'cannot read JSON: {error}')
        return
    bookmarks = _singer_bookmarks(context.path, data)
    if bookmarks is None:
        context.problem(0, 'not the state of a Singer tap: no bookmarks of streams')
        return
    tap = _tap(context.path)
    for stream_id in bookmarks:
        schema, table = _singer_table(str(stream_id))
        context.feed('singer', tap, (re.sub(r'^tap-', '', tap), schema, table), (schema, table))


def _meltano_plugins(entries: Any) -> dict[str, dict[str, Any]]:
    """A meltano.yml plugin list by name, each with what it inherits filled in
    (https://docs.meltano.com/concepts/plugins/#plugin-inheritance): the settings of the plugin
    it names in ``inherit_from`` under its own, their ``config`` merged, and ``base``, the name of
    the plugin at the root of the chain, which names the source system and the namespace."""
    if not isinstance(entries, list | None):
        raise TypeError(f'a plugin list is a {type(entries).__name__}')
    given = {p['name']: p for p in entries or [] if isinstance(p, dict) and isinstance(p.get('name'), str)}
    resolved: dict[str, dict[str, Any]] = {}
    for name in given:
        chain: list[str] = []
        current: Any = name
        while current in given and current not in chain and current not in resolved:
            chain.append(current)
            current = given[current].get('inherit_from')
        if current in resolved:
            parent = resolved[current]
        elif isinstance(current, str) and current not in given:
            parent = {'base': current}  # a plugin the file inherits from without defining it
        else:
            parent = {'base': chain[-1]}  # no parent, or a cycle
        for link in reversed(chain):
            plugin = given[link]
            inherited, own = parent.get('config'), plugin.get('config')
            config = {
                **(inherited if isinstance(inherited, dict) else {}),
                **(own if isinstance(own, dict) else {}),
            }
            parent = resolved[link] = {**parent, **plugin, 'config': config, 'base': parent['base']}
    return resolved


def _selections(patterns: list[str]) -> dict[str, list[str] | None]:
    """Streams a Meltano ``select`` names without wildcards, each with its columns (None for
    every column), with excluded columns left out."""
    streams: dict[str, list[str] | None] = {}
    excluded: set[tuple[str, str]] = set()
    for pattern in patterns:
        text = str(pattern)
        negate = text.startswith('!')
        text = text.lstrip('!')
        stream, _, column = text.partition('.')
        if any(c in stream for c in '*?['):
            continue
        if negate:
            excluded.add((stream, column))
            continue
        if column in ('*', ''):
            streams[stream] = None
        elif not any(c in column for c in '*?['):
            current = streams.setdefault(stream, [])
            if current is not None:
                current.append(column)
    for stream, column in excluded:
        if streams.get(stream):
            streams[stream] = [c for c in streams[stream] or [] if c != column]
    return streams


_MELTANO_NAMESPACE = re.compile(r'\$(?:\{MELTANO_EXTRACTOR_NAMESPACE\}|MELTANO_EXTRACTOR_NAMESPACE(?!\w))')
_MELTANO_LOAD_SCHEMA = re.compile(
    r'\$(?:\{MELTANO_EXTRACT__LOAD_SCHEMA\}|MELTANO_EXTRACT__LOAD_SCHEMA(?!\w))'
)


def _read_meltano_project(files: list[tuple[str, str]], context: _Context) -> None:
    """meltano.yml, with the lock files of its plugins (:data:`COMPANIONS`)."""
    hub: dict[tuple[str, ...], dict[str, Any]] = {}
    for path, text in files:
        if not path.endswith('.lock'):
            continue
        with context.reading_file(path):
            data = json.loads(text)
            if not (isinstance(data, dict) and isinstance(data.get('name'), str)):
                continue
            defaults = {
                'namespace': data.get('namespace'),
                'settings': {
                    s['name']: s['value']
                    for s in data.get('settings') or []
                    if isinstance(s, dict) and isinstance(s.get('name'), str) and 'value' in s
                },
            }
            kind = PurePosixPath(path).parent.name
            hub[(kind, data['name'])] = hub[(kind, data['name'], str(data.get('variant')))] = defaults
    for path, text in files:
        if not path.endswith('.lock'):
            with context.reading_file(path):
                _read_meltano(text, context, hub)


def _read_meltano(text: str, context: _Context, hub: dict[tuple[str, ...], dict[str, Any]]) -> None:
    """meltano.yml: each extractor and loader a job or schedule pairs, the streams the extractor
    selects, and the schema the loader lands them in, from the file or its lock file; without
    one, the table is found by name."""
    data = _yaml(text, context)
    if data is None:
        return
    plugins = data.get('plugins') or {}
    extractors = _meltano_plugins(plugins.get('extractors'))
    loaders = _meltano_plugins(plugins.get('loaders'))
    pairs: dict[tuple[str, str], None] = {}
    for job in data.get('jobs') or []:
        tasks = job.get('tasks') if isinstance(job, dict) else None
        for task in tasks if isinstance(tasks, list) else _strings(tasks):
            # A task runs blocks of extractors (and mappers) each ending in a loader.
            pending: dict[str, None] = {}
            for word in task.split() if isinstance(task, str) else _strings(task):
                if word in extractors:
                    pending[word] = None
                elif word in loaders:
                    pairs.update(dict.fromkeys((tap, word) for tap in pending))
                    pending = {}
    for schedule in data.get('schedules') or []:
        if isinstance(schedule, dict):
            extractor, loader = schedule.get('extractor'), schedule.get('loader')
            if isinstance(extractor, str) and isinstance(loader, str):
                pairs[(extractor, loader)] = None
    if not pairs and len(loaders) == 1:
        pairs = dict.fromkeys((tap, next(iter(loaders))) for tap in extractors)

    def locked(kind: str, plugin: dict[str, Any]) -> dict[str, Any]:
        """A plugin with the defaults of its lock file under its own settings."""
        base = str(plugin['base'])
        defaults = hub.get((kind, base, str(plugin.get('variant')))) or hub.get((kind, base)) or {}
        return {
            **plugin,
            'namespace': plugin.get('namespace') or defaults.get('namespace'),
            'config': {**defaults.get('settings', {}), **plugin['config']},
        }

    for tap_name, target_name in pairs:
        tap = locked('extractors', extractors.get(tap_name) or {'base': tap_name, 'config': {}})
        target = locked('loaders', loaders.get(target_name) or {'base': target_name, 'config': {}})
        _meltano_pair(context, tap_name, tap, target_name, target)


def _meltano_pair(
    context: _Context, tap_name: str, tap: dict[str, Any], target_name: str, target: dict[str, Any]
) -> None:
    """Feeds for the streams one extractor selects, landed by one loader."""
    config, target_config = tap['config'], target['config']
    schema = target_config.get('default_target_schema') or target_config.get('schema')
    if isinstance(schema, str):
        # https://docs.meltano.com/concepts/plugins/: a loader reads the extractor's
        # load_schema, which defaults to its namespace (tap_gitlab for tap-gitlab).
        namespace = tap.get('namespace')
        namespace = namespace if isinstance(namespace, str) else str(tap['base']).replace('-', '_')
        load_schema = tap.get('load_schema')
        load_schema = _MELTANO_NAMESPACE.sub(
            namespace, load_schema if isinstance(load_schema, str) else '$MELTANO_EXTRACTOR_NAMESPACE'
        )
        schema = _MELTANO_LOAD_SCHEMA.sub(load_schema, _MELTANO_NAMESPACE.sub(namespace, schema))
    database = target_config.get('database') or target_config.get('dbname')
    system = re.sub(r'^tap-', '', str(tap['base']))
    source_database = config.get('database') or config.get('dbname')
    streams = _selections(_strings(tap.get('select')))
    if not streams:
        context.problem(0, f'{tap_name} selects no stream by name, so the tables it lands are not known')
    for stream, columns in streams.items():
        stream_schema, table = _singer_table(stream)
        context.feed(
            'meltano',
            f'{tap_name} {target_name}',
            (system, source_database, stream_schema, table),
            (database, schema, table),
            columns=_direct(columns or []),
        )


# Airbyte


def _airbyte_system(image_or_type: str) -> str:
    """The source system an Airbyte connector reads: ``airbyte/source-postgres`` is ``postgres``."""
    name = image_or_type.rsplit('/', 1)[-1].split(':')[0]
    return _slug(re.sub(r'^(airbyte_)?source[-_]', '', name)) or 'airbyte'


def _airbyte_namespace(definition: str, format_: str, stream_namespace: Any, default: Any) -> Any:
    """The schema an Airbyte connection lands a stream in, as Airbyte documents it
    (https://docs.airbyte.com/platform/using-airbyte/core-concepts/namespaces): the stream's
    namespace, else the destination's default; a custom format, else the default when it is
    blank."""
    if definition == 'source':
        return stream_namespace or default
    if definition == 'customformat':
        namespace = stream_namespace if isinstance(stream_namespace, str) else ''
        text = format_.replace('${SOURCE_NAMESPACE}', namespace)
        return text if text.strip() else default
    return default


def _airbyte_connection(
    context: _Context,
    name: str,
    system: tuple[Any, ...],
    default_schema: Any,
    settings: dict[str, Any],
    streams: Iterable[dict[str, Any]],
    source_schemas: Any = None,
    line: int = 0,
) -> None:
    """Feeds for one connection's streams, each ``{'name', 'namespace', 'fields', 'mapped'}``.
    ``settings`` holds its ``definition``, ``format`` and ``prefix``. A stream without a
    namespace reads the source's schema when its configuration lists only one (``schemas``)."""
    for setting, value in settings.items():
        literal = value is None or (
            isinstance(value, str) and not _PLACEHOLDER.search(value.replace('${SOURCE_NAMESPACE}', ''))
        )
        if not literal:
            shown = value if isinstance(value, str) else getattr(value, 'text', type(value).__name__)
            context.problem(
                line,
                f'connection {name}: its namespace {setting} {shown} is not a literal, so its tables are not known',
            )
            return
    definition = str(settings.get('definition') or 'destination').lower().replace('_', '')
    prefix = settings.get('prefix') or ''
    only = (
        source_schemas[0]
        if isinstance(source_schemas, list)
        and len(source_schemas) == 1
        and isinstance(source_schemas[0], str)
        else None
    )
    for stream in streams:
        namespace = stream.get('namespace') or only
        schema = _airbyte_namespace(definition, settings.get('format') or '', namespace, default_schema)
        # A mapper renames, hashes, encrypts or filters fields: its columns are not followed.
        columns = [] if stream.get('mapped') else _direct(stream.get('fields') or [])
        context.feed(
            'airbyte',
            name,
            (*system, namespace, stream['name']),
            (schema, f'{prefix}{stream["name"]}'),
            columns=columns,
            line=line,
        )


def _name(value: Any, fallback: str) -> str:
    """A name given as a string, else the fallback: never the text of a list or mapping."""
    return value if isinstance(value, str) and value else fallback


def _fields(selected: Any, properties: Any) -> list[str]:
    """The top-level fields a stream syncs: those selected, else every property of its schema."""
    if isinstance(selected, list) and selected:
        paths = [s.get('field_path') or s.get('fieldPath') for s in selected if isinstance(s, dict)]
        return list(dict.fromkeys(p[0] for p in paths if isinstance(p, list) and p and isinstance(p[0], str)))
    return [k for k in properties if isinstance(k, str)] if isinstance(properties, dict) else []


def _read_airbyte_api(text: str, context: _Context) -> None:
    """A connection as Airbyte's API returns it: the public API (``configurations.streams``) or
    the configuration API (``syncCatalog``), alone, in a list, or under ``data``."""
    try:
        data = json.loads(text)
    except ValueError as error:
        context.problem(0, f'cannot read JSON: {error}')
        return
    connections = data.get('data', [data]) if isinstance(data, dict) else data
    for connection in connections if isinstance(connections, list) else []:
        if not isinstance(connection, dict):
            continue
        name = _name(
            connection.get('name'), _name(connection.get('connectionId'), PurePosixPath(context.path).stem)
        )
        source = connection.get('source') or {}
        source_config = source.get('connectionConfiguration') or source.get('configuration') or {}
        kind = _name(source.get('sourceName'), _name(source.get('sourceType'), ''))
        system = (_airbyte_system(kind), source_config.get('database')) if kind else (_slug(name),)
        destination = (connection.get('destination') or {}).get('connectionConfiguration') or {}
        default = destination.get('schema') or destination.get('dataset_id')
        streams = []
        for item in (connection.get('syncCatalog') or {}).get('streams') or []:
            stream, config = item.get('stream') or {}, item.get('config') or {}
            if not config.get('selected', True) or not isinstance(stream.get('name'), str):
                continue
            fields = _fields(
                config.get('selectedFields') if config.get('fieldSelectionEnabled') else None,
                (stream.get('jsonSchema') or {}).get('properties'),
            )
            streams.append(
                {
                    'name': stream['name'],
                    'namespace': stream.get('namespace'),
                    'fields': fields,
                    'mapped': bool(config.get('mappers')),
                }
            )
        for item in (connection.get('configurations') or {}).get('streams') or []:
            if isinstance(item.get('name'), str):
                streams.append(
                    {
                        'name': item['name'],
                        'namespace': item.get('namespace'),
                        'fields': _fields(item.get('selectedFields'), None),
                        'mapped': bool(item.get('mappers')),
                    }
                )
        _airbyte_connection(
            context,
            name,
            system,
            default,
            {
                'definition': connection.get('namespaceDefinition'),
                'format': connection.get('namespaceFormat'),
                'prefix': connection.get('prefix'),
            },
            streams,
            source_config.get('schemas'),
        )


def _read_octavia(files: list[tuple[str, str]], context: _Context) -> None:
    """An Octavia project: each connection's ``configuration.yaml`` with the source and
    destination configurations it points to."""
    documents: dict[str, dict[str, Any]] = {}
    for path, text in files:
        with context.reading_file(path):
            data = _yaml(text, context)
            if isinstance(data, dict):
                documents[path] = data

    def referenced(reference: Any) -> dict[str, Any]:
        wanted = PurePosixPath(_name(reference, '')).parts
        for path, data in documents.items():
            if wanted and PurePosixPath(path).parts[-len(wanted) :] == wanted:
                return data
        return {}

    for path, data in sorted(documents.items()):
        if data.get('definition_type') != 'connection':
            continue
        with context.reading_file(path):
            source = referenced(data.get('source_configuration_path'))
            destination = referenced(data.get('destination_configuration_path'))
            configuration = data.get('configuration') or {}
            source_config = source.get('configuration') or {}
            system = (
                _airbyte_system(_name(source.get('definition_image'), 'airbyte')),
                source_config.get('database'),
            )
            default = (destination.get('configuration') or {}).get('schema') or (
                destination.get('configuration') or {}
            ).get('dataset_id')
            streams = []
            for item in (configuration.get('sync_catalog') or {}).get('streams') or []:
                stream, config = item.get('stream') or {}, item.get('config') or {}
                if not config.get('selected', True) or not isinstance(stream.get('name'), str):
                    continue
                fields = _fields(
                    config.get('selected_fields') if config.get('field_selection_enabled') else None,
                    (stream.get('json_schema') or {}).get('properties'),
                )
                streams.append(
                    {
                        'name': stream['name'],
                        'namespace': stream.get('namespace'),
                        'fields': fields,
                        'mapped': bool(config.get('mappers')),
                    }
                )
            _airbyte_connection(
                context,
                _name(data.get('resource_name'), PurePosixPath(path).parent.name),
                system,
                default,
                {
                    'definition': configuration.get('namespace_definition'),
                    'format': configuration.get('namespace_format'),
                    'prefix': configuration.get('prefix'),
                },
                streams,
                source_config.get('schemas'),
            )


# Terraform: Airbyte and Fivetran providers


def _resource_ref(value: Any, prefix: str) -> tuple[str, str] | None:
    """The resource a reference names: ``airbyte_source_postgres.app.source_id`` is
    ``('airbyte_source_postgres', 'app')``."""
    text = value.text if isinstance(value, hcl.Ref) else ''
    found = re.match(rf'({prefix}[a-z0-9_]*)\.([A-Za-z0-9_-]+)', text)
    return (found.group(1), found.group(2)) if found else None


def _attr(block: hcl.Block, name: str) -> Any:
    """An argument written as an attribute (``destination_schema = {...}``) or a block."""
    if name in block.arguments:
        return block.arguments[name]
    inner = block.block(name)
    return dict(inner.arguments) if inner is not None else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and '${' not in value else None


def _read_terraform(files: list[tuple[str, str]], context: _Context) -> None:
    resources: dict[tuple[str, str], tuple[str, hcl.Block]] = {}
    for path, text in files:
        with context.reading_file(path):
            try:
                blocks = hcl.parse(text)
            except hcl.HclError as error:
                context.problem(error.line, f'cannot read Terraform: {error}')
                continue
            for block in blocks:
                if block.type == 'resource' and len(block.labels) == 2:
                    resources[(block.labels[0], block.labels[1])] = (path, block)
    for (kind, label), (path, block) in sorted(resources.items()):
        with context.reading_file(path):
            if kind == 'airbyte_connection':
                _terraform_airbyte(context, label, block, resources)
            elif kind == 'fivetran_connector_schema_config':
                _terraform_fivetran(context, block, resources)


def _terraform_airbyte(
    context: _Context, label: str, block: hcl.Block, resources: dict[tuple[str, str], tuple[str, hcl.Block]]
) -> None:
    source_ref = _resource_ref(block.arguments.get('source_id'), 'airbyte_source')
    destination_ref = _resource_ref(block.arguments.get('destination_id'), 'airbyte_destination')
    source = resources[source_ref][1] if source_ref and source_ref in resources else None
    destination = resources[destination_ref][1] if destination_ref and destination_ref in resources else None
    name = _text(block.arguments.get('name')) or label
    source_config = _attr(source, 'configuration') if source else None
    source_config = source_config if isinstance(source_config, dict) else {}
    # The generic airbyte_source resource names its connector by id only.
    if source_ref and source_ref[0] != 'airbyte_source':
        system: tuple[Any, ...] = (_airbyte_system(source_ref[0]), source_config.get('database'))
    else:
        system = (_slug(name),)
    destination_config = _attr(destination, 'configuration') if destination else None
    destination_config = destination_config if isinstance(destination_config, dict) else {}
    default = destination_config.get('schema') or destination_config.get('dataset_id')
    streams = []
    configurations = _attr(block, 'configurations') or {}
    for item in configurations.get('streams') or [] if isinstance(configurations, dict) else []:
        if isinstance(item, dict) and _text(item.get('name')):
            streams.append(
                {
                    'name': item['name'],
                    'namespace': item.get('namespace'),
                    'fields': _fields(item.get('selected_fields'), None),
                    'mapped': bool(item.get('mappers')),
                }
            )
    _airbyte_connection(
        context,
        name,
        system,
        default,
        {
            'definition': block.arguments.get('namespace_definition'),
            'format': block.arguments.get('namespace_format'),
            'prefix': block.arguments.get('prefix'),
        },
        streams,
        source_config.get('schemas'),
        block.line,
    )


def _enabled(value: Any) -> bool:
    return not (isinstance(value, dict) and value.get('enabled') in (False, 'false'))


def _terraform_fivetran(
    context: _Context, block: hcl.Block, resources: dict[tuple[str, str], tuple[str, hcl.Block]]
) -> None:
    """A connection's schema configuration, with the connection it configures: each enabled
    table, and the columns it names (hashed ones transformed)."""
    reference = _resource_ref(
        block.arguments.get('connector_id') or block.arguments.get('connection_id'), 'fivetran_conne'
    )
    connector = resources[reference][1] if reference and reference in resources else None
    if connector is None:
        context.problem(block.line, 'a schema configuration whose connection is not in this module')
        return
    # Values Terraform evaluates are passed on as they are, for the feed to report.
    service = connector.arguments.get('service') or 'fivetran'
    destination = _attr(connector, 'destination_schema') or {}
    config = _attr(connector, 'config') or {}
    database = config.get('database') if isinstance(config, dict) else None
    prefix = destination.get('prefix') if isinstance(destination, dict) else None
    single = destination.get('name') if isinstance(destination, dict) else None
    task = next((v for v in (single, prefix) if isinstance(v, str)), reference[1])  # type: ignore[index]
    schemas: dict[str, Any] = {}
    if isinstance(block.arguments.get('schemas'), dict):
        schemas = block.arguments['schemas']
    for old in block.blocks:
        # The deprecated form: schema { name, table { name, column { name } } } blocks.
        if old.type == 'schema' and _text(old.arguments.get('name')):
            tables = {
                str(t.arguments.get('name')): {
                    **t.arguments,
                    'columns': {
                        str(c.arguments.get('name')): c.arguments for c in t.blocks if c.type == 'column'
                    },
                }
                for t in old.blocks
                if t.type == 'table'
            }
            schemas[str(old.arguments['name'])] = {**old.arguments, 'tables': tables}
    for schema, spec in schemas.items():
        if not _enabled(spec) or not isinstance(spec, dict):
            continue
        # A prefix Terraform evaluates is passed on as it is, for the feed to report.
        landed_schema = (
            (f'{prefix}_{schema}' if isinstance(prefix, str) else prefix) if prefix else (single or schema)
        )
        for table, table_spec in (spec.get('tables') or {}).items():
            if not _enabled(table_spec):
                continue
            columns = (table_spec or {}).get('columns') if isinstance(table_spec, dict) else None
            copied = [
                (
                    str(c),
                    str(c),
                    'transformed' if isinstance(s, dict) and s.get('hashed') in (True, 'true') else 'direct',
                )
                for c, s in (columns or {}).items()
                if _enabled(s)
            ]
            source = (service, database, schema, table) if prefix else (service, table)
            context.feed('fivetran', task, source, (landed_schema, table), columns=copied, line=block.line)


# Kafka Connect: Debezium source connectors and sink connectors


_DEBEZIUM: dict[str, tuple[str, int]] = {
    # The source system, and how many parts of a table's name its topic carries after the prefix
    # (a schema and a table, or a database and a table). SQL Server topics carry the database too.
    # https://debezium.io/documentation/reference/stable/connectors/postgresql.html#postgresql-topic-names
    'io.debezium.connector.postgresql.PostgresConnector': ('postgres', 2),
    # https://debezium.io/documentation/reference/stable/connectors/mysql.html#mysql-topic-names
    'io.debezium.connector.mysql.MySqlConnector': ('mysql', 2),
    'io.debezium.connector.mariadb.MariaDbConnector': ('mariadb', 2),
    # https://debezium.io/documentation/reference/stable/connectors/sqlserver.html#sqlserver-topic-names
    'io.debezium.connector.sqlserver.SqlServerConnector': ('sqlserver', 3),
    'io.debezium.connector.oracle.OracleConnector': ('oracle', 2),
    'io.debezium.connector.db2.Db2Connector': ('db2', 2),
    'io.debezium.connector.mongodb.MongoDbConnector': ('mongodb', 2),
}
_PLAIN = re.compile(r'^(?:[A-Za-z0-9_$-]|\\\.)+(?:\.(?:[A-Za-z0-9_$-]|\\\.)+)*$')


def _names(value: Any) -> list[str] | None:
    """The names a comma-separated include list spells out, or None when one is a pattern."""
    items = [i.strip() for i in str(value or '').split(',') if i.strip()]
    if not all(_PLAIN.match(i) for i in items):
        return None
    return [i.replace('\\.', '.') for i in items]


MAX_TOPIC = 249
"""The longest topic name Kafka accepts."""
MAX_TRANSFORMS = 32
"""The most transformations a connector may list for Konstruct to follow its topics."""
MAX_EXPRESSIONS = 64
"""The most distinct routing expressions Konstruct compiles for one file."""

_Compiled = dict[tuple[str, str], '_core.Router | str']
"""A file's routing transformations by (expression, replacement): each compiled, or why not."""


def _routers(
    context: _Context, connector: str, config: dict[str, Any], compiled: _Compiled
) -> list[tuple[str, _core.Router]] | None:
    """The routing transformations a connector applies (RegexRouter and Debezium's
    ByLogicalTableRouter), or None after a problem when one cannot be run. They run in
    :class:`konstruct._core.Router`, in time linear in the topic, since anyone can write the
    expressions; one using a backreference or lookaround, which it cannot run, is refused rather
    than run on a matcher that backtracks. Each expression is compiled once for the file, and at
    most :data:`MAX_EXPRESSIONS` of them, as compiling one takes time of its own."""
    names = _names(config.get('transforms')) or []
    if len(names) > MAX_TRANSFORMS:
        context.problem(
            0,
            f'{connector}: it lists more than {MAX_TRANSFORMS} transformations, so its topics are not known',
        )
        return None
    routers = []
    for name in names:
        kind = str(config.get(f'transforms.{name}.type', ''))
        if kind.endswith(('RegexRouter', 'RegexRouter$Value')):
            pattern = config.get(f'transforms.{name}.regex')
            replacement = config.get(f'transforms.{name}.replacement')
        elif kind.endswith('ByLogicalTableRouter'):
            pattern = config.get(f'transforms.{name}.topic.regex')
            replacement = config.get(f'transforms.{name}.topic.replacement')
        else:
            continue
        if not (isinstance(pattern, str) and isinstance(replacement, str)):
            continue
        key = (pattern, replacement)
        if key not in compiled:
            if len(compiled) >= MAX_EXPRESSIONS:
                context.problem(
                    0,
                    f'{connector}: the file holds more than {MAX_EXPRESSIONS} routing expressions, '
                    'so its topics are not known',
                )
                return None
            try:
                if len(replacement) > MAX_TOPIC:
                    raise ValueError('its replacement is longer than a topic can be')
                compiled[key] = _core.Router(pattern, replacement)
            except ValueError as error:
                compiled[key] = str(error)
        router = compiled[key]
        if isinstance(router, str):
            context.problem(
                0, f'{connector}: transformation {name} is not run, as {router}, so its topics are not known'
            )
            return None
        routers.append((name, router))
    return routers


def _routed(
    context: _Context, connector: str, routers: list[tuple[str, _core.Router]], topic: str
) -> str | None:
    """A topic after a connector's routing transformations, or None after a problem when it, or
    the topic a transformation gives, is longer than Kafka allows."""
    if len(topic) > MAX_TOPIC:
        context.problem(
            0,
            f'{connector}: a topic of {len(topic):,} characters is longer than Kafka allows, so its topics are not known',
        )
        return None
    for name, router in routers:
        routed = router.route(topic)
        if routed is None:
            context.problem(
                0, f'{connector}: transformation {name} splits a character, so its topics are not known'
            )
            return None
        topic = routed
        if len(topic) > MAX_TOPIC:
            context.problem(
                0,
                f'{connector}: transformation {name} gives a topic longer than Kafka allows, '
                'so its topics are not known',
            )
            return None
    return topic


def _jdbc_database(url: Any) -> str | None:
    """The database a JDBC URL names: ``jdbc:postgresql://host:5432/app`` is ``app``. A URL
    filled in when the connector starts (``${file:...}``, ``${env:...}``) is passed on as it is,
    for the feed to report."""
    if isinstance(url, str) and _PLACEHOLDER.search(url):
        return url
    text = str(url or '')
    found = re.match(r'jdbc:[a-z0-9]+://[^/;?]+/([^/;?]+)', text) or re.search(
        r';database(?:Name)?=([^;]+)', text
    )
    return found.group(1) if found else None


def _read_kafka_connect(text: str, context: _Context) -> None:
    """A connector's configuration, as the Kafka Connect REST API takes it (JSON, alone or with
    ``name`` and ``config``, or a list of them) or as a standalone worker reads it
    (``.properties``)."""
    connectors: list[Any]
    if context.path.endswith('.properties'):
        properties: dict[str, str] = {}
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith(('#', '!')) and '=' in stripped:
                key, _, value = stripped.partition('=')
                properties[key.strip()] = value.strip()
        connectors = [
            {'name': properties.get('name') or PurePosixPath(context.path).stem, 'config': properties}
        ]
    else:
        try:
            data = json.loads(text)
        except ValueError as error:
            context.problem(0, f'cannot read JSON: {error}')
            return
        connectors = data if isinstance(data, list) else [data]
    compiled: _Compiled = {}
    for connector in connectors:
        if not isinstance(connector, dict):
            continue
        given = connector.get('config')
        config = {str(k): v for k, v in (given if isinstance(given, dict) else connector).items()}
        name = str(connector.get('name') or config.get('name') or PurePosixPath(context.path).stem)
        kind = str(config.get('connector.class', ''))
        if kind in _DEBEZIUM:
            _debezium(context, name, kind, config, compiled)
        elif kind:
            _sink(context, name, kind, config, compiled)


def _debezium(context: _Context, name: str, kind: str, config: dict[str, Any], compiled: _Compiled) -> None:
    """A Debezium connector: each table its include list names, captured into its topic."""
    system, parts = _DEBEZIUM[kind]
    prefix = config.get('topic.prefix') or config.get('database.server.name')
    database = config.get('database.dbname') or config.get('database.names')
    listed = config.get('collection.include.list') if system == 'mongodb' else None
    listed = listed or config.get('table.include.list') or config.get('table.whitelist')
    tables = _names(listed)
    if not prefix or tables is None or not tables:
        reason = 'no topic prefix' if not prefix else 'its tables are not listed by name'
        context.problem(0, f'Debezium connector {name}: {reason}, so its topics are not known')
        return
    columns = _names(config.get('column.include.list')) or []
    routers = _routers(context, f'Debezium connector {name}', config, compiled)
    if routers is None:
        return
    for table in tables:
        names = table.split('.')
        if len(names) != 2:
            context.problem(0, f'Debezium connector {name}: {table} is not <schema>.<table>')
            continue
        # A SQL Server topic carries the database before the schema.
        middle = [str(database).split(',')[0], *names] if parts == 3 and database else names
        topic = _routed(context, f'Debezium connector {name}', routers, '.'.join([str(prefix), *middle]))
        if topic is None:
            return
        source = (
            (system, database, *names)
            if parts == 3 or system in ('postgres', 'oracle', 'db2')
            else (system, *names)
        )
        copied = [c.rsplit('.', 1)[1] for c in columns if c.rsplit('.', 1)[0] == table]
        context.feed('debezium', name, source, ('kafka', topic), columns=_direct(copied), stream=True)


def _sink(context: _Context, name: str, kind: str, config: dict[str, Any], compiled: _Compiled) -> None:
    """A sink connector writing topics into warehouse tables."""
    topics = _names(config.get('topics'))
    if not topics:
        if config.get('topics.regex'):
            context.problem(0, f'sink {name} reads topics by a pattern, so its tables are not known')
        return
    routers = _routers(context, f'sink {name}', config, compiled)
    if routers is None:
        return
    for topic in topics:
        routed = _routed(context, f'sink {name}', routers, topic)
        if routed is None:
            return
        table = _sink_table(context, name, kind, config, topic, routed)
        if table:
            context.feed('kafka_connect', name, ('kafka', topic), table)


def _sink_table(
    context: _Context, name: str, kind: str, config: dict[str, Any], read_from: str, topic: str
) -> tuple[str | None, ...] | None:
    """The table a sink writes a topic into, by the rules of its connector class. ``read_from``
    is the topic as the sink reads it, ``topic`` the same after the sink's own routing."""
    if kind == 'io.debezium.connector.jdbc.JdbcSinkConnector':
        # https://debezium.io/documentation/reference/stable/connectors/jdbc.html: the default
        # naming strategy turns the topic's dots into underscores and fills ${source.<field>}
        # from the change event's source block; the passthrough strategy does neither.
        form = str(config.get('collection.name.format') or config.get('table.name.format') or '${topic}')
        if str(config.get('collection.naming.strategy', '')).endswith('PassthroughCollectionNamingStrategy'):
            form = form.replace('${topic}', topic)
        else:
            form = form.replace('${topic}', topic.replace('.', '_'))
            names = read_from.split('.')
            # A Debezium source connector's topic ends with the schema (or database) and table.
            if len(names) >= 3:
                form = form.replace('${source.schema}', names[-2]).replace('${source.table}', names[-1])
        unfilled = re.search(r'\$\{[^}]*\}?', form)
        if unfilled:
            context.problem(
                0,
                f'sink {name}: {unfilled.group(0)} in collection.name.format is known only from '
                f'the events of topic {read_from}, so its table is not known',
            )
            return None
        return (_jdbc_database(config.get('connection.url')), *form.split('.'))
    if kind == 'io.confluent.connect.jdbc.JdbcSinkConnector':
        # https://docs.confluent.io/kafka-connectors/jdbc/current/sink-connector/sink_config_options.html
        form = str(config.get('table.name.format') or '${topic}')
        return (_jdbc_database(config.get('connection.url')), *form.replace('${topic}', topic).split('.'))
    if kind == 'com.snowflake.kafka.connector.SnowflakeSinkConnector':
        # https://docs.snowflake.com/en/user-guide/kafka-connector/how-the-connector-works
        mapped = dict(
            pair.split(':', 1)
            for pair in str(config.get('snowflake.topic2table.map') or '').split(',')
            if ':' in pair
        )
        table = mapped.get(topic) or (
            topic.upper() if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', topic) else None
        )
        if table is None:
            context.problem(
                0,
                f'sink {name}: topic {topic} is not a table name and has no snowflake.topic2table.map entry',
            )
            return None
        return (config.get('snowflake.database.name'), config.get('snowflake.schema.name'), table.strip())
    if kind == 'com.wepay.kafka.connect.bigquery.BigQuerySinkConnector':
        # https://github.com/confluentinc/kafka-connect-bigquery: topic2TableMap, else the topic,
        # with characters BigQuery refuses replaced when sanitizeTopics is on.
        mapped = dict(
            pair.split(':', 1) for pair in str(config.get('topic2TableMap') or '').split(',') if ':' in pair
        )
        table = mapped.get(topic) or (
            re.sub(r'[^A-Za-z0-9_]', '_', topic)
            if str(config.get('sanitizeTopics')).lower() == 'true'
            else topic
        )
        return (config.get('project'), config.get('defaultDataset'), table)
    if kind in (
        'io.tabular.iceberg.connect.IcebergSinkConnector',
        'org.apache.iceberg.connect.IcebergSinkConnector',
    ):
        # https://iceberg.apache.org/docs/latest/kafka-connect/: static routing writes every topic
        # into each table iceberg.tables lists.
        tables = _names(config.get('iceberg.tables'))
        if str(config.get('iceberg.tables.dynamic-enabled')).lower() == 'true' or not tables:
            context.problem(
                0, f'sink {name} routes records to tables at run time, so its tables are not known'
            )
            return None
        for table in tables[1:]:
            context.feed('kafka_connect', name, ('kafka', topic), tuple(table.split('.')))
        return tuple(tables[0].split('.'))
    return None


# Estuary Flow


def _estuary_system(image: Any) -> str:
    """The system an Estuary connector reads or writes: ``ghcr.io/estuary/source-postgres:v1`` is
    ``postgres``."""
    name = (image if isinstance(image, str) else '').rsplit('/', 1)[-1].split(':')[0]
    return _slug(re.sub(r'^(source|materialize)-', '', name)) or 'estuary'


def _read_estuary(text: str, context: _Context) -> None:
    """A Flow catalog (``flow.yaml``): each capture's bindings into collections, and each
    materialization's bindings from collections into tables."""
    data = _yaml(text, context)
    if data is None:
        return
    for name, spec in (data.get('captures') or {}).items():
        connector = ((spec or {}).get('endpoint') or {}).get('connector') or {}
        given = connector.get('config')
        config: dict[str, Any] = given if isinstance(given, dict) else {}
        system = _estuary_system(connector.get('image'))
        for binding in (spec or {}).get('bindings') or []:
            resource = binding.get('resource') or {}
            stream = resource.get('stream') or resource.get('name') or resource.get('table')
            if stream and binding.get('target') and not binding.get('disable'):
                context.feed(
                    'estuary',
                    str(name),
                    (
                        system,
                        config.get('database'),
                        resource.get('namespace') or resource.get('schema'),
                        stream,
                    ),
                    ('estuary', binding['target']),
                    stream=True,
                )
    for name, spec in (data.get('materializations') or {}).items():
        connector = ((spec or {}).get('endpoint') or {}).get('connector') or {}
        given = connector.get('config')
        settings: dict[str, Any] = given if isinstance(given, dict) else {}
        for binding in (spec or {}).get('bindings') or []:
            resource = binding.get('resource') or {}
            source = binding.get('source')
            source = source.get('name') if isinstance(source, dict) else source
            table = resource.get('table')
            if not (source and table) or binding.get('disable'):
                continue
            schema = (
                resource.get('schema')
                or resource.get('dataset')
                or settings.get('schema')
                or settings.get('dataset')
            )
            context.feed(
                'estuary',
                str(name),
                ('estuary', source),
                (settings.get('database') or settings.get('project_id'), schema, table),
            )


# Sling


def _fill(template: str, values: dict[str, str]) -> str:
    """A Sling object name with its runtime variables (``{stream_table}``) filled in."""
    return re.sub(r'\{(\w+)\}', lambda m: values[m.group(1)], template)


def _read_sling(text: str, context: _Context) -> None:
    """A Sling replication (https://docs.slingdata.io/concepts/replication): each stream from
    the source connection into the object its runtime variables name."""
    data = _yaml(text, context)
    if data is None:
        return
    source = _slug(_name(data.get('source'), 'sling'))
    task = f'{_name(data.get("source"), "source")} to {_name(data.get("target"), "target")}'
    defaults = data.get('defaults') or {}
    for key, spec in (data.get('streams') or {}).items():
        spec = {**defaults, **(spec or {})}
        stream = str(key)
        if spec.get('disabled'):
            continue
        if '*' in stream:
            context.problem(0, f'stream {stream} is a wildcard, so the tables it lands are not known')
            continue
        if spec.get('sql'):
            context.problem(0, f'stream {stream} runs its own SQL, which Konstruct does not follow')
            continue
        template = spec.get('object')
        if not isinstance(template, str):
            context.problem(0, f'stream {stream} has no object')
            continue
        is_file = '://' in stream
        schema, _, table = (
            ('', '', PurePosixPath(stream.split('://', 1)[1]).stem) if is_file else stream.rpartition('.')
        )
        values = {
            'stream_schema': schema,
            'stream_table': table,
            'stream_name': table,
            'stream_full_name': stream,
            'stream_file_name': table,
            # The target connection's own schema is not in the file: the object is found by name.
            'target_schema': '',
        }
        for name in ('stream_schema', 'stream_table'):
            values[f'{name}_lower'] = values[name].lower()
            values[f'{name}_upper'] = values[name].upper()
        unresolved = [v for v in re.findall(r'\{(\w+)\}', template) if v not in values]
        if unresolved:
            context.problem(
                0, f'stream {stream}: {{{unresolved[0]}}} in its object is known only at run time'
            )
            continue
        landed = _fill(template, values).strip('.')
        columns = [c for c in _strings(spec.get('select')) if not c.startswith('-')]
        context.feed(
            'sling',
            task,
            (source, *([stream] if is_file else [schema, table])),
            tuple(landed.split('.')),
            columns=_direct(columns),
        )


# Databricks Lakeflow Connect


def _read_lakeflow(text: str, context: _Context) -> None:
    """Lakeflow Connect pipelines in a Databricks Asset Bundle: each ingestion pipeline's table
    objects, from its connection (or its gateway's) into the destination tables."""
    data = _yaml(text, context)
    if data is None:
        return
    pipelines = ((data.get('resources') or {}).get('pipelines') or {}) if isinstance(data, dict) else {}
    for key, pipeline in pipelines.items():
        definition = (pipeline or {}).get('ingestion_definition')
        if not isinstance(definition, dict):
            continue
        connection = definition.get('connection_name')
        gateway = re.search(
            r'resources\.pipelines\.([A-Za-z0-9_-]+)\.', _name(definition.get('ingestion_gateway_id'), '')
        )
        if not connection and gateway:
            gateway_spec = (pipelines.get(gateway.group(1)) or {}).get('gateway_definition') or {}
            connection = gateway_spec.get('connection_name')
        system = _slug(_name(connection, str(key)))
        name = _name(pipeline.get('name'), str(key))
        for item in definition.get('objects') or []:
            table = (item or {}).get('table')
            if isinstance(table, dict) and table.get('source_table'):
                configuration = table.get('table_configuration') or {}
                context.feed(
                    'lakeflow',
                    name,
                    (system, table.get('source_catalog'), table.get('source_schema'), table['source_table']),
                    (
                        table.get('destination_catalog'),
                        table.get('destination_schema'),
                        table.get('destination_table') or table['source_table'],
                    ),
                    columns=_direct(_strings(configuration.get('include_columns'))),
                )
            elif isinstance((item or {}).get('schema'), dict):
                context.problem(
                    0,
                    f'{name} ingests every table of {item["schema"].get("source_schema")}, which it does not list',
                )


# AWS DMS


def _like(pattern: Any, value: str) -> bool:
    """Whether a value matches a DMS name pattern, in which ``%`` matches any run of characters,
    ignoring case. The pieces between wildcards are found in order, leftmost first, so the time
    is linear in the value rather than a regular expression's backtracking."""
    pieces = (pattern if isinstance(pattern, str) else '').lower().split('%')
    value = value.lower()
    if len(pieces) == 1:
        return value == pieces[0]
    first, *middle, last = pieces
    end = len(value) - len(last)
    if end < len(first) or not value.startswith(first) or not value.endswith(last):
        return False
    position = len(first)
    for piece in middle:
        found = value.find(piece, position, end)
        if found < 0:
            return False
        position = found + len(piece)
    return True


def _locates(locator: dict[str, Any], schema: str, table: str | None = None) -> bool:
    """Whether a DMS object locator (``%`` matches anything) names a schema and table."""
    return _like(locator.get('schema-name', '%'), schema) and (
        table is None or _like(locator.get('table-name', '%'), table)
    )


MAX_DMS_CHECKS = 1_000_000
"""The most pairings of a selected table with a rule that names tables by pattern that Konstruct
checks in one task's mappings."""


class _DmsRules:
    """A task's rules, split once: selections, and transformations by what they change, those
    that name one schema or table by name looked up, those with ``%`` checked in turn."""

    def __init__(self, rules: list[dict[str, Any]]) -> None:
        self.includes: list[dict[str, Any]] = []
        self.excluded: set[tuple[str, str]] = set()
        self.patterns: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        self.exact: dict[tuple[str, str, str | None], list[tuple[int, dict[str, Any]]]] = {}
        for position, rule in enumerate(rules):
            locator = rule.get('object-locator')
            locator = locator if isinstance(locator, dict) else {}
            schema, table = locator.get('schema-name', '%'), locator.get('table-name', '%')
            if rule.get('rule-type') == 'selection':
                if rule.get('rule-action') == 'include':
                    self.includes.append(rule)
                elif rule.get('rule-action') == 'exclude':
                    if _plain(schema) and _plain(table):
                        self.excluded.add((schema.lower(), table.lower()))
                    else:
                        self.patterns.setdefault('exclude', []).append((position, rule))
            elif rule.get('rule-type') == 'transformation':
                target = str(rule.get('rule-target'))
                named = None if target == 'schema' else table
                if _plain(schema) and (named is None or _plain(named)):
                    key = (target, schema.lower(), named.lower() if named is not None else None)
                    self.exact.setdefault(key, []).append((position, rule))
                else:
                    self.patterns.setdefault(target, []).append((position, rule))

    def checks(self) -> int:
        """How many pairings of a selected table with a pattern rule reading the task takes."""
        return len(self.includes) * sum(len(rules) for rules in self.patterns.values())

    def excludes(self, schema: str, table: str) -> bool:
        return (schema.lower(), table.lower()) in self.excluded or any(
            _locates(r.get('object-locator') or {}, schema, table)
            for _, r in self.patterns.get('exclude', [])
        )

    def transformations(self, target: str, schema: str, table: str | None) -> list[dict[str, Any]]:
        """The transformation rules for a schema (``table`` None), a table or its columns, in the
        order the task applies them."""
        found = self.exact.get((target, schema.lower(), table.lower() if table is not None else None), [])
        found = found + [
            (position, rule)
            for position, rule in self.patterns.get(target, [])
            if _locates(rule.get('object-locator') or {}, schema, table)
        ]
        return [rule for _, rule in sorted(found, key=lambda pair: pair[0])]


def _plain(name: Any) -> bool:
    """Whether a DMS locator's name is a name, not a pattern."""
    return isinstance(name, str) and '%' not in name


def _dms_name(rules: list[dict[str, Any]], name: str) -> str:
    """A schema's or table's name after the transformation rules for it."""
    for rule in rules:
        action, value = rule.get('rule-action'), rule.get('value')
        value = value if isinstance(value, str) else ''
        if action == 'rename':
            name = value
        elif action == 'add-prefix':
            name = value + name
        elif action == 'add-suffix':
            name = name + value
        elif action == 'remove-prefix' and name.lower().startswith(value.lower()):
            name = name[len(value) :]
        elif action == 'remove-suffix' and value and name.lower().endswith(value.lower()):
            name = name[: -len(value)]
        elif action == 'convert-lowercase':
            name = name.lower()
        elif action == 'convert-uppercase':
            name = name.upper()
    return name


def _read_dms(text: str, context: _Context) -> None:
    """A DMS task's table mappings: each table a selection rule includes by name, renamed as its
    transformation rules say, and the columns they rename."""
    try:
        data = json.loads(text)
    except ValueError as error:
        context.problem(0, f'cannot read JSON: {error}')
        return
    rules = _DmsRules(
        [r for r in (data.get('rules') or [] if isinstance(data, dict) else []) if isinstance(r, dict)]
    )
    if rules.checks() > MAX_DMS_CHECKS:
        context.problem(
            0,
            f'its {len(rules.includes):,} selections and rules that name tables by pattern make more than '
            f'{MAX_DMS_CHECKS:,} pairs to check, so its tables are not known',
        )
        return
    task = PurePosixPath(context.path).stem
    for rule in rules.includes:
        locator = rule.get('object-locator') or {}
        schema, table = str(locator.get('schema-name', '%')), str(locator.get('table-name', '%'))
        if '%' in schema or '%' in table:
            context.problem(
                0,
                f'rule {rule.get("rule-name")} selects {schema}.{table} by pattern, so its tables are not known',
            )
            continue
        if rules.excludes(schema, table):
            continue
        renamed = [
            (r.get('value'), (r.get('object-locator') or {}).get('column-name'), 'direct')
            for r in rules.transformations('column', schema, table)
            if r.get('rule-action') == 'rename'
        ]
        context.feed(
            'dms',
            task,
            (_slug(task), schema, table),
            (
                _dms_name(rules.transformations('schema', schema, None), schema),
                _dms_name(rules.transformations('table', schema, table), table),
            ),
            columns=[(str(a), str(b), c) for a, b, c in renamed if _plain(a) and _plain(b)],
        )


READERS: dict[str, Callable[[str, _Context], None]] = {
    'dlt': _read_dlt,
    'dlt_schema': _read_dlt_schema,
    'singer_catalog': _read_singer_catalog,
    'singer_state': _read_singer_state,
    'airbyte_api': _read_airbyte_api,
    'kafka_connect': _read_kafka_connect,
    'estuary': _read_estuary,
    'sling': _read_sling,
    'lakeflow': _read_lakeflow,
    'dms': _read_dms,
}
GROUP_READERS: dict[str, Callable[[list[tuple[str, str]], _Context], None]] = {
    'meltano': _read_meltano_project,
    'terraform': _read_terraform,
    'airbyte_octavia': _read_octavia,
}


def read_files(kind: str, files: list[tuple[str, str]]) -> Reading:
    """The feeds a format's files describe: one file, or for the formats in :data:`GROUPED`, every
    file of one directory. Each file is a ``(path, text)`` pair."""
    context = _Context(files[0][0] if files else '')
    if kind in GROUP_READERS:
        GROUP_READERS[kind](files, context)
    else:
        for path, text in files:
            with context.reading_file(path):
                READERS[kind](text, context)
    return context.reading


def read(kind: str, path: str, text: str) -> Reading:
    """The feeds one configuration file describes."""
    return read_files(kind, [(path, text)])
