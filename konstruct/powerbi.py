"""Power BI semantic models in TMDL, read as exposures of the warehouse columns they use.

A Power BI project (PBIP) keeps its semantic model as TMDL text files under
``<name>.SemanticModel/definition/``. Each table there becomes an exposure. It reads the warehouse
table its partition's M source navigates to (``Source{[Schema="dbo",Item="orders"]}`` after
``Sql.Database``, or ``{[Name=..., Kind="Database"]}`` navigation for Snowflake, Databricks and
BigQuery, directly or through a shared expression), the tables a native query reads (in the
connector's dialect), or, for a Direct Lake partition, its ``schemaName`` and ``entityName``.
Each column reads the warehouse column its ``sourceColumn`` names. Measures, calculated columns
and calculated tables read the columns their DAX names, resolved across the whole model:
``Table[Column]`` on any table, a measure named as ``[Measure]`` through the columns it reads,
and a calculated column through its own DAX. A table named without a column reads that table.
The files are read as text; no M or DAX is evaluated.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from konstruct.sql import PARSE_ERRORS

FILE_SUFFIX = '.tmdl'
_OBJECT = re.compile(r"^(\w+)\s+('(?:[^']|'')*'|[^\s=:']+)\s*(?:=\s*(.*))?$")
_OBJECT_KINDS = frozenset(
    (
        'annotation',
        'calculationGroup',
        'calculationItem',
        'changedProperty',
        'column',
        'culture',
        'database',
        'dataSource',
        'expression',
        'extendedProperty',
        'formatStringDefinition',
        'hierarchy',
        'level',
        'linguisticMetadata',
        'measure',
        'model',
        'partition',
        'perspective',
        'queryGroup',
        'relationship',
        'role',
        'table',
        'tablePermission',
        'variation',
    )
)
_PROPERTY = re.compile(r'^(\w+)\s*(?::\s*(.*)|=\s*(.*))?$')
_RECORD = re.compile(r'\[([^\[\]]*)\]')
_FIELD = re.compile(r'(\w+)\s*=\s*"((?:[^"]|"")*)"')
_QUERY = re.compile(r'\bQuery\s*=\s*("(?:[^"]|"")*")')
_M_ESCAPE = re.compile(r'#\((cr,lf|lf|cr|tab)\)')
_M_ESCAPES = {'lf': '\n', 'cr': '\r', 'tab': '\t', 'cr,lf': '\r\n'}
_CONNECTOR_DIALECTS = (
    ('Sql.Database', 'tsql'),
    ('Snowflake.', 'snowflake'),
    ('Databricks', 'databricks'),
    ('GoogleBigQuery.', 'bigquery'),
    ('PostgreSQL.', 'postgres'),
    ('AmazonRedshift.', 'redshift'),
    ('Oracle.', 'oracle'),
    ('MySQL.', 'mysql'),
)
_DATABASE_KINDS = frozenset(('database', 'catalog', 'project'))
_TABLE_KINDS = frozenset(('table', 'view'))
_AUTO_DATE = frozenset(('__PBI_LocalDateTable', '__PBI_TemplateDateTable'))
_DAX_REFERENCE = re.compile(
    r"'((?:[^']|'')+)'\s*(?:\[([^\]]+)\])?|\b(\w+)\s*\[([^\]]+)\]|\[([^\]]+)\]|\b([A-Za-z_]\w*)\b(?!\s*\()"
)
_DAX_VAR = re.compile(r'\bVAR\s+(\w+)', re.IGNORECASE)
_DAX_STRIP = re.compile(r'"(?:[^"]|"")*"|//[^\n]*|--[^\n]*|/\*.*?\*/', re.DOTALL)


@dataclass
class _Object:
    kind: str
    name: str
    depth: int
    expression: str | None = None
    properties: dict[str, str] = field(default_factory=dict)
    children: list[_Object] = field(default_factory=list)
    description: str | None = None


def _name(text: str) -> str:
    text = text.strip()
    return text[1:-1].replace("''", "'") if len(text) > 1 and text[0] == text[-1] == "'" else text


def model_name(path: str) -> str:
    """The semantic model a file belongs to: the ``<name>.SemanticModel`` folder it is in."""
    for part in reversed(path.replace('\\', '/').split('/')):
        if part.endswith('.SemanticModel'):
            return part.removesuffix('.SemanticModel')
    return 'model'


def model_folder(path: str) -> str:
    """The folder of the semantic model a file belongs to."""
    parts = path.replace('\\', '/').split('/')
    for index in range(len(parts) - 1, -1, -1):
        if parts[index].endswith('.SemanticModel'):
            return '/'.join(parts[: index + 1])
    return '/'.join(parts[:-1]) or '.'


def _tree(text: str) -> list[_Object]:
    """The objects of a TMDL file, nested by indentation (tabs, or a consistent number of
    spaces), with their properties and expressions. A multi-line expression follows ``=`` on lines
    indented deeper than its property, or at least two levels deeper than its object, or sits
    between ```` ``` ```` fences."""
    lines = text.lstrip('﻿').replace('\r\n', '\n').replace('\r', '\n').split('\n')
    widths = [len(line) - len(line.lstrip(' ')) for line in lines if line.startswith(' ') and line.strip()]
    unit = min((w for w in widths if w), default=4)

    def depth_of(line: str) -> int:
        tabs = len(line) - len(line.lstrip('\t'))
        rest = line[tabs:]
        return tabs + (len(rest) - len(rest.lstrip(' '))) // unit

    roots: list[_Object] = []
    stack: list[_Object] = []
    description: list[str] = []
    index = 0

    def expression(start: str, min_depth: int) -> str:
        nonlocal index
        if start.startswith('```'):
            body = [start[3:].strip()] if start[3:].strip() else []
            while index < len(lines):
                line = lines[index]
                index += 1
                if line.strip().startswith('```'):
                    break
                body.append(line.strip())
            return '\n'.join(body)
        if start:
            return start
        body = []
        while index < len(lines):
            line = lines[index]
            if line.strip() and depth_of(line) < min_depth:
                break
            body.append(line.strip())
            index += 1
        return '\n'.join(body).strip()

    while index < len(lines):
        raw = lines[index]
        index += 1
        line = raw.strip()
        if not line:
            continue
        if line.startswith('///'):
            description.append(line[3:].strip())
            continue
        depth = depth_of(raw)
        while stack and stack[-1].depth >= depth:
            stack.pop()
        match = _OBJECT.match(line)
        if match and match.group(1) in _OBJECT_KINDS:
            kind, name, value = match.group(1), _name(match.group(2)), match.group(3)
            item = _Object(kind, name, depth, description=' '.join(description) or None)
            if value is not None:
                item.expression = expression(value.strip(), depth + 2)
            (stack[-1].children if stack else roots).append(item)
            stack.append(item)
        elif stack:
            prop = _PROPERTY.match(line)
            if prop and prop.group(3) is not None:
                stack[-1].properties[prop.group(1)] = expression(prop.group(3).strip(), depth + 1)
            elif prop:
                stack[-1].properties[prop.group(1)] = (prop.group(2) or 'true').strip()
        description = []
    return roots


@dataclass
class PowerBIColumn:
    name: str
    source_column: str | None = None
    """The warehouse column it imports, for a data column."""
    expression: str | None = None
    """The DAX of a calculated column."""


@dataclass
class PowerBIPartition:
    name: str
    kind: str
    """``m``, ``calculated``, ``entity`` or another partition kind."""
    source: str | None = None
    """Its M source, or the DAX of a calculated partition."""
    entity: str | None = None
    """For a Direct Lake partition, ``schemaName.entityName``."""


@dataclass
class PowerBITable:
    name: str
    path: str
    model: str
    columns: list[PowerBIColumn] = field(default_factory=list)
    measures: dict[str, str] = field(default_factory=dict)
    """Measure name to its DAX."""
    partitions: list[PowerBIPartition] = field(default_factory=list)
    description: str | None = None
    helper: bool = False
    """A table Power BI makes for itself, such as an automatic date table."""
    refresh_source: str | None = None
    """The M of its incremental refresh policy, which its ``policyRange`` partitions read."""
    sources: list[str] = field(default_factory=list)
    """Once resolved: the warehouse tables its partitions read."""
    mapped: bool = False
    """Once resolved: whether its columns are its one source's columns (not a query's)."""
    reads: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    """Once resolved: each warehouse ``(table, column)`` it reads, with the fields that read it;
    a column of ``''`` is the table as a whole."""

    def to_dict(self) -> dict[str, Any]:
        return {
            'name': self.name,
            'path': self.path,
            'model': self.model,
            'columns': [vars(c) for c in self.columns],
            'measures': self.measures,
            'partitions': [vars(p) for p in self.partitions],
            'description': self.description,
            'helper': self.helper,
            'refresh_source': self.refresh_source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PowerBITable:
        return cls(
            name=str(data['name']),
            path=str(data['path']),
            model=str(data['model']),
            columns=[PowerBIColumn(**c) for c in data['columns']],
            measures=dict(data['measures']),
            partitions=[PowerBIPartition(**p) for p in data['partitions']],
            description=data.get('description'),
            helper=bool(data.get('helper')),
            refresh_source=data.get('refresh_source'),
        )


@dataclass
class PowerBIFile:
    """One TMDL file as read: its tables and shared expressions, not yet resolved."""

    path: str
    tables: list[PowerBITable] = field(default_factory=list)
    expressions: dict[str, str] = field(default_factory=dict)
    """Shared M expressions (``expressions.tmdl``) by name."""

    def to_dict(self) -> dict[str, Any]:
        return {
            'path': self.path,
            'tables': [t.to_dict() for t in self.tables],
            'expressions': self.expressions,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PowerBIFile:
        return cls(
            path=str(data['path']),
            tables=[PowerBITable.from_dict(t) for t in data['tables']],
            expressions=dict(data['expressions']),
        )


def parse(text: str, path: str) -> PowerBIFile:
    """The tables and shared expressions of a TMDL file."""
    result = PowerBIFile(path=path)
    model = model_name(path)
    for item in _tree(text):
        if item.kind == 'expression' and item.expression:
            result.expressions[item.name] = item.expression
        if item.kind != 'table':
            continue
        table = PowerBITable(
            item.name,
            path,
            model,
            description=item.description,
            refresh_source=item.properties.get('sourceExpression'),
        )
        annotations = {c.name for c in item.children if c.kind == 'annotation'}
        table.helper = (
            bool(annotations & _AUTO_DATE)
            or item.properties.get('showAsVariationsOnly') == 'true'
            or item.properties.get('isPrivate') == 'true'
        )
        for child in item.children:
            props = child.properties
            if child.kind == 'column':
                source = _name(props['sourceColumn']) if 'sourceColumn' in props else None
                table.columns.append(PowerBIColumn(child.name, source, child.expression or None))
            elif child.kind == 'measure':
                table.measures[child.name] = child.expression or ''
            elif child.kind == 'partition':
                entity = props.get('entityName')
                table.partitions.append(
                    PowerBIPartition(
                        child.name,
                        (child.expression or 'm').strip().lower(),
                        props.get('source'),
                        '.'.join(p for p in (props.get('schemaName'), entity) if p) if entity else None,
                    )
                )
        result.tables.append(table)
    return result


def _arguments(text: str, start: int) -> list[str]:
    """The top-level arguments, as written, of the call whose ``(`` is at ``start``."""
    depth, args, current, index = 0, [], [], start
    while index < len(text):
        char = text[index]
        if char == '"':
            end = index + 1
            while end < len(text) and not (text[end] == '"' and text[end + 1 : end + 2] != '"'):
                end += 2 if text[end] == '"' else 1
            current.append(text[index : end + 1])
            index = end + 1
            continue
        if char in '([{':
            depth += 1
            if depth == 1:
                index += 1
                continue
        elif char in ')]}':
            depth -= 1
            if depth == 0:
                args.append(''.join(current).strip())
                return args
        elif char == ',' and depth == 1:
            args.append(''.join(current).strip())
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    return args


def _m_string(argument: str) -> str | None:
    """An M text literal's value, with its escapes decoded."""
    if len(argument) < 2 or argument[0] != '"' or argument[-1] != '"':
        return None
    return _M_ESCAPE.sub(lambda m: _M_ESCAPES[m.group(1)], argument[1:-1].replace('""', '"'))


def connector_dialect(m: str) -> str | None:
    """The SQL dialect of the source an M expression connects to."""
    return next((dialect for prefix, dialect in _CONNECTOR_DIALECTS if prefix in m), None)


def native_query(m: str) -> str | None:
    """The SQL of a native query: the second argument of ``Value.NativeQuery(source, "sql")``, or
    a connector's ``[Query="sql"]`` option."""
    call = re.search(r'Value\.NativeQuery\s*\(', m)
    if call:
        args = _arguments(m, call.end() - 1)
        return _m_string(args[1]) if len(args) > 1 else None
    query = _QUERY.search(m)
    return _m_string(query.group(1)) if query else None


def navigation(m: str) -> list[str]:
    """The warehouse tables an M expression navigates to, in order."""
    prefix: list[str] = []
    database = re.search(r'Sql\.Databases?\s*\(', m)
    if database:
        args = _arguments(m, database.end() - 1)
        name = _m_string(args[1]) if len(args) > 1 else None
        prefix = [name] if name else []
    found: list[str] = []
    steps: dict[str, str] = {}
    for record in _RECORD.finditer(m):
        fields = {k.lower(): v.replace('""', '"') for k, v in _FIELD.findall(record.group(1))}
        if 'item' in fields and 'schema' in fields:
            found.append('.'.join([*prefix, fields['schema'], fields['item']]))
        elif 'name' in fields:
            kind = fields.get('kind', 'table' if steps else 'database').lower()
            if kind in _DATABASE_KINDS:
                steps = {'database': fields['name']}
            elif kind == 'schema':
                steps = {k: v for k, v in steps.items() if k == 'database'} | {'schema': fields['name']}
            elif kind in _TABLE_KINDS:
                found.append(
                    '.'.join([*(steps[k] for k in ('database', 'schema') if k in steps), fields['name']])
                )
    return list(dict.fromkeys(found))


def native_reads(sql: str, dialect: str | None) -> list[str] | None:
    """The tables a native query reads, or None when it is not SQL that can be read."""
    try:
        trees = [t for t in sqlglot.parse(sql, read=dialect) if t is not None]
    except PARSE_ERRORS:
        return None
    reads = set()
    for tree in trees:
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        for table in tree.find_all(exp.Table):
            if table.name and not (not table.db and table.name.lower() in ctes):
                reads.add('.'.join(p for p in (table.catalog, table.db, table.name) if p))
    return sorted(reads) if trees else None


def references(dax: str) -> list[tuple[str | None, str | None]]:
    """The ``(table, name)`` pairs a DAX expression names: ``Table[Column]`` gives both,
    ``[Name]`` a name on no table, and a table named alone a table with no name. Strings and
    comments are left out."""
    found: list[tuple[str | None, str | None]] = []
    for quoted, quoted_name, bare, bare_name, alone, word in _DAX_REFERENCE.findall(_DAX_STRIP.sub(' ', dax)):
        if quoted:
            found.append((quoted.replace("''", "'"), quoted_name or None))
        elif bare:
            found.append((bare, bare_name))
        elif alone:
            found.append((None, alone))
        elif word:
            found.append((word, None))
    return found


ReadSql = Callable[[str, str | None], list[str] | None]


def resolve(
    files: list[PowerBIFile], dialect: str | None = None, read_sql: ReadSql = native_reads
) -> tuple[list[PowerBITable], list[str]]:
    """The tables of each semantic model, with the warehouse tables they read and the warehouse
    columns each of their fields reads, through measures and calculated columns on any table of
    the same model. Tables Power BI makes for itself, such as automatic date tables, are left
    out. ``read_sql`` gives the tables a native query reads, in a dialect."""
    problems: list[str] = []
    models: dict[str, dict[str, PowerBITable]] = {}
    shared: dict[str, dict[str, str]] = {}
    folders: dict[str, str] = {}
    for file in sorted(files, key=lambda f: f.path):
        model, folder = model_name(file.path), model_folder(file.path)
        first = folders.setdefault(model, folder)
        if first != folder:
            problems.append(f'{file.path}: semantic model {model!r} is also in {first}; kept that one')
            continue
        shared.setdefault(model, {}).update(file.expressions)
        tables = models.setdefault(model, {})
        for table in file.tables:
            if table.name.lower() in tables:
                problems.append(
                    f'{file.path}: table {table.name!r} is also defined in {tables[table.name.lower()].path}'
                )
                continue
            tables[table.name.lower()] = table

    for model, tables in models.items():
        for table in tables.values():
            _resolve_sources(table, shared.get(model, {}), dialect, read_sql, problems)
        _resolve_fields(tables, problems)
    resolved = [t for tables in models.values() for t in tables.values() if not t.helper]
    return sorted(resolved, key=lambda t: (t.model, t.name)), problems


def _resolve_sources(
    table: PowerBITable, shared: dict[str, str], dialect: str | None, read_sql: ReadSql, problems: list[str]
) -> None:
    where = f'{table.path}: table {table.name!r}'
    for partition in table.partitions:
        if partition.kind == 'calculated' or table.helper:
            continue
        if partition.kind == 'entity':
            if partition.entity:
                table.sources.append(partition.entity)
            else:
                problems.append(f'{where}: Direct Lake partition {partition.name!r} names no table')
            continue
        m = (table.refresh_source if partition.kind == 'policyrange' else None) or partition.source or ''
        # A partition built on a shared expression reads what that expression reads.
        for name, body in shared.items():
            if re.search(
                r'#"' + re.escape(name) + r'"|(?<![\w."#])' + re.escape(name) + r'(?![\w"]|\s*=)', m
            ):
                m += '\n' + body
        sql = native_query(m)
        if sql is not None:
            reads = read_sql(sql, connector_dialect(m) or dialect)
            if reads:
                table.sources += reads
            else:
                problems.append(f'{where}: the native query in partition {partition.name!r} reads no table')
            continue
        found = navigation(m)
        if found:
            table.sources += found
        else:
            problems.append(f'{where}: no warehouse table found in partition {partition.name!r}')
    table.sources = list(dict.fromkeys(table.sources))
    native = any(native_query(p.source or '') for p in table.partitions)
    table.mapped = len(table.sources) == 1 and not native


Read = tuple[str, str]


def _resolve_fields(tables: dict[str, PowerBITable], problems: list[str]) -> None:
    """Each field's warehouse reads. Every measure and calculated column is scanned once for what
    it reads directly and the fields it names; a field then reads what every field it reaches
    reads, found by a walk that visits each field once, so chains and cycles of measures cost
    time in proportion to their size."""
    measures = {name.lower(): (table, name) for table in tables.values() for name in table.measures}
    direct: dict[str, tuple[set[Read], set[str]]] = {}
    for table in tables.values():
        for name, dax in table.measures.items():
            if not dax.strip():
                problems.append(f'{table.path}: table {table.name!r}: measure {name!r} has no expression')
            direct[f'm:{name.lower()}'] = _direct(dax, table, tables, measures)
        for column in table.columns:
            if column.expression:
                direct[_column_key(table, column)] = _direct(column.expression, table, tables, measures)
    closed: dict[str, set[Read]] = {}

    def reach(start: str) -> set[Read]:
        if start in closed:
            return closed[start]
        reads: set[Read] = set()
        seen, pending = {start}, [start]
        while pending:
            key = pending.pop()
            if key in closed:
                reads |= closed[key]
                continue
            found, names = direct.get(key, (set(), set()))
            reads |= found
            for name in names - seen:
                seen.add(name)
                pending.append(name)
        closed[start] = reads
        return reads

    for table in tables.values():
        for column in table.columns:
            if column.source_column and table.mapped:
                reads = {(table.sources[0], column.source_column)}
            elif column.expression:
                reads = reach(_column_key(table, column))
            else:
                reads = {(source, '') for source in table.sources}
            for read in reads:
                table.reads.setdefault(read, set()).add(column.name)
        for name in table.measures:
            for read in reach(f'm:{name.lower()}'):
                table.reads.setdefault(read, set()).add(name)
        for partition in table.partitions:
            if partition.kind == 'calculated' and partition.source:
                found, names = _direct(partition.source, table, tables, measures)
                for read in found.union(*(reach(n) for n in names)):
                    table.reads.setdefault(read, set())
        for source in table.sources:
            table.reads.setdefault((source, ''), set())


def _column_key(table: PowerBITable, column: PowerBIColumn) -> str:
    return f'c:{table.name.lower()}[{column.name.lower()}]'


def _direct(
    dax: str,
    owner: PowerBITable,
    tables: dict[str, PowerBITable],
    measures: dict[str, tuple[PowerBITable, str]],
) -> tuple[set[Read], set[str]]:
    """What a DAX expression reads directly, as warehouse ``(table, column)`` pairs (``(table,
    '')`` for a table read as a whole), and the measures and calculated columns it names. Names
    the expression binds with ``VAR`` are not tables."""
    reads: set[Read] = set()
    names: set[str] = set()
    bound = {v.lower() for v in _DAX_VAR.findall(_DAX_STRIP.sub(' ', dax))}
    for table_name, name in references(dax):
        if table_name and name is None and table_name.lower() in bound:
            continue
        table = tables.get(table_name.lower()) if table_name else owner
        if table is None:
            continue
        if name is None:
            reads.update((source, '') for source in table.sources)
            continue
        column = next((c for c in table.columns if c.name.lower() == name.lower()), None)
        if column is not None and column.source_column and table.mapped:
            reads.add((table.sources[0], column.source_column))
        elif column is not None and column.expression:
            names.add(_column_key(table, column))
        elif column is not None:
            reads.update((source, '') for source in table.sources)
        elif name.lower() in measures:
            names.add(f'm:{name.lower()}')
    return reads, names
