"""`konstruct gate`: the check a change must pass before it is proposed.

A coding agent (or a person) runs it before opening a pull request, and the GitHub Action runs
the same check with ``gate: true``. It compares the working tree with the base as
``konstruct diff`` does, and fails when the change:

* **breaks something downstream:** it removes a column that a model, task or dashboard still
  reads;
* **changes what an approved term means:** a term in ``definitions.yml`` whose status is
  approved gets new wording, columns, filters or grain, or a column it names (or one upstream of
  it) is computed differently or loses or gains a row condition, so the term must be approved
  again by its owner;
* **adds a conflicting definition:** a column the change adds or changes, or that changes because
  of it, computes the same business idea as another column in a different way (another filter,
  source, join or aggregation), as ``konstruct conflicts`` finds, unless the two are bound to
  different business terms in ``definitions.yml``;
* **breaks a data law:** a law that ``konstruct verify --laws`` found to hold on every day no
  longer follows from a model's SQL (a join on a key no test covers, a new filter, another factor,
  a SUM become an AVG), as :func:`konstruct.analysis.laws.violations` finds;
* **changes what a consumer pinned:** a column a repository listed in ``repositories`` pins in its
  ``konstruct.lock`` is altered by the change, as ``konstruct lock --check`` would find there.
* **moves sensitive data where it is not marked to go:** a column or an exposure the change
  alters receives sensitive data and nothing marks its model or exposure to hold it, a table
  the change alters copies a masked column in clear, or the change takes a sensitivity mark away
  (see :mod:`konstruct.analysis.sensitivity`).

Each finding says what to do about it, so an agent can fix the change and run the gate again.

Each rule is a check in the plugin registry (:mod:`konstruct.plugins.registry`), as a plugin's
check is, and :func:`gate` runs every check the registry has.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from konstruct.analysis import sensitivity
from konstruct.analysis.conflicts import Conflicts, find_conflicts
from konstruct.analysis.diff import Diff, RelationChange
from konstruct.core.graph import Graph, column_id
from konstruct.plugins.api import API_VERSION, CheckContext, Finding, finding_problem, one_line
from konstruct.plugins.registry import describe

if TYPE_CHECKING:
    from konstruct.analysis.laws import Laws
    from konstruct.interfaces.layout import Style
    from konstruct.plugins.registry import Plugin, Plugins

__all__ = ['RULES', 'Finding', 'Gate', 'gate']


@dataclass
class Gate:
    """The result of the gate on one change."""

    base: str
    """The commit compared with, as given."""
    findings: list[Finding] = field(default_factory=list)
    """Everything that stops the change."""
    changed: int = 0
    """Files the change touches."""
    laws_from: str | None = None
    """Where the data laws the change is judged against came from (``konstruct-out/laws.json at
    main``), or that there were none, so no data law was checked."""

    @property
    def passed(self) -> bool:
        """Whether nothing stops the change."""
        return not self.findings

    @property
    def verdict(self) -> str:
        """``Gate passed`` or ``Gate failed``."""
        return 'Gate passed' if self.passed else 'Gate failed'

    def detail(self) -> str:
        """What the verdict rests on: the files changed, or how many findings there are."""
        if self.passed:
            return f'{self.changed} changed file{"s" if self.changed != 1 else ""}, nothing breaks.'
        count = len(self.findings)
        return f'{count} finding{"s" if count != 1 else ""} to fix before proposing the change.'

    def summary(self) -> str:
        """One line: passed, or how many findings there are."""
        return f'{self.verdict}: {self.detail()}'

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import gate_lines, show

        return show(gate_lines, self, style)

    def __str__(self) -> str:
        return self.text()

    def markdown(self) -> str:
        """The result as markdown, for a pull request comment or a job summary."""
        lines = [f'**{self.summary()}**']
        if self.findings:
            lines += ['', '| Rule | Finding | Fix |', '|---|---|---|']
            for finding in self.findings:
                where = f' (`{_cell(finding.path)}`)' if finding.path else ''
                lines.append(f'| `{finding.rule}` | {_cell(finding.message)}{where} | {_cell(finding.fix)} |')
        if self.laws_from:
            lines += ['', f'Data laws: {self.laws_from}.']
        return '\n'.join(lines) + '\n'

    def to_dict(self) -> dict[str, Any]:
        """The result as JSON-ready data, each finding with its fix."""
        return {
            'passed': self.passed,
            'base': self.base,
            'summary': self.summary(),
            'findings': [{**asdict(f), 'fix': f.fix} for f in self.findings],
            'laws_from': self.laws_from,
        }


def _cell(text: str) -> str:
    """``text`` in a markdown table cell, where a ``|`` would end the cell."""
    return text.replace('|', '\\|')


def _changed_columns(graph: Graph, diff: Diff) -> dict[str, RelationChange]:
    """Every column the change alters, by graph id, with the change that alters it: added and
    changed columns, every column of a relation whose row conditions changed, and the columns
    downstream of either."""
    changed: dict[str, RelationChange] = {}
    for relation in diff.relations:
        if not relation.node_id:
            continue
        for column in relation.columns:
            if column.change in ('added', 'changed'):
                changed.setdefault(column_id(relation.node_id, column.column), relation)
                for item in (column.impact or {}).get('columns', []):
                    changed.setdefault(str(item['id']), relation)
        if relation.conditions_added or relation.conditions_removed:
            for node in graph.columns_of(relation.node_id):
                changed.setdefault(node.id, relation)
            for item in (relation.rows_impact or {}).get('columns', []):
                changed.setdefault(str(item['id']), relation)
    return changed


def _name(graph: Graph, node_id: str, fallback: str) -> str:
    """A column's short label, or its full name when another relation shares the short one."""
    node = graph.nodes.get(node_id)
    parent = graph.parent_of(node) if node is not None else None
    if node is None or parent is None:
        return fallback
    if parent.relation and len([n for n in graph.resolve(graph.label(parent)) if n.kind != 'column']) > 1:
        return f'{parent.relation}.{node.name}'
    return graph.label(node)


def gate(
    graph: Graph,
    diff: Diff,
    conflicts: Conflicts | None = None,
    *,
    plugins: Plugins | None = None,
    laws: Laws | None = None,
    laws_from: str | None = None,
) -> Gate:
    """The gate for ``diff``, given the graph at the head: every check that ``plugins`` (default:
    Konstruct's own checks) runs. A plugin's check that raises, returns something other than
    findings or cannot be loaded fails the gate with a finding that says so.

    ``conflicts`` are the conflicts at the head, as :func:`~konstruct.analysis.conflicts.find_conflicts`
    finds them. By default the gate finds them itself: every conflict when a plugin's check runs,
    which sees them all, and otherwise only those of the columns the change alters, which are all
    that Konstruct's own checks report, at a cost that grows with the change rather than with the
    square of the columns that share a name. ``laws`` are the data laws as confirmed before the
    change, from ``laws.json``, and ``laws_from`` says where they came from."""
    from konstruct.plugins.registry import Plugins

    registry = plugins or Plugins.builtin()
    checks = registry.group('checks')
    if conflicts is None:
        touching = None if any(not p.builtin for p in checks) else set(_changed_columns(graph, diff))
        conflicts = find_conflicts(graph, touching=touching)
    result = Gate(base=diff.shown_base, changed=len(diff.source_files), laws_from=laws_from)
    context = CheckContext(graph=graph, diff=diff, conflicts=conflicts, laws=laws)
    for plugin in checks:
        result.findings += _run(plugin, context)
    for plugin in registry.failed('checks'):
        result.findings.append(_failure(plugin, f'cannot run: {plugin.error}'))
    return result


def plugins_at(root: Path, base: str | None) -> list[str]:
    """The plugins the project's configuration enables at ``base`` (default: the merge base with
    main, as for :func:`konstruct.analysis.diff.diff`); empty when it enables none there or the
    file cannot be read. The gate runs these checks as well as the head's, so a change cannot
    turn off a check that would judge it."""
    from konstruct.analysis.diff import default_base, file_at, resolve_ref
    from konstruct.engine.config import ConfigError, enabled_plugins

    top = subprocess.run(
        ['git', '-C', str(root), 'rev-parse', '--show-toplevel'], capture_output=True, text=True, check=False
    )
    if top.returncode != 0:
        return []
    repository = Path(top.stdout.strip())
    ref = resolve_ref(root, base) if base else default_base(root)
    prefix = root.resolve().relative_to(repository.resolve())
    for name, pyproject in (('konstruct.toml', False), ('pyproject.toml', True)):
        text = file_at(repository, ref, (prefix / name).as_posix())
        if text is None:
            continue
        try:
            found = enabled_plugins(text, pyproject=pyproject)
        except ConfigError:
            return []
        # As load_config does: konstruct.toml wins, then [tool.konstruct] in pyproject.toml.
        if found or not pyproject:
            return found
    return []


def _run(plugin: Plugin, context: CheckContext) -> list[Finding]:
    if plugin.builtin:
        found: list[Finding] = plugin.loaded.check(context)
        return found
    try:
        found = plugin.loaded.check(context)
    except Exception as error:
        return [_failure(plugin, f'raised {describe(error)}')]
    if not isinstance(found, list) or any(finding_problem(f) is not None for f in found):
        return [_failure(plugin, 'returned something other than findings, each with a rule, message and fix')]
    # One line each, as the text, the markdown table and the JSON show them.
    return [
        Finding(
            f'{plugin.name}.{one_line(f.rule)}',
            one_line(f.message),
            one_line(f.path) if f.path is not None else None,
            one_line(f.fix),
        )
        for f in found
    ]


def _failure(plugin: Plugin, what: str) -> Finding:
    return Finding(
        f'{plugin.name}.failed',
        f'the gate check from {plugin.distribution} {plugin.version} {what}',
        fix='Fix or upgrade the plugin.',
    )


@dataclass(frozen=True)
class BuiltinCheck:
    """One of Konstruct's own gate rules, as the registry loads it."""

    description: str
    """What the rule stops."""
    fix: str
    """What to do about a finding of the rule."""
    run: Callable[[CheckContext], list[Finding]]
    api_version: tuple[int, int] = API_VERSION

    def check(self, context: CheckContext) -> list[Finding]:
        """The findings of this rule, each with the rule's fix."""
        return [replace(f, fix=f.fix or self.fix) for f in self.run(context)]


def _breaking(context: CheckContext) -> list[Finding]:
    found: list[Finding] = []
    for relation in context.diff.relations:
        for column in relation.columns:
            if column.breaks:
                found.append(
                    Finding(
                        'breaking',
                        f'{relation.label}.{column.column} is removed but still read by {", ".join(column.breaks)}',
                        relation.path,
                    )
                )
    return found


def _approved_definition_changed(context: CheckContext) -> list[Finding]:
    graph, diff = context.graph, context.diff
    found: list[Finding] = []
    for definitions in diff.definitions:
        for name in definitions.approved_changed:
            found.append(
                Finding(
                    'approved-definition-changed',
                    f'{name!r} is approved, but the change gives it a new meaning',
                    definitions.path,
                )
            )
    changed = _changed_columns(graph, diff)
    removed = {
        column_id(r.node_id, c.column): r
        for r in diff.relations
        if r.node_id
        for c in r.columns
        if c.change == 'removed'
    }
    # A removed column is gone from the graph, and so is the term's edge to it: match the names.
    # Each removed column by its full name, and by its short label too where no other relation
    # has that label.
    removed_names: list[tuple[str, bool, RelationChange]] = []
    for r in diff.relations:
        node = graph.nodes.get(r.node_id) if r.node_id else None
        for c in r.columns:
            if c.change == 'removed':
                if node is not None and node.relation:
                    removed_names.append((f'{node.relation}.{c.column}'.lower(), True, r))
                if len([n for n in graph.resolve(r.label) if n.kind != 'column']) <= 1:
                    removed_names.append((f'{r.label}.{c.column}'.lower(), False, r))
    named = {t for d in diff.definitions for t in d.approved_changed}
    for term in sorted((n for n in graph.nodes.values() if n.kind == 'term'), key=lambda n: n.name.lower()):
        if term.attrs.get('status') != 'approved' or term.name in named:
            continue
        hits = [
            (changed.get(e.target) or removed.get(e.target), e.target)
            for e in graph.out_edges(term.id, 'defines')
        ]
        for ref in term.attrs.get('columns', []) or []:
            text = str(ref).lower()
            hits += [
                (r, str(ref))
                for name, qualified, r in removed_names
                if text == name or (qualified and (name.endswith('.' + text) or text.endswith('.' + name)))
            ]
        for altering, target in hits:
            if altering is not None:
                bound = _name(graph, target, target)
                found.append(
                    Finding(
                        'approved-definition-changed',
                        f'{term.name!r} is approved, and the change alters {bound}, which it names',
                        altering.path,
                    )
                )
                break
    return found


def _conflicting_definition(context: CheckContext) -> list[Finding]:
    graph, conflicts = context.graph, context.conflicts
    changed = _changed_columns(graph, context.diff)
    found: list[Finding] = []

    def terms_of(column: str) -> set[str]:
        return {e.source for e in graph.in_edges(column, 'defines') if e.attrs.get('role') != 'input'}

    seen: set[tuple[str, str]] = set()
    for conflict in conflicts.conflicts:
        mine_terms, their_terms = terms_of(conflict.a.column), terms_of(conflict.b.column)
        if mine_terms and their_terms and not mine_terms & their_terms:
            # Each side is bound to a business term of its own: two definitions, on purpose.
            continue
        for side, other in ((conflict.a, conflict.b), (conflict.b, conflict.a)):
            source = changed.get(side.column)
            if source is None or (side.column, other.column) in seen:
                continue
            seen.add((side.column, other.column))
            differences = '; '.join(f'{d.kind} {d.detail}' for d in conflict.differences[:2])
            mine, theirs = _name(graph, side.column, side.label), _name(graph, other.column, other.label)
            found.append(
                Finding(
                    'conflicting-definition',
                    f'{mine} computes {conflict.idea!r} differently from {theirs} ({differences})',
                    source.path,
                )
            )
    return found


def _data_law_broken(context: CheckContext) -> list[Finding]:
    from konstruct.analysis.diff import removed_tests, rendered_now
    from konstruct.analysis.laws import propose, violations

    laws, graph, changed = context.laws, context.graph, context.diff.paths
    if laws is None or not changed or not any(law.always_held for law in laws.laws):
        return []
    root = Path(str(graph.meta.get('root', '.')))
    # Only the models the laws run through, each as its file reads now: without dbt, the
    # manifest's compiled SQL is from before the edit.
    models = laws.models()
    head = propose(
        graph,
        root,
        models=models,
        sql=rendered_now(graph, root, models),
        without_tests=removed_tests(graph, context.diff),
    )
    return [
        Finding('data-law-broken', f'the change breaks a law that held on all the data: {v}', v.path)
        for v in violations(graph, laws, head, changed=changed)
    ]


def _consumer_pin_changed(context: CheckContext) -> list[Finding]:
    from konstruct.analysis.lock import FILE_NAME, LockError, check, consumer_lockfiles, upstream_reads

    graph, diff = context.graph, context.diff
    if not graph.meta.get('repositories'):
        return []
    # A pin judges the change only where the change alters its column, so a consumer that has not
    # re-locked since an earlier change fails its own check, not every change after it.
    altered = set(_changed_columns(graph, diff))
    altered |= {column_id(r.node_id, c.column) for r in diff.relations if r.node_id for c in r.columns}
    # A reworded approved term can change any pin bound to it, whichever column it names.
    reworded = any(d.approved_changed for d in diff.definitions)
    if not altered and not reworded:
        return []
    root = Path(str(graph.meta.get('root', '.')))
    found: list[Finding] = []
    for consumer, path in consumer_lockfiles(graph, root):
        try:
            result = check(
                graph, root, path=path, consumer=consumer, history=False, only=None if reworded else altered
            )
        except LockError as error:
            # Only a change to what the consumer reads needs its lockfile to be judged.
            if altered & {n.id for n in upstream_reads(graph, consumer)}:
                found.append(
                    Finding('consumer-pin-changed', f'the lockfile of {consumer} cannot be checked: {error}')
                )
            continue
        for change in result.changes:
            if (
                reworded
                and 'definition' not in change.reasons
                and not _pinned_in(graph, change.column, altered)
            ):
                continue
            what = ', '.join(change.reasons)
            shown = (
                f' (locked {change.formula_before}; now {change.formula_after})'
                if 'formula' in change.reasons
                else ''
            )
            found.append(
                Finding(
                    'consumer-pin-changed',
                    f'{consumer} pins {change.column} in its {FILE_NAME}, and the change alters '
                    f'its {what}{shown}',
                    change.path,
                    f'Keep what {change.column} means, or agree the change with {consumer}, who '
                    f'accept it with `{change.accept}`.',
                )
            )
    return found


def _pinned_in(graph: Graph, column: str, altered: set[str]) -> bool:
    """Whether the pinned ``column`` (``relation.column``) is one of the ``altered`` columns."""
    from konstruct.analysis.lock import find

    node = find(graph, column)
    return node is None or node.id in altered


BREAKING = BuiltinCheck(
    'a removed column that something downstream still reads',
    'Keep the column, or change the models that read it in the same change.',
    _breaking,
)
APPROVED_DEFINITION_CHANGED = BuiltinCheck(
    'a change to what an approved business term means',
    "Ask the term's owner to re-approve it before the change is merged.",
    _approved_definition_changed,
)
CONFLICTING_DEFINITION = BuiltinCheck(
    'a changed column that computes a business idea differently from another',
    'Compute it the way the existing column does, or bind each to its own term in definitions.yml.',
    _conflicting_definition,
)

DATA_LAW_BROKEN = BuiltinCheck(
    'a change that breaks a data law which held on all the data',
    'Keep what the law relies on (a join on a tested key, the filters, the factor, the aggregate), '
    'or, if the change is meant, run `konstruct verify --laws` once it is merged.',
    _data_law_broken,
)
CONSUMER_PIN_CHANGED = BuiltinCheck(
    'a change to a column that a consuming repository pins in its konstruct.lock',
    'Keep what the column means, or agree the change with the consumer, who accept it with '
    '`konstruct lock --update`.',
    _consumer_pin_changed,
)


def _sensitive_flow(context: CheckContext) -> list[Finding]:
    return sensitivity.gate_findings(context)


SENSITIVE_FLOW = BuiltinCheck(
    'sensitive data moved into a model or exposure not marked to hold it, a masked column copied in '
    'clear, or a sensitivity mark taken away',
    sensitivity.FLOW_FIX,
    _sensitive_flow,
)

RULES = {
    'breaking': BREAKING.fix,
    'approved-definition-changed': APPROVED_DEFINITION_CHANGED.fix,
    'conflicting-definition': CONFLICTING_DEFINITION.fix,
    'data-law-broken': DATA_LAW_BROKEN.fix,
    'consumer-pin-changed': CONSUMER_PIN_CHANGED.fix,
    'sensitive-flow': sensitivity.FLOW_FIX,
    'unmasked-copy': sensitivity.UNMASKED_FIX,
    'sensitivity-removed': sensitivity.REMOVED_FIX,
}
"""Each of Konstruct's own rules with its fix."""
