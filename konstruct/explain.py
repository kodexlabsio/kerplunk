"""What a column is, as a formula: ``konstruct explain orders.credit_card_amount``.

The build keeps each column's expression as SQL over physical columns, with CTEs, subqueries and
aliases inlined (``attrs["expression"]`` on column nodes), and each relation's join, filter and
grouping conditions in the same form (``attrs["conditions"]``). This module reads those back and:

* renders them with short names (``stg_payments.amount`` rather than the fully qualified name);
* composes the formula down to sources, by substituting each input column with that column's own
  expression, recursively, until the inputs are columns nothing in the graph derives (seeds,
  sources, warehouse tables);
* lists, step by step, the expression of every column on the way and the conditions of every
  relation it passes through.

The composed formula describes values, not rows. Substitution is exact for the expression, but
the joins, filters and grouping of each step decide which rows feed it, so they are listed beside
it rather than folded in.
"""

from __future__ import annotations

import functools
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect

from konstruct import jsonio
from konstruct.graph import Graph, Node

_QUALIFIED_COLUMN = re.compile(r'[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*){1,3}')
MAX_COMPOSED = 4000
"""Stop substituting once the composed formula is longer than this many characters."""

MAX_DEPTH = 25


@dataclass
class Step:
    """One column on the way down: its formula and the conditions on the rows of its relation."""

    column: str
    """The column id."""
    label: str
    """Its short name."""
    relation_kind: str
    """The kind of the column's relation."""
    expression: str | None
    """The column's own expression with short names, or None for a source column."""
    conditions: list[str] = field(default_factory=list)
    """Joins, filters and grouping of the column's relation."""
    inputs: list[str] = field(default_factory=list)
    """Labels of the columns the expression reads."""
    verified: list[str] = field(default_factory=list)
    """For each input, ``verify``'s result for the edge, when it has been run."""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [])}


@dataclass
class Explanation:
    column: str
    """The column explained."""
    label: str
    """Its short name."""
    data_type: str | None
    """Its type."""
    description: str | None
    """Its description."""
    expression: str | None
    """Its own expression, with CTEs and aliases inlined, or None for a source column."""
    composed: str | None
    """The formula in terms of source columns, or None when nothing could be substituted."""
    sources: list[str] = field(default_factory=list)
    """The source columns the formula reaches."""
    steps: list[Step] = field(default_factory=list)
    """Every column from this one upstream, nearest first."""
    complete: bool = True
    """False when some column on the way has no recorded expression (Python, unparsed SQL)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            'column': self.column,
            'label': self.label,
            'data_type': self.data_type,
            'description': self.description,
            'expression': self.expression,
            'composed': self.composed,
            'sources': self.sources,
            'complete': self.complete,
            'steps': [s.to_dict() for s in self.steps],
        }

    def __str__(self) -> str:
        head = self.label + (f'  ({self.data_type})' if self.data_type else '')
        lines = [head]
        if self.description:
            lines.append(f'  {self.description}')
        if self.expression is None:
            lines += ['', '  No expression recorded: this column is read from a source, or the SQL that']
            lines.append('  builds it could not be analysed.')
            return '\n'.join(lines)
        lines += ['', f'  = {self.expression}']
        if self.composed and self.composed != self.expression:
            lines += ['', 'In terms of sources', f'  = {self.composed}']
        if not self.complete:
            lines.append('  (partial: some columns on the way have no recorded expression)')
        if self.sources:
            lines += ['', f'Sources ({len(self.sources)})', *(f'  {s}' for s in self.sources)]
        derived = [s for s in self.steps if s.expression is not None]
        if derived:
            lines += ['', 'Step by step']
            for step in derived:
                checked = step.verified and all(v == 'confirmed' for v in step.verified)
                lines.append(f'  {step.label} = {step.expression}' + ('  [verified]' if checked else ''))
                for condition in step.conditions:
                    lines.append(f'      {condition}')
        return '\n'.join(lines)


def _composed_from(graph: Graph, node: Node) -> list[Node]:
    """The columns a column's formula is composed from. A column a loader lands is a source:
    its link to the source system's column is lineage, not a formula."""
    return [
        graph.nodes[e.target] for e in graph.out_edges(node.id, 'derived_from') if not e.attrs.get('loaded')
    ]


class _Namer:
    """Short names for the fully qualified columns in an expression."""

    def __init__(self, graph: Graph, dialect: str | None) -> None:
        self.graph = graph
        self.dialect = dialect
        # Resolved once: sqlglot builds a new dialect object for every call given only its name.
        self.sqlglot = Dialect.get_or_raise(dialect)
        self.cache: dict[str, Node | None] = {}
        self.texts: dict[str, str] = {}
        self.qualified = False

    def relation(self, key: str) -> Node | None:
        if key not in self.cache:
            self.cache[key] = self.graph.node_for_relation(key)
        return self.cache[key]

    def column(self, name: str, relation: Node) -> exp.Column:
        """``relation.name`` as a column: the relation's short label, or with ``qualified``, its full
        relation key, which stays unambiguous where two relations share a label."""
        if self.qualified and relation.relation:
            *prefix, table = relation.relation.split('.')
            return exp.column(name, table, *reversed(prefix), quoted=False)
        return exp.column(name, table=self.graph.label(relation), quoted=False)

    def resolve(self, column: exp.Column) -> tuple[Node, str] | None:
        """The relation node and column name a qualified column refers to: the longest prefix of
        its parts that names a relation, so nested fields (``orders.customer.country``) work."""
        parts = [p.name for p in column.parts]
        for split in range(len(parts) - 1, 0, -1):
            node = self.relation('.'.join(parts[:split]))
            if node is not None:
                return node, '.'.join(parts[split:])
        return None

    def parse(self, sql: str) -> exp.Expr | None:
        # A plain qualified column, the commonest expression in a graph (every column a model passes
        # through), is built directly rather than parsed. A single name goes through the parser,
        # which knows the words that are functions or literals rather than columns.
        if _QUALIFIED_COLUMN.fullmatch(sql):
            *prefix, name = sql.split('.')
            return exp.column(name, *reversed(prefix), quoted=False)
        try:
            tree: exp.Expr = sqlglot.parse_one(sql, read=self.sqlglot)
            return tree
        except (sqlglot.errors.ParseError, sqlglot.errors.TokenError, ValueError):
            return None

    def short(self, tree: exp.Expr) -> str:
        def rename(node: exp.Expr) -> exp.Expr:
            if isinstance(node, exp.Column):
                found = self.resolve(node)
                if found is not None:
                    relation, name = found
                    return self.column(name, relation)
            return node

        # The tree is the one text() just parsed, so it is renamed in place.
        return _unparen(tree.transform(rename, copy=False)).sql(dialect=self.sqlglot, copy=False)

    def text(self, sql: str) -> str:
        """``sql`` with short relation names, worked out once per distinct text."""
        found = self.texts.get(sql)
        if found is None:
            tree = self.parse(sql)
            found = self.short(tree) if tree is not None else sql
            self.texts[sql] = found
        return found

    def condition(self, text: str) -> str:
        prefix, sep, rest = text.partition(': ')
        if not sep:
            return self.text(text)
        return f'{prefix}: {self.text(rest)}'


def short_sql(graph: Graph, sql: str, *, condition: bool = False) -> str:
    """An expression or condition from the graph with short relation names."""
    dialect = graph.meta.get('dialect')
    namer = _Namer(graph, dialect if isinstance(dialect, str) else None)
    return namer.condition(sql) if condition else namer.text(sql)


def _call(node: exp.Expr | None) -> bool:
    """Whether ``node`` prints as a function call, ``f(...)``: some functions print as operators
    (``a XOR b``), and parentheses around or inside those still matter."""
    return isinstance(node, exp.Func) and not isinstance(node, (exp.Binary, exp.Unary, exp.Connector))


def _unparen(tree: exp.Expr) -> exp.Expr:
    """Drop parentheses that substitution added where they change nothing: around a whole
    function argument, a CASE branch, another parenthesis or the whole formula.

    Children are tidied before their parents, so tidying a formula alone gives the same result
    as tidying it inside a larger one."""

    def droppable(node: exp.Paren) -> bool:
        inner, parent = node.this, node.parent
        if isinstance(parent, exp.Dot) and parent.this is node:
            return False  # (a).b is field b of a; a.b is column b of a
        return (
            parent is None
            or isinstance(inner, (exp.Column, exp.Literal, exp.Paren))
            or _call(inner)
            or isinstance(parent, (exp.Paren, exp.If, exp.Case, exp.Alias))
            or _call(parent)
            or (isinstance(parent, exp.Connector) and isinstance(inner, exp.Predicate))
        )

    # Reversed pre-order puts every node after its descendants, without recursion: a formula
    # can be thousands of levels deep (a wide sum, a long OR chain).
    root = tree
    for node in reversed(list(tree.dfs())):
        if isinstance(node, exp.Paren) and droppable(node):
            inner: exp.Expr = node.this
            if node.parent is not None:
                node.replace(inner)
            else:
                inner.pop()
                root = inner
    return root


@dataclass
class _Composed:
    """A column's formula with its inputs substituted, the source columns it reached, and whether
    every column on the way had an expression."""

    tree: exp.Expr | None
    """The composed formula, already with short names."""
    sources: frozenset[str]
    complete: bool
    size: int = 0
    """Approximate length of the composed formula as SQL, to keep substitution within budget."""
    original: str | None = None
    """The column's own expression with short names, before substitution."""


class Explainer:
    """Explains columns of one graph, composing each column once however many ask for it."""

    def __init__(self, graph: Graph, max_composed: int = MAX_COMPOSED, *, qualified: bool = False) -> None:
        self.graph = graph
        self.max_composed = max_composed
        dialect = graph.meta.get('dialect')
        self.namer = _Namer(graph, dialect if isinstance(dialect, str) else None)
        self.namer.qualified = qualified
        self.memo: dict[str, _Composed] = {}
        self.active: set[str] = set()
        self.short_memo: dict[str, str | None] = {}

    def composed_text(self, node: Node) -> str | None:
        """The composed formula of ``node`` with short names, rendered once per column."""
        if node.id not in self.short_memo:
            composed = self.compose(node)
            self.short_memo[node.id] = (
                _unparen(composed.tree.copy()).sql(dialect=self.namer.sqlglot, copy=False)
                if composed.tree is not None
                else None
            )
        return self.short_memo[node.id]

    def compose(self, node: Node, depth: int = 0) -> _Composed:
        """``node``'s expression with every input column replaced by that column's own composed
        expression; a column without an expression is a source (or, if it has inputs, a gap)."""
        if node.id in self.memo:
            return self.memo[node.id]
        if node.id in self.active or depth > MAX_DEPTH:
            return _Composed(None, frozenset(), False)
        graph = self.graph
        expression = node.attrs.get('expression')
        upstream = _composed_from(graph, node)
        if not isinstance(expression, str):
            leaf = _Composed(None, frozenset() if upstream else frozenset({node.id}), not upstream)
            self.memo[node.id] = leaf
            return leaf
        parsed = self.namer.parse(expression)
        if parsed is None:
            failed = _Composed(None, frozenset(), False)
            self.memo[node.id] = failed
            return failed
        tree: exp.Expr = parsed
        self.active.add(node.id)
        by_name = {(u.parent or '', u.name.lower()): u for u in upstream}
        by_relation = {}
        for u in upstream:
            owner = graph.parent_of(u)
            if owner is not None and owner.relation:
                by_relation[(owner.relation.lower(), u.name.lower())] = u
        sources: set[str] = set()
        complete = True
        # Formulas roughly double at each join, so the budget is checked before substituting,
        # not after: an input too large to inline stays a column reference.
        size = len(expression)

        def substitute(item: exp.Expr) -> exp.Expr:
            nonlocal complete, size
            if not isinstance(item, exp.Column):
                return item
            found = self.namer.resolve(item)
            if found is None:
                return item
            relation, name = found
            # Whatever happens next, a column that stays in the formula gets its short name.
            short = self.namer.column(name, relation)
            target = (
                by_name.get((relation.id, name.lower()))
                or (by_relation.get((relation.relation.lower(), name.lower())) if relation.relation else None)
                or graph.nodes.get(f'{relation.id}#{name}')
            )
            if target is None:
                return short
            inner = self.compose(target, depth + 1)
            sources.update(inner.sources)
            complete = complete and inner.complete
            if inner.tree is None:
                return short
            if size + inner.size > self.max_composed:
                complete = False
                return short
            size += inner.size
            if isinstance(inner.tree, (exp.Column, exp.Literal, exp.Paren)) or _call(inner.tree):
                return inner.tree.copy()
            return exp.Paren(this=inner.tree.copy())

        # The column's own expression is named before substitution, so the parsed tree, which
        # nothing else holds, can then be rewritten in place.
        original = self.namer.text(expression)
        composed = tree.transform(substitute, copy=False)
        self.active.discard(node.id)
        result = _Composed(composed, frozenset(sources), complete, size, original)
        self.memo[node.id] = result
        return result

    def steps(self, column: Node) -> list[Step]:
        """Every column from ``column`` upstream, nearest first, with its formula and conditions."""
        graph = self.graph
        seen = {column.id}
        queue = [column]
        steps = []
        while queue:
            node = queue.pop(0)
            expression = node.attrs.get('expression')
            owner = graph.parent_of(node)
            edges = graph.out_edges(node.id, 'derived_from')
            steps.append(
                Step(
                    column=node.id,
                    label=graph.label(node),
                    relation_kind=owner.kind if owner else 'column',
                    expression=self.namer.text(expression) if isinstance(expression, str) else None,
                    conditions=[self.namer.condition(c) for c in owner.attrs.get('conditions', [])]
                    if owner
                    else [],
                    inputs=[graph.label(graph.nodes[e.target]) for e in edges],
                    verified=[str(e.attrs['verified']) for e in edges if e.attrs.get('verified')],
                )
            )
            for edge in edges:
                if edge.target not in seen:
                    seen.add(edge.target)
                    queue.append(graph.nodes[edge.target])
        return steps

    def explain(self, column: Node) -> Explanation:
        """The formula behind ``column`` and how it composes down to sources (see :func:`explain`)."""
        raw = column.attrs.get('expression')
        result = Explanation(
            column=column.id,
            label=self.graph.label(column),
            data_type=column.data_type,
            description=column.description,
            expression=self.namer.text(raw) if isinstance(raw, str) else None,
            composed=None,
        )
        if not isinstance(raw, str):
            return result
        composed = self.compose(column)
        result.composed = self.composed_text(column)
        result.sources = sorted(self.graph.label(self.graph.nodes[s]) for s in composed.sources)
        result.complete = composed.complete
        result.steps = self.steps(column)
        return result

    def fingerprints(self) -> dict[str, str]:
        """A hash per column of everything its summary depends on: its expression, its label and
        the fingerprints of the columns it is derived from (a Merkle hash over the lineage), so a
        cached summary is reused exactly when nothing upstream of the column changed."""
        graph = self.graph
        found: dict[str, str] = {}
        for start in graph.nodes.values():
            if start.kind != 'column' or start.id in found:
                continue
            stack: list[tuple[Node, bool]] = [(start, False)]
            on_stack = {start.id}
            while stack:
                node, expanded = stack.pop()
                upstream = _composed_from(graph, node)
                if not expanded:
                    stack.append((node, True))
                    for other in upstream:
                        if other.id not in found and other.id not in on_stack:
                            on_stack.add(other.id)
                            stack.append((other, False))
                    continue
                parts = [
                    node.id,
                    graph.label(node),
                    str(node.attrs.get('expression', '')),
                    *sorted(f'{u.id}={found.get(u.id, "cycle")}' for u in upstream),
                ]
                found[node.id] = hashlib.sha256('\x00'.join(parts).encode('utf-8')).hexdigest()
                on_stack.discard(node.id)
        return found

    def summary(self, node: Node) -> dict[str, Any]:
        composed = self.compose(node)
        entry: dict[str, Any] = {'e': composed.original or self.namer.text(node.attrs['expression'])}
        text = self.composed_text(node)
        if text is not None and text != entry['e']:
            entry['c'] = text
            entry['s'] = sorted(self.graph.label(self.graph.nodes[s]) for s in composed.sources)
        if not composed.complete:
            entry['p'] = 1
        return entry

    def summaries(self, limit: int = 50_000, cache: _Cache | None = None) -> dict[str, Any]:
        """Formulas for every column that has one, and conditions for every relation, compactly:
        ``{"columns": {id: {"e": expression, "c": composed, "s": sources, "p": partial}},
        "conditions": {relation id: [condition, ...]}}``. Used by graph.html.

        With ``cache`` (the build cache), each column's summary is keyed by its fingerprint and
        each relation's conditions by their text and the names in the graph, so an update only
        recomputes what changed."""
        columns: dict[str, dict[str, Any]] = {}
        conditions: dict[str, list[str]] = {}
        dialect = self.namer.dialect
        prints = self.fingerprints() if cache is not None else {}
        names = (
            hashlib.sha256(
                '\x00'.join(
                    sorted(
                        f'{n.relation}={self.graph.label(n)}' for n in self.graph.nodes.values() if n.relation
                    )
                ).encode('utf-8')
            ).hexdigest()
            if cache is not None
            else ''
        )
        for node in self.graph.nodes.values():
            if node.kind == 'column' and isinstance(node.attrs.get('expression'), str):
                if len(columns) >= limit:
                    continue
                if cache is None:
                    columns[node.id] = self.summary(node)
                else:
                    key = _cache_key('explain', prints[node.id], self.max_composed, dialect)
                    columns[node.id] = cache.get_or_compute(key, functools.partial(self.summary, node))
            elif node.attrs.get('conditions'):
                raw = [str(c) for c in node.attrs['conditions']]

                def shorten(raw: list[str] = raw) -> list[str]:
                    return [self.namer.condition(c) for c in raw]

                if cache is None:
                    conditions[node.id] = shorten()
                else:
                    conditions[node.id] = cache.get_or_compute(
                        _cache_key('conditions', raw, names, dialect), shorten
                    )
        return {'columns': columns, 'conditions': conditions}


def _cache_key(*parts: Any) -> str:
    return hashlib.sha256(jsonio.dumps(parts, sort_keys=True).encode('utf-8')).hexdigest()


class _Cache(Protocol):
    def get_or_compute(self, key: str, compute: Callable[[], Any]) -> Any: ...


def explain(graph: Graph, column: Node) -> Explanation:
    """The formula behind ``column`` and how it composes down to sources."""
    return Explainer(graph).explain(column)
