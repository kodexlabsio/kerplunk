"""Data laws: equalities the lineage says the data must obey, proposed from each model's SQL and
confirmed on the warehouse.

Column lineage says which columns a value comes from; the shape of each model's SQL says more.
When a model sums an amount through joins that cannot repeat or drop a row, the total of the
output must equal the total of the input. :func:`propose` reads every model's SQL and proposes
three kinds of law along such paths:

* **conservation**: ``SUM(daily_revenue.revenue) = SUM(raw_payments.amount)``, with the factor
  of a unit conversion and the filters the SQL applies (a ``CASE WHEN`` inside a ``SUM``
  included), followed across models for as long as each step keeps the total; a ``UNION ALL``
  adds up its branches, each followed back to its own source;
* **count**: a model that maps an input one row to one row has as many rows as the input, and a
  ``COUNT`` column adds up to the rows it counts;
* **additivity**: whether a measure may be summed across rows (an amount), across entities but not
  across days (a balance), or not at all (an average or a distinct count).

A join is safe only where something proves it can neither repeat nor drop rows: a key on the
side joined to (a ``unique`` or ``unique_combination_of_columns`` test without a ``where``, a
primary key, or in the same SQL a ``GROUP BY``, ``DISTINCT ON`` or ``QUALIFY ROW_NUMBER() ... = 1``
on it), and, where unmatched rows would be dropped, ``relationships`` and ``not_null`` tests on
the key that must match. A ``FULL`` or ``RIGHT`` join adds the far side's unmatched rows, which
leave sums alone and are counted by ``COUNT(*)``, so it keeps sums and refuses row counts. A path
with any other join, a filter on another relation, ``DISTINCT``, ``LIMIT``, ``HAVING``,
``QUALIFY``, ``UNION``, ``INTERSECT`` or ``EXCEPT``, a window function, a rounding cast or an
integer division proposes no law, and neither does an incremental model; the reason is kept.

:func:`confirm` checks each law in verify's engines, read-only. Conservation and count laws are
compared per day (of a date column the path carries from the source; one partition when there is
none) within a tolerance relative to the day's sum of magnitudes, giving the number of days each
held on; a broken law names the model where the totals first part. Additivity is classified by
probes on a sample of the model's input, rerun through the model's SQL: every input row twice, one
row twice, and the input split into two halves whose totals must add up. The probes can show only
that a column is less additive than it looks, so a column the SQL makes non-additive (or
semi-additive) that they find no more than additive is left unchecked.
:func:`violations` gives the gate the laws that held on every day and that a change no longer
satisfies, each with the model to blame.
"""

from __future__ import annotations

import copy
import datetime
import decimal
import re
import time
from collections import Counter, defaultdict
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope

from konstruct.analysis.conflicts import KEY_WORDS, core
from konstruct.analysis.verify import (
    Candidate,
    DuckDBEngine,
    Engine,
    PostgresEngine,
    SampledEngine,
    candidates,
    engine_for,
    read_only_query,
    rewrite,
)
from konstruct.core import jsonio
from konstruct.core.graph import Graph, Node
from konstruct.core.sql import PARSE_ERRORS, build_schema, case_insensitive, table_key

FORMAT = 1
"""The version of the ``laws.json`` format, raised when a reader of an older one would misread it."""
KINDS = ('conservation', 'count', 'additivity')
REPEATS = (1, 2, 3, 5, 8, 13, 21, 34)
"""The rows (by date) the additivity probe repeats, one at a time, before it gives up on a column:
spread out, so a CASE inside a SUM that leaves out the first rows still meets one it keeps."""
TOLERANCE = 1e-9
"""The default tolerance of a conservation law over floating-point values, relative to the day's sum
of magnitudes (``SUM(ABS(x))``): rounding aside, totals agree to far better than this, and values
that cancel on a day are held to the rounding their size gives, not to their small net. Integer and
decimal totals are compared exactly."""

_NUMERIC = re.compile(
    r'^\s*(U?(TINY|SMALL|BIG|HUGE)?INT(EGER|\d+)?|DECIMAL|NUMERIC|BIGNUMERIC|NUMBER|FLOAT\d*|DOUBLE|REAL|MONEY)\b',
    re.IGNORECASE,
)
_DATE = re.compile(r'^\s*(DATE|DATETIME|TIMESTAMP)', re.IGNORECASE)
_PHYSICAL = re.compile(r'^[\w$]+(\.[\w$]+)+$')
# A composed expression worth parsing for its additivity: one with an aggregate or a window.
_AGGREGATE = re.compile(
    r'\b(SUM|COUNT|AVG|MIN|MAX|MEDIAN|MODE|STDDEV\w*|VAR\w*|APPROX_\w+|PERCENTILE\w*|QUANTILE\w*)\s*\(|\bOVER\b',
    re.IGNORECASE,
)
# Casts that keep every value of a number exactly (or to floating point), so a total survives them.
# FLOAT and REAL are single precision, which rounds a large total.
_EXACT_CASTS = frozenset({exp.DataType.Type.DOUBLE, exp.DataType.Type.BIGDECIMAL})
# Dialects where DECIMAL with no precision keeps any number exactly. Elsewhere it has a default
# scale (DuckDB's DECIMAL(18, 3), Spark's DECIMAL(10, 0), Snowflake's NUMBER(38, 0)) and rounds.
_EXACT_DECIMAL = frozenset({'postgres'})
# Warehouses where a primary key is informational: declared, never checked.
_INFORMATIONAL_KEYS = frozenset({'snowflake', 'bigquery', 'redshift', 'databricks', 'spark'})
# Projections that return several rows for each input row.
_SET_RETURNING = (
    exp.Explode,
    exp.ExplodeOuter,
    exp.Posexplode,
    exp.Inline,
    exp.GenerateSeries,
    exp.Unnest,
    exp.Stack,
)
_SET_RETURNING_NAMES = frozenset(
    {
        'unnest',
        'explode',
        'explode_outer',
        'posexplode',
        'posexplode_outer',
        'inline',
        'inline_outer',
        'generate_series',
        'generate_subscripts',
        'json_array_elements',
        'json_array_elements_text',
        'jsonb_array_elements',
        'jsonb_array_elements_text',
        'json_each',
        'json_each_text',
        'jsonb_each',
        'jsonb_each_text',
        'json_object_keys',
        'jsonb_object_keys',
        'json_to_recordset',
        'jsonb_to_recordset',
        'json_populate_recordset',
        'jsonb_populate_recordset',
        'jsonb_path_query',
        'regexp_matches',
        'regexp_split_to_table',
        'string_to_table',
        'flatten',
        'stack',
    }
)
# Values that change from one run to the next, so a filter on them selects different rows each time.
_VOLATILE = (exp.Rand, exp.CurrentDate, exp.CurrentTimestamp, exp.CurrentTime, exp.CurrentDatetime, exp.Uuid)
_VOLATILE_NAMES = frozenset(
    {
        'now',
        'random',
        'getdate',
        'sysdate',
        'systimestamp',
        'localtimestamp',
        'localtime',
        'clock_timestamp',
        'statement_timestamp',
        'transaction_timestamp',
        'today',
        'gen_random_uuid',
        'newid',
        'uuid',
    }
)
# What reading a model's SQL can raise, beyond parsing: sqlglot's qualifier on odd input.
_ANALYSIS_ERRORS: tuple[type[Exception], ...] = (*PARSE_ERRORS, KeyError, IndexError, LookupError)


# Results


@dataclass(frozen=True)
class Step:
    """One model's part in a law: how a total of its output follows from a total of one input."""

    model: str
    """The model or table whose SQL makes the step, by graph id."""
    column: str | None
    """Its column, or None for its row count."""
    input: str
    """The relation the step reads, by graph id."""
    input_column: str | None
    """The input's column, or None for its row count."""
    op: str
    """``sum``: SUM(column) = factor * SUM(input column); ``count``: SUM(column) = factor *
    COUNT(input column, or rows); ``rows``: the output has one row for each input row."""
    factor: float = 1.0
    """The constant the SQL multiplies by, such as 0.01 for cents to units."""
    filter: str | None = None
    """The input rows that count, as SQL over the input's columns."""
    partition: tuple[str, str] | None = None
    """The day each row falls on, as SQL over the output's columns and over the input's."""
    relies_on: tuple[str, ...] = ()
    """The tests that make the step's joins safe, by graph id."""
    branch: int = 0
    """The branch of the model's ``UNION ALL`` the step reads, from 0; 0 for any other query."""

    def same(self, other: Step) -> bool:
        """Whether ``other`` makes the same claim (tests and partitions aside)."""
        return (
            self.model,
            self.column,
            self.branch,
            self.input,
            self.input_column,
            self.op,
            self.filter,
        ) == (
            other.model,
            other.column,
            other.branch,
            other.input,
            other.input_column,
            other.op,
            other.filter,
        ) and abs(self.factor - other.factor) <= 1e-12 * max(1.0, abs(self.factor))

    def to_dict(self) -> dict[str, Any]:
        return {
            'model': self.model,
            'column': self.column,
            'input': self.input,
            'input_column': self.input_column,
            'op': self.op,
            'factor': self.factor,
            'filter': self.filter,
            'partition': list(self.partition) if self.partition else None,
            'relies_on': list(self.relies_on),
            'branch': self.branch,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Step:
        partition = data.get('partition')
        return cls(
            model=data['model'],
            column=data.get('column'),
            input=data['input'],
            input_column=data.get('input_column'),
            op=data['op'],
            factor=float(data.get('factor', 1.0)),
            filter=data.get('filter'),
            partition=(str(partition[0]), str(partition[1])) if partition else None,
            relies_on=tuple(data.get('relies_on', [])),
            branch=int(data.get('branch', 0)),
        )


@dataclass
class Failure:
    """One partition on which a law did not hold."""

    partition: str
    """The day (``2026-01-31``), ``null`` for rows without one, or ``all``."""
    expected: float
    """The total the source gives."""
    actual: float
    """The total the relation has."""


@dataclass(frozen=True)
class Term:
    """One source of a law's total: ``factor`` times the total of a source column. A law over a
    ``UNION ALL`` has one term for each branch; any other law has one."""

    source: str
    """The relation the total comes from (for additivity, the input the probes change), by graph id."""
    source_label: str
    """Its short name."""
    source_column: str | None = None
    """The source's column, or None for its row count."""
    op: str = 'sum'
    """``sum``: the term totals the source column; ``count``: it counts the column's values (or,
    with no column, the rows). The branches of a ``UNION ALL`` can differ."""
    factor: float = 1.0
    """The constant between the two totals."""
    filter: str | None = None
    """The source rows that count, as SQL over the source's columns."""
    partition: str | None = None
    """The day each source row falls on, as SQL over the source's columns, when the law has one."""
    steps: tuple[Step, ...] = ()
    """The models the term runs through, from the law's relation back to the source."""

    def text(self) -> str:
        """The term in a statement, such as ``SUM(raw.payments.amount) / 100``."""
        if self.op == 'sum':
            total = f'SUM({self.source_label}.{self.source_column})'
        elif self.source_column:
            total = f'COUNT({self.source_label}.{self.source_column})'
        else:
            total = f'COUNT(*) of {self.source_label}'
        return f'{total}{_factor_text(self.factor)}'

    def to_dict(self) -> dict[str, Any]:
        return {
            'source': self.source,
            'source_label': self.source_label,
            'source_column': self.source_column,
            'op': self.op,
            'factor': self.factor,
            'filter': self.filter,
            'partition': self.partition,
            'steps': [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Term:
        return cls(
            source=data['source'],
            source_label=data.get('source_label', data['source']),
            source_column=data.get('source_column'),
            op=str(data.get('op', 'sum')),
            factor=float(data.get('factor', 1.0)),
            filter=data.get('filter'),
            partition=data.get('partition'),
            steps=tuple(Step.from_dict(s) for s in data.get('steps', [])),
        )


@dataclass
class Law:
    """An equality the data must obey, with the result of checking it once checked."""

    id: str
    """``kind:relation id#column``, or ``count:relation id`` for a row count."""
    kind: str
    """``conservation`` (some term sums), ``count`` (every term counts) or ``additivity``."""
    relation: str
    """The relation the law is about, by graph id."""
    label: str
    """Its short name."""
    column: str | None
    """Its column, or None for its row count."""
    terms: list[Term] = field(default_factory=list)
    """What the total equals: the sum of the terms. For additivity, one term: the input the probes
    change."""
    partition: str | None = None
    """The day each row falls on, as SQL over the relation's columns; each term has the same day
    over its source's."""
    expected: str | None = None
    """For additivity, what the SQL suggests: additive, semi-additive or non-additive."""
    status: str = 'proposed'
    """``proposed``, ``holds``, ``broken`` or ``unchecked``."""
    held: int = 0
    """Partitions (or, for additivity, probe sets) on which the law held."""
    checked: int = 0
    """Partitions (or probe sets) checked."""
    failures: list[Failure] = field(default_factory=list)
    """The first partitions on which it did not hold."""
    broken_at: str | None = None
    """For a broken law, the model where the totals first part, by graph id."""
    classification: str | None = None
    """For additivity, what the probes found."""
    evidence: str | None = None
    """For additivity, what the probes did and saw."""
    note: str | None = None
    """Why the law was not checked, when it was not."""
    unprobed: str | None = None
    """For additivity, why the probes cannot classify the column, read from its SQL: a model that
    keeps rows by comparing them with others (DISTINCT, QUALIFY, LIMIT, HAVING or a filter with a
    subquery) keeps different rows once its input is repeated or split."""

    @property
    def steps(self) -> list[Step]:
        """Every step of every term, each once, in the order the terms reach them."""
        return list(dict.fromkeys(step for term in self.terms for step in term.steps))

    @property
    def relies_on(self) -> list[str]:
        """Every test the law's joins depend on."""
        return sorted({t for step in self.steps for t in step.relies_on})

    @property
    def always_held(self) -> bool:
        """Whether the law was checked and held on every partition."""
        return self.status == 'holds' and self.checked > 0 and self.held == self.checked

    @property
    def statement(self) -> str:
        """The law in one line."""
        if self.kind == 'additivity':
            meaning = {
                'additive': 'is additive: it may be summed across rows',
                'semi-additive': 'is semi-additive: it may be summed across entities, not across days',
                'non-additive': 'is non-additive: its values may not be summed',
            }
            found = self.classification or self.expected or 'additive'
            return f'{self.label}.{self.column} {meaning[found]}'
        left = f'SUM({self.label}.{self.column})' if self.column else f'COUNT(*) of {self.label}'
        if len(self.terms) == 1:
            [term] = self.terms
            text = f'{left} = {term.text()}' + (f' where {term.filter}' if term.filter else '')
        else:
            parts = [
                f'({term.text()} where {term.filter})' if term.filter else term.text() for term in self.terms
            ]
            text = f'{left} = {" + ".join(parts)}'
        if self.partition:
            text += f', per day of {self.label}.{self.partition}'
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            'id': self.id,
            'kind': self.kind,
            'statement': self.statement,
            'relation': self.relation,
            'label': self.label,
            'column': self.column,
            'partition': self.partition,
            'terms': [term.to_dict() for term in self.terms],
            'expected': self.expected,
            'status': self.status,
            'held': self.held,
            'checked': self.checked,
            'failures': [
                {'partition': f.partition, 'expected': f.expected, 'actual': f.actual} for f in self.failures
            ],
            'broken_at': self.broken_at,
            'classification': self.classification,
            'evidence': self.evidence,
            'note': self.note,
            'unprobed': self.unprobed,
            'relies_on': self.relies_on,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Law:
        return cls(
            id=data['id'],
            kind=data['kind'],
            relation=data['relation'],
            label=data.get('label', data['relation']),
            column=data.get('column'),
            terms=[Term.from_dict(t) for t in data.get('terms', [])],
            partition=data.get('partition'),
            expected=data.get('expected'),
            status=data.get('status', 'proposed'),
            held=int(data.get('held', 0)),
            checked=int(data.get('checked', 0)),
            failures=[
                Failure(str(f['partition']), float(f['expected']), float(f['actual']))
                for f in data.get('failures', [])
            ],
            broken_at=data.get('broken_at'),
            classification=data.get('classification'),
            evidence=data.get('evidence'),
            note=data.get('note'),
            unprobed=data.get('unprobed'),
        )


@dataclass
class Blocked:
    """A measure column that has no law, and why."""

    model: str
    """The model, by graph id."""
    column: str | None
    """The column, or None for the model's row count."""
    reason: str
    """What in the SQL stops the law, such as a join on a key no uniqueness test covers."""
    measure: bool = True
    """Whether the column is a measure (numeric and not a key), which would have a law of its own."""


@dataclass
class Laws:
    """The laws of a project, proposed and, once confirmed, checked."""

    laws: list[Law] = field(default_factory=list)
    """Every law, ordered by relation and column."""
    blocked: list[Blocked] = field(default_factory=list)
    """Every column (and row count) of a model with no step, with the reason. Kept in memory, not
    in laws.json."""
    steps: dict[tuple[str, str | None], tuple[Step, ...]] = field(default_factory=dict)
    """Every step the SQL proves, by model id and column (None for the row count), one for each
    branch of a ``UNION ALL``, whether or not a law starts at it. Kept in memory, not in laws.json."""
    dialect: str | None = None
    """The SQL dialect of the filters and partitions."""
    warehouse: str | None = None
    """What the laws were checked against, once confirmed."""
    tolerance: float = TOLERANCE
    """The relative tolerance of conservation laws."""
    seconds: float = 0.0
    """Time taken."""

    def models(self) -> set[str]:
        """The models the laws run through (or, for additivity, are about), by graph id."""
        found = {step.model for law in self.laws for step in law.steps}
        return found | {law.relation for law in self.laws if law.kind == 'additivity'}

    def law(self, law_id: str) -> Law | None:
        """The law with this id."""
        return next((law for law in self.laws if law.id == law_id), None)

    def totals(self) -> dict[str, int]:
        """Laws by status and by kind."""
        counts = Counter(law.status for law in self.laws)
        kinds = Counter(law.kind for law in self.laws)
        return {
            'laws': len(self.laws),
            **{status: counts.get(status, 0) for status in ('proposed', 'holds', 'broken', 'unchecked')},
            **{kind: kinds.get(kind, 0) for kind in KINDS},
            'blocked': sum(1 for b in self.blocked if b.measure),
        }

    def summary(self) -> str:
        """One line on what was found."""
        t = self.totals()
        if not t['laws']:
            return 'No law follows from the lineage.'
        plural = 's' if t['laws'] != 1 else ''
        if self.warehouse is None:
            return (
                f'{t["laws"]} law{plural} follow from the lineage ({t["conservation"]} conservation, '
                f'{t["count"]} count, {t["additivity"]} additivity); none checked yet.'
            )
        return (
            f'{t["laws"]} law{plural} checked against {self.warehouse}: {t["holds"]} held on every partition, '
            f'{t["broken"]} broken, {t["unchecked"]} not checked.'
        )

    def __str__(self) -> str:
        lines = [self.summary()]
        if self.laws:
            lines.append('')
        for law in self.laws:
            detail = ''
            if law.status in ('holds', 'broken') and law.kind != 'additivity':
                unit = 'days' if law.partition else 'partitions'
                detail = f'  ({law.held} of {law.checked} {unit})'
            if law.broken_at:
                detail += f'; parts at {_short(law.broken_at)}'
            if law.status == 'unchecked' and law.note:
                detail = f'  ({law.note})'
            lines.append(f'  {law.status:<9} {law.statement}{detail}')
            for failure in law.failures[:3]:
                lines.append(
                    f'            {failure.partition}: {_number_text(failure.actual)} '
                    f'against {_number_text(failure.expected)}'
                )
            if law.kind == 'additivity' and law.evidence and law.status == 'broken':
                lines.append(f'            {law.evidence}')
        return '\n'.join(lines)

    def to_dict(self) -> dict[str, Any]:
        """The ``laws.json`` document."""
        return {
            'format': FORMAT,
            'dialect': self.dialect,
            'warehouse': self.warehouse,
            'tolerance': self.tolerance,
            'seconds': round(self.seconds, 2),
            'totals': self.totals(),
            'laws': [law.to_dict() for law in self.laws],
        }

    def write(self, path: Path) -> None:
        """Write ``laws.json``."""
        jsonio.write_text(path, jsonio.dumps(self.to_dict(), indent=True) + '\n')

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Laws:
        version = data.get('format')
        if not isinstance(version, int) or isinstance(version, bool):
            raise ValueError('this is not a laws.json: it has no format number')
        if version > FORMAT:
            raise ValueError(f'laws.json format {version} is newer than this Konstruct reads ({FORMAT})')
        return cls(
            laws=[Law.from_dict(item) for item in data.get('laws', [])],
            dialect=data.get('dialect'),
            warehouse=data.get('warehouse'),
            tolerance=float(data.get('tolerance', TOLERANCE)),
            seconds=float(data.get('seconds', 0.0)),
        )


def load(path: Path) -> Laws:
    """Read a ``laws.json`` written by :meth:`Laws.write`."""
    return Laws.from_dict(jsonio.loads(path.read_bytes()))


@dataclass
class Violation:
    """A law that held on every partition and that a change no longer satisfies."""

    law: str
    """The law's id."""
    statement: str
    """The law in one line."""
    model: str
    """The model the change breaks it in, by graph id."""
    label: str
    """The model's short name."""
    message: str
    """What changed."""
    path: str | None = None
    """The model's file, relative to the project, when known."""

    def __str__(self) -> str:
        return f'{self.statement}: {self.message}'

    def to_dict(self) -> dict[str, Any]:
        return {
            'law': self.law,
            'statement': self.statement,
            'model': self.model,
            'label': self.label,
            'message': self.message,
            'path': self.path,
        }


def _short(node_id: str) -> str:
    return node_id.rsplit('.', 1)[-1]


def _factor_text(factor: float) -> str:
    if factor == 1:
        return ''
    if factor == -1:
        return ' * -1'
    inverse = 1 / factor
    if abs(inverse - round(inverse)) < 1e-9 and abs(inverse) > 1:
        return f' / {round(inverse)}'
    return f' * {factor:g}'


def _number_text(value: float) -> str:
    return f'{value:.0f}' if value == int(value) and abs(value) < 1e15 else f'{value:.6g}'


# Static analysis of one model's SQL


@dataclass
class _Path:
    """How the rows of a query relate to the rows of one relation it reads, its base."""

    base: str
    """The base's relation key."""
    mode: str
    """``map``: one query row per base row that passes the filters; ``agg``: base rows grouped."""
    filters: tuple[exp.Expr, ...]
    """The base rows that reach the query, over the base's (unqualified) columns."""
    carried: dict[str, exp.Expr] | None
    """Query columns computed row by row from the base (or grouping keys of it), as expressions
    over the base's columns. None for a table: every column is itself."""
    tests: frozenset[str]
    """The tests the path's joins depend on."""


@dataclass
class _Flow:
    """How the total of a query column follows from a total of its base."""

    op: str
    """``sum``: SUM(column) = factor * SUM(base column); ``count``: = factor * COUNT(base column)."""
    column: str | None
    """The base column, or None for COUNT(*)."""
    factor: float
    path: _Path


@dataclass
class _Local:
    """What one SELECT does to the rows of one of its sources."""

    mode: str
    filters: list[exp.Expr]
    """WHERE conditions on the source's columns, qualified with its alias."""
    keys: list[exp.Expr]
    """GROUP BY expressions."""
    tests: frozenset[str]
    kept: str | None = None
    """A relation whose unmatched rows a FULL or RIGHT join keeps, adding rows no source row has."""


@dataclass(frozen=True)
class _Joins:
    """What the joins of one SELECT do to the rows of one of its sources."""

    tests: frozenset[str]
    """The tests that prove each source row gives exactly one row (at most one, for a key)."""
    kept: str | None = None
    """A relation whose unmatched rows a FULL or RIGHT join keeps: rows with no source row, which a
    sum of the source's columns leaves alone and a row count counts."""


@dataclass
class _Term:
    """A projection read as a linear function of one source column, possibly aggregated."""

    aggregate: str | None
    """None (row by row), ``sum`` or ``count``."""
    alias: str | None
    """The source, or None for COUNT(*)."""
    column: str | None
    factor: float
    conditions: list[exp.Expr]
    """CASE or IF conditions under which the value counts."""


@dataclass
class _Link:
    """A step of one model in graph terms, with expressions still as trees."""

    model: Node
    column: str | None
    input: Node
    input_column: str | None
    op: str
    factor: float
    filters: tuple[exp.Expr, ...]
    carried: dict[str, exp.Expr]
    tests: frozenset[str]
    branch: int = 0


class _Tests:
    """The graph's keys and its not-null and relationships tests.

    A key is a set of columns that holds each combination of values once: a ``unique`` test, a
    ``unique_combination_of_columns`` test, or a primary key the warehouse enforces. A test with a
    ``where`` holds only on the rows it selects, so it proves nothing about the whole relation: its
    key is kept apart, to name in a reason, and its other tests are left out. A dbt constraint is
    enforced only under an enforced contract, and Snowflake, BigQuery, Redshift and Databricks
    never enforce a primary key.
    """

    def __init__(self, graph: Graph) -> None:
        self.keys: dict[str, list[tuple[frozenset[str], str]]] = defaultdict(list)
        self.filtered: dict[str, list[tuple[frozenset[str], str]]] = defaultdict(list)
        self.not_null: dict[tuple[str, str], str] = {}
        self.relationships: dict[tuple[str, str], dict[tuple[str, str], str]] = defaultdict(dict)
        enforcing = str(graph.meta.get('dialect') or 'duckdb') not in _INFORMATIONAL_KEYS
        for node in graph.nodes.values():
            if node.kind != 'test':
                primary = [str(c).lower() for c in node.attrs.get('primary_key') or []]
                enforced = 'dbt' not in node.origins or node.attrs.get('primary_key_enforced')
                if primary and enforcing and enforced:
                    self.keys[node.id].append((frozenset(primary), f'{node.id}:primary_key'))
                continue
            where = bool(node.attrs.get('where'))
            keys = self.filtered if where else self.keys
            kind = str(node.attrs.get('test') or '')
            edges = graph.out_edges(node.id, 'tests')
            targets = [
                self._column(graph, e.target) for e in edges if e.attrs.get('role') == 'relationship target'
            ]
            for edge in edges:
                if edge.attrs.get('role') == 'relationship target':
                    continue
                if kind == 'unique_combination_of_columns':
                    columns = node.attrs.get('columns')
                    relation = self._relation(graph, edge.target)
                    if relation and isinstance(columns, list) and columns:
                        keys[relation].append((frozenset(str(c).lower() for c in columns), node.id))
                    continue
                key = self._column(graph, edge.target, node.attrs.get('column'))
                if key is None:
                    continue
                if kind == 'unique':
                    keys[key[0]].append((frozenset({key[1]}), node.id))
                elif where:
                    continue
                elif kind == 'not_null':
                    self.not_null.setdefault(key, node.id)
                elif kind == 'relationships':
                    for target in targets:
                        if target is not None:
                            self.relationships[key].setdefault(target, node.id)

    def key(self, relation: str, columns: Collection[str], *, filtered: bool = False) -> str | None:
        """The test (or primary key) proving that ``columns`` of ``relation`` hold each combination of
        values at most once: a key they include, tests before primary keys, then the narrowest.
        With ``filtered``, the unique test with a ``where`` that would, but for its ``where``."""
        given = set(columns)
        found = [
            (proof.endswith(':primary_key'), len(k), proof)
            for k, proof in (self.filtered if filtered else self.keys).get(relation, [])
            if k <= given
        ]
        return min(found)[2] if found else None

    def single(self) -> set[tuple[str, str]]:
        """The columns that are keys on their own."""
        return {
            (relation, next(iter(k))) for relation, keys in self.keys.items() for k, _ in keys if len(k) == 1
        }

    @staticmethod
    def _relation(graph: Graph, node_id: str) -> str | None:
        node = graph.nodes.get(node_id)
        if node is not None and node.kind == 'column':
            return node.parent
        return node.id if node is not None else None

    @staticmethod
    def _column(graph: Graph, node_id: str, column: Any = None) -> tuple[str, str] | None:
        """The column a test edge points at; a test on a column the manifest does not list points
        at its relation, with the column named on the test."""
        node = graph.nodes.get(node_id)
        if node is not None and node.kind != 'column' and isinstance(column, str) and column:
            return node.id, column.lower()
        if node is None or node.kind != 'column' or not node.parent:
            return None
        return node.parent, node.name.lower()


def _calls(expression: exp.Expr, classes: tuple[type[exp.Expr], ...], names: frozenset[str]) -> bool:
    """Whether ``expression`` calls one of ``classes``, or a function sqlglot does not know by one of
    ``names``, outside a subquery."""
    for node in expression.walk(
        prune=lambda n: isinstance(n, (exp.Subquery, exp.Select)) and n is not expression
    ):
        if isinstance(node, classes):
            return True
        if isinstance(node, exp.Anonymous) and str(node.this).lower() in names:
            return True
    return False


def _pivoted(source: exp.Expr, *, unpivot: bool = False) -> bool:
    """Whether a table or subquery in a FROM clause is pivoted (with ``unpivot``, unpivoted)."""
    return any(p.args.get('unpivot') or not unpivot for p in source.args.get('pivots') or [])


def _volatile(expression: exp.Expr) -> bool:
    return _calls(expression, _VOLATILE, _VOLATILE_NAMES)


def _conjuncts(condition: exp.Expr) -> list[exp.Expr]:
    while isinstance(condition, exp.Paren):
        condition = condition.this
    if isinstance(condition, exp.And):
        return [*_conjuncts(condition.this), *_conjuncts(condition.expression)]
    return [condition]


def _unalias(expression: exp.Expr | None) -> exp.Expr | None:
    return expression.this if isinstance(expression, exp.Alias) else expression


def _aggregates(expression: exp.Expr) -> bool:
    """Whether ``expression`` aggregates its query's rows: an aggregate outside a window, and not in
    a scalar subquery, which aggregates rows of its own."""
    return any(
        isinstance(node, exp.AggFunc) and node.find_ancestor(exp.Window) is None
        for node in expression.walk(
            prune=lambda n: isinstance(n, (exp.Subquery, exp.Select)) and n is not expression
        )
    )


def _grouping_sets(group: exp.Group) -> bool:
    return any(group.args.get(a) for a in ('rollup', 'cube', 'grouping_sets')) or any(
        isinstance(e, (exp.Rollup, exp.Cube, exp.GroupingSets)) for e in group.expressions
    )


def _row_level(expression: exp.Expr) -> bool:
    return expression.find(exp.AggFunc, exp.Window, exp.Subquery, exp.Select, exp.Star) is None


def _number(expression: exp.Expr) -> float | None:
    negative = False
    while isinstance(expression, (exp.Paren, exp.Neg)):
        negative ^= isinstance(expression, exp.Neg)
        expression = expression.this
    if isinstance(expression, exp.Literal) and not expression.is_string:
        try:
            value = float(expression.this)
        except ValueError:
            return None
        return -value if negative else value
    return None


def _is_zero_or_null(expression: exp.Expr) -> bool:
    return isinstance(expression, exp.Null) or _number(expression) == 0


def _exact_cast(to: exp.DataType, dialect: str) -> bool:
    if to.this == exp.DataType.Type.DECIMAL:
        return not to.expressions and dialect in _EXACT_DECIMAL
    return to.this in _EXACT_CASTS


def _unwrap(expression: exp.Expr, dialect: str) -> tuple[exp.Expr, float, list[exp.Expr]] | str:
    """Strip what keeps a total: aliases, exact casts, ``COALESCE(x, 0)``, constant factors and
    ``CASE WHEN p THEN x ELSE 0 END``. Returns what is left, the factor and the conditions, or why
    the expression does not keep a total."""
    factor = 1.0
    conditions: list[exp.Expr] = []
    while True:
        if isinstance(expression, (exp.Alias, exp.Paren)):
            expression = expression.this
        elif isinstance(expression, exp.Cast):
            to = expression.to
            if not _exact_cast(to, dialect):
                return f'a cast to {to.sql(dialect=dialect)}, which can round'
            expression = expression.this
        elif isinstance(expression, exp.Coalesce):
            rest = expression.expressions
            if len(rest) != 1 or _number(rest[0]) != 0:
                return 'a COALESCE with a value other than 0'
            expression = expression.this
        elif isinstance(expression, exp.Neg):
            factor, expression = -factor, expression.this
        elif isinstance(expression, exp.Mul):
            number, other = _number(expression.expression), expression.this
            if number is None:
                number, other = _number(expression.this), expression.expression
            if number is None:
                return 'a product of two columns'
            factor, expression = factor * number, other
        elif isinstance(expression, exp.Div):
            number = _number(expression.expression)
            if not number:
                return 'a division by a column'
            literal = expression.expression
            if expression.args.get('typed') and not (
                isinstance(literal, exp.Literal) and '.' in literal.this
            ):
                return 'an integer division, which rounds'
            factor, expression = factor / number, expression.this
        elif isinstance(expression, exp.Case):
            ifs = expression.args.get('ifs') or []
            default = expression.args.get('default')
            if (
                expression.this is not None
                or len(ifs) != 1
                or (default is not None and not _is_zero_or_null(default))
            ):
                return 'a CASE expression with more than one outcome'
            conditions.append(ifs[0].this)
            expression = ifs[0].args['true']
        elif isinstance(expression, exp.If):
            false = expression.args.get('false')
            if false is not None and not _is_zero_or_null(false):
                return 'an IF expression with more than one outcome'
            conditions.append(expression.this)
            expression = expression.args['true']
        else:
            return expression, factor, conditions


_CHANGING = 'a condition on a value that changes between runs, such as the current date'


def _term(expression: exp.Expr, dialect: str) -> _Term | str:
    unwrapped = _unwrap(expression, dialect)
    if isinstance(unwrapped, str):
        return unwrapped
    if any(_volatile(c) for c in unwrapped[2]):
        return _CHANGING
    found, factor, conditions = unwrapped
    if isinstance(found, exp.Column):
        if not found.table:
            return 'a column of no known relation'
        return _Term(None, found.table, found.name.lower(), factor, conditions)
    if isinstance(found, exp.Window):
        return 'a window function'
    if not isinstance(found, exp.AggFunc):
        return f'a value computed with {found.key.upper()}'
    if conditions:
        return 'a condition on an aggregate'
    argument = found.this
    if isinstance(found, exp.Sum):
        if isinstance(argument, exp.Distinct):
            return 'SUM(DISTINCT ...), which does not add up'
        inner = _unwrap(argument, dialect)
        if isinstance(inner, str):
            return inner
        column, inner_factor, inner_conditions = inner
        if any(_volatile(c) for c in inner_conditions):
            return _CHANGING
        if not isinstance(column, exp.Column) or not column.table:
            return 'a SUM of a computed value'
        return _Term('sum', column.table, column.name.lower(), factor * inner_factor, inner_conditions)
    if isinstance(found, exp.Count):
        if argument is None or isinstance(argument, exp.Star):
            return _Term('count', None, None, factor, [])
        if isinstance(argument, exp.Column) and argument.table:
            return _Term('count', argument.table, argument.name.lower(), factor, [])
        if isinstance(argument, exp.Distinct):
            return 'COUNT(DISTINCT ...), which does not add up'
        return 'a COUNT of a computed value'
    return f'{found.key.upper()}, which does not add up'


def _suggests(expression: exp.Expr, dialect: str) -> str | None:
    """What a projection's SQL suggests about its additivity, before any probe."""
    unwrapped = _unwrap(expression, dialect)
    found = expression if isinstance(unwrapped, str) else unwrapped[0]
    found = _unalias(found) or found
    if isinstance(found, exp.Window):
        inner = found.this
        if isinstance(inner, (exp.Sum, exp.Count)) and found.args.get('order') is not None:
            return 'semi-additive'
        return 'non-additive' if isinstance(inner, exp.AggFunc) else None
    if isinstance(found, (exp.Sum, exp.Count)):
        return 'non-additive' if isinstance(found.this, exp.Distinct) else 'additive'
    if isinstance(found, exp.AggFunc):
        return 'non-additive'
    if isinstance(found, (exp.Div, exp.Mul)) and _aggregates(found):
        return 'non-additive'
    return None


class _Analyser:
    """Reads models' SQL into steps: how each column's total, and each model's row count, follows
    from one input."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        self.dialect = str(graph.meta.get('dialect') or 'duckdb')
        self.tests = _Tests(graph)
        # Qualifying a query needs the columns' names, not their types (which cost a parse each).
        known: dict[str, dict[str, str]] = {
            node.relation: {c.name.lower(): 'UNKNOWN' for c in graph.columns_of(node.id)}
            for node in graph.relations()
            if node.relation
        }
        self.schema = build_schema(known, self.dialect)
        self.sql_dialect = self.dialect
        self._reset(None, None)

    def _reset(self, catalog: str | None, schema: str | None) -> None:
        self.catalog, self.db = catalog, schema
        # Keyed by id() of scopes, so cleared for every model.
        self._flows: dict[tuple[int, str], _Flow | str] = {}
        self._locals: dict[tuple[int, str], _Local | str] = {}
        self._rows_cache: dict[int, _Path | str] = {}
        self._composed: dict[tuple[int, str, int], _Path | str] = {}
        self._projections_cache: dict[int, dict[str, exp.Expr]] = {}
        self._identities: dict[str, _Path] = {}

    # One model

    def hopeless(self, node: Node, sql: str) -> str | None:
        """Why no column of ``node`` can have a step, without parsing the SQL again: an incremental
        model (each run adds to or merges into the rows already there, so its totals are not its
        SQL's over the input), or, from the conditions the build recorded, a join on equal columns
        where neither side's columns include a key. A model that groups (a grouped query can be
        the unique side of a join) or uses DISTINCT or QUALIFY is left to the full analysis."""
        if node.attrs.get('materialized') == 'incremental':
            return 'an incremental model keeps rows its input no longer has'
        conditions = [str(c) for c in node.attrs.get('conditions') or []]
        lowered = sql.lower()
        if (
            any(c.startswith('GROUP BY: ') for c in conditions)
            or 'distinct' in lowered
            or 'qualify' in lowered
        ):
            return None
        for condition in conditions:
            head, _, body = condition.partition(': ')
            if not head.endswith('JOIN ON'):
                continue
            equalities = [part.split(' = ') for part in body.split(' AND ')]
            if any(len(e) != 2 or not all(_PHYSICAL.match(side) for side in e) for e in equalities):
                continue
            sides: dict[str, set[str]] = defaultdict(set)
            for side in (side for e in equalities for side in e):
                relation, _, name = side.rpartition('.')
                sides[relation].add(name)
            if not any(self._keyed(relation, names) for relation, names in sides.items()):
                if any(self._keyed(relation, names, filtered=True) for relation, names in sides.items()):
                    return (
                        f'joins on {body}, where the only uniqueness test has a where, so holds on some rows'
                    )
                return f'joins on {body}, where no uniqueness test covers either side'
        return None

    def _keyed(self, relation: str, columns: set[str], *, filtered: bool = False) -> bool:
        node = self.graph.node_for_relation(relation)
        return node is not None and self.tests.key(node.id, columns, filtered=filtered) is not None

    def model(self, candidate: Candidate) -> tuple[dict[str, list[_Flow] | str], list[_Path] | str]:
        """Each output column's flows (or why it has none) and the row paths: one of each for every
        branch of a ``UNION ALL``, one for any other query."""
        self._reset(candidate.default_catalog, candidate.default_schema)
        # verify renders a table's SELECT from a SQL file in DuckDB's dialect.
        dialect = self.sql_dialect = 'duckdb' if candidate.node.kind == 'table' else self.dialect
        try:
            tree = read_only_query(candidate.sql, dialect)
        except ValueError as error:
            return {}, str(error)
        except _ANALYSIS_ERRORS as error:
            return {}, f'the SQL could not be read: {str(error).strip().splitlines()[0][:120]}'
        # The qualifier expands a star to the columns the graph knows, which may be fewer than the
        # relation has: a SELECT DISTINCT * is a key only of the columns it really returns.
        for select in tree.find_all(exp.Select):
            if any(isinstance(p, exp.Star) or isinstance(p.this, exp.Star) for p in select.selects):
                select.meta['star'] = True
        try:
            # Names match in any case, as in the schema.
            options: dict[str, Any] = {'dialect': case_insensitive(dialect), 'schema': self.schema}
            if self.db:
                options['db'] = self.db
                if self.catalog:
                    options['catalog'] = self.catalog
            qualified = qualify(tree, validate_qualify_columns=False, identify=False, **options)
            root = build_scope(qualified)
        except _ANALYSIS_ERRORS as error:
            return {}, f'the SQL could not be read: {str(error).strip().splitlines()[0][:120]}'
        branches = self._branches(root) if root is not None else 'a query with no SELECT'
        if isinstance(branches, str):
            return {}, branches
        names = [list(self._projections(branch)) for branch in branches]
        widths = [len(cast(exp.Select, branch.expression).selects) for branch in branches]
        if any(len(n) != len(names[0]) or len(n) != width for n, width in zip(names, widths, strict=True)):
            return {}, 'a UNION ALL whose branches cannot be lined up column by column'
        flows: dict[str, list[_Flow] | str] = {}
        # Every branch's columns go by the first branch's names, as the query's output does.
        renames = [dict(zip(n, names[0], strict=True)) for n in names]
        for position, name in enumerate(names[0]):
            try:
                found = [self._flow(branch, n[position]) for branch, n in zip(branches, names, strict=True)]
            except _ANALYSIS_ERRORS as error:
                found = [f'the column could not be analysed: {type(error).__name__}']
            reason = next((f for f in found if isinstance(f, str)), None)
            ok = [f for f in found if not isinstance(f, str)]
            if reason is None and len({f.op for f in ok}) > 1:
                reason = 'a UNION ALL that sums in one branch and counts in another'
            flows[name] = reason or [
                _Flow(f.op, f.column, f.factor, _renamed(f.path, rename))
                for f, rename in zip(ok, renames, strict=True)
            ]
        try:
            paths = [self._rows_of(branch) for branch in branches]
        except _ANALYSIS_ERRORS as error:
            paths = [f'the rows could not be analysed: {type(error).__name__}']
        reason = next((p for p in paths if isinstance(p, str)), None)
        rows = [p for p in paths if not isinstance(p, str)]
        return flows, reason or [_renamed(p, rename) for p, rename in zip(rows, renames, strict=True)]

    def _branches(self, scope: Scope) -> list[Scope] | str:
        """The SELECTs whose rows a query returns: itself, or each branch of a ``UNION ALL``."""
        query = scope.expression
        if isinstance(query, exp.Select):
            return [scope]
        if not isinstance(query, exp.Union) or query.args.get('distinct') or query.args.get('by_name'):
            return 'a set operation (UNION, INTERSECT or EXCEPT)'
        for arg, clause in (('limit', 'LIMIT'), ('offset', 'OFFSET')):
            if query.args.get(arg) is not None:
                return f'{clause}, which drops rows'
        found: list[Scope] = []
        for branch in scope.set_operation_scopes:
            inner = self._branches(branch)
            if isinstance(inner, str):
                return inner
            found.extend(inner)
        return found

    def _projections(self, scope: Scope) -> dict[str, exp.Expr]:
        cached = self._projections_cache.get(id(scope))
        if cached is None:
            select = scope.expression
            selects = select.selects if isinstance(select, exp.Select) else []
            cached = {p.alias_or_name.lower(): p for p in selects if not isinstance(p, exp.Star)}
            self._projections_cache[id(scope)] = cached
        return cached

    def _identity(self, table: exp.Table) -> _Path:
        key = table_key(table, self.catalog, self.db)
        path = self._identities.get(key)
        if path is None:
            path = self._identities[key] = _Path(key, 'map', (), None, frozenset())
        return path

    # Columns

    def _flow(self, scope: Scope, name: str) -> _Flow | str:
        key = (id(scope), name)
        found = self._flows.get(key)
        if found is None:
            self._flows[key] = 'a column defined in terms of itself'
            found = self._flows[key] = self._flow_of(scope, name)
        return found

    def _flow_of(self, scope: Scope, name: str) -> _Flow | str:
        if not isinstance(scope.expression, exp.Select):
            return 'a set operation (UNION, INTERSECT or EXCEPT)'
        projection = self._projections(scope).get(name)
        if projection is None:
            return 'a column the query does not name'
        term = _term(projection, self.sql_dialect)
        if isinstance(term, str):
            return term
        alias = term.alias or self._driving(scope)
        if alias is None:
            return 'a COUNT with no single relation to count'
        local = self._local(scope, alias)
        if isinstance(local, str):
            return local
        if term.aggregate is None and local.mode == 'agg':
            return 'a grouping column, not a measure'
        if term.aggregate == 'count' and term.column is None and local.kept:
            return f'a COUNT(*) over a FULL or RIGHT join, which counts the unmatched rows of {local.kept}'
        if term.aggregate == 'count':
            rows = self._rows(scope, alias)
            if isinstance(rows, str):
                return rows
            if rows.mode != 'map':
                return 'a COUNT of rows that are already grouped'
            column = None
            if term.column is not None:
                carried = _carry(rows, term.column)
                if not isinstance(carried, exp.Column):
                    return 'a COUNT of a computed column'
                column = carried.name.lower()
            path = self._compose(scope, alias, rows, local, term.conditions)
            return path if isinstance(path, str) else _Flow('count', column, term.factor, path)
        assert term.column is not None
        source = scope.sources.get(alias)
        if isinstance(source, exp.Table):
            inner: _Flow | str = _Flow('sum', term.column, 1.0, self._identity(source))
        elif isinstance(source, Scope):
            inner = self._flow(source, term.column)
        else:
            return 'a table function'
        if isinstance(inner, str):
            return inner
        path = self._compose(scope, alias, inner.path, local, term.conditions)
        return (
            path if isinstance(path, str) else _Flow(inner.op, inner.column, inner.factor * term.factor, path)
        )

    def _driving(self, scope: Scope) -> str | None:
        from_ = scope.expression.args.get('from_')
        return from_.this.alias_or_name if from_ is not None else None

    def _rows(self, scope: Scope, alias: str) -> _Path | str:
        source = scope.sources.get(alias)
        if isinstance(source, exp.Table):
            return self._identity(source)
        if isinstance(source, Scope):
            return self._rows_of(source)
        return 'a table function'

    def _rows_of(self, scope: Scope) -> _Path | str:
        """How the rows of ``scope`` relate to the relation its FROM clause reads, through CTEs."""
        found = self._rows_cache.get(id(scope))
        if found is None:
            self._rows_cache[id(scope)] = 'a query defined in terms of itself'
            alias = self._driving(scope) if isinstance(scope.expression, exp.Select) else None
            if alias is None:
                found = 'a set operation (UNION, INTERSECT or EXCEPT)'
            else:
                local = self._local(scope, alias)
                inner = self._rows(scope, alias)
                found = local if isinstance(local, str) else inner
                if not isinstance(local, str) and local.kept:
                    found = f'a FULL or RIGHT join, which keeps the unmatched rows of {local.kept}'
                elif not isinstance(local, str) and not isinstance(inner, str):
                    found = self._compose(scope, alias, inner, local, [])
            self._rows_cache[id(scope)] = found
        return found

    def _compose(
        self, scope: Scope, alias: str, inner: _Path, local: _Local, conditions: list[exp.Expr]
    ) -> _Path | str:
        """``inner`` (the alias's rows against the base) carried through this SELECT."""
        key = (id(scope), alias, id(inner))
        path = self._composed.get(key)
        if path is None:
            path = self._composed[key] = self._compose_of(scope, alias, inner, local)
        if isinstance(path, str) or not conditions:
            return path
        extra = []
        for condition in conditions:
            translated = _translate(condition, alias, inner)
            if translated is None:
                return 'a condition on a computed column'
            extra.append(translated)
        return _Path(path.base, path.mode, (*path.filters, *extra), path.carried, path.tests)

    def _compose_of(self, scope: Scope, alias: str, inner: _Path, local: _Local) -> _Path | str:
        filters = list(inner.filters)
        for condition in local.filters:
            translated = _translate(condition, alias, inner)
            if translated is None:
                return 'a filter on a computed column'
            filters.append(translated)
        carried: dict[str, exp.Expr] = {}
        for name, projection in self._projections(scope).items():
            body = _unalias(projection)
            if body is None or not _row_level(body):
                continue
            if any(c.table != alias for c in body.find_all(exp.Column)):
                continue
            if local.mode == 'agg' and body not in local.keys:
                continue
            translated = _translate(body, alias, inner)
            if translated is not None:
                carried[name] = translated
        mode = 'agg' if 'agg' in (local.mode, inner.mode) else 'map'
        return _Path(inner.base, mode, tuple(filters), carried, inner.tests | local.tests)

    # Rows of one SELECT against one of its sources

    def _local(self, scope: Scope, alias: str) -> _Local | str:
        key = (id(scope), alias)
        found = self._locals.get(key)
        if found is None:
            found = self._locals[key] = self._local_of(scope, alias)
        return found

    @staticmethod
    def _reshapes(scope: Scope) -> str | None:
        """Why a SELECT does not keep its rows as its FROM clause gives them, apart from its joins,
        WHERE and GROUP BY: a clause that drops rows, a sample, or a set-returning function that
        repeats them."""
        select = scope.expression
        if not isinstance(select, exp.Select):
            return 'a set operation (UNION, INTERSECT or EXCEPT)'
        for arg, clause in (
            ('distinct', 'SELECT DISTINCT'),
            ('limit', 'LIMIT'),
            ('offset', 'OFFSET'),
            ('qualify', 'QUALIFY'),
            ('having', 'HAVING'),
        ):
            if select.args.get(arg) is not None:
                return f'{clause}, which drops rows'
        if select.args.get('sample') is not None or any(t.args.get('sample') for t in scope.tables):
            return 'a sample of a table (TABLESAMPLE or USING SAMPLE), which drops rows'
        from_ = select.args.get('from_')
        sources = [*scope.tables, *([from_.this] if from_ is not None else [])]
        sources += [join.this for join in select.args.get('joins') or []]
        if any(_pivoted(source) for source in sources):
            return 'a PIVOT or UNPIVOT, which groups rows or repeats them and drops NULLs'
        if any(_calls(p, _SET_RETURNING, _SET_RETURNING_NAMES) for p in select.selects):
            return 'a set-returning function in the SELECT, which repeats rows'
        return None

    def _local_of(self, scope: Scope, alias: str) -> _Local | str:
        reshaped = self._reshapes(scope)
        if reshaped:
            return reshaped
        select = scope.expression
        assert isinstance(select, exp.Select)
        group = select.args.get('group')
        if group is not None and _grouping_sets(group):
            return 'GROUPING SETS, ROLLUP or CUBE, which count rows more than once'
        keys = list(group.expressions) if group is not None else []
        aggregated = group is not None or any(_aggregates(p) for p in select.selects)
        joins = self._joins(scope, alias, preserve=True)
        if isinstance(joins, str):
            return joins
        filters = []
        where = select.args.get('where')
        if where is not None:
            for condition in _conjuncts(where.this):
                if condition.find(exp.Subquery, exp.Select) is not None:
                    return 'a filter with a subquery'
                if condition.find(exp.AggFunc, exp.Window) is not None:
                    return 'a filter on an aggregate'
                if _volatile(condition):
                    return _CHANGING
                others = {c.table for c in condition.find_all(exp.Column)} - {alias}
                if others:
                    labels = ', '.join(sorted(self._label(scope, o) for o in others))
                    return f'a WHERE condition on {labels}, which drops rows'
                filters.append(condition)
        return _Local('agg' if aggregated else 'map', filters, keys, joins.tests, joins.kept)

    def _joins(self, scope: Scope, alias: str, *, preserve: bool) -> _Joins | str:
        """The tests that prove the joins of ``scope`` give each row of ``alias`` exactly one row
        (at most one, without ``preserve``), and with ``preserve`` a relation whose unmatched rows
        a FULL or RIGHT join adds; or why nothing proves it."""
        select = scope.expression
        from_ = select.args.get('from_')
        if from_ is None:
            return 'a query with no FROM clause'
        names = [from_.this.alias_or_name]
        edges: list[tuple[str, str, list[str], list[str], str, bool]] = []
        for join in select.args.get('joins') or []:
            target = join.this
            if not isinstance(target, (exp.Table, exp.Subquery)):
                return 'an UNNEST or LATERAL join, which repeats rows'
            name = target.alias_or_name
            kind, side = (join.kind or '').upper(), (join.side or '').upper()
            on = join.args.get('on')
            if kind in ('SEMI', 'ANTI'):
                return f'a {kind} JOIN, which drops rows'
            if kind == 'CROSS' or on is None:
                return 'a CROSS JOIN, which repeats rows'
            left_columns: list[str] = []
            right_columns: list[str] = []
            other: str | None = None
            extra = False
            for condition in _conjuncts(on):
                if isinstance(condition, exp.EQ) and all(
                    isinstance(s, exp.Column) for s in (condition.this, condition.expression)
                ):
                    a, b = condition.this, condition.expression
                    if b.table == name:
                        a, b = b, a
                    if a.table == name and b.table in names:
                        if other not in (None, b.table):
                            return 'a join to several relations at once'
                        other = b.table
                        right_columns.append(a.name.lower())
                        left_columns.append(b.name.lower())
                        continue
                if {c.table for c in condition.find_all(exp.Column)} <= {name}:
                    extra = True
                    continue
                return 'a join condition other than equal keys'
            if other is None:
                return 'a join with no equal keys'
            edges.append((other, name, left_columns, right_columns, side, extra))
            names.append(name)
        if alias not in names:
            return 'a column of no known relation'
        tests: set[str] = set()
        kept: str | None = None
        unmatched = {alias: False}
        queue = [alias]
        while queue:
            near = queue.pop(0)
            for left, right, left_columns, right_columns, side, extra in edges:
                if near not in (left, right):
                    continue
                far = right if near == left else left
                if far in unmatched:
                    continue
                near_columns, far_columns = (
                    (left_columns, right_columns) if near == left else (right_columns, left_columns)
                )
                unique = self._unique(scope, far, far_columns)
                if unique is None:
                    joined = f'joins {self._label(scope, far)} on {", ".join(far_columns)}'
                    if self._unique(scope, far, far_columns, filtered=True):
                        return f'{joined}, whose only uniqueness test has a where, so holds on some rows'
                    return f'{joined}, which no uniqueness test covers'
                tests |= unique
                near_side = 'LEFT' if near == left else 'RIGHT'
                far_side = 'RIGHT' if near == left else 'LEFT'
                if preserve and side in (far_side, 'FULL') and kept is None:
                    kept = self._label(scope, far)
                if not preserve:
                    unmatched[far] = False
                elif side in (near_side, 'FULL'):
                    unmatched[far] = True
                else:
                    proof = (
                        None
                        if extra or unmatched[near]
                        else self._referential(scope, near, near_columns, far, far_columns)
                    )
                    if proof is None:
                        return (
                            f'a join that drops rows of {self._label(scope, near)} with no match in '
                            f'{self._label(scope, far)}, where no relationships and not_null tests cover '
                            f'{", ".join(near_columns)}'
                        )
                    tests |= proof
                    unmatched[far] = False
                queue.append(far)
        return _Joins(frozenset(tests), kept)

    def _unique(
        self, scope: Scope, alias: str, columns: list[str], *, filtered: bool = False
    ) -> frozenset[str] | None:
        """The tests that prove ``columns`` of ``alias`` hold each value once (empty when the SQL
        itself proves it), or None. With ``filtered``, a unique test with a ``where`` on the table
        that would."""
        source = scope.sources.get(alias)
        if isinstance(source, exp.Table):
            # An UNPIVOT gives each key one row for each column it unpivots.
            if _pivoted(source, unpivot=True):
                return None
            node = self.graph.node_for_relation(table_key(source, self.catalog, self.db))
            proof = self.tests.key(node.id, columns, filtered=filtered) if node is not None else None
            return frozenset({proof}) if proof else None
        if filtered:
            return None
        if not isinstance(source, Scope) or not isinstance(source.expression, exp.Select):
            return None
        select = source.expression
        # A set-returning function repeats rows whatever the grouping: no key survives it.
        if any(_calls(p, _SET_RETURNING, _SET_RETURNING_NAMES) for p in select.selects):
            return None
        projections = self._projections(source)
        named = [_unalias(projections[c]) for c in columns if c in projections]
        group = select.args.get('group')
        if group is not None:
            keys = group.expressions
            plain = keys and not _grouping_sets(group)
            return frozenset() if plain and all(any(k == n for n in named) for k in keys) else None
        if any(_aggregates(p) for p in select.selects):
            return frozenset()  # one row
        distinct = select.args.get('distinct')
        if distinct is not None:
            on = distinct.args.get('on')
            if on is None and not select.meta.get('star') and set(projections) <= set(columns):
                return frozenset()
            # DISTINCT ON (k) keeps one row for each k, named by its output column or by its source's.
            keys = on.expressions if on is not None else []
            if keys and all(
                any(k == n for n in named)
                or (isinstance(k, exp.Column) and not k.table and k.name.lower() in columns)
                for k in keys
            ):
                return frozenset()
        if self._deduplicated(source, named):
            return frozenset()
        by_table: dict[str, list[str]] = defaultdict(list)
        for body in named:
            if isinstance(body, exp.Column) and body.table:
                by_table[body.table].append(body.name.lower())
        for table, names in by_table.items():
            at_most_one = self._joins(source, table, preserve=False)
            if isinstance(at_most_one, str):
                continue
            inner = self._unique(source, table, names)
            if inner is not None:
                return inner | at_most_one.tests
        return None

    def _deduplicated(self, scope: Scope, named: list[exp.Expr | None]) -> bool:
        """Whether a QUALIFY keeps one row for each value of expressions among ``named``:
        ``ROW_NUMBER() OVER (PARTITION BY k ...) = 1``, directly or through its alias."""
        qualify = scope.expression.args.get('qualify')
        if qualify is None:
            return False
        projections = self._projections(scope)
        for condition in _conjuncts(qualify.this):
            if not isinstance(condition, (exp.EQ, exp.LTE)):
                continue
            window, bound = condition.this, condition.expression
            if isinstance(window, exp.Column) and not window.table:
                window = _unalias(projections.get(window.name.lower()))
            if not isinstance(window, exp.Window) or not isinstance(window.this, exp.RowNumber):
                continue
            partition = window.args.get('partition_by') or []
            if _number(bound) == 1 and partition and all(any(k == n for n in named) for k in partition):
                return True
        return False

    def _referential(
        self, scope: Scope, near: str, near_columns: list[str], far: str, far_columns: list[str]
    ) -> frozenset[str] | None:
        """The relationships and not_null tests that prove every row of ``near`` has a match in
        ``far``, or None."""
        for near_column, far_column in zip(near_columns, far_columns, strict=True):
            child = self._trace(scope, near, near_column, strict=False)
            parent = self._trace(scope, far, far_column, strict=True)
            if child is None or parent is None:
                continue
            relationship = self.tests.relationships.get(child, {}).get(parent)
            not_null = self.tests.not_null.get(child)
            if relationship and not_null:
                return frozenset({relationship, not_null})
        return None

    def _trace(self, scope: Scope, alias: str, column: str, *, strict: bool) -> tuple[str, str] | None:
        """The relation (by graph id) and column ``alias.column`` passes through unchanged. With
        ``strict``, only through queries that keep every row (so every key value) of it."""
        for _ in range(50):
            source = scope.sources.get(alias)
            if isinstance(source, exp.Table):
                node = self.graph.node_for_relation(table_key(source, self.catalog, self.db))
                return (node.id, column) if node is not None else None
            if not isinstance(source, Scope) or not isinstance(source.expression, exp.Select):
                return None
            select = source.expression
            if strict and (
                self._reshapes(source) or any(select.args.get(a) for a in ('where', 'joins', 'group'))
            ):
                return None
            body = _unalias(self._projections(source).get(column))
            if not isinstance(body, exp.Column) or not body.table:
                return None
            scope, alias, column = source, body.table, body.name.lower()
        return None

    def _label(self, scope: Scope, alias: str) -> str:
        """The name to give a source in a reason: its relation's, through a CTE that only selects
        from one table."""
        for _ in range(50):
            source = scope.sources.get(alias)
            if isinstance(source, exp.Table):
                node = self.graph.node_for_relation(table_key(source, self.catalog, self.db))
                return self.graph.label(node) if node is not None else source.name
            if not isinstance(source, Scope) or not isinstance(source.expression, exp.Select):
                return alias
            select = source.expression
            from_ = select.args.get('from_')
            if from_ is None or select.args.get('joins'):
                return alias
            scope, alias = source, from_.this.alias_or_name
        return alias


def _renamed(path: _Path, names: dict[str, str]) -> _Path:
    """The path of one branch of a ``UNION ALL`` with its carried columns named as the query's are,
    by the first branch."""
    if path.carried is None or all(k == v for k, v in names.items()):
        return path
    carried = {names[k]: v for k, v in path.carried.items() if k in names}
    return _Path(path.base, path.mode, path.filters, carried, path.tests)


def _carry(path: _Path, column: str) -> exp.Expr | None:
    if path.carried is None:
        return exp.Column(this=exp.to_identifier(column))
    return path.carried.get(column)


def _translate(expression: exp.Expr, alias: str, inner: _Path) -> exp.Expr | None:
    """``expression`` over ``alias``'s columns, rewritten over the base's columns."""
    missing = False

    def replace(node: exp.Expr) -> exp.Expr:
        nonlocal missing
        if isinstance(node, exp.Column):
            found = _carry(inner, node.name.lower()) if node.table == alias else None
            if found is None:
                missing = True
                return node
            return found.copy()
        return node

    result = expression.transform(replace)
    return None if missing else result


def _substitute(expression: exp.Expr, carried: dict[str, exp.Expr]) -> exp.Expr | None:
    """``expression`` over a model's columns, rewritten over its input's through ``carried``."""
    missing = False

    def replace(node: exp.Expr) -> exp.Expr:
        nonlocal missing
        if isinstance(node, exp.Column):
            found = carried.get(node.name.lower())
            if found is None:
                missing = True
                return node
            return found.copy()
        return node

    result = expression.transform(replace)
    return None if missing else result


# Proposing


def _is_measure(graph: Graph, node: Node, column: str, tested_unique: set[tuple[str, str]]) -> bool:
    """A column whose total means something: numeric (or of unknown type), not a key."""
    found = graph.column(node.id, column)
    dtype = (found.data_type if found is not None else None) or ''
    if dtype and not _NUMERIC.match(dtype):
        return False
    return not (core(column) & KEY_WORDS) and (node.id, column) not in tested_unique


def _is_date(graph: Graph, node: Node, column: str) -> bool:
    found = graph.column(node.id, column)
    return bool(found is not None and found.data_type and _DATE.match(found.data_type))


def propose(graph: Graph, root: Path, *, models: Collection[str] | None = None) -> Laws:
    """The laws the lineage implies, from every dbt model's compiled SQL and every SQL-built
    table's, without reading the warehouse.

    Each numeric measure column (not a key) whose total follows from an input through safe joins
    gets a conservation or count law, followed upstream through every model that keeps the total;
    each model that maps an input one row to one row gets a count law; each aggregate measure gets
    an additivity law for :func:`confirm` to classify. A ``UNION ALL`` gives a law whose total is
    the sum of its branches'. Columns with no law are in ``blocked``.

    ``models`` (graph ids) limits the reading to those models, such as the ones the laws of
    :meth:`Laws.models` run through, which is all :func:`violations` needs.
    """
    started = time.perf_counter()
    analyser = _Analyser(graph)
    dialect = analyser.dialect
    result = Laws(dialect=dialect)
    links: dict[tuple[str, str | None], list[_Link]] = {}
    tops: list[list[_Link]] = []
    additivity: list[tuple[Node, str, str, Node | None, str | None, str | None]] = []
    tested_unique = analyser.tests.single()
    classes: dict[str, str] = {}
    for candidate in candidates(graph, root):
        node = candidate.node
        if models is not None and node.id not in models:
            continue
        hopeless = analyser.hopeless(node, candidate.sql)
        if hopeless:
            flows: dict[str, list[_Flow] | str] = {
                c.name.lower(): hopeless for c in graph.columns_of(node.id) if '.' not in c.name
            }
            rows: list[_Path] | str = hopeless
        else:
            flows, rows = analyser.model(candidate)
        selecting: str | None = None
        read = False
        for name, flow in flows.items():
            measure = _is_measure(graph, node, name, tested_unique)
            branches = _links(graph, node, name, flow) if not isinstance(flow, str) else None
            if branches is None:
                reason = flow if isinstance(flow, str) else 'reads a relation the graph does not know'
                result.blocked.append(Blocked(node.id, name, reason, measure))
            else:
                links[(node.id, name)] = branches
                if measure:
                    tops.append(branches)
            suggestion = _suggestion(graph, node, name, classes) if measure else None
            if suggestion:
                if not read:
                    # verify renders a table's SELECT from a SQL file in DuckDB's dialect.
                    selecting = _selecting(candidate.sql, 'duckdb' if node.kind == 'table' else dialect)
                    read = True
                source, source_column = _probe_input(graph, node, name, branches[0] if branches else None)
                additivity.append((node, name, suggestion, source, source_column, selecting))
        if isinstance(rows, str):
            result.blocked.append(Blocked(node.id, None, rows))
        elif all(path.mode == 'map' for path in rows):
            branches = _links(graph, node, None, [_Flow('rows', None, 1.0, path) for path in rows])
            if branches is not None:
                links[(node.id, None)] = branches
                tops.append(branches)
        else:
            result.blocked.append(Blocked(node.id, None, 'groups its rows'))
    for key, branches in links.items():
        result.steps[key] = tuple(_step(link, None, dialect) for link in branches)
    for top in tops:
        result.laws.append(_law(graph, top, links, dialect))
    for node, name, suggestion, source, source_column, selecting in additivity:
        result.laws.append(
            Law(
                id=f'additivity:{node.id}#{name}',
                kind='additivity',
                relation=node.id,
                label=graph.label(node),
                column=name,
                terms=[Term(source.id, graph.label(source), source_column)] if source is not None else [],
                expected=suggestion,
                unprobed=f'the model keeps rows by comparing them with others ({selecting})'
                if selecting
                else None,
            )
        )
    result.laws.sort(key=lambda law: (law.label, law.column or '', KINDS.index(law.kind)))
    result.seconds = time.perf_counter() - started
    return result


_WEAKEST = ('non-additive', 'semi-additive', 'additive')


def _selecting(sql: str, dialect: str) -> str | None:
    """The clause by which a model keeps rows by comparing them with one another, anywhere in its
    SQL, so that repeating or splitting its input changes which rows it keeps, not only their
    values: DISTINCT, QUALIFY, LIMIT, OFFSET, HAVING or a WHERE with a subquery."""
    try:
        tree = read_only_query(sql, dialect)
    except _ANALYSIS_ERRORS:
        return 'SQL the probes cannot read'
    for select in tree.find_all(exp.Select):
        for arg, clause in (
            ('distinct', 'SELECT DISTINCT'),
            ('qualify', 'QUALIFY'),
            ('limit', 'LIMIT'),
            ('offset', 'OFFSET'),
            ('having', 'HAVING'),
        ):
            if select.args.get(arg) is not None:
                return clause
        where = select.args.get('where')
        if where is not None and where.find(exp.Subquery, exp.Select) is not None:
            return 'a filter with a subquery'
    return None


def _reading(graph: Graph, column: Node) -> tuple[str | None, bool]:
    """What a column's own expression suggests about its additivity, and whether it sums its
    inputs (so is no more additive than they are): a SUM does, a COUNT does not."""
    text = str(column.attrs.get('expression') or '')
    if not _AGGREGATE.search(text):
        return None, True
    dialect = str(graph.meta.get('dialect') or 'duckdb')
    try:
        tree = sqlglot.parse_one(text, dialect=dialect)
    except PARSE_ERRORS:
        return None, False
    unwrapped = _unwrap(tree, dialect)
    found = _unalias(tree if isinstance(unwrapped, str) else unwrapped[0]) or tree
    inner = found.this if isinstance(found, exp.Window) else found
    return _suggests(tree, dialect), isinstance(inner, exp.Sum)


def _class(graph: Graph, column: Node, classes: dict[str, str], seen: frozenset[str] = frozenset()) -> str:
    """The additivity of a column, its own and that of every column it sums upstream, the weakest
    of them: a SUM of a daily average is no more additive than the average."""
    known = classes.get(column.id)
    if known is not None:
        return known
    own, sums = _reading(graph, column)
    found = [own or 'additive']
    if sums and column.id not in seen and len(seen) < 50:
        for edge in graph.out_edges(column.id, 'derived_from'):
            upstream = graph.nodes.get(edge.target)
            if upstream is not None and upstream.kind == 'column' and upstream.parent != column.parent:
                found.append(_class(graph, upstream, classes, seen | {column.id}))
    classes[column.id] = min(found, key=_WEAKEST.index)
    return classes[column.id]


def _suggestion(graph: Graph, node: Node, column: str, classes: dict[str, str]) -> str | None:
    """What the column's composed expression (from the build) suggests about its additivity, made no
    stronger than the columns it sums upstream."""
    found = graph.column(node.id, column)
    if found is None or _reading(graph, found)[0] is None:
        return None
    return _class(graph, found, classes)


def _links(graph: Graph, node: Node, column: str | None, flows: list[_Flow]) -> list[_Link] | None:
    """The model's steps for one column (None for its row count), one for each branch."""
    found: list[_Link] = []
    for branch, flow in enumerate(flows):
        source = graph.node_for_relation(flow.path.base)
        if source is None or source.id == node.id:
            return None
        found.append(
            _Link(
                model=node,
                column=column,
                input=source,
                input_column=flow.column,
                op=flow.op,
                factor=flow.factor,
                filters=flow.path.filters,
                carried=dict(flow.path.carried or {}),
                tests=flow.path.tests,
                branch=branch,
            )
        )
    return found


def _probe_input(graph: Graph, node: Node, column: str, link: _Link | None) -> tuple[Node | None, str | None]:
    """The input whose rows the additivity probes repeat: the one the column's total comes from,
    or else the first input its lineage reads."""
    if link is not None:
        return link.input, link.input_column
    found = graph.column(node.id, column)
    for edge in graph.out_edges(found.id, 'derived_from') if found is not None else []:
        upstream = graph.nodes.get(edge.target)
        parent = graph.parent_of(upstream) if upstream is not None else None
        if parent is not None and parent.relation and upstream is not None:
            return parent, upstream.name.lower()
    # A COUNT(*) reads no column: repeat the rows of the first relation the model reads.
    inputs = sorted(
        (
            n
            for n in (graph.nodes.get(e.source) for e in graph.in_edges(node.id, 'feeds'))
            if n and n.relation
        ),
        key=lambda n: n.id,
    )
    return (inputs[0], None) if inputs else (None, None)


def _sql(expression: exp.Expr | None, dialect: str) -> str | None:
    return expression.sql(dialect=dialect) if expression is not None else None


def _and(filters: tuple[exp.Expr, ...] | list[exp.Expr]) -> exp.Expr | None:
    if not filters:
        return None
    return exp.and_(*(f.copy() for f in filters)) if len(filters) > 1 else filters[0]


def _step(link: _Link, partition: tuple[exp.Expr, exp.Expr] | None, dialect: str) -> Step:
    return Step(
        model=link.model.id,
        column=link.column,
        input=link.input.id,
        input_column=link.input_column,
        op=link.op,
        factor=link.factor,
        filter=_sql(_and(link.filters), dialect),
        partition=(partition[0].sql(dialect=dialect), partition[1].sql(dialect=dialect))
        if partition
        else None,
        relies_on=tuple(sorted(link.tests)),
        branch=link.branch,
    )


@dataclass
class _Chain:
    """A term followed upstream: the total it has reached, of which relation, through which steps."""

    op: str
    column: str | None
    factor: float
    filters: list[exp.Expr]
    carried: dict[str, exp.Expr]
    """The law's relation's columns, as expressions over ``relation``'s."""
    relation: Node
    links: list[_Link]


def _extend(chain: _Chain, upstream: _Link) -> _Chain | None:
    """``chain`` carried one model further upstream, or None when its filters or the column it
    counts do not survive that model."""
    if chain.op == 'count' and chain.column is not None:
        counted = upstream.carried.get(chain.column)
        if not isinstance(counted, exp.Column):
            return None
        column: str | None = counted.name.lower()
    else:
        column = upstream.input_column
    moved = [_substitute(f, upstream.carried) for f in chain.filters]
    filters = [f for f in moved if f is not None]
    if len(filters) < len(moved):
        return None
    carried = {
        k: v
        for k, v in ((k, _substitute(v, upstream.carried)) for k, v in chain.carried.items())
        if v is not None
    }
    op, factor = (
        (upstream.op, chain.factor * upstream.factor) if chain.op == 'sum' else (chain.op, chain.factor)
    )
    return _Chain(
        op, column, factor, [*upstream.filters, *filters], carried, upstream.input, [*chain.links, upstream]
    )


def _follow(link: _Link, links: dict[tuple[str, str | None], list[_Link]]) -> list[_Chain]:
    """The terms from ``link``, followed upstream while each model keeps the total, and split into
    the branches of each ``UNION ALL`` on the way; in branch order."""
    done: list[_Chain] = []
    stack = [
        _Chain(
            link.op,
            link.input_column,
            link.factor,
            list(link.filters),
            dict(link.carried),
            link.input,
            [link],
        )
    ]
    while stack:
        chain = stack.pop()
        upstream = links.get((chain.relation.id, chain.column if chain.op == 'sum' else None))
        if not upstream or chain.relation.id in {step.model.id for step in chain.links}:
            done.append(chain)
            continue
        extended = [_extend(chain, branch) for branch in upstream]
        moved = [e for e in extended if e is not None]
        if len(moved) < len(extended):
            done.append(chain)
            continue
        stack.extend(reversed(moved))
    return done


def _law(
    graph: Graph, top: list[_Link], links: dict[tuple[str, str | None], list[_Link]], dialect: str
) -> Law:
    """The law from ``top`` (one link for each branch of the model), followed upstream."""
    chains = [chain for link in top for chain in _follow(link, links)]
    head = top[0]
    carried = set.intersection(*(set(chain.carried) for chain in chains))
    day = next((name for name in sorted(carried) if _is_date(graph, head.model, name)), None)
    terms = []
    for chain in chains:
        at: exp.Expr | None = exp.Column(this=exp.to_identifier(day)) if day else None
        steps = []
        for link in chain.links:
            below = _substitute(at, link.carried) if at is not None else None
            steps.append(_step(link, (at, below) if at is not None and below is not None else None, dialect))
            at = below
        terms.append(
            Term(
                source=chain.relation.id,
                source_label=graph.label(chain.relation),
                source_column=chain.column,
                op='sum' if chain.op == 'sum' else 'count',
                factor=chain.factor,
                filter=_sql(_and(chain.filters), dialect),
                partition=_sql(chain.carried[day], dialect) if day else None,
                steps=tuple(steps),
            )
        )
    # A law sums when any term does, and so is held to the tolerance of sums.
    kind = 'conservation' if any(chain.op == 'sum' for chain in chains) else 'count'
    law_id = f'{kind}:{head.model.id}#{head.column}' if head.column else f'count:{head.model.id}'
    return Law(
        id=law_id,
        kind=kind,
        relation=head.model.id,
        label=graph.label(head.model),
        column=head.column,
        terms=terms,
        partition=_sql(exp.Column(this=exp.to_identifier(day)), dialect) if day else None,
    )


# Confirming


def confirm(
    graph: Graph,
    root: Path,
    warehouse: str | Path | Engine,
    laws: Laws | None = None,
    *,
    tolerance: float = TOLERANCE,
    max_rows: int = 10000,
) -> Laws:
    """Check ``laws`` (proposed from the graph when not given) against the warehouse, read-only,
    in the engine verify uses: a DuckDB file attached read-only, a Postgres transaction that is
    rolled back, or a sample of a remote warehouse.

    Conservation and count laws compare totals per day within ``tolerance`` (relative to the day's
    sum of magnitudes; counts exactly) and need the full tables, so on a sampled warehouse they are
    left unchecked, as is a law with no rows to compare. A filter or partition that is not one
    expression over the relation's columns, as a hand-edited laws.json could give, leaves its law
    unchecked. Additivity laws rerun the model's SQL on up to ``max_rows`` rows of each input: with
    every row of the measure's input twice, with one row twice, and on two halves of it split at the
    middle value of a date (or of the measure's input column).
    """
    started = time.perf_counter()
    result = copy.deepcopy(laws) if laws is not None else propose(graph, root)
    result.tolerance = tolerance
    engine = engine_for(warehouse)
    result.warehouse = engine.label
    try:
        totals = [law for law in result.laws if law.kind != 'additivity']
        if isinstance(engine, SampledEngine):
            for law in totals:
                law.status, law.note = 'unchecked', 'totals need whole tables, and this warehouse is sampled'
        else:
            _Totals(engine, graph, result.dialect or engine.dialect, tolerance).check(totals)
        _probe(engine, graph, root, [law for law in result.laws if law.kind == 'additivity'], max_rows)
    finally:
        engine.close()
    result.seconds = time.perf_counter() - started
    return result


def _value(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float, decimal.Decimal)):
        return float(value)
    return float(str(value))


def _partition_key(value: Any) -> str:
    if value is None:
        return 'null'
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()[:10]
    return str(value)


_Request = tuple[str, str | None, str | None]  # relation id, filter, partition
_Number = int | float | decimal.Decimal
_ByDay = dict[str, _Number]  # by partition


def _amount(value: Any) -> _Number:
    """A total as the warehouse gives it: exact for integers and decimals, so a sum of cents is
    compared to the cent."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, decimal.Decimal)):
        return value
    try:
        return decimal.Decimal(str(value))
    except decimal.InvalidOperation:
        return float(str(value))


def _add(a: _Number, b: _Number) -> _Number:
    if isinstance(a, float) or isinstance(b, float):
        return float(a) + float(b)
    return a + b


def _exact_factor(factor: float) -> decimal.Decimal | None:
    """``factor`` as an exact decimal when it is one that SQL writes exactly, such as 0.01, and None
    for one such as a third."""
    text = repr(factor)
    digits = text.lstrip('-').replace('.', '').lstrip('0')
    return decimal.Decimal(text) if 'e' not in text and len(digits) <= 12 else None


@dataclass(frozen=True)
class _Check:
    """One equality to test per partition: the total of a column (or the row count) of ``top``
    against the sum of the factored totals of ``bases``."""

    top: _Request
    column: str | None
    bases: tuple[tuple[_Request, str | None, float, bool], ...]
    """Each base: what is totalled, its column (None for its row count), its factor and whether it
    sums (or counts) the column."""

    @classmethod
    def of_law(cls, law: Law) -> _Check:
        return cls(
            (law.relation, None, law.partition),
            law.column,
            tuple(
                ((t.source, t.filter, t.partition), t.source_column, t.factor, t.op == 'sum')
                for t in law.terms
            ),
        )

    @classmethod
    def of_steps(cls, steps: tuple[Step, ...]) -> _Check:
        """The check of one model's steps for one column: one step, or one for each branch."""
        days = [s.partition for s in steps]
        partitioned = all(days)
        return cls(
            (steps[0].model, None, days[0][0] if partitioned and days[0] else None),
            steps[0].column,
            tuple(
                (
                    (s.input, s.filter, d[1] if partitioned and d else None),
                    s.input_column,
                    s.factor,
                    s.op == 'sum',
                )
                for s, d in zip(steps, days, strict=True)
            ),
        )

    @property
    def sums(self) -> bool:
        return any(sums for *_, sums in self.bases)

    def top_total(self) -> str:
        return f'SUM({_quoted(self.column)})' if self.column else 'COUNT(*)'

    @staticmethod
    def base_total(column: str | None, sums: bool) -> str:
        if sums:
            return f'SUM({_quoted(column)})'
        return f'COUNT({_quoted(column)})' if column else 'COUNT(*)'

    def wanted(self) -> list[tuple[_Request, str]]:
        """The totals the check needs, with each sum's magnitude for the tolerance."""
        found = [(self.top, self.top_total())]
        found += [(request, self.base_total(column, sums)) for request, column, _, sums in self.bases]
        if self.sums and self.column:
            found.append((self.top, _magnitude(self.column)))
        found += [(request, _magnitude(column)) for request, column, _, sums in self.bases if sums]
        return found


def _magnitude(column: str | None) -> str:
    return f'SUM(ABS({_quoted(column)}))'


class _Totals:
    """Totals per partition, computed with one query per relation, filter and partition (and one
    per aggregate when that query fails, so one column that will not sum leaves the rest)."""

    def __init__(self, engine: Engine, graph: Graph, dialect: str, tolerance: float) -> None:
        self.engine, self.graph, self.dialect, self.tolerance = engine, graph, dialect, tolerance
        self.wanted: dict[_Request, set[str]] = defaultdict(set)
        self.results: dict[_Request, dict[str, _ByDay | str]] = defaultdict(dict)

    def check(self, laws: list[Law]) -> None:
        checks = {law.id: _Check.of_law(law) for law in laws}
        self._run(checks.values())
        broken = []
        for law in laws:
            outcome = self._compare(checks[law.id])
            if isinstance(outcome, str):
                law.status, law.note = 'unchecked', outcome
                continue
            law.held, law.checked, law.failures = outcome
            law.status = 'holds' if law.held == law.checked else 'broken'
            if law.status == 'broken':
                broken.append(law)
        # Find where each broken law parts: the step nearest a source whose totals differ, where
        # a UNION ALL's steps are checked together.
        groups = {law.id: _groups(law) for law in broken}
        step_checks = {steps: _Check.of_steps(steps) for found in groups.values() for steps in found.values()}
        self._run(step_checks.values())
        for law in broken:
            for term in law.terms:
                for step in reversed(term.steps):
                    outcome = self._compare(step_checks[groups[law.id][(step.model, step.column)]])
                    if not isinstance(outcome, str) and outcome[0] < outcome[1]:
                        law.broken_at = step.model
                        break
                if law.broken_at:
                    break

    def _total(self, request: _Request, aggregate: str) -> _ByDay | str:
        return self.results.get(request, {}).get(aggregate, 'not queried')

    def _compare(self, check: _Check) -> tuple[int, int, list[Failure]] | str:
        totals: dict[tuple[_Request, str], _ByDay] = {}
        for request, aggregate in check.wanted():
            found = self._total(request, aggregate)
            if isinstance(found, str):
                return found
            totals[(request, aggregate)] = found
        actual = totals[(check.top, check.top_total())]
        size: dict[str, float] = defaultdict(float)
        if check.sums and check.column:
            for key, value in totals[(check.top, _magnitude(check.column))].items():
                size[key] = float(value)
        keys = sorted(set(actual).union(*(totals[(r, check.base_total(c, s))] for r, c, _, s in check.bases)))
        if not keys:
            return 'no rows to compare'
        factors = [_exact_factor(factor) for _, _, factor, _ in check.bases]
        failures = []
        for key in keys:
            a = actual.get(key, 0)
            parts = [totals[(r, check.base_total(c, s))].get(key, 0) for r, c, _, s in check.bases]
            for request, column, factor, sums in check.bases:
                if sums:
                    magnitude = float(totals[(request, _magnitude(column))].get(key, 0))
                    size[key] = max(size[key], abs(factor) * magnitude)
            exact = all(isinstance(v, (int, decimal.Decimal)) for v in (a, *parts)) and None not in factors
            if exact:
                e: _Number = sum(
                    (
                        cast(decimal.Decimal, f) * decimal.Decimal(v)
                        for f, v in zip(factors, parts, strict=True)
                    ),
                    decimal.Decimal(0),
                )
                # Integers and decimals are exact: one duplicated cent breaks the law.
                held = decimal.Decimal(a) == e
            else:
                e = sum(factor * float(v) for (_, _, factor, _), v in zip(check.bases, parts, strict=True))
                # Relative to the sum of magnitudes, so values that cancel on a day are held to the
                # rounding their own size gives, not to their small net.
                held = abs(float(a) - e) <= self.tolerance * max(
                    size.get(key, 0.0), abs(float(a)), abs(e), 1.0
                )
            if not held:
                failures.append(Failure(key, float(e), float(a)))
        return len(keys) - len(failures), len(keys), failures[:10]

    def _run(self, checks: Collection[_Check]) -> None:
        for check in checks:
            for request, aggregate in check.wanted():
                self.wanted[request].add(aggregate)
        for request, aggregates in self.wanted.items():
            missing = sorted(aggregates - set(self.results[request]))
            if not missing:
                continue
            try:
                self.results[request].update(self._query(request, missing))
                continue
            except Exception as error:  # a relation that is missing, or one aggregate that will not run
                if len(missing) == 1:
                    self.results[request][missing[0]] = _error(error)
                    continue
            for aggregate in missing:
                try:
                    self.results[request].update(self._query(request, [aggregate]))
                except Exception as error:  # this aggregate will not run on this relation
                    self.results[request][aggregate] = _error(error)

    def _query(self, request: _Request, aggregates: list[str]) -> dict[str, _ByDay | str]:
        relation_id, filter_sql, partition_sql = request
        node = self.graph.nodes.get(relation_id)
        if node is None or not node.relation:
            raise LookupError(f'{relation_id} is not a relation in the graph')
        table = _engine_table(self.engine, node.relation)
        columns = [sqlglot.parse_one(a, read=self.dialect).as_(f'a{i}') for i, a in enumerate(aggregates)]
        if partition_sql:
            day = exp.cast(_condition(partition_sql, self.dialect), exp.DataType.Type.DATE)
            query = exp.select(day.as_('kp_day'), *columns).from_(table).group_by(day.copy())
        else:
            query = exp.select(*columns).from_(table)
        if filter_sql:
            query = query.where(_condition(filter_sql, self.dialect))
        sql = query.sql(dialect=self.engine.dialect)
        read_only_query(sql, self.engine.dialect)
        _, rows = self.engine.read(sql)
        found: dict[str, _ByDay] = {a: {} for a in aggregates}
        for row in rows:
            key, values = (_partition_key(row[0]), row[1:]) if partition_sql else ('all', row)
            for aggregate, value in zip(aggregates, values, strict=True):
                found[aggregate][key] = _add(found[aggregate].get(key, 0), _amount(value))
        return dict(found)


def _error(error: Exception) -> str:
    return f'{type(error).__name__}: {str(error).strip().splitlines()[0][:160]}'


def _groups(law: Law) -> dict[tuple[str, str | None], tuple[Step, ...]]:
    """The law's steps by model and column: one, or one for each branch of a ``UNION ALL``."""
    found: dict[tuple[str, str | None], list[Step]] = defaultdict(list)
    for step in law.steps:
        found[(step.model, step.column)].append(step)
    return {key: tuple(steps) for key, steps in found.items()}


def _condition(sql: str, dialect: str) -> exp.Expr:
    """A filter or partition from a law, read as one expression over a relation's columns: a
    laws.json edited by hand can narrow what a total reads, never add a statement or a query."""
    statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    if len(statements) != 1 or not isinstance(statements[0], exp.Condition):
        raise ValueError(f'{sql[:80]!r} is not one expression over the columns')
    if statements[0].find(exp.Select, exp.Subquery, exp.Command) is not None:
        raise ValueError(f'{sql[:80]!r} reads more than the relation it filters')
    # AND and OR are functions to sqlglot, and join the conditions of a law with several filters.
    impure = next(
        (f for f in statements[0].find_all(exp.Func) if not isinstance(f, (*_PURE, exp.Connector))), None
    )
    if impure is not None:
        raise ValueError(f'{sql[:80]!r} calls {impure.sql_name()}, which is not a known pure function')
    return statements[0]


# The functions a law's filter or partition may call: pure ones over a row's values, as a model's
# filters and days use them. Anything else, such as pg_sleep or dblink_exec, is refused.
_PURE: tuple[type[exp.Expr], ...] = tuple(
    getattr(exp, name)
    for name in (
        'Cast',
        'TryCast',
        'Coalesce',
        'Case',
        'If',
        'Nullif',
        'Lower',
        'Upper',
        'Initcap',
        'Trim',
        'Substring',
        'Length',
        'Concat',
        'ConcatWs',
        'Replace',
        'StartsWith',
        'StrPosition',
        'Left',
        'Right',
        'RegexpLike',
        'Abs',
        'Round',
        'Floor',
        'Ceil',
        'Sign',
        'Greatest',
        'Least',
        'DateTrunc',
        'TimestampTrunc',
        'DatetimeTrunc',
        'Extract',
        'DateAdd',
        'DateSub',
        'DateDiff',
        'Date',
        'TsOrDsToDate',
        'StrToDate',
        'StrToTime',
        'TimeToStr',
        'Year',
        'Month',
        'Day',
        'DayOfWeek',
        'DateFromParts',
        'ToChar',
    )
)


def _quoted(column: str | None) -> str:
    return exp.to_identifier(column or '').sql() if column else '*'


def _engine_table(engine: Engine, key: str) -> exp.Table:
    """The relation ``key`` as the engine names it: catalog, schema and table in DuckDB, schema and
    table in the Postgres database connected to."""
    parts = key.split('.')
    database = str(getattr(engine, 'database', ''))
    if len(parts) != 3 or parts[0] != database:
        raise LookupError(f'{key} is not in the warehouse {database}')
    quoted = [exp.to_identifier(p, quoted=True) for p in parts]
    if isinstance(engine, PostgresEngine):
        return exp.Table(this=quoted[2], db=quoted[1])
    if isinstance(engine, DuckDBEngine):
        return exp.Table(this=quoted[2], db=quoted[1], catalog=quoted[0])
    raise LookupError(f'{type(engine).__name__} cannot read whole tables')


# Additivity probes


def _probe(engine: Engine, graph: Graph, root: Path, laws: list[Law], max_rows: int) -> None:
    by_model: dict[str, list[Law]] = defaultdict(list)
    for law in laws:
        if law.unprobed or not law.terms:
            law.status = 'unchecked'
            law.note = law.unprobed or f'{law.column} reads no input the probes can repeat'
        else:
            by_model[law.relation].append(law)
    if not by_model:
        return
    sql = {c.node.id: c for c in candidates(graph, root)}
    for model, group in sorted(by_model.items()):
        candidate = sql.get(model)
        try:
            if candidate is None:
                raise LookupError('no SQL to rerun')
            _probe_model(engine, graph, candidate, group, max_rows)
        except Exception as error:  # a model that will not run on its sample is reported, not fatal
            for law in group:
                law.status = 'unchecked'
                law.note = f'{type(error).__name__}: {str(error).strip().splitlines()[0][:160]}'


def _probe_model(engine: Engine, graph: Graph, candidate: Candidate, laws: list[Law], max_rows: int) -> None:
    catalog, schema = candidate.default_catalog, candidate.default_schema
    dialect, write = engine.read_dialect, engine.dialect
    _, inputs = rewrite(candidate.sql, {}, catalog, schema, dialect, write)
    engine.start()
    mapping: dict[str, str] = {}
    columns: dict[str, list[tuple[str, str]]] = {}
    for index, key in enumerate(sorted(inputs)):
        mapping[key], columns[key] = engine.copy(key, index, max_rows)
    query, _ = rewrite(candidate.sql, mapping, catalog, schema, dialect, write)
    names, baseline = engine.run(query)
    if not baseline:
        raise RuntimeError('the model returns no rows on the sample')
    doubled: dict[str, list[tuple[Any, ...]]] = {}
    repeated: dict[tuple[str, int], list[tuple[Any, ...]]] = {}
    halves: dict[tuple[str, str], _Halves | None] = {}
    for law in laws:
        source = graph.nodes.get(law.terms[0].source)
        relation = source.relation if source is not None else None
        if relation is None or relation not in mapping or law.column not in names:
            law.status, law.note = 'unchecked', 'the input or the column is not in the rerun query'
            continue
        scratch = mapping[relation]
        if relation not in doubled:
            twice = f'SELECT * FROM {scratch} UNION ALL SELECT * FROM {scratch}'
            doubled[relation] = engine.run(_swap(query, scratch, twice, write))[1]
        index = names.index(law.column)
        date = next((name for name, dtype in columns[relation] if _DATE.match(dtype)), None)
        changed: list[tuple[Any, ...]] = []
        # The first rows (by date) in turn, until one reaches the column: a CASE inside the SUM
        # can leave a row out.
        for k in REPEATS:
            if (relation, k) not in repeated:
                once_more = _repeat(scratch, columns[relation], date, k)
                repeated[(relation, k)] = engine.run(_swap(query, scratch, once_more, write))[1]
            changed = _changed(baseline, repeated[(relation, k)], index)
            if changed:
                break
        listed = [name for name, _ in columns[relation]]
        at = date or (law.terms[0].source_column if law.terms[0].source_column in listed else None)
        if at is not None and (relation, at) not in halves:
            halves[(relation, at)] = _split(engine, query, scratch, columns[relation], at, write)
        split = halves.get((relation, at)) if at is not None else None
        _classify(law, names, baseline, doubled[relation], changed, split)


@dataclass
class _Halves:
    """The model rerun on two disjoint halves of one input, split at the middle value of a column."""

    column: str
    first: list[tuple[Any, ...]]
    second: list[tuple[Any, ...]]


def _split(
    engine: Engine, query: str, scratch: str, columns: list[tuple[str, str]], key: str, dialect: str
) -> _Halves | None:
    """``query`` rerun on the rows of ``scratch`` up to the middle value of ``key`` and on the rest,
    or None when ``key`` has a single value. A model that sums its input row by row gives totals
    on the halves that add up to its total; one that averages, takes a maximum or carries a balance
    across the split does not."""
    quoted = exp.to_identifier(key, quoted=True).sql()
    _, counted = engine.run(f'SELECT COUNT(DISTINCT {quoted}) FROM {scratch}')
    middle = int(counted[0][0] or 0) // 2
    if middle < 1:
        return None
    listed = ', '.join(exp.to_identifier(name, quoted=True).sql() for name, _ in columns)
    ranked = f'(SELECT *, DENSE_RANK() OVER (ORDER BY {quoted}) AS kp_rank FROM {scratch}) AS kp_h'
    first, second = (
        engine.run(_swap(query, scratch, f'SELECT {listed} FROM {ranked} WHERE kp_rank {test}', dialect))[1]
        for test in (f'<= {middle}', f'> {middle}')
    )
    return _Halves(key, first, second)


def _swap(query: str, scratch: str, replacement: str, dialect: str) -> str:
    """``query`` with the scratch copy ``scratch`` replaced by the subquery ``replacement``."""
    target = exp.to_table(scratch)
    tree = sqlglot.parse_one(query, dialect=dialect)

    def swap(node: exp.Expr) -> exp.Expr:
        if isinstance(node, exp.Table) and node.name == target.name and node.db == target.db:
            replaced = sqlglot.parse_one(replacement, dialect=dialect)
            assert isinstance(replaced, exp.Query)
            return replaced.subquery(node.alias or node.name)
        return node

    return tree.transform(swap).sql(dialect=dialect)


def _repeat(scratch: str, columns: list[tuple[str, str]], date: str | None, k: int) -> str:
    listed = ', '.join(exp.to_identifier(name, quoted=True).sql() for name, _ in columns)
    order = f'ORDER BY {exp.to_identifier(date, quoted=True).sql()}' if date else ''
    return (
        f'SELECT * FROM {scratch} UNION ALL SELECT {listed} FROM '
        f'(SELECT *, ROW_NUMBER() OVER ({order}) AS kp_rn FROM {scratch}) AS kp_r WHERE kp_rn = {int(k)}'
    )


def _total(rows: list[tuple[Any, ...]], index: int) -> float:
    return sum(_value(row[index]) for row in rows if row[index] is not None)


def _changed(
    baseline: list[tuple[Any, ...]], rows: list[tuple[Any, ...]], index: int
) -> list[tuple[Any, ...]]:
    """The baseline rows whose ``index`` column changed, told apart by the columns the change left
    as they were."""
    width = len(baseline[0]) if baseline else 0
    stable = [
        i
        for i in range(width)
        if i != index and Counter(repr(r[i]) for r in baseline) == Counter(repr(r[i]) for r in rows)
    ]

    def keyed(source: list[tuple[Any, ...]]) -> Counter[str]:
        return Counter(repr((tuple(r[i] for i in stable), r[index])) for r in source)

    gone = keyed(baseline) - keyed(rows)
    return [r for r in baseline if gone.get(repr((tuple(r[i] for i in stable), r[index])), 0) > 0]


_STRENGTH = {'non-additive': 0, 'semi-additive': 1, 'additive': 2}


def _classify(
    law: Law,
    names: list[str],
    baseline: list[tuple[Any, ...]],
    doubled: list[tuple[Any, ...]],
    changed: list[tuple[Any, ...]],
    halves: _Halves | None = None,
) -> None:
    assert law.column is not None
    index = names.index(law.column)
    total = _total(baseline, index)
    source = law.terms[0].source_label
    if total == 0:
        law.status, law.note = 'unchecked', f'{law.column} totals zero on the sample'
        return
    ratio = _total(doubled, index) / total
    # A model that keeps one row per input row returns twice the rows, each with its own value:
    # an additive column's total then doubles once per row, and one summed row by row doubles twice.
    twice = 2 * len(doubled) / len(baseline)
    days = max(
        [len({r[i] for r in changed if isinstance(r[i], datetime.date)}) for i in range(len(names))] or [0]
    )
    split = _total(halves.first, index) + _total(halves.second, index) if halves else total
    size = max(sum(abs(_value(r[index])) for r in baseline), 1.0)
    # A balance restarts on the second half, so the halves are compared only once the days probe
    # has found none.
    if abs(ratio - twice) > 1e-6 * twice:
        found = 'non-additive'
        evidence = f'every row of {source} twice multiplied the total of {law.column} by {ratio:.4g}, not {twice:.4g}'
    elif days > 1:
        found = 'semi-additive'
        evidence = (
            f'every row of {source} twice doubled the total, and one row twice changed {len(changed)} rows '
            f'of {law.column} across {days} days, so a total over days counts that row more than once'
        )
    elif halves is not None and abs(split - total) > 1e-6 * size:
        found = 'non-additive'
        evidence = (
            f'the totals of {law.column} on the two halves of {source} split by {halves.column} add up '
            f'to {_number_text(split)}, not {_number_text(total)}'
        )
    elif not changed:
        law.status, law.note = (
            'unchecked',
            f'repeating a row of {source} did not change {law.column} on the sample',
        )
        return
    elif len(changed) == 1:
        found = 'additive'
        evidence = f'every row of {source} twice doubled the total, and one row twice changed one row'
        if halves:
            evidence += f', and the halves of {source} split by {halves.column} add up to the whole'
    else:
        found = 'non-additive'
        evidence = f'one row of {source} twice changed {len(changed)} rows of {law.column}'
    law.evidence = evidence
    expected = law.expected or 'additive'
    if _STRENGTH[found] > _STRENGTH[expected]:
        # The probes find totals that count a row more than once or do not add up; finding none
        # does not show that a column the SQL makes non-additive may be summed.
        law.status = 'unchecked'
        law.note = f'the SQL suggests {expected}, and the probes, which can only show less, found {found}'
        return
    law.classification, law.checked = found, 1
    law.held = int(found == expected)
    law.status = 'holds' if law.held else 'broken'


# The gate's check


def violations(graph: Graph, laws: Laws, change: Laws) -> list[Violation]:
    """The laws in ``laws`` (as confirmed before a change) that held on every partition and that
    the changed project no longer satisfies.

    ``change`` is :func:`propose` on the changed project and ``graph`` its graph. A conservation or
    count law is violated when one of its steps no longer follows from that model's SQL (a new join
    on a key no uniqueness test covers, a new filter, a removed test, a different factor) and the
    violation names the model, nearest the source first; an additivity law is violated when the SQL
    now suggests another class. Nothing is run.
    """
    expected = {(law.relation, law.column): law.expected for law in change.laws if law.kind == 'additivity'}
    blocked = {(b.model, b.column): b.reason for b in change.blocked}
    found: list[Violation] = []
    for law in laws.laws:
        if not law.always_held:
            continue
        if law.kind == 'additivity':
            now = expected.get((law.relation, law.column))
            if now != law.expected:
                reason = blocked.get((law.relation, law.column))
                message = (
                    f'{law.column} now looks {now}'
                    if now
                    else f'{law.column} is no longer an aggregate' + (f' ({reason})' if reason else '')
                )
                found.append(_violation(graph, law, law.relation, message))
            continue
        groups = _groups(law)
        for step in (step for term in law.terms for step in reversed(term.steps)):
            mine = groups[(step.model, step.column)]
            current = change.steps.get((step.model, step.column))
            unchanged = current is not None and len(current) == len(mine)
            if unchanged and all(a.same(b) for a, b in zip(current or (), mine, strict=True)):
                continue
            found.append(_violation(graph, law, step.model, _what_changed(graph, mine, current, blocked)))
            break
    return found


def _violation(graph: Graph, law: Law, model: str, message: str) -> Violation:
    node = graph.nodes.get(model)
    label = graph.label(node) if node is not None else _short(model)
    return Violation(law.id, law.statement, model, label, f'{label}: {message}', node.path if node else None)


def _what_changed(
    graph: Graph,
    steps: tuple[Step, ...],
    now: tuple[Step, ...] | None,
    blocked: dict[tuple[str, str | None], str],
) -> str:
    step = steps[0]
    node = graph.nodes.get(step.model)
    what = 'its row count' if step.column is None else step.column
    if node is None:
        return 'the model is removed'
    if now is None:
        reason = blocked.get((step.model, step.column))
        if reason:
            return f'{what}: {reason}'
        if step.column is not None and graph.column(node.id, step.column) is None:
            return f'{step.column} is removed'
        return f'{what} no longer follows from {_short(step.input)}'
    if len(now) != len(steps):
        return f'{what} now adds up {len(now)} branches instead of {len(steps)}'
    before, after = next((a, b) for a, b in zip(steps, now, strict=True) if not a.same(b))
    return f'{what} now {_describe(after)} instead of {_describe(before)}'


def _describe(step: Step) -> str:
    verb = {'sum': 'sums', 'count': 'counts', 'rows': 'maps the rows of'}[step.op]
    column = f'.{step.input_column}' if step.input_column else ''
    text = f'{verb} {_short(step.input)}{column}{_factor_text(step.factor)}'
    return f'{text} where {step.filter}' if step.filter else text
