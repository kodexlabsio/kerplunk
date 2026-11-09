"""Column-level lineage for SQL statements, built on sqlglot.

`analyse()` takes one SQL string (one or more statements) and returns, per statement, the
relation it writes (if any), the relations it reads, the physical columns each output column
is derived from, and the columns it only joins, filters or groups on.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect, DialectType
from sqlglot.errors import SqlglotError
from sqlglot.lineage import Node as LineageNode
from sqlglot.lineage import lineage, to_node
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope
from sqlglot.schema import MappingSchema

from konstruct.graph import relation_key

Transform = Literal['direct', 'transformed']

# dbt adapter types (and common aliases) to sqlglot dialect names.
DIALECTS: dict[str, str] = {
    'duckdb': 'duckdb',
    'postgres': 'postgres',
    'postgresql': 'postgres',
    'redshift': 'redshift',
    'snowflake': 'snowflake',
    'bigquery': 'bigquery',
    'databricks': 'databricks',
    'spark': 'spark',
    'trino': 'trino',
    'athena': 'athena',
    'sqlserver': 'tsql',
    'synapse': 'tsql',
    'fabric': 'tsql',
    'clickhouse': 'clickhouse',
    'mysql': 'mysql',
    'sqlite': 'sqlite',
    'oracle': 'oracle',
    'teradata': 'teradata',
}

# Schema columns: relation key ("catalog.db.table") -> {column: type}
SchemaColumns = Mapping[str, Mapping[str, str]]


def dialect_for(name: str | None) -> str | None:
    if not name:
        return None
    return DIALECTS.get(name.lower(), name.lower())


@dataclass(frozen=True)
class ColumnSource:
    relation: str
    column: str
    transform: Transform


@dataclass
class StatementLineage:
    kind: str
    target: str | None = None
    reads: list[str] = field(default_factory=list)
    columns: dict[str, list[ColumnSource]] = field(default_factory=dict)
    references: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Each output column's expression with CTEs, subqueries and aliases inlined down to physical
    # columns, and the statement's join, filter and grouping conditions in the same form. Both
    # ignore formatting, quoting, aliases and CTE names, so `konstruct diff` can compare versions.
    expressions: dict[str, str] = field(default_factory=dict)
    conditions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            'kind': self.kind,
            'target': self.target,
            'reads': self.reads,
            'columns': {
                name: [[s.relation, s.column, s.transform] for s in sources]
                for name, sources in self.columns.items()
            },
            'references': [list(r) for r in self.references],
            'errors': self.errors,
            'expressions': self.expressions,
            'conditions': self.conditions,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StatementLineage:
        return cls(
            kind=data['kind'],
            target=data.get('target'),
            reads=list(data.get('reads', [])),
            columns={
                name: [ColumnSource(r, c, t) for r, c, t in sources]
                for name, sources in data.get('columns', {}).items()
            },
            references=[(r, c) for r, c in data.get('references', [])],
            errors=list(data.get('errors', [])),
            expressions=dict(data.get('expressions', {})),
            conditions=list(data.get('conditions', [])),
        )


_JINJA_EXPR = re.compile(r"(['\"]?)\{\{.*?\}\}(['\"]?)", re.DOTALL)
_JINJA_BLOCK = re.compile(r'\{%.*?%\}|\{#.*?#\}', re.DOTALL)
PLACEHOLDER = '__konstruct__'


def strip_templating(sql: str) -> str:
    """Replace Jinja expressions with placeholders and drop Jinja blocks and comments.

    Good enough for Airflow-style ``{{ ds }}`` parameters; dbt models are read compiled. A
    template next to a dot is part of a name, so it becomes an identifier; anywhere else it
    becomes a string literal.
    """

    def replace(match: re.Match[str]) -> str:
        before = sql[match.start() - 1] if match.start() > 0 else ''
        after = sql[match.end()] if match.end() < len(sql) else ''
        open_quote, close_quote = match.group(1), match.group(2)
        if open_quote == '"' or before == '.' or after == '.':
            return f'{open_quote}{PLACEHOLDER}{close_quote}'
        return f"'{PLACEHOLDER}'"

    return _JINJA_BLOCK.sub('', _JINJA_EXPR.sub(replace, sql))


def build_schema(columns: SchemaColumns, dialect: DialectType) -> MappingSchema:
    """A sqlglot schema with every relation at catalog.db.table depth."""
    nested: dict[str, Any] = {}
    for key, cols in columns.items():
        parts = key.split('.')
        if len(parts) == 2:
            parts = ['', *parts]
        elif len(parts) == 1:
            parts = ['', '', *parts]
        if len(parts) != 3 or not cols:
            continue
        catalog, db, table = parts
        nested.setdefault(catalog, {}).setdefault(db, {})[table] = {
            name: _safe_type(dtype, dialect) for name, dtype in cols.items()
        }
    return MappingSchema(nested, dialect=dialect, normalize=False)


def _safe_type(dtype: str | None, dialect: DialectType) -> str:
    if not dtype:
        return 'UNKNOWN'
    try:
        exp.DataType.build(dtype, dialect=dialect)
    except (SqlglotError, ValueError):
        return 'UNKNOWN'
    return dtype


def table_key(table: exp.Table, default_catalog: str | None, default_db: str | None) -> str:
    return relation_key(
        table.catalog or (default_catalog if table.db or default_db else None),
        table.db or default_db,
        table.name,
    )


def analyse(
    sql: str,
    *,
    dialect: str | None = None,
    schema: SchemaColumns | None = None,
    default_catalog: str | None = None,
    default_db: str | None = None,
    target: str | None = None,
) -> list[StatementLineage]:
    """Analyse every statement in ``sql``.

    ``target`` names the relation a bare SELECT writes (a dbt model's relation). Relations
    created by earlier statements become part of the schema for later ones.
    """
    known: dict[str, dict[str, str]] = {k: dict(v) for k, v in (schema or {}).items()}
    # One dialect object for the whole analysis: sqlglot otherwise builds a new one for every parse
    # and every expression it prints.
    resolved = Dialect.get_or_raise(dialect)
    try:
        statements = [s for s in sqlglot.parse(sql, dialect=resolved) if s is not None]
    except SqlglotError as error:
        return [StatementLineage(kind='unparsed', target=target, errors=[_first_line(error)])]

    results: list[StatementLineage] = []
    for statement in statements:
        result = _analyse_statement(statement, resolved, known, default_catalog, default_db, target)
        if result is None:
            continue
        if result.target and result.columns and result.target not in known:
            known[result.target] = dict.fromkeys(result.columns, 'UNKNOWN')
        results.append(result)
    return results


def _first_line(error: Exception) -> str:
    text = str(error).strip()
    return text.splitlines()[0] if text else type(error).__name__


def _analyse_statement(
    statement: exp.Expr,
    dialect: DialectType,
    known: SchemaColumns,
    default_catalog: str | None,
    default_db: str | None,
    target: str | None,
) -> StatementLineage | None:
    query: exp.Expr | None = None
    out_names: list[str] | None = None
    kind = type(statement).__name__.lower()
    write: exp.Table | None = None

    if isinstance(statement, exp.Query):
        query, kind = statement, 'select'
    elif isinstance(statement, exp.Create):
        if str(statement.args.get('kind') or '').upper() not in ('TABLE', 'VIEW'):
            return None
        this = statement.this
        write = this.this if isinstance(this, exp.Schema) else this
        if isinstance(this, exp.Schema):
            out_names = [c.name for c in this.expressions if isinstance(c, exp.ColumnDef)] or None
        expression = statement.expression
        query = expression if isinstance(expression, exp.Query) else None
        kind = f'create_{(statement.args.get("kind") or "table").lower()}'
        if query is None and not isinstance(write, exp.Table):
            return None
    elif isinstance(statement, exp.Insert):
        this = statement.this
        write = this.this if isinstance(this, exp.Schema) else this
        if isinstance(this, exp.Schema):
            out_names = [c.name for c in this.expressions] or None
        expression = statement.expression
        query = expression if isinstance(expression, exp.Query) else None
    elif isinstance(statement, (exp.Merge, exp.Update, exp.Delete, exp.Copy)):
        this = statement.this
        write = this if isinstance(this, exp.Table) else None
    else:
        return None

    write_key = table_key(write, default_catalog, default_db) if isinstance(write, exp.Table) else target
    result = StatementLineage(kind=kind, target=write_key)

    ctes = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    reads: list[str] = []
    for table in statement.find_all(exp.Table):
        if table is write or (not table.db and table.name.lower() in ctes) or not table.name:
            continue
        key = table_key(table, default_catalog, default_db)
        if key != write_key and key not in reads:
            reads.append(key)
    result.reads = reads

    if query is None:
        return result

    try:
        _column_lineage(query, dialect, known, default_catalog, default_db, out_names, result)
    except (SqlglotError, ValueError, KeyError, IndexError, AttributeError, RecursionError) as error:
        result.errors.append(f'column lineage failed: {_first_line(error)}')
    return result


def _column_lineage(
    query: exp.Expr,
    dialect: DialectType,
    known: SchemaColumns,
    default_catalog: str | None,
    default_db: str | None,
    out_names: list[str] | None,
    result: StatementLineage,
) -> None:
    schema = build_schema(known, dialect)
    qualify_kwargs: dict[str, Any] = {
        'dialect': dialect,
        'schema': schema,
        'validate_qualify_columns': False,
        'identify': False,
    }
    if default_db:
        qualify_kwargs['db'] = default_db
        if default_catalog:
            qualify_kwargs['catalog'] = default_catalog
    # The statement was parsed for this analysis alone and is not read again, so it is qualified in
    # place rather than copied first.
    qualified = qualify(query, **qualify_kwargs)
    scope = build_scope(qualified)
    if scope is None:
        result.errors.append('column lineage failed: not a query')
        return

    # trim_selects only tidies node labels, at the cost of copying the query for every node.
    nodes = lineage(
        None, qualified, schema=schema, dialect=dialect, scope=scope, copy=False, trim_selects=False
    )
    if out_names is not None and len(out_names) != len(nodes):
        result.errors.append('INSERT column list does not match the SELECT list')
        out_names = None

    star_tables = [table_key(t, default_catalog, default_db) for t in _unexpanded_star_tables(scope)]
    for index, (name, node) in enumerate(nodes.items()):
        if name == '*':
            continue
        out_name = (out_names[index] if out_names else name).lower()
        sources: list[ColumnSource] = []
        for leaf, transform, asked_for, path in _leaves(node):
            source = _leaf_source(leaf, asked_for, transform, default_catalog, default_db, path)
            if source and source not in sources:
                sources.append(source)
        result.columns[out_name] = sources
    if star_tables:
        result.errors.append(
            'SELECT * over relations with unknown columns: ' + ', '.join(sorted(set(star_tables)))
        )

    result.references = _references(scope, dialect, schema, default_catalog, default_db)
    canon = _Canonical(dialect, default_catalog, default_db)
    for index, (name, node) in enumerate(nodes.items()):
        if name != '*':
            out_name = (out_names[index] if out_names else name).lower()
            result.expressions[out_name] = _unbracket(canon.node(node))
    result.conditions = canon.conditions(scope, schema)


def _strip_fields(expression: exp.Expr) -> exp.Expr:
    """``customer.address.city`` or ``customer['city']`` down to the ``customer`` column."""
    while _is_field_access(expression):
        expression = expression.this
    return expression


def _is_field_access(expression: exp.Expr | None) -> bool:
    if isinstance(expression, exp.Dot):
        return isinstance(expression.expression, exp.Identifier)
    return isinstance(expression, exp.Bracket)


def _is_index(bracket: exp.Bracket) -> bool:
    """``items[1]``: an element of a list, which reads the same field path as the list itself."""
    keys = bracket.expressions
    return len(keys) == 1 and isinstance(keys[0], exp.Literal) and not keys[0].is_string


def _bracket_field(bracket: exp.Bracket) -> str | None:
    keys = bracket.expressions
    if len(keys) == 1 and isinstance(keys[0], exp.Literal) and keys[0].is_string:
        return str(keys[0].this)
    return None


def _is_passthrough(node: LineageNode) -> bool:
    expression = node.expression
    if isinstance(node.source, exp.SetOperation) or isinstance(expression, (exp.Table, exp.Star)):
        return True
    if isinstance(node.source, (exp.Unnest, exp.Explode)) and isinstance(expression, exp.Identifier):
        return True
    if isinstance(expression, exp.Alias):
        expression = expression.this
    expression = _strip_fields(expression)
    # Unnesting a list passes its elements through unchanged; only the rows multiply.
    if isinstance(expression, (exp.Unnest, exp.Explode)):
        arguments = expression.expressions if isinstance(expression, exp.Unnest) else [expression.this]
        return len(arguments) == 1 and isinstance(_strip_fields(arguments[0]), exp.Column)
    return isinstance(expression, (exp.Column, exp.Star))


def _plain(name: str) -> str:
    return re.sub(r'["`\[\]]', '', name).lower()


def _fields_read(column: exp.Column) -> tuple[str, ...]:
    parts: list[str] = []
    node: exp.Expr = column
    while True:
        parent = node.parent
        if (
            isinstance(parent, exp.Dot)
            and parent.this is node
            and isinstance(parent.expression, exp.Identifier)
        ):
            parts.append(parent.expression.name.lower())
        elif isinstance(parent, exp.Bracket) and parent.this is node and _bracket_field(parent) is not None:
            parts.append(str(_bracket_field(parent)).lower())
        elif not (isinstance(parent, exp.Bracket) and parent.this is node and _is_index(parent)):
            return tuple(parts)
        node = parent


def _field_suffixes(expression: exp.Expr, child: LineageNode) -> set[tuple[str, ...]]:
    """The struct fields ``expression`` reads from the column ``child`` stands for: ``{("country",)}``
    for ``o.customer.country``, ``{()}`` when it uses the whole column."""
    wanted = _plain(child.name)
    suffixes: set[tuple[str, ...]] = set()
    for column in expression.find_all(exp.Column):
        name = f'{column.table}.{column.name}' if column.table else column.name
        if _plain(name) != wanted:
            continue
        suffixes.add(_fields_read(column))
    if not suffixes or () in suffixes:
        return {()}
    return suffixes


Leaf = tuple[LineageNode, Transform, str, tuple[str, ...]]


def _leaves(node: LineageNode) -> Iterator[Leaf]:
    """Leaf (physical table) nodes under ``node``: whether the path changes the value, the name of
    the node that reached the leaf (for a ``*`` leaf, the column that was asked for), and the struct
    field path read from the leaf column."""
    stack: list[tuple[LineageNode, bool, frozenset[int], str, tuple[str, ...]]] = [
        (node, True, frozenset(), node.name, ())
    ]
    while stack:
        current, direct, seen, parent, path = stack.pop()
        if id(current) in seen:
            continue
        direct = direct and _is_passthrough(current)
        if not current.downstream:
            if isinstance(current.expression, exp.Table):
                yield current, 'direct' if direct else 'transformed', parent, path
            continue
        # sqlglot collects a node's inputs in a set, whose order follows string hashing and so
        # changes between processes; walking them by name makes the sources, and the edges built
        # from them, come out in the same order on every build.
        for child in sorted(current.downstream, key=lambda c: c.name)[::-1]:
            for suffix in sorted(_field_suffixes(current.expression, child)):
                stack.append((child, direct, seen | {id(current)}, current.name, suffix + path))


def _leaf_source(
    leaf: LineageNode,
    asked_for: str,
    transform: Transform,
    default_catalog: str | None,
    default_db: str | None,
    path: tuple[str, ...] = (),
) -> ColumnSource | None:
    table = leaf.expression
    # A table-valued function (generate_series(...) AS t(x)) has no name and no upstream columns.
    if not isinstance(table, exp.Table) or not table.name:
        return None
    relation = table_key(table, default_catalog, default_db)
    column = leaf.name.rsplit('.', 1)[-1].strip('"`[]').lower()
    if column == '*':
        # A relation whose columns sqlglot could not expand: the column is the one the query
        # asked the star for, which the relation must therefore have.
        column = asked_for.rsplit('.', 1)[-1].strip('"`[]').lower()
        if not column or column == '*':
            return None
    return ColumnSource(relation, '.'.join((column, *path)), transform)


def _unexpanded_star_tables(scope: Scope) -> Iterator[exp.Table]:
    for inner in scope.traverse():
        if not isinstance(inner.expression, exp.Select) or not inner.expression.is_star:
            continue
        for source in inner.sources.values():
            if isinstance(source, exp.Table):
                yield source


_REFERENCE_CLAUSES = ('joins', 'where', 'group', 'having', 'qualify')


_ATOM = re.compile(r'[\w."`$]+')


def _inline(text: str) -> exp.Expr:
    """An already-canonical expression placed inside another, bracketed unless it is a name."""
    return exp.Var(this=text) if _ATOM.fullmatch(text) else exp.Paren(this=exp.Var(this=text))


def _unbracket(text: str) -> str:
    """Drop brackets that enclose the whole expression: ``(a / 100)`` becomes ``a / 100``."""
    while text.startswith('(') and text.endswith(')'):
        depth = 0
        for index, char in enumerate(text):
            depth += char == '('
            depth -= char == ')'
            if depth == 0 and index < len(text) - 1:
                return text
        text = text[1:-1]
    return text


class _Canonical:
    """Column expressions and conditions in terms of physical columns only."""

    def __init__(self, dialect: DialectType, default_catalog: str | None, default_db: str | None) -> None:
        self.dialect = dialect
        self.default_catalog = default_catalog
        self.default_db = default_db
        # Keyed by id(), so each entry keeps its node alive: lineage nodes built for conditions are
        # temporary, and a freed node's id can be reused by the next one.
        self.memo: dict[int, tuple[LineageNode, str]] = {}

    def node(self, node: LineageNode) -> str:
        cached = self.memo.get(id(node))
        if cached is not None and cached[0] is node:
            return cached[1]
        self.memo[id(node)] = (node, '...')  # guards against cycles while this node is expanded
        text = self._expand(node)
        self.memo[id(node)] = (node, text)
        return text

    def _expand(self, node: LineageNode) -> str:
        expression = node.expression
        if isinstance(expression, exp.Table):
            relation = table_key(expression, self.default_catalog, self.default_db)
            return f'{relation}.{_plain(node.name.rsplit(".", 1)[-1])}'
        if isinstance(node.source, exp.SetOperation) and node.downstream:
            branches = sorted(self.node(child) for child in node.downstream)
            return f'{node.name}({", ".join(branches)})'
        if isinstance(expression, exp.Alias):
            expression = expression.this
        if isinstance(expression, exp.Star):
            return ', '.join(self.node(child) for child in node.downstream)
        children = {_plain(child.name): child for child in node.downstream}
        star_tables = [
            c for c in node.downstream if isinstance(c.expression, exp.Table) and c.name.endswith('*')
        ]

        def replace(item: exp.Expr) -> exp.Expr:
            if not isinstance(item, exp.Column):
                return item
            name = f'{item.table}.{item.name}' if item.table else item.name
            child = children.get(_plain(name))
            if child is not None:
                return _inline(self.node(child))
            if len(star_tables) == 1 and isinstance(star_tables[0].expression, exp.Table):
                relation = table_key(star_tables[0].expression, self.default_catalog, self.default_db)
                return exp.Var(this=f'{relation}.{item.name.lower()}')
            return item

        # transform() copies the tree, so the copy it returns can be printed without another one.
        return expression.transform(replace).sql(dialect=self.dialect, copy=False)

    def conditions(self, root: Scope, schema: MappingSchema) -> list[str]:
        found: set[str] = set()
        for scope in root.traverse():
            select = scope.expression
            if not isinstance(select, exp.Select):
                continue
            for key in _REFERENCE_CLAUSES:
                value = select.args.get(key)
                for part in value if isinstance(value, list) else [value]:
                    if not isinstance(part, exp.Expr):
                        continue
                    if isinstance(part, exp.Join):
                        on = part.args.get('on')
                        if not isinstance(on, exp.Expr):
                            continue
                        label = ' '.join(x for x in (part.side, part.kind, 'JOIN ON') if x).upper()
                        part = on
                    else:
                        label = {
                            'where': 'WHERE',
                            'group': 'GROUP BY',
                            'having': 'HAVING',
                            'qualify': 'QUALIFY',
                        }[key]
                        if isinstance(part, (exp.Where, exp.Having, exp.Qualify)):
                            part = part.this
                    found.add(f'{label}: {self._condition(part, scope, schema)}')
        return sorted(found)

    def _condition(self, part: exp.Expr, scope: Scope, schema: MappingSchema) -> str:
        def replace(item: exp.Expr) -> exp.Expr:
            if not isinstance(item, exp.Column):
                return item
            source = scope.sources.get(item.table)
            if isinstance(source, exp.Table):
                return exp.Var(
                    this=f'{table_key(source, self.default_catalog, self.default_db)}.{item.name.lower()}'
                )
            if isinstance(source, Scope):
                try:
                    traced = to_node(item.name, scope=source, dialect=self.dialect, schema=schema)
                except (SqlglotError, ValueError, KeyError, IndexError, AttributeError):
                    return item
                return _inline(self.node(traced))
            return item

        if isinstance(part, exp.Group):
            if part.args.get('all') or not part.expressions:
                return 'ALL'
            return ', '.join(
                sorted(e.transform(replace).sql(dialect=self.dialect, copy=False) for e in part.expressions)
            )
        return part.transform(replace).sql(dialect=self.dialect, copy=False)


def _references(
    root: Scope,
    dialect: DialectType,
    schema: MappingSchema,
    default_catalog: str | None,
    default_db: str | None,
) -> list[tuple[str, str]]:
    """Physical columns used in joins, filters, GROUP BY, HAVING and QUALIFY."""
    found: list[tuple[str, str]] = []

    def add(relation: str, column: str) -> None:
        item = (relation, column.lower())
        if item not in found:
            found.append(item)

    for scope in root.traverse():
        select = scope.expression
        if not isinstance(select, exp.Select):
            continue
        columns: list[exp.Column] = []
        for key in _REFERENCE_CLAUSES:
            value = select.args.get(key)
            for part in value if isinstance(value, list) else [value]:
                if isinstance(part, exp.Expr):
                    columns.extend(part.find_all(exp.Column))
        for column in columns:
            source = scope.sources.get(column.table)
            fields = _fields_read(column)
            if isinstance(source, exp.Table):
                add(table_key(source, default_catalog, default_db), '.'.join((column.name, *fields)))
            elif isinstance(source, Scope):
                try:
                    traced = to_node(column.name, scope=source, dialect=dialect, schema=schema)
                except (SqlglotError, ValueError, KeyError, IndexError, AttributeError):
                    continue
                for leaf, _, asked_for, path in _leaves(traced):
                    leaf_source = _leaf_source(
                        leaf, asked_for, 'direct', default_catalog, default_db, path + fields
                    )
                    if leaf_source:
                        add(leaf_source.relation, leaf_source.column)
    return found
