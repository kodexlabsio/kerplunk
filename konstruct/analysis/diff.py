"""`konstruct diff`: what a change does to the data stack, column by column, before it is merged.

For every dbt model, SQL file and Airflow DAG file that differs between a base commit and the
working tree (or a head commit), Konstruct analyses both versions and compares each output column's
canonical expression (CTEs, subqueries and aliases inlined down to physical columns, so formatting,
renamed CTEs and reordered columns do not count as changes) and the join, filter and grouping
conditions. It then reports:

* columns added, removed or changed, with the expression before and after;
* changed conditions, which change the rows a model returns;
* breaking changes: a removed column that a downstream model still reads in the new version;
* the impact of each change, from the graph: columns, relations, tests, exposures and tasks;
* DAG changes: tasks and dependencies added or removed, and dbt selections that now run
  different models.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from konstruct.analysis.query import Walk, impact
from konstruct.analysis.verify import defining_sql_file
from konstruct.core.graph import RELATION_KINDS, Graph, Node
from konstruct.core.sql import StatementLineage, analyse, strip_templating
from konstruct.inputs import airflow, dbt
from konstruct.inputs.jinja import Renderer, render_model
from konstruct.semantics.definitions import FILE_NAMES as DEFINITION_FILES


class DiffError(Exception):
    pass


# git


def _git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise DiffError(f'git {" ".join(args)} failed: {result.stderr.strip() or result.stdout.strip()}')
    return result.stdout


_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?$')


def check_ref(ref: str) -> str:
    """``ref``, refused when git would read it as an option rather than a revision.

    A base or head comes from the command line, the Python API and the MCP server's ``diff`` and
    ``gate`` tools, where an agent chooses it. Passed on to git, ``--output=<file>`` would make
    ``git diff`` and ``git show`` write that file. No branch, tag or revision starts with a dash,
    so refusing one loses nothing."""
    if not ref or ref.startswith('-') or '\0' in ref:
        raise DiffError(f'{ref!r} is not a commit, branch, tag or date')
    return ref


def resolve_ref(root: Path, ref: str) -> str:
    """``ref`` as git takes it, or for a date (``2026-10-01``, ``2026-10-01 14:30``) the last
    commit on the current branch's history made before it, so a change can be reviewed since a
    point in time. A date alone means midnight at its start, in the machine's local time zone; a
    branch or tag named like a date is taken as that ref."""
    check_ref(ref)
    if not _DATE.match(ref) or _git(
        root, 'rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}', check=False
    ):
        return ref
    try:
        moment = datetime.fromisoformat(ref)
    except ValueError:
        raise DiffError(f'{ref} is not a valid date') from None
    before = moment.strftime('%Y-%m-%d %H:%M:%S')
    commit = _git(root, 'rev-list', '-1', '--first-parent', f'--before={before}', 'HEAD', check=False).strip()
    if not commit:
        raise DiffError(f'no commit on this branch before {ref}')
    return commit


def default_base(root: Path) -> str:
    """The merge base with origin/main (or main, or master); otherwise HEAD."""
    head = _git(root, 'rev-parse', 'HEAD', check=False).strip()
    for candidate in ('origin/main', 'origin/master', 'main', 'master'):
        if _git(root, 'rev-parse', '--verify', '--quiet', candidate, check=False).strip():
            base = _git(root, 'merge-base', 'HEAD', candidate, check=False).strip()
            # The first main branch found decides: on main itself, compare with HEAD.
            return base if base and base != head else 'HEAD'
    return 'HEAD'


def changed_files(root: Path, base: str, head: str | None) -> dict[str, str]:
    """Paths (relative to the repository) mapped to A, M, D or R, between base and head or the
    working tree including untracked files."""
    args = ['diff', '--name-status', '--no-renames', check_ref(base)] + ([check_ref(head)] if head else [])
    changes: dict[str, str] = {}
    for line in _git(root, *args).splitlines():
        status, _, path = line.partition('\t')
        if path:
            changes[path] = status[:1]
    if head is None:
        for path in _git(root, 'ls-files', '--others', '--exclude-standard').splitlines():
            changes.setdefault(path, 'A')
    return changes


def file_at(repository: Path, ref: str | None, path: str) -> str | None:
    """A file's text at ``ref``, or in the working tree when ``ref`` is None; None if absent."""
    if ref is None:
        target = repository / path
        # A binary file (a database beside the project, say) is not text to compare.
        return target.read_text(encoding='utf-8', errors='replace') if target.is_file() else None
    result = subprocess.run(
        ['git', '-C', str(repository), 'show', f'{check_ref(ref)}:{path}'],
        capture_output=True,
        text=True,
        errors='replace',
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


# Results


@dataclass
class ColumnChange:
    column: str
    """The column name."""
    change: str
    """``added``, ``removed`` or ``changed``."""
    before: str | None = None
    """The canonical expression at the base (CTEs and aliases inlined)."""
    after: str | None = None
    """The canonical expression at the head."""
    impact: dict[str, Any] | None = None
    """What is downstream of the column, as ``Walk.to_dict()``."""
    breaks: list[str] = field(default_factory=list)
    """Relations that still read a removed column, which makes the change breaking."""


@dataclass
class RelationChange:
    path: str
    """The changed file."""
    label: str
    """The relation it builds."""
    kind: str
    """``model``, ``table`` and so on."""
    status: str
    """``added``, ``removed`` or ``modified``."""
    node_id: str | None = None
    """The graph node, when there is one."""
    columns: list[ColumnChange] = field(default_factory=list)
    """Column changes."""
    conditions_added: list[str] = field(default_factory=list)
    """Join, filter and grouping conditions only at the head."""
    conditions_removed: list[str] = field(default_factory=list)
    """Conditions only at the base."""
    rows_impact: dict[str, Any] | None = None
    """What is downstream of a relation whose rows changed."""
    note: str | None = None
    """Why the relation could not be compared, when it could not."""

    @property
    def changed(self) -> bool:
        return bool(
            self.columns
            or self.conditions_added
            or self.conditions_removed
            or self.status in ('added', 'removed')
        )


@dataclass
class DagChange:
    path: str
    """The changed DAG file."""
    dag: str
    """The DAG id."""
    status: str
    """``added``, ``removed`` or ``modified``."""
    tasks_added: list[str] = field(default_factory=list)
    """Task ids only at the head."""
    tasks_removed: list[str] = field(default_factory=list)
    """Task ids only at the base."""
    dependencies_added: list[str] = field(default_factory=list)
    """Dependencies only at the head, as ``a >> b``."""
    dependencies_removed: list[str] = field(default_factory=list)
    """Dependencies only at the base."""
    runs_changed: list[str] = field(default_factory=list)
    """Tasks whose dbt selection changed."""
    sql_changed: list[str] = field(default_factory=list)
    """Tasks whose SQL changed."""


@dataclass
class DefinitionsChange:
    """What changed in a definitions.yml: terms added or removed, and per term which fields."""

    path: str
    """The changed definitions file."""
    added: list[str] = field(default_factory=list)
    """Terms only at the head."""
    removed: list[str] = field(default_factory=list)
    """Terms only at the base."""
    changed: list[str] = field(default_factory=list)
    """One line per changed field, such as ``Order amount: status agreed -> approved``."""
    approved_changed: list[str] = field(default_factory=list)
    """Approved terms whose meaning changed (definition, columns or filters) but stayed approved."""

    @property
    def changed_any(self) -> bool:
        return bool(self.added or self.removed or self.changed)

    def lines(self) -> list[str]:
        return [
            *(f'term added: {t}' for t in self.added),
            *(f'term removed: {t}' for t in self.removed),
            *self.changed,
            *(
                f'{t}: approved, but its meaning changed; check it was re-approved'
                for t in self.approved_changed
            ),
        ]


_TERM_FIELDS = (
    'kind',
    'definition',
    'status',
    'owner',
    'approved',
    'grain',
    'columns',
    'filters',
    'unit',
    'currency',
    'synonyms',
    'related',
    'exceptions',
    'broader',
)
_MEANING = ('definition', 'columns', 'filters', 'grain')


def compare_definitions(path: str, before: str | None, after: str | None) -> DefinitionsChange:
    from konstruct.semantics.definitions import parse

    old = {t.name: t for t in parse(before or '', path).terms}
    new = {t.name: t for t in parse(after or '', path).terms}
    result = DefinitionsChange(path=path)
    result.added = sorted(set(new) - set(old))
    result.removed = sorted(set(old) - set(new))

    def shown(value: Any) -> str:
        if isinstance(value, list):
            return ', '.join(value) or 'none'
        return str(value) if value not in (None, '') else 'none'

    for name in sorted(set(old) & set(new)):
        a, b = old[name], new[name]
        for key in _TERM_FIELDS:
            va, vb = getattr(a, key), getattr(b, key)
            if va == vb:
                continue
            if key == 'definition':
                result.changed.append(f'{name}: definition changed: {shown(va)!r} -> {shown(vb)!r}')
            elif isinstance(va, list):
                plus = [v for v in vb if v not in va]
                minus = [v for v in va if v not in vb]
                parts = [*(f'+{v}' for v in plus), *(f'-{v}' for v in minus)]
                result.changed.append(f'{name}: {key} {" ".join(parts)}')
            else:
                result.changed.append(f'{name}: {key} {shown(va)} -> {shown(vb)}')
        meaning = any(getattr(a, k) != getattr(b, k) for k in _MEANING)
        if meaning and a.status == 'approved' and b.status == 'approved' and a.approved == b.approved:
            result.approved_changed.append(name)
    return result


@dataclass
class YamlChange:
    """What changed in one dbt YAML file: tests, descriptions, sources and exposures."""

    path: str
    """The changed dbt YAML file."""
    tests_added: list[str] = field(default_factory=list)
    """Tests only at the head."""
    tests_removed: list[str] = field(default_factory=list)
    """Tests only at the base."""
    descriptions: list[str] = field(default_factory=list)
    """Descriptions that changed."""
    sources_added: list[str] = field(default_factory=list)
    """Source tables only at the head."""
    sources_removed: list[str] = field(default_factory=list)
    """Source tables only at the base."""
    exposures: list[str] = field(default_factory=list)
    """Exposures added, removed or changed."""

    @property
    def changed(self) -> bool:
        return any(
            (
                self.tests_added,
                self.tests_removed,
                self.descriptions,
                self.sources_added,
                self.sources_removed,
                self.exposures,
            )
        )

    def lines(self) -> list[str]:
        return [
            *(f'test removed: {t}' for t in self.tests_removed),
            *(f'test added: {t}' for t in self.tests_added),
            *(f'source table removed: {s}' for s in self.sources_removed),
            *(f'source table added: {s}' for s in self.sources_added),
            *self.exposures,
            *self.descriptions,
        ]


def _test_names(entries: Any) -> set[str]:
    names = set()
    for entry in entries or []:
        if isinstance(entry, str):
            names.add(entry)
        elif isinstance(entry, dict) and entry:
            key = next(iter(entry))
            value = entry[key]
            label = str(entry.get('test_name') or entry.get('name') or key)
            if isinstance(value, dict) and value.get('name'):
                label = f'{key} ({value["name"]})'
            names.add(label)
    return names


def _yaml_facts(text: str | None) -> dict[str, Any]:
    """Tests, descriptions, source tables and exposures declared in a dbt YAML file."""
    import yaml

    facts: dict[str, Any] = {'tests': set(), 'descriptions': {}, 'sources': set(), 'exposures': {}}
    try:
        data = yaml.safe_load(text or '') or {}
    except (yaml.YAMLError, RecursionError):
        return facts
    if not isinstance(data, dict):
        return facts
    for kind in ('models', 'seeds', 'snapshots'):
        for model in data.get(kind) or []:
            if not isinstance(model, dict) or not model.get('name'):
                continue
            name = str(model['name'])
            facts['descriptions'][name] = (model.get('description') or '').strip()
            for test in _test_names(model.get('data_tests') or model.get('tests')):
                facts['tests'].add(f'{name}: {test}')
            for column in model.get('columns') or []:
                if not isinstance(column, dict) or not column.get('name'):
                    continue
                label = f'{name}.{column["name"]}'
                facts['descriptions'][label] = (column.get('description') or '').strip()
                for test in _test_names(column.get('data_tests') or column.get('tests')):
                    facts['tests'].add(f'{label}: {test}')
    for source in data.get('sources') or []:
        if isinstance(source, dict):
            for table in source.get('tables') or []:
                if isinstance(table, dict) and table.get('name'):
                    facts['sources'].add(f'{source.get("name")}.{table["name"]}')
    for exposure in data.get('exposures') or []:
        if isinstance(exposure, dict) and exposure.get('name'):
            facts['exposures'][str(exposure['name'])] = sorted(map(str, exposure.get('depends_on') or []))
    return facts


def compare_yaml(path: str, before: str | None, after: str | None) -> YamlChange:
    old, new = _yaml_facts(before), _yaml_facts(after)
    change = YamlChange(path)
    change.tests_added = sorted(new['tests'] - old['tests'])
    change.tests_removed = sorted(old['tests'] - new['tests'])
    change.sources_added = sorted(new['sources'] - old['sources'])
    change.sources_removed = sorted(old['sources'] - new['sources'])
    for name in sorted(set(old['descriptions']) | set(new['descriptions'])):
        was, now = old['descriptions'].get(name), new['descriptions'].get(name)
        if was and now and was != now:
            change.descriptions.append(f'description changed: {name}')
        elif now and not was:
            change.descriptions.append(f'description added: {name}')
        elif was and not now:
            change.descriptions.append(f'description removed: {name}')
    for name in sorted(set(old['exposures']) | set(new['exposures'])):
        if name not in old['exposures']:
            change.exposures.append(f'exposure added: {name}')
        elif name not in new['exposures']:
            change.exposures.append(f'exposure removed: {name}')
        elif old['exposures'][name] != new['exposures'][name]:
            change.exposures.append(
                f'exposure {name} now depends on {", ".join(new["exposures"][name])} '
                f'(was {", ".join(old["exposures"][name])})'
            )
    return change


@dataclass
class Selection:
    """The dbt nodes a change actually needs rebuilt and tested, column by column, against what
    `state:modified+` would build."""

    models: list[str] = field(default_factory=list)
    """dbt models whose data the change can affect."""
    tests: list[str] = field(default_factory=list)
    """dbt tests on those models."""
    modified_plus: list[str] = field(default_factory=list)
    """What ``state:modified+`` would select, for comparison."""

    @property
    def selector(self) -> str:
        return ' '.join([*self.models, *self.tests])

    def command(self) -> str:
        return f'dbt build --select {self.selector}' if self.selector else 'nothing to build'

    def summary(self) -> str:
        total = len(self.modified_plus)
        plural = 's' if total != 1 else ''
        if not total:
            return 'No dbt model changed.'
        if not self.models:
            return (
                'Nothing to build: no column or condition changed in a way dbt would see '
                f'(state:modified+ would build {total} model{plural}).'
            )
        tests = len(self.tests)
        return f'Build {len(self.models)} of the {total} model{plural} state:modified+ would, and {tests} test{"s" if tests != 1 else ""}.'

    def to_dict(self) -> dict[str, Any]:
        return {
            'models': self.models,
            'tests': self.tests,
            'selector': self.selector,
            'command': self.command(),
            'state_modified_plus': self.modified_plus,
            'summary': self.summary(),
        }


@dataclass
class Diff:
    base: str
    """The commit compared with."""
    head: str
    """The commit compared, or ``working tree``."""
    files: dict[str, str]
    """Every changed file."""
    base_commit: str | None = None
    """The commit a date given as ``base`` stands for."""
    head_commit: str | None = None
    """The commit a date given as ``head`` stands for."""
    selection: Selection = field(default_factory=Selection)
    """The column-aware dbt selection for the change."""
    relations: list[RelationChange] = field(default_factory=list)
    """Changes to dbt models and SQL."""
    dags: list[DagChange] = field(default_factory=list)
    """Changes to DAGs."""
    other: list[str] = field(default_factory=list)
    """Changed files that were not analysed."""
    yaml: list[YamlChange] = field(default_factory=list)
    """Changes to dbt YAML."""
    definitions: list[DefinitionsChange] = field(default_factory=list)
    """Changes to definitions.yml."""

    @property
    def shown_base(self) -> str:
        """``base``, with the commit a date stands for (``2026-10-01 (abc1234)``)."""
        return f'{self.base} ({self.base_commit[:7]})' if self.base_commit else self.base

    @property
    def shown_head(self) -> str:
        """``head``, with the commit a date stands for."""
        return f'{self.head} ({self.head_commit[:7]})' if self.head_commit else self.head

    @property
    def source_files(self) -> list[str]:
        """Changed files, leaving out generated ones (target/, logs/, konstruct-out/, packages)."""
        return [f for f in self.files if not set(Path(f).parts) & dbt.SKIP_DIRS]

    @property
    def breaking(self) -> list[tuple[str, ColumnChange]]:
        return [(r.label, c) for r in self.relations for c in r.columns if c.breaks]

    def counts(self) -> dict[str, int]:
        columns = [c for r in self.relations for c in r.columns]
        exposures: set[str] = set()
        for change in columns:
            for exposure in (change.impact or {}).get('exposures', []):
                exposures.add(exposure['label'])
        for relation in self.relations:
            for exposure in (relation.rows_impact or {}).get('exposures', []):
                exposures.add(exposure['label'])
        return {
            'files': len(self.source_files),
            'relations': sum(1 for r in self.relations if r.changed),
            'added': sum(1 for c in columns if c.change == 'added'),
            'removed': sum(1 for c in columns if c.change == 'removed'),
            'changed': sum(1 for c in columns if c.change == 'changed'),
            'conditions': sum(1 for r in self.relations if r.conditions_added or r.conditions_removed),
            'breaking': len(self.breaking),
            'dags': len(self.dags),
            'tests_removed': sum(len(y.tests_removed) for y in self.yaml),
            'tests_added': sum(len(y.tests_added) for y in self.yaml),
            'exposures': len(exposures),
            'terms': sum(
                len(d.added) + len(d.removed) + len({line.split(':')[0] for line in d.changed})
                for d in self.definitions
            ),
        }

    def summary(self) -> str:
        c = self.counts()
        if not c['relations'] and not c['dags'] and not self.yaml and not self.definitions:
            return 'No change to models, SQL or DAGs.'
        parts = []
        if c['breaking']:
            parts.append(f'{c["breaking"]} breaking change{"s" if c["breaking"] != 1 else ""}')
        for key, noun in (('changed', 'changed'), ('removed', 'removed'), ('added', 'added')):
            if c[key]:
                parts.append(f'{c[key]} column{"s" if c[key] != 1 else ""} {noun}')
        if c['conditions']:
            parts.append(
                f'row logic changed in {c["conditions"]} relation{"s" if c["conditions"] != 1 else ""}'
            )
        if c['dags']:
            parts.append(f'{c["dags"]} DAG{"s" if c["dags"] != 1 else ""} changed')
        if c['tests_removed']:
            parts.append(f'{c["tests_removed"]} dbt test{"s" if c["tests_removed"] != 1 else ""} removed')
        if c['tests_added']:
            parts.append(f'{c["tests_added"]} dbt test{"s" if c["tests_added"] != 1 else ""} added')
        if c['exposures']:
            parts.append(f'{c["exposures"]} exposure{"s" if c["exposures"] != 1 else ""} affected')
        if c['terms']:
            parts.append(f'{c["terms"]} business term{"s" if c["terms"] != 1 else ""} changed')
        text = ', '.join(parts)
        return text[:1].upper() + text[1:] + '.'

    def to_dict(self) -> dict[str, Any]:
        def relation(r: RelationChange) -> dict[str, Any]:
            return {
                'path': r.path,
                'relation': r.label,
                'kind': r.kind,
                'status': r.status,
                'note': r.note,
                'columns': [c.__dict__ for c in r.columns],
                'conditions_added': r.conditions_added,
                'conditions_removed': r.conditions_removed,
                'rows_impact': r.rows_impact,
            }

        return {
            'base': self.base,
            'head': self.head,
            **({'base_commit': self.base_commit} if self.base_commit else {}),
            **({'head_commit': self.head_commit} if self.head_commit else {}),
            'summary': self.summary(),
            'counts': self.counts(),
            'breaking': [
                {'relation': label, 'column': change.column, 'breaks': change.breaks}
                for label, change in self.breaking
            ],
            'files': self.files,
            'relations': [relation(r) for r in self.relations if r.changed or r.note],
            'dags': [d.__dict__ for d in self.dags],
            'other': self.other,
            'yaml': [y.__dict__ for y in self.yaml if y.changed],
            'definitions': [d.__dict__ for d in self.definitions],
            'dbt': self.selection.to_dict(),
        }

    # Rendering

    def _impact_line(self, data: dict[str, Any] | None) -> str | None:
        if not data or not data.get('total'):
            return None
        text = str(data['summary']).rstrip('.')
        exposures = [e['label'] for e in data.get('exposures', [])]
        if exposures:
            text += f' (exposures: {", ".join(exposures)})'
        return text

    def __str__(self) -> str:
        lines = [
            f'konstruct diff  {self.shown_base}..{self.shown_head}  ({len(self.source_files)} file{"s" if len(self.source_files) != 1 else ""} changed)',
            '',
        ]
        lines.append(self.summary())
        for relation in self.relations:
            if not relation.changed and not relation.note:
                continue
            lines += ['', f'{relation.path}  ({relation.kind} {relation.label}, {relation.status})']
            if relation.note:
                lines.append(f'  {relation.note}')
            for change in relation.columns:
                head = f'  {change.change:<8} {change.column}'
                if change.change == 'changed':
                    head += f'\n             was  {change.before}\n             now  {change.after}'
                lines.append(head)
                for consumer in change.breaks:
                    lines.append(f'           BREAKS {consumer}, which still reads it')
                impact_line = self._impact_line(change.impact)
                if impact_line:
                    lines.append(f'           affects {impact_line}')
            for condition in relation.conditions_added:
                lines.append(f'  rows     + {condition}')
            for condition in relation.conditions_removed:
                lines.append(f'  rows     - {condition}')
            if relation.conditions_added or relation.conditions_removed:
                impact_line = self._impact_line(relation.rows_impact)
                if impact_line:
                    lines.append(f'           affects {impact_line}')
        for dag in self.dags:
            lines += ['', f'{dag.path}  (DAG {dag.dag}, {dag.status})']
            for label, items in (
                ('task +', dag.tasks_added),
                ('task -', dag.tasks_removed),
                ('after +', dag.dependencies_added),
                ('after -', dag.dependencies_removed),
                ('runs', dag.runs_changed),
                ('sql', dag.sql_changed),
            ):
                lines += [f'  {label:<8} {item}' for item in items]
        for yaml_change in self.yaml:
            lines += ['', f'{yaml_change.path}  (dbt YAML)']
            lines += [f'  {line}' for line in yaml_change.lines()]
        for definitions_change in self.definitions:
            lines += ['', f'{definitions_change.path}  (business definitions)']
            lines += [f'  {line}' for line in definitions_change.lines()]
        if self.selection.modified_plus:
            lines += ['', f'dbt: {self.selection.summary()}', f'  {self.selection.command()}']
        if self.other:
            lines += ['', f'Not analysed: {", ".join(self.other)}']
        return '\n'.join(lines)

    def markdown(self) -> str:
        c = self.counts()
        lines = [f'### Konstruct: {self.summary()}', '']
        if self.breaking:
            lines += ['**Breaking changes**', '']
            for label, change in self.breaking:
                lines.append(
                    f'- `{label}.{change.column}` is removed but still read by '
                    + ', '.join(f'`{b}`' for b in change.breaks)
                )
            lines.append('')
        rows = []
        for relation in self.relations:
            for change in relation.columns:
                detail = ''
                if change.change == 'changed':
                    detail = f'`{change.before}` → `{change.after}`'
                impact_line = self._impact_line(change.impact) or ''
                rows.append(
                    f'| `{relation.label}.{change.column}` | {change.change} | {detail} | {impact_line} |'
                )
            for condition in relation.conditions_added:
                rows.append(
                    f'| `{relation.label}` rows | condition added | `{condition}` | {self._impact_line(relation.rows_impact) or ""} |'
                )
            for condition in relation.conditions_removed:
                rows.append(
                    f'| `{relation.label}` rows | condition removed | `{condition}` | {self._impact_line(relation.rows_impact) or ""} |'
                )
        if rows:
            lines += [
                '| Column | Change | Detail | Downstream |',
                '|--------|--------|--------|------------|',
                *rows,
                '',
            ]
        for dag in self.dags:
            items = [
                *(f'task added: `{t}`' for t in dag.tasks_added),
                *(f'task removed: `{t}`' for t in dag.tasks_removed),
                *(f'dependency added: `{d}`' for d in dag.dependencies_added),
                *(f'dependency removed: `{d}`' for d in dag.dependencies_removed),
                *dag.runs_changed,
                *dag.sql_changed,
            ]
            if items:
                lines += [f'**DAG `{dag.dag}`** ({dag.path})', '', *(f'- {i}' for i in items), '']
        for yaml_change in self.yaml:
            lines += [f'**{yaml_change.path}**', '', *(f'- {line}' for line in yaml_change.lines()), '']
        for definitions_change in self.definitions:
            lines += [
                f'**Business definitions** ({definitions_change.path})',
                '',
                *(f'- {line}' for line in definitions_change.lines()),
                '',
            ]
        if self.selection.modified_plus:
            lines += [f'**dbt:** {self.selection.summary()}', '', f'```\n{self.selection.command()}\n```', '']
        lines.append(
            f'<sub>{c["files"]} file{"s" if c["files"] != 1 else ""} compared between `{self.shown_base}` and `{self.shown_head}` by Konstruct. '
            'Expressions are compared with CTEs and aliases inlined, so formatting and renames do not count.</sub>'
        )
        return '\n'.join(lines) + '\n'


# Analysis


class _Context:
    def __init__(self, graph: Graph, root: Path) -> None:
        self.graph = graph
        self.root = root
        self.dialect = graph.meta.get('dialect')
        self.schema = self._schema()
        self.manifests: dict[str, dict[str, Any]] = {}
        self.items: dict[str, dict[str, Any]] = {}
        self.renderers: dict[str, Renderer] = {}
        for project in dbt.find_projects(root):
            try:
                manifest = json.loads(project.manifest_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            macro_paths = [p for p in project.source_paths if p.name == 'macros' or 'macro' in p.name]
            renderer = Renderer(project.root, manifest, macro_paths=macro_paths or None)
            for node_id, item in manifest.get('nodes', {}).items():
                self.manifests[node_id] = manifest
                self.items[node_id] = item
                self.renderers[node_id] = renderer

    def _schema(self) -> dict[str, dict[str, str]]:
        schema: dict[str, dict[str, str]] = {}
        for node in self.graph.relations():
            if node.relation:
                columns = {
                    c.name: c.data_type or 'UNKNOWN'
                    for c in self.graph.columns_of(node.id)
                    if not c.attrs.get('nested') and not c.attrs.get('documented_only')
                }
                if columns:
                    schema[node.relation] = columns
        return schema

    def model_sql(self, node: Node, text: str) -> tuple[str, str | None, str | None]:
        item = self.items.get(node.id, {})
        manifest = self.manifests.get(node.id, {'nodes': {}, 'sources': {}})
        sql = render_model(text, self.renderers.get(node.id), lambda raw: dbt.render_refs(raw, manifest))
        return sql, item.get('database'), item.get('schema')

    def lineage(
        self,
        sql: str,
        catalog: str | None,
        schema: str | None,
        target: str | None,
        *,
        columns: dict[str, dict[str, str]] | None = None,
    ) -> list[StatementLineage]:
        return analyse(
            sql,
            dialect=self.dialect,
            schema=self.schema if columns is None else columns,
            default_catalog=catalog or self.graph.meta.get('default_catalog'),
            default_db=schema or self.graph.meta.get('default_schema'),
            target=target,
        )


def diff(graph: Graph, root: Path, *, base: str | None = None, head: str | None = None) -> Diff:
    """Compare ``base`` (default: the merge base with main) with ``head`` (default: the working tree)."""
    repository = Path(_git(root, 'rev-parse', '--show-toplevel').strip())
    given_base, given_head = base, head
    base = resolve_ref(root, base) if base else default_base(root)
    head = resolve_ref(root, head) if head else None
    files = changed_files(root, base, head)
    result = Diff(
        base=given_base or base,
        head=given_head or 'working tree',
        files=files,
        base_commit=base if given_base and base != given_base else None,
        head_commit=head if given_head and head != given_head else None,
    )
    context = _Context(graph, root)
    prefix = root.resolve().relative_to(repository.resolve())
    by_path: dict[str, list[Node]] = {}
    for node in graph.nodes.values():
        if node.path:
            by_path.setdefault(node.path, []).append(node)
        sql_path = node.attrs.get('sql_path')
        if sql_path:
            by_path.setdefault(str(sql_path), []).append(node)
    for path in sorted(files):
        relative = Path(path)
        if set(relative.parts) & dbt.SKIP_DIRS:
            continue
        try:
            in_project = str(relative.relative_to(prefix)) if str(prefix) != '.' else path
        except ValueError:
            continue
        before = file_at(repository, base, path)
        after = file_at(repository, head, path)
        nodes = by_path.get(in_project, [])
        models = [n for n in nodes if n.kind in ('model', 'snapshot') and 'dbt' in n.origins]
        if models:
            for model in models:
                result.relations.append(_compare_model(context, model, in_project, before, after))
        elif in_project.endswith('.sql') and (nodes or before or after):
            result.relations.extend(_compare_sql_file(context, in_project, before, after))
        elif Path(in_project).name in DEFINITION_FILES and (before or after):
            definitions_change = compare_definitions(in_project, before, after)
            if definitions_change.changed_any or definitions_change.approved_changed:
                result.definitions.append(definitions_change)
        elif in_project.endswith(('.yml', '.yaml')) and (before or after):
            yaml_change = compare_yaml(in_project, before, after)
            if yaml_change.changed:
                result.yaml.append(yaml_change)
        elif in_project.endswith('.py') and (
            'DAG' in (before or '') + (after or '') or '@dag' in (before or '') + (after or '')
        ):
            result.dags.extend(_compare_dag_file(context, repository / path, in_project, before, after))
        else:
            result.other.append(in_project)
    touched = [n for p in files for n in by_path.get(_in_project(p, prefix), [])]
    result.selection = select_for(graph, result, touched)
    return result


def _in_project(path: str, prefix: Path) -> str:
    if str(prefix) == '.':
        return path
    try:
        return str(Path(path).relative_to(prefix))
    except ValueError:
        return ''


def select_for(graph: Graph, result: Diff, touched_nodes: list[Node]) -> Selection:
    """Column-aware dbt selection: the changed models, the models whose columns, joins or filters
    are downstream of a changed column (or of changed row logic), and the tests on any of them."""

    def is_dbt(node: Node | None) -> bool:
        return node is not None and 'dbt' in node.origins and node.kind in ('model', 'snapshot', 'seed')

    def name(node: Node) -> str:
        return str(node.attrs.get('dbt_name') or node.name)

    modified = {n.id for n in touched_nodes if is_dbt(n)}
    downstream = dbt.downstream_of(graph, modified)
    everything = [graph.nodes[i] for i in modified | downstream if i in graph.nodes]
    selection = Selection(modified_plus=sorted(name(n) for n in everything if is_dbt(n)))
    models: set[str] = set()
    tests: set[str] = set()

    def take(steps: list[Any]) -> None:
        for step in steps:
            node = step.node
            if node.kind == 'test':
                tests.add(node.id)
            elif node.kind == 'column':
                parent = graph.parent_of(node)
                if parent is not None and is_dbt(parent):
                    models.add(parent.id)
                tests.update(e.source for e in graph.in_edges(node.id, 'tests'))
            elif is_dbt(node):
                models.add(node.id)

    for relation in result.relations:
        node = graph.nodes.get(relation.node_id or '')
        if node is None or not is_dbt(node) or relation.status == 'removed':
            continue
        rows_changed = bool(relation.conditions_added or relation.conditions_removed)
        if rows_changed:
            models.add(node.id)
            take(graph.impact(node))
        for change in relation.columns:
            models.add(node.id)
            column = graph.column(node.id, change.column)
            if column is not None and change.change != 'added':
                take(graph.impact(column))
        if relation.columns or rows_changed:
            tests.update(e.source for e in graph.in_edges(node.id, 'tests'))
    selection.models = sorted(name(graph.nodes[i]) for i in models)
    selection.tests = sorted(graph.nodes[i].name for i in tests if i in graph.nodes)
    return selection


def _status(before: str | None, after: str | None) -> str:
    if before is None:
        return 'added'
    if after is None:
        return 'removed'
    return 'modified'


def _compare_model(
    context: _Context, node: Node, path: str, before: str | None, after: str | None
) -> RelationChange:
    change = RelationChange(
        path=path,
        label=context.graph.label(node),
        kind=node.kind,
        status=_status(before, after),
        node_id=node.id,
    )
    statements = {}
    for side, text in (('before', before), ('after', after)):
        if text is None:
            continue
        sql, catalog, schema = context.model_sql(node, text)
        selects = [
            s
            for s in context.lineage(sql, catalog, schema, node.relation)
            if s.kind in ('select', 'unparsed')
        ]
        if selects and not selects[-1].errors:
            statements[side] = selects[-1]
        elif selects:
            change.note = f'could not compare the {side} version: {selects[-1].errors[0]}'
    _compare_statements(
        context, change, node, statements.get('before'), statements.get('after'), before, after
    )
    return change


def _compare_sql_file(
    context: _Context, path: str, before: str | None, after: str | None
) -> list[RelationChange]:
    sides: dict[str, dict[str, StatementLineage]] = {'before': {}, 'after': {}}
    for side, text in (('before', before), ('after', after)):
        if text is None:
            continue
        for statement in context.lineage(strip_templating(text), None, None, None):
            if statement.target and statement.columns:
                sides[side][statement.target] = statement
    changes = []
    for target in sorted(set(sides['before']) | set(sides['after'])):
        node = context.graph.node_for_relation(target)
        label = context.graph.label(node) if node else target.rsplit('.', 1)[-1]
        old, new = sides['before'].get(target), sides['after'].get(target)
        change = RelationChange(
            path=path,
            label=label,
            kind=node.kind if node else 'table',
            status='added' if old is None else 'removed' if new is None else 'modified',
            node_id=node.id if node else None,
        )
        _compare_statements(context, change, node, old, new, before if old else None, after if new else None)
        changes.append(change)
    return changes


def _compare_statements(
    context: _Context,
    change: RelationChange,
    node: Node | None,
    old: StatementLineage | None,
    new: StatementLineage | None,
    before: str | None,
    after: str | None,
) -> None:
    graph = context.graph
    old_columns = old.expressions if old else {}
    new_columns = new.expressions if new else {}
    for column in sorted(
        set(old_columns) | set(new_columns),
        key=lambda c: (list(new_columns).index(c) if c in new_columns else 10**6, c),
    ):
        if column not in old_columns:
            change.columns.append(ColumnChange(column, 'added', after=new_columns[column]))
        elif column not in new_columns:
            entry = ColumnChange(column, 'removed', before=old_columns[column])
            if node is not None:
                entry.breaks = _still_read(context, node, column)
                entry.impact = _walk(graph, node, column)
            change.columns.append(entry)
        elif old_columns[column] != new_columns[column]:
            entry = ColumnChange(column, 'changed', before=old_columns[column], after=new_columns[column])
            if node is not None:
                entry.impact = _walk(graph, node, column)
            change.columns.append(entry)
    if old is not None and new is not None:
        change.conditions_added = sorted(set(new.conditions) - set(old.conditions))
        change.conditions_removed = sorted(set(old.conditions) - set(new.conditions))
        if (change.conditions_added or change.conditions_removed) and node is not None:
            change.rows_impact = impact(graph, node.id).to_dict()
    if change.status == 'removed' and node is not None:
        change.rows_impact = impact(graph, node.id).to_dict()


def _walk(graph: Graph, node: Node, column: str) -> dict[str, Any] | None:
    target = graph.column(node.id, column)
    if target is None:
        return None
    walk: Walk = impact(graph, target.id)
    return walk.to_dict()


def _still_read(context: _Context, node: Node, column: str) -> list[str]:
    """Downstream models and SQL-built tables whose current SQL still reads ``node.column``."""
    graph = context.graph
    readers = []
    columns = context.schema
    if node.relation and column not in context.schema.get(node.relation, {}):
        # Only the new SQL defines a relation the warehouse does not have, so its schema no longer
        # lists the column; put it back, or a consumer reading it through ``select *`` resolves it
        # to nothing.
        columns = {**columns, node.relation: {**columns.get(node.relation, {}), column: 'UNKNOWN'}}
    consumers = {e.target for e in graph.out_edges(node.id, 'feeds')}
    target = graph.column(node.id, column)
    if target is not None:
        consumers |= {e.source.split('#')[0] for e in graph.in_edges(target.id, 'derived_from')}
        consumers |= {e.target for e in graph.out_edges(target.id, 'feeds')}
    for consumer_id in sorted(consumers):
        consumer = graph.nodes.get(consumer_id)
        if consumer is None or consumer.kind not in RELATION_KINDS:
            continue
        current = (
            (context.root / consumer.path)
            if consumer.path
            else defining_sql_file(graph, consumer, context.root)
        )
        if current is None or not current.is_file():
            continue
        text = current.read_text(encoding='utf-8')
        if 'dbt' in consumer.origins:
            sql, catalog, schema = context.model_sql(consumer, text)
            statements = context.lineage(sql, catalog, schema, consumer.relation, columns=columns)
        else:
            statements = [
                s
                for s in context.lineage(strip_templating(text), None, None, None, columns=columns)
                if s.target == consumer.relation
            ]
        for statement in statements:
            uses = {
                (s.relation, s.column.split('.')[0])
                for sources in statement.columns.values()
                for s in sources
            }
            uses |= {(relation, name.split('.')[0]) for relation, name in statement.references}
            if (node.relation, column) in uses:
                readers.append(graph.label(consumer))
                break
    return readers


def _compare_dag_file(
    context: _Context, path: Path, rel: str, before: str | None, after: str | None
) -> list[DagChange]:
    def parse(text: str | None) -> dict[str, airflow.DagInfo]:
        if text is None:
            return {}
        try:
            return {d.dag_id: d for d in airflow.parse_source(path, text)}
        except SyntaxError:
            return {}

    old, new = parse(before), parse(after)
    changes = []
    for dag_id in sorted(set(old) | set(new)):
        a, b = old.get(dag_id), new.get(dag_id)
        change = DagChange(
            path=rel, dag=dag_id, status='added' if a is None else 'removed' if b is None else 'modified'
        )
        old_tasks = a.tasks if a else {}
        new_tasks = b.tasks if b else {}
        change.tasks_added = sorted(set(new_tasks) - set(old_tasks))
        change.tasks_removed = sorted(set(old_tasks) - set(new_tasks))
        old_deps = {f'{u} >> {d}' for u, d in (a.dependencies if a else [])}
        new_deps = {f'{u} >> {d}' for u, d in (b.dependencies if b else [])}
        change.dependencies_added = sorted(new_deps - old_deps)
        change.dependencies_removed = sorted(old_deps - new_deps)
        for task_id in sorted(set(old_tasks) & set(new_tasks)):
            ran = _runs(context.graph, old_tasks[task_id])
            runs = _runs(context.graph, new_tasks[task_id])
            if ran != runs:
                added, removed = sorted(runs - ran), sorted(ran - runs)
                detail = '; '.join(
                    part
                    for part in (
                        f'now also runs {", ".join(added)}' if added else '',
                        f'no longer runs {", ".join(removed)}' if removed else '',
                    )
                    if part
                )
                change.runs_changed.append(f'task `{task_id}`: {detail}')
            if (old_tasks[task_id].sql or '').strip() != (new_tasks[task_id].sql or '').strip():
                change.sql_changed.append(f'task `{task_id}`: SQL changed')
        if (
            any(
                (
                    change.tasks_added,
                    change.tasks_removed,
                    change.dependencies_added,
                    change.dependencies_removed,
                    change.runs_changed,
                    change.sql_changed,
                )
            )
            or change.status != 'modified'
        ):
            changes.append(change)
    return changes


def _runs(graph: Graph, task: airflow.TaskInfo) -> set[str]:
    selected: set[str] = set()
    for invocation in task.dbt:
        nodes = dbt.select_nodes(graph, invocation.selectors, invocation.excludes)
        kinds = {'seed': {'seed'}, 'snapshot': {'snapshot'}, 'run': {'model'}}.get(invocation.command)
        selected |= {graph.label(n) for n in nodes if kinds is None or n.kind in kinds}
    return selected
