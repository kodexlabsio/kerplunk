"""Conflicts: the same business idea computed in more than one way.

``konstruct conflicts`` finds columns that look like the same measure (by name, by synonyms from
definitions.yml, or by being bound to the same business term) and compares how each is computed:

* the formula in terms of source columns (from :mod:`konstruct.analysis.explain`);
* the source columns it reads;
* the filters and joins on its route back to source, with the file each lives in;
* the aggregation at the top of the formula.

A column derived from another is a copy, not a competing definition, so such pairs are left out,
and so are pairs that agree on everything but grain (per order against per customer). What is
left is ranked by how much depends on each side downstream (exposures, tasks, columns and
approved terms), so the most consequential disagreements come first.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

import sqlglot
from sqlglot import exp

from konstruct.analysis.explain import Explainer, short_sql
from konstruct.core.graph import RELATION_KINDS, Graph, Node
from konstruct.core.sql import PARSE_ERRORS

# Words that qualify a measure without changing which measure it is.
MODIFIERS = frozenset(
    {
        'total',
        'sum',
        'overall',
        'final',
        'all',
        'the',
        'of',
        'calc',
        'calculated',
        'adjusted',
        'agg',
        'metric',
        'kpi',
    }
)
# Words that name the same idea. Each set maps to its first word.
SYNONYMS = (
    ('revenue', 'sales', 'turnover', 'income', 'takings', 'amount', 'amt'),
    ('count', 'cnt', 'number', 'num', 'qty', 'quantity'),
    ('customer', 'client', 'cust', 'shopper', 'buyer'),
    ('order', 'purchase', 'transaction', 'txn'),
    ('margin', 'profit'),
    ('cost', 'expense', 'spend'),
    ('price', 'unit_price'),
    ('discount', 'rebate'),
)
KEY_WORDS = frozenset({'id', 'key', 'uuid', 'pk', 'fk', 'sk', 'hash'})
FILTER_HEADS = ('WHERE', 'HAVING', 'QUALIFY')
_CANONICAL = {word: group[0] for group in SYNONYMS for word in group}


def _words(name: str) -> list[str]:
    spaced = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', name)
    return [w for w in re.split(r'[^a-z0-9]+', spaced.lower()) if w]


def _singular(word: str) -> str:
    if len(word) > 3 and word.endswith('ies'):
        return word[:-3] + 'y'
    if len(word) > 3 and word.endswith('s') and not word.endswith('ss'):
        return word[:-1]
    return word


def core(name: str) -> frozenset[str]:
    """The idea a column name stands for: its words, singular, without qualifiers, with synonyms
    folded together. ``total_revenue``, ``revenue`` and ``sales_amount`` all give {revenue}."""
    words = {_CANONICAL.get(w) or _CANONICAL.get(_singular(w), _singular(w)) for w in _words(name)}
    return frozenset(words - MODIFIERS)


@dataclass
class Difference:
    kind: str
    """``formula``, ``source``, ``filter``, ``join`` or ``aggregation``."""
    detail: str
    """What differs, such as a condition, a source column or two formulas."""
    side: str | None = None
    """Which column has it (``a`` or ``b``) when only one does; None when both differ."""
    path: str | None = None
    """The file the filter or join is in."""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Side:
    column: str
    """The column id."""
    label: str
    """Its short name."""
    relation_kind: str
    """The kind of its relation."""
    path: str | None
    """The file that builds it."""
    expression: str | None
    """Its own expression."""
    composed: str | None
    """Its formula in terms of source columns."""
    sources: list[str] = field(default_factory=list)
    """The source columns it reads."""
    filters: list[tuple[str, str, str | None]] = field(default_factory=list)
    """(relation, condition, file) for each filter on the route back to source."""
    joins: list[tuple[str, str, str | None]] = field(default_factory=list)
    """(relation, condition, file) for each join on its route back to source."""
    reach: dict[str, int] = field(default_factory=dict)
    """Counts of what depends on it downstream (exposures, tasks, columns and approved terms)."""
    terms: list[str] = field(default_factory=list)
    """Business terms bound to it or to anything downstream."""
    carried_into: list[str] = field(default_factory=list)
    """Columns of the same idea that are derived from this one, and so inherit its definition."""

    @property
    def weight(self) -> int:
        r = self.reach
        return (
            10 * r.get('exposures', 0) + 5 * r.get('terms', 0) + 3 * r.get('tasks', 0) + r.get('columns', 0)
        )

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data['filters'] = [{'relation': r, 'condition': c, 'path': p} for r, c, p in self.filters]
        data['joins'] = [{'relation': r, 'condition': c, 'path': p} for r, c, p in self.joins]
        data['weight'] = self.weight
        return data


@dataclass
class Conflict:
    idea: str
    """What the columns appear to measure, as a term name or the shared words of their names."""
    reason: str
    """Why the two columns were compared."""
    a: Side
    """One side."""
    b: Side
    """The other side."""
    differences: list[Difference] = field(default_factory=list)
    """What differs between them."""

    @property
    def score(self) -> int:
        return self.a.weight + self.b.weight

    def to_dict(self) -> dict[str, Any]:
        return {
            'idea': self.idea,
            'reason': self.reason,
            'score': self.score,
            'a': self.a.to_dict(),
            'b': self.b.to_dict(),
            'differences': [d.to_dict() for d in self.differences],
        }

    def lines(self) -> list[str]:
        out = [f'{self.a.label}  vs  {self.b.label}  ({self.reason}; downstream weight {self.score})']
        for side, name in ((self.a, 'a'), (self.b, 'b')):
            formula = side.composed or side.expression or 'no recorded formula'
            out.append(f'  {name}: {side.label} = {formula}')
            where = side.path or '?'
            out.append(f'     in {where}; feeds {_reach_text(side.reach)}')
            if side.carried_into:
                out.append(f'     carried into {", ".join(side.carried_into)}')
        for difference in self.differences:
            who = (
                f' only in {self.a.label if difference.side == "a" else self.b.label}'
                if difference.side
                else ''
            )
            where = f'  ({difference.path})' if difference.path else ''
            out.append(f'  - {difference.kind}{who}: {difference.detail}{where}')
        return out


def _reach_text(reach: dict[str, int]) -> str:
    parts = [f'{v} {k}' for k, v in reach.items() if v]
    return ', '.join(parts) or 'nothing downstream'


@dataclass
class Conflicts:
    conflicts: list[Conflict] = field(default_factory=list)
    """Every conflict, most consequential first."""
    candidates: int = 0
    """Computed columns considered."""
    groups: int = 0
    """Groups of columns that appear to be one idea."""

    def summary(self) -> str:
        if not self.conflicts:
            return f'No conflicting definitions among {self.candidates} computed columns.'
        count = len(self.conflicts)
        return (
            f'{count} conflicting definition{"s" if count != 1 else ""} among '
            f'{self.candidates} computed columns.'
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            'summary': self.summary(),
            'candidates': self.candidates,
            'groups': self.groups,
            'conflicts': [c.to_dict() for c in self.conflicts],
        }

    def __str__(self) -> str:
        from konstruct.interfaces.render import plain

        return plain(self)

    def markdown(self) -> str:
        lines = ['# Conflicting definitions', '', self.summary(), '']
        if self.conflicts:
            lines += [
                'Each entry is a pair of columns that appear to measure the same thing but are computed '
                'differently. They are ranked by how much depends on them downstream.',
                '',
            ]
        for index, conflict in enumerate(self.conflicts, start=1):
            lines += [f'## {index}. {conflict.idea}: `{conflict.a.label}` and `{conflict.b.label}`', '']
            lines += [f'Found because they {conflict.reason}. Downstream weight {conflict.score}.', '']
            lines += ['| | Column | Formula in terms of sources | File | Feeds |', '|---|---|---|---|---|']
            for side, name in ((conflict.a, 'A'), (conflict.b, 'B')):
                formula = (side.composed or side.expression or 'none recorded').replace('|', '\\|')
                lines.append(
                    f'| {name} | `{side.label}` | `{formula}` | {side.path or ""} | {_reach_text(side.reach)} |'
                )
            lines += ['', '**What differs**', '']
            for difference in conflict.differences:
                who = ''
                if difference.side:
                    who = f' (only in {"A" if difference.side == "a" else "B"})'
                where = f', in `{difference.path}`' if difference.path else ''
                lines.append(f'- {difference.kind}{who}: `{difference.detail}`{where}')
            for side, name in ((conflict.a, 'A'), (conflict.b, 'B')):
                if side.carried_into:
                    copies = ', '.join(f'`{c}`' for c in side.carried_into)
                    lines += ['', f"{name}'s definition is carried into {copies}."]
            terms = sorted(set(conflict.a.terms) | set(conflict.b.terms))
            if terms:
                lines += ['', f'Business terms involved: {", ".join(terms)}.']
            lines.append('')
        return '\n'.join(lines).rstrip() + '\n'


def _top_function(formula: str | None, dialect: str | None) -> str | None:
    if not formula:
        return None
    try:
        tree = sqlglot.parse_one(formula, read=dialect)
    except PARSE_ERRORS:
        return None
    node = tree
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.AggFunc):
        return node.key.upper()
    return None


class _Finder:
    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        dialect = graph.meta.get('dialect')
        self.dialect = dialect if isinstance(dialect, str) else None
        self.explainer = Explainer(graph)
        self.ancestors: dict[str, set[str]] = {}

    def upstream(self, column_id: str) -> set[str]:
        if column_id not in self.ancestors:
            seen: set[str] = set()
            stack = [column_id]
            while stack:
                for edge in self.graph.out_edges(stack.pop(), 'derived_from'):
                    if edge.target not in seen:
                        seen.add(edge.target)
                        stack.append(edge.target)
            self.ancestors[column_id] = seen
        return self.ancestors[column_id]

    def candidates(self) -> list[Node]:
        found = []
        for node in self.graph.nodes.values():
            if node.kind != 'column' or not isinstance(node.attrs.get('expression'), str):
                continue
            owner = self.graph.parent_of(node)
            if owner is None or owner.kind not in RELATION_KINDS:
                continue
            words = {_singular(w) for w in _words(node.name)}
            if words & KEY_WORDS or not core(node.name):
                continue
            # A column copied as it is (a dimension, a key, a date) is lineage, not a definition.
            inputs = self.graph.out_edges(node.id, 'derived_from')
            if inputs and all(e.attrs.get('transform') == 'direct' for e in inputs):
                continue
            found.append(node)
        return found

    def term_keys(self) -> dict[frozenset[str], str]:
        keys: dict[frozenset[str], str] = {}
        for node in self.graph.nodes.values():
            if node.kind == 'term':
                for name in [node.name, *node.attrs.get('synonyms', [])]:
                    key = core(name)
                    if key:
                        keys.setdefault(key, node.name)
        return keys

    def where(self, relation: Node) -> str | None:
        """The file that builds a relation: its own, or that of the task that writes it."""
        if relation.path:
            return relation.path
        for edge in self.graph.in_edges(relation.id, 'feeds'):
            source = self.graph.nodes[edge.source]
            if source.kind == 'task' and source.path:
                return source.path
        return None

    def side(self, column: Node) -> Side:
        graph = self.graph
        owner = graph.parent_of(column)
        assert owner is not None
        explanation = self.explainer.explain(column)
        filters: list[tuple[str, str, str | None]] = []
        joins: list[tuple[str, str, str | None]] = []
        relations = {owner.id: owner}
        for ancestor in self.upstream(column.id):
            parent = graph.parent_of(graph.nodes[ancestor])
            if parent is not None:
                relations[parent.id] = parent
        for relation in relations.values():
            for condition in relation.attrs.get('conditions', []):
                text = short_sql(graph, str(condition), condition=True)
                head = text.split(':', 1)[0].upper()
                entry = (graph.label(relation), text, self.where(relation))
                if head.startswith(FILTER_HEADS):
                    filters.append(entry)
                elif 'JOIN' in head:
                    joins.append(entry)
        walk = graph.impact(column)
        reach = {
            'exposures': sum(1 for s in walk if s.node.kind == 'exposure'),
            'tasks': sum(1 for s in walk if s.node.kind == 'task'),
            'columns': sum(1 for s in walk if s.node.kind == 'column'),
        }
        terms = sorted(
            {
                graph.nodes[e.source].name
                for target in [column.id, *(s.node.id for s in walk)]
                for e in graph.in_edges(target, 'defines')
            }
        )
        reach['terms'] = sum(
            1
            for name in terms
            for n in graph.nodes.values()
            if n.kind == 'term' and n.name == name and n.attrs.get('status') == 'approved'
        )
        return Side(
            column=column.id,
            label=graph.label(column),
            relation_kind=owner.kind,
            path=self.where(owner),
            expression=explanation.expression,
            composed=explanation.composed,
            sources=explanation.sources,
            filters=sorted(set(filters)),
            joins=sorted(set(joins)),
            reach=reach,
            terms=terms,
        )

    def compare(self, a: Side, b: Side) -> list[Difference]:
        differences: list[Difference] = []
        sources_a, sources_b = set(a.sources), set(b.sources)
        for source in sorted(sources_a - sources_b):
            differences.append(Difference('source', source, 'a'))
        for source in sorted(sources_b - sources_a):
            differences.append(Difference('source', source, 'b'))
        for kind, items_a, items_b in (('filter', a.filters, b.filters), ('join', a.joins, b.joins)):
            conditions_a = {c for _, c, _ in items_a}
            conditions_b = {c for _, c, _ in items_b}
            for _relation, condition, path in items_a:
                if condition not in conditions_b:
                    differences.append(Difference(kind, condition, 'a', path))
            for _relation, condition, path in items_b:
                if condition not in conditions_a:
                    differences.append(Difference(kind, condition, 'b', path))
        top_a, top_b = _top_function(a.composed, self.dialect), _top_function(b.composed, self.dialect)
        if top_a != top_b and (top_a or top_b):
            differences.append(Difference('aggregation', f'{top_a or "none"} against {top_b or "none"}'))
        if a.composed != b.composed and not any(d.kind in ('source', 'aggregation') for d in differences):
            differences.append(Difference('formula', f'{a.composed} against {b.composed}'))
        return differences


def find_conflicts(graph: Graph, limit: int | None = None) -> Conflicts:
    """Pairs of columns that appear to be one business idea computed differently, ranked."""
    finder = _Finder(graph)
    candidates = finder.candidates()
    term_keys = finder.term_keys()
    groups: dict[str, list[Node]] = defaultdict(list)
    reasons: dict[str, str] = {}
    for column in candidates:
        words = core(column.name)
        term = term_keys.get(words)
        if term:
            groups[f'term:{term}'].append(column)
            reasons[f'term:{term}'] = f'are named like the business term {term!r}'
        else:
            groups['name:' + ' '.join(sorted(words))].append(column)
        for edge in graph.in_edges(column.id, 'defines'):
            # A column a metric reads is its input, not a column that holds the measure.
            if edge.attrs.get('role') == 'input':
                continue
            name = graph.nodes[edge.source].name
            if column not in groups[f'term:{name}']:
                groups[f'term:{name}'].append(column)
            reasons[f'term:{name}'] = f'are bound to the business term {name!r}'
    result = Conflicts(candidates=len(candidates))
    sides: dict[str, Side] = {}
    seen_pairs: set[tuple[str, str]] = set()
    for key, members in groups.items():
        relations = {m.parent for m in members}
        if len(members) < 2 or len(relations) < 2:
            continue
        result.groups += 1
        idea = key.split(':', 1)[1]
        reason = reasons.get(key, 'have names that mean the same thing')
        # Compare only the columns that define the idea themselves; one derived from another
        # member inherits its definition and is listed as carried into it.
        ids = {m.id for m in members}
        roots = [m for m in members if not (finder.upstream(m.id) & ids)]
        carried = {
            r.id: sorted(graph.label(m) for m in members if r.id in finder.upstream(m.id)) for r in roots
        }
        for first, second in combinations(sorted(roots, key=lambda n: n.id), 2):
            pair = (first.id, second.id)
            if pair in seen_pairs or first.parent == second.parent:
                continue
            seen_pairs.add(pair)
            # A column derived from the other is a copy, not a competing definition.
            if first.id in finder.upstream(second.id) or second.id in finder.upstream(first.id):
                continue
            a = sides.setdefault(first.id, finder.side(first))
            b = sides.setdefault(second.id, finder.side(second))
            a.carried_into = carried.get(first.id, [])
            b.carried_into = carried.get(second.id, [])
            differences = finder.compare(a, b)
            if not differences:
                continue
            result.conflicts.append(Conflict(idea=idea, reason=reason, a=a, b=b, differences=differences))
    result.conflicts.sort(key=lambda c: (-c.score, c.idea, c.a.label, c.b.label))
    if limit is not None:
        result.conflicts = result.conflicts[:limit]
    return result


def measure_conflicts(graph: Graph, term: str, columns: list[Node]) -> list[Conflict]:
    """Pairs of the columns bound to one business term that compute it differently.

    The same comparison as :func:`find_conflicts`, restricted to one term's columns: a column
    derived from another of them carries its definition and is not compared. The ontology uses it
    for the rule that a measure has one formula."""
    finder = _Finder(graph)
    ids = {c.id for c in columns}
    roots = sorted((c for c in columns if not (finder.upstream(c.id) & ids)), key=lambda n: n.id)
    found: list[Conflict] = []
    sides: dict[str, Side] = {}
    for first, second in combinations(roots, 2):
        if first.parent == second.parent:
            continue
        a = sides.setdefault(first.id, finder.side(first))
        b = sides.setdefault(second.id, finder.side(second))
        differences = finder.compare(a, b)
        if differences:
            found.append(
                Conflict(
                    idea=term,
                    reason=f'are bound to the business term {term!r}',
                    a=a,
                    b=b,
                    differences=differences,
                )
            )
    return found
