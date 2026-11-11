"""`konstruct erasure`: where one person's data ends up, for a request to erase it.

Given a key (a column such as ``customers.customer_id``, an entity such as ``customer``, or a
relation, for its entity's key), the erasure map lists every relation that holds rows of that
person, the columns that hold the key there, how the key got there, the sensitive columns beside
it (:mod:`konstruct.analysis.sensitivity`), what to do with each relation, and the exposures
that show its data. It follows the key:

* **up and down the column lineage**: the column the key is copied from, every copy of it, every
  transformed copy (a cast, a concatenation) and every hashed copy, since a hashed identifier
  still finds the person's rows for anyone who hashes the key; a count of keys, or an aggregate
  over groups of at least *k* distinct keys, does not carry it;
* **across the entity**: every key column of the entity the ontology derives (``customer_id``
  where a ``unique`` test or a primary key makes it the key), in every relation keyed by it;
* **through foreign keys**: every column that refers to one of those keys (a dbt
  ``relationships`` test, a join that matches it to the key, an ontology pack's or an Ossie
  model's relationship), so an order holding ``customer_id`` is one of the person's rows;
* **to the rows the person owns**: the key of a relation whose rows refer to the person (an
  order's ``order_id``) is followed in turn, so a payment of one of those orders is found too.

Each relation's action is ``delete`` for a source, a seed (its rows are in the repository) or a
table nothing in the graph builds, and for a snapshot, which keeps rows its source no longer has; ``full refresh`` for
an incremental model; ``rebuild`` for another built table; and ``none`` for a view, which holds no
rows of its own.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from konstruct.analysis.sensitivity import Settings, _Analysis, joined_columns, materialised, shown
from konstruct.core.graph import RELATION_KINDS, Graph, Node

if TYPE_CHECKING:
    from konstruct.interfaces.layout import Style


@dataclass
class ErasureKey:
    """A column of a relation that holds the person's key, or the key of rows the person owns."""

    column: str
    """The column, by short name."""
    how: str
    """``key`` (the entity's key), ``copy``, ``transformed``, ``hashed`` or ``foreign key``."""
    via: str | None = None
    """The column it was reached from, by short name; None for the key asked about."""
    entity: str | None = None
    """The entity whose instance the value identifies: the person's, or one the person owns."""

    def to_dict(self) -> dict[str, Any]:
        return {'column': self.column, 'how': self.how, 'via': self.via, 'entity': self.entity}


@dataclass
class Holding:
    """A relation that holds rows of the person."""

    relation: str
    """The relation, by short name."""
    relation_id: str
    """Its id."""
    kind: str
    """Its node kind: ``source``, ``model``, ``seed``, ``snapshot`` or ``table``."""
    action: str
    """``delete``, ``rebuild``, ``full refresh`` or ``none`` (see the module documentation)."""
    keys: list[ErasureKey] = field(default_factory=list)
    """The columns holding the key, the first found first."""
    personal: list[str] = field(default_factory=list)
    """Its sensitive columns, by name."""
    level: int = 0
    """0 for rows keyed by the person, 1 for rows of something the person owns, and so on."""
    path: list[str] = field(default_factory=list)
    """The columns from the key asked about to this relation's first key column."""

    def to_dict(self) -> dict[str, Any]:
        return {
            'relation': self.relation,
            'kind': self.kind,
            'action': self.action,
            'keys': [k.to_dict() for k in self.keys],
            'personal': self.personal,
            'level': self.level,
            'path': self.path,
        }


@dataclass
class ErasureMap:
    """Every relation holding one person's rows, by the key that finds them."""

    subject: str
    """The key, entity or relation asked about."""
    key: str
    """The key column the map starts from, by short name."""
    entity: str | None = None
    """The entity the key identifies, when the ontology knows it."""
    holdings: list[Holding] = field(default_factory=list)
    """The relations holding the person's rows, those keyed by the person first."""
    exposures: list[str] = field(default_factory=list)
    """The exposures that read any of those relations."""
    seconds: float = 0.0
    """Time taken."""

    def actions(self) -> dict[str, int]:
        """How many relations take each action."""
        counts: dict[str, int] = {}
        for holding in self.holdings:
            counts[holding.action] = counts.get(holding.action, 0) + 1
        return counts

    def summary(self) -> str:
        """One sentence on what holds the person's data."""
        what = f'a {self.entity}' if self.entity else self.key
        if not self.holdings:
            return f'Nothing holds the rows of {what}.'
        relations = len(self.holdings)
        counts = ', '.join(f'{n} {action}' for action, n in sorted(self.actions().items()))
        hashed = sum(1 for h in self.holdings if any(k.how == 'hashed' for k in h.keys))
        text = f'{relations} relation{" holds" if relations == 1 else "s hold"} the rows of {what} ({counts})'
        if hashed:
            text += f'; {hashed} {"holds" if hashed == 1 else "hold"} the key hashed, found by hashing it'
        if self.exposures:
            count = len(self.exposures)
            text += f'; {count} exposure{" shows" if count == 1 else "s show"} it'
        return text + '.'

    def to_dict(self) -> dict[str, Any]:
        return {
            'subject': self.subject,
            'key': self.key,
            'entity': self.entity,
            'summary': self.summary(),
            'actions': self.actions(),
            'holdings': [h.to_dict() for h in self.holdings],
            'exposures': self.exposures,
            'seconds': round(self.seconds, 3),
        }

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import erasure_lines, show

        return show(erasure_lines, self, style)

    def __str__(self) -> str:
        return self.text()


def _action(graph: Graph, relation: Node) -> str:
    if not materialised(relation):
        return 'none'
    if relation.kind in ('source', 'seed', 'snapshot') or not graph.built(relation):
        return 'delete'
    materialized = str(relation.attrs.get('materialized') or '').lower()
    return 'full refresh' if materialized == 'incremental' else 'rebuild'


@dataclass
class _Found:
    how: str
    via: str | None
    entity: str | None
    level: int


class _Walk:
    def __init__(self, graph: Graph, settings: Settings) -> None:
        from konstruct.semantics.ontology import derive

        self.graph = graph
        self.analysis = _Analysis(graph, settings)
        ontology = derive(graph)
        self.ontology = ontology
        self.key_of: dict[str, str] = {}
        self.keys: dict[str, list[str]] = {}
        for entity in ontology.entities:
            for column_id in entity.key_ids:
                self.key_of.setdefault(column_id, entity.name)
                self.keys.setdefault(entity.name, []).append(column_id)
        self.refers_to: dict[str, set[str]] = {}
        self.referred_by: dict[str, set[str]] = {}
        for relationship in ontology.relationships:
            if relationship.columns is None:
                continue
            many, one = relationship.columns
            self.refers_to.setdefault(many, set()).add(one)
            self.referred_by.setdefault(one, set()).add(many)
        # Columns a join matches to each other: a key joined to a column finds the rows that
        # hold it there, whether or not a test makes the join a relationship.
        self.joined: dict[str, set[str]] = {}
        for node in graph.relations():
            for condition in node.attrs.get('conditions', []) or []:
                for left, right in joined_columns(graph, str(condition)):
                    self.joined.setdefault(left.id, set()).add(right.id)
                    self.joined.setdefault(right.id, set()).add(left.id)
        self.found: dict[str, _Found] = {}
        self.order: list[str] = []

    def entity_of(self, column_id: str) -> str | None:
        if column_id in self.key_of:
            return self.key_of[column_id]
        for target in sorted(self.refers_to.get(column_id, ())):
            if target in self.key_of:
                return self.key_of[target]
        return None

    def walk(self, start: list[str], entity: str | None, level: int) -> None:
        queue: deque[str] = deque()

        def add(column_id: str, how: str, via: str | None) -> None:
            node = self.graph.nodes.get(column_id)
            if node is None or node.kind != 'column' or column_id in self.found:
                return
            self.found[column_id] = _Found(how, via, entity, level)
            self.order.append(column_id)
            queue.append(column_id)

        for column_id in start:
            add(column_id, 'key', None)
        graph = self.graph
        while queue:
            current = queue.popleft()
            node = graph.nodes[current]
            hashed = self.found[current].how == 'hashed'
            upstream = sorted(graph.out_edges(current, 'derived_from'), key=lambda e: e.target)
            for edge in upstream:
                source = graph.nodes.get(edge.target)
                if source is None or self.analysis.classify(node, source)[0] == 'stopped':
                    continue
                # A cast or a trim of one column holds that column's key; of several, such as a
                # concatenation with a country, only the columns it copies are the key.
                if edge.attrs.get('transform') == 'direct' or len(upstream) == 1:
                    add(
                        edge.target,
                        'copy' if edge.attrs.get('transform') == 'direct' else 'transformed',
                        current,
                    )
            for edge in sorted(graph.in_edges(current, 'derived_from'), key=lambda e: e.source):
                column = graph.nodes.get(edge.source)
                if column is None:
                    continue
                kind = self.analysis.classify(column, node)[0]
                if kind == 'stopped' or self.analysis.counted(column, node):
                    # A count of keys holds no key, though it may still be sensitive.
                    continue
                copied = 'copy' if edge.attrs.get('transform') == 'direct' else 'transformed'
                add(column.id, 'hashed' if hashed or kind == 'pseudonymised' else copied, current)
            owner = self.key_of.get(current)
            if owner is not None and owner == entity:
                for key in sorted(self.keys.get(owner, [])):
                    add(key, 'key', current)
            for referring in sorted(self.referred_by.get(current, set()) | self.joined.get(current, set())):
                add(referring, 'foreign key', current)
            for referred in sorted(self.refers_to.get(current, ())):
                if self.key_of.get(referred) == entity:
                    add(referred, 'key', current)


def erasure(graph: Graph, ref: str, settings: Settings | None = None, *, levels: int = 3) -> ErasureMap:
    """The erasure map for ``ref``: a key column, an entity of the ontology (``customer``), or a
    relation, for the key of the entity it holds (see the module documentation). ``levels`` is
    how many steps of owned rows to follow: an order of the person, a payment of that order, and
    so on.

    Raises:
        konstruct.QueryError: ``ref`` matches nothing, several things, or a relation with no key.
    """
    from konstruct.analysis.query import QueryError, resolve_one
    from konstruct.analysis.sensitivity import sensitive_columns

    started = time.perf_counter()
    settings = settings or Settings()
    walk = _Walk(graph, settings)
    named = walk.ontology.entity(ref)
    if named is not None and named.key_ids:
        start = sorted(named.key_ids)
        entity: str | None = named.name
    else:
        node = resolve_one(graph, ref)
        if node.kind in RELATION_KINDS:
            keys = [c.id for c in graph.columns_of(node.id) if c.id in walk.key_of]
            if not keys:
                raise QueryError(
                    f'{graph.label(node)} has no key Konstruct knows; give the key column, such as '
                    f'{graph.label(node)}.id, or add a unique test to it'
                )
            node = graph.nodes[keys[0]]
        elif node.kind != 'column':
            raise QueryError(
                f'{graph.label(node)} is a {node.kind}; erasure takes a key column, an entity or a table'
            )
        start = [node.id]
        entity = walk.entity_of(node.id)
    key = shown(graph, graph.nodes[start[0]])
    walk.walk(start, entity, 0)
    owned: set[str] = {entity} if entity else set()
    for level in range(1, levels + 1):
        # The relations whose rows refer to the person: their own keys find the rows they own.
        seeds: dict[str, list[str]] = {}
        for column_id in list(walk.order):
            found = walk.found[column_id]
            if found.level != level - 1 or found.how not in ('foreign key', 'copy', 'transformed'):
                continue
            parent_id = graph.nodes[column_id].parent
            if parent_id is None or any(
                walk.found[c.id].how == 'key' and walk.found[c.id].entity == found.entity
                for c in graph.columns_of(parent_id)
                if c.id in walk.found
            ):
                continue
            for column in graph.columns_of(parent_id):
                other = walk.key_of.get(column.id)
                if other is not None and other not in owned:
                    seeds.setdefault(other, []).append(column.id)
        if not seeds:
            break
        for other, columns in sorted(seeds.items()):
            owned.add(other)
            walk.walk(sorted(set(columns) | set(walk.keys.get(other, []))), other, level)
    sensitive = sensitive_columns(graph, settings)
    # The person's personal data copied on without the key (a list of email addresses): each
    # copy holds the person's value, found by the value rather than the key.
    reached: dict[str, _Found] = {}
    queue: deque[str] = deque()
    for column_id in list(walk.order):
        parent = graph.nodes[column_id].parent
        for column in graph.columns_of(parent) if parent else []:
            if column.id in sensitive and column.id not in walk.found and column.id not in reached:
                reached[column.id] = walk.found[column_id]
                queue.append(column.id)
    while queue:
        current = queue.popleft()
        source = graph.nodes[current]
        for edge in sorted(graph.in_edges(current, 'derived_from'), key=lambda e: e.source):
            target = graph.nodes.get(edge.source)
            if (
                target is None
                or target.id not in sensitive
                or target.id in walk.found
                or target.id in reached
            ):
                continue
            if walk.analysis.classify(target, source)[0] == 'stopped':
                continue
            base = reached[target.id] = reached[current]
            if not any(c.id in walk.found for c in graph.columns_of(target.parent or '')):
                walk.found[target.id] = _Found('personal data', current, base.entity, base.level)
                walk.order.append(target.id)
            queue.append(target.id)
    holdings: dict[str, Holding] = {}
    for column_id in walk.order:
        node = graph.nodes[column_id]
        relation = graph.parent_of(node)
        if relation is None or relation.kind not in RELATION_KINDS:
            continue
        found = walk.found[column_id]
        holding = holdings.get(relation.id)
        if holding is None:
            holding = holdings[relation.id] = Holding(
                shown(graph, relation),
                relation.id,
                relation.kind,
                _action(graph, relation),
                personal=[c.name for c in graph.columns_of(relation.id) if c.id in sensitive],
                level=found.level,
                path=_path(walk, graph, column_id),
            )
        via = shown(graph, graph.nodes[found.via]) if found.via else None
        holding.keys.append(ErasureKey(shown(graph, node), found.how, via, found.entity))
        holding.level = min(holding.level, found.level)
    result = ErasureMap(ref, key, entity)
    result.holdings = sorted(holdings.values(), key=lambda h: (h.level, h.relation.lower(), h.relation_id))
    exposures = {
        e.target
        for h in result.holdings
        for e in graph.out_edges(h.relation_id, 'feeds')
        if e.target in graph.nodes and graph.nodes[e.target].kind == 'exposure'
    } | {
        e.target
        for h in result.holdings
        for c in graph.columns_of(h.relation_id)
        for e in graph.out_edges(c.id, 'feeds')
        if e.target in graph.nodes and graph.nodes[e.target].kind == 'exposure'
    }
    result.exposures = sorted(shown(graph, graph.nodes[e]) for e in exposures)
    result.seconds = time.perf_counter() - started
    return result


def _path(walk: _Walk, graph: Graph, column_id: str) -> list[str]:
    path: list[str] = []
    current: str | None = column_id
    seen: set[str] = set()
    while current is not None and current not in seen:
        seen.add(current)
        path.append(shown(graph, graph.nodes[current]))
        found = walk.found.get(current)
        current = found.via if found is not None else None
    return path[::-1]
