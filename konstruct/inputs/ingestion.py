"""Where landed tables come from, read from the files that configure the loaders.

Each format gives :class:`Feed` records: a loader's task (a pipeline, connection or connector),
the source system's table it reads (``postgres.app.public.orders``), the table it lands, and the
columns it copies when the file names them. Nothing is imported, run or connected to: Python is
parsed with :mod:`ast`, YAML with a safe loader and JSON with :mod:`json`.

:func:`kind_of` says from a file's name and a cheap look at its text which format it is, so
discovery reads nothing else; :func:`read` turns one file into feeds and problems.
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

MAX_BYTES = 2_000_000
"""Files larger than this are not configuration, and are not read."""
SUFFIXES = ('.py', '.yml', '.yaml', '.json')
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
}
"""The loader each format configures."""


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
    if lower.endswith('.json'):
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


def _read_meltano(text: str, context: _Context) -> None:
    """meltano.yml: each extractor and loader a job or schedule pairs, the streams the extractor
    selects, and the schema the loader lands them in; without one, the table is found by name."""
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
    for tap_name, target_name in pairs:
        tap = extractors.get(tap_name) or {'base': tap_name, 'config': {}}
        target = loaders.get(target_name) or {'base': target_name, 'config': {}}
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


READERS: dict[str, Callable[[str, _Context], None]] = {
    'dlt': _read_dlt,
    'dlt_schema': _read_dlt_schema,
    'meltano': _read_meltano,
    'singer_catalog': _read_singer_catalog,
    'singer_state': _read_singer_state,
}


def read(kind: str, path: str, text: str) -> Reading:
    """The feeds one configuration file of a format describes."""
    context = _Context(path)
    with context.reading_file(path):
        READERS[kind](text, context)
    return context.reading
