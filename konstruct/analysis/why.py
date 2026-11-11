"""`konstruct verify --why`: the source rows behind one value.

Column lineage says which columns a value is computed from; this module answers which *rows*. For
one output value, such as the lifetime value of customer 42 in ``customer_ltv``, it lists every
source row that contributes to it, with its contribution, and every related source row that a
filter or a join left out, with the reason.

It works the way :mod:`konstruct.analysis.verify` reruns models, inside the same sandbox (the
DuckDB warehouse attached read-only, a Postgres transaction that is rolled back, or a sample of a
remote warehouse in memory), and it never writes to the warehouse:

1. The model's SQL is composed down to sources: every model it reads is inlined as a subquery,
   recursively, the way :mod:`konstruct.analysis.explain` composes formulas, and every CTE is
   inlined where it is used. What remains reads only source tables (seeds, sources, tables no SQL
   in the project builds), each copied into the sandbox. A model's SQL must be a single query
   that only reads (:func:`~konstruct.analysis.verify.read_only_query`) before any of it runs.
   Each source is copied for the key alone: the key's value is followed down through plain
   columns, group keys and join equalities to the source's own column, and only the rows that
   hold it are copied. A source read beyond the key (by a window not partitioned by it, a LIMIT,
   an aggregate over other keys, or a subquery inside an expression) is copied whole, with a note.
2. The column is followed down that composed query through plain references until the step that
   computes it: the aggregate (``SUM``, ``COUNT`` and so on) or the row expression. The key
   (``customer_id = 42``) is followed down with it, through join equalities, until it is a
   condition on the rows that step reads.
3. Three read-only queries are run: the model's own query filtered by the key, for the value; the
   computing step with every input rewritten to carry the identity of its source rows
   (provenance rewriting, as in Perm and GProM), for the contributions; and the same step with
   its filters turned into reasons and its joins made full outer joins, for the rows left out.

Contributions are exact for ``SUM`` (each row's addend), ``COUNT`` (one each, or none for a null),
``AVG`` (each addend and the count), and ``MIN`` and ``MAX`` (which row won). Other aggregates,
``DISTINCT`` aggregates and window functions are not decomposable into shares: their input rows
are listed with their values and the result says so.

A row is *left out* when the key relates it to the value (it would hold the key in the computing
step, directly or through a join equality) but a WHERE, QUALIFY or HAVING condition, or a join that
found no match, removed it on the way. Rows read only by subqueries inside expressions (``IN``,
``EXISTS``, a scalar subquery) are not traced, and a UNION, a recursive CTE or a window over
grouped rows on the way is refused rather than guessed at.
"""

from __future__ import annotations

import datetime as dt
import math
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.qualify import qualify

from konstruct.analysis.explain import Explainer
from konstruct.analysis.query import QueryError, resolve_one
from konstruct.analysis.sensitivity import Settings, mask, sensitive_columns
from konstruct.analysis.verify import Candidate, Engine, candidates, engine_for, rewrite
from konstruct.core.graph import Graph, Node
from konstruct.semantics.ontology import id_like

if TYPE_CHECKING:
    from konstruct.interfaces.layout import Style

DECOMPOSABLE = ('SUM', 'COUNT', 'AVG', 'MIN', 'MAX')
"""The aggregates whose value can be split into one share per input row."""

MAX_WITNESSES = 200_000
"""The most provenance rows read for one value."""

_FUNCTIONS: dict[type[exp.Expr], str] = {
    exp.Sum: 'SUM',
    exp.Count: 'COUNT',
    exp.Avg: 'AVG',
    exp.Min: 'MIN',
    exp.Max: 'MAX',
    exp.CountIf: 'COUNT',
}


class _Unsupported(Exception):
    """SQL whose rows this module cannot trace."""


# Results


@dataclass
class SourceRow:
    """One row of a source table."""

    relation: str
    """The source's short name."""
    row_id: str
    """The row's position in the sandbox copy of the source, which identifies it in this run."""
    values: dict[str, Any] = field(default_factory=dict)
    """The row's columns."""
    shown: list[str] = field(default_factory=list)
    """The columns that identify it or that the formula reads, shown in the table."""

    def text(self) -> str:
        names = self.shown or list(self.values)[:3]
        cells = ' '.join(f'{n}={show_value(self.values.get(n))}' for n in names)
        return f'{self.relation} {cells}'.rstrip()

    def to_dict(self) -> dict[str, Any]:
        return {
            'relation': self.relation,
            'row_id': self.row_id,
            'values': {k: _jsonable(v) for k, v in self.values.items()},
        }


@dataclass
class Contribution:
    """One input row of the computing step, with the source rows behind it and its share."""

    rows: list[SourceRow]
    """The source rows that make up this input row, the one the formula reads first."""
    values: list[Any]
    """Per aggregate, this row's share: the addend of a SUM or AVG, 1 or 0 for a COUNT, the
    candidate of a MIN or MAX, or the input value of an aggregate that cannot be split. For a value
    computed without an aggregate, the value itself."""
    chosen: bool = False
    """For MIN and MAX: this row holds the result."""

    def to_dict(self) -> dict[str, Any]:
        return {
            'values': [_jsonable(v) for v in self.values],
            'rows': [r.to_dict() for r in self.rows],
            'chosen': self.chosen,
        }


@dataclass
class Exclusion:
    """A source row related to the value by the key that a filter or a join left out."""

    row: SourceRow
    """The source row."""
    reason: str
    """The model and the WHERE condition, HAVING condition or join that removed it."""

    def to_dict(self) -> dict[str, Any]:
        return {'reason': self.reason, **self.row.to_dict()}


@dataclass
class Aggregate:
    """One aggregate in the computing step."""

    sql: str
    """The aggregate as written, with the step's names."""
    function: str
    """SUM, COUNT, AVG, MIN, MAX, or another aggregate's name."""
    decomposable: bool
    """Whether the value splits into one share per row (not for DISTINCT, MEDIAN and the like)."""
    total: Any = None
    """The aggregate recomputed from the rows' shares."""
    count: int = 0
    """The rows it counts: those with a non-null input."""
    addends: Any = None
    """For AVG, the sum of the addends, which ``count`` divides; None for the others."""

    def to_dict(self) -> dict[str, Any]:
        return {
            'sql': self.sql,
            'function': self.function,
            'decomposable': self.decomposable,
            'total': _jsonable(self.total),
            'count': self.count,
            'addends': _jsonable(self.addends),
        }


@dataclass
class Why:
    """The rows behind one value of a column."""

    column: str
    """The column id."""
    label: str
    """Its short name."""
    where: dict[str, str]
    """The key that picks the row: output column to value."""
    warehouse: str
    """Where it ran."""
    value: Any = None
    """The value, computed by the model's SQL in the sandbox."""
    matches: int = 0
    """How many rows of the model the key matches, 1 when it picks one row."""
    expression: str | None = None
    """The column's own formula."""
    composed: str | None = None
    """The formula in terms of source columns."""
    step: str | None = None
    """The model whose step computes the value, which the contributions are rows of."""
    aggregates: list[Aggregate] = field(default_factory=list)
    """The aggregates of that step; empty for a value computed row by row."""
    contributions: list[Contribution] = field(default_factory=list)
    """The contributing rows, up to ``limit``, largest share first."""
    contributors: int = 0
    """How many rows contribute, before the cap."""
    excluded: list[Exclusion] = field(default_factory=list)
    """Related rows left out, up to ``limit``."""
    exclusions: int = 0
    """How many related rows were left out, before the cap."""
    limit: int = 50
    """The most rows listed in each table."""
    consistent: bool | None = None
    """Whether the shares add up to the value (None where that cannot be checked)."""
    notes: list[str] = field(default_factory=list)
    """Limits of this explanation: sampled inputs, untraced exclusions, approximate shares."""
    seconds: float = 0.0
    """Time taken."""
    masked: list[str] = field(default_factory=list)
    """The sensitive columns whose values are shown masked, by short name (see
    :mod:`konstruct.analysis.sensitivity`)."""

    @property
    def capped(self) -> bool:
        """Whether either table was cut at ``limit``."""
        return self.contributors > len(self.contributions) or self.exclusions > len(self.excluded)

    @property
    def decomposable(self) -> bool:
        return bool(self.aggregates) and all(a.decomposable for a in self.aggregates)

    def explanation(self) -> list[str]:
        """A few sentences on how the rows make up the value."""
        lines: list[str] = []
        key = key_text(self.where)
        if self.matches == 0:
            reach = '; the rows below reach the step that computes the value' if self.contributors else ''
            lines.append(f'No row of {self._model()} has {key}{reach}.')
        rows = _plural(self.contributors, 'row')
        for aggregate in self.aggregates:
            function = aggregate.function
            if not aggregate.decomposable:
                lines.append(
                    f'{aggregate.sql} is not decomposable: it cannot be split into a share per row, '
                    f'so the input value of each of the {rows} is listed instead.'
                )
            elif not self.contributors:
                lines.append(f'No rows reach {aggregate.sql}.')
            elif function == 'SUM':
                lines.append(
                    f'{aggregate.sql} adds up {rows}: their addends total {show_value(aggregate.total)}.'
                )
            elif function == 'COUNT':
                lines.append(f'{aggregate.sql} counts {aggregate.count} of {rows}.')
            elif function == 'AVG':
                lines.append(
                    f'{aggregate.sql} averages {_plural(aggregate.count, "value")}: '
                    f'{show_value(aggregate.addends)} / {aggregate.count} = {show_value(aggregate.total)}.'
                )
            else:
                winner = 'largest' if function == 'MAX' else 'smallest'
                lines.append(
                    f'{aggregate.sql} takes the {winner} of {rows}: {show_value(aggregate.total)}, marked *.'
                )
        if not self.aggregates and self.contributors:
            lines.append(f'The value is computed row by row, from {rows}.')
        if self.consistent is True and not self.aggregates:
            lines.append(f'The row gives the value, {show_value(self.value)}.')
        elif self.consistent is True:
            lines.append(f'The shares add up to the value, {show_value(self.value)}.')
        elif self.consistent is False:
            lines.append(
                f'The shares do not add up to the value, {show_value(self.value)}: '
                'something on the way changes it (see the notes).'
            )
        if self.exclusions:
            lines.append(f'{_plural(self.exclusions, "related row")} left out, each with its reason.')
        if self.masked:
            lines.append(
                f'The values of {_plural(len(self.masked), "sensitive column")} are masked: '
                f'{", ".join(self.masked)}.'
            )
        return lines + self.notes

    def _model(self) -> str:
        return self.label.rsplit('.', 1)[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            'column': self.column,
            'label': self.label,
            'where': self.where,
            'warehouse': self.warehouse,
            'value': _jsonable(self.value),
            'matches': self.matches,
            'expression': self.expression,
            'composed': self.composed,
            'step': self.step,
            'aggregates': [a.to_dict() for a in self.aggregates],
            'decomposable': self.decomposable,
            'consistent': self.consistent,
            'contributors': self.contributors,
            'contributions': [c.to_dict() for c in self.contributions],
            'exclusions': self.exclusions,
            'excluded': [e.to_dict() for e in self.excluded],
            'limit': self.limit,
            'capped': self.capped,
            'explanation': self.explanation(),
            'masked': self.masked,
            'seconds': round(self.seconds, 3),
        }

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import show, why_lines

        return show(why_lines, self, style)

    def __str__(self) -> str:
        return self.text()


def key_text(where: Mapping[str, str]) -> str:
    """The key as words: ``customer_id = 42 and month = 2024-01``."""
    return ' and '.join(f'{k} = {v}' for k, v in where.items())


def _plural(count: int, word: str) -> str:
    return f'{count} {word}{"" if count == 1 else "s"}'


def show_value(value: Any) -> str:
    """A value as the text shows it."""
    if value is None:
        return 'null'
    if isinstance(value, float):
        return repr(round(value, 10))
    return str(value)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def _sum(values: Sequence[Any]) -> Any:
    present = [v for v in values if v is not None]
    if not present:
        return None
    if any(isinstance(v, float) for v in present):
        return math.fsum(float(v) for v in present)
    total = present[0]
    for v in present[1:]:
        total = total + v
    return total


def _article(word: str) -> str:
    """``a`` or ``an`` before ``word``, a join keyword such as ``ANTI`` or ``SEMI``."""
    return 'an' if word[:1] in 'AEIOU' else 'a'


def parse_where(items: Sequence[str]) -> dict[str, str]:
    """``['customer_id=42']`` as ``{'customer_id': '42'}``: the ``--where`` options."""
    where: dict[str, str] = {}
    for item in items:
        name, sep, value = item.partition('=')
        if not sep or not name.strip():
            raise QueryError(f'--where takes column=value, not {item!r}')
        where[name.strip()] = value.strip()
    if not where:
        raise QueryError('give the row with --where column=value')
    return where


# Composing the model's SQL down to sources


def _short(node: exp.Expr, dialect: str) -> str:
    return node.sql(dialect=dialect)


class _Composer:
    """The model's query with every model it reads inlined, down to source tables."""

    def __init__(self, graph: Graph, found: dict[str, Candidate], read: str, write: str) -> None:
        self.graph = graph
        self.found = found
        self.read = read
        self.write = write
        self.sources: dict[str, int] = {}
        """Source relation key to its index; the composed query reads it as ``__kp_src<index>``."""
        self.models = {key: f'__kp_model{i}' for i, key in enumerate(sorted(found))}
        """Each model's placeholder, which the composed query replaces by the model's own query."""
        self.rewritten: dict[str, tuple[str, dict[str, Candidate]]] = {}
        """Each model's SQL pointed at its placeholders, worked out once however many paths read it
        (a diamond): its text, and the model each ``__kp_model<n>`` placeholder stands for."""

    def query(self, candidate: Candidate, stack: tuple[str, ...] = ()) -> exp.Expr:
        relation = candidate.node.relation or candidate.node.id
        if relation not in self.rewritten:
            self.rewritten[relation] = self._rewrite(candidate, (*stack, relation))
        sql, models = self.rewritten[relation]
        # Each reading of a model is rows of its own, so it is parsed afresh: a small model's SQL
        # parses faster than a composed tree copies.
        tree = sqlglot.parse_one(sql, dialect=self.write)
        for table in list(tree.find_all(exp.Table)):
            inner = models.get(table.name) if not table.db else None
            if inner is not None:
                alias = exp.TableAlias(this=exp.to_identifier(table.alias or table.name))
                table.replace(exp.Subquery(this=self.query(inner, (*stack, relation)), alias=alias))
        tree.meta['kp_model'] = self.graph.label(candidate.node)
        return tree

    def _rewrite(self, candidate: Candidate, stack: tuple[str, ...]) -> tuple[str, dict[str, Candidate]]:
        # verify's rewrite points each table at its placeholder, translates the SQL to the engine's
        # dialect and refuses anything but one query that reads. Every model has its placeholder
        # before the SQL is read, so a model that reads only models is rewritten once.
        catalog, schema = candidate.default_catalog, candidate.default_schema
        mapping = {key: name for key, name in self.models.items() if key != candidate.node.relation}
        try:
            sql, reads = rewrite(candidate.sql, mapping, catalog, schema, self.read, self.write)
        except ValueError as error:
            label = self.graph.label(candidate.node)
            raise QueryError(f'{label} is not a query that only reads: {error}') from error
        cycle = sorted(key for key in reads if key in mapping and key in stack)
        if cycle:
            raise QueryError(f'the models reading {cycle[0]} form a cycle')
        sources = sorted(key for key in reads if key not in mapping)
        if sources:
            for key in sources:
                mapping[key] = f'__kp_src{self.sources.setdefault(key, len(self.sources))}'
            sql, _ = rewrite(candidate.sql, mapping, catalog, schema, self.read, self.write)
        models = {mapping[key]: self.found[key] for key in reads if key in self.found and key in mapping}
        return sql, models


def _inline_ctes(query: exp.Expr, env: Mapping[str, list[Any]] | None = None) -> None:
    """Replace every reference to a CTE with its query, so each step is a subquery. The first
    reference takes the CTE's query itself and any other a copy, each being rows of its own."""
    scope: dict[str, list[Any]] = dict(env or {})
    with_ = query.args.get('with_')
    if isinstance(with_, exp.With):
        if with_.args.get('recursive'):
            raise _Unsupported('a recursive CTE')
        for cte in with_.expressions:
            _inline_ctes(cte.this, scope)
            scope[cte.alias_or_name.lower()] = [cte.this, False]
        query.set('with_', None)
    stack = list(query.iter_expressions())
    while stack:
        node = stack.pop()
        if isinstance(node, exp.Query) and node.args.get('with_'):
            _inline_ctes(node, scope)
            continue
        if isinstance(node, exp.Table) and not node.db and node.name.lower() in scope:
            entry = scope[node.name.lower()]
            body = entry[0].copy() if entry[1] else entry[0]
            entry[1] = True
            alias = exp.TableAlias(this=exp.to_identifier(node.alias or node.name))
            node.replace(exp.Subquery(this=body, alias=alias))
            continue
        stack.extend(node.iter_expressions())


_GROUPING_SETS = (exp.Rollup, exp.Cube, exp.GroupingSets)


def _normalise(tree: exp.Expr) -> None:
    """Refuse the steps whose rows cannot be traced, wherever they are, and write a DISTINCT ON as
    the deduplication it is.

    ROLLUP, CUBE and GROUPING SETS (in sqlglot's GROUP BY expressions) give rows that no group key
    joins back to, and a SEMI or ANTI join reads rows it never returns. ``DISTINCT ON (k)`` keeps
    the first row of each ``k`` by the ORDER BY: it is ``QUALIFY ROW_NUMBER() OVER (PARTITION BY k
    ORDER BY ...) = 1``, so the row kept is the one contributor, the others of its key are left out
    with the DISTINCT ON as the reason, and the key is pushed down only through its ON columns."""
    for select in list(tree.find_all(exp.Select)):
        model = _model(select)
        group = select.args.get('group')
        if group is not None and (
            any(isinstance(e, _GROUPING_SETS) for e in group.expressions)
            or any(group.args.get(arg) for arg in ('rollup', 'cube', 'grouping_sets'))
        ):
            raise _Unsupported(f'ROLLUP, CUBE or GROUPING SETS in {model}')
        for join in select.args.get('joins') or []:
            kind = (join.kind or '').upper()
            if kind in ('SEMI', 'ANTI'):
                raise _Unsupported(f'{_article(kind)} {kind} JOIN in {model}')
        distinct = select.args.get('distinct')
        on = distinct.args.get('on') if isinstance(distinct, exp.Distinct) else None
        if on is None:
            continue
        if group is not None or any(_aggregates(p.unalias()) for p in select.expressions):
            raise _Unsupported(f'DISTINCT ON over grouped rows in {model}')
        keys = list(on.expressions) if isinstance(on, exp.Tuple) else [on]
        order = select.args.get('order')
        window = exp.Window(
            this=exp.RowNumber(),
            partition_by=[_unalias(select, k) for k in keys],
            order=_unalias(select, order) if isinstance(order, exp.Order) else None,
        )
        first: exp.Expr = exp.EQ(this=window, expression=exp.Literal.number(1))
        shown = ', '.join(k.name if isinstance(k, exp.Column) else k.sql() for k in keys)
        first.meta['kp_reason'] = f'DISTINCT ON ({shown}) kept another row'
        qualify_ = select.args.get('qualify')
        if isinstance(qualify_, exp.Qualify):
            first = exp.And(this=qualify_.this, expression=first)
        select.set('qualify', exp.Qualify(this=first))
        select.set('distinct', None)
        if not select.args.get('limit'):
            select.set('order', None)


def _unalias(select: exp.Select, expression: exp.Expr) -> exp.Expr:
    """``expression`` with each name of one of the step's outputs (DISTINCT ON and ORDER BY may name
    them) replaced by that output's expression, as a window inside the step must read it."""

    def replace(column: exp.Column) -> exp.Expr:
        found = _projection(select, column.name) if not column.table else None
        return found.copy() if found is not None else column.copy()

    return _replace_columns(expression, replace)


def _mark_models(tree: exp.Expr) -> None:
    """Record on every step the model it comes from, so copies of a step keep it."""
    for select in tree.find_all(exp.Select):
        select.meta['kp_model'] = _model(select)


# Reading the composed query


def _select(query: exp.Expr) -> exp.Select:
    while isinstance(query, exp.Subquery):
        query = query.this
    if isinstance(query, exp.SetOperation):
        raise _Unsupported(f'a {query.key.upper()}')
    if not isinstance(query, exp.Select):
        raise _Unsupported(f'a {type(query).__name__} query')
    return query


def _model(node: exp.Expr) -> str:
    """The model a step comes from (every step is marked by :func:`_mark_models`)."""
    current: exp.Expr | None = node
    while current is not None:
        if 'kp_model' in current.meta:
            return str(current.meta['kp_model'])
        current = current.parent
    return 'the model'


def _items(select: exp.Select) -> list[exp.Expr]:
    from_ = select.args.get('from_')
    found = [from_.this] if isinstance(from_, exp.From) else []
    return found + [join.this for join in select.args.get('joins') or []]


def _item(select: exp.Select, alias: str) -> exp.Expr | None:
    for item in _items(select):
        if item.alias_or_name.lower() == alias.lower():
            return item
    return None


def _projection(select: exp.Select, name: str) -> exp.Expr | None:
    for projection in select.expressions:
        if projection.alias_or_name.lower() == name.lower():
            found: exp.Expr = projection.unalias()
            return found
    return None


def _local(node: exp.Expr, root: exp.Expr) -> bool:
    """Whether ``node`` belongs to ``root``'s own step, not a subquery or window inside it."""
    if node is root:
        return True
    current = node.parent
    while current is not None and current is not root:
        if isinstance(current, (exp.Query, exp.Subquery, exp.Window)):
            return False
        current = current.parent
    return True


def _aggregates(expression: exp.Expr) -> list[exp.AggFunc]:
    found = [expression] if isinstance(expression, exp.AggFunc) else []
    return found + [
        node
        for node in expression.find_all(exp.AggFunc)
        if node is not expression and _local(node, expression) and not isinstance(node.parent, exp.Window)
    ]


def _columns(expression: exp.Expr) -> list[exp.Column]:
    if isinstance(expression, exp.Column):
        return [expression]
    return [c for c in expression.find_all(exp.Column) if _local(c, expression)]


def _grouped(select: exp.Select) -> bool:
    return bool(
        select.args.get('group')
        or select.args.get('distinct')
        or any(_aggregates(p.unalias()) for p in select.expressions)
    )


def _has_window(expression: exp.Expr) -> bool:
    return any(_local(w, expression) for w in expression.find_all(exp.Window))


def _conjuncts(condition: exp.Expr | None) -> list[exp.Expr]:
    if condition is None:
        return []
    if isinstance(condition, (exp.Where, exp.Qualify)):
        condition = condition.this
    return list(condition.flatten()) if isinstance(condition, exp.And) else [condition]


def _ref(column: exp.Column) -> tuple[str, str]:
    return column.table.lower(), column.name.lower()


def _equalities(select: exp.Select, *, directed: bool) -> dict[tuple[str, str], set[tuple[str, str]]]:
    """Column equalities of a step's joins and filter. With ``directed``, a LEFT JOIN's equality
    only leads from the kept side to the joined side: on the kept side, the column also holds
    values that matched nothing."""
    edges: dict[tuple[str, str], set[tuple[str, str]]] = {}

    def add(condition: exp.Expr | None, joined: str | None) -> None:
        for conjunct in _conjuncts(condition):
            if not isinstance(conjunct, exp.EQ):
                continue
            left, right = conjunct.this, conjunct.expression
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                continue
            a, b = _ref(left), _ref(right)
            if directed and joined is not None and joined in (a[0], b[0]):
                kept, other = (b, a) if a[0] == joined else (a, b)
                edges.setdefault(kept, set()).add(other)
                continue
            edges.setdefault(a, set()).add(b)
            edges.setdefault(b, set()).add(a)

    add(select.args.get('where'), None)
    for join in select.args.get('joins') or []:
        side = (join.side or '').upper()
        add(join.args.get('on'), join.this.alias_or_name.lower() if side == 'LEFT' else None)
    return edges


def _reachable(
    start: tuple[str, str], edges: Mapping[tuple[str, str], set[tuple[str, str]]]
) -> list[tuple[str, str]]:
    seen = {start}
    queue = [start]
    while queue:
        for nxt in sorted(edges.get(queue.pop(0), set())):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return sorted(seen - {start})


def _replace_columns(expression: exp.Expr, replace: Callable[[exp.Column], exp.Expr]) -> exp.Expr:
    copied = expression.copy()
    if isinstance(copied, exp.Column):
        return replace(copied)
    for column in _columns(copied):
        column.replace(replace(column))
    return copied


def _descend(key: str, expression: exp.Expr, select: exp.Select, alias: str, inner: exp.Select) -> exp.Expr:
    """``expression``, over the items of ``select``, as an expression over ``inner``'s items, where
    ``inner`` is the step read as ``alias``."""
    edges = _equalities(select, directed=True)

    def replace(column: exp.Column) -> exp.Expr:
        ref = _ref(column)
        if ref[0] != alias.lower():
            matches = [r for r in _reachable(ref, edges) if r[0] == alias.lower()]
            if not matches:
                raise QueryError(
                    f'cannot follow {key} into {alias} in {_model(select)}: '
                    f'{column.sql()} is not joined to it by an equality'
                )
            ref = matches[0]
        found = _projection(inner, ref[1])
        if found is None:
            raise QueryError(f'cannot follow {key}: {alias} has no column {ref[1]}')
        if _aggregates(found) or _has_window(found):
            raise QueryError(f'{key} is computed by an aggregate or window, so it does not pick rows')
        return found.copy()

    return _replace_columns(expression, replace)


@dataclass
class _Plan:
    """Where the value is computed and what picks its rows there."""

    top: exp.Select
    anchor: exp.Select
    """The step that computes the value: its aggregate, or its row expression."""
    expression: exp.Expr
    """The value's expression in that step."""
    keys: list[exp.Expr]
    """The key's columns as expressions over the rows that step reads, in the order given."""
    plain: bool
    """Whether every step above the anchor passes the value through unchanged."""
    filters: list[str] = field(default_factory=list)
    """The filters of the steps above the anchor, which decide whether the value's row exists."""


def _plan(top: exp.Select, column: str, where: Mapping[str, str]) -> _Plan:
    """Follow the column down through plain references to the step that computes it, and the key
    with it. A value computed row by row is explained at the first step that changes it; one that
    passes through an expression on the way to an aggregate (``COALESCE(total, 0)``) at the
    aggregate."""
    expression = _projection(top, column)
    if expression is None:
        raise QueryError(f'the composed query has no column {column}')
    keys: dict[str, exp.Expr] = {}
    for name in where:
        found = _projection(top, name)
        if found is None:
            names = ', '.join(p.alias_or_name for p in top.expressions)
            raise QueryError(f'{name} is not a column of the model; it has {names}')
        keys[name] = found
    block, plain = top, True
    filters: list[str] = []
    changed: tuple[exp.Select, exp.Expr, dict[str, exp.Expr], list[str]] | None = None
    while not _grouped(block) and not _aggregates(expression):
        columns = _columns(expression)
        if len(columns) != 1:
            break
        item = _item(block, columns[0].table)
        if not isinstance(item, exp.Subquery):
            break
        inner = _select(item.this)
        found = _projection(inner, columns[0].name)
        if found is None:
            break
        if not isinstance(expression, exp.Column) and changed is None:
            changed = (block, expression, keys, list(filters))
        try:
            keys = {k: _descend(k, e, block, item.alias_or_name, inner) for k, e in keys.items()}
        except QueryError:
            if changed is None:
                raise
            break
        filters += [f'{_model(block)}: WHERE {c.sql()}' for c in _conjuncts(block.args.get('where'))]
        block, expression = inner, found
    if changed is not None and not _grouped(block) and not _aggregates(expression):
        block, expression, keys, filters = changed
    else:
        plain = changed is None
    for name, key in keys.items():
        if _aggregates(key) or _has_window(key):
            raise QueryError(f'{name} is computed by an aggregate or window, so it does not pick rows')
    return _Plan(top, block, expression, [keys[name] for name in where], plain, filters)


# Provenance rewriting


@dataclass
class _Rewritten:
    query: exp.Select
    provenance: list[int]
    """The source-row instances it carries, as columns ``__kp_r<instance>``."""
    identity: list[str]
    """Columns that identify each of its rows (a source row id, or a group's key)."""
    expands: bool = False
    """Whether it returns a row per contributing row rather than per row of the original."""


@dataclass
class _ItemOut:
    provenance: list[tuple[int, exp.Expr]]
    identity: list[tuple[str, exp.Expr]]
    present: exp.Expr
    why: exp.Expr | None
    expands: bool


def _column(name: str, table: str) -> exp.Column:
    return exp.column(name, table=table, quoted=False)


def _output(projection: exp.Expr) -> exp.Identifier:
    """The name a projection is read by, quoted as written."""
    alias = projection.args.get('alias') if isinstance(projection, exp.Alias) else None
    if isinstance(alias, exp.Identifier):
        return alias.copy()
    if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Identifier):
        return projection.this.copy()
    return exp.to_identifier(projection.alias_or_name)


def _subquery(query: exp.Expr, alias: str) -> exp.Subquery:
    return exp.Subquery(this=query, alias=exp.TableAlias(this=exp.to_identifier(alias)))


def _or(conditions: Sequence[exp.Expr]) -> exp.Expr:
    if not conditions:
        return exp.false()
    result = conditions[0]
    for condition in conditions[1:]:
        result = exp.Or(this=result, expression=condition)
    return exp.Paren(this=result)


def _and(conditions: Sequence[exp.Expr]) -> exp.Expr:
    if not conditions:
        return exp.true()
    result = conditions[0]
    for condition in conditions[1:]:
        result = exp.And(this=result, expression=condition)
    return result


def _when(condition: exp.Expr, text: str) -> exp.Expr:
    return exp.Case(ifs=[exp.If(this=condition, true=exp.Literal.string(text))])


def _null_text() -> exp.Expr:
    return exp.cast(exp.null(), 'TEXT')


class _Rewriter:
    """Rewrites steps so each row carries the ids of the source rows behind it.

    In exact mode the rows are those of the original query. In relaxed mode, joins become full
    outer joins and filters become reasons, so every related row is kept with ``__kp_why``, the
    first condition that removed it (null for a row the original keeps)."""

    def __init__(self, scratch: Mapping[str, str], row_id: Callable[[str], exp.Expr], dialect: str) -> None:
        self.scratch = scratch
        self.row_id = row_id
        self.dialect = dialect
        self.instances: dict[int, str] = {}
        self.blocks = 0
        self.notes: list[str] = []
        """What the relaxed rewrite cannot give a reason for."""

    def item(self, item: exp.Expr, relaxed: bool) -> _ItemOut:
        alias = item.alias_or_name
        if isinstance(item, exp.Table):
            key = self.scratch.get('.'.join(p.name for p in item.parts).lower())
            if key is None:
                raise _Unsupported(f'the table {item.sql(dialect=self.dialect)}')
            instance = len(self.instances)
            self.instances[instance] = key
            rid = self.row_id(alias)
            return _ItemOut(
                [(instance, rid)],
                [(f'__kp_r{instance}', rid)],
                exp.Not(this=exp.Is(this=rid, expression=exp.null())),
                None,
                False,
            )
        if isinstance(item, exp.Subquery):
            inner = self.query(_select(item.this), relaxed)
            item.set('this', inner.query)
            return _ItemOut(
                [(n, _column(f'__kp_r{n}', alias)) for n in inner.provenance],
                [(name, _column(name, alias)) for name in inner.identity],
                exp.Not(this=exp.Is(this=_column('__kp_m', alias), expression=exp.null())),
                _column('__kp_why', alias) if relaxed else None,
                inner.expands,
            )
        raise _Unsupported(f'reading from {item.sql(dialect=self.dialect)}')

    def query(self, select: exp.Select, relaxed: bool) -> _Rewritten:
        if _grouped(select):
            return self.grouped(select, relaxed)
        return self.rows(select, relaxed)

    def rows(self, select: exp.Select, relaxed: bool) -> _Rewritten:
        """A step that returns a row per input row: the ids of each input row's sources are added."""
        items = _items(select)
        outs = [self.item(i, relaxed) for i in items]
        expands = any(o.expands for o in outs)
        if expands and any(_has_window(p) for p in select.expressions):
            raise _Unsupported(f'a window function over grouped rows in {_model(select)}')
        provenance = [p for o in outs for p in o.provenance]
        identity = [i for o in outs for i in o.identity]
        if relaxed:
            if select.args.get('limit') or select.args.get('offset'):
                self.notes.append(
                    f'{_model(select)} keeps only some rows (LIMIT): the rows it drops are not listed as left out.'
                )
            _join_on_where(select)
            reasons = [o.why for o in outs if o.why is not None]
            reasons += self._join_reasons(select, [o.present for o in outs])
            # A dbt deduplication (QUALIFY row_number() ... = 1) is a filter like WHERE.
            for clause in ('where', 'qualify'):
                for condition in _conjuncts(select.args.get(clause)):
                    said = (
                        condition.meta.get('kp_reason')
                        or f'{clause.upper()} {_short(condition, self.dialect)}'
                    )
                    text = f'{_model(select)}: {said}'
                    failed = exp.Not(this=exp.Coalesce(this=condition.copy(), expressions=[exp.false()]))
                    reasons.append(_when(failed, text))
                select.set(clause, None)
            why = exp.Coalesce(this=reasons[0], expressions=reasons[1:]) if reasons else _null_text()
        names = {f'__kp_r{n}' for n, _ in provenance}
        added = [exp.alias_(e, f'__kp_r{n}', quoted=False) for n, e in provenance]
        added += [exp.alias_(e, name, quoted=False) for name, e in identity if name not in names]
        if relaxed:
            added += [exp.alias_(exp.Literal.number(1), '__kp_m'), exp.alias_(why, '__kp_why')]
        select.set('expressions', [*select.expressions, *added])
        return _Rewritten(select, [n for n, _ in provenance], [name for name, _ in identity], expands)

    def _join_reasons(self, select: exp.Select, present: list[exp.Expr]) -> list[exp.Expr]:
        reasons: list[exp.Expr] = []
        for index, join in enumerate(select.args.get('joins') or [], start=1):
            side = (join.side or '').upper()
            if side in ('RIGHT', 'FULL'):
                raise _Unsupported(f'{_article(side)} {side} JOIN in {_model(select)}')
            on = join.args.get('on')
            if on is None:
                if not join.args.get('using'):
                    self.notes.append(
                        f'{_model(select)}: {(join.kind or "comma").upper()} JOIN {join.this.alias_or_name} has no '
                        'condition: when one side has no rows, the rows of the other are left out without a reason.'
                    )
                continue
            left = _or([p.copy() for p in present[:index]])
            here = present[index].copy()
            moved = join.meta.get('kp_where')
            if moved:
                # The equalities were a WHERE in the SQL: the reason names them as written.
                text = f'{_model(select)}: WHERE {_short(_and(moved), self.dialect)} found no match'
            else:
                text = (
                    f'{_model(select)}: {"LEFT JOIN" if side == "LEFT" else "JOIN"} '
                    f'{join.this.alias_or_name} ON {_short(on, self.dialect)} found no match'
                )
            unmatched: exp.Expr = exp.And(this=here, expression=exp.Not(this=left))
            if side != 'LEFT':
                unmatched = exp.Or(
                    this=exp.Paren(this=unmatched),
                    expression=exp.Paren(
                        this=exp.And(this=left.copy(), expression=exp.Not(this=here.copy()))
                    ),
                )
            reasons.append(_when(unmatched, text))
            join.set('side', 'FULL')
            join.set('kind', None)
        return reasons

    def grouped(self, select: exp.Select, relaxed: bool) -> _Rewritten:
        """An aggregating step, joined back to its input rows on the group key (Perm's rule), so
        each group is repeated once per input row with that row's source ids."""
        block = self.blocks
        self.blocks += 1
        group = select.args.get('group')
        if group is not None:
            keys = [g.copy() for g in group.expressions]
        elif select.args.get('distinct'):
            keys = [p.unalias().copy() for p in select.expressions]
        else:
            keys = []
        names = [_output(p) for p in select.expressions]
        original = select.copy()
        original.set(
            'expressions',
            [
                *original.expressions,
                *(exp.alias_(k.copy(), f'__kp_k{block}_{j}', quoted=False) for j, k in enumerate(keys)),
                exp.alias_(exp.Literal.number(1), '__kp_n'),
            ],
        )
        having = select.args.get('having')
        inputs = select
        for arg in ('group', 'having', 'order', 'limit', 'offset', 'distinct', 'qualify'):
            inputs.set(arg, None)
        inputs.set(
            'expressions',
            [exp.alias_(k.copy(), f'__kp_g{block}_{j}', quoted=False) for j, k in enumerate(keys)],
        )
        rewritten = self.rows(inputs, relaxed)
        o, i = f'__kp_o{block}', f'__kp_i{block}'
        on = _and(
            [
                exp.NullSafeEQ(
                    this=_column(f'__kp_k{block}_{j}', o), expression=_column(f'__kp_g{block}_{j}', i)
                )
                for j in range(len(keys))
            ]
        )
        projections: list[exp.Expr] = []
        for name, projection in zip(names, original.expressions[: len(names)], strict=True):
            value: exp.Expr = exp.Column(this=name.copy(), table=exp.to_identifier(o))
            if relaxed:
                for j, k in enumerate(keys):
                    if projection.unalias() == k:
                        value = exp.Coalesce(this=value, expressions=[_column(f'__kp_g{block}_{j}', i)])
                        break
            projections.append(exp.alias_(value, name.copy()))
        identity = [f'__kp_k{block}_{j}' for j in range(len(keys))]
        for j, key in enumerate(identity):
            value = _column(key, o)
            if relaxed:
                value = exp.Coalesce(this=value, expressions=[_column(f'__kp_g{block}_{j}', i)])
            projections.append(exp.alias_(value, key, quoted=False))
        projections += [
            exp.alias_(_column(f'__kp_r{n}', i), f'__kp_r{n}', quoted=False) for n in rewritten.provenance
        ]
        result = exp.select(*projections)
        if relaxed:
            text = f'{_model(select)}: HAVING {_short(having.this, self.dialect)}' if having else 'no group'
            missing = exp.Is(this=_column('__kp_n', o), expression=exp.null())
            why = exp.Coalesce(this=_column('__kp_why', i), expressions=[_when(missing, text)])
            result.select(
                exp.alias_(exp.Literal.number(1), '__kp_m'), exp.alias_(why, '__kp_why'), copy=False
            )
            result.from_(_subquery(rewritten.query, i), copy=False)
            result.join(_subquery(original, o), on=on, join_type='LEFT', copy=False)
        else:
            result.from_(_subquery(original, o), copy=False)
            result.join(_subquery(rewritten.query, i), on=on, copy=False)
        return _Rewritten(result, rewritten.provenance, identity, True)


# Running


def _join_on_where(select: exp.Select) -> None:
    """Move each WHERE equality between two FROM items onto the inner join that brings in the later
    of them (``FROM a, b WHERE a.k = b.k``, or ``JOIN b ON TRUE WHERE``), before the relaxed rewrite
    makes the join a full join. Left in the WHERE, the equality would become a reason over a cross
    product, and every row of one side would be related to every row of the other."""
    joins = select.args.get('joins') or []
    where = select.args.get('where')
    if not joins or where is None:
        return
    position = {item.alias_or_name.lower(): i for i, item in enumerate(_items(select))}
    kept: list[exp.Expr] = []
    moved: dict[int, list[exp.Expr]] = {}
    for conjunct in _conjuncts(where):
        left, right = conjunct.args.get('this'), conjunct.args.get('expression')
        if isinstance(conjunct, exp.EQ) and isinstance(left, exp.Column) and isinstance(right, exp.Column):
            a, b = position.get(left.table.lower()), position.get(right.table.lower())
            if a is not None and b is not None and a != b:
                join = joins[max(a, b) - 1]
                if not join.side and (join.kind or 'CROSS').upper() in ('CROSS', 'INNER'):
                    moved.setdefault(max(a, b) - 1, []).append(conjunct)
                    continue
        kept.append(conjunct)
    for index, conditions in moved.items():
        join = joins[index]
        on = join.args.get('on')
        existing = [] if on is None or on == exp.true() else [on]
        join.set('on', _and([*existing, *(c.copy() for c in conditions)]))
        join.set('kind', None)
        if not existing:
            join.meta['kp_where'] = [c.copy() for c in conditions]
    select.set('where', exp.Where(this=_and([c.copy() for c in kept])) if kept else None)


def _row_id(dialect: str) -> Callable[[str], exp.Expr]:
    """The expression for a row's id in the sandbox copy of a source: DuckDB's ``rowid``, or a
    Postgres row's ``ctid`` as text. Both are stable for the copy, which nothing changes."""
    if dialect == 'postgres':
        return lambda alias: exp.cast(_column('ctid', alias), 'TEXT')
    return lambda alias: _column('rowid', alias)


def _share(aggregate: exp.AggFunc, dialect: str) -> tuple[exp.Expr, Aggregate]:
    """Each input row's share of ``aggregate``, as an expression over the step's rows."""
    parent = aggregate.parent
    condition = (
        parent.expression.this if isinstance(parent, exp.Filter) and parent.this is aggregate else None
    )
    function = _FUNCTIONS.get(type(aggregate)) or (
        aggregate.name.upper() if isinstance(aggregate, exp.AnonymousAggFunc) else aggregate.key.upper()
    )
    argument = aggregate.this
    distinct = isinstance(argument, exp.Distinct)
    if isinstance(argument, exp.Distinct):
        argument = argument.expressions[0] if argument.expressions else None
    counting = isinstance(aggregate, (exp.Count, exp.CountIf)) and not distinct
    share: exp.Expr
    if isinstance(aggregate, exp.CountIf) and isinstance(argument, exp.Expr):
        share = exp.Case(
            ifs=[exp.If(this=argument.copy(), true=exp.Literal.number(1))], default=exp.Literal.number(0)
        )
    elif counting and (argument is None or isinstance(argument, exp.Star)):
        share = exp.Literal.number(1)
    elif counting and isinstance(argument, exp.Expr):
        share = exp.Case(
            ifs=[
                exp.If(this=exp.Is(this=argument.copy(), expression=exp.null()), true=exp.Literal.number(0))
            ],
            default=exp.Literal.number(1),
        )
    elif isinstance(argument, exp.Expr) and not isinstance(argument, exp.Star):
        share = argument.copy()
    else:
        share = exp.null()
    if condition is not None:
        share = exp.Case(
            ifs=[exp.If(this=condition.copy(), true=share)],
            default=exp.Literal.number(0) if counting else exp.null(),
        )
    shown = parent if condition is not None and parent is not None else aggregate
    decomposable = function in DECOMPOSABLE and not distinct
    return share, Aggregate(_short(shown, dialect), function, decomposable)


def _coalesced(expression: exp.Expr, select: exp.Select) -> exp.Expr:
    """``expression`` with each column replaced by the first non-null of it and the columns a
    join or filter equates it with, so a row whose join found no match is still matched to the key."""
    edges = _equalities(select, directed=False)

    def replace(column: exp.Column) -> exp.Expr:
        others = [_column(name, table) for table, name in _reachable(_ref(column), edges)]
        return exp.Coalesce(this=column.copy(), expressions=others) if others else column.copy()

    return _replace_columns(expression, replace)


def _pushdown(
    tree: exp.Expr, where: Mapping[str, str]
) -> tuple[dict[str, list[dict[str, str]]], dict[str, str]]:
    """Follow the key's values down the composed query to the source columns that must hold them.

    Returns, for each source placeholder, the column values each of its references needs (one
    mapping per reference reached), and, for a source the key stops short of, the step that reads
    rows of other keys (a window not partitioned by the key, a LIMIT or an aggregate)."""
    found: dict[str, list[dict[str, str]]] = {}
    reasons: dict[str, str] = {}
    _push(_select(tree), {name.lower(): value for name, value in where.items()}, found, reasons)
    return found, reasons


def _push(
    select: exp.Select,
    values: dict[str, str],
    found: dict[str, list[dict[str, str]]],
    reasons: dict[str, str],
) -> None:
    def whole(reason: str) -> None:
        for table in select.find_all(exp.Table):
            reasons.setdefault(table.name, reason)

    model = _model(select)
    if select.args.get('limit') or select.args.get('offset'):
        return whole(f'a LIMIT in {model} reads rows of other keys')
    group = select.args.get('group')
    keys: list[exp.Expr] | None = None
    if group is not None:
        keys = list(group.expressions)
    elif select.args.get('distinct'):
        keys = [p.unalias() for p in select.expressions]
    elif any(_aggregates(p.unalias()) for p in select.expressions):
        keys = []
    pushed: dict[tuple[str, str], str] = {}
    computed: list[str] = []
    for name, value in values.items():
        expression = _projection(select, name)
        if not isinstance(expression, exp.Column):
            computed.append(name)
        elif keys is None or any(expression == k for k in keys):
            pushed[_ref(expression)] = value
    if not pushed:
        if computed:
            # A USING join's key is the COALESCE of both sides' columns, as is an expression.
            whole(
                f'the key {computed[0]} is not a plain column in {model} (it comes from a USING join or '
                'an expression)'
            )
        elif keys is not None and values:
            whole(f'an aggregate in {model} reads rows of other keys')
        return None
    clauses = [*select.expressions, select.args.get('qualify')]
    windows = [w for c in clauses if c is not None for w in c.find_all(exp.Window) if _local(w, c)]
    for window in windows:
        # A window reads every row of its partition: only key columns it partitions by may cut the
        # rows, or a finer key column would drop the partition's other rows from the window.
        partition = {_ref(p) for p in window.args.get('partition_by') or [] if isinstance(p, exp.Column)}
        pushed = {ref: value for ref, value in pushed.items() if ref in partition}
        if not pushed:
            return whole(f'a window function in {model} reads rows of other keys')
    edges = _equalities(select, directed=False)
    reached = dict(pushed)
    for ref, value in pushed.items():
        for other in _reachable(ref, edges):
            reached.setdefault(other, value)
    for item in _items(select):
        alias = item.alias_or_name.lower()
        mine = {column: value for (table, column), value in reached.items() if table == alias}
        if not mine:
            continue
        if isinstance(item, exp.Table) and item.name.startswith('__kp_src'):
            found.setdefault(item.name, []).append(mine)
        elif isinstance(item, exp.Subquery):
            try:
                inner = _select(item.this)
            except _Unsupported:
                continue
            _push(inner, mine, found, reasons)
    return None


def _condition(
    references: list[dict[str, str]], total: int, columns: list[tuple[str, str]], dialect: str
) -> str | None:
    """The copy's WHERE: the rows any reference to the source needs. None, to copy it whole, when
    some reference (in a subquery inside an expression, say) was not reached by the key."""
    names = {name.lower(): name for name, _ in columns}
    if not references or len(references) != total:
        return None
    if any(column not in names for needed in references for column in needed):
        return None
    alternatives = [
        _and(
            [
                exp.EQ(this=exp.column(names[column], quoted=True), expression=exp.Literal.string(value))
                for column, value in needed
            ]
        ).sql(dialect=dialect)
        for needed in dict.fromkeys(tuple(sorted(n.items())) for n in references)
    ]
    return alternatives[0] if len(alternatives) == 1 else ' OR '.join(f'({a})' for a in alternatives)


def _strip(select: exp.Select, *, grouped: bool) -> None:
    for arg in ('order', 'limit', 'offset'):
        select.set(arg, None)
    if grouped:
        for arg in ('group', 'having', 'distinct', 'qualify'):
            select.set(arg, None)


def _close(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is b
    numbers = (int, float, Decimal)
    if isinstance(a, numbers) and isinstance(b, numbers) and not isinstance(a, bool):
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)
    return bool(a == b)


def _shown_columns(graph: Graph, key: str, names: list[str], formula: Mapping[str, set[str]]) -> list[str]:
    """The columns that identify a source's rows (a unique-tested column, or else the first
    id-like one) and those the formula reads."""
    relation = graph.node_for_relation(key)
    keys: list[str] = []
    if relation is not None:
        for column in graph.columns_of(relation.id):
            for edge in graph.in_edges(column.id, 'tests'):
                test = graph.nodes.get(edge.source)
                if test is not None and test.attrs.get('test') == 'unique' and column.name.lower() in names:
                    keys.append(column.name.lower())
    if not keys:
        keys = [n for n in names if id_like(n)][:1]
    read = [n for n in names if n in formula.get(key, set()) and n not in keys]
    return keys + read


def why(
    graph: Graph,
    root: Path,
    warehouse: str | Path | Engine,
    column: Node | str,
    where: Mapping[str, str],
    *,
    limit: int = 50,
    max_rows: int = 1_000_000,
    settings: Settings | None = None,
) -> Why:
    """The source rows behind one value of ``column``: the row of its model that ``where`` picks.

    Runs read-only in verify's sandbox (see the module docstring). The value of a sensitive
    column, as ``settings`` (``[sensitivity]``) and the graph's marks make it, is shown masked:
    in each source row, and the value and its shares when ``column`` is sensitive itself. Raises
    :class:`~konstruct.analysis.query.QueryError` when the column is not built by SQL that verify
    can rerun, the key does not lead to the rows that compute it, or the SQL is of a shape whose
    rows cannot be traced (a UNION, a RIGHT JOIN in the computing step, a recursive CTE)."""
    started = time.perf_counter()
    node = resolve_one(graph, column) if isinstance(column, str) else column
    if node.kind != 'column':
        raise QueryError(f'{graph.label(node)} is a {node.kind}; --why takes a column such as orders.amount')
    where = dict(where)
    if not where:
        raise QueryError('give the row with --where column=value')
    owner = graph.parent_of(node)
    found = {c.node.relation: c for c in candidates(graph, root) if c.node.relation}
    candidate = found.get(owner.relation) if owner is not None and owner.relation else None
    if candidate is None or owner is None:
        name = graph.label(owner) if owner is not None else node.id
        raise QueryError(f'{name} is not built by SQL that verify can rerun (a dbt model or a SQL file)')
    explainer = Explainer(graph)
    explanation = explainer.explain(node)
    formula: dict[str, set[str]] = {}
    for source in explainer.compose(node).sources:
        source_node = graph.nodes[source]
        parent = graph.parent_of(source_node)
        if parent is not None and parent.relation:
            formula.setdefault(parent.relation, set()).add(source_node.name.lower())
    engine = engine_for(warehouse)
    result = Why(
        node.id,
        graph.label(node),
        where,
        engine.label,
        expression=explanation.expression,
        composed=explanation.composed,
        limit=limit,
    )
    sensitive = sensitive_columns(graph, settings)
    try:
        _Run(engine, graph, found, formula, result, max_rows, sensitive).run(candidate, node.name)
    except _Unsupported as error:
        raise QueryError(f'the rows behind {result.label} cannot be traced: the SQL uses {error}') from error
    except SqlglotError as error:
        raise QueryError(
            f'cannot analyse the SQL behind {result.label}: {str(error).splitlines()[0]}'
        ) from error
    finally:
        engine.close()
    # A share is one source row's own value: masked when any column the value is computed from is
    # sensitive, even where the value itself, an aggregate over large groups, is not.
    upstream = [s.node.id for s in graph.trace(node) if s.node.kind == 'column' and s.node.id in sensitive]
    kinds = sorted({k for i in [node.id, *upstream] if i in sensitive for k in sensitive[i]})
    if kinds:
        hidden = mask(kinds)
        for contribution in result.contributions:
            contribution.values = [hidden if v is not None else None for v in contribution.values]
        for aggregate in result.aggregates:
            aggregate.total = hidden if aggregate.total is not None else None
            aggregate.addends = hidden if aggregate.addends is not None else None
        result.masked.insert(0, f"{result.label} (each row's share)")
    if node.id in sensitive:
        result.value = mask(sensitive[node.id]) if result.value is not None else None
        result.masked.insert(0, result.label)
    result.seconds = time.perf_counter() - started
    return result


class _Run:
    def __init__(
        self,
        engine: Engine,
        graph: Graph,
        found: dict[str, Candidate],
        formula: dict[str, set[str]],
        result: Why,
        max_rows: int,
        sensitive: Mapping[str, list[str]],
    ) -> None:
        self.engine = engine
        self.sensitive = sensitive
        self.graph = graph
        self.found = found
        self.formula = formula
        self.result = result
        self.max_rows = max_rows
        self.dialect = engine.dialect
        self.scratch: dict[str, str] = {}

    def run(self, candidate: Candidate, name: str) -> None:
        result, dialect = self.result, self.dialect
        composer = _Composer(self.graph, self.found, self.engine.read_dialect, dialect)
        tree = composer.query(candidate)
        columns = self._qualify(tree, composer.sources)
        _inline_ctes(tree)
        _mark_models(tree)
        _normalise(tree)
        plan = _plan(_select(tree), name, result.where)
        self._copy(tree, composer.sources, columns)
        result.step = _model(plan.anchor)
        rewriter = _Rewriter({v.lower(): k for k, v in self.scratch.items()}, _row_id(dialect), dialect)

        values = self._value(plan, name)
        result.matches = len(values)
        if len(values) > 1:
            raise QueryError(
                f'{len(values)} rows of {_model(plan.top)} match {key_text(result.where)}; '
                'add --where conditions until one row matches'
            )
        result.value = values[0] if values else None
        having = plan.anchor.args.get('having')
        removed = plan.filters + ([f'{result.step}: HAVING {_short(having.this, dialect)}'] if having else [])
        if not values and removed:
            result.notes.append(f'A filter may remove the row: {"; ".join(removed)}.')

        aggregates = _aggregates(plan.expression)
        window = not aggregates and _has_window(plan.expression)
        shares = [_share(a, dialect) for a in aggregates]
        result.aggregates = [s[1] for s in shares]
        if window:
            result.notes.append(
                'The value is computed by a window function, which reads other rows too: it is not '
                'decomposable, and only the source rows of its own row are listed.'
            )
        terms = self._terms(rewriter, plan, [s[0] for s in shares])
        contributing = {source for term in terms.values() for source in term[1]}
        excluded = self._excluded(rewriter, plan, contributing)
        self._report(plan, terms, excluded)

    def _read(self, key: str, index: int, where: str | None) -> tuple[str, list[tuple[str, str]]]:
        try:
            return self.engine.copy(key, index, self.max_rows, where)
        except Exception as error:  # a source missing from the warehouse, or unreadable
            first = str(error).strip().splitlines()[0][:200] if str(error).strip() else type(error).__name__
            raise QueryError(f'cannot read {self._label(key)} from {self.engine.label}: {first}') from error

    def _qualify(self, tree: exp.Expr, sources: dict[str, int]) -> dict[str, list[tuple[str, str]]]:
        """Qualify the composed query's columns against each source's columns, read from an empty
        copy, so the key can be followed down before any row is copied."""
        self.engine.start()
        columns: dict[str, list[tuple[str, str]]] = {}
        for key, index in sources.items():
            _, columns[key] = self._read(key, len(sources) + index, 'FALSE')
        schema: dict[str, object] = {f'__kp_src{index}': dict(columns[key]) for key, index in sources.items()}
        qualify(tree, dialect=self.dialect, schema=schema, quote_identifiers=False)
        return columns

    def _copy(
        self, tree: exp.Expr, sources: dict[str, int], columns: dict[str, list[tuple[str, str]]]
    ) -> None:
        """Copy each source's rows for the key into the sandbox, and point the query at the copies."""
        pushed, reasons = _pushdown(tree, self.result.where)
        references = Counter(t.name for t in tree.find_all(exp.Table) if t.name.startswith('__kp_src'))
        tables: dict[str, str] = {}
        for key, index in sources.items():
            name = f'__kp_src{index}'
            label = self._label(key)
            where = _condition(pushed.get(name, []), references[name], columns[key], self.dialect)
            if where is None and name in reasons:
                self.result.notes.append(
                    f'{label} was read whole, up to {self.max_rows} rows: {reasons[name]}.'
                )
            scratch, _ = self._read(key, index, where)
            self.scratch[key] = scratch
            tables[name] = scratch
            _, rows = self.engine.run(f'SELECT count(*) FROM {scratch}')
            if rows and int(rows[0][0]) >= self.max_rows:
                which = ' for the key' if where else ''
                self.result.notes.append(
                    f'{label} has more than {self.max_rows} rows{which}; only the first {self.max_rows} were '
                    'read, so the value and its rows are those of that sample.'
                )
        for table in list(tree.find_all(exp.Table)):
            if table.name in tables and not table.db:
                replacement = exp.to_table(tables[table.name])
                replacement.set('alias', table.args.get('alias'))
                table.replace(replacement)

    def _label(self, key: str) -> str:
        relation = self.graph.node_for_relation(key)
        return self.graph.label(relation) if relation is not None else key

    def _value(self, plan: _Plan, name: str) -> list[Any]:
        """The value as the model's own query computes it, for the rows the key picks."""
        projection = next(p for p in plan.top.expressions if p.alias_or_name.lower() == name.lower())
        alias = '__kp_t'

        def output(p: exp.Expr) -> exp.Column:
            return exp.column(p.alias_or_name, table=alias, quoted=True)

        conditions = [
            exp.EQ(
                this=output(next(p for p in plan.top.expressions if p.alias_or_name.lower() == k.lower())),
                expression=exp.Literal.string(v),
            )
            for k, v in self.result.where.items()
        ]
        # Printed around the query rather than built on a copy of it: the composed query is large.
        dialect = self.dialect
        sql = (
            f'SELECT {output(projection).sql(dialect=dialect)} FROM ({plan.top.sql(dialect=dialect)}) AS {alias} '
            f'WHERE {_and(conditions).sql(dialect=dialect)} LIMIT 100'
        )
        _, rows = self.engine.run(sql)
        return [row[0] for row in rows]

    def _terms(
        self, rewriter: _Rewriter, plan: _Plan, shares: list[exp.Expr]
    ) -> dict[tuple[Any, ...], tuple[list[Any], list[tuple[str, Any]]]]:
        """The computing step's input rows for the key, each with its shares and source rows."""
        anchor = plan.anchor.copy()
        grouped = _grouped(anchor)
        group = anchor.args.get('group')
        groups = [g.copy() for g in group.expressions] if group is not None else []
        if not shares:
            shares = [plan.expression.copy()]
        anchor.set(
            'expressions',
            [exp.alias_(s, f'__kp_c{t}', quoted=False) for t, s in enumerate(shares)]
            + [exp.alias_(g, f'__kp_a{j}', quoted=False) for j, g in enumerate(groups)]
            + [exp.alias_(k.copy(), f'__kp_w{j}', quoted=False) for j, k in enumerate(plan.keys)],
        )
        _strip(anchor, grouped=grouped)
        rewritten = rewriter.rows(anchor, relaxed=False)
        names, rows = self.engine.run(self._keyed(rewritten.query))
        if len(rows) > MAX_WITNESSES:
            raise QueryError(f'more than {MAX_WITNESSES} rows reach this value; explain a narrower one')
        index = {n: i for i, n in enumerate(names)}
        share_at = [index[f'__kp_c{t}'] for t in range(len(shares))]
        group_at = [index[f'__kp_a{j}'] for j in range(len(groups))]
        identity_at = [index[n.lower()] for n in rewritten.identity]
        provenance = [(rewriter.instances[n], index[f'__kp_r{n}']) for n in rewritten.provenance]
        if len({tuple(row[i] for i in group_at) for row in rows}) > 1:
            raise QueryError(
                f'{key_text(self.result.where)} picks more than one group of {_model(plan.anchor)}; '
                'add --where conditions on the other grouping columns'
            )
        terms: dict[tuple[Any, ...], tuple[list[Any], list[tuple[str, Any]]]] = {}
        for row in rows:
            identity = tuple(row[i] for i in identity_at)
            _, sources = terms.setdefault(identity, ([row[i] for i in share_at], []))
            for key, at in provenance:
                if row[at] is not None and (key, row[at]) not in sources:
                    sources.append((key, row[at]))
        return terms

    def _keyed(self, query: exp.Select, *, excluded: bool = False) -> str:
        """``query``'s rows that hold the key, filtered outside it, so the step's own filters and
        windows see the rows the model's query gives them."""
        alias = '__kp_q'
        conditions: list[exp.Expr] = [
            exp.EQ(this=_column(f'__kp_w{j}', alias), expression=exp.Literal.string(value))
            for j, value in enumerate(self.result.where.values())
        ]
        if excluded:
            conditions.append(exp.Not(this=exp.Is(this=_column('__kp_why', alias), expression=exp.null())))
        wrapped = (
            exp.select(exp.Star())
            .from_(_subquery(query, alias), copy=False)
            .where(_and(conditions), copy=False)
            .limit(MAX_WITNESSES + 1, copy=False)
        )
        return wrapped.sql(dialect=self.dialect)

    def _excluded(
        self, rewriter: _Rewriter, plan: _Plan, contributing: set[tuple[str, Any]]
    ) -> dict[tuple[str, Any], str]:
        """Source rows the key relates to the value that a filter or join removed, with the reason.

        This is the last use of the composed query, so the step is rewritten in place."""
        anchor = plan.anchor
        keys = [_coalesced(k, anchor) for k in plan.keys]
        _strip(anchor, grouped=_grouped(anchor))
        anchor.set('expressions', [exp.alias_(k, f'__kp_w{j}', quoted=False) for j, k in enumerate(keys)])
        try:
            rewritten = rewriter.rows(anchor, relaxed=True)
        except _Unsupported as error:
            self.result.notes.append(f'Rows left out are not traced: the computing step uses {error}.')
            return {}
        self.result.notes += list(dict.fromkeys(rewriter.notes))
        query = self._keyed(rewritten.query, excluded=True)
        try:
            names, rows = self.engine.run(query)
        except Exception as error:  # a warehouse that refuses a full join on this condition
            first = str(error).strip().splitlines()[0][:160] if str(error).strip() else type(error).__name__
            self.result.notes.append(
                f'Rows left out are not traced: the warehouse refused the query ({first}).'
            )
            return {}
        if len(rows) > MAX_WITNESSES:
            self.result.notes.append(f'Rows left out are not traced: more than {MAX_WITNESSES} are related.')
            return {}
        index = {n: i for i, n in enumerate(names)}
        why_at = index['__kp_why']
        provenance = [(rewriter.instances[n], index[f'__kp_r{n}']) for n in rewritten.provenance]
        reasons: dict[tuple[str, Any], str] = {}
        for row in sorted(rows, key=lambda r: (str(r[why_at]), repr(r))):
            for key, at in provenance:
                if row[at] is not None and (key, row[at]) not in contributing:
                    reasons.setdefault((key, row[at]), str(row[why_at]))
        return reasons

    def _rows(self, wanted: set[tuple[str, Any]]) -> dict[tuple[str, Any], SourceRow]:
        """The listed source rows, read back from the sandbox copies."""
        by_key: dict[str, list[Any]] = {}
        for key, rid in wanted:
            by_key.setdefault(key, []).append(rid)
        row_id = _row_id(self.dialect)
        found: dict[tuple[str, Any], SourceRow] = {}
        for key, rids in by_key.items():
            label = self._label(key)
            for start in range(0, len(rids), 500):
                chunk = rids[start : start + 500]
                literals = [
                    exp.Literal.number(r) if isinstance(r, int) else exp.Literal.string(str(r)) for r in chunk
                ]
                query = (
                    exp.select(exp.alias_(row_id('t'), '__kp_rid'), exp.Star())
                    .from_(exp.to_table(self.scratch[key]).as_('t'))
                    .where(exp.In(this=row_id('t'), expressions=literals))
                )
                names, rows = self.engine.run(query.sql(dialect=self.dialect))
                shown = _shown_columns(self.graph, key, names[1:], self.formula)
                hidden = self._hidden(key, names[1:])
                for row in rows:
                    values = dict(zip(names[1:], row[1:], strict=True))
                    for name, kinds in hidden.items():
                        if values.get(name) is not None:
                            values[name] = mask(kinds)
                    found[(key, row[0])] = SourceRow(label, str(row[0]), values, shown)
        return found

    def _hidden(self, key: str, names: Sequence[str]) -> dict[str, list[str]]:
        """The sensitive columns of a source, by the name the sandbox gives them, with their kinds."""
        relation = self.graph.node_for_relation(key)
        if relation is None:
            return {}
        hidden: dict[str, list[str]] = {}
        for name in names:
            column = self.graph.column(relation.id, name)
            if column is not None and column.id in self.sensitive:
                hidden[name] = self.sensitive[column.id]
                label = self.graph.label(column)
                if label not in self.result.masked:
                    self.result.masked.append(label)
        return hidden

    def _report(
        self,
        plan: _Plan,
        terms: dict[tuple[Any, ...], tuple[list[Any], list[tuple[str, Any]]]],
        excluded: dict[tuple[str, Any], str],
    ) -> None:
        result = self.result
        aggregates = result.aggregates
        items = list(terms.values())
        for t, aggregate in enumerate(aggregates):
            column = [values[t] for values, _ in items]
            present = [v for v in column if v is not None]
            function = aggregate.function
            aggregate.count = sum(1 for v in present if v) if function == 'COUNT' else len(present)
            if not aggregate.decomposable:
                continue
            if function == 'SUM':
                aggregate.total = _sum(present)
            elif function == 'COUNT':
                aggregate.total = aggregate.count
            elif function == 'AVG':
                aggregate.addends = _sum(present)
                aggregate.total = None if not present else float(aggregate.addends) / len(present)
            elif present:
                aggregate.total = max(present) if function == 'MAX' else min(present)

        def order(item: tuple[list[Any], list[tuple[str, Any]]]) -> tuple[Any, ...]:
            values, sources = item
            first = values[0] if values else None
            function = aggregates[0].function if aggregates else ''
            tie = repr(sources)
            if first is None or isinstance(first, bool) or not isinstance(first, (int, float, Decimal)):
                return (1, 0.0, repr(first), tie)
            number = float(first)
            if function == 'MIN':
                return (0, number, '', tie)
            if function == 'MAX':
                return (0, -number, '', tie)
            return (0, -abs(number), '', tie)

        items.sort(key=order)
        result.contributors = len(items)
        result.exclusions = len(excluded)
        shown_items = items[: result.limit]
        shown_excluded = sorted(excluded.items(), key=lambda e: (self._label(e[0][0]), repr(e[0][1])))[
            : result.limit
        ]
        wanted = {s for _, sources in shown_items for s in sources} | {k for k, _ in shown_excluded}
        rows = self._rows(wanted)
        for values, sources in shown_items:
            primary = sorted(sources, key=lambda s: (0 if self.formula.get(s[0]) else 1, self._label(s[0])))
            chosen = any(
                a.function in ('MIN', 'MAX')
                and a.decomposable
                and values[t] is not None
                and _close(values[t], a.total)
                for t, a in enumerate(aggregates)
            )
            result.contributions.append(Contribution([rows[s] for s in primary if s in rows], values, chosen))
        result.excluded = [Exclusion(rows[k], reason) for k, reason in shown_excluded if k in rows]
        result.consistent = self._consistent(plan, items)

    def _consistent(self, plan: _Plan, items: list[tuple[list[Any], list[tuple[str, Any]]]]) -> bool | None:
        """Whether the shares give the value: checked where the value is the aggregate itself,
        passed up unchanged."""
        result = self.result
        if result.matches != 1 or not plan.plain or not items:
            return None
        expression = plan.expression
        if isinstance(expression, exp.Coalesce) and not expression.expressions[1:]:
            expression = expression.this
        if isinstance(expression, exp.Filter):
            expression = expression.this
        if not result.aggregates:
            if len(items) != 1:
                return None
            return _close(items[0][0][0], result.value)
        if len(result.aggregates) != 1 or not isinstance(expression, exp.AggFunc):
            return None
        aggregate = result.aggregates[0]
        if not aggregate.decomposable or aggregate.total is None:
            return None
        return _close(aggregate.total, result.value)
