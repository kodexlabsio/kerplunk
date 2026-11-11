"""Conflicts: the same business idea computed in more than one way.

``konstruct conflicts`` finds columns that look like the same measure (by name, by synonyms from
definitions.yml, or by being bound to the same business term) and compares how each is computed:

* the formula in terms of source columns (from :mod:`konstruct.analysis.explain`), compared by
  what it computes rather than how it is written (:mod:`konstruct.analysis.identity`), with
  NULLs; formulas that are the same only when no source value is NULL are reported as such;
* the source columns it reads;
* the filters and joins on its route back to source, with the file each lives in;
* the aggregation at the top of the formula.

A column derived from another is a copy, not a competing definition, so such pairs are left out,
and so are pairs that agree on everything but grain (per order against per customer). What is
left is ranked by how much depends on each side downstream (exposures, tasks, columns and
approved terms), so the most consequential disagreements come first.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Collection, Iterator
from dataclasses import dataclass, field, replace
from itertools import combinations
from typing import TYPE_CHECKING, Any

import sqlglot
from sqlglot import exp

from konstruct import _core
from konstruct.analysis.explain import Explainer, short_sql
from konstruct.analysis.identity import Formulas, Verdict, Witness
from konstruct.core.graph import RELATION_KINDS, Graph, Node
from konstruct.core.sql import PARSE_ERRORS, condition_key

if TYPE_CHECKING:
    from konstruct.interfaces.layout import Style

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
# Words that name a measure only when nothing else does: sales_amount is sales, but amount is
# an amount.
GENERIC = frozenset({'amount'})
# As :func:`core` folds them, since it turns ``id`` into its concept.
KEY_WORDS = frozenset(
    {w for k in ('id', 'key', 'uuid', 'pk', 'fk', 'sk', 'hash') for w in (k, _core.concept(k) or k)}
)
FILTER_HEADS = ('WHERE', 'HAVING', 'QUALIFY')
SAME_UNLESS_NULL = 'the same unless a source value is NULL or there are no rows'

# (relation, condition, file), as a side lists its filters and joins.
Condition = tuple[str, str, str | None]
# A side's conditions of one kind, each as (condition, file, key), and the set of their keys.
Keyed = tuple[list[tuple[str, str | None, str]], frozenset[str]]


def core(name: str) -> frozenset[str]:
    """The idea a column name stands for: its words, as search by meaning reads them
    (:func:`konstruct._core.terms`), without qualifiers, each folded to its concept.
    ``total_revenue``, ``revenue`` and ``sales_amount`` all give {revenue}."""
    words = [w for w in _core.terms(name) if w not in MODIFIERS]
    named = [w for w in words if w not in GENERIC] or words
    return frozenset(_core.concept(w) or w for w in named)


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
    witness: Witness | None = None
    """For a formula or aggregation not shown to be the same, values at which the two give
    different results (a proof only when it sets source columns alone; see
    :class:`~konstruct.analysis.identity.Witness`), or the function they apply differently."""

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if v is not None}
        if self.witness is not None:
            data['witness'] = self.witness.to_dict()
        return data


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
            if difference.witness is not None:
                out.append(f'      {difference.witness}')
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

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import conflicts_lines, show

        return show(conflicts_lines, self, style)

    def __str__(self) -> str:
        return self.text()

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
                if difference.witness is not None:
                    lines.append(f'  - {difference.witness}')
            for side, name in ((conflict.a, 'A'), (conflict.b, 'B')):
                if side.carried_into:
                    copies = ', '.join(f'`{c}`' for c in side.carried_into)
                    lines += ['', f"{name}'s definition is carried into {copies}."]
            terms = sorted(set(conflict.a.terms) | set(conflict.b.terms))
            if terms:
                lines += ['', f'Business terms involved: {", ".join(terms)}.']
            lines.append('')
        return '\n'.join(lines).rstrip() + '\n'


def top_function(formula: str | None, dialect: str | None) -> str | None:
    """The aggregate a formula applies last (``SUM`` for ``SUM(x / 100)``), or None."""
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
    """Compares columns two at a time. A column is in many pairs, so everything about one column
    (its side, the keys of its conditions, its top aggregate) is worked out once, and a pair costs
    only the comparison of the two."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        dialect = graph.meta.get('dialect')
        self.dialect = dialect if isinstance(dialect, str) else None
        self.explainer = Explainer(graph)
        self.ancestors: dict[str, set[str]] = {}
        self.formulas: Formulas | None = None
        self.joinless: Formulas | None = None
        self._conditions: dict[str, tuple[list[Condition], list[Condition]]] = {}
        self._sides: dict[str, Side] = {}
        self._ideas: dict[tuple[str, str], Side] = {}
        self._keyed: dict[str, tuple[Keyed, Keyed]] = {}
        self._keys: dict[str, str] = {}
        self._tops: dict[str, str | None] = {}
        self._approved: Counter[str] | None = None

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
            words = set(_core.terms(node.name))
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

    def conditions(self, relation: Node) -> tuple[list[Condition], list[Condition]]:
        """A relation's filters and joins as (relation, condition, file). Many columns share the
        relations on their routes back to source, so each relation's conditions are read once."""
        found = self._conditions.get(relation.id)
        if found is None:
            filters: list[Condition] = []
            joins: list[Condition] = []
            for condition in relation.attrs.get('conditions', []):
                text = short_sql(self.graph, str(condition), condition=True)
                head = text.split(':', 1)[0].upper()
                entry = (self.graph.label(relation), text, self.where(relation))
                if head.startswith(FILTER_HEADS):
                    filters.append(entry)
                elif 'JOIN' in head:
                    joins.append(entry)
            found = self._conditions[relation.id] = (filters, joins)
        return found

    def side(self, column: Node) -> Side:
        """The column as one side of a pair, made once and shared by every pair it is in."""
        found = self._sides.get(column.id)
        if found is None:
            found = self._sides[column.id] = self._side(column)
        return found

    def side_in(self, idea: str, column: Node, carried: list[str]) -> Side:
        """The column's side in the conflicts of one idea, with the columns of that idea derived
        from it: a column can be a side in several ideas, with other copies in each."""
        found = self._ideas.get((idea, column.id))
        if found is None:
            found = self._ideas[idea, column.id] = replace(self.side(column), carried_into=carried)
        return found

    def approved(self) -> Counter[str]:
        """How many approved terms have each name."""
        if self._approved is None:
            self._approved = Counter(
                n.name
                for n in self.graph.nodes.values()
                if n.kind == 'term' and n.attrs.get('status') == 'approved'
            )
        return self._approved

    def _side(self, column: Node) -> Side:
        graph = self.graph
        owner = graph.parent_of(column)
        assert owner is not None
        explanation = self.explainer.explain(column)
        filters: list[Condition] = []
        joins: list[Condition] = []
        relations = {owner.id: owner}
        for ancestor in self.upstream(column.id):
            parent = graph.parent_of(graph.nodes[ancestor])
            if parent is not None:
                relations[parent.id] = parent
        for relation in relations.values():
            relation_filters, relation_joins = self.conditions(relation)
            filters.extend(relation_filters)
            joins.extend(relation_joins)
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
        approved = self.approved()
        reach['terms'] = sum(approved[name] for name in terms)
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

    def key(self, condition: str) -> str:
        """The condition in the form that compares equal to any condition that filters alike."""
        found = self._keys.get(condition)
        if found is None:
            found = self._keys[condition] = condition_key(condition, self.dialect)
        return found

    def keyed(self, side: Side) -> tuple[Keyed, Keyed]:
        """The side's filters and its joins, each condition with its key."""
        found = self._keyed.get(side.column)
        if found is None:
            kinds = []
            for items in (side.filters, side.joins):
                listed = [(condition, path, self.key(condition)) for _, condition, path in items]
                kinds.append((listed, frozenset(k for _, _, k in listed)))
            found = self._keyed[side.column] = (kinds[0], kinds[1])
        return found

    def top(self, formula: str | None) -> str | None:
        """:func:`top_function` of the formula, read once for every pair it is in."""
        if formula is None:
            return None
        if formula not in self._tops:
            self._tops[formula] = top_function(formula, self.dialect)
        return self._tops[formula]

    def compare(self, a: Side, b: Side) -> list[Difference]:
        differences: list[Difference] = []
        sources_a, sources_b = set(a.sources), set(b.sources)
        for source in sorted(sources_a - sources_b):
            differences.append(Difference('source', source, 'a'))
        for source in sorted(sources_b - sources_a):
            differences.append(Difference('source', source, 'b'))
        for kind, (listed_a, keys_a), (listed_b, keys_b) in zip(
            ('filter', 'join'), self.keyed(a), self.keyed(b), strict=True
        ):
            # Conditions that filter alike, however they are written, are not a difference.
            for condition, path, key in listed_a:
                if key not in keys_b:
                    differences.append(Difference(kind, condition, 'a', path))
            for condition, path, key in listed_b:
                if key not in keys_a:
                    differences.append(Difference(kind, condition, 'b', path))
        if a.composed is not None and a.composed == b.composed:
            # The same text is the same formula. What else tells the two apart, such as a
            # not_null test on one or an outer join on the route of one, is in the filters and
            # joins above, not in the formula.
            return differences
        verdict = self.verdict(a, b)
        # A verdict of the same with no guarantee (a divisor zero at a sampled point) is not
        # taken as the same.
        trusted = verdict is not None and verdict.error_bound < 1
        if trusted and verdict is not None and verdict.equal:
            return differences
        if trusted and any(d.kind == 'join' for d in differences) and self.same_inside_joins(a, b):
            # They differ only through an outer join on one route, which is reported above.
            return differences
        witness = verdict.witness if verdict is not None else None
        if trusted and verdict is not None and verdict.equal_without_nulls:
            if not any(d.kind == 'source' for d in differences):
                detail = f'{a.composed} against {b.composed}, {SAME_UNLESS_NULL}'
                differences.append(Difference('formula', detail, witness=witness))
            return differences
        top_a, top_b = self.top(a.composed), self.top(b.composed)
        if top_a != top_b and (top_a or top_b):
            detail = f'{top_a or "none"} against {top_b or "none"}'
            differences.append(Difference('aggregation', detail, witness=witness))
            witness = None
        differs = verdict is not None or a.composed != b.composed
        if differs and not any(d.kind in ('source', 'aggregation') for d in differences):
            differences.append(Difference('formula', f'{a.composed} against {b.composed}', witness=witness))
        return differences

    def same_inside_joins(self, a: Side, b: Side) -> bool:
        """Whether the two formulas are the same when a column read through an outer join is
        taken as it is in its own relation: then what tells them apart is the join."""
        if self.formulas is None or not self.formulas.joins:
            return False
        if self.joinless is None:
            self.joinless = Formulas(self.graph, conditions=False, outer=False)
        first = self.joinless.column(self.graph.nodes[a.column])
        verdict = self.joinless.compare(first, self.joinless.column(self.graph.nodes[b.column]))
        return verdict.equal and verdict.error_bound < 1

    def verdict(self, a: Side, b: Side) -> Verdict | None:
        """Whether two columns' formulas compute the same values, however they are written.
        Their conditions are left out: the filters and joins on each route are compared on
        their own, and reported with the files they are in."""
        if a.composed is None or b.composed is None:
            return None
        if self.formulas is None:
            self.formulas = Formulas(self.graph, conditions=False)
        first = self.formulas.column(self.graph.nodes[a.column])
        return self.formulas.compare(first, self.formulas.column(self.graph.nodes[b.column]))


def _pairs(nodes: list[Node], touching: Collection[str] | None) -> Iterator[tuple[Node, Node]]:
    """Each pair of ``nodes`` in the order :func:`itertools.combinations` gives them; with
    ``touching``, only the pairs with a column in it, without visiting the others."""
    if touching is None:
        yield from combinations(nodes, 2)
        return
    marked = [i for i, node in enumerate(nodes) if node.id in touching]
    for i, first in enumerate(nodes):
        later = range(i + 1, len(nodes)) if first.id in touching else marked[bisect_right(marked, i) :]
        for j in later:
            yield first, nodes[j]


def find_conflicts(
    graph: Graph, limit: int | None = None, *, touching: Collection[str] | None = None
) -> Conflicts:
    """Pairs of columns that appear to be one business idea computed differently, ranked.

    With ``touching``, a set of column ids, only the pairs with a column in it are compared: the
    conflicts are those of every pair that involve one of those columns, in the same order, at a
    cost that grows with how many columns it names rather than with the number of pairs."""
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
        # Each member's own ancestors among the roots, not every root against every member.
        carried: dict[str, list[str]] = {r.id: [] for r in roots}
        for member in members:
            for root in finder.upstream(member.id) & carried.keys():
                carried[root].append(graph.label(member))
        for labels in carried.values():
            labels.sort()
        for first, second in _pairs(sorted(roots, key=lambda n: n.id), touching):
            pair = (first.id, second.id)
            if pair in seen_pairs or first.parent == second.parent:
                continue
            seen_pairs.add(pair)
            # A column derived from the other is a copy, not a competing definition.
            if first.id in finder.upstream(second.id) or second.id in finder.upstream(first.id):
                continue
            a, b = (
                finder.side_in(key, first, carried[first.id]),
                finder.side_in(key, second, carried[second.id]),
            )
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
    for first, second in combinations(roots, 2):
        if first.parent == second.parent:
            continue
        a, b = finder.side(first), finder.side(second)
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
