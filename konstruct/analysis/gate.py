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
  different business terms in ``definitions.yml``.

Each finding says what to do about it, so an agent can fix the change and run the gate again.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from konstruct.analysis.conflicts import Conflicts
from konstruct.analysis.diff import Diff, RelationChange
from konstruct.core.graph import Graph, column_id

RULES = {
    'breaking': 'Keep the column, or change the models that read it in the same change.',
    'approved-definition-changed': "Ask the term's owner to re-approve it before the change is merged.",
    'conflicting-definition': (
        'Compute it the way the existing column does, or bind each to its own term in definitions.yml.'
    ),
}


@dataclass
class Finding:
    """One thing that stops a change."""

    rule: str
    """``breaking``, ``approved-definition-changed`` or ``conflicting-definition``."""
    message: str
    """What is wrong."""
    path: str | None = None
    """The file to change, when one is known, relative to the project."""

    @property
    def fix(self) -> str:
        """What to do about it."""
        return RULES[self.rule]


@dataclass
class Gate:
    """The result of the gate on one change."""

    base: str
    """The commit compared with, as given."""
    findings: list[Finding] = field(default_factory=list)
    """Everything that stops the change."""
    changed: int = 0
    """Files the change touches."""

    @property
    def passed(self) -> bool:
        """Whether nothing stops the change."""
        return not self.findings

    def summary(self) -> str:
        """One line: passed, or how many findings there are."""
        if self.passed:
            return (
                f'Gate passed: {self.changed} changed file{"s" if self.changed != 1 else ""}, nothing breaks.'
            )
        count = len(self.findings)
        return f'Gate failed: {count} finding{"s" if count != 1 else ""} to fix before proposing the change.'

    def __str__(self) -> str:
        from konstruct.interfaces.render import plain

        return plain(self)

    def markdown(self) -> str:
        """The result as markdown, for a pull request comment or a job summary."""
        lines = [f'**{self.summary()}**']
        if self.findings:
            lines += ['', '| Rule | Finding | Fix |', '|---|---|---|']
            for finding in self.findings:
                where = f' (`{finding.path}`)' if finding.path else ''
                lines.append(f'| `{finding.rule}` | {finding.message}{where} | {finding.fix} |')
        return '\n'.join(lines) + '\n'

    def to_dict(self) -> dict[str, Any]:
        """The result as JSON-ready data, each finding with its fix."""
        return {
            'passed': self.passed,
            'base': self.base,
            'summary': self.summary(),
            'findings': [{**asdict(f), 'fix': f.fix} for f in self.findings],
        }


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


def gate(graph: Graph, diff: Diff, conflicts: Conflicts) -> Gate:
    """The gate for ``diff``, given the graph and the conflicts at the head."""
    result = Gate(base=diff.shown_base, changed=len(diff.source_files))
    for relation in diff.relations:
        for column in relation.columns:
            if column.breaks:
                result.findings.append(
                    Finding(
                        'breaking',
                        f'{relation.label}.{column.column} is removed but still read by {", ".join(column.breaks)}',
                        relation.path,
                    )
                )
    for definitions in diff.definitions:
        for name in definitions.approved_changed:
            result.findings.append(
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
                result.findings.append(
                    Finding(
                        'approved-definition-changed',
                        f'{term.name!r} is approved, and the change alters {bound}, which it names',
                        altering.path,
                    )
                )
                break

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
            result.findings.append(
                Finding(
                    'conflicting-definition',
                    f'{mine} computes {conflict.idea!r} differently from {theirs} ({differences})',
                    source.path,
                )
            )
    return result
