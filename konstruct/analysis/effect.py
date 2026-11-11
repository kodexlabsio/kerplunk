"""`konstruct verify --effect`: what each change in the release notes does on today's data.

A number moves when its definition changes or when its data does. For each formula or row change
in :mod:`konstruct.analysis.release_notes`, this module separates the first: it runs the changed
model's SQL as it was before the commit and as it was after, both on the same data, the warehouse
as it is now, so any difference is the definition's alone. dbt refs and macros are rendered as they
are now for both versions, so both read today's tables.

It runs the way :mod:`konstruct.analysis.verify` reruns models, through the same engines and
sandbox: each version must be a single query that only reads, the tables either version reads are
copied (up to a number of rows each) and both versions run on the copies. Nothing is written to
the warehouse. For each change it reports the rows each version returns, for a column the sum of
its values in each when they are numbers, and, where the relation has a key (a unique test, a
primary key), how many rows of the same key changed value and how many keys appeared or
disappeared.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any

from konstruct.analysis.diff import Context, file_at, run_git
from konstruct.analysis.release_notes import Change, ReleaseNotes
from konstruct.analysis.verify import Engine, defaults, engine_for, query_for, rewrite
from konstruct.core.graph import Graph, Node
from konstruct.core.sql import PLACEHOLDER
from konstruct.semantics.ontology import derive

if TYPE_CHECKING:
    from konstruct.semantics.ontology import Ontology

Output = tuple[list[str], list[tuple[Any, ...]]]


@dataclass
class ColumnEffect:
    """What one change does to one column of the model on today's data."""

    column: str
    """The column."""
    total_before: float | None = None
    """The sum of its values before the commit, when they are numbers."""
    total_after: float | None = None
    """The sum after."""
    matched: int | None = None
    """Rows of the same key in both versions, when the relation has a key."""
    changed: int | None = None
    """Of those, the rows whose value of the column differs."""
    only_before: int | None = None
    """For a formula change with no key to match rows by, the column's values only the version
    before the commit gives, counted with repeats."""
    only_after: int | None = None
    """The values only the version after the commit gives."""

    @property
    def moved(self) -> bool:
        """Whether the column's sum or any of its values changed."""
        return (
            bool(self.changed)
            or bool(self.only_before or self.only_after)
            or not _same(self.total_before, self.total_after)
        )

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def text(self, key: list[str]) -> str | None:
        parts = []
        if self.total_before is not None and self.total_after is not None:
            parts.append(f'the sum of {self.column} {_move(self.total_before, self.total_after)}')
        if self.matched is not None:
            parts.append(
                f'{self.changed} of {_count(self.matched, "row")} with the same {", ".join(key)} change '
                f'{self.column}'
            )
        if self.only_before is not None and self.only_after is not None:
            if self.only_before or self.only_after:
                parts.append(
                    f'{_count(self.only_before, "value")} of {self.column} only before and '
                    f'{self.only_after} only after'
                )
            else:
                parts.append(f'the same values of {self.column} in both')
        return '; '.join(parts) or None


@dataclass
class Effect:
    """What one change does on today's data: the changed model's SQL before and after the commit,
    run on the same rows."""

    rows_before: int | None = None
    """Rows the model returns as it was before the commit."""
    rows_after: int | None = None
    """Rows it returns as it was after."""
    columns: list[ColumnEffect] = field(default_factory=list)
    """For a formula change, the changed column; for a change to the relation's joins, filters or
    grouping, every column in both versions other than the key."""
    every_column: bool = False
    """Whether every column was compared, for a change to the relation's joins, filters or
    grouping; then only the columns that moved are named in the summary."""
    key: list[str] = field(default_factory=list)
    """The key rows are matched by, when the relation has one."""
    gained: int | None = None
    """Keys only after the commit."""
    only_before: int | None = None
    """For a change to the rows of a relation with no key, the rows (over the columns both versions
    return) only the version before the commit gives, counted with repeats."""
    only_after: int | None = None
    """The rows only the version after the commit gives."""
    lost: int | None = None
    """Keys only before it."""
    sampled: bool = False
    """Whether a copy reached the row limit, so the result may be for a sample."""
    skipped: str | None = None
    """Why the effect could not be worked out, when it could not."""

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, 'columns': [c.to_dict() for c in self.columns]}

    def summary(self) -> str:
        """The effect as one sentence, every number in it from the two runs. For a change to the
        rows, only the columns that moved are named."""
        if self.skipped:
            return f"effect on today's data not worked out: {self.skipped}"
        where = "on what may be a sample of today's data" if self.sampled else "on today's data"
        parts = []
        if self.rows_before != self.rows_after:
            parts.append(f'{_count(self.rows_before or 0, "row")} before and {self.rows_after} after')
        else:
            parts.append(f'{_count(self.rows_before or 0, "row")} in both')
        shown = [c for c in self.columns if c.moved] if self.every_column else self.columns
        parts += [t for t in (c.text(self.key) for c in shown) if t]
        if self.only_before is not None and self.only_after is not None:
            # With no key, rows cannot be matched: values that moved between rows leave every sum
            # as it was, so the rows themselves are compared.
            if self.only_before or self.only_after:
                parts.append(
                    f'{_count(self.only_before, "row")} only before and {self.only_after} only after'
                )
            else:
                parts.append('the same rows in both')
        elif self.every_column and not shown:
            parts.append('no column changes value')
        if self.gained or self.lost:
            parts.append(f'{self.gained} {", ".join(self.key)} appear and {self.lost} disappear')
        return f'{where}: ' + '; '.join(parts)


def _count(value: int, noun: str) -> str:
    return f'{value} {noun}' + ('' if value == 1 else 's')


def _move(before: float, after: float) -> str:
    if _same(before, after):
        return f'is {_number(before)} in both'
    if math.isnan(before) or math.isnan(after):
        return f'changes from {_number(before)} to {_number(after)}'
    if math.isinf(before) or math.isinf(after):
        # A change to or from infinity has a direction but no proportion.
        direction = 'rises' if after > before else 'falls'
        return f'{direction} from {_number(before)} to {_number(after)}'
    direction = 'rises' if after > before else 'falls'
    text = f'{direction} from {_number(before)} to {_number(after)}'
    if before:
        text += f' ({(after - before) / abs(before) * 100:+.1f}%)'
    return text


def _number(value: float) -> str:
    if math.isnan(value):
        return 'not a number (NaN)'
    if math.isinf(value):
        return 'infinity' if value > 0 else 'minus infinity'
    return f'{value:,.2f}' if not float(value).is_integer() else f'{int(value):,}'


def runnable(notes: ReleaseNotes) -> bool:
    """Whether ``notes`` hold a formula or row change, which :func:`effects` runs."""
    return any(c.kind in ('formula', 'rows') for c in notes.changes)


def effects(
    graph: Graph,
    root: Path,
    notes: ReleaseNotes,
    warehouse: str | Path | Engine,
    *,
    max_rows: int = 1_000_000,
    ontology: Ontology | None = None,
) -> ReleaseNotes:
    """``notes`` with the effect of each formula and row change on today's data filled in. With
    no such change, no engine is opened."""
    groups: dict[tuple[str, str, str], list[Change]] = {}
    for change in notes.changes:
        if change.kind in ('formula', 'rows'):
            groups.setdefault((change.commit.sha, change.path, change.relation or ''), []).append(change)
    if not groups:
        return notes
    top = Path(run_git(root, 'rev-parse', '--show-toplevel').strip())
    prefix = Path(root).resolve().relative_to(top.resolve())
    engine: Engine | None = None
    try:
        engine = engine_for(warehouse)
        notes.warehouse = engine.label
        runner = _Runner(graph, root, top, prefix, engine, max_rows, ontology or derive(graph))
        for group in groups.values():
            runner.run(group)
    finally:
        if engine is not None:
            engine.close()
    return notes


class _Runner:
    def __init__(
        self,
        graph: Graph,
        root: Path,
        top: Path,
        prefix: Path,
        engine: Engine,
        max_rows: int,
        ontology: Ontology,
    ) -> None:
        self.graph = graph
        self.top = top
        self.prefix = prefix
        self.engine = engine
        self.max_rows = max_rows
        self.context = Context(graph, root)
        self.keys: dict[str, list[str]] = {}
        for entity in ontology.entities:
            for key in entity.keys:
                relation, _, column = key.rpartition('.')
                self.keys.setdefault(relation, []).append(column)

    def _path(self, path: str) -> str:
        return (self.prefix / path).as_posix() if str(self.prefix) != '.' else path

    def run(self, changes: list[Change]) -> None:
        first = changes[0]
        relation = self._relation(first)
        try:
            if relation is None:
                raise LookupError(f'{first.relation} is not in the graph now')
            texts = [
                file_at(self.top, f'{first.commit.sha}^', self._path(first.previous_path or first.path)),
                file_at(self.top, first.commit.sha, self._path(first.path)),
            ]
            before, after, sampled = self._outputs(relation, texts)
        except Exception as error:  # a version that will not run is reported, not fatal
            reason = (
                f'{type(error).__name__}: {str(error).strip().splitlines()[0][:160]}'
                if str(error)
                else type(error).__name__
            )
            for change in changes:
                change.effect = Effect(skipped=reason)
            return
        key = [k for k in self.keys.get(self.graph.label(relation), []) if k in before[0] and k in after[0]]
        for change in changes:
            change.effect = compare(change, before, after, key, sampled)

    def _relation(self, change: Change) -> Node | None:
        node = self.graph.nodes.get(change.node_id or '')
        if node is not None and node.kind == 'column':
            return self.graph.parent_of(node)
        if node is not None:
            return node
        return next(
            (n for n in self.graph.relations() if self.graph.label(n) == change.relation),
            None,
        )

    def _query(self, relation: Node, text: str | None) -> tuple[str, str | None, str | None]:
        """The query that builds ``relation`` in one version of its file, refused where templating
        could not be rendered as it would be run: a guess at the SQL is not run."""
        if text is None:
            raise LookupError('the file does not exist on this side of the commit')
        if relation.kind in ('model', 'snapshot') and 'dbt' in relation.origins:
            renderer = self.context.renderers.get(relation.id)
            if renderer is None and any(mark in text for mark in ('{%', '{#')):
                raise LookupError('the model has Jinja that renders only with the dbt artefacts (target/)')
            if renderer is not None:
                try:
                    renderer.render(text)
                except Exception as error:
                    raise LookupError(f'the model does not render as dbt would: {error}') from error
            sql, catalog, schema = self.context.model_sql(relation, text)
        else:
            if not relation.relation:
                raise LookupError(f'{self.graph.label(relation)} has no relation name')
            if any(mark in text for mark in ('{%', '{#')):
                raise LookupError('the SQL file has Jinja blocks, which are not rendered')
            found = query_for(text, relation.relation, self.graph)
            if found is None:
                raise LookupError(f'no query that builds {self.graph.label(relation)} in this version')
            sql, (catalog, schema) = found, defaults(self.graph)
        if PLACEHOLDER in sql:
            raise LookupError(
                f'the SQL is templated where it shows {PLACEHOLDER!r}, which only its own runner fills in'
            )
        return sql, catalog, schema

    def _outputs(self, relation: Node, texts: Sequence[str | None]) -> tuple[Output, Output, bool]:
        engine = self.engine
        dialect, write = engine.read_dialect, engine.dialect
        queries = [self._query(relation, t) for t in texts]
        inputs: set[str] = set()
        for sql, catalog, schema in queries:
            inputs |= rewrite(sql, {}, catalog, schema, dialect, write)[1]
        engine.start()
        mapping: dict[str, str] = {}
        sampled = False
        for index, key in enumerate(sorted(inputs)):
            mapping[key], _ = engine.copy(key, index, self.max_rows)
            _, counted = engine.run(f'SELECT count(*) FROM {mapping[key]}')
            # A copy that reached the limit may have left rows behind: counted as a sample, so a
            # remote warehouse is read once rather than again to count.
            sampled = sampled or counted[0][0] >= self.max_rows
        outputs = []
        for sql, catalog, schema in queries:
            query, _ = rewrite(sql, mapping, catalog, schema, dialect, write)
            outputs.append(engine.run(query))
        return outputs[0], outputs[1], sampled


def _numeric(values: list[Any]) -> float | None:
    present = [v for v in values if v is not None]
    if not present or not all(
        isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) for v in present
    ):
        return None
    total = math.fsum(float(v) for v in present)
    return round(total, 6)


def compare(change: Change, before: Output, after: Output, key: list[str], sampled: bool) -> Effect:
    """The effect of ``change`` from the model's output before and after the commit: for a formula
    change its column, and for a change to the rows every column both versions return but the
    key."""
    effect = Effect(rows_before=len(before[1]), rows_after=len(after[1]), sampled=sampled)
    by_key: tuple[dict[tuple[Any, ...], tuple[Any, ...]], dict[tuple[Any, ...], tuple[Any, ...]]] | None = (
        None
    )
    if key:
        rows_before = {tuple(r[before[0].index(k)] for k in key): r for r in before[1]}
        rows_after = {tuple(r[after[0].index(k)] for k in key): r for r in after[1]}
        if len(rows_before) == len(before[1]) and len(rows_after) == len(after[1]):
            effect.key = key
            effect.gained = len(rows_after.keys() - rows_before.keys())
            effect.lost = len(rows_before.keys() - rows_after.keys())
            by_key = rows_before, rows_after
    if change.column:
        names = [change.column.rsplit('.', 1)[-1].lower()]
    else:
        effect.every_column = True
        names = [n for n in after[0] if n in before[0] and n not in effect.key]
        if by_key is None:
            old_rows = Counter(tuple(_value(r[before[0].index(n)]) for n in names) for r in before[1])
            new_rows = Counter(tuple(_value(r[after[0].index(n)]) for n in names) for r in after[1])
            effect.only_before = sum((old_rows - new_rows).values())
            effect.only_after = sum((new_rows - old_rows).values())
    for name in names:
        column = ColumnEffect(name)
        effect.columns.append(column)
        if name not in before[0] or name not in after[0]:
            continue
        old, new = before[0].index(name), after[0].index(name)
        column.total_before = _numeric([r[old] for r in before[1]])
        column.total_after = _numeric([r[new] for r in after[1]])
        if by_key is not None:
            shared = by_key[0].keys() & by_key[1].keys()
            column.matched = len(shared)
            column.changed = sum(1 for k in shared if not _same(by_key[0][k][old], by_key[1][k][new]))
        elif not effect.every_column:
            # No key to match rows by: the column's values are compared as a whole, text included.
            old_values = Counter(_value(r[old]) for r in before[1])
            new_values = Counter(_value(r[new]) for r in after[1])
            column.only_before = sum((old_values - new_values).values())
            column.only_after = sum((new_values - old_values).values())
    return effect


def _value(value: Any) -> Any:
    """``value`` as a key equal for the values that are the same, by which both comparisons count
    a value as changed: 1, 1.0 and Decimal('1.00') are one number, a boolean is not a number (so
    rewriting 1 as TRUE is a change), every NaN is one value, and lists and maps compare by their
    items. Anything else that cannot be a key is compared by its text."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, Decimal)):
        if value != value:
            return ('number', 'NaN')
        try:
            return ('number', Fraction(value))
        except (OverflowError, ValueError):  # an infinity, which no fraction holds
            return ('number', 'infinity', value > 0)
    if isinstance(value, (list, tuple)):
        return ('sequence', tuple(_value(v) for v in value))
    if isinstance(value, dict):
        return ('map', tuple(sorted((repr(k), _value(v)) for k, v in value.items())))
    try:
        hash(value)
    except TypeError:
        return ('text', repr(value))
    return value


def _same(a: Any, b: Any) -> bool:
    """Whether ``a`` and ``b`` are the same value, by the one rule both the comparison by key and
    the comparison without one use (:func:`_value`): a number is not a boolean, so ``1`` and
    ``TRUE`` differ, and NaN equals NaN."""
    return bool(_value(a) == _value(b))
