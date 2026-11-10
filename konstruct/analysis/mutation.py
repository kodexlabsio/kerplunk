"""`konstruct verify --mutate`: mutation testing for a dbt project's data tests.

A test suite that passes says little until it is known to fail when the model is wrong. This
module makes small semantic mutants of each dbt model's compiled SQL (:mod:`konstruct.core.mutate`),
runs each mutant on the sample verify reads, and then runs the dbt tests that read the model,
compiled from ``manifest.json``, against the mutant's output. The model's dbt unit tests run too,
each on its own given rows. Each mutant is then:

* killed: at least one test fails on the mutant (and passed on the original);
* survived: the mutant's output differs from the original's, yet every test passes, so the tests
  are too weak to notice this change;
* equivalent: the mutant is the same query as the original in canonical form (CTEs and aliases
  inlined down to physical columns, as :mod:`konstruct.core.sql` writes them), so no test could
  kill it;
* indistinguishable: the mutant's output is the same as the original's on the sample, but the two
  are not the same query, so the sample has no row that exercises the change; this says nothing
  about the tests;
* invalid: the mutant does not run, or does not give the same rows twice.

The mutation score is killed / (killed + survived), per model and per output column. A model whose
tests could not run (none compiled, or each failing before any mutation) is skipped rather than
given a score of 0%; only a model with no tests at all scores 0%. For each
survivor the module finds, from the two outputs, a dbt test that would have killed it: a
``not_null``, ``unique``, ``accepted_values``, ``relationships`` or ``dbt_utils.expression_is_true``
test, or else a dbt unit test whose given rows are a small subset of the sample on which the
mutant and the original disagree.

Everything runs in verify's engines: in DuckDB with the warehouse attached read-only, in a
Postgres transaction that is rolled back, or on a sample of a cloud warehouse copied into an
in-memory DuckDB. Only queries are run: a model's SQL, and each test's, must pass
:func:`~konstruct.analysis.verify.read_only_query` before anything of it runs.
"""

from __future__ import annotations

import datetime
import decimal
import json
import math
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlglot import exp

from konstruct.analysis.verify import (
    Candidate,
    Engine,
    candidates,
    engine_for,
    quote,
    read_only_query,
    rewrite,
)
from konstruct.core.graph import Graph, Node
from konstruct.core.mutate import OPERATORS, Mutant, mutants, parse_query
from konstruct.core.sql import analyse
from konstruct.semantics.ontology import Relationship, derive

STATUSES = ('killed', 'survived', 'equivalent', 'indistinguishable', 'invalid')
ROW_COUNT = '(row count)'
"""The pseudo-column a mutant is scored against when it changes how many rows the model returns."""

_PLACEHOLDER = 'kp_subst_'
_TOKEN = re.compile(r'(?<![\w"])kp_subst_(\d+)(?![\w"])')
_THRESHOLD = re.compile(r'^\s*(==|=|!=|<>|>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)\s*$')
_IDENTIFIER = re.compile(r'^[a-z_][a-z0-9_]*$')
_CATEGORIES = 12
_UNIT_ROWS = 40
_BUDGET = 100
_PREFIXES = (8, 64)
_UNIT_TESTS = 3
_SCALARS = (bool, int, float, decimal.Decimal, str, datetime.date, datetime.time, uuid.UUID)


@dataclass
class Suggestion:
    """A dbt test that passes on the original model and fails on a survivor."""

    test: str
    """The dbt test: not_null, unique, accepted_values, relationships,
    dbt_utils.expression_is_true or unit_test."""
    column: str | None
    """The column it is on, for a column test."""
    reason: str
    """Why it kills the mutant, from the two outputs."""
    yaml: str
    """The test as a YAML list item, to paste where :attr:`where` says. Empty when the test is one
    already suggested for another survivor."""
    where: str = ''
    """Where the YAML goes in the project's properties files."""

    def to_dict(self) -> dict[str, Any]:
        return {
            'test': self.test,
            'column': self.column,
            'reason': self.reason,
            'yaml': self.yaml,
            'where': self.where,
        }


@dataclass
class MutantResult:
    """What became of one mutant: its status, what it changed and what killed it."""

    mutant: Mutant
    """The mutation."""
    status: str
    """One of :data:`STATUSES`."""
    rows: int = 0
    """Rows the mutant returned."""
    changed: list[str] = field(default_factory=list)
    """Output columns whose values changed, and ``(row count)`` when the number of rows did."""
    killed_by: list[str] = field(default_factory=list)
    """The tests that failed on the mutant."""
    reach: int = 0
    """Nodes downstream of the changed columns, which ranks survivors by what they would break."""
    suggestions: list[Suggestion] = field(default_factory=list)
    """For a survivor, the tests that would have killed it."""
    error: str | None = None
    """Why an invalid mutant could not be judged."""

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.mutant.to_dict(),
            'status': self.status,
            'rows': self.rows,
            'changed': self.changed,
            'killed_by': self.killed_by,
            'reach': self.reach,
            'suggestions': [s.to_dict() for s in self.suggestions],
            'error': self.error,
        }


@dataclass
class TestCheck:
    """One dbt test of the model (a data test or a unit test) and whether it could be used."""

    id: str
    """The dbt test's unique id."""
    name: str
    """Its name."""
    test: str
    """Its kind: not_null, relationships, a package test, singular, or unit_test."""
    column: str | None = None
    """The column it tests, when it is a column test."""
    state: str = 'used'
    """``used``; ``fails on the sample`` (a data test that fails before any mutation, so it cannot
    tell a mutant apart); ``fails on the original`` (a unit test the original model already fails);
    or ``not run`` (it has no compiled SQL, its SQL will not run here, or its rows cannot be read)."""
    reason: str | None = None
    """Why it was not used."""
    kills: int = 0
    """Mutants it killed."""

    def to_dict(self) -> dict[str, Any]:
        return {
            'id': self.id,
            'name': self.name,
            'test': self.test,
            'column': self.column,
            'state': self.state,
            'reason': self.reason,
            'kills': self.kills,
        }


@dataclass
class ColumnScore:
    """The mutants that changed one output column, killed and survived."""

    killed: int = 0
    """Mutants that changed the column and that a test killed."""
    survived: int = 0
    """Mutants that changed the column and that every test let through."""

    @property
    def score(self) -> float | None:
        total = self.killed + self.survived
        return self.killed / total if total else None


def _percent(score: float | None) -> str:
    return '-' if score is None else f'{100 * score:.0f}%'


@dataclass
class ModelMutation:
    """The mutation testing of one dbt model: its tests, its mutants and its score."""

    node_id: str
    """The dbt model."""
    label: str
    """Its short name."""
    rows: int = 0
    """Rows the original model returns on the sample."""
    sites: int = 0
    """Every mutation its SQL admits, before sampling."""
    tests: list[TestCheck] = field(default_factory=list)
    """The dbt tests that read the model."""
    mutants: list[MutantResult] = field(default_factory=list)
    """The sampled mutants and what became of each."""
    skipped: str | None = None
    """Why the model was not mutation tested, when it was not."""
    seconds: float = 0.0
    """Time taken."""

    def count(self, status: str) -> int:
        return sum(1 for m in self.mutants if m.status == status)

    @property
    def score(self) -> float | None:
        """Killed over killed and survived: the share of the changes the data shows that the
        tests notice. None when no mutant changed the output."""
        killed, survived = self.count('killed'), self.count('survived')
        return killed / (killed + survived) if killed + survived else None

    def columns(self) -> dict[str, ColumnScore]:
        """The score of each output column (and ``(row count)``), over the mutants that changed it."""
        scores: dict[str, ColumnScore] = {}
        for result in self.mutants:
            if result.status not in ('killed', 'survived'):
                continue
            for column in result.changed:
                score = scores.setdefault(column, ColumnScore())
                if result.status == 'killed':
                    score.killed += 1
                else:
                    score.survived += 1
        return dict(sorted(scores.items()))

    @property
    def survivors(self) -> list[MutantResult]:
        """Survivors, the ones with the most downstream reach first."""
        found = [m for m in self.mutants if m.status == 'survived']
        return sorted(found, key=lambda m: (-m.reach, m.mutant.id))

    def to_dict(self) -> dict[str, Any]:
        return {
            'node_id': self.node_id,
            'label': self.label,
            'rows': self.rows,
            'sites': self.sites,
            'score': None if self.score is None else round(self.score, 4),
            'counts': {status: self.count(status) for status in STATUSES},
            'columns': {
                name: {
                    'killed': c.killed,
                    'survived': c.survived,
                    'score': None if c.score is None else round(c.score, 4),
                }
                for name, c in self.columns().items()
            },
            'tests': [t.to_dict() for t in self.tests],
            'mutants': [m.to_dict() for m in self.mutants],
            'skipped': self.skipped,
            'seconds': self.seconds,
        }


@dataclass
class MutationTesting:
    """The result of :func:`mutate`: one :class:`ModelMutation` per model, and the settings that
    make the run repeatable."""

    warehouse: str
    """The DuckDB file, Postgres database or sampled warehouse the mutants ran on."""
    seed: int
    """The seed of the mutant sample, so a run can be repeated exactly."""
    max_rows: int
    """The most rows sampled from any input."""
    max_mutants: int
    """The most mutants run per model."""
    operators: tuple[str, ...]
    """The kinds of mutation made."""
    models: list[ModelMutation] = field(default_factory=list)
    """One result per model."""
    seconds: float = 0.0
    """Total time taken."""

    @property
    def checked(self) -> list[ModelMutation]:
        return [m for m in self.models if not m.skipped]

    def totals(self) -> dict[str, int]:
        checked = self.checked
        counts = {status: sum(m.count(status) for m in checked) for status in STATUSES}
        return {
            'models': len(checked),
            'skipped': len(self.models) - len(checked),
            'mutants': sum(len(m.mutants) for m in checked),
            **counts,
        }

    @property
    def score(self) -> float | None:
        t = self.totals()
        return t['killed'] / (t['killed'] + t['survived']) if t['killed'] + t['survived'] else None

    def to_dict(self) -> dict[str, Any]:
        return {
            'warehouse': self.warehouse,
            'seed': self.seed,
            'max_rows': self.max_rows,
            'max_mutants': self.max_mutants,
            'operators': list(self.operators),
            'seconds': round(self.seconds, 2),
            'score': None if self.score is None else round(self.score, 4),
            'totals': self.totals(),
            'models': [m.to_dict() for m in self.models],
        }

    def summary(self) -> str:
        t = self.totals()
        if not t['models']:
            return 'No model could be mutation tested.'
        told = t['killed'] + t['survived']
        lines = [
            f'Mutation score {_percent(self.score)}: the tests killed {t["killed"]} of the {told} '
            f'mutant{"s" if told != 1 else ""} whose output differed on the sample, '
            f'across {t["models"]} model{"s" if t["models"] != 1 else ""}.'
        ]
        if t['equivalent']:
            many = t['equivalent'] != 1
            lines.append(
                f'{t["equivalent"]} mutant{"s are" if many else " is"} the same query as the original in '
                f'canonical form, so no test could kill {"them" if many else "it"}.'
            )
        if t['indistinguishable']:
            many = t['indistinguishable'] != 1
            lines.append(
                f'{t["indistinguishable"]} mutant{"s" if many else ""} gave the same output as the original on '
                'this data without being the same query: the sample has no row that exercises '
                f'{"these changes" if many else "the change"}, so {"they say" if many else "it says"} nothing '
                'about the tests.'
            )
        if t['invalid']:
            lines.append(f'{t["invalid"]} mutant{"s" if t["invalid"] != 1 else ""} did not run.')
        return '\n'.join(lines)

    def __str__(self) -> str:
        header = (
            f'Mutation testing against {self.warehouse} (seed {self.seed}, up to {self.max_mutants} mutants '
            f'per model, up to {self.max_rows} rows per input)'
        )
        width = max([len(m.label) for m in self.models] + [5])
        rows = [
            f'  {"model".ljust(width)}  mutants  killed  survived  equivalent  indistinguishable  invalid  score'
        ]
        for m in self.models:
            if m.skipped:
                rows.append(f'  {m.label.ljust(width)}  skipped: {m.skipped}')
                continue
            rows.append(
                f'  {m.label.ljust(width)}  {len(m.mutants):>7}  {m.count("killed"):>6}  {m.count("survived"):>8}  '
                f'{m.count("equivalent"):>10}  {m.count("indistinguishable"):>17}  {m.count("invalid"):>7}  '
                f'{_percent(m.score):>5}'
            )
        text = [header, '', *rows, '', self.summary()]
        survivors = sorted(
            ((m, s) for m in self.checked for s in m.survivors), key=lambda pair: -pair[1].reach
        )
        if survivors:
            text += ['', 'Survivors, by downstream reach:']
            for model, survivor in survivors[:20]:
                text.append(
                    f'  {model.label}: {_where(survivor.mutant)}; changes {", ".join(survivor.changed)}'
                )
                if survivor.suggestions:
                    best = survivor.suggestions[0]
                    text.append(f'    would be killed by {_name(best)}: {best.reason}')
            if len(survivors) > 20:
                text.append(f'  and {len(survivors) - 20} more in MUTATION.md')
        unused = [(m, t) for m in self.checked for t in m.tests if t.state != 'used']
        if unused:
            text += ['', 'Tests not used:']
            text += [
                f'  {m.label}: {t.name} ({t.state}{": " + t.reason if t.reason else ""})'
                for m, t in unused[:20]
            ]
            if len(unused) > 20:
                text.append(f'  and {len(unused) - 20} more in MUTATION.md')
        return '\n'.join(text)

    def markdown(self) -> str:
        t = self.totals()
        lines = [
            '# Konstruct mutation testing',
            '',
            f'Warehouse `{self.warehouse}`, seed {self.seed}, up to {self.max_mutants} mutants per model and '
            f'{self.max_rows} rows per input, {self.seconds:.1f}s.',
            '',
            self.summary(),
            '',
            "Each mutant is a small change to a model's SQL. A mutant is *killed* when a dbt test fails on it, "
            '*survived* when its output differs from the original but every test passes, *equivalent* when '
            'it is the same query as the original in canonical form, and *not distinguishable on this data* '
            'when its output is the same as the original on the sample although the query differs. The '
            'score is killed over killed and survived.',
            '',
            '| Model | Rows | Mutants | Killed | Survived | Equivalent | Not distinguishable | Invalid | Score |',
            '|-------|------|---------|--------|----------|------------|---------------------|---------|-------|',
        ]
        for m in self.models:
            if m.skipped:
                lines.append(f'| `{m.label}` | | | | | | | | skipped: {m.skipped} |')
                continue
            lines.append(
                f'| `{m.label}` | {m.rows} | {len(m.mutants)} | {m.count("killed")} | {m.count("survived")} | '
                f'{m.count("equivalent")} | {m.count("indistinguishable")} | {m.count("invalid")} | '
                f'{_percent(m.score)} |'
            )
        lines.append(
            f'| **Total** | | **{t["mutants"]}** | **{t["killed"]}** | **{t["survived"]}** | '
            f'**{t["equivalent"]}** | **{t["indistinguishable"]}** | **{t["invalid"]}** | '
            f'**{_percent(self.score)}** |'
        )
        for m in self.checked:
            lines += ['', f'## `{m.label}`', '']
            if not m.mutants:
                lines.append(
                    'Its SQL has nothing to mutate: no aggregate, filter, join, comparison, COALESCE or CASE.'
                )
            columns = m.columns()
            if columns:
                lines += ['| Column | Killed | Survived | Score |', '|--------|--------|----------|-------|']
                lines += [
                    f'| `{name}` | {c.killed} | {c.survived} | {_percent(c.score)} |'
                    for name, c in columns.items()
                ]
                lines.append('')
            for survivor in m.survivors:
                lines.append(f'- Survived: {_where(survivor.mutant)}; changes {", ".join(survivor.changed)}.')
                for suggestion in survivor.suggestions[:1]:
                    lines.append(f'  Would be killed by {_name(suggestion)}: {suggestion.reason}.')
                    if suggestion.yaml:
                        lines += [
                            f'  Add it to {suggestion.where}:',
                            '',
                            '  ```yaml',
                            *(f'  {line}' for line in suggestion.yaml.splitlines()),
                            '  ```',
                        ]
            for status, heading in (
                ('equivalent', 'Equivalent to the original:'),
                ('indistinguishable', 'Not distinguishable on this data:'),
            ):
                same = [r for r in m.mutants if r.status == status]
                if same:
                    lines += ['', heading, '']
                    lines += [f'- {_where(r.mutant)}' for r in same]
            unused = [t for t in m.tests if t.state != 'used']
            if unused:
                lines += ['', 'Tests not used:', '']
                lines += [f'- `{t.name}`: {t.state}{": " + t.reason if t.reason else ""}' for t in unused]
        return '\n'.join(lines) + '\n'


def _where(mutant: Mutant) -> str:
    return mutant.description + (f' in CTE `{mutant.scope}`' if mutant.scope else '')


def _name(suggestion: Suggestion) -> str:
    if suggestion.column:
        return f'{suggestion.test} on {suggestion.column}'
    return suggestion.test.replace('_', ' ') if suggestion.test == 'unit_test' else suggestion.test


# The dbt tests that read each model, compiled, from manifest.json


@dataclass
class _Test:
    check: TestCheck
    sql: str | None
    fail_calc: str = 'count(*)'
    error_if: str = '!= 0'
    warn_if: str = '!= 0'
    unit: dict[str, Any] | None = None
    """For a unit test, its manifest entry, with the given and the expected rows."""


@dataclass
class _Unit:
    """A unit test made ready to run: each input's given rows as SQL, and the expected rows."""

    check: TestCheck
    relations: dict[str, str]
    columns: list[str]
    expected: list[dict[str, Any]]


def _tests(root: Path) -> dict[str, list[_Test]]:
    """The dbt data tests and unit tests in the project's manifests, by each model they read."""
    found: dict[str, list[_Test]] = {}
    for path in sorted(root.rglob('target/manifest.json')):
        if 'dbt_packages' in path.parts:
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        for item in sorted(data.get('nodes', {}).values(), key=lambda i: str(i.get('unique_id'))):
            if item.get('resource_type') != 'test':
                continue
            config = item.get('config') or {}
            meta = item.get('test_metadata') or {}
            namespace = meta.get('namespace')
            kind = f'{namespace}.{meta["name"]}' if namespace and meta.get('name') else meta.get('name')
            test = _Test(
                TestCheck(item['unique_id'], item['name'], kind or 'singular', item.get('column_name')),
                item.get('compiled_code'),
                str(config.get('fail_calc') or 'count(*)'),
                str(config.get('error_if') or '!= 0'),
                str(config.get('warn_if') or '!= 0'),
            )
            _attach(found, item, test)
        for item in sorted(data.get('unit_tests', {}).values(), key=lambda i: str(i.get('unique_id'))):
            if item.get('resource_type') == 'unit_test':
                _attach(
                    found,
                    item,
                    _Test(TestCheck(item['unique_id'], item['name'], 'unit_test'), None, unit=item),
                )
    return found


def _attach(found: dict[str, list[_Test]], item: dict[str, Any], test: _Test) -> None:
    for node in dict.fromkeys(item.get('depends_on', {}).get('nodes', [])):
        tests = found.setdefault(node, [])
        if all(t.check.id != test.check.id for t in tests):
            tests.append(test)


def _holds(threshold: str, value: float) -> bool:
    match = _THRESHOLD.match(threshold)
    operator, bound = (match[1], float(match[2])) if match else ('!=', 0.0)
    return {
        '==': value == bound,
        '=': value == bound,
        '!=': value != bound,
        '<>': value != bound,
        '>=': value >= bound,
        '<=': value <= bound,
        '>': value > bound,
        '<': value < bound,
    }[operator]


# Comparing outputs


def _canon(value: Any) -> Any:
    if isinstance(value, float) and math.isnan(value):
        return ('nan',)
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


def _bag(rows: list[tuple[Any, ...]]) -> Counter[tuple[Any, ...]]:
    return Counter(tuple(_canon(v) for v in row) for row in rows)


def _changed(
    names: list[str], baseline: list[tuple[Any, ...]], rows: list[tuple[Any, ...]] | None, total: int
) -> list[str]:
    """The output columns a mutant changed. With the same number of rows a column changed when its
    values did; otherwise when its set of values did, and the row count is marked as changed. A
    mutant whose rows were not all read (``rows`` is None) changed the row count alone."""
    changed = []
    same = total == len(baseline)
    for index, name in enumerate(names if rows is not None else []):
        assert rows is not None
        before = [_canon(row[index]) for row in baseline]
        after = [_canon(row[index]) for row in rows]
        if (Counter(before) != Counter(after)) if same else (set(before) != set(after)):
            changed.append(name)
    if not same:
        changed.append(ROW_COUNT)
    return changed


# Running one model


class _Run:
    """The model's inputs copied into the engine, and the SQL to run it, a mutant or a test on it."""

    def __init__(
        self, engine: Engine, candidate: Candidate, tests: list[_Test], max_rows: int, shared: _Shared
    ) -> None:
        self.engine = engine
        self.shared = shared
        self.candidate = candidate
        self.dialect, self.write = engine.read_dialect, engine.dialect
        relation = candidate.node.relation or ''
        self.relation = relation
        _, inputs = self._rewrite(candidate.sql, {})
        self.inputs = sorted(inputs - {relation})
        self.tests: list[tuple[_Test, str, set[str]]] = []
        units: list[TestCheck] = []
        needed = set(self.inputs)
        for test in tests:
            check = TestCheck(**test.check.to_dict())
            if test.unit is not None:
                units.append(check)
                continue
            if not test.sql:
                check.state, check.reason = (
                    'not run',
                    'it has no compiled SQL in the manifest; run dbt compile',
                )
                self.tests.append((_Test(check, None, '', '', ''), '', set()))
                continue
            wrapper = f'SELECT {test.fail_calc} AS failures FROM ({test.sql.strip().rstrip(";")}) AS kp_test'
            try:
                read_only_query(test.sql, self.dialect)
                _, reads = self._rewrite(wrapper, {})
            except Exception as error:
                check.state, check.reason = 'not run', _reason(error)
                self.tests.append((_Test(check, None, '', '', ''), '', set()))
                continue
            self.tests.append(
                (_Test(check, test.sql, test.fail_calc, test.error_if, test.warn_if), wrapper, reads)
            )
            needed |= reads - {relation}
        self._templates: dict[tuple[str, tuple[str, ...]], str] = {}
        engine.start()
        self.mapping: dict[str, str] = {}
        self.columns: dict[str, list[tuple[str, str]]] = {}
        self.missing: dict[str, str] = {}
        self.max_rows = max_rows
        self.copied: list[str] = []
        for key in sorted(needed):
            if self.copy_of(key) is None and key in self.inputs:
                raise LookupError(self.missing[key])
        self._canonical: dict[str, tuple[Any, ...] | None] = {}
        self.unit_checks = units
        self.units: list[_Unit] = []
        specs = {t.check.id: t.unit for t in tests if t.unit is not None}
        for check in units:
            prepared = self._unit(check, specs[check.id] or {})
            if isinstance(prepared, str):
                check.state, check.reason = 'not run', prepared
            else:
                self.units.append(prepared)

    def _unit(self, check: TestCheck, spec: dict[str, Any]) -> _Unit | str:
        """The unit test ``spec`` ready to run on its given rows, or why it cannot be run."""
        if spec.get('overrides'):
            return 'it overrides macros, variables or environment variables, which its compiled SQL cannot follow'
        shown: dict[str, str] = {}
        for key in self.inputs:
            node = self.shared.graph.node_for_relation(key)
            ref = _dbt_ref(node) if node is not None else None
            shown[key] = ref or key
        refs = {_ref_text(ref): key for key, ref in shown.items()}
        given: dict[str, list[dict[str, Any]]] = {}
        for item in spec.get('given') or []:
            name = str(item.get('input'))
            rows = item.get('rows')
            if item.get('fixture') or item.get('format') == 'sql' or not _row_list(rows):
                return (
                    f'its rows for {name} are in a fixture file or SQL; only rows written in the YAML are run'
                )
            target = refs.get(_ref_text(name))
            if target is None:
                return f'{name} is not an input of the model'
            given[target] = rows
        missing = [key for key in self.inputs if key not in given]
        if missing:
            return f'it gives no rows for {shown[missing[0]]}'
        relations: dict[str, str] = {}
        for key, rows in given.items():
            columns = self.columns[key]
            known = {name.lower() for name, _ in columns}
            values: list[tuple[Any, ...]] = []
            for row in rows:
                named = {str(column).lower(): value for column, value in row.items()}
                unknown = sorted(set(named) - known)
                if unknown:
                    return f'its rows for {shown[key]} name {unknown[0]}, which the input does not have'
                values.append(tuple(named.get(name.lower()) for name, _ in columns))
            relations[key] = _values_sql(columns, values)
        expect = spec.get('expect') or {}
        rows = expect.get('rows')
        if expect.get('fixture') or expect.get('format') == 'sql' or not _row_list(rows):
            return 'its expected rows are in a fixture file or SQL; only rows written in the YAML are run'
        expected = [{str(column).lower(): value for column, value in row.items()} for row in rows or []]
        compared = list(expected[0]) if expected else []  # as in dbt, the first row's columns
        return _Unit(check, relations, compared, expected)

    def unit_fails(self, unit: _Unit, sql: str, names: list[str]) -> bool:
        """Whether the unit test fails on ``sql`` (the model or a mutant) run on its given rows. As
        dbt does, it compares the expected columns alone, as a multiset of rows, each value cast to
        the type of the model's column. The output's columns are renamed to the original's."""
        listed = ', '.join(quote(n) for n in names)
        query = f'SELECT * FROM ({self.with_inputs(sql, unit.relations)}) AS kp_model({listed})'
        _, rows = self.engine.run(query)
        index = {name: i for i, name in enumerate(names)}
        missing = [column for column in unit.columns if column not in index]
        if missing:
            raise LookupError(f'the model has no column {missing[0]}')
        positions = [index[column] for column in unit.columns]
        kinds = [_unit_kind([row[i] for row in rows]) for i in positions]
        actual = Counter(
            tuple(_unit_key(row[i], k) for i, k in zip(positions, kinds, strict=True)) for row in rows
        )
        expected = Counter(
            tuple(_unit_key(row.get(c), k) for c, k in zip(unit.columns, kinds, strict=True))
            for row in unit.expected
        )
        return actual != expected

    def canonical(self, sql: str) -> tuple[Any, ...] | None:
        """``sql`` in canonical form: each output column's expression and every condition in terms
        of physical columns, as :mod:`konstruct.core.sql` writes them, with each ORDER BY and window
        as written, which that form leaves out. None when the analysis is incomplete."""
        if sql not in self._canonical:
            schema = {key: dict(columns) for key, columns in self.columns.items()}
            found = analyse(
                sql,
                dialect=self.dialect,
                schema=schema,
                default_catalog=self.candidate.default_catalog,
                default_db=self.candidate.default_schema,
            )
            form: tuple[Any, ...] | None = None
            if len(found) == 1 and not found[0].errors and found[0].expressions:
                tree = parse_query(sql, self.dialect)
                ordering = tuple(n.sql(dialect=self.dialect) for n in tree.find_all(exp.Order, exp.Window))
                statement = found[0]
                form = (
                    tuple(sorted(statement.expressions.items())),
                    tuple(statement.conditions),
                    tuple(sorted(statement.reads)),
                    ordering,
                )
            self._canonical[sql] = form
        return self._canonical[sql]

    def same_query(self, sql: str, other: str) -> bool:
        """Whether two queries are the same in canonical form, so no data can tell them apart."""
        form = self.canonical(sql)
        return form is not None and form == self.canonical(other)

    def copy_of(self, key: str) -> str | None:
        """The copy of the relation ``key``, made now if it was not needed before; None when it
        cannot be read."""
        if key not in self.mapping and key not in self.missing and key != self.relation:
            try:
                self.mapping[key], self.columns[key] = self.engine.copy(key, len(self.copied), self.max_rows)
            except Exception as error:
                self.missing[key] = _reason(error)
            self.copied.append(key)
        return self.mapping.get(key)

    def relationships(self) -> list[Relationship]:
        """The derived ontology's relationships, derived once per run, when a suggestion first needs them."""
        return self.shared.relationships()

    def _rewrite(self, sql: str, mapping: dict[str, str]) -> tuple[str, set[str]]:
        return rewrite(
            sql,
            mapping,
            self.candidate.default_catalog,
            self.candidate.default_schema,
            self.dialect,
            self.write,
        )

    def query(self, sql: str) -> str:
        """``sql`` (the model or a mutant) reading the copies of its inputs."""
        return self._rewrite(sql, self.mapping)[0]

    def with_inputs(self, sql: str, relations: dict[str, str]) -> str:
        """``sql`` reading each input in ``relations`` from the subquery given for it, and the other
        inputs from their copies."""
        keys = tuple(sorted(relations))
        text = self._templates.get((sql, keys))
        if text is None:
            placeholders = {key: f'{_PLACEHOLDER}{i}' for i, key in enumerate(keys)}
            text = self._templates[(sql, keys)] = self._rewrite(sql, {**self.mapping, **placeholders})[0]
        return _TOKEN.sub(lambda m: f'({relations[keys[int(m[1])]]})', text)

    def run_test(self, test: _Test, wrapper: str, output: str, names: list[str]) -> bool:
        """Whether the test fails on ``output``, a query of the model (or a mutant) in the engine's
        dialect. Its columns are renamed to the original's, so a test reads them by name."""
        listed = ', '.join(quote(n) for n in names)
        model = f'SELECT * FROM ({output}) AS kp_model({listed})'
        sql = _TOKEN.sub(
            lambda m: f'({model})', self._rewrite(wrapper, {**self.mapping, self.relation: 'kp_subst_0'})[0]
        )
        _, rows = self.engine.run(sql)
        value = rows[0][0] if rows and rows[0] else 0
        failures = float(value or 0)
        return _holds(test.error_if, failures) or _holds(test.warn_if, failures)

    def count(self, query: str) -> int:
        _, rows = self.engine.run(f'SELECT count(*) FROM ({query}) AS kp_count')
        return int(rows[0][0])

    def fetch(self, query: str, limit: int) -> list[tuple[Any, ...]]:
        return self.engine.run(f'SELECT * FROM ({query}) AS kp_fetch LIMIT {int(limit)}')[1]


def _reason(error: Exception) -> str:
    lines = str(error).strip().splitlines()
    return f'{type(error).__name__}: {lines[0][:160] if lines else ""}'


def mutate(
    graph: Graph,
    root: Path,
    warehouse: str | Path | Engine,
    *,
    seed: int = 1729,
    max_rows: int = 1000,
    max_mutants: int = 30,
    operators: tuple[str, ...] = OPERATORS,
    only: list[str] | None = None,
    suggest: bool = True,
) -> MutationTesting:
    """Mutation test the dbt models of the project at ``root``, read-only, in verify's engines.

    Up to ``max_mutants`` mutants are drawn per model with ``seed``; each runs on up to ``max_rows``
    rows of each input, with the model's dbt tests. ``only`` keeps the models named; ``suggest``
    finds, for each survivor, a test that would kill it.
    """
    started = time.perf_counter()
    engine = engine_for(warehouse)
    result = MutationTesting(engine.label, seed, max_rows, max_mutants, tuple(operators))
    tests = _tests(root)
    shared = _Shared(graph)
    try:
        for candidate in candidates(graph, root):
            node = candidate.node
            if node.kind != 'model':
                continue
            if only and not any(name in (node.name, node.id, graph.label(node)) for name in only):
                continue
            model = ModelMutation(node.id, graph.label(node))
            result.models.append(model)
            timer = time.perf_counter()
            try:
                _mutate_one(
                    engine,
                    shared,
                    candidate,
                    tests.get(node.id, []),
                    model,
                    seed,
                    max_rows,
                    max_mutants,
                    operators,
                    suggest,
                )
            except Exception as error:  # a model that will not run is reported, not fatal
                model.skipped = _reason(error)
            model.seconds = round(time.perf_counter() - timer, 3)
    finally:
        engine.close()
    result.seconds = time.perf_counter() - started
    return result


def _mutate_one(
    engine: Engine,
    shared: _Shared,
    candidate: Candidate,
    tests: list[_Test],
    model: ModelMutation,
    seed: int,
    max_rows: int,
    max_mutants: int,
    operators: tuple[str, ...],
    suggest: bool,
) -> None:
    graph = shared.graph
    # Nothing of the model runs, not even the copies of its inputs, until its SQL is known to be
    # a single query that only reads.
    read_only_query(candidate.sql, engine.read_dialect)
    run = _Run(engine, candidate, tests, max_rows, shared)
    model.tests = [test.check for test, _, _ in run.tests] + run.unit_checks
    original = run.query(candidate.sql)
    names, baseline = engine.run(original)
    _, again = engine.run(original)
    if _bag(baseline) != _bag(again):
        raise RuntimeError('the model is not deterministic, so a mutant cannot be told apart from it')
    model.rows = len(baseline)
    usable: list[tuple[_Test, str]] = []
    for test, wrapper, reads in run.tests:
        if test.sql is None:
            continue
        missing = sorted(reads & set(run.missing))
        if missing:
            test.check.state, test.check.reason = 'not run', f'{missing[0]}: {run.missing[missing[0]]}'
            continue
        try:
            failing = run.run_test(test, wrapper, original, names)
        except Exception as error:
            test.check.state, test.check.reason = 'not run', _reason(error)
            continue
        if failing:
            test.check.state = 'fails on the sample'
            continue
        usable.append((test, wrapper))
    units: list[_Unit] = []
    for unit in run.units:
        try:
            failing = run.unit_fails(unit, candidate.sql, names)
        except Exception as error:
            unit.check.state, unit.check.reason = 'not run', _reason(error)
            continue
        if failing:
            unit.check.state = 'fails on the original'
            continue
        units.append(unit)
    if model.tests and not usable and not units:
        # Tests that cannot run say nothing about the model, which is not the same as 0%.
        model.skipped = _unusable(model.tests)
        return

    generated = mutants(
        candidate.sql, run.dialect, operators=operators, limit=max_mutants, seed=f'{seed}:{candidate.node.id}'
    )
    model.sites = generated.sites
    base_bag = _bag(baseline)
    # A mutant's rows are read when there are not many more than the original's: a mutant that
    # multiplies rows (a dropped join condition) differs in its count, which is enough to judge it.
    cap = 2 * len(baseline) + 100
    outputs: dict[str, list[tuple[Any, ...]]] = {}
    for mutant in generated.mutants:
        outcome = MutantResult(mutant, 'invalid')
        model.mutants.append(outcome)
        try:
            query = run.query(mutant.sql)
            total = run.count(query)
            rows = run.fetch(query, cap) if total <= cap else None
        except Exception as error:  # a mutant that will not run tells nothing about the tests
            outcome.error = _reason(error)
            continue
        outcome.rows = total
        same = rows is not None and total == len(baseline) and _bag(rows) == base_bag
        if not same:  # a data test cannot fail on the same output it passed on
            outcome.changed = _changed(names, baseline, rows, total)
            for test, wrapper in usable:
                try:
                    failing = run.run_test(test, wrapper, query, names)
                except Exception:  # a test that errors on the mutant fails the dbt build too
                    failing = True
                if failing:
                    outcome.killed_by.append(test.check.name)
                    test.check.kills += 1
        # A unit test runs on its own rows, so it can kill a mutant the sample cannot tell apart.
        for unit in units:
            try:
                failing = run.unit_fails(unit, mutant.sql, names)
            except Exception:
                failing = True
            if failing:
                outcome.killed_by.append(unit.check.name)
                unit.check.kills += 1
        if outcome.killed_by:
            outcome.status = 'killed'
            continue
        if same:
            outcome.status = (
                'equivalent' if run.same_query(candidate.sql, mutant.sql) else 'indistinguishable'
            )
            continue
        if run.count(query) != total or (
            rows is not None and total == len(baseline) and _bag(run.fetch(query, cap)) != _bag(rows)
        ):
            outcome.status, outcome.error = 'invalid', 'the mutant does not give the same rows twice'
            continue
        outcome.status = 'survived'
        outcome.reach = sum(shared.reach(candidate.node, column) for column in outcome.changed)
        # Enough of a large output to find a test it breaks: a broken row in part breaks the whole.
        outputs[mutant.id] = rows if rows is not None else run.fetch(query, cap)
    if not suggest:
        return
    # Survivors with the most reach first, so the unit tests, which take the most runs, go to them.
    written: dict[str, str] = {}
    for rank, survivor in enumerate(model.survivors):
        found = _suggest(
            run,
            graph,
            candidate,
            names,
            baseline,
            outputs[survivor.mutant.id],
            survivor.mutant,
            rank < _UNIT_TESTS,
        )
        survivor.suggestions = [_once(s, written) for s in found]


def _once(suggestion: Suggestion, written: dict[str, str]) -> Suggestion:
    """A unit test with the same given rows as one already suggested is that test: its expected
    rows are the original's on those rows, which this survivor changes too."""
    if suggestion.test != 'unit_test':
        return suggestion
    head, _, body = suggestion.yaml.partition('\n')
    first = written.setdefault(body, head.removeprefix('- name: '))
    if first == head.removeprefix('- name: '):
        return suggestion
    return Suggestion(
        'unit_test', None, f'the unit test {first}, suggested for another survivor, kills it too', ''
    )


def _unusable(tests: list[TestCheck]) -> str:
    states = Counter(t.state for t in tests)
    listed = ', '.join(f'{count} {state}' for state, count in sorted(states.items()))
    text = f'none of its {len(tests)} test{"s" if len(tests) != 1 else ""} can tell a mutant apart ({listed})'
    if any('dbt compile' in (t.reason or '') for t in tests):
        text += '; run dbt compile so the manifest holds their compiled SQL'
    return text


def _ref_text(text: str) -> str:
    """A ref or source call, written as dbt does whatever its spacing and quotes."""
    return re.sub(r'\s+', '', text).replace('"', "'")


def _row_list(rows: Any) -> bool:
    return isinstance(rows, list) and all(isinstance(row, dict) for row in rows)


def _unit_kind(values: list[Any]) -> str:
    """How a unit test compares an output column, from the model's values in it."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            return 'boolean'
        if _numeric(value):
            return 'number'
        if isinstance(value, (datetime.date, datetime.datetime)):
            return 'time'
        return 'text'
    return 'text'


def _unit_key(value: Any, kind: str) -> tuple[str, str] | None:
    """A value of a unit test row, the model's or the expected one, as dbt compares them once the
    expected value is cast to the type of the model's column: ``2.50`` is ``2.5``, and the
    string ``'2024-01-02'`` is that date."""
    if value is None:
        return None
    if kind == 'number':
        try:
            number = decimal.Decimal(str(value))
        except decimal.InvalidOperation:
            number = None
        if number is not None and number.is_finite():
            return ('number', str(number.normalize()))
    elif kind == 'boolean':
        text = str(value).lower()
        if text in ('true', 'false'):
            return ('boolean', text)
    elif kind == 'time':
        moment: datetime.datetime | None
        if isinstance(value, datetime.datetime):
            moment = value
        elif isinstance(value, datetime.date):
            moment = datetime.datetime.combine(value, datetime.time())
        else:
            try:
                moment = datetime.datetime.fromisoformat(str(value))
            except ValueError:
                moment = None
        if moment is not None:
            return ('time', moment.replace(tzinfo=None).isoformat(sep=' '))
    return ('text', str(value))


class _Shared:
    """What every model's run reads from the graph, worked out once: the derived ontology's
    relationships and each column's downstream reach."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        self._relationships: list[Relationship] | None = None
        self._reach: dict[tuple[str, str], int] = {}

    def relationships(self) -> list[Relationship]:
        if self._relationships is None:
            self._relationships = derive(self.graph).relationships
        return self._relationships

    def reach(self, node: Node, column: str) -> int:
        key = (node.id, column)
        if key not in self._reach:
            start = node if column == ROW_COUNT else self.graph.column(node.id, column)
            steps = self.graph.impact(start) if start is not None else []
            self._reach[key] = max(0, len({s.node.id for s in steps}) - 1)
        return self._reach[key]


# Suggestions: a test that passes on the original output and fails on the mutant's


def _column_yaml(model: str, column: str, test: str, arguments: list[str] | None = None) -> tuple[str, str]:
    """A column test as one list item, and where it goes. The item alone is given, so pasting it
    into a properties file never repeats the model's or the column's entry."""
    lines = (
        [f'- {test}:', '    arguments:', *(f'      {a}' for a in arguments)] if arguments else [f'- {test}']
    )
    return '\n'.join(lines), f'the data_tests of column {column} of model {model}'


def _scalar(value: Any) -> str:
    """A value as YAML, the way dbt reads it in test arguments and unit test rows."""
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, decimal.Decimal)) or (isinstance(value, float) and math.isfinite(value)):
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, datetime.datetime):
        value = value.isoformat(sep=' ')
    elif isinstance(value, (datetime.date, datetime.time)):
        value = value.isoformat()
    return "'" + str(value).replace("'", "''") + "'"


def _sql_name(name: str) -> str:
    return name if _IDENTIFIER.match(name) else quote(name)


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float, decimal.Decimal)) and not isinstance(value, bool)


def _comparable(a: Any, b: Any) -> bool:
    if _numeric(a) and _numeric(b):
        return True
    for kind in (datetime.datetime, datetime.date):
        if isinstance(a, kind) and isinstance(b, kind):
            return isinstance(a, datetime.datetime) == isinstance(b, datetime.datetime)
    return False


def _suggest(
    run: _Run,
    graph: Graph,
    candidate: Candidate,
    names: list[str],
    baseline: list[tuple[Any, ...]],
    rows: list[tuple[Any, ...]],
    mutant: Mutant,
    unit_test: bool,
) -> list[Suggestion]:
    """Tests that pass on the original output and fail on the mutant's: generic tests where one
    does, else (when ``unit_test``) a unit test."""
    node = candidate.node
    model = str(node.attrs.get('dbt_name') or node.name)
    found: list[Suggestion] = []
    for index, name in enumerate(names):
        before = [row[index] for row in baseline]
        after = [row[index] for row in rows]
        present_before = [v for v in before if v is not None]
        present_after = [v for v in after if v is not None]
        if None not in before and None in after:
            nulls = sum(1 for v in after if v is None)
            found.append(
                Suggestion(
                    'not_null',
                    name,
                    f'{name} is never null on the sample, and null in {nulls} row{"s" if nulls != 1 else ""} '
                    'of the mutant',
                    *_column_yaml(model, name, 'not_null'),
                )
            )
        if _distinct(present_before) and not _distinct(present_after):
            found.append(
                Suggestion(
                    'unique',
                    name,
                    f'{name} is unique on the sample, and repeated in the mutant',
                    *_column_yaml(model, name, 'unique'),
                )
            )
        values = sorted({v for v in present_before if isinstance(v, str)})
        categorical = all(isinstance(v, str) for v in present_before) and len(values) <= _CATEGORIES
        if values and categorical and len(present_before) >= 2 * len(values):
            extra = [v for v in present_after if v not in values]
            if extra:
                listed = ', '.join(_scalar(v) for v in values)
                found.append(
                    Suggestion(
                        'accepted_values',
                        name,
                        f'{name} takes {len(values)} values on the sample, and the mutant adds {_scalar(extra[0])}',
                        *_column_yaml(model, name, 'accepted_values', [f'values: [{listed}]']),
                    )
                )
        found += _relationship(run, graph, node, model, name, present_before, present_after)
        found += _non_negative(model, name, present_before, present_after)
    found += _orderings(model, names, baseline, rows)
    if found or not unit_test:
        return found[:3]
    unit = _unit_test(run, graph, candidate, names, mutant)
    return [unit] if unit is not None else []


def _distinct(values: list[Any]) -> bool:
    canon = [_canon(v) for v in values]
    return len(set(canon)) == len(canon)


def _relationship(
    run: _Run, graph: Graph, node: Node, model: str, name: str, before: list[Any], after: list[Any]
) -> list[Suggestion]:
    """A relationships test to a key this column refers to, when every original value is found
    there and a value of the mutant's is not."""
    column = graph.column(node.id, name)
    if column is None or not after or not before:
        return []
    for key in _parents(run, graph, column):
        parent = graph.parent_of(key)
        target = _dbt_ref(parent) if parent is not None else None
        scratch = run.copy_of(parent.relation) if parent is not None and parent.relation else None
        if parent is None or target is None or scratch is None:
            continue
        try:
            _, rows = run.engine.run(f'SELECT {quote(key.name)} FROM {scratch}')
        except Exception:  # the column is not in the copy under that name
            continue
        values = [r[0] for r in rows if r[0] is not None]
        known = {_canon(v) for v in values}
        if len(known) != len(values) or not all(_canon(v) in known for v in before):
            continue
        orphans = [v for v in after if _canon(v) not in known]
        if orphans:
            return [
                Suggestion(
                    'relationships',
                    name,
                    f'every {name} on the sample is a {key.name} of {parent.name}, and the mutant has '
                    f'{_scalar(orphans[0])}, which is not',
                    *_column_yaml(model, name, 'relationships', [f'to: {target}', f'field: {key.name}']),
                )
            ]
    return []


def _parents(run: _Run, graph: Graph, column: Node) -> list[Node]:
    """Key columns ``column`` may refer to: those the derived ontology relates it, or a column it is
    derived from, to (by a relationships test or a join), and the columns it is derived from."""
    lineage: list[str] = []
    frontier = [column.id]
    for _ in range(4):
        frontier = [
            e.target for f in frontier for e in graph.out_edges(f, 'derived_from') if e.target not in lineage
        ]
        lineage += frontier
    found = [
        graph.nodes[r.columns[1]]
        for r in run.relationships()
        if r.columns and r.columns[0] in {column.id, *lineage} and r.columns[1] in graph.nodes
    ]
    found += [
        graph.nodes[e.target] for e in graph.out_edges(column.id, 'derived_from') if e.target in graph.nodes
    ]
    owner = graph.parent_of(column)
    unique = {n.id: n for n in found}
    return [n for n in unique.values() if graph.parent_of(n) is not owner]


def _dbt_ref(node: Node) -> str | None:
    if node.kind == 'source' and node.id.startswith('source.'):
        parts = node.id.split('.')
        return f"source('{node.attrs.get('source_name') or parts[-2]}', '{parts[-1]}')"
    if node.kind in ('model', 'seed', 'snapshot') and 'dbt' in node.origins:
        return f"ref('{node.attrs.get('dbt_name') or node.name}')"
    return None


def _expression_yaml(model: str, expression: str) -> tuple[str, str]:
    """A model test as one list item, and where it goes."""
    lines = [
        '- dbt_utils.expression_is_true:',
        '    arguments:',
        f'      expression: {json.dumps(expression)}',
    ]
    return '\n'.join(lines), f'the data_tests of model {model}'


def _non_negative(model: str, name: str, before: list[Any], after: list[Any]) -> list[Suggestion]:
    if not before or not all(_numeric(v) and v >= 0 for v in before) or not any(v > 0 for v in before):
        return []
    negative = [v for v in after if _numeric(v) and v < 0]
    if not negative:
        return []
    expression = f'{_sql_name(name)} >= 0'
    return [
        Suggestion(
            'dbt_utils.expression_is_true',
            None,
            f'{name} is never negative on the sample, and the mutant gives {_scalar(negative[0])}',
            *_expression_yaml(model, expression),
        )
    ]


def _orderings(
    model: str, names: list[str], baseline: list[tuple[Any, ...]], rows: list[tuple[Any, ...]]
) -> list[Suggestion]:
    """``a <= b`` (or ``a = b``) between two output columns, when it holds in every original row and
    not in some row of the mutant."""
    found: list[Suggestion] = []
    for i, a in enumerate(names):
        for j, b in enumerate(names):
            if i == j:
                continue
            pairs = [(row[i], row[j]) for row in baseline if row[i] is not None and row[j] is not None]
            if not pairs or not all(_comparable(x, y) and x <= y for x, y in pairs):
                continue
            equal = all(x == y for x, y in pairs)
            if equal and i > j:
                continue
            broken = [
                (row[i], row[j])
                for row in rows
                if row[i] is not None
                and row[j] is not None
                and _comparable(row[i], row[j])
                and not (row[i] == row[j] if equal else row[i] <= row[j])
            ]
            if not broken:
                continue
            sign = '=' if equal else '<='
            expression = f'{_sql_name(a)} {sign} {_sql_name(b)}'
            found.append(
                Suggestion(
                    'dbt_utils.expression_is_true',
                    None,
                    f'{expression} in every row of the sample, and not for {_scalar(broken[0][0])} and '
                    f'{_scalar(broken[0][1])} in the mutant',
                    *_expression_yaml(model, expression),
                )
            )
    return found


# A unit test: the fewest sample rows on which the mutant and the original disagree


def _values_sql(columns: list[tuple[str, str]], rows: list[tuple[Any, ...]]) -> str:
    casts = ', '.join(
        f'CAST(kp_v.c{i} AS {dtype}) AS {quote(name)}' for i, (name, dtype) in enumerate(columns)
    )
    if not rows:
        empty = ', '.join(f'CAST(NULL AS {dtype}) AS {quote(name)}' for name, dtype in columns)
        return f'SELECT {empty} WHERE 1 = 0'
    body = ', '.join('(' + ', '.join(_literal(v) for v in row) + ')' for row in rows)
    listed = ', '.join(f'c{i}' for i in range(len(columns)))
    return f'SELECT {casts} FROM (VALUES {body}) AS kp_v({listed})'


def _literal(value: Any) -> str:
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, (int, decimal.Decimal)) or (isinstance(value, float) and math.isfinite(value)):
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, datetime.datetime):
        return "'" + value.isoformat(sep=' ') + "'"
    if isinstance(value, (datetime.date, datetime.time)):
        return "'" + value.isoformat() + "'"
    return "'" + str(value).replace("'", "''") + "'"


def _unit_test(
    run: _Run, graph: Graph, candidate: Candidate, names: list[str], mutant: Mutant
) -> Suggestion | None:
    """A dbt unit test for the survivor: a subset of the sample, reduced by delta debugging, on
    which the original and the mutant give different rows, with the original's rows expected."""
    refs = {key: _dbt_ref(node) for key in run.inputs if (node := graph.node_for_relation(key)) is not None}
    if len(refs) != len(run.inputs) or any(r is None for r in refs.values()):
        return None
    data: dict[str, list[tuple[Any, ...]]] = {}
    for key in run.inputs:
        _, rows = run.engine.run(f'SELECT * FROM {run.mapping[key]}')
        if any(not isinstance(v, _SCALARS) for row in rows for v in row if v is not None):
            return None
        if any(isinstance(v, float) and not math.isfinite(v) for row in rows for v in row):
            return None
        data[key] = rows
    items = [(key, i) for key in run.inputs for i in range(len(data[key]))]

    def outputs(subset: list[tuple[str, int]]) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]] | None:
        chosen: dict[str, list[tuple[Any, ...]]] = {key: [] for key in run.inputs}
        for key, i in subset:
            chosen[key].append(data[key][i])
        relations = {key: _values_sql(run.columns[key], rows) for key, rows in chosen.items()}
        try:
            before = run.engine.run(run.with_inputs(candidate.sql, relations))[1]
            after = run.engine.run(run.with_inputs(mutant.sql, relations))[1]
        except Exception:  # a subset the SQL cannot run on (a cast that fails, say) is not a witness
            return None
        return before, after

    def differs(subset: list[tuple[str, int]]) -> bool:
        found = outputs(subset)
        return found is not None and _bag(found[0]) != _bag(found[1])

    # Most mutants differ on a few rows: a short prefix of each input is tried before the whole
    # sample, so delta debugging starts from tens of rows rather than every row read.
    prefixes = [[(key, i) for key in run.inputs for i in range(min(n, len(data[key])))] for n in _PREFIXES]
    start = next((p for p in prefixes if len(p) < len(items) and differs(p)), None)
    if start is None and not differs(items):
        return None
    items = _reduce(start or items, differs, budget=_BUDGET)
    if len(items) > _UNIT_ROWS:
        return None
    found = outputs(items)
    if found is None:
        return None
    expected = found[0]
    node = candidate.node
    model = str(node.attrs.get('dbt_name') or node.name)
    lines = [
        f'- name: {model}_{re.sub(r"[^a-z0-9]+", "_", mutant.id.lower())}',
        f'  model: {model}',
        '  given:',
    ]
    for key in run.inputs:
        chosen = [data[key][i] for k, i in items if k == key]
        columns = [name for name, _ in run.columns[key]]
        lines.append(f'    - input: {refs[key]}')
        if not chosen:
            lines.append('      rows: []')
            continue
        lines.append('      rows:')
        lines += [f'        - {_row_yaml(columns, row)}' for row in chosen]
    lines += ['  expect:']
    if expected:
        lines.append('    rows:')
        lines += [f'      - {_row_yaml(names, row)}' for row in expected]
    else:
        lines.append('    rows: []')
    given = len(items)
    return Suggestion(
        'unit_test',
        None,
        f'on {given} input row{"s" if given != 1 else ""} the mutant returns different rows from the original',
        '\n'.join(lines),
        'the unit_tests of a properties file',
    )


def _row_yaml(columns: list[str], row: tuple[Any, ...]) -> str:
    return '{' + ', '.join(f'{c}: {_scalar(v)}' for c, v in zip(columns, row, strict=False)) + '}'


def _reduce(items: list[Any], keep: Any, budget: int) -> list[Any]:
    """Delta debugging by removing chunks: a smaller list for which ``keep`` still holds, within
    ``budget`` calls."""
    granularity, calls = 2, 0
    while len(items) >= 2 and calls < budget:
        size = math.ceil(len(items) / granularity)
        reduced = False
        for start in range(0, len(items), size):
            candidate = items[:start] + items[start + size :]
            calls += 1
            if keep(candidate):
                items, granularity, reduced = candidate, max(granularity - 1, 2), True
                break
            if calls >= budget:
                break
        if not reduced:
            if granularity >= len(items):
                break
            granularity = min(len(items), granularity * 2)
    return items
