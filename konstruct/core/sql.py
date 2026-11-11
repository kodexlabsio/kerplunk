"""Column-level lineage for SQL statements, built on sqlglot.

`analyse()` takes one SQL string (one or more statements) and returns, per statement, the
relation it writes (if any), the relations it reads, the physical columns each output column
is derived from, and the columns it only joins, filters or groups on.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
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

from konstruct.core.graph import relation_key

# What parsing SQL can raise: sqlglot's parse functions can fail with TypeError, ValueError,
# AttributeError or RecursionError rather than a SqlglotError, for example on `->` in DuckDB, `#>`
# in Postgres or `{:}` in BigQuery.
PARSE_ERRORS: tuple[type[Exception], ...] = (
    SqlglotError,
    TypeError,
    ValueError,
    AttributeError,
    RecursionError,
)

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
    # The relations read only on the side of an outer join that may find no row of them, with
    # that join's side (LEFT, RIGHT or FULL), and those read before a window function, QUALIFY
    # or LIMIT, with those steps: their own conditions apply there. And the relations whose every
    # column reaches the output through a SELECT * that could not be expanded, with the columns
    # it leaves out.
    nullable: dict[str, str] = field(default_factory=dict)
    read_before: dict[str, list[str]] = field(default_factory=dict)
    star_reads: dict[str, list[str]] = field(default_factory=dict)

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
            'nullable': self.nullable,
            'read_before': self.read_before,
            'star_reads': self.star_reads,
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
            nullable=dict(data.get('nullable', {})),
            read_before={k: list(v) for k, v in data.get('read_before', {}).items()},
            star_reads={k: list(v) for k, v in data.get('star_reads', {}).items()},
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
    except PARSE_ERRORS as error:
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
    # A bind parameter (`from ?`) or a Snowflake stage (`from @stage`) where a relation's name belongs
    # names nothing a schema can hold, and qualify cannot normalise it.
    if any(
        isinstance(part, (exp.Placeholder, exp.Var)) for t in query.find_all(exp.Table) for part in t.parts
    ):
        result.errors.append('column lineage failed: a relation is named by a parameter or a stage')
        return
    # The statement was parsed for this analysis alone and is not read again, so it is qualified in
    # place rather than copied first.
    qualified = qualify(query, **qualify_kwargs)
    scope = build_scope(qualified)
    if scope is None:
        result.errors.append('column lineage failed: not a query')
        return
    for inner in scope.traverse():
        _spell_out_group_by_all(inner.expression)

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
    scopes = _scopes(scope, lambda t: table_key(t, default_catalog, default_db))
    result.nullable, result.read_before, result.star_reads = scopes.nullable, scopes.read_before, scopes.stars
    result.conditions = canon.conditions(scope, schema, scopes)


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


OUTER_SIDES = ('LEFT', 'RIGHT', 'FULL')
"""The sides of an outer join, as ``Join.side`` spells them."""


def in_context(condition: str, side: str | None = None, before: Sequence[str] = ()) -> str:
    """``condition`` labelled with where it applies, so that moving it changes it.

    A WHERE on the nullable side of an outer join (``side``: ``LEFT``, ``RIGHT`` or ``FULL``)
    decides which rows join, not which rows there are: it is a ``LEFT JOIN WHERE``. A condition
    applied before a window function or QUALIFY (``WINDOW``) or a LIMIT or OFFSET (``LIMIT``)
    changes what they see: it lists them, nearest first, as ``WHERE BEFORE WINDOW, LIMIT``.
    ``before`` adds steps further away than those the label lists already."""
    label, sep, rest = condition.partition(': ')
    if not sep:
        return condition
    base, _, earlier = label.partition(' BEFORE ')
    if side and base == 'WHERE':
        base = f'{side} JOIN WHERE'
    steps = [s for s in earlier.split(', ') if s] + list(before)
    return f'{base} BEFORE {", ".join(steps)}: {rest}' if steps else f'{base}: {rest}'


def _steps_after(query: exp.Expr, clause: str) -> list[str]:
    """The steps of ``query`` that see the rows its ``clause`` leaves: its window functions and
    QUALIFY after its joins, filters and grouping, and its LIMIT or OFFSET after everything."""
    args = query.args
    window = isinstance(query, exp.Select) and (
        bool(args.get('qualify')) or any(item.find(exp.Window) for item in query.selects)
    )
    limited = bool(args.get('limit') or args.get('offset'))
    if clause in ('LIMIT', 'OFFSET', 'ORDER BY'):
        return []
    if clause in ('QUALIFY', 'DISTINCT', 'DISTINCT ON'):
        return ['LIMIT'] if limited else []
    return (['WINDOW'] if window else []) + (['LIMIT'] if limited else [])


@dataclass
class _Scopes:
    """Where each scope of a statement, and each table it reads, sits: on the nullable side of
    an outer join, and before which window functions, QUALIFYs and LIMITs."""

    sides: dict[int, str] = field(default_factory=dict)
    """The side of the outer join each scope (by id) is on the nullable side of."""
    before: dict[int, list[str]] = field(default_factory=dict)
    """The steps that see each scope's rows after the scope itself, nearest first."""
    nullable: dict[str, str] = field(default_factory=dict)
    """Tables read only on the nullable side of an outer join, with its side."""
    read_before: dict[str, list[str]] = field(default_factory=dict)
    """Tables read before a window function, QUALIFY or LIMIT, with those steps."""
    folded: dict[int, Scope] = field(default_factory=dict)
    """For a LEFT JOIN (by id) of a CTE or subquery that only selects and filters, that scope:
    its WHERE is part of the join's ON, as it decides the same rows."""
    stars: dict[str, list[str]] = field(default_factory=dict)
    """Tables whose every column reaches the output through a ``SELECT *`` that could not be
    expanded, with the columns the star leaves out."""


def _scopes(root: Scope, key: Callable[[exp.Table], str]) -> _Scopes:
    found = _Scopes()
    scope_reads: dict[int, list[tuple[str | None, list[str]]]] = {}
    table_reads: dict[str, list[tuple[str | None, list[str]]]] = {}
    # Reversed, a scope comes before the scopes it reads.
    for scope in reversed(list(root.traverse())):
        reads = scope_reads.get(id(scope))
        if reads:
            side = reads[0][0] if all(r[0] for r in reads) else None
            before = reads[0][1]
        elif scope.parent is not None:
            # A union's branch, or a subquery in a filter: where its parent's filters are.
            side = found.sides.get(id(scope.parent))
            before = _steps_after(scope.parent.expression, 'WHERE') + found.before.get(id(scope.parent), [])
        else:
            side, before = None, []
        if side:
            found.sides[id(scope)] = side
        found.before[id(scope)] = before
        select = scope.expression
        if not isinstance(select, exp.Select):
            continue
        inner = _steps_after(select, 'WHERE') + before
        nullable = _nullable_names(select, list(scope.selected_sources))
        for name, (_, source) in scope.selected_sources.items():
            read = (side or nullable.get(name), inner)
            if isinstance(source, Scope):
                scope_reads.setdefault(id(source), []).append(read)
            elif isinstance(source, exp.Table):
                table_reads.setdefault(key(source), []).append(read)
        for join in select.args.get('joins') or []:
            joined = scope.selected_sources.get(join.this.alias_or_name)
            target = joined[1] if joined else None
            if (join.side or '').upper() == 'LEFT' and isinstance(target, Scope) and _filters_only(target):
                found.folded[id(join)] = target
    for table, reads in table_reads.items():
        if all(r[0] for r in reads):
            found.nullable[table] = str(reads[0][0])
        if reads[0][1]:
            found.read_before[table] = reads[0][1]
    # A folded scope read more than once is read elsewhere too, where its WHERE is its own.
    for join_id, source in list(found.folded.items()):
        if len(scope_reads.get(id(source), [])) != 1:
            del found.folded[join_id]
    _star_reads(root, key, found.stars)
    return found


def _filters_only(scope: Scope) -> bool:
    """Whether ``scope`` only selects and filters rows, so its WHERE could as well be in the ON
    of a join that reads it."""
    select = scope.expression
    if not isinstance(select, exp.Select) or not isinstance(select.args.get('where'), exp.Where):
        return False
    shaping = ('group', 'having', 'qualify', 'distinct', 'limit', 'offset', 'joins')
    return not any(select.args.get(k) for k in shaping) and not any(
        i.find(exp.Window) for i in select.selects
    )


def _star_reads(scope: Scope, key: Callable[[exp.Table], str], found: dict[str, list[str]]) -> None:
    """The tables whose columns ``scope`` outputs through a ``SELECT *`` or ``t.*`` it could not
    expand, through the CTEs and subqueries the star reads, each with the columns it leaves out."""
    select = scope.expression
    if isinstance(select, exp.SetOperation):
        for branch in scope.set_operation_scopes:
            _star_reads(branch, key, found)
        return
    if not isinstance(select, exp.Select):
        return
    for item in select.selects:
        star = item.this if isinstance(item, exp.Column) else item
        if not isinstance(star, exp.Star):
            continue
        left_out = star.args.get('except_') or star.args.get('except') or []
        excluded = [c.name.lower() for c in left_out if isinstance(c, exp.Column)]
        if isinstance(item, exp.Column) and item.table:
            sources = [scope.selected_sources[item.table][1]] if item.table in scope.selected_sources else []
        else:
            sources = [source for _, source in scope.selected_sources.values()]
        for source in sources:
            if isinstance(source, exp.Table):
                found.setdefault(key(source), excluded)
            elif isinstance(source, Scope):
                _star_reads(source, key, found)


def _nullable_names(select: exp.Select, names: list[str]) -> dict[str, str]:
    """The relations ``select`` reads, by name, that an outer join may find no row of: the one a
    LEFT JOIN joins, those before a RIGHT JOIN, and both sides of a FULL JOIN."""
    joined = [j.this.alias_or_name for j in select.args.get('joins') or []]
    earlier = [n for n in names if n not in joined]
    found: dict[str, str] = {}
    for join, name in zip(select.args.get('joins') or [], joined, strict=True):
        side = (join.side or '').upper()
        if side in ('LEFT', 'FULL'):
            found.setdefault(name, side)
        if side in ('RIGHT', 'FULL'):
            for other in earlier:
                found.setdefault(other, side)
        earlier.append(name)
    return found


def _named(query: exp.Query, term: exp.Expr) -> exp.Expr:
    """``term`` of an ORDER BY or DISTINCT ON with each bare name of a select item, which they may
    use, replaced by that item's expression."""
    if not isinstance(query, exp.Select):
        return term
    items = {i.alias_or_name.lower(): i.unalias() for i in query.selects if not isinstance(i, exp.Star)}

    def replace(node: exp.Expr) -> exp.Expr:
        if isinstance(node, exp.Column) and not node.table and node.name.lower() in items:
            named = items[node.name.lower()]
            return node if named is node or named == node else named.copy()
        return node

    return term.transform(replace)


def _spell_out_group_by_all(select: exp.Expr) -> None:
    """Rewrite ``GROUP BY ALL`` as the select items it groups by, those without an aggregate or a
    window function, so that its columns are references and its condition names them."""
    group = select.args.get('group')
    if not isinstance(select, exp.Select) or not isinstance(group, exp.Group):
        return
    if not group.args.get('all') or group.expressions:
        return
    keys = [s.unalias().copy() for s in select.selects if not s.find(exp.AggFunc, exp.Window)]
    if keys:
        group.set('all', None)
        group.set('expressions', keys)
    else:
        select.set('group', None)  # only aggregates: one row, as with no GROUP BY at all


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

        # The lineage node's expression belongs to the analysed statement, so rather than copying
        # it (the commonest cost of a build), each column is swapped for its replacement, the SQL
        # is printed, and the columns are put back.
        if isinstance(expression, exp.Column):
            return replace(expression).sql(dialect=self.dialect)
        swapped: list[tuple[exp.Expr, exp.Expr]] = []
        try:
            for found in list(expression.find_all(exp.Column)):
                new = replace(found)
                if new is not found:
                    found.replace(new)
                    swapped.append((new, found))
            return expression.sql(dialect=self.dialect, copy=False)
        finally:
            for new, old in reversed(swapped):
                new.replace(old)

    def conditions(self, root: Scope, schema: MappingSchema, scopes: _Scopes) -> list[str]:
        """The statement's joins, filters, grouping, DISTINCT, LIMIT and OFFSET, and the ORDER BY
        a LIMIT or DISTINCT ON picks rows by, each labelled with where it applies (see
        :func:`in_context`)."""
        found: set[str] = set()
        folded = {id(s) for s in scopes.folded.values()}
        for scope in root.traverse():
            query = scope.expression
            side, before = scopes.sides.get(id(scope)), scopes.before.get(id(scope), [])

            def add(
                label: str,
                text: str,
                scope: Scope = scope,
                side: str | None = side,
                before: list[str] = before,
            ) -> None:
                steps = _steps_after(scope.expression, label.split(' JOIN ')[-1])
                found.add(in_context(f'{label}: {text}', side, steps + before))

            if isinstance(query, exp.SetOperation):
                kind = type(query).__name__.upper()
                if query.args.get('distinct') or kind != 'UNION':
                    add(kind, 'DISTINCT' if query.args.get('distinct') else 'ALL')
            if not isinstance(query, exp.Query):
                continue
            # Rows the query makes itself, from a table function or VALUES, which no column of
            # a relation describes: what they are is in the SQL.
            for _, source in scope.selected_sources.values():
                if isinstance(source, exp.Table) and not isinstance(source.this, exp.Identifier):
                    add('ROWS FROM', source.this.sql(dialect=self.dialect))
                elif isinstance(source, Scope) and isinstance(source.expression, exp.Values):
                    values = source.expression.copy()
                    values.set('alias', None)  # what it is called is not what it holds
                    add('ROWS FROM', _unbracket(values.sql(dialect=self.dialect)))
            for label, terms, ordered in self._clauses(query, scope, schema, scopes, folded):
                add(label, ', '.join(terms if ordered else sorted(terms)))
        return sorted(found)

    def _clauses(
        self, query: exp.Query, scope: Scope, schema: MappingSchema, scopes: _Scopes, folded: set[int]
    ) -> Iterator[tuple[str, list[str], bool]]:
        """Each clause of ``query`` that decides its rows, as (label, terms, whether their order
        matters)."""
        args = query.args
        for join in args.get('joins') or []:
            on = join.args.get('on')
            if not isinstance(on, exp.Expr):
                continue
            label = ' '.join(x for x in (join.side, join.kind, 'JOIN ON') if x).upper()
            text = self._condition(on, scope, schema)
            inner = scopes.folded.get(id(join))
            if inner is not None:
                where = self._condition(inner.expression.args['where'].this, inner, schema)
                text = ' AND '.join(f'({t})' if re.search(r'\bOR\b', t, re.I) else t for t in (text, where))
            yield label, [text], False
        for key, label in (('where', 'WHERE'), ('having', 'HAVING'), ('qualify', 'QUALIFY')):
            clause = args.get(key)
            if isinstance(clause, exp.Expr) and not (key == 'where' and id(scope) in folded):
                yield label, [self._condition(clause.this, scope, schema)], False
        group = args.get('group')
        if isinstance(group, exp.Group) and group.expressions:
            yield 'GROUP BY', [self._condition(e, scope, schema) for e in group.expressions], False
        distinct = args.get('distinct')
        if isinstance(distinct, exp.Distinct) and isinstance(query, exp.Select):
            on = distinct.args.get('on')
            if isinstance(on, exp.Expr):
                keys = list(on.expressions) if isinstance(on, exp.Tuple) else [on]
                yield 'DISTINCT ON', [self._condition(_named(query, k), scope, schema) for k in keys], False
            else:
                yield 'DISTINCT', [self._condition(s.unalias(), scope, schema) for s in query.selects], False
        limit, offset = args.get('limit'), args.get('offset')
        if isinstance(limit, exp.Limit) and isinstance(limit.expression, exp.Expr):
            yield 'LIMIT', [limit.expression.sql(dialect=self.dialect)], False
        elif isinstance(limit, exp.Fetch):
            count = limit.args.get('count')
            yield 'LIMIT', [count.sql(dialect=self.dialect) if isinstance(count, exp.Expr) else '1'], False
        if isinstance(offset, exp.Offset) and isinstance(offset.expression, exp.Expr):
            yield 'OFFSET', [offset.expression.sql(dialect=self.dialect)], False
        order = args.get('order')
        picks = limit or offset or (isinstance(distinct, exp.Distinct) and distinct.args.get('on'))
        if isinstance(order, exp.Order) and picks:
            yield (
                'ORDER BY',
                [self._condition(_named(query, o), scope, schema) for o in order.expressions],
                True,
            )

    def _condition(self, part: exp.Expr, scope: Scope, schema: MappingSchema) -> str:
        # A bare name in a query that reads one relation is that relation's: qualify leaves it
        # bare when the relation's columns are unknown, and then the filters of a union's
        # branches, each over its own relation, would read the same.
        only = list(scope.selected_sources.values())
        single = only[0][1] if len(only) == 1 else None

        def replace(item: exp.Expr) -> exp.Expr:
            if not isinstance(item, exp.Column):
                return item
            source = scope.sources.get(item.table) if item.table else single
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
