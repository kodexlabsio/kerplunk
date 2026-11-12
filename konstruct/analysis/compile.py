"""`konstruct compile-metric`: a question about a metric, compiled to SQL without running it.

An agent asks for a metric by the entities' properties (``lifetime value by customer``), not by
tables, and gets SQL that follows the ontology, with what it rests on:

- **the metric** is found by name, as ``konstruct metric`` finds it. A metric held in a column (a
  business term's column, an aggregate or a measure column Konstruct proposes) is read from the
  relation whose rows it is held per: as it is when the query keeps one row per row of that relation
  (it groups by a unique key of it), and otherwise added up with ``SUM`` where its values add up. A
  metric whose formula aggregates the rows of one relation (a dbt semantic layer metric, an entity
  count) is that formula over that relation's rows.
- **joins** follow only relationships that join each row of the metric's relation to at most one
  row, as far as the project shows it: from its grain, each step goes from the referring side to
  the key it refers to (many-to-one) or along a one-to-one, on the relationship's own columns or on
  the entity's key between two relations holding it, and each lands on a key that a test, a primary
  key something enforces or ``konstruct verify`` shows unique. Where every path to a property
  could repeat rows, the query is refused, naming the link; a link ``konstruct verify`` found the
  data contradicting is never walked. Joins are ``LEFT JOIN``, so a row of the metric whose
  reference finds nothing still counts when the query groups by the joined property (in a group
  of NULL), but a condition on a joined property that is not true of that NULL (a ``where``, a
  required filter, or the range on a joined time property) leaves such a row out, and ``flags``
  says so. A column whose lineage shows it copies a value held at
  another grain than its relation's rows is not added up.
- **required filters** of the metric's definition are always added, written on the relations the
  query reads. One the SQL of the metric's column applies to every source row its value is
  computed from (:meth:`konstruct.analysis.checksql.Checker.held_filters`) is listed as applied
  there, as Konstruct reads it; one that can be placed neither way refuses the query.
- **facts**: the metric, its grain, its column, each relationship and each key the joins rest on,
  and each filter, with its confidence. The result is as trusted as the weakest of them, and says
  in ``flags`` which are only inferred.

Nothing is run: the result is SQL text, written in the project's dialect or the one asked for.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import sqlglot
from sqlglot import exp

from konstruct.core.graph import RELATION_KINDS, Graph, Node
from konstruct.core.sql import PARSE_ERRORS
from konstruct.semantics.ontology import (
    CONFIDENCES,
    Ontology,
    compact,
    derive,
    enforced_key,
    fails_on_any_row,
    matcher,
)
from konstruct.semantics.world import (
    STAGED,
    At,
    Hop,
    Joins,
    Metric,
    Step,
    entity_holders,
    hop,
    keeps_rows,
    key_state,
    metrics,
    path_to,
    paths,
    relation_grain,
    rows,
    shortest,
)

if TYPE_CHECKING:
    from konstruct.interfaces.layout import Style

GRAINS = ('day', 'week', 'month', 'quarter', 'year')
"""The time grains a query can group by."""

HELD = frozenset({'definitions', 'vocabulary', 'ossie', 'ontology pack', 'aggregate', 'measure column'})
"""Where a metric comes from when a column holds its value for each row of a relation."""

_ORDER = {c: i for i, c in enumerate(CONFIDENCES)}
_TIME_TYPE = re.compile(r'^(?:date|datetime|timestamp|time)', re.IGNORECASE)
_TIME_NAME = re.compile(r'(?:^|_)(?:date|at|time|timestamp|ts|day)$', re.IGNORECASE)
_FORBIDDEN = (exp.Query, exp.Subquery, exp.AggFunc, exp.Window, exp.Star, exp.Command, exp.Placeholder)
_OUTER_JOIN = re.compile(r'(LEFT|RIGHT|FULL)\b.*\bJOIN\b.* ON\b')
"""The label of an outer join's ON: ``LEFT JOIN ON``, ``FULL OUTER JOIN ON``, or with the reading
it joins, ``LEFT JOIN db.main."orders#2" ON``, before any later step (``BEFORE LIMIT``)."""
_AGGREGATE = re.compile(
    r'\b(?:SUM|COUNT|AVG|MIN|MAX|ANY_VALUE|ARRAY_AGG|STRING_AGG|LISTAGG|MEDIAN)\s*\(|\bOVER\s*\(', re.I
)
_TYPE_ERRORS: tuple[type[Exception], ...] = (*PARSE_ERRORS, ValueError)
_WEEK = {'bigquery': 'ISOWEEK', 'tsql': 'ISO_WEEK'}
"""Week units that start on Monday, as DuckDB's, Postgres's and Snowflake's (by default) do."""


@dataclass
class OntologyQuery:
    """A question about a metric in the business's terms: the metric, what to group it by, the
    conditions on the rows it adds up and the time it covers."""

    metric: str
    """The metric's name or synonym (``lifetime value``), or a column that holds it, as
    ``konstruct metric`` finds it."""
    by: list[str] = field(default_factory=list)
    """What to group by: an entity (``customer``, its key), an entity's property
    (``customer.first_name``, or ``first_name`` when one entity has it), a time grain of the
    metric's time property (``month``) or of a named one (``order_date:month``). The grains are
    ``day``, ``week``, ``month``, ``quarter`` and ``year``."""
    where: list[str] = field(default_factory=list)
    """Conditions on properties, as SQL (``customer.first_name = 'Michael'``), each named as in
    ``by``; all of them hold."""
    since: str | None = None
    """The first date the metric covers (``2024-01-01``), on its time property, whatever ``by``
    groups by."""
    until: str | None = None
    """The date after the last one it covers: the range is ``since`` up to, not including,
    ``until``."""
    time: str | None = None
    """The metric's time property, for a bare time grain in ``by`` and for the range: by default
    the one date or time column of the relation that holds the metric."""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, [])}


@dataclass
class Fact:
    """One thing the compiled SQL rests on, with how far to trust it."""

    kind: str
    """``metric``, ``grain``, ``column``, ``relationship``, ``key`` or ``filter``."""
    name: str
    """What it is: the metric's name, a relation's grain, a column, a relationship's columns, a
    filter."""
    confidence: str
    """``verified``, ``declared`` or ``inferred``, as the ontology gives it."""
    check: str | None = None
    """``confirmed`` when ``konstruct verify`` confirmed it against the data."""
    evidence: list[str] = field(default_factory=list)
    """What shows it."""


@dataclass
class RequiredFilter:
    """A filter the metric's definition requires, and how the SQL applies it."""

    filter: str
    """As the definition writes it."""
    applied: str | None
    """As the SQL's WHERE clause applies it, on the query's own relations; None when the SQL of
    the column that holds the metric already applies it, on a query the column is computed
    through, to the rows it aggregates: Konstruct's reading of that SQL, so inferred."""
    source: str
    """Where it is required: the metric and its file."""
    by: str = 'query'
    """``query`` when the SQL's WHERE clause applies it, else the relations whose SQL applies it
    on the way to the metric's column."""


@dataclass
class Refusal:
    """Something the compiler would not write, and why."""

    item: str
    """What was asked for: the metric, a ``by`` item, a condition, a filter or the time range."""
    reason: str
    """Why it was refused."""
    link: str | None = None
    """The relationship (its columns) whose join could repeat rows, when that is the reason."""


@dataclass
class Compiled:
    """A metric query compiled to SQL, with the joins, filters and facts it rests on, or what was
    refused and why."""

    query: OntologyQuery
    """What was asked."""
    metric: str | None = None
    """The metric found."""
    sql: str | None = None
    """The SQL, or None when anything was refused."""
    dialect: str | None = None
    """The dialect the SQL is written in."""
    relation: str | None = None
    """The relation whose rows the metric is read from."""
    grain: str | None = None
    """The entity that relation holds one row per, when it holds one."""
    aggregation: str | None = None
    """How the metric is read: ``none`` (the query keeps one row per row of its relation, so the
    column is read as it is), ``sum`` (its values added up) or ``formula`` (its own aggregate over
    the relation's rows)."""
    path: list[Hop] = field(default_factory=list)
    """The relationships the joins follow, each in the direction walked, every one many-to-one or
    one-to-one as the project shows it."""
    filters: list[RequiredFilter] = field(default_factory=list)
    """The filters the metric's definition requires, every one in the SQL."""
    facts: list[Fact] = field(default_factory=list)
    """What the SQL rests on, each with its confidence."""
    confidence: str | None = None
    """The weakest confidence of the facts: the result is as trustworthy as that."""
    flags: list[str] = field(default_factory=list)
    """The facts that are only inferred, and anything else to say when the result is used."""
    refused: list[Refusal] = field(default_factory=list)
    """What was refused, with why; the SQL is None when anything was."""

    @property
    def compiled(self) -> bool:
        """Whether SQL was written: nothing was refused."""
        return self.sql is not None

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import compiled_lines, show

        return show(compiled_lines, self, style)

    def __str__(self) -> str:
        return self.text()

    def to_dict(self) -> dict[str, Any]:
        return {
            'compiled': self.compiled,
            'query': self.query.to_dict(),
            'metric': self.metric,
            'sql': self.sql,
            'dialect': self.dialect,
            'relation': self.relation,
            'grain': self.grain,
            'aggregation': self.aggregation,
            'path': [compact(asdict(h)) for h in self.path],
            'filters': [asdict(f) for f in self.filters],
            'facts': [compact(asdict(f)) for f in self.facts],
            'confidence': self.confidence,
            'flags': self.flags,
            'refused': [compact(asdict(r)) for r in self.refused],
        }


def compile_query(
    graph: Graph, query: OntologyQuery, *, dialect: str | None = None, ontology: Ontology | None = None
) -> Compiled:
    """Compile ``query`` to SQL over the project's relations, in ``dialect`` (by default the
    project's), without running it. Joins follow only relationships the project shows many-to-one
    or one-to-one from the metric's grain, each onto a key shown unique; the metric's required
    filters are always added; and every fact the SQL rests on is listed with its confidence. A join
    that could repeat rows, a value repeated from a coarser grain, a check ``konstruct verify``
    failed, and anything that cannot be placed is refused, with the reason, and then no SQL is
    written.

    Raises:
        konstruct.QueryError: No metric has that name.
    """
    ontology = ontology or derive(graph)
    return _Compiler(graph, ontology, query, dialect).run()


# The compiler


Mark = tuple[int, int, int, dict[str, At], dict[tuple[str, str], Fact], tuple[Any, ...] | None]


class _Compiler:
    def __init__(self, graph: Graph, ontology: Ontology, query: OntologyQuery, dialect: str | None) -> None:
        self.graph = graph
        self.ontology = ontology
        self.query = query
        project = graph.meta.get('dialect')
        self.read = project if isinstance(project, str) and project else None
        self.write = dialect or self.read
        self.result = Compiled(query, dialect=self.write)
        self.holders = entity_holders(graph, ontology)
        self.entities = {e.name: e for e in ontology.entities}
        self.reached: dict[str, At] = {}
        self.tree: dict[str, Step | None] = {}
        self.facts: dict[tuple[str, str], Fact] = {}
        self.joins: Joins | None = None
        self.entity: str | None = None
        self.column: Node | None = None
        self.grouped: Node | None = None
        self.applied: dict[str, str] | None = None

    # Refusing and recording

    def refuse(self, item: str, reason: str, link: str | None = None) -> None:
        refusal = Refusal(item, reason, link)
        if refusal not in self.result.refused:
            self.result.refused.append(refusal)

    def failed(self, item: str) -> None:
        """Refuse ``item`` for the reason the joins give, if they give one."""
        if self.joins is not None and self.joins.problem:
            self.refuse(item, self.joins.problem)
            self.joins.problem = None

    def fact(self, kind: str, name: str, confidence: str, check: str | None, evidence: list[str]) -> None:
        self.facts.setdefault((kind, name), Fact(kind, name, confidence, check, evidence))

    def key_fact(self, column: Node) -> str | None:
        """Record that the SQL rests on ``column`` being unique; why it may not, or None."""
        label = self.graph.label(column)
        state = key_state(self.graph, self.ontology, column)
        if state is None:
            return f'{label} is not shown unique, so joining on it could repeat rows'
        if state[1] == 'failed':
            return f'konstruct verify found {label} repeated or null, so joining on it could repeat rows'
        evidence = (
            'konstruct verify confirmed it unique and never null'
            if state[1]
            else 'a test or a primary key something enforces shows it unique'
        )
        self.fact('key', label, state[0], state[1], [evidence])
        return None

    def land(self, column: Node, key: bool) -> str | None:
        return self.key_fact(column)

    def mark(self) -> Mark:
        """What the query reads, refuses and rests on so far, to go back to."""
        return (
            len(self.result.path),
            len(self.result.refused),
            len(self.result.filters),
            dict(self.reached),
            dict(self.facts),
            self.joins.mark() if self.joins is not None else None,
        )

    def restore(self, mark: Mark) -> None:
        path, refused, filters, self.reached, self.facts, joins = mark
        del self.result.path[path:], self.result.refused[refused:], self.result.filters[filters:]
        if self.joins is not None and joins is not None:
            self.joins.restore(joins)

    # Walking the relationships

    def walk(self) -> None:
        """The best path from the grain to every entity along steps that join each row to at most
        one: a relationship from its referring side, a one-to-one either way, a shared row."""
        start = self.entity
        if start is not None and (start in self.reached or self.grouped is not None):
            self.tree = paths(self.ontology, start, rows(self.graph, self.ontology), keeps_rows)

    def reach(self, name: str, item: str) -> At | None:
        """The relation holding the entity ``name`` that the joins reach, joining what it takes."""
        if name in self.reached:
            return self.reached[name]
        if name == self.entity and self.grouped is not None:
            return self.regroup(name, self.grouped, item)
        path = path_to(self.tree, name) if self.tree else None
        if path is None or self.joins is None or self.entity is None:
            self.unreachable(name, item)
            return None
        at = self.reach(self.entity, item)
        here = self.entity
        for relationship, forward in path:
            there = relationship.target if forward else relationship.source
            if there in self.reached:
                at, here = self.reached[there], there
                continue
            step = hop(relationship, forward)
            joined = self.joins.follow(at, relationship, forward) if at is not None else None
            if joined is None:
                self.failed(item)
                return None
            self.result.path.append(step)
            self.fact('relationship', step.via, step.confidence, step.check, step.evidence)
            self.reached[there] = at = joined
            here = there
        return at if here == name else None

    def regroup(self, name: str, grouped: Node, item: str) -> At | None:
        """For a metric's relation grouped by one column naming its grain, a relation holding that
        entity joined on its key: each group matches at most one of its rows."""
        if self.joins is None:
            return None
        found = self.entities.get(name)
        keys = [
            self.graph.nodes[k] for k in (found.key_ids if found is not None else []) if k in self.graph.nodes
        ]
        keys = [
            k for k in keys if key_state(self.graph, self.ontology, k) not in (None, ('inferred', 'failed'))
        ]
        keys.sort(key=lambda k: (bool(STAGED.match(self.graph.label(k))), self.graph.label(k)))
        relation = self.graph.parent_of(keys[0]) if keys else None
        if relation is None:
            self.refuse(item, f'no relation holding {name} has a key shown unique to join on')
            return None
        joined = self.joins.join(relation, keys[0], self.joins.ref(self.joins.root, grouped), key=True)
        if joined is None:
            self.failed(item)
            return None
        self.reached[name] = joined
        return joined

    def unreachable(self, name: str, item: str) -> None:
        """Refuse ``item``, naming the first link on the shortest path that could repeat rows."""
        start = self.entity
        if start is None or (start not in self.reached and self.grouped is None):
            self.refuse(
                item, f'{self.result.relation} holds no key of an entity, so nothing can be joined to it'
            )
            return
        path = shortest(self.ontology, start, name, rows(self.graph, self.ontology))
        if not path:
            self.refuse(item, f'no relationship connects {start} to {name}')
            return
        for relationship, forward in path:
            step = hop(relationship, forward)
            if relationship.check == 'failed':
                why = f'konstruct verify found the data contradicting {step.start} -> {step.end}, so the join can lose or repeat rows'
            elif step.cardinality is None:
                why = (
                    f'the cardinality of {step.start} -> {step.end} is not known (its key is not shown '
                    'unique), so the join could repeat rows'
                )
            elif not keeps_rows(relationship, forward):
                why = (
                    f'{step.start} -> {step.end} is {step.cardinality}: each {step.start} has many {step.end} '
                    'rows, so joining them would repeat the metric on each, and count it more than once'
                )
            else:
                continue
            self.refuse(item, why, step.via)
            return
        self.refuse(item, f'no path from {start} to {name} joins each row to at most one')

    # Names

    def ref(self, at: At, column: Node) -> exp.Column:
        assert self.joins is not None
        return self.joins.ref(at, column)

    def column_of(self, name: str, at: At, item: str) -> exp.Column | None:
        """The property ``name`` of the entity ``at`` holds: in that relation, else in another
        relation holding the entity, joined on its key."""
        column = self.graph.column(at.relation.id, name)
        if column is not None:
            return self.ref(at, column)
        held = next((n for n, a in self.reached.items() if a is at), None)
        refused = len(self.result.refused)
        found = self.elsewhere(name, at, held, item) if held is not None else None
        if found is None and len(self.result.refused) == refused:
            self.refuse(item, f'{held or self.graph.label(at.relation)} has no property {name!r}')
        return found

    def elsewhere(self, name: str, at: At, held: str, item: str) -> exp.Column | None:
        if self.joins is None:
            return None
        others = [
            self.graph.nodes[r]
            for r, n in self.holders.items()
            if n == held and r in self.graph.nodes and self.graph.column(r, name) is not None
        ]
        others.sort(key=lambda r: (bool(STAGED.match(r.name)), len(self.graph.label(r)), self.graph.label(r)))
        for relation in others:
            mark = self.mark()
            joined = self.joins.by_key(held, at, relation)
            found = self.graph.column(relation.id, name)
            if joined is not None and found is not None:
                return self.ref(joined, found)
            if relation is others[-1]:
                self.failed(item)
            else:
                self.restore(mark)  # another relation holding the entity may still have it
        return None

    def prop(self, text: str, item: str) -> exp.Column | None:
        """The column a property names (``customer.first_name``, ``first_name``, or ``customer``
        for its key), joined in; refuses ``item`` when it cannot be."""
        if self.joins is None:
            return None
        root = self.joins.root
        parts = [p.strip() for p in text.split('.')]
        if len(parts) == 2 and all(parts):
            named = self.ontology.entity(parts[0])
            if named is not None:
                at = self.reach(named.name, item)
                return self.column_of(parts[1], at, item) if at is not None else None
            matches = [n for n in self.graph.resolve(text) if n.kind == 'column']
            if matches:
                return self.place(matches[0], item)
            self.refuse(item, f'no entity or relation called {parts[0]!r}')
            return None
        if len(parts) != 1 or not parts[0]:
            self.refuse(item, f'{text!r} names no property: write entity.property, or a property')
            return None
        name = parts[0]
        named = self.ontology.entity(name)
        if named is not None and named.name == self.entity and self.grouped is not None:
            return self.ref(root, self.grouped)
        if named is not None:
            at = self.reach(named.name, item)
            key = self.joins.key_in(named.name, at.relation) if at is not None else None
            if at is not None and key is None:
                self.refuse(item, f'{self.graph.label(at.relation)} holds no key of {named.name}')
            return self.ref(at, key) if at is not None and key is not None else None
        own = self.graph.column(root.relation.id, name)
        if own is not None:
            return self.ref(root, own)
        having = sorted(
            {
                n
                for r, n in self.holders.items()
                if n in self.tree and r in self.graph.nodes and self.graph.column(r, name) is not None
            }
        )
        if len(having) > 1:
            self.refuse(
                item,
                f'{name!r} is a property of {", ".join(having)}: write which, such as {having[0]}.{name}',
            )
            return None
        if having:
            at = self.reach(having[0], item)
            return self.column_of(name, at, item) if at is not None else None
        elsewhere = sorted(
            {n for r, n in self.holders.items() if r in self.graph.nodes and self.graph.column(r, name)}
        )
        if elsewhere:
            self.unreachable(elsewhere[0], item)
        else:
            self.refuse(item, f'no entity this metric reaches has a property {name!r}')
        return None

    def place(self, column: Node, item: str) -> exp.Column | None:
        """``column`` on a relation the query reads: one already joined, or the relation joined on
        the key of the entity it holds."""
        relation = self.graph.parent_of(column)
        if relation is None or self.joins is None:
            return None
        found = next((a for a in self.joins.at if a.relation.id == relation.id), None)
        if found is not None:
            return self.ref(found, column)
        held = self.holders.get(relation.id)
        if held is None:
            self.refuse(
                item,
                f'{self.graph.label(column)} is on {self.graph.label(relation)}, which holds no entity this '
                'query can join on its key',
            )
            return None
        at = self.reach(held, item)
        joined = self.joins.by_key(held, at, relation) if at is not None else None
        if at is not None and joined is None:
            self.failed(item)
        return self.ref(joined, column) if joined is not None else None

    def condition(
        self,
        text: str,
        item: str,
        resolve: Callable[[exp.Column, str], exp.Column | None],
        dialect: str | None,
    ) -> exp.Expr | None:
        """``text``, read in ``dialect``, as one condition with each column replaced by
        ``resolve(column, item)``; refuses ``item`` when it is not one predicate on columns."""
        try:
            parsed = [s for s in sqlglot.parse(text, read=dialect) if s is not None]
        except PARSE_ERRORS:
            parsed = []
        if len(parsed) != 1 or not _predicate(parsed[0]):
            self.refuse(
                item, f"{text!r} is not one SQL condition: compare a property, such as status = 'completed'"
            )
            return None
        tree = parsed[0]
        if any(True for _ in tree.find_all(*_FORBIDDEN)):
            self.refuse(
                item, f'{text!r} is not a condition on properties: it holds a query, an aggregate or a window'
            )
            return None
        placed: dict[int, exp.Expr] = {}
        for column in list(tree.find_all(exp.Column)):
            found = resolve(column, item)
            if found is None:
                return None
            placed[id(column)] = found
        return tree.transform(lambda n: placed[id(n)].copy() if id(n) in placed else n, copy=False)

    # The question

    def run(self) -> Compiled:
        try:
            sqlglot.Dialect.get_or_raise(self.write)
        except ValueError:
            self.refuse('dialect', f'sqlglot has no SQL dialect called {self.write!r}')
            return self.result
        metric = self.find_metric()
        if metric is None:
            return self.result
        self.result.metric = metric.name
        value = self.source(metric)
        if value is not None:
            self.walk()
            self.build(metric, value)
        self.finish()
        return self.result

    def find_metric(self) -> Metric | None:
        found = metrics(self.graph, self.query.metric, self.ontology).metrics
        wanted = matcher(self.query.metric.strip())
        exact = [m for m in found if any(wanted(n) for n in (m.name, *m.synonyms))]
        chosen = (exact or found)[0]
        same = [m for m in (exact or found) if not m.proposed and m.name != chosen.name]
        if not chosen.proposed and same:
            self.refuse(
                'metric',
                f'{self.query.metric!r} finds {len(same) + 1} defined metrics ({chosen.name}, '
                f'{", ".join(m.name for m in same)}): name one',
            )
            return None
        return chosen

    def source(self, metric: Metric) -> exp.Expr | None:
        """The metric's value over the rows of its relation, which becomes the query's root:
        the column that holds it, or its own aggregate formula."""
        if metric.status == 'retired':
            self.refuse('metric', f'{metric.name} is retired: its definition no longer holds')
            return None
        expression: exp.Expr | None = None
        measure = self.ontology.measure(metric.name) if metric.source == 'ossie' else None
        if metric.source in HELD and not (measure is not None and measure.expression):
            self.column = self.holding(metric)
            relation = self.graph.parent_of(self.column) if self.column is not None else None
            if relation is None:
                self.refuse('metric', f'no column of the project holds {metric.name}')
                return None
            self.result.aggregation = 'sum'
        else:
            if not metric.formula:
                self.refuse('metric', f'{metric.name} has no formula to compile')
                return None
            read = self.formula(metric)
            if read is None:
                return None
            relation, expression = read
            self.result.aggregation = 'formula'
        joins = Joins(self.graph, self.ontology, relation, kind='LEFT', land=self.land)
        if joins.table(relation) is None:
            self.refuse('metric', f'{self.graph.label(relation)} has no warehouse name to read it by')
            return None
        self.joins = joins
        self.result.relation = self.graph.label(relation)
        self.entity = self.holders.get(relation.id) or relation_grain(
            self.graph, relation, self.holders, set(self.holders.values())
        )
        self.result.grain = self.entity
        if self.entity is not None and relation.id in self.holders:
            self.reached[self.entity] = joins.root
        elif self.entity is not None:
            groups = [
                str(c) for c in relation.attrs.get('conditions') or [] if str(c).startswith('GROUP BY: ')
            ]
            name = groups[0].split(': ', 1)[1].rsplit('.', 1)[-1] if groups else ''
            self.grouped = self.graph.column(relation.id, name) if name else None
        self.grain_fact(metric, relation)
        if self.column is not None:
            self.repeated(metric)
            return self.ref(joins.root, self.column)
        if expression is None:
            return None
        placed: dict[int, exp.Expr] = {}
        for item in expression.find_all(exp.Column):
            node = self.labelled(item)
            if node is not None:
                placed[id(item)] = self.ref(joins.root, node)
        return expression.transform(lambda n: placed[id(n)].copy() if id(n) in placed else n, copy=False)

    def repeated(self, metric: Metric) -> None:
        """Refuse a column that holds the metric at another grain than its relation's rows: a
        per-customer value repeated on every order row would be counted once per order."""
        if self.column is None or self.joins is None:
            return
        label = self.graph.label(self.joins.root.relation)
        if metric.entity and self.entity and metric.entity != self.entity:
            self.refuse(
                'metric',
                f'{metric.name} is held one row per {metric.entity}, but {label} holds one row per '
                f'{self.entity}: the value repeats on each {self.entity} row, so a sum would count it '
                'more than once',
            )
            return
        grain = self.entity or metric.entity
        copied = self.coarser(self.column, grain) if grain is not None else None
        if copied is not None:
            self.refuse(
                'metric',
                f'{self.graph.label(self.column)} copies {copied}, a value of another grain than '
                f"{label}'s rows (one per {grain}): it repeats, so a sum would count it more than once",
            )
            return
        joined = self.joined_from(self.column)
        if joined is not None:
            self.refuse(
                'metric',
                f'{self.graph.label(self.column)} copies {joined} across a join, and nothing shows that '
                f'relation holds one row per row of {label}: the value can repeat, so a sum could count it '
                'more than once',
            )

    def joined_from(self, column: Node) -> str | None:
        """For a column that passes a value on unaggregated, through a join on the way to it, the
        relation it copies the value from when that relation's grain is not shown to be its own."""
        if _AGGREGATE.search(str(column.attrs.get('expression') or '')):
            return None  # a sum over the groups of its own query: each source row is in one group
        relation = self.graph.parent_of(column)
        conditions = relation.attrs.get('conditions') if relation is not None else None
        if relation is None or not conditions:
            return None
        positions = column.attrs.get('row_conditions')
        held = (
            [conditions[i] for i in positions if isinstance(i, int) and 0 <= i < len(conditions)]
            if isinstance(positions, list)
            else conditions
        )
        # An outer join's ON holds on no row of the side that keeps them all, and repeats them. Its
        # label can name the reading it joins (``LEFT JOIN db.main."orders#2" ON``).
        outer = [c for c in conditions if _OUTER_JOIN.match(str(c).split(': ', 1)[0])]
        if not any(' JOIN' in f' {str(c).split(": ", 1)[0]}' for c in [*held, *outer]):
            return None
        entities = set(self.holders.values())
        own = relation_grain(self.graph, relation, self.holders, entities)
        for edge in self.graph.out_edges(column.id, 'derived_from'):
            upstream = self.graph.nodes.get(edge.target)
            source = self.graph.parent_of(upstream) if upstream is not None else None
            grain = relation_grain(self.graph, source, self.holders, entities) if source is not None else None
            if source is not None and (own is None or grain != own):
                return self.graph.label(source)
        return None

    def coarser(self, column: Node, grain: str) -> str | None:
        """The column ``column`` passes on, unaggregated, from a relation of another grain than
        ``grain``: the value repeats on each of its rows that joins to that one."""
        entities = set(self.holders.values())
        seen = {column.id}
        pending = [column] if not _AGGREGATE.search(str(column.attrs.get('expression') or '')) else []
        while pending:
            for edge in self.graph.out_edges(pending.pop().id, 'derived_from'):
                upstream = self.graph.nodes.get(edge.target)
                if upstream is None or upstream.id in seen:
                    continue
                seen.add(upstream.id)
                relation = self.graph.parent_of(upstream)
                other = (
                    relation_grain(self.graph, relation, self.holders, entities)
                    if relation is not None
                    else None
                )
                if other is not None and other != grain:
                    return self.graph.label(upstream)
                if not _AGGREGATE.search(str(upstream.attrs.get('expression') or '')):
                    pending.append(upstream)
        return None

    def holding(self, metric: Metric) -> Node | None:
        """Of the columns that hold the metric, the one on a relation holding its grain with a key
        shown unique, else on any relation holding an entity; a mart before its staging model."""
        nodes = [self.graph.nodes[c.id] for c in metric.columns if c.id and c.id in self.graph.nodes]

        def rank(column: Node) -> tuple[int, bool, int, str]:
            relation = self.graph.parent_of(column)
            if relation is None or relation.kind not in RELATION_KINDS:
                return (9, True, 0, '')
            held = self.holders.get(relation.id)
            key = self.key_in(held, relation) if held is not None else None
            shown = key is not None and key_state(self.graph, self.ontology, key) not in (
                None,
                ('inferred', 'failed'),
            )
            place = 0 if held == metric.entity and shown else 1 if shown else 2 if held else 3
            label = self.graph.label(relation)
            return (place, bool(STAGED.match(relation.name)), len(label), label)

        nodes = [n for n in nodes if rank(n)[0] < 9]
        return min(nodes, key=rank) if nodes else None

    def key_in(self, name: str, relation: Node) -> Node | None:
        found = self.entities.get(name)
        ids = found.key_ids if found is not None else []
        return next((self.graph.nodes[i] for i in ids if self.graph.nodes[i].parent == relation.id), None)

    def labelled(self, column: exp.Column) -> Node | None:
        label = '.'.join(p.name for p in column.parts)
        return next((n for n in self.graph.resolve(label) if n.kind == 'column'), None)

    def formula(self, metric: Metric) -> tuple[Node, exp.Expr] | None:
        """The relation the metric's formula aggregates the rows of, and the formula."""
        try:
            tree = sqlglot.parse_one(str(metric.formula), read=self.read)
        except PARSE_ERRORS:
            self.refuse(
                'metric', f'the formula of {metric.name} ({metric.formula}) is not SQL Konstruct reads'
            )
            return None
        nodes = [self.labelled(c) for c in tree.find_all(exp.Column)]
        relations = {n.parent for n in nodes if n is not None}
        if not nodes or None in nodes or len(relations) != 1 or tree.find(exp.AggFunc) is None:
            self.refuse(
                'metric',
                f'the formula of {metric.name} ({metric.formula}) is not an aggregate over the columns of one relation',
            )
            return None
        relation = self.graph.nodes.get(str(next(iter(relations))))
        return (relation, tree) if relation is not None else None

    def grain_fact(self, metric: Metric, relation: Node) -> None:
        label = self.graph.label(relation)
        evidence = [f'{metric.source}: {e}' for e in metric.evidence[:3]]
        if metric.status:
            evidence.insert(0, f'{metric.status}' + (f' ({metric.path})' if metric.path else ''))
        if metric.proposed:
            evidence.insert(0, 'proposed from the code: no one has defined or approved it')
        self.fact('metric', metric.name, metric.confidence, None, evidence or [metric.source])
        if self.column is not None:
            column = next((c for c in metric.columns if c.id == self.column.id), None)
            lineage = (
                [f'konstruct verify confirmed {column.verified} of its {column.edges} lineage edges']
                if column is not None and column.edges
                else []
            )
            whole = column is not None and column.edges and column.verified == column.edges
            self.fact(
                'column',
                self.graph.label(self.column),
                'verified' if whole else 'inferred' if metric.proposed else 'declared',
                None,
                [f'holds {metric.name} for each row of {label}', *lineage],
            )
        if self.entity is None and self.column is not None:
            if metric.entity is None:
                self.refuse(
                    'metric',
                    f'{label} holds no entity and {metric.name} states no grain, so a sum of its rows '
                    'could count one instance more than once',
                )
            else:
                why = f'{metric.name} states it, but {label} holds no key of {metric.entity}'
                self.fact('grain', f'{label}: one row per {metric.entity}', 'inferred', None, [why])
        if self.entity is None:
            return
        key = self.key_in(self.entity, relation) if relation.id in self.holders else None
        state = key_state(self.graph, self.ontology, key) if key is not None else None
        if key is None:
            confidence, check, why = 'inferred', None, f'{label} groups by one column named for {self.entity}'
        elif state is None:
            confidence, check = 'inferred', None
            why = f'{self.graph.label(key)} identifies {self.entity}, but nothing shows it unique'
        else:
            confidence, check = state
            why = f'{self.graph.label(key)} is the key of {self.entity}'
        self.fact('grain', f'{label}: one row per {self.entity}', confidence, check, [why])
        if check == 'failed':
            self.refuse(
                'metric',
                f'konstruct verify found {self.graph.label(key) if key else label} repeated or null, so '
                f'{label} may hold more than one row per {self.entity}',
            )

    def build(self, metric: Metric, value: exp.Expr) -> None:
        query = self.query
        if self.joins is None:
            return
        groups: list[tuple[str, exp.Expr]] = []
        names: set[str] = set()
        time: exp.Column | None = None
        if query.time:
            time = self.prop(query.time, f'time {query.time}')
        for item in query.by:
            text, _, grain = item.partition(':')
            text, grain = text.strip(), grain.strip().lower()
            if not grain and text.lower() in GRAINS:
                grain, text = text.lower(), ''
            if grain and grain not in GRAINS:
                self.refuse(f'by {item}', f'{grain!r} is no time grain: use {", ".join(GRAINS)}')
                continue
            if text:
                column = self.prop(text, f'by {item}')
            else:
                column = time = time or self.time_column(f'by {item}')
            if column is None:
                continue
            if grain:
                expression: exp.Expr = self.trunc(column, grain)
                name = f'{column.name}_{grain}'
            else:
                expression, name = column, column.name
            if name in names:
                name = f'{column.table}_{name}'
            names.add(name)
            groups.append((name, expression))
        conditions: list[exp.Expr] = []
        for text in query.where:
            made = self.condition(
                text, f'where {text}', lambda c, item: self.prop(self.dotted(c), item), self.write
            )
            if made is not None:
                conditions.append(made)
                self.drops_unmatched(text, made)
        bounds = [(day, op) for day, op in ((query.since, exp.GTE), (query.until, exp.LT)) if day]
        for day, _ in bounds:
            if _date(day) is None:
                self.refuse('time range', f'{day!r} is not a date: write YYYY-MM-DD')
        if bounds and not self.result.refused:
            # The range is on the metric's own time property, whichever date the query groups by.
            dated = time or self.time_column('time range')
            for day, op in bounds:
                if dated is not None:
                    bound = op(this=dated.copy(), expression=self.literal(dated, str(_date(day))))
                    conditions.append(bound)
                    self.drops_unmatched(f'{"since" if op is exp.GTE else "until"} {day}', bound)
        self.required_filters(metric, conditions)
        if self.result.refused:
            return
        alias = re.sub(r'\W+', '_', metric.name.lower()).strip('_') or 'value'
        if self.result.aggregation == 'sum':
            if self.whole([e for _, e in groups]):
                self.result.aggregation = 'none'
            elif metric.additive:
                value = exp.Sum(this=value)
            else:
                self.not_additive(metric)
                return
        select = exp.select(*[exp.alias_(e, n) for n, e in groups], exp.alias_(value, alias))
        root = self.joins.table(self.joins.root.relation)
        if root is None:
            return
        select = select.from_(root.as_(self.joins.root.alias))
        for join in self.joins.joins:
            select = select.join(join)
        if conditions:
            select = select.where(exp.and_(*conditions))
        if groups and self.result.aggregation != 'none':
            select = select.group_by(*[e.copy() for _, e in groups])
        if groups:
            select = select.order_by(*[e.copy() for _, e in groups])
        self.result.sql = select.sql(dialect=self.write, pretty=True)

    def drops_unmatched(self, text: str, condition: exp.Expr) -> None:
        """Flag a condition on a property of a joined relation that is not true of the NULL a LEFT
        JOIN gives a row of the metric that finds no row there: in the WHERE clause, it leaves that
        row out. A where, a filter the metric's definition requires or the range of a --since or
        --until on a joined time property can each be one."""
        root = self.joins.root.alias if self.joins is not None else None
        joined = sorted({c.table for c in condition.find_all(exp.Column) if c.table and c.table != root})
        # One that holds of a missing value (``region IS NULL``) keeps those rows.
        if joined and not _can_hold(condition, set(joined))[0]:
            self.result.flags.append(
                f"{text} is on {', '.join(joined)}, joined to the metric's rows: a row that finds no "
                f'row there is left out, as the condition does not hold of a missing value'
            )

    def required_filters(self, metric: Metric, conditions: list[exp.Expr]) -> None:
        """Every filter the metric's definition requires: in the WHERE clause, on the query's
        relations, else as applied by the SQL of the metric's column; refused otherwise."""
        where = f'required by {metric.name}' + (f' ({metric.path})' if metric.path else '')
        for text in metric.filters:
            mark = self.mark()
            made = self.condition(
                text, f'filter {text}', lambda c, item: self.required(c, metric, item), self.read
            )
            if made is not None:
                conditions.append(made)
                self.drops_unmatched(text, made)
                self.result.filters.append(RequiredFilter(text, made.sql(dialect=self.write), where))
                self.fact('filter', text, metric.confidence, None, [where])
                continue
            upstream = self.on_route(metric).get(text)
            if upstream is None:
                continue
            # The column holds an aggregate of rows its own SQL filters as required, as Konstruct
            # reads that SQL: a reading, not a statement, so it is inferred.
            self.restore(mark)
            self.result.filters.append(RequiredFilter(text, None, where, upstream))
            self.fact(
                'filter',
                text,
                'inferred',
                None,
                [f"applied by {upstream}, on a query the metric's column is computed through"],
            )

    def literal(self, column: exp.Column, day: str) -> exp.Expr:
        """``day`` as a value of the time property's own type, so a timestamp compares with a
        timestamp: BigQuery compares a TIMESTAMP with nothing else."""
        kind = self.time_type(column)
        return exp.cast(exp.Literal.string(day), kind or exp.DataType.build('DATE'))

    def time_type(self, column: exp.Column) -> exp.DataType | None:
        node = self.column_node(column)
        if node is None or not node.data_type or not _TIME_TYPE.match(node.data_type.strip()):
            return None
        try:
            return exp.DataType.build(node.data_type, dialect=self.read)
        except _TYPE_ERRORS:
            return None

    def column_node(self, column: exp.Column) -> Node | None:
        if self.joins is None:
            return None
        at = next((a for a in self.joins.at if a.alias == column.table), None)
        return self.graph.column(at.relation.id, column.name) if at is not None else None

    def trunc(self, column: exp.Column, grain: str) -> exp.Expr:
        """``column`` truncated to ``grain`` in the written dialect: weeks start on Monday, and a
        timestamp is truncated as a timestamp."""
        unit = exp.var(_WEEK.get(self.write or '', 'WEEK') if grain == 'week' else grain.upper())
        kind = self.time_type(column)
        dated = kind is None or kind.this == exp.DType.DATE
        if self.write == 'bigquery' and not dated and kind is not None and kind.this != exp.DType.TIMESTAMPTZ:
            return exp.DatetimeTrunc(this=column.copy(), unit=unit)
        if dated and self.write != 'tsql':
            return exp.DateTrunc(this=column.copy(), unit=unit)  # type: ignore[no-untyped-call]
        return exp.TimestampTrunc(this=column.copy(), unit=unit)

    def dotted(self, column: exp.Column) -> str:
        return '.'.join(p.name for p in column.parts)

    def whole(self, groups: list[exp.Expr]) -> bool:
        """Whether the groups keep one row per row of the metric's relation: one is a column of
        that relation shown unique and never null there, recorded as a fact the SQL rests on."""
        if self.joins is None:
            return False
        root = self.joins.root
        for group in groups:
            if not isinstance(group, exp.Column) or group.table != root.alias:
                continue
            column = self.graph.column(root.relation.id, group.name)
            if column is not None and self.not_null(column) and self.key_fact(column) is None:
                return True
        return False

    def not_null(self, column: Node) -> bool:
        """Whether a column is shown never null: a not_null test on every row that fails on any, a
        primary key something enforces, or ``konstruct verify`` finding it unique and never null."""
        if key_state(self.graph, self.ontology, column) == ('verified', 'confirmed'):
            return True
        relation = self.graph.parent_of(column)
        # A primary key something enforces is never null; one nothing enforces says nothing.
        if relation is not None and column.name.lower() in enforced_key(self.graph, relation):
            return True
        if column.attrs.get('ossie_key'):
            return True
        for edge in self.graph.in_edges(column.id, 'tests'):
            test = self.graph.nodes.get(edge.source)
            if (
                test is not None
                and test.attrs.get('test') == 'not_null'
                and not test.attrs.get('where')
                and fails_on_any_row(test)
            ):
                return True
        return False

    def not_additive(self, metric: Metric) -> None:
        held = self.entity or 'row'
        if metric.aggregation == 'average':
            why = f'{metric.name} is an average for each {held}, and averages do not add up across {held}s'
        elif metric.aggregation == 'count':
            why = f'{metric.name} is a distinct count for each {held}, and those do not add up across {held}s'
        elif metric.aggregation:
            why = f'{metric.name} is a {metric.aggregation} for each {held}, which does not add up across {held}s'
        else:
            why = (
                f'{metric.name} does not say how its values add up across {held}s (its formula has no SUM '
                'or COUNT, so it could be a price or a balance)'
            )
        self.refuse('metric', f'{why}: group by {held}, or ask for a metric that aggregates its source rows')

    def time_column(self, item: str) -> exp.Column | None:
        """The metric's time property: the one date or time column of its relation."""
        if self.joins is None:
            return None
        root = self.joins.root
        found = [
            c
            for c in self.graph.columns_of(root.relation.id)
            if (c.data_type and _TIME_TYPE.match(c.data_type.strip()))
            or (not c.data_type and _TIME_NAME.search(c.name))
        ]
        if len(found) == 1:
            return self.ref(root, found[0])
        label = self.graph.label(root.relation)
        if found:
            names = ', '.join(c.name for c in found)
            self.refuse(
                item,
                f'{label} has several date or time columns ({names}): name one as the time property, '
                f'or group by {found[0].name}:month',
            )
        else:
            self.refuse(item, f'{label} has no date or time column: name the property that dates the metric')
        return None

    def required(self, column: exp.Column, metric: Metric, item: str) -> exp.Column | None:
        """A column of a required filter, on the query's relations: by its label, else on a
        relation of the metric's own columns."""
        if column.table:
            node = self.labelled(column)
        else:
            relations = [
                p for c in metric.columns if c.id in self.graph.nodes and (p := self.graph.nodes[c.id].parent)
            ]
            if self.joins is not None:
                relations.insert(0, self.joins.root.relation.id)
            node = next((n for r in relations if (n := self.graph.column(r, column.name)) is not None), None)
        if node is None:
            self.refuse(item, f'the filter reads {self.dotted(column)}, which the project does not have')
            return None
        return self.place(node, item)

    def on_route(self, metric: Metric) -> dict[str, str]:
        """The required filters the SQL of the metric's column applies, on the queries it is
        computed through, with the relations that apply them."""
        if self.column is None:
            return {}
        if self.applied is None:
            from konstruct.analysis.checksql import Checker

            self.applied = Checker(self.graph).applied_on_route(metric.name, self.column)
        return self.applied

    def finish(self) -> None:
        result = self.result
        result.facts = list(self.facts.values())
        if result.facts:
            result.confidence = max((f.confidence for f in result.facts), key=lambda c: _ORDER.get(c, 9))
        for item in result.facts:
            if item.confidence == 'inferred':
                why = f': {item.evidence[0]}' if item.evidence else ''
                result.flags.append(f'the {item.kind} {item.name} is only inferred{why}')
        if result.refused:
            result.sql = None


def _can_hold(tree: exp.Expr, missing: set[str]) -> tuple[bool, bool]:
    """Whether ``tree`` can be true, and whether it can be false, where every column of the aliases
    in ``missing`` is NULL and any other column is any value: a comparison with a NULL is neither,
    ``IS NULL`` of one is true. What it cannot tell, it takes as able to be either."""
    if isinstance(tree, exp.Paren):
        return _can_hold(tree.this, missing)
    if isinstance(tree, exp.Not):
        true, false = _can_hold(tree.this, missing)
        return false, true
    if isinstance(tree, exp.And | exp.Or):
        left, right = _can_hold(tree.this, missing), _can_hold(tree.expression, missing)
        if isinstance(tree, exp.And):
            return left[0] and right[0], left[1] or right[1]
        return left[0] or right[0], left[1] and right[1]

    def null(item: exp.Expr | None) -> bool:
        item = item.unnest() if isinstance(item, exp.Expr) else item
        return isinstance(item, exp.Null) or (isinstance(item, exp.Column) and item.table in missing)

    if isinstance(tree, exp.Is) and isinstance(tree.expression, exp.Null):
        if null(tree.this):
            return True, False
        return (False, True) if isinstance(tree.this, exp.Literal) else (True, True)
    if isinstance(tree, exp.Predicate) and any(null(tree.args.get(k)) for k in ('this', 'expression')):
        return False, False  # NULL, so neither true nor false
    return True, True


def _predicate(tree: exp.Expr) -> bool:
    """Whether ``tree`` compares something: a comparison, IN, BETWEEN, LIKE or IS, or a NOT,
    AND or OR of them; a bare column or value is not a condition on a property."""
    if isinstance(tree, exp.Paren | exp.Not):
        return _predicate(tree.this)
    if isinstance(tree, exp.Connector):
        return _predicate(tree.left) and _predicate(tree.right)
    return isinstance(tree, exp.Predicate)


def _date(text: str) -> str | None:
    try:
        return datetime.date.fromisoformat(text.strip()).isoformat()
    except ValueError:
        return None
