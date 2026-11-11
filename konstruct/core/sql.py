"""Column-level lineage for SQL statements, built on sqlglot.

`analyse()` takes one SQL string (one or more statements) and returns, per statement, the
relation it writes (if any), the relations it reads, the physical columns each output column
is derived from, and the columns it only joins, filters or groups on.
"""

from __future__ import annotations

import datetime
import functools
import re
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, NamedTuple

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect, DialectType, NormalizationStrategy
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

STAR_UNKNOWN = 'SELECT * over relations with unknown columns'
LINEAGE_FAILED = 'column lineage failed'
# Marks a select item added so that a star passes a column on by name.
PASSED_ON = 'konstruct_passed_on'
PARTIAL_COLUMNS = (STAR_UNKNOWN, LINEAGE_FAILED)
"""How the errors of a statement whose columns may not be all its relation has begin."""

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


_AGGREGATE_SQL = {
    'sum': 'SUM({})',
    'sum_boolean': 'SUM(CASE WHEN {} THEN 1 ELSE 0 END)',
    'count': 'COUNT({})',
    'count_distinct': 'COUNT(DISTINCT {})',
    'average': 'AVG({})',
    'min': 'MIN({})',
    'max': 'MAX({})',
    'median': 'MEDIAN({})',
}


def aggregate_sql(kind: str, expression: str) -> str | None:
    """A semantic layer's aggregation (``sum``, ``count_distinct``, ``average``, as dbt's semantic
    models and LookML name them) applied to ``expression``, as SQL; None for one that SQL cannot
    state without more than the expression, such as a percentile or a sum over distinct keys."""
    template = _AGGREGATE_SQL.get(kind)
    return template.format(expression) if template else None


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
    """A sqlglot schema with every relation at catalog.db.table depth.

    Its names are matched whatever their case, quoted or not, as Konstruct matches identifiers: a
    build lists them in lower case, while qualify folds Snowflake's unquoted names to upper case and
    keeps a quoted name as written. The names are put in lower case here, once, rather than
    normalised by sqlglot, which costs as much as the rest of an analysis on a large schema; the
    query's are put in lower case by the same case-insensitive dialect when it is qualified.
    """
    nested: dict[str, Any] = {}
    for key, cols in columns.items():
        parts = key.split('.')
        if len(parts) == 2:
            parts = ['', *parts]
        elif len(parts) == 1:
            parts = ['', '', *parts]
        if len(parts) != 3 or not cols:
            continue
        catalog, db, table = (part.lower() for part in parts)
        nested.setdefault(catalog, {}).setdefault(db, {})[table] = {
            name.lower(): _safe_type(dtype, dialect) for name, dtype in cols.items()
        }
    return MappingSchema(nested, dialect=case_insensitive(dialect), normalize=False)


_CASE_INSENSITIVE: dict[type[Dialect], Dialect] = {}


def case_insensitive(dialect: DialectType) -> Dialect:
    """``dialect`` with names that match in any case, made once per dialect."""
    resolved = Dialect.get_or_raise(dialect)
    found = _CASE_INSENSITIVE.get(type(resolved))
    if found is None:
        found = type(resolved)(normalization_strategy=NormalizationStrategy.CASE_INSENSITIVE)
        _CASE_INSENSITIVE[type(resolved)] = found
    return found


# Every analysis builds the schema of the whole project, so each type would be parsed once per
# column per statement; a project has few distinct types.
@functools.lru_cache(maxsize=65536)
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


def relations_named(
    sql: str, *, dialect: str | None = None, default_catalog: str | None = None, default_db: str | None = None
) -> list[str]:
    """The keys of the relations ``sql`` names, as :func:`analyse` reads them, or none if it does
    not parse. Only these relations' columns can change what the analysis finds."""
    try:
        statements = sqlglot.parse(sql, dialect=Dialect.get_or_raise(dialect))
    except PARSE_ERRORS:
        return []
    return sorted(
        {
            table_key(table, default_catalog, default_db)
            for statement in statements
            if statement is not None
            for table in statement.find_all(exp.Table)
            if table.name
        }
    )


def analyse(
    sql: str,
    *,
    dialect: str | None = None,
    schema: SchemaColumns | None = None,
    default_catalog: str | None = None,
    default_db: str | None = None,
    target: str | None = None,
    guessed: Collection[str] = (),
) -> list[StatementLineage]:
    """Analyse every statement in ``sql``.

    ``target`` names the relation a bare SELECT writes (a dbt model's relation). Relations
    created by earlier statements become part of the schema for later ones. ``guessed`` names the
    relations of ``schema`` whose columns are only those other queries were seen to read: they place
    a column, but are not taken to be all the columns the relation has.
    """
    known: dict[str, dict[str, str]] = {k: dict(v) for k, v in (schema or {}).items()}
    partial = {key.lower() for key in guessed}
    # One dialect object for the whole analysis: sqlglot otherwise builds a new one for every parse
    # and every expression it prints.
    resolved = Dialect.get_or_raise(dialect)
    try:
        statements = [s for s in sqlglot.parse(sql, dialect=resolved) if s is not None]
    except PARSE_ERRORS as error:
        return [StatementLineage(kind='unparsed', target=target, errors=[_first_line(error)])]

    results: list[StatementLineage] = []
    for statement in statements:
        result = _analyse_statement(statement, resolved, known, partial, default_catalog, default_db, target)
        if result is None:
            continue
        if result.target and result.columns and (result.target not in known or result.target in partial):
            known[result.target] = dict.fromkeys(result.columns, 'UNKNOWN')
            partial.discard(result.target)
        results.append(result)
    return results


def _first_line(error: Exception) -> str:
    text = str(error).strip()
    return text.splitlines()[0] if text else type(error).__name__


def _analyse_statement(
    statement: exp.Expr,
    dialect: DialectType,
    known: SchemaColumns,
    partial: Collection[str],
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
        _column_lineage(query, dialect, known, partial, default_catalog, default_db, out_names, result)
    except (SqlglotError, ValueError, KeyError, IndexError, AttributeError, RecursionError) as error:
        result.errors.append(f'{LINEAGE_FAILED}: {_first_line(error)}')
    return result


def _column_lineage(
    query: exp.Expr,
    dialect: DialectType,
    known: SchemaColumns,
    partial: Collection[str],
    default_catalog: str | None,
    default_db: str | None,
    out_names: list[str] | None,
    result: StatementLineage,
) -> None:
    # Names match whatever their case, as in the schema: Snowflake would fold the query's unquoted
    # names to upper case and keep its quoted ones as written.
    dialect = case_insensitive(dialect)
    qualify_kwargs: dict[str, Any] = {
        'dialect': dialect,
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
        result.errors.append(f'{LINEAGE_FAILED}: a relation is named by a parameter or a stage')
        return
    # A relation whose columns are only guessed may have others, so naming treats them as unknown.
    known_columns = {
        key.lower(): {c.lower() for c in columns}
        for key, columns in known.items()
        if columns and key.lower() not in partial
    }
    named: dict[str, set[str]] = {}
    # Relations whose columns are only guessed that a star expands over: what it selects may not be
    # all there is.
    guessed_stars: set[str] = set()
    unlisted: set[str] = set()
    if any(read not in known_columns for read in result.reads):
        _name_unknown_columns(
            query,
            Dialect.get_or_raise(dialect),
            known_columns,
            default_catalog,
            default_db,
            {key.lower() for key, columns in known.items() if columns},
            named,
            guessed_stars,
            unlisted,
        )
    # The columns of a relation whose columns are only guessed are those, and those the query was
    # just found to name: the schema places them and expands a star to them, and none is missing.
    widened = {
        key: {**columns, **dict.fromkeys(sorted(named.get(key.lower(), set()) - set(columns)), 'UNKNOWN')}
        if key.lower() in partial
        else columns
        for key, columns in known.items()
        if key.lower() not in unlisted
    }
    schema = build_schema(widened, dialect)
    qualify_kwargs['schema'] = schema
    # The statement was parsed for this analysis alone and is not read again, so it is qualified in
    # place rather than copied first.
    try:
        qualified = qualify(query, **qualify_kwargs)
    except AssertionError as error:
        # An internal check of qualify, as on a position over a star it cannot expand.
        result.errors.append(f'{LINEAGE_FAILED}: {_first_line(error)}')
        return
    scope = build_scope(qualified)
    if scope is None:
        result.errors.append(f'{LINEAGE_FAILED}: not a query')
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
    star_tables += guessed_stars
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
        result.errors.append(f'{STAR_UNKNOWN}: ' + ', '.join(sorted(set(star_tables))))

    tracer = _Tracer(dialect)
    result.references = _references(
        scope, _Sources(scope), tracer, dialect, schema, default_catalog, default_db
    )
    canon = Canonical(dialect, default_catalog, default_db)
    canon.tracer = tracer
    for index, (name, node) in enumerate(nodes.items()):
        if name != '*':
            out_name = (out_names[index] if out_names else name).lower()
            result.expressions[out_name] = _unbracket(canon.node(node))
    scopes = _scopes(scope, lambda t: table_key(t, default_catalog, default_db))
    result.nullable, result.read_before, result.star_reads = scopes.nullable, scopes.read_before, scopes.stars
    result.conditions = canon.conditions(scope, schema, scopes)


def _name_unknown_columns(
    query: exp.Expr,
    dialect: Dialect,
    known: Mapping[str, set[str]],
    default_catalog: str | None,
    default_db: str | None,
    listed: Collection[str],
    named: dict[str, set[str]],
    guessed_stars: set[str],
    unlisted: set[str],
) -> None:
    """Name, before qualify, the columns a query reads from relations whose columns are unknown.

    sqlglot leaves an unqualified column unqualified when its scope's one relation has unknown
    columns: lineage then cannot place it once CTEs are in view, it is missing from the references,
    and in a subquery, where it could as well belong to the outer query, qualify refuses the whole
    statement. Such a column is qualified with that relation, as SQL looks a name up in the innermost
    scope first. And lineage cannot follow a column through a ``SELECT *`` it cannot expand, so a CTE
    or subquery whose star passes on a column another scope reads also selects that column by name.
    ``listed`` are the relations the schema lists columns for, ``known`` those of them whose list is
    complete. Each column it names in a relation is added to ``named``, by the relation's key, and each
    relation of those not complete that a star selects from to ``guessed_stars``.
    """

    def record(source: exp.Table | Scope, column: str) -> None:
        if isinstance(source, exp.Table):
            named.setdefault(table_key(source, default_catalog, default_db), set()).add(column.lower())

    # What each scope is known to have does not change during the pass: a column passed on joins a
    # scope only while that scope may have columns it does not list. Without the memo every star
    # in a chain of CTEs looks through all the stars below it, again for every column.
    memo: dict[int, tuple[Scope, set[str] | None]] = {}
    expandable_memo: dict[int, tuple[Scope, bool]] = {}
    selected_memo: dict[int, tuple[Scope, set[str]]] = {}
    stars_memo: dict[int, tuple[Scope, list[tuple[exp.Expr, exp.Table | Scope]], set[str]]] = {}

    def stars(scope: Scope) -> tuple[list[tuple[exp.Expr, exp.Table | Scope]], set[str]]:
        """What the stars of ``scope`` pass on, and the names they leave out, replace or rename.
        The select list grows as columns are passed on, but its stars do not change."""
        cached = stars_memo.get(id(scope))
        if cached is None or cached[0] is not scope:
            changed: set[str] = set()
            if isinstance(scope.expression, exp.Select):
                for item in scope.expression.selects:
                    star = item.this if isinstance(item, exp.Column) else item
                    if isinstance(star, exp.Star):
                        changed |= _changed_by(star)
            cached = stars_memo[id(scope)] = (scope, _star_sources(scope), changed)
        return cached[1], cached[2]

    def selected(scope: Scope) -> set[str]:
        """The names ``scope`` selects, kept up to date as columns are passed on to it: worked out
        again for every column, they made a chain of stars cost the square of its length."""
        cached = selected_memo.get(id(scope))
        if cached is None or cached[0] is not scope:
            query = scope.expression
            names = {n.lower() for n in query.named_selects} if isinstance(query, exp.Select) else set()
            cached = selected_memo[id(scope)] = (scope, names)
        return cached[1]

    def columns(source: exp.Table | Scope) -> set[str] | None:
        """The columns ``source`` is known to have, or None when it may have others."""
        if isinstance(source, exp.Table):
            if not isinstance(source.this, exp.Identifier):
                return set()  # a table function such as read_parquet(...), which Konstruct cannot read
            return known.get(table_key(source, default_catalog, default_db))
        cached = memo.get(id(source))
        if cached is not None and cached[0] is source:
            return cached[1]
        found = scope_columns(source)
        memo[id(source)] = (source, found)
        return found

    def scope_columns(source: Scope) -> set[str] | None:
        query = source.expression
        if isinstance(query, exp.SetOperation):
            branches = [columns(branch) for branch in source.set_operation_scopes]
            return None if None in branches else branches[0]
        if not isinstance(query, exp.Select):
            return set()
        found = {item.alias_or_name.lower() for item in query.selects if not _is_star(item)}
        for _, inner in stars(source)[0]:
            inner_columns = columns(inner)
            if inner_columns is None:
                return None
            found |= inner_columns
        return found

    def supplier(scope: Scope, name: str) -> tuple[exp.Identifier, exp.Table | Scope] | None:
        """The one relation the ``SELECT *`` in ``scope`` can pass the column ``name`` on from."""
        if not isinstance(scope.expression, exp.Select):
            return None
        found = candidates(scope, name)
        identifier = _source_name(found[0][0]) if len(found) == 1 else None
        return (identifier, found[0][1]) if identifier is not None else None

    def candidates(scope: Scope, name: str) -> list[tuple[exp.Expr, exp.Table | Scope]]:
        """The relations the ``SELECT *`` in ``scope`` can pass the column ``name`` on from."""
        if not isinstance(scope.expression, exp.Select):
            return []
        sources, changed = stars(scope)
        if name in changed:
            return []  # a star that leaves out, replaces or renames the column
        return [
            (node, source) for node, source in sources if (inner := columns(source)) is None or name in inner
        ]

    def expandable(source: exp.Table | Scope) -> bool:
        """Whether qualify can expand every star ``source`` passes on: each reaches a relation the
        schema lists, if only by the columns seen read."""
        if isinstance(source, exp.Table):
            return not isinstance(source.this, exp.Identifier) or (
                table_key(source, default_catalog, default_db) in listed
            )
        cached = expandable_memo.get(id(source))
        if cached is not None and cached[0] is source:
            return cached[1]
        query = source.expression
        if isinstance(query, exp.SetOperation):
            found = all(expandable(branch) for branch in source.set_operation_scopes)
        else:
            found = all(expandable(inner) for _, inner in stars(source)[0])
        expandable_memo[id(source)] = (source, found)
        return found

    def pass_on(scope: Scope, column: exp.Column, add: bool = True) -> None:
        """Have ``scope``, whose ``SELECT *`` lineage cannot expand, select ``column`` by name. Where
        qualify can expand the stars (``add`` false), the column is only recorded for the relation
        that supplies it, which then lists it: selecting it as well would name it twice."""
        query = scope.expression
        name = column.name.lower()
        alias = query.parent.args.get('alias') if query.parent else None
        if columns(scope) is not None or (isinstance(alias, exp.TableAlias) and alias.columns):
            return  # known columns, which qualify expands, or columns renamed by position
        add = add and not expandable(scope)
        if isinstance(query, exp.Select):
            if name in selected(scope):
                return
            branches = [scope]
        elif isinstance(query, exp.SetOperation):
            branches = scope.set_operation_scopes
        else:
            return
        found = [(branch, supplier(branch, name)) for branch in branches]
        if add and any(supplied is None for _, supplied in found):
            return
        for branch, supplied in found:
            if supplied is None:
                if not add and len(may := candidates(branch, name)) > 1:
                    # More than one relation whose columns are only guessed may have it: expanded to
                    # what is listed, the star would have it from none, so it stays a star.
                    for _, source in may:
                        if isinstance(source, exp.Table):
                            unlisted.add(table_key(source, default_catalog, default_db))
                continue
            identifier, source = supplied
            if add:
                # Built rather than copied: copying an expression is the commonest cost of a build.
                new = exp.Alias(
                    this=exp.Column(this=_renamed(column.this), table=_renamed(identifier)),
                    alias=_renamed(column.this),
                )
                new.meta[PASSED_ON] = True
                # First, so that a union's branches stay aligned and lineage finds it before the star.
                branch.expression.set('expressions', [new, *branch.expression.expressions])
                selected(branch).add(name)
            if isinstance(source, Scope):
                pass_on(source, column, add)
            else:
                record(source, column.name)

    root = build_scope(query)
    scopes = list(root.traverse()) if root is not None else []
    # A position over a star that qualify cannot expand names no column it can find, and qualify
    # fails on it. It stays the position, which the condition records as it is, and no longer
    # moves when a column is selected before the star.
    for scope in scopes:
        if not expandable(scope):
            for position in _positions(scope.expression):
                position.replace(exp.Var(this=position.sql()))
    for scope in scopes:
        select = scope.expression
        if not isinstance(select, exp.Select):
            continue
        # The scope's own columns, wherever they are in it: sqlglot leaves those in QUALIFY, and in
        # an ORDER BY that names a select item, out of the scope's columns.
        own = _own_columns(select)
        for _, source in _star_sources(scope):
            if isinstance(source, exp.Table) and isinstance(source.this, exp.Identifier):
                key = table_key(source, default_catalog, default_db)
                if key in listed and key not in known:
                    guessed_stars.add(key)
        if len(scope.selected_sources) == 1:
            [(name, (node, source))] = scope.selected_sources.items()
            identifier = _source_name(node)
            if identifier is not None and columns(source) is None:
                # A select alias that WHERE or GROUP BY refers to, a pseudocolumn such as Oracle's
                # ROWNUM and a relation read as a record (BigQuery's `select t from t`) are not
                # columns of the relation.
                others = {name.lower()} | {p.lower() for p in dialect.PSEUDOCOLUMNS}
                others |= {
                    item.alias.lower()
                    for item in select.selects
                    if isinstance(item, exp.Alias)
                    and not (isinstance(item.this, exp.Column) and item.this.name == item.alias)
                }
                for column in own:
                    if not column.table and column.name.lower() not in others:
                        column.set('table', identifier.copy())
                        record(source, column.name)
        for column in own:
            # The relation a column names is this scope's, or for a correlated reference, that of a
            # query around it.
            around: Scope | None = scope
            read = None
            while around is not None and read is None and column.table:
                read = around.sources.get(column.table)
                around = around.parent
            if isinstance(read, Scope):
                pass_on(read, column)
            elif isinstance(read, exp.Table):
                record(read, column.name)


def _own_columns(select: exp.Select) -> list[exp.Column]:
    """The columns of ``select`` itself, outside the queries within it and the names a star leaves
    out."""

    def inside(node: exp.Expr) -> bool:
        return node is not select and isinstance(node, (exp.Query, exp.Star))

    return [node for node in select.walk(prune=inside) if isinstance(node, exp.Column)]


def _renamed(identifier: exp.Identifier) -> exp.Identifier:
    """A new identifier spelled as ``identifier`` is."""
    return exp.Identifier(this=identifier.this, quoted=identifier.args.get('quoted'))


def _positions(query: exp.Expr) -> list[exp.Literal]:
    """The positions of select items by which ``query`` orders, groups or picks distinct rows."""
    clauses = [query.args.get('order'), query.args.get('group')]
    distinct = query.args.get('distinct')
    if isinstance(distinct, exp.Distinct):
        clauses.append(distinct.args.get('on'))
    found = []
    for clause in clauses:
        if not isinstance(clause, exp.Expr):
            continue
        terms = clause.expressions if isinstance(clause, (exp.Order, exp.Group, exp.Tuple)) else [clause]
        for term in terms:
            if isinstance(term, exp.Ordered):
                term = term.this
            if isinstance(term, exp.Literal) and not term.is_string:
                found.append(term)
    return found


def _renames(query: exp.Expr) -> bool:
    """Whether a star in ``query`` replaces or renames columns."""
    return isinstance(query, exp.Select) and any(
        isinstance(star := item.this if isinstance(item, exp.Column) else item, exp.Star)
        and bool(star.args.get('replace') or star.args.get('rename'))
        for item in query.selects
    )


def _star_changes(query: exp.Expr) -> tuple[dict[str, str], set[str]]:
    """What the stars of ``query`` rename (new name to old) and replace."""
    renamed: dict[str, str] = {}
    replaced: set[str] = set()
    for item in query.selects if isinstance(query, exp.Select) else []:
        star = item.this if isinstance(item, exp.Column) else item
        if not isinstance(star, exp.Star):
            continue
        for alias in star.args.get('rename') or []:
            if isinstance(alias, exp.Alias) and isinstance(alias.this, exp.Column):
                renamed[alias.alias.lower()] = alias.this.name.lower()
        for alias in star.args.get('replace') or []:
            if isinstance(alias, exp.Alias):
                replaced.add(alias.alias.lower())
    return renamed, replaced


def _changed_by(star: exp.Star) -> set[str]:
    """The columns a star leaves out (EXCEPT, EXCLUDE), replaces (REPLACE) or renames, by either name."""
    names: set[str] = set()
    for key in ('except_', 'replace', 'rename'):
        for item in star.args.get(key) or []:
            if isinstance(item, exp.Alias):
                names.add(item.alias.lower())
                item = item.this
            if isinstance(item, exp.Column):
                names.add(item.name.lower())
    return names


def _source_name(node: exp.Expr) -> exp.Identifier | None:
    """The name a query gives the relation it reads in FROM or JOIN: its alias, or the table's name."""
    if isinstance(node, exp.Query) and isinstance(node.parent, exp.Subquery):
        node = node.parent  # a subquery in FROM, whose alias is on its brackets
    alias = node.args.get('alias')
    name = alias.this if isinstance(alias, exp.TableAlias) else node.this
    return name if isinstance(name, exp.Identifier) else None


def _is_star(item: exp.Expr) -> bool:
    return isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star))


def _star_sources(scope: Scope) -> list[tuple[exp.Expr, exp.Table | Scope]]:
    """The relations, with the node that reads each, whose every column a ``SELECT *`` or ``t.*`` in
    ``scope`` passes on."""
    select = scope.expression
    if not isinstance(select, exp.Select):
        return []
    found: list[tuple[exp.Expr, exp.Table | Scope]] = []
    for item in select.selects:
        if isinstance(item, exp.Star):
            found.extend(scope.selected_sources.values())
        elif isinstance(item, exp.Column) and _is_star(item) and item.table in scope.selected_sources:
            found.append(scope.selected_sources[item.table])
    return found


class _Sources:
    """The relation each column of a query reads, including the columns of subqueries in its filters."""

    def __init__(self, root: Scope) -> None:
        self.root = root
        self.owners: dict[int, Scope] | None = None

    def of(self, column: exp.Column, scope: Scope) -> exp.Table | Scope | None:
        """The source ``column`` names: one of ``scope``'s, or, for a column inside one of its
        subqueries, one of that subquery's or of a query around it (a correlated reference)."""
        source = scope.sources.get(column.table)
        if source is not None or not column.table:
            return source
        if self.owners is None:
            self.owners = {}
            for inner in self.root.traverse():
                for found in inner.columns:
                    self.owners.setdefault(id(found), inner)
        owner = self.owners.get(id(column))
        while owner is not None:
            source = owner.sources.get(column.table)
            if source is not None:
                return source
            owner = owner.parent
        return None


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
        name = current.name
        if isinstance(current.expression, exp.Star) and _renames(current.source):
            # A star that renames or replaces columns: the relation's column is the one renamed, and a
            # replaced column is computed.
            asked = _plain(name.rsplit('.', 1)[-1])
            renamed, replaced = _star_changes(current.source)
            name = renamed.get(asked, name)
            direct = direct and asked not in replaced
        for child in sorted(current.downstream, key=lambda c: c.name)[::-1]:
            for suffix in sorted(_field_suffixes(current.expression, child)):
                stack.append((child, direct, seen | {id(current)}, name, suffix + path))


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


class _Printed(NamedTuple):
    """A term of a condition printed as SQL as it is, rather than in terms of physical columns."""

    expression: exp.Expr
    unbracket: bool = False


class _Clause(NamedTuple):
    """A clause that decides which rows a query returns."""

    label: str | None
    """How its condition is labelled, such as ``LEFT JOIN ON``; None if it records no condition."""
    terms: list[exp.Expr | _Printed | str]
    """What its condition says, in order: an expression over the query's columns, SQL printed as it
    is, or text."""
    parts: list[exp.Expr]
    """The parts of the query whose columns are references."""
    ordered: bool = False
    """Whether the order of the terms matters (ORDER BY), or they are sorted (GROUP BY)."""
    union: exp.SetOperation | None = None
    """For an ORDER BY of a set operation, the set operation, whose branches its terms name."""
    join: exp.Join | None = None
    """The join, for a join's ON."""


def _row_clauses(query: exp.Query, scope: Scope, folded: Collection[int] = ()) -> Iterator[_Clause]:
    """The joins, filters and grouping of ``query``, its DISTINCT and DISTINCT ON, LIMIT and OFFSET,
    the ORDER BY those pick rows by, the rows a set operation that removes duplicates compares, and
    the rows it makes from a table function or VALUES. One walk gives both the conditions and the
    references; the WHERE of a scope in ``folded`` (by id) is part of a join's ON instead."""
    args = query.args
    for join in args.get('joins') or []:
        on = join.args.get('on')
        label = ' '.join(x for x in (join.side, join.kind, 'JOIN ON') if x).upper()
        # The columns an UNNEST, FLATTEN or table function joined expands decide the rows too; a
        # joined subquery's own columns belong to its scope.
        expanded = [
            n for n in join.this.walk(prune=lambda n: isinstance(n, exp.Query)) if isinstance(n, exp.Column)
        ]
        matched = [on] if isinstance(on, exp.Expr) else []
        yield _Clause(
            label if matched else None,
            list(matched),
            [*matched, *(join.args.get('using') or []), *expanded],
            join=join,
        )
    for key, label in (('where', 'WHERE'), ('having', 'HAVING'), ('qualify', 'QUALIFY')):
        clause = args.get(key)
        if isinstance(clause, exp.Expr):
            here = not (key == 'where' and id(scope) in folded)
            yield _Clause(label if here else None, [clause.this], [clause])
    group = args.get('group')
    if isinstance(group, exp.Group) and group.expressions:
        yield _Clause('GROUP BY', list(group.expressions), [group])
    distinct = args.get('distinct')
    on = distinct.args.get('on') if isinstance(distinct, exp.Distinct) else None
    if isinstance(distinct, exp.Distinct) and isinstance(query, exp.Select):
        if isinstance(on, exp.Expr):
            keys = [_named(query, k) for k in (on.expressions if isinstance(on, exp.Tuple) else [on])]
            yield _Clause('DISTINCT ON', list(keys), list(keys))
        else:
            items = [s.unalias() for s in query.selects if not s.meta.get(PASSED_ON)]
            yield _Clause('DISTINCT', list(items), _selected(query))
    limit, offset = args.get('limit'), args.get('offset')
    if isinstance(limit, exp.Limit) and isinstance(limit.expression, exp.Expr):
        yield _Clause('LIMIT', [_Printed(limit.expression)], [])
    elif isinstance(limit, exp.Fetch):
        count = limit.args.get('count')
        yield _Clause('LIMIT', [_Printed(count) if isinstance(count, exp.Expr) else '1'], [])
    if isinstance(offset, exp.Offset) and isinstance(offset.expression, exp.Expr):
        yield _Clause('OFFSET', [_Printed(offset.expression)], [])
    order = args.get('order')
    if isinstance(order, exp.Order) and (limit or offset or isinstance(on, exp.Expr)):
        terms = list(order.expressions)
        if isinstance(query, exp.SetOperation):
            items = [
                item for term in terms for items in _branch_items(query, term).values() for item in items
            ]
            yield _Clause('ORDER BY', list(terms), items, ordered=True, union=query)
        else:
            named = [_named(query, term) for term in terms]
            yield _Clause('ORDER BY', list(named), list(named), ordered=True)
    if isinstance(query, exp.SetOperation):
        kind = type(query).__name__.upper()
        if query.args.get('distinct') or kind != 'UNION':
            items = [item for select in _branches(query) for item in _selected(select)]
            yield _Clause(kind, ['DISTINCT' if query.args.get('distinct') else 'ALL'], items)
    # Rows the query makes itself, from a table function or VALUES, which no column of a relation
    # describes: what they are is in the SQL.
    for _, source in scope.selected_sources.values():
        if isinstance(source, exp.Table) and not isinstance(source.this, exp.Identifier):
            yield _Clause('ROWS FROM', [_Printed(source.this)], [])
        elif isinstance(source, Scope) and isinstance(source.expression, exp.Values):
            values = source.expression.copy()
            values.set('alias', None)  # what it is called is not what it holds
            yield _Clause('ROWS FROM', [_Printed(values, unbracket=True)], [])


def _selected(select: exp.Select) -> list[exp.Expr]:
    """The items of ``select`` that name columns, leaving out stars and the columns a star was made to
    pass on by name, which depend on what other queries read from it."""
    return [item.unalias() for item in select.selects if not _is_star(item) and not item.meta.get(PASSED_ON)]


def _branches(query: exp.Expr) -> Iterator[exp.Select]:
    """The SELECTs a set operation combines, through nested set operations and brackets."""
    while isinstance(query, exp.Subquery):
        query = query.this
    if isinstance(query, exp.SetOperation):
        yield from _branches(query.this)
        yield from _branches(query.expression)
    elif isinstance(query, exp.Select):
        yield query


def _branch_items(query: exp.SetOperation, term: exp.Expr) -> dict[str, list[exp.Expr]]:
    """The items of every branch of ``query`` that an ORDER BY ``term`` orders by, by the name or
    position it uses for them: a name or position is an item of the first branch, and so the item
    at the same place in every branch. ``a + b`` uses two names; a position is the whole term."""
    key = term.this if isinstance(term, exp.Ordered) else term
    branches = list(_branches(query))
    names = [name.lower() for name in branches[0].named_selects] if branches else []

    def at(index: int) -> list[exp.Expr]:
        return [b.selects[index].unalias() for b in branches if 0 <= index < len(b.selects)]

    if isinstance(key, exp.Literal) and not key.is_string and str(key.this).isdigit():
        return {key.sql(): at(int(str(key.this)) - 1)}
    found: dict[str, list[exp.Expr]] = {}
    for column in [key] if isinstance(key, exp.Column) else list(key.find_all(exp.Column)):
        if not column.table and column.name.lower() in names:
            found[column.name.lower()] = at(names.index(column.name.lower()))
    return found


_ATOM = re.compile(r'[\w."`$]+(?:\.\*)?')


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
    """Rewrite ``GROUP BY ALL`` as the select items it groups by, those without an aggregate, a
    window function or a function sqlglot does not know, so that its columns are references and its
    condition names them."""
    group = select.args.get('group')
    if not isinstance(select, exp.Select) or not isinstance(group, exp.Group):
        return
    if not group.args.get('all') or group.expressions:
        return
    # A function sqlglot does not know (a user-defined aggregate, or one such as DuckDB's product)
    # could aggregate, so an item that calls one is left out rather than claimed as a key.
    keys = [s.unalias().copy() for s in select.selects if not s.find(exp.AggFunc, exp.Window, exp.Anonymous)]
    if keys:
        group.set('all', None)
        group.set('expressions', keys)
    else:
        select.set('group', None)  # only aggregates: one row, as with no GROUP BY at all


class Canonical:
    """Column expressions and conditions in terms of physical columns only: CTEs, subqueries and
    aliases inlined, each column named by its relation key, as the graph holds them."""

    def __init__(self, dialect: DialectType, default_catalog: str | None, default_db: str | None) -> None:
        self.dialect = dialect
        self.default_catalog = default_catalog
        self.default_db = default_db
        # Keyed by id(), so each entry keeps its node alive: lineage nodes built for conditions are
        # temporary, and a freed node's id can be reused by the next one.
        self.memo: dict[int, tuple[LineageNode, str]] = {}
        self.sources: _Sources | None = None
        self.scopes: dict[int, Scope] = {}
        self.tracer = _Tracer(dialect)

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
            # A star that could not be expanded, over several relations: each could supply the column
            # asked for, as in the column's sources. One that replaces or renames columns may not pass
            # the column on as it is, so its formula keeps the relation's star.
            asked = '*' if _renames(node.source) else _plain(node.name.rsplit('.', 1)[-1])
            return ', '.join(
                f'{table_key(child.expression, self.default_catalog, self.default_db)}.{asked}'
                if isinstance(child.expression, exp.Table) and child.name == '*'
                else self.node(child)
                for child in node.downstream
            )
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
        self.sources = _Sources(root)
        self.scopes = {id(inner.expression): inner for inner in root.traverse()}
        for scope in root.traverse():
            query = scope.expression
            if not isinstance(query, exp.Query):
                continue
            side, before = scopes.sides.get(id(scope)), scopes.before.get(id(scope), [])
            for clause in _row_clauses(query, scope, folded):
                if clause.label is None:
                    continue
                terms = [self._term(term, scope, schema, clause.union) for term in clause.terms]
                inner = scopes.folded.get(id(clause.join)) if clause.join is not None else None
                if inner is not None:
                    where = self.expression(inner.expression.args['where'].this, inner, schema)
                    terms = [
                        ' AND '.join(
                            f'({t})' if re.search(r'\bOR\b', t, re.I) else t for t in (*terms, where)
                        )
                    ]
                text = ', '.join(terms if clause.ordered else sorted(terms))
                steps = _steps_after(query, clause.label.split(' JOIN ')[-1])
                found.add(in_context(f'{clause.label}: {text}', side, steps + before))
        return sorted(found)

    def _term(
        self,
        term: exp.Expr | _Printed | str,
        scope: Scope,
        schema: MappingSchema,
        union: exp.SetOperation | None,
    ) -> str:
        if isinstance(term, str):
            return term
        if isinstance(term, _Printed):
            text = term.expression.sql(dialect=self.dialect)
            return _unbracket(text) if term.unbracket else text
        if union is not None:
            return self._union_term(union, term, schema)
        return self.expression(term, scope, schema)

    def _union_term(self, query: exp.SetOperation, term: exp.Expr, schema: MappingSchema) -> str:
        """An ORDER BY term of a set operation, with each item it names as the items of every branch
        at its place, in the form of a set operation's column."""
        items = _branch_items(query, term)
        if not items:
            return term.sql(dialect=self.dialect)

        def union(found: list[exp.Expr]) -> exp.Expr:
            texts = []
            for item in found:
                branch = self.scopes.get(id(item.find_ancestor(exp.Select)))
                texts.append(
                    self.expression(item, branch, schema) if branch else item.sql(dialect=self.dialect)
                )
            return exp.Var(this=f'UNION({", ".join(sorted(texts))})')

        key = term.this if isinstance(term, exp.Ordered) else term
        if isinstance(key, exp.Literal):
            replaced: exp.Expr = union(next(iter(items.values())))
        else:

            def replace(node: exp.Expr) -> exp.Expr:
                if isinstance(node, exp.Column) and not node.table and node.name.lower() in items:
                    return union(items[node.name.lower()])
                return node

            replaced = key.transform(replace)
        if isinstance(term, exp.Ordered):
            ordered = exp.Ordered(
                this=replaced, desc=term.args.get('desc'), nulls_first=term.args.get('nulls_first')
            )
            return ordered.sql(dialect=self.dialect)
        return replaced.sql(dialect=self.dialect)

    def expression(self, part: exp.Expr, scope: Scope, schema: MappingSchema) -> str:
        """``part``, an expression of the qualified query that ``scope`` belongs to, in canonical
        form; ``part`` itself is left as it was."""
        if _is_star(part):
            # A star it could not expand, as in DISTINCT *: every column of the relations it reads.
            # Through a CTE or subquery, those its own stars pass on.
            key = functools.partial(
                table_key, default_catalog=self.default_catalog, default_db=self.default_db
            )
            tables: dict[str, list[str]] = {}
            for name, (_, source) in scope.selected_sources.items():
                if isinstance(part, exp.Column) and name != part.table:
                    continue
                if isinstance(source, exp.Table):
                    tables.setdefault(key(source), [])
                elif isinstance(source, Scope):
                    _star_reads(source, key, tables)
            return ', '.join(f'{table}.*' for table in sorted(tables)) or '*'
        # A bare name in a query that reads one relation is that relation's: qualify leaves it
        # bare when the relation's columns are unknown, and then the filters of a union's
        # branches, each over its own relation, would read the same.
        only = list(scope.selected_sources.values())
        single = only[0][1] if len(only) == 1 else None

        def replace(item: exp.Expr) -> exp.Expr:
            if not isinstance(item, exp.Column):
                return item
            resolver = self.sources or _Sources(scope)
            source = resolver.of(item, scope) if item.table else single
            if isinstance(source, exp.Table):
                return exp.Var(
                    this=f'{table_key(source, self.default_catalog, self.default_db)}.{item.name.lower()}'
                )
            if isinstance(source, Scope):
                try:
                    traced = self.tracer.trace(item.name, source, schema)
                except (SqlglotError, ValueError, KeyError, IndexError, AttributeError):
                    return item
                return _inline(self.node(traced))
            return item

        # In place, not on a copy: a column's own node says which scope it is in.
        if isinstance(part, exp.Column):
            return replace(part).sql(dialect=self.dialect)
        swapped: list[tuple[exp.Expr, exp.Expr]] = []
        try:
            for column in list(part.find_all(exp.Column)):
                new = replace(column)
                if new is not column:
                    column.replace(new)
                    swapped.append((new, column))
            return part.sql(dialect=self.dialect, copy=False)
        finally:
            for new, old in reversed(swapped):
                new.replace(old)


class _Tracer:
    """Lineage of the columns a statement's conditions and references read, with each step shared
    between them: a chain of CTEs is otherwise walked again for every column that reaches it."""

    def __init__(self, dialect: DialectType) -> None:
        self.dialect = dialect
        self.cache: dict[tuple[Any, ...], LineageNode] = {}
        self.meta: dict[int, Any] = {}

    def trace(self, name: str, scope: Scope, schema: MappingSchema) -> LineageNode:
        # trim_selects only tidies node labels, at the cost of copying the query for every node.
        return to_node(
            name,
            scope=scope,
            dialect=self.dialect,
            schema=schema,
            trim_selects=False,
            _cache=self.cache,
            _scope_meta=self.meta,
        )


def _references(
    root: Scope,
    sources: _Sources,
    tracer: _Tracer,
    dialect: DialectType,
    schema: MappingSchema,
    default_catalog: str | None,
    default_db: str | None,
) -> list[tuple[str, str]]:
    """Physical columns that decide which rows a query returns (see :func:`_row_clauses`)."""
    found: list[tuple[str, str]] = []

    def add(relation: str, column: str) -> None:
        item = (relation, column.lower())
        if item not in found:
            found.append(item)

    for scope in root.traverse():
        if not isinstance(scope.expression, exp.Query):
            continue
        columns = [
            c
            for clause in _row_clauses(scope.expression, scope)
            for part in clause.parts
            for c in part.find_all(exp.Column)
        ]
        for column in columns:
            source = sources.of(column, scope)
            fields = _fields_read(column)
            if isinstance(source, exp.Table):
                add(table_key(source, default_catalog, default_db), '.'.join((column.name, *fields)))
            elif isinstance(source, Scope):
                try:
                    traced = tracer.trace(column.name, source, schema)
                except (SqlglotError, ValueError, KeyError, IndexError, AttributeError):
                    continue
                for leaf, _, asked_for, path in _leaves(traced):
                    leaf_source = _leaf_source(
                        leaf, asked_for, 'direct', default_catalog, default_db, path + fields
                    )
                    if leaf_source:
                        add(leaf_source.relation, leaf_source.column)
    return found


# Filters in one form, so that two written differently compare equal

_DATE_LITERAL = re.compile(r'(\d{4}-\d{2}-\d{2})(?:[ T]00:00(?::00(?:\.0+)?)?)?')


def split_conjuncts(tree: exp.Expr, *, between: bool = False) -> list[exp.Expr]:
    """The conjuncts of a condition; with ``between``, ``x BETWEEN a AND b`` as the two new
    conjuncts ``x >= a`` and ``x <= b``."""
    parts: list[exp.Expr] = []
    stack = [tree]
    while stack:
        node = stack.pop()
        while isinstance(node, exp.Paren):
            node = node.this
        if isinstance(node, exp.And):
            stack += [node.expression, node.this]
        elif between and isinstance(node, exp.Between) and not node.args.get('symmetric'):
            parts += [
                exp.GTE(this=node.this.copy(), expression=node.args['low'].copy()),
                exp.LTE(this=node.this.copy(), expression=node.args['high'].copy()),
            ]
        else:
            parts.append(node)
    return parts


_FLIPPED: dict[type[exp.Expr], type[exp.Expr]] = {
    exp.GT: exp.LT,
    exp.LT: exp.GT,
    exp.GTE: exp.LTE,
    exp.LTE: exp.GTE,
    exp.EQ: exp.EQ,
    exp.NEQ: exp.NEQ,
}
_NEGATED: dict[type[exp.Expr], type[exp.Expr]] = {
    exp.EQ: exp.NEQ,
    exp.NEQ: exp.EQ,
    exp.GT: exp.LTE,
    exp.LTE: exp.GT,
    exp.LT: exp.GTE,
    exp.GTE: exp.LT,
}


def _listed(node: exp.Expr) -> exp.Expr:
    """A list or pattern written another way, as ``IN`` or ``=``: ``IN UNNEST(['a'])``, ``IN (VALUES
    ('a'))`` and ``= ANY(['a'])`` as ``IN ('a')``, ``<> ALL(['a'])`` as ``NOT IN ('a')``, and
    ``LIKE 'a'`` with no wildcard as ``= 'a'``."""
    if isinstance(node, exp.In):
        unnest = node.args.get('unnest')
        found: list[exp.Expr] | None = None
        if isinstance(unnest, exp.Unnest) and len(unnest.expressions) == 1:
            array = unnest.expressions[0]
            found = list(array.expressions) if isinstance(array, exp.Array) else None
        elif len(node.expressions) == 1 and isinstance(node.expressions[0], exp.Values):
            rows = node.expressions[0].expressions
            if all(isinstance(r, exp.Tuple) and len(r.expressions) == 1 for r in rows):
                found = [r.expressions[0] for r in rows]
        if found and all(isinstance(f, exp.Literal) for f in found):
            return exp.In(this=node.this, expressions=[f.copy() for f in found])
    if isinstance(node, (exp.EQ, exp.NEQ)):
        quantified = node.expression
        listed = None
        if isinstance(node, exp.EQ) and isinstance(quantified, exp.Any):
            listed = quantified.this
        elif isinstance(node, exp.NEQ) and isinstance(quantified, (exp.All, exp.Anonymous)):
            named = quantified.name.upper() if isinstance(quantified, exp.Anonymous) else 'ALL'
            listed = (
                quantified.expressions[0] if named == 'ALL' and quantified.expressions else quantified.this
            )
        while isinstance(listed, exp.Paren):
            listed = listed.this
        if isinstance(listed, exp.Array) and all(isinstance(e, exp.Literal) for e in listed.expressions):
            found_in = exp.In(this=node.this, expressions=[e.copy() for e in listed.expressions])
            return found_in if isinstance(node, exp.EQ) else exp.Not(this=found_in)
    escape = None
    if isinstance(node, exp.Escape) and isinstance(node.expression, exp.Literal):
        escape = node.expression.name
        node = node.this
    if (
        type(node) is exp.Like
        and isinstance(node.expression, exp.Literal)
        and node.expression.is_string
        and not any(c in node.expression.name for c in ['%', '_', '\\', *([escape] if escape else [])])
    ):
        compare = exp.NEQ if node.args.get('negate') else exp.EQ
        return compare(this=node.this, expression=node.expression.copy())
    return node


def _constant(node: exp.Expr) -> bool:
    """Whether a value is written out: a literal, a typed literal or a date built from literals."""
    if isinstance(node, exp.Cast):
        node = node.this
    if isinstance(node, exp.DateFromParts):
        return all(isinstance(node.args.get(k), exp.Literal) for k in ('year', 'month', 'day'))
    return isinstance(node, exp.Literal)


def normal_condition(
    tree: exp.Expr,
    dialect: DialectType,
    dated: Collection[str] = (),
    timed: Collection[str] = (),
    untimed: Collection[str] = (),
) -> exp.Expr:
    """One form for a conjunct, however it is written, so that equal filters compare equal:
    operands of ``=`` and ``<>`` in order, a literal on the right of a comparison, a one-item ``IN``
    as ``=``, a list or a pattern without wildcards written another way as ``IN`` or ``=``, and
    ``NOT`` folded into the comparison it negates. ``dated`` and ``timed`` hold the operands (by
    their SQL) whose type is a date or a timestamp, and ``untimed`` those of another known type.
    Compared with an operand not in ``untimed``, a typed date or time literal (``DATE '...'``,
    ``CAST('...' AS TIMESTAMP)``, ``MAKE_DATE(2020, 1, 1)``) is the plain string, a midnight one
    its day; against a date or timestamp, a midnight timestamp string is its day too; and on a date
    ``x > d`` is ``x >= d + 1`` and ``x < d`` is ``x <= d - 1``. ``tree`` is changed in place."""
    while isinstance(tree, exp.Paren):
        tree = tree.this

    def day(text: str) -> str:
        match = _DATE_LITERAL.fullmatch(text)
        return match.group(1) if match else text

    def value(node: exp.Expr, operand: str) -> exp.Expr:
        temporal = operand not in untimed
        if (
            temporal
            and isinstance(node, exp.Cast)
            and isinstance(node.this, exp.Literal)
            and node.this.is_string
            and node.to.is_type(*exp.DataType.TEMPORAL_TYPES)
        ):
            return exp.Literal.string(day(node.this.name))
        if temporal and isinstance(node, exp.DateFromParts) and _constant(node):
            parts = [int(node.args[k].name) for k in ('year', 'month', 'day')]
            try:
                return exp.Literal.string(str(datetime.date(*parts)))
            except ValueError:
                return node
        if isinstance(node, exp.Literal) and node.is_string and (operand in dated or operand in timed):
            return exp.Literal.string(day(node.name))
        return node

    tree = tree.transform(_listed, copy=False)
    listed = tree.this if isinstance(tree, exp.Not) else tree
    while isinstance(listed, exp.Paren):
        listed = listed.this
    if isinstance(listed, exp.In) and not listed.args.get('query'):
        operand = listed.this.sql(dialect=dialect)
        # A list is a set: its order and repeats change nothing.
        values = {(v := value(e, operand)).sql(dialect=dialect): v for e in listed.expressions}
        listed.set('expressions', [values[k] for k in sorted(values)])
    if isinstance(tree, exp.Not):
        inner = tree.this
        while isinstance(inner, exp.Paren):
            inner = inner.this
        if type(inner) in _NEGATED:
            tree = _NEGATED[type(inner)](this=inner.this, expression=inner.expression)
        elif isinstance(inner, exp.In) and len(inner.expressions) == 1 and not inner.args.get('query'):
            tree = exp.NEQ(this=inner.this, expression=inner.expressions[0])
    if isinstance(tree, exp.In) and len(tree.expressions) == 1 and not tree.args.get('query'):
        tree = exp.EQ(this=tree.this, expression=tree.expressions[0])
    if type(tree) in _FLIPPED:
        left, right = tree.this, tree.expression
        if _constant(left) and not _constant(right):
            tree = _FLIPPED[type(tree)](this=right, expression=left)
        operand = tree.this.sql(dialect=dialect)
        tree.set('expression', value(tree.expression, operand))
        if isinstance(tree, (exp.EQ, exp.NEQ)) and (
            tree.this.sql(dialect=dialect) > tree.expression.sql(dialect=dialect)
        ):
            tree = type(tree)(this=tree.expression, expression=tree.this)
        if (
            isinstance(tree, (exp.GT, exp.LT))
            and operand in dated
            and isinstance(tree.expression, exp.Literal)
        ):
            try:
                first = datetime.date.fromisoformat(tree.expression.name)
            except ValueError:
                return tree
            if isinstance(tree, exp.GT):
                return exp.GTE(
                    this=tree.this, expression=exp.Literal.string(str(first + datetime.timedelta(1)))
                )
            return exp.LTE(this=tree.this, expression=exp.Literal.string(str(first - datetime.timedelta(1))))
    return tree


def condition_key(condition: str, dialect: DialectType) -> str:
    """A condition from the graph (``WHERE: ...``), its conjuncts each in one form and sorted, so
    that two conditions that filter alike compare equal however they are written."""
    label, sep, text = condition.partition(': ')
    if not sep:
        return condition
    try:
        tree = sqlglot.parse_one(text, dialect=dialect)
    except PARSE_ERRORS:
        return condition
    parts = sorted(
        normal_condition(p, dialect).sql(dialect=dialect) for p in split_conjuncts(tree, between=True)
    )
    return f'{label}: {" AND ".join(parts)}'
