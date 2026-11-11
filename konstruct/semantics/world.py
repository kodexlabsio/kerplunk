"""The ontology as a model of the business that people and agents work through: one entity with
its properties, relationships and metrics; one metric with its definition, formula and grain;
and how two entities connect, with the joins that connect them.

The questions are answered from :func:`konstruct.semantics.ontology.derive`, so every fact keeps
the confidence and evidence the ontology gives it: ``verified``, ``declared`` or ``inferred``,
never more than its evidence supports.

Metrics come from three places:

- the ontology's measures: terms in ``definitions.yml``, a vocabulary, an Apache Ossie model or an
  ontology pack;
- the metrics of dbt's semantic layer (MetricFlow), and the measures of Looker views and Power BI
  semantic models, which people define there;
- **proposals** that Konstruct reads from the code itself where nothing defines a metric: a column
  whose SQL aggregates with ``SUM``, ``COUNT`` or ``AVG`` in a relation with a grain, a numeric
  column of an entity's relation, named as a measure, that holds one value per row rather than an
  aggregate or a value repeated from a coarser grain, and the count of each entity. A proposal is
  always ``inferred`` and has no status: nothing has defined, agreed or approved it. A minimum, a
  maximum, a median, a rate, a share and a key are never proposed.

A metric's grain and entity are read from the relation that holds it unless its definition states
the grain, and ``grain_confidence`` says which.
"""

from __future__ import annotations

import copy
import heapq
import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import sqlglot
from sqlglot import exp

from konstruct.core.graph import RELATION_KINDS, Graph, Node
from konstruct.core.sql import PARSE_ERRORS, aggregate_sql
from konstruct.semantics import definitions
from konstruct.semantics.ontology import (
    CONFIDENCES,
    Entity,
    Measure,
    MeasureColumn,
    Ontology,
    Relationship,
    compact,
    derive,
    entity_name,
    id_like,
    matcher,
    measure_column,
)

if TYPE_CHECKING:
    from konstruct.analysis.explain import Explainer
    from konstruct.analysis.query import QueryError
    from konstruct.interfaces.layout import Style

# Words that name a measure in a column (``amount``, ``unit_price``, ``number_of_orders``).
_MEASURE_WORDS = re.compile(
    r'(?:^|_)(?:amount|amt|revenue|sales|price|cost|costs|total|subtotal|quantity|qty|value|balance|fee|'
    r'fees|tax|discount|spend|profit|margin|income|payment|payments|count|number|num|units|weight|'
    r'duration|minutes|seconds|hours|days|points|score)(?:_|$)',
    re.IGNORECASE,
)
# Values that are not added up: a rate, a share, a ratio, an average or an extreme.
_NOT_ADDED = re.compile(
    r'(?:^|_)(?:rate|ratio|pct|percent|percentage|share|avg|average|mean|median|min|max|first|last|'
    r'latest|earliest|rank|score|index|year|month|day|date|time|at|on|flag|is|has|largest|smallest|'
    r'highest|lowest|typical)(?:_|$)',
    re.IGNORECASE,
)
# Values that summing across rows does not give: a price is per unit, a balance is a snapshot.
_NOT_SUMMED = re.compile(r'(?:^|_)(?:price|prices|balance|balances|per|unit)(?:_|$)', re.IGNORECASE)
_STAGED = re.compile(r'^(?:stg|staging|base|src|raw|int)_', re.IGNORECASE)
_NUMERIC = re.compile(
    r'^(?:tiny|small|big|huge|u)?int|^integer|^decimal|^numeric|^number|^float|^double|^real|^money|^bigint',
    re.IGNORECASE,
)
_AGGREGATES: dict[type[exp.Expr], tuple[str, bool]] = {
    exp.Sum: ('sum', True),
    exp.Count: ('count', True),
    exp.Avg: ('average', False),
}
"""The aggregates a proposal is made from, with whether what they give adds up across groups."""
_TOOLS = {'looker': ('looker', 'Looker view'), 'powerbi': ('power bi', 'Power BI table')}
"""Konstruct's own BI inputs, whose measures are read from what people wrote in the tool."""
_PENALTY = {'verified': 0, 'declared': 1, 'inferred': 2}


@dataclass
class Property:
    """A column that describes an entity, gathered across the relations that hold it."""

    name: str
    """The column's name, lower case (``first_name``)."""
    columns: list[str] = field(default_factory=list)
    """The columns of that name in the entity's relations, by label."""
    description: str | None = None
    """The first description among those columns."""
    data_type: str | None = None
    """The type of the first column with one."""
    terms: list[str] = field(default_factory=list)
    """The business terms bound to any of the columns."""
    refers_to: str | None = None
    """The entity it refers to, when it is the referring column of a relationship."""


@dataclass
class Metric:
    """A business measure: what it measures, how it is computed, at what grain, on whose authority
    and how far to trust it."""

    name: str
    """Its name (``Customer lifetime value``, or for a proposal one made from its column)."""
    source: str
    """Where it comes from: ``definitions``, ``vocabulary``, ``ossie``, ``ontology pack``,
    ``dbt semantic layer``, ``looker``, ``power bi`` or the name of the plugin input that read it
    when something defines it; ``aggregate``, ``measure column`` or ``entity count`` for a
    proposal Konstruct read from the code."""
    confidence: str
    """``verified``, ``declared`` or ``inferred``; a proposal is always ``inferred``."""
    proposed: bool = False
    """True when nothing defines it and Konstruct proposes it from the code: it has no status and
    is not approved."""
    synonyms: list[str] = field(default_factory=list)
    """Other names it goes by."""
    definition: str | None = None
    """The agreed wording, or the description where it is defined."""
    status: str | None = None
    """``draft``, ``agreed``, ``approved`` or ``retired`` for a business term; None for anything
    else, a proposal included."""
    owner: str | None = None
    """Who owns the definition."""
    approved: str | None = None
    """When it was approved."""
    entity: str | None = None
    """The entity it measures, from its grain."""
    grain: str | None = None
    """Its grain (``one row per customer``)."""
    grain_confidence: str | None = None
    """``declared`` when its definition states the grain, ``inferred`` when Konstruct read the
    grain and the entity from the relation that holds it; None without a grain."""
    unit: str | None = None
    """Its unit, such as ``currency``."""
    currency: str | None = None
    """Its currency, for a monetary measure."""
    filters: list[str] = field(default_factory=list)
    """Filters any answer must apply, as its definition writes them."""
    formula: str | None = None
    """Its formula as written where it is defined, or composed down to source columns from SQL;
    left out when its columns compute it in different ways or the tool's aggregation has no exact
    SQL."""
    aggregation: str | None = None
    """``sum``, ``count`` or ``average`` when its formula aggregates, as far as it can be read."""
    additive: bool | None = None
    """Whether its values add up across groups: true for a sum or a count, false for an average,
    a distinct count or a measure its definition makes non-additive; None when it cannot be said,
    as for a price or a balance."""
    columns: list[MeasureColumn] = field(default_factory=list)
    """The columns that hold it (or, for a metric defined over a model, that it reads), each
    with its formula and how many of its lineage edges ``konstruct verify`` confirmed."""
    evidence: list[str] = field(default_factory=list)
    """Where it comes from and what checked it."""
    path: str | None = None
    """The file that defines it, with the line where known."""

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in asdict(self).items() if k != 'columns'}
        return {
            **{k: v for k, v in data.items() if v not in (None, [], '') or k == 'proposed'},
            'columns': [compact(asdict(c)) for c in self.columns],
        }


@dataclass
class Metrics:
    """The metrics a name finds, or every metric."""

    query: str | None
    """The name asked for, or None for every metric."""
    metrics: list[Metric] = field(default_factory=list)
    """Defined metrics first, then proposals, each in name order; at most the limit asked for."""
    total: int = 0
    """How many metrics were found, before any limit."""

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import metrics_lines, show

        return show(metrics_lines, self, style)

    def __str__(self) -> str:
        return self.text()

    def to_dict(self) -> dict[str, Any]:
        return {'query': self.query, 'total': self.total, 'metrics': [m.to_dict() for m in self.metrics]}


@dataclass
class EntityDetail:
    """One entity of the business: how it is identified, what describes it, how it relates to
    the others and what is measured about it."""

    entity: Entity
    """The entity, with its key, relations, confidence and evidence."""
    properties: list[Property] = field(default_factory=list)
    """The columns that describe it, across the relations holding it, keys left out."""
    relationships: list[Relationship] = field(default_factory=list)
    """The relationships it takes part in, either side."""
    metrics: list[Metric] = field(default_factory=list)
    """The metrics whose grain is this entity, proposals included."""

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import entity_lines, show

        return show(entity_lines, self, style)

    def __str__(self) -> str:
        return self.text()

    def to_dict(self) -> dict[str, Any]:
        return {
            **compact(asdict(self.entity)),
            'properties': [compact(asdict(p)) for p in self.properties],
            'relationships': [compact(asdict(r)) for r in self.relationships],
            'metrics': [m.to_dict() for m in self.metrics],
        }


@dataclass
class Hop:
    """One relationship on the way from one entity to another, in the direction it is walked."""

    start: str
    """The entity walked from."""
    end: str
    """The entity walked to."""
    via: str
    """The columns that join them."""
    cardinality: str | None
    """In the direction walked: ``many-to-one`` (each row of ``start`` has one ``end``),
    ``one-to-many``, ``one-to-one``, or None when it is not known, as when ``konstruct verify``
    found the key it refers to repeated."""
    confidence: str
    """The relationship's confidence."""
    check: str | None = None
    """``confirmed`` or ``failed``: what ``konstruct verify`` found of the relationship, with the
    detail in ``evidence``; left out when it did not check it."""
    evidence: list[str] = field(default_factory=list)
    """The tests, joins and checks that show it."""
    same_row: bool = False
    """True for a step along one row of a relation that holds both entities, which joins nothing
    and is no relationship."""


@dataclass
class Connection:
    """How two entities connect: the relationships between them, the cardinality and confidence
    of the whole path, and the joins that follow it."""

    start: str
    """The entity it starts from."""
    end: str
    """The entity it ends at."""
    found: bool
    """Whether the relationships connect them."""
    hops: list[Hop] = field(default_factory=list)
    """The relationships on the way: fewest failed checks first, then fewest relationships, then
    the most trusted."""
    cardinality: str | None = None
    """Of the whole path: ``many-to-one`` when each ``start`` row has at most one ``end``,
    ``one-to-many`` when it can have several (aggregate ``end`` before joining it to ``start``),
    ``many-to-many``, ``one-to-one``, or None when a relationship on the way has no known
    cardinality."""
    confidence: str | None = None
    """The weakest confidence of a relationship on the way: the path is as trustworthy as that."""
    check: str | None = None
    """``failed`` when ``konstruct verify`` found the data contradicting a relationship on the
    way, so the joins can lose or repeat rows; ``confirmed`` when it confirmed every one."""
    join: str | None = None
    """SQL joins that follow the path, from a relation that holds ``start``: the warehouse's
    relation names, each with an alias, joined on the columns of each relationship or, between two
    relations holding one entity, on that entity's keys. None when that cannot be written."""

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import connection_lines, show

        return show(connection_lines, self, style)

    def __str__(self) -> str:
        return self.text()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data['hops'] = [compact(h) for h in data['hops']]
        return {k: v for k, v in data.items() if v is not None}


# Entities


def entity(graph: Graph, name: str, ontology: Ontology | None = None) -> EntityDetail:
    """The entity called ``name`` (or named by the term ``name``), with its properties, the
    relationships it takes part in and the metrics whose grain it is.

    Raises:
        konstruct.QueryError: No entity has that name.
    """
    ontology = ontology or derive(graph)
    found = ontology.entity(name)
    if found is None:
        raise _unknown('entity', name, [e.name for e in ontology.entities])
    # Copies: the ontology may be kept for later questions, so nothing handed out may change it.
    return EntityDetail(
        entity=copy.deepcopy(found),
        properties=_properties(graph, ontology, found),
        relationships=[
            copy.deepcopy(r) for r in ontology.relationships if found.name in (r.source, r.target)
        ],
        metrics=all_metrics(graph, ontology, entity=found.name),
    )


def _properties(graph: Graph, ontology: Ontology, found: Entity) -> list[Property]:
    keys = set(found.key_ids)
    refers = {r.columns[0]: r.target for r in ontology.relationships if r.columns and r.source == found.name}
    relations = []
    for key_id in found.key_ids:
        column = graph.nodes.get(key_id)
        relation = graph.parent_of(column) if column is not None else None
        if relation is not None and relation not in relations:
            relations.append(relation)
    gathered: dict[str, Property] = {}
    for relation in relations:
        for column in graph.columns_of(relation.id):
            if column.id in keys:
                continue
            item = gathered.setdefault(column.name.lower(), Property(column.name.lower()))
            item.columns.append(graph.label(column))
            item.description = item.description or column.description
            item.data_type = item.data_type or column.data_type
            for edge in graph.in_edges(column.id, 'defines'):
                term = graph.nodes.get(edge.source)
                if term is not None and term.name not in item.terms:
                    item.terms.append(term.name)
            item.refers_to = item.refers_to or refers.get(column.id)
    for item in gathered.values():
        item.columns.sort()
        item.terms.sort()
    return [gathered[k] for k in sorted(gathered)]


# Metrics


def metrics(
    graph: Graph, name: str | None = None, ontology: Ontology | None = None, limit: int | None = None
) -> Metrics:
    """Every metric, or those that ``name`` finds: a metric's name or synonym (case, spaces and
    underscores are not told apart, and a plural finds the singular) or a column that holds it
    (``customers.customer_lifetime_value``); at most ``limit`` of them.

    Raises:
        konstruct.QueryError: ``name`` finds no metric.
    """
    ontology = ontology or derive(graph)
    found = all_metrics(graph, ontology, name=name)
    if name is not None and not found:
        raise _unknown('metric', name, [m.name for m in all_metrics(graph, ontology, named=True)])
    shown = found if limit is None else found[: max(limit, 0)]
    return Metrics(name, shown, total=len(found))


def all_metrics(
    graph: Graph,
    ontology: Ontology,
    *,
    entity: str | None = None,
    name: str | None = None,
    named: bool = False,
) -> list[Metric]:
    """The ontology's measures, the metrics people define in dbt's semantic layer, Looker and
    Power BI, and Konstruct's proposals for what nothing defines: with ``entity`` only those whose
    grain is that entity, and with ``name`` only those it names (see :func:`metrics`). Each
    metric's formula is composed only once it is kept; ``named`` keeps every metric but composes
    none, for the names alone."""
    from konstruct.analysis.explain import Explainer

    wanted = name.strip().lower() if name is not None else ''
    named_as = matcher(wanted) if name is not None else None

    def keep(title: str | None, synonyms: list[str], columns: list[str], grain: str | None) -> bool:
        """Whether a metric is asked for; with no title, whether any metric of ``grain`` can be."""
        if entity is not None and grain != entity:
            return False
        if named_as is None or title is None:
            return True
        return any(named_as(n) for n in (title, *synonyms)) or wanted in (c.lower() for c in columns)

    explainer = Explainer(graph)
    holders = _holders(graph, ontology)
    defined = [_from_measure(graph, m) for m in ontology.measures]
    defined = [m for m in defined if keep(m.name, m.synonyms, [c.column for c in m.columns], m.entity)]
    defined += _semantic_layer(graph, explainer, holders, keep, named)
    defined += _bi_measures(graph, explainer, holders, keep, named)
    # Columns a definition already holds a metric in are not proposed again: a term's, a dbt
    # metric's, and the columns a BI measure reads.
    taken = {e.target for e in graph.edges if e.kind == 'defines'}
    for node in graph.nodes.values():
        measures = node.attrs.get('measures') if node.kind == 'exposure' else None
        if isinstance(measures, dict) and measures:
            for edge in graph.in_edges(node.id, 'feeds'):
                if set(edge.attrs.get('fields') or []) & set(measures):
                    taken.add(edge.source)
    for item in graph.meta.get('dbt_metrics') or []:
        model = graph.nodes.get(str(item.get('model') or '')) if isinstance(item, dict) else None
        column = graph.column(model.id, str(item.get('expr') or '')) if model is not None else None
        if column is not None:
            taken.add(column.id)
    proposed = _proposals(graph, ontology, explainer, holders, taken, keep, named)
    order = {c: i for i, c in enumerate(CONFIDENCES)}
    defined.sort(key=lambda m: (m.name.lower(), order.get(m.confidence, 9)))
    proposed.sort(key=lambda m: (m.name.lower(), m.entity or ''))
    return defined + proposed


def _origin(graph: Graph, measure: Measure) -> str:
    term = graph.nodes.get(f'term.{definitions.slug(measure.name)}')
    origins = term.origins if term is not None else []
    if 'registry' in origins:
        return 'ontology pack'
    return next((o for o in ('ossie', 'vocabulary') if o in origins), 'definitions')


def _from_measure(graph: Graph, measure: Measure) -> Metric:
    formulas = {c.formula for c in measure.columns if c.formula}
    formula = measure.expression or (formulas.pop() if len(formulas) == 1 else None)
    aggregation, additive = _aggregation(formula, graph)
    term = graph.nodes.get(f'term.{definitions.slug(measure.name)}')
    synonyms = term.attrs.get('synonyms') if term is not None else None
    source = _origin(graph, measure)
    # A pack writes a grain for the source system's table; that this table has it is a match.
    stated = 'inferred' if source == 'ontology pack' else 'declared'
    return Metric(
        name=measure.name,
        source=source,
        synonyms=[str(s) for s in synonyms] if isinstance(synonyms, list) else [],
        confidence=measure.confidence,
        definition=measure.definition,
        status=measure.status,
        owner=measure.owner,
        approved=measure.approved,
        entity=measure.entity,
        grain=measure.grain,
        grain_confidence=stated if measure.grain else None,
        unit=measure.unit,
        currency=measure.currency,
        filters=list(measure.filters),
        formula=formula,
        aggregation=aggregation,
        additive=additive,
        columns=copy.deepcopy(measure.columns),
        evidence=list(measure.evidence),
        path=measure.path,
    )


def _aggregation(formula: str | None, graph: Graph) -> tuple[str | None, bool | None]:
    """What the formula's outermost function aggregates with, through a COALESCE, a cast or a
    rounding; a sum or count of a CASE (a sum over some rows) is still a sum or a count."""
    if not formula or not re.search(r'\b(?:SUM|COUNT|AVG)\s*\(', formula, re.IGNORECASE):
        return None, None
    dialect = graph.meta.get('dialect')
    try:
        node = sqlglot.parse_one(formula, read=dialect if isinstance(dialect, str) else None)
    except PARSE_ERRORS:
        # A BI tool's formula (LookML's ${TABLE}) is not SQL: read its outermost function alone.
        outer = _OUTER.match(formula.strip())
        if outer is None or not _balanced(outer[2]):
            return None, None
        word = outer[1].upper()
        distinct = word == 'COUNT' and outer[2].lstrip().upper().startswith('DISTINCT ')
        return {'SUM': ('sum', True), 'COUNT': ('count', not distinct), 'AVG': ('average', False)}[word]
    while isinstance(node, exp.Paren | exp.Cast | exp.Round | exp.Alias | exp.Coalesce):
        node = node.this
    found = _AGGREGATES.get(type(node))
    if found is None:
        return None, None
    aggregation, additive = found
    if isinstance(node, exp.Count) and isinstance(node.this, exp.Distinct):
        additive = False  # distinct counts of two groups do not add up to the count of both
    return aggregation, additive


_OUTER = re.compile(r'^(SUM|COUNT|AVG)\s*\((.*)\)$', re.IGNORECASE | re.DOTALL)


def _balanced(text: str) -> bool:
    depth = 0
    for char in text:
        depth += {'(': 1, ')': -1}.get(char, 0)
        if depth < 0:
            return False
    return depth == 0


def _row_level(expression: Any, graph: Graph) -> bool:
    """Whether a column's own SQL computes one value per row: no aggregate and no window, so a
    sum of it over rows means what its name says."""
    if not isinstance(expression, str):
        return True
    dialect = graph.meta.get('dialect')
    try:
        tree = sqlglot.parse_one(expression, read=dialect if isinstance(dialect, str) else None)
    except PARSE_ERRORS:
        return False
    return tree.find(exp.AggFunc, exp.Window) is None


def _holders(graph: Graph, ontology: Ontology) -> dict[str, str]:
    """Each relation that holds one row per instance of an entity, with that entity: the one
    named for the relation when it holds several."""
    held: dict[str, list[str]] = {}
    for item in ontology.entities:
        for key_id in item.key_ids:
            column = graph.nodes.get(key_id)
            if column is not None and column.parent:
                held.setdefault(column.parent, []).append(item.name)
    result = {}
    for relation_id, names in held.items():
        relation = graph.nodes.get(relation_id)
        own = entity_name('id', graph.label(relation)) if relation is not None else None
        result[relation_id] = own if own in names else sorted(names)[0]
    return result


def _grain(graph: Graph, relation: Node, holders: dict[str, str], entities: set[str]) -> str | None:
    """The entity a relation has one row per: the entity it holds, else the entity its one
    grouping column is named for."""
    if relation.id in holders:
        return holders[relation.id]
    grouped = [
        str(c).split(': ', 1)[1]
        for c in relation.attrs.get('conditions') or []
        if str(c).startswith('GROUP BY: ')
    ]
    if len(grouped) == 1 and ',' not in grouped[0]:
        name = entity_name(grouped[0].rsplit('.', 1)[-1])
        if name in entities:
            return name
    return None


def _repeated(graph: Graph, column: Node, grain: str, grains: Callable[[Node], str | None]) -> str | None:
    """The column upstream of ``column`` held at another entity's grain, if any: a value read
    from there is repeated on every row of ``grain`` that joins to it, so summing it double counts."""
    seen = {column.id}
    pending = [column.id]
    while pending:
        for edge in graph.out_edges(pending.pop(), 'derived_from'):
            upstream = graph.nodes.get(edge.target)
            if upstream is None or upstream.id in seen:
                continue
            seen.add(upstream.id)
            relation = graph.parent_of(upstream)
            other = grains(relation) if relation is not None else None
            if other is not None and other != grain:
                return graph.label(upstream)
            pending.append(upstream.id)
    return None


@dataclass
class _Candidate:
    """A proposal before its formula is composed, which is the costly part."""

    metric: Metric
    columns: list[Node] = field(default_factory=list)


def _proposals(
    graph: Graph,
    ontology: Ontology,
    explainer: Explainer,
    holders: dict[str, str],
    taken: set[str],
    keep: Callable[[str | None, list[str], list[str], str | None], bool],
    named: bool,
) -> list[Metric]:
    entities = {e.name for e in ontology.entities}
    keys = {k for e in ontology.entities for k in e.key_ids}
    grains: dict[str, str | None] = {}

    def grain_of(relation: Node) -> str | None:
        if relation.id not in grains:
            grains[relation.id] = _grain(graph, relation, holders, entities)
        return grains[relation.id]

    found: dict[tuple[str, str | None, str], _Candidate] = {}
    for relation in graph.nodes.values():
        if relation.kind not in RELATION_KINDS:
            continue
        grain = grain_of(relation)
        if grain is None or not keep(None, [], [], grain):
            continue
        for column in graph.columns_of(relation.id):
            if (
                column.id in taken
                or column.id in keys
                or id_like(column.name)
                or '.' in column.name
                or column.attrs.get('documented_only')  # in the YAML, not in the model's SQL
            ):
                continue
            name = _proposed_name(column.name, grain)
            if not keep(name, [], [graph.label(column)], grain):
                continue  # before reading its SQL, so a question about one metric reads little
            expression = column.attrs.get('expression')
            # The build inlines CTEs and subqueries, so the column's own SQL shows its aggregate.
            aggregation, additive = _aggregation(expression if isinstance(expression, str) else None, graph)
            note = None
            if aggregation is not None:
                source = 'aggregate'
                # Columns of one grain whose SQL, with CTEs inlined, is the same compute one metric.
                key = (source, grain, str(expression))
            elif (
                relation.id in holders
                and _named_as_measure(column)
                and _row_level(expression, graph)
                and _repeated(graph, column, grain, grain_of) is None
            ):
                source, key = 'measure column', ('measure column', grain, column.name.lower())
                additive = None if _NOT_SUMMED.search(column.name) else True
                if additive is None:
                    note = 'a price or a balance: a sum of it over rows is not a total'
            else:
                continue
            candidate = found.get(key)
            if candidate is None:
                candidate = found[key] = _Candidate(
                    Metric(
                        name=name,
                        source=source,
                        confidence='inferred',
                        proposed=True,
                        definition=column.description,
                        entity=grain,
                        grain=f'one row per {grain}',
                        grain_confidence='inferred',
                        aggregation=aggregation,
                        additive=additive,
                        evidence=[note] if note else [],
                    )
                )
            candidate.columns.append(column)
            candidate.metric.definition = candidate.metric.definition or column.description
    for item in ontology.entities:
        if not keep(None, [], [], item.name):
            continue
        held = [graph.nodes[r] for r, n in holders.items() if n == item.name and r in graph.nodes]
        # Counted where the business reads it: a mart before its staging model.
        held.sort(key=lambda r: (bool(_STAGED.match(r.name)), len(graph.label(r)), graph.label(r)))
        key_column = next(
            (graph.nodes[k] for r in held[:1] for k in item.key_ids if graph.nodes[k].parent == r.id), None
        )
        if key_column is None:
            continue
        found[('entity count', item.name, item.name)] = _Candidate(
            Metric(
                name=f'{item.name.replace("_", " ")} count',
                source='entity count',
                confidence='inferred',
                proposed=True,
                entity=item.name,
                formula=f'COUNT(DISTINCT {graph.label(key_column)})',
                aggregation='count',
                additive=False,
                evidence=[f'the count of instances of the entity {item.name!r}, by its key'],
            )
        )
    result = []
    for candidate in found.values():
        metric = candidate.metric
        labels = sorted(graph.label(c) for c in candidate.columns)
        if not keep(metric.name, [], labels, metric.entity):
            continue
        result.append(metric)
        if not candidate.columns:
            continue
        how = {
            'aggregate': f'aggregates with {(metric.aggregation or "").upper()} in',
            'measure column': 'a numeric column named as a measure, one value per row:',
        }[metric.source]
        metric.evidence = [
            f'proposed from the code: {how} {", ".join(labels)}',
            f'grain from the relation that holds {metric.entity!r}',
            *metric.evidence,
        ]
        if named:
            continue
        metric.columns = sorted(
            (measure_column(graph, explainer, c) for c in candidate.columns), key=lambda c: c.column
        )
        metric.formula = metric.columns[0].formula
    return result


def _named_as_measure(column: Node) -> bool:
    name = column.name
    if not _MEASURE_WORDS.search(name) or _NOT_ADDED.search(name):
        return False
    dtype = column.data_type
    return dtype is None or bool(_NUMERIC.search(dtype.strip()))


def _proposed_name(column: str, grain: str | None) -> str:
    words = column.lower().replace('-', '_').strip('_').replace('_', ' ')
    if grain and ' ' not in words and grain.replace('_', ' ') not in words:
        words = f'{grain.replace("_", " ")} {words}'
    return words


def _semantic_layer(
    graph: Graph,
    explainer: Explainer,
    holders: dict[str, str],
    keep: Callable[[str | None, list[str], list[str], str | None], bool],
    named: bool,
) -> list[Metric]:
    """The metrics dbt's semantic layer defines, each over the model its semantic model reads."""
    entities = set(holders.values())
    result = []
    for item in graph.meta.get('dbt_metrics') or []:
        if not isinstance(item, dict):
            continue
        model = graph.nodes.get(str(item.get('model') or ''))
        agg = str(item.get('agg') or '')
        expr = str(item.get('expr') or '')
        column = graph.column(model.id, expr) if model is not None and expr else None
        grain = _grain(graph, model, holders, entities) if model is not None else None
        title = str(item.get('label') or item.get('name'))
        labels = [graph.label(column)] if column is not None else []
        if not keep(title, [str(item.get('name'))], labels, grain):
            continue
        if agg:
            formula = aggregate_sql(agg, graph.label(column) if column is not None else expr)
        else:
            formula = expr or None  # a derived or ratio metric, as dbt writes its expression
        aggregation, additive = _aggregation(formula, graph)
        evidence = [f'dbt {item.get("type") or "metric"} {item.get("name")!r} ({item.get("path")})']
        if item.get('non_additive'):
            additive = False
            evidence.append(f'not additive across {item["non_additive"]}, as its measure says')
        if agg and formula is None:
            evidence.append(f'its aggregation {agg!r} has no exact SQL without its parameters')
        result.append(
            Metric(
                name=title,
                source='dbt semantic layer',
                synonyms=[str(item['name'])] if item.get('label') else [],
                confidence='declared',
                definition=item.get('description') or None,
                entity=grain,
                grain=f'one row per {grain}' if grain else None,
                grain_confidence='inferred' if grain else None,
                filters=[str(f) for f in item.get('filters') or []],
                formula=formula,
                aggregation=aggregation,
                additive=additive,
                columns=[measure_column(graph, explainer, column)]
                if column is not None and not named
                else [],
                evidence=evidence,
                path=item.get('path'),
            )
        )
    return result


def _bi_measures(
    graph: Graph,
    explainer: Explainer,
    holders: dict[str, str],
    keep: Callable[[str | None, list[str], list[str], str | None], bool],
    named: bool,
) -> list[Metric]:
    """The measures of BI views and models, each with the warehouse columns it reads. Those of
    Konstruct's own Looker and Power BI inputs are declared, as people wrote them in the tool;
    those of a plugin's input are only inferred, since Konstruct cannot tell how it read them."""
    entities = set(holders.values())
    result = []
    for node in graph.nodes.values():
        measures = node.attrs.get('measures') if node.kind == 'exposure' else None
        if not isinstance(measures, dict) or not measures:
            continue
        origin = node.origins[0] if node.origins else 'plugin'
        source, noun = _TOOLS.get(origin, (origin, str(node.attrs.get('type') or 'exposure')))
        filters = node.attrs.get('measure_filters') or {}
        reads: dict[str, list[Node]] = {}
        for edge in graph.in_edges(node.id, 'feeds'):
            column = graph.nodes.get(edge.source)
            if column is None or column.kind != 'column':
                continue
            for field_name in edge.attrs.get('fields') or []:
                reads.setdefault(str(field_name), []).append(column)
        for name, formula in sorted(measures.items()):
            columns = sorted(reads.get(name, []), key=graph.label)
            relations = {c.parent for c in columns if c.parent}
            parent = graph.nodes.get(next(iter(relations))) if len(relations) == 1 else None
            grain = _grain(graph, parent, holders, entities) if parent is not None else None
            if not keep(name, [], [graph.label(c) for c in columns], grain):
                continue
            aggregation, additive = _aggregation(str(formula), graph)
            evidence = [f'{noun} {node.name} ({node.path}), read by the {origin} input']
            result.append(
                Metric(
                    name=name,
                    source=source,
                    confidence='declared' if origin in _TOOLS else 'inferred',
                    entity=grain,
                    grain=f'one row per {grain}' if grain else None,
                    grain_confidence='inferred' if grain else None,
                    filters=[str(filters[name])] if isinstance(filters, dict) and filters.get(name) else [],
                    formula=str(formula) or None,
                    aggregation=aggregation,
                    additive=additive,
                    columns=[] if named else [measure_column(graph, explainer, c) for c in columns],
                    evidence=evidence,
                    path=node.path,
                )
            )
    return result


# Relating two entities


def relate(graph: Graph, start: str, end: str, ontology: Ontology | None = None) -> Connection:
    """How the entities ``start`` and ``end`` connect: the path of relationships between them
    with the fewest failed checks, then the fewest relationships, then the most trusted, where a
    step along one row of a relation holding both entities counts no relationship; its
    cardinality, confidence and check; and the SQL joins that follow it.

    Raises:
        konstruct.QueryError: ``start`` or ``end`` is no entity.
    """
    ontology = ontology or derive(graph)
    names = [e.name for e in ontology.entities]
    first, last = ontology.entity(start), ontology.entity(end)
    if first is None:
        raise _unknown('entity', start, names)
    if last is None:
        raise _unknown('entity', end, names)
    if first.name == last.name:
        return Connection(first.name, last.name, True, cardinality='one-to-one', confidence=first.confidence)
    path = _shortest(ontology, first.name, last.name, _rows(graph, ontology))
    if path is None:
        return Connection(first.name, last.name, False)
    hops = [_hop(r, forward) for r, forward in path]
    order = {c: i for i, c in enumerate(CONFIDENCES)}
    checks = {h.check for h in hops}
    return Connection(
        start=first.name,
        end=last.name,
        found=True,
        hops=hops,
        cardinality=_compose([h.cardinality for h in hops]),
        confidence=max((h.confidence for h in hops), key=lambda c: order.get(c, 9)),
        check='failed' if 'failed' in checks else 'confirmed' if checks == {'confirmed'} else None,
        join=_joins(graph, ontology, path),
    )


class _Row(Relationship):
    """Two entities on one row: a relation that holds one row per instance of each, so each of
    its rows is one of both. A step along it joins nothing, so a path takes it before any
    relationship, at either end or in the middle."""


def _rows(graph: Graph, ontology: Ontology) -> list[Relationship]:
    """A step for each pair of entities whose key columns in one relation are both unique there,
    so each of its rows is one instance of each: by a test, a primary key or an identifier field,
    or as ``konstruct verify`` confirmed. A key column with no such guarantee (a referring column a
    definition also lists, say) gives no step, and one verify found repeated gives a failed one.
    A step is as trusted as the less trusted of its two key columns."""
    recorded = graph.meta.get('integrity')
    found_keys = recorded.get('keys') if isinstance(recorded, dict) else None
    keys: dict[str, Any] = found_keys if isinstance(found_keys, dict) else {}

    def column_state(column: Node) -> tuple[str, str | None] | None:
        """How far the column is a key of its relation, and verify's check of it."""
        found = keys.get(column.id)
        state = found.get('state') if isinstance(found, dict) else None
        if state == 'failed':
            return 'inferred', 'failed'
        if state == 'confirmed':
            return 'verified', 'confirmed'
        return ('declared', None) if column.id in ontology.unique_ids else None

    held: dict[str, list[tuple[Entity, Node]]] = {}
    for item in ontology.entities:
        for key_id in item.key_ids:
            column = graph.nodes.get(key_id)
            if column is not None and column.parent in graph.nodes:
                held.setdefault(column.parent, []).append((item, column))
    order = {c: i for i, c in enumerate(CONFIDENCES)}
    found: dict[tuple[str, str], Relationship] = {}
    for relation_id in sorted(held, key=lambda r: graph.label(graph.nodes[r])):
        label = graph.label(graph.nodes[relation_id])
        entries = sorted(held[relation_id], key=lambda pair: pair[0].name)
        for i, (first, first_key) in enumerate(entries):
            for last, last_key in entries[i + 1 :]:
                states = (column_state(first_key), column_state(last_key))
                if first.name == last.name or states[0] is None or states[1] is None:
                    continue
                checks_found = {states[0][1], states[1][1]}
                check = (
                    'failed'
                    if 'failed' in checks_found
                    else 'confirmed'
                    if checks_found == {'confirmed'}
                    else None
                )
                step = _Row(
                    first.name,
                    last.name,
                    f'one row of {label}',
                    None if check == 'failed' else 'one-to-one',
                    evidence=[
                        f'{label} holds one row per {first.name} ({graph.label(first_key)}) and per '
                        f'{last.name} ({graph.label(last_key)}), both unique there'
                    ],
                    confidence=max(states[0][0], states[1][0], key=lambda c: order.get(c, 9)),
                    check=check,
                    columns=(first_key.id, last_key.id),
                )
                kept = found.get((first.name, last.name))
                rank = (step.check == 'failed', order.get(step.confidence, 9))
                if kept is None or rank < (kept.check == 'failed', order.get(kept.confidence, 9)):
                    found[(first.name, last.name)] = step
    return list(found.values())


def _table_sql(graph: Graph, relation: Node) -> str | None:
    """The relation as the warehouse names it, each part quoted where the dialect needs it."""
    dialect = graph.meta.get('dialect')
    parts = [p for p in str(relation.attrs.get('parts') or relation.relation or '').split('.') if p]
    if not parts:
        return None
    return '.'.join(
        exp.to_identifier(p).sql(dialect=dialect if isinstance(dialect, str) else None) for p in parts
    )


def _alias_base(relation: Node) -> str:
    return re.sub(r'\W', '_', relation.name.lower()) or 't'


def _shortest(
    ontology: Ontology, start: str, end: str, rows: Sequence[Relationship] = ()
) -> list[tuple[Relationship, bool]] | None:
    """From ``start`` to ``end``, the path with the fewest relationships ``konstruct verify``
    found the data contradicting, then the fewest relationships, then the most trusted; each with
    whether it is walked from its referring side (``source``) to the side it refers to. A step
    along one row (``rows``) counts no relationship."""
    steps: dict[str, list[tuple[str, Relationship, bool]]] = {}
    for r in [*ontology.relationships, *rows]:
        steps.setdefault(r.source, []).append((r.target, r, True))
        steps.setdefault(r.target, []).append((r.source, r, False))
    Cost = tuple[int, int, int]
    queue: list[tuple[int, int, int, str, int]] = [(0, 0, 0, start, 0)]
    back: dict[str, tuple[str, Relationship, bool] | None] = {start: None}
    best: dict[str, Cost] = {start: (0, 0, 0)}
    counter = 1
    while queue:
        failed, length, penalty, here, _ = heapq.heappop(queue)
        if here == end:
            break
        if (failed, length, penalty) > best.get(here, (failed, length, penalty)):
            continue
        for there, relationship, forward in steps.get(here, []):
            cost = (
                failed + (relationship.check == 'failed'),
                length + (not isinstance(relationship, _Row)),
                penalty + _PENALTY.get(relationship.confidence, 2),
            )
            if cost < best.get(there, (1 << 30, 0, 0)):
                best[there] = cost
                back[there] = (here, relationship, forward)
                heapq.heappush(queue, (*cost, there, counter))
                counter += 1
    if end not in back:
        return None
    path = []
    here = end
    while (step := back[here]) is not None:
        previous, relationship, forward = step
        path.append((relationship, forward))
        here = previous
    return path[::-1]


def _hop(relationship: Relationship, forward: bool) -> Hop:
    cardinality = relationship.cardinality
    if cardinality == 'many-to-one' and not forward:
        cardinality = 'one-to-many'
    start, end = (
        (relationship.source, relationship.target) if forward else (relationship.target, relationship.source)
    )
    return Hop(
        start,
        end,
        relationship.via,
        cardinality,
        relationship.confidence,
        relationship.check,
        list(relationship.evidence),
        isinstance(relationship, _Row),
    )


def _compose(cardinalities: list[str | None]) -> str | None:
    if any(c is None for c in cardinalities):
        return None
    kinds = {c for c in cardinalities if c != 'one-to-one'}
    if not kinds:
        return 'one-to-one'
    return kinds.pop() if len(kinds) == 1 else 'many-to-many'


def _joins(graph: Graph, ontology: Ontology, path: list[tuple[Relationship, bool]]) -> str | None:
    """``FROM`` and ``JOIN`` lines that follow ``path``, over the warehouse's relation names with
    an alias for each time a relation is joined (``orders``, then ``orders_2``). Each step joins on
    the columns of its relationship; when its column is on another relation holding the entity
    walked from, that relation is joined first on the entity's key in both. Nothing is joined on a
    column's name alone. None only when a relation has no warehouse name."""
    by_name = {e.name: e for e in ontology.entities}
    dialect = graph.meta.get('dialect')
    write = dialect if isinstance(dialect, str) else None
    used: set[str] = set()
    lines: list[str] = []

    def parent(column_id: str) -> Node | None:
        column = graph.nodes.get(column_id)
        return graph.parent_of(column) if column is not None else None

    def key_in(name: str, relation: Node) -> Node | None:
        found = by_name.get(name)
        ids = found.key_ids if found is not None else []
        return next((graph.nodes[i] for i in ids if graph.nodes[i].parent == relation.id), None)

    def alias(relation: Node) -> str:
        base = _alias_base(relation)
        name, n = base, 2
        while name in used:
            name, n = f'{base}_{n}', n + 1
        used.add(name)
        return name

    def ref(name: str, column: Node) -> str:
        return f'{name}.{exp.to_identifier(column.name).sql(dialect=write)}'

    def join(relation: Node, column: Node, other: str) -> str | None:
        """Join ``relation`` on its ``column`` equal to ``other``; the new alias, or None."""
        table = _table_sql(graph, relation)
        if table is None:
            return None
        name = alias(relation)
        lines.append(f'JOIN {table} AS {name} ON {ref(name, column)} = {other}')
        return name

    first, forward = path[0]
    if first.columns is None:
        return None
    start = parent(first.columns[0] if forward else first.columns[1])
    start_table = _table_sql(graph, start) if start is not None else None
    if start is None or start_table is None:
        return None
    current: tuple[Node, str] = (start, alias(start))
    lines.append(f'FROM {start_table} AS {current[1]}')
    for relationship, forward in path:
        if relationship.columns is None:
            return None
        referring_id, key_id = relationship.columns
        here_id, there_id = (referring_id, key_id) if forward else (key_id, referring_id)
        here_column, there_column = graph.nodes.get(here_id), graph.nodes.get(there_id)
        here_relation, there_relation = parent(here_id), parent(there_id)
        if here_column is None or there_column is None or here_relation is None or there_relation is None:
            return None
        if isinstance(relationship, _Row) and here_relation.id == there_relation.id:
            if here_relation.id != current[0].id:
                # The row's relation is not the one joined: join it on the walked entity's key.
                walked = relationship.source if forward else relationship.target
                ours, theirs = key_in(walked, current[0]), key_in(walked, here_relation)
                if ours is None or theirs is None:
                    return None
                name = join(here_relation, theirs, ref(current[1], ours))
                if name is None:
                    return None
                current = (here_relation, name)
            continue  # the same row holds the next entity: nothing more to join
        if here_relation.id != current[0].id:
            # Another relation holding the entity walked from: join it on that entity's key.
            walked = relationship.source if forward else relationship.target
            ours, theirs = key_in(walked, current[0]), key_in(walked, here_relation)
            if ours is None or theirs is None:
                return None
            name = join(here_relation, theirs, ref(current[1], ours))
            if name is None:
                return None
            current = (here_relation, name)
        name = join(there_relation, there_column, ref(current[1], here_column))
        if name is None:
            return None
        current = (there_relation, name)
    return '\n'.join(lines)


def _unknown(kind: str, name: str, names: list[str]) -> QueryError:
    import difflib

    from konstruct.analysis.query import QueryError

    close = difflib.get_close_matches(name.lower(), [n.lower() for n in names], n=3, cutoff=0.5)
    hint = f'; did you mean {", ".join(close)}?' if close else ''
    command = '`konstruct ontology`' if kind == 'entity' else '`konstruct metric`'
    return QueryError(f'no {kind} called {name!r}{hint}', suggestions=close, hint=command)
