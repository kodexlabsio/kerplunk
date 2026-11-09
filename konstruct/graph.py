"""The Konstruct graph: nodes, edges, traversal and (de)serialisation.

Edge directions
---------------
Every edge is stored as ``source -> target`` with these meanings:

``contains``      relation -> column
``feeds``         upstream -> downstream (relation to relation, task to the table it writes,
                  table to the task that reads it, or column to the relation that filters,
                  joins or groups on it: ``role = "reference"``)
``derived_from``  downstream column -> upstream column
``runs``          DAG -> task, and task -> the dbt node or table it executes
``precedes``      upstream task -> downstream task (Airflow ``>>``)
``tests``         test -> the relation or column it tests
``defines``       business term -> the column (or relation) that holds it
``relates_to``    business term -> a related term

`downstream()` and `upstream()` hide those directions so callers think only in data flow.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from konstruct import jsonio

NodeKind = Literal[
    'source', 'table', 'column', 'model', 'seed', 'snapshot', 'dag', 'task', 'test', 'exposure', 'term'
]
EdgeKind = Literal['contains', 'feeds', 'derived_from', 'runs', 'precedes', 'tests', 'defines', 'relates_to']

RELATION_KINDS: frozenset[str] = frozenset({'source', 'table', 'model', 'seed', 'snapshot'})
# When several nodes describe one physical relation (a seed and the dbt source that reads it), SQL
# that names the relation resolves to the first kind in this order.
RELATION_PRIORITY: dict[str, int] = {'source': 0, 'model': 1, 'snapshot': 2, 'seed': 3, 'table': 4}
GRAPH_FORMAT_VERSION = 1


@dataclass
class Node:
    id: str
    """A unique id, such as ``model.<package>.<name>``, ``table.<relation>``, ``<relation id>#<column>``, ``task.<dag>.<task>``, ``looker.view.<view>``, ``powerbi.table.<model>.<table>`` or ``term.<slug>``."""
    kind: NodeKind
    """What the node is."""
    name: str
    """Its own name."""
    parent: str | None = None
    """For a column, its relation's id."""
    relation: str | None = None
    """For a relation, its lower-case warehouse name, such as ``shop.main.orders``."""
    description: str | None = None
    """Description from dbt, the warehouse or definitions.yml."""
    data_type: str | None = None
    """Column type."""
    path: str | None = None
    """The file that defines it, relative to the project."""
    origins: list[str] = field(default_factory=list)
    """Inputs it came from."""
    attrs: dict[str, Any] = field(default_factory=dict)
    """Everything else, such as expressions, conditions, schedules, usage and term fields."""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {'id': self.id, 'kind': self.kind, 'name': self.name}
        for key in ('parent', 'relation', 'description', 'data_type', 'path'):
            value = getattr(self, key)
            if value:
                data[key] = value
        if self.origins:
            data['origins'] = sorted(self.origins)
        if self.attrs:
            data['attrs'] = self.attrs
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Node:
        return cls(
            id=data['id'],
            kind=data['kind'],
            name=data['name'],
            parent=data.get('parent'),
            relation=data.get('relation'),
            description=data.get('description'),
            data_type=data.get('data_type'),
            path=data.get('path'),
            origins=list(data.get('origins', [])),
            attrs=dict(data.get('attrs', {})),
        )


@dataclass
class Edge:
    source: str
    """Source node id."""
    target: str
    """Target node id."""
    kind: EdgeKind
    """Edge kind; the module docstring gives each direction."""
    attrs: dict[str, Any] = field(default_factory=dict)
    """Details such as ``transform``, ``role``, ``verified`` and ``via``."""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.source, self.target, self.kind)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {'source': self.source, 'target': self.target, 'kind': self.kind}
        if self.attrs:
            data['attrs'] = self.attrs
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Edge:
        return cls(data['source'], data['target'], data['kind'], dict(data.get('attrs', {})))


def column_id(parent_id: str, column: str) -> str:
    return f'{parent_id}#{column.lower()}'


def relation_key(*parts: str | None) -> str:
    """Canonical, case-insensitive key for a warehouse relation, e.g. ``jaffle.main.orders``."""
    cleaned = (part.strip('"`[]').lower() for part in parts if part)
    return '.'.join(part for part in cleaned if part)


@dataclass
class Step:
    """One node reached by a traversal, with the edge that reached it."""

    node: Node
    """The node reached."""
    depth: int
    """Steps from the start."""
    via: Edge | None
    """The edge that reached it."""
    previous: str | None
    """The id of the node it was reached from."""


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self._edges: dict[tuple[str, str, str], Edge] = {}
        self._out: dict[str, list[Edge]] = {}
        self._in: dict[str, list[Edge]] = {}
        self._relations: dict[str, list[str]] = {}
        self.warnings: list[str] = []
        self.meta: dict[str, Any] = {}

    # Construction

    def add_node(self, node: Node) -> Node:
        """Add a node, or merge it into an existing node with the same id."""
        existing = self.nodes.get(node.id)
        if existing is None:
            self.nodes[node.id] = node
            self._index_relation(node)
            return node
        for key in ('parent', 'relation', 'description', 'data_type', 'path'):
            if not getattr(existing, key) and getattr(node, key):
                setattr(existing, key, getattr(node, key))
        for origin in node.origins:
            if origin not in existing.origins:
                existing.origins.append(origin)
        for key, value in node.attrs.items():
            existing.attrs.setdefault(key, value)
        self._index_relation(existing)
        return existing

    def _index_relation(self, node: Node) -> None:
        if node.relation and node.kind in RELATION_KINDS:
            ids = self._relations.setdefault(node.relation, [])
            if node.id not in ids:
                ids.append(node.id)
                ids.sort(key=lambda i: RELATION_PRIORITY.get(self.nodes[i].kind, 9))

    def add_column(
        self,
        parent: Node,
        name: str,
        *,
        data_type: str | None = None,
        description: str | None = None,
        origin: str | None = None,
        attrs: dict[str, Any] | None = None,
    ) -> Node:
        node = self.add_node(
            Node(
                id=column_id(parent.id, name),
                kind='column',
                name=name,
                parent=parent.id,
                data_type=data_type,
                description=description,
                origins=[origin] if origin else [],
                attrs=attrs or {},
            )
        )
        self.add_edge(parent.id, node.id, 'contains')
        return node

    def add_edge(self, source: str, target: str, kind: EdgeKind, **attrs: Any) -> Edge | None:
        """Add an edge, or merge ``attrs`` into an existing one of the same kind between the same nodes. Self-loops are ignored and return None."""
        if source == target:
            return None
        key = (source, target, kind)
        existing = self._edges.get(key)
        if existing is not None:
            for name, value in attrs.items():
                existing.attrs.setdefault(name, value)
            return existing
        edge = Edge(source, target, kind, dict(attrs))
        self._edges[key] = edge
        self._out.setdefault(source, []).append(edge)
        self._in.setdefault(target, []).append(edge)
        return edge

    def remove_edge(self, edge: Edge) -> None:
        if self._edges.pop(edge.key, None) is not None:
            self._out[edge.source].remove(edge)
            self._in[edge.target].remove(edge)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    # Lookup

    @property
    def edges(self) -> list[Edge]:
        return list(self._edges.values())

    def out_edges(self, node_id: str, kind: str | None = None) -> list[Edge]:
        """Edges leaving ``node_id``, optionally only of one kind."""
        return [e for e in self._out.get(node_id, []) if kind is None or e.kind == kind]

    def in_edges(self, node_id: str, kind: str | None = None) -> list[Edge]:
        """Edges arriving at ``node_id``, optionally only of one kind."""
        return [e for e in self._in.get(node_id, []) if kind is None or e.kind == kind]

    def node_for_relation(self, key: str) -> Node | None:
        """The node SQL naming this relation refers to (see RELATION_PRIORITY)."""
        ids = self._relations.get(key.lower())
        return self.nodes[ids[0]] if ids else None

    def nodes_for_relation(self, key: str) -> list[Node]:
        """Every node that describes this relation, the preferred one first."""
        return [self.nodes[i] for i in self._relations.get(key.lower(), [])]

    def shared_relations(self) -> Iterator[tuple[str, list[Node]]]:
        for key, ids in self._relations.items():
            if len(ids) > 1:
                yield key, [self.nodes[i] for i in ids]

    def relations(self) -> Iterator[Node]:
        return (n for n in self.nodes.values() if n.kind in RELATION_KINDS)

    def columns_of(self, node_id: str) -> list[Node]:
        """The columns of a relation, in their order."""
        return [self.nodes[e.target] for e in self.out_edges(node_id, 'contains')]

    def column(self, parent_id: str, name: str) -> Node | None:
        """The column ``name`` of the relation ``parent_id``, or None."""
        return self.nodes.get(column_id(parent_id, name))

    def parent_of(self, node: Node) -> Node | None:
        """A column's relation, or None for anything else."""
        return self.nodes.get(node.parent) if node.parent else None

    def label(self, node: Node) -> str:
        """Short human label: ``orders.amount`` for a column, ``orders`` for a relation."""
        if node.kind == 'column':
            parent = self.parent_of(node)
            return f'{self.label(parent)}.{node.name}' if parent else node.name
        if node.kind == 'source' and node.attrs.get('source_name'):
            return f'{node.attrs["source_name"]}.{node.name}'
        if node.kind == 'task' and node.attrs.get('dag'):
            return f'{node.attrs["dag"]}.{node.name}'
        return node.name

    # Name resolution

    def resolve(self, ref: str) -> list[Node]:
        """Find the nodes a user reference such as ``orders.amount`` or ``stg_orders`` names."""
        if ref in self.nodes:
            return [self.nodes[ref]]
        needle = ref.strip().lower()
        if not needle:
            return []
        relations = self._match_relations(needle)
        if relations:
            return relations
        parts = needle.split('.')
        # Try every split into relation and column, the longest relation first, so that nested
        # columns (orders.customer.country) resolve as well as qualified tables.
        for cut in range(len(parts) - 1, 0, -1):
            rel_part, col_part = '.'.join(parts[:cut]), '.'.join(parts[cut:])
            columns = [
                col
                for rel in self._match_relations(rel_part)
                if (col := self.column(rel.id, col_part)) is not None
            ]
            if columns:
                return columns
        others = [
            n
            for n in self.nodes.values()
            if n.kind not in RELATION_KINDS
            and n.kind != 'column'
            and (
                needle in (n.name.lower(), self.label(n).lower())
                or (n.kind == 'term' and needle in (s.lower() for s in n.attrs.get('synonyms', [])))
            )
        ]
        if others:
            return others
        return [n for n in self.nodes.values() if n.id.lower() == needle]

    def _match_relations(self, needle: str) -> list[Node]:
        matches: list[Node] = []
        for node in self.relations():
            keys = {node.name.lower(), node.id.lower()}
            if node.attrs.get('outside_warehouse'):
                # A source system's table (Fivetran's) is named in full, so the short names of
                # the warehouse table it lands as keep meaning that table.
                if needle in keys:
                    matches.append(node)
                continue
            if node.relation:
                keys.add(node.relation)
            if any(k == needle or k.endswith('.' + needle) for k in keys):
                matches.append(node)
        exact = [n for n in matches if n.name.lower() == needle or n.relation == needle]
        if len(exact) == 1 or (exact and len(matches) > len(exact)):
            matches = exact
        # Nodes for one physical relation (a seed and the source over it) are one answer.
        relations = {n.relation for n in matches}
        if len(matches) > 1 and len(relations) == 1 and None not in relations:
            preferred = self.node_for_relation(str(next(iter(relations))))
            return [preferred] if preferred else matches
        return matches

    # Traversal

    def downstream_edges(self, node_id: str) -> Iterator[tuple[Edge, str]]:
        """Edges along which a change to ``node_id`` propagates, with the node they reach."""
        for edge in self._out.get(node_id, []):
            if edge.kind == 'feeds':
                yield edge, edge.target
        for edge in self._in.get(node_id, []):
            if edge.kind in ('derived_from', 'tests', 'runs'):
                yield edge, edge.source

    def upstream_edges(self, node_id: str) -> Iterator[tuple[Edge, str]]:
        """Edges along which ``node_id`` depends on something, with the node they reach."""
        for edge in self._in.get(node_id, []):
            if edge.kind in ('feeds', 'runs'):
                yield edge, edge.source
        for edge in self._out.get(node_id, []):
            if edge.kind == 'derived_from':
                yield edge, edge.target

    def _walk(
        self,
        start: Iterable[str],
        step: Any,
        max_depth: int | None,
        stop_at_reference: bool,
    ) -> list[Step]:
        seen: dict[str, Step] = {}
        queue: deque[tuple[str, int]] = deque()
        for node_id in start:
            if node_id in self.nodes and node_id not in seen:
                seen[node_id] = Step(self.nodes[node_id], 0, None, None)
                queue.append((node_id, 0))
        while queue:
            node_id, depth = queue.popleft()
            if max_depth is not None and depth >= max_depth:
                continue
            current = seen[node_id]
            if stop_at_reference and current.via is not None and current.via.attrs.get('role') == 'reference':
                continue
            for edge, other in step(node_id):
                if other in seen or other not in self.nodes:
                    continue
                seen[other] = Step(self.nodes[other], depth + 1, edge, node_id)
                # Tests and the tasks that run a node are reported, not walked through.
                if edge.kind not in ('tests', 'runs'):
                    queue.append((other, depth + 1))
        return list(seen.values())

    def expand(self, node: Node) -> list[str]:
        """A relation stands for itself and all its columns; anything else for itself."""
        if node.kind in RELATION_KINDS:
            return [node.id, *(c.id for c in self.columns_of(node.id))]
        return [node.id]

    def impact(self, node: Node, max_depth: int | None = None) -> list[Step]:
        """Everything downstream of ``node``.

        A column reached through a join, filter or group-by (``role = "reference"``) is reported,
        but the walk does not continue from the whole relation it reached, so one join key does
        not mark every downstream column as affected. For a column, the exposures that use and
        the tasks that read or run each affected relation are reported too.
        """
        steps = self._walk(self.expand(node), self.downstream_edges, max_depth, True)
        if node.kind == 'column':
            steps += self._relation_consumers(steps, upstream=False)
        return steps + self._through_tasks(steps)

    def _through_tasks(self, steps: list[Step]) -> list[Step]:
        """Tasks whose SQL an edge in the walk came from (an Airflow SQL task, a Dagster asset):
        they read the changed data, so they are affected too."""
        seen = {s.node.id for s in steps}
        extra: list[Step] = []
        for step in steps:
            if step.via is None:
                continue
            for through in [step.via.attrs.get('via'), *step.via.attrs.get('also_via', [])]:
                if (
                    isinstance(through, str)
                    and through in self.nodes
                    and through not in seen
                    and self.nodes[through].kind == 'task'
                ):
                    seen.add(through)
                    extra.append(Step(self.nodes[through], step.depth, step.via, step.previous))
        return extra

    def trace(self, node: Node, max_depth: int | None = None) -> list[Step]:
        """Everything upstream of ``node``. For a column, the tasks that build each relation on the
        way (including the column's own) are reported too."""
        steps = self._walk(self.expand(node), self.upstream_edges, max_depth, False)
        if node.kind == 'column':
            steps += self._relation_consumers(steps, upstream=True)
        return steps

    def _fed_by_columns_of(self, consumer: str, relation: str) -> bool:
        """Whether ``consumer`` (a Looker view, say) records which of ``relation``'s columns it
        reads, so a column reaches it only through its own edge."""
        return any(
            e.source in self.nodes and self.nodes[e.source].parent == relation
            for e in self.in_edges(consumer, 'feeds')
        )

    def _relation_consumers(self, steps: list[Step], *, upstream: bool) -> list[Step]:
        seen = {s.node.id for s in steps}
        extra: list[Step] = []
        for step in steps:
            column = step.node
            if column.kind != 'column' or not column.parent or (step.depth == 0 and not upstream):
                continue
            parent = column.parent
            candidates: list[tuple[Edge, str]] = [(e, e.source) for e in self.in_edges(parent, 'runs')]
            if upstream:
                candidates += [
                    (e, e.source)
                    for e in self.in_edges(parent, 'feeds')
                    if e.source in self.nodes and self.nodes[e.source].kind == 'task'
                ]
            else:
                candidates += [
                    (e, e.target)
                    for e in self.out_edges(parent, 'feeds')
                    if e.target in self.nodes
                    and self.nodes[e.target].kind in ('exposure', 'task')
                    and not self._fed_by_columns_of(e.target, parent)
                ]
            for edge, other in candidates:
                if other not in seen:
                    seen.add(other)
                    extra.append(Step(self.nodes[other], step.depth + 1, edge, parent))
        return extra

    def path(self, start: Node, end: Node) -> list[Step]:
        """Shortest path from ``start`` to ``end``, following data flow in either direction."""
        for step in (self.downstream_edges, self.upstream_edges):
            found = self._shortest(start.id, end.id, step)
            if found:
                return found

        def either(node_id: str) -> Iterator[tuple[Edge, str]]:
            yield from self.downstream_edges(node_id)
            yield from self.upstream_edges(node_id)
            for edge in self._out.get(node_id, []):
                if edge.kind in ('contains', 'precedes'):
                    yield edge, edge.target
            for edge in self._in.get(node_id, []):
                if edge.kind in ('contains', 'precedes'):
                    yield edge, edge.source

        return self._shortest(start.id, end.id, either)

    def _shortest(self, start: str, end: str, step: Any) -> list[Step]:
        if start == end:
            return [Step(self.nodes[start], 0, None, None)]
        seen: dict[str, Step] = {start: Step(self.nodes[start], 0, None, None)}
        queue: deque[str] = deque([start])
        while queue:
            node_id = queue.popleft()
            # Neighbours in a fixed order, so that of several routes of the same length the same one
            # is found whatever order the build happened to add the edges in.
            for edge, other in sorted(step(node_id), key=lambda pair: (pair[1], pair[0].kind)):
                if other in seen or other not in self.nodes:
                    continue
                seen[other] = Step(self.nodes[other], seen[node_id].depth + 1, edge, node_id)
                if other == end:
                    chain = [seen[end]]
                    while chain[-1].previous is not None:
                        chain.append(seen[chain[-1].previous])
                    return list(reversed(chain))
                queue.append(other)
        return []

    # Serialisation

    def to_dict(self) -> dict[str, Any]:
        """The graph as plain data: ``format_version``, ``meta``, ``nodes``, ``edges`` and ``warnings``."""
        return {
            'format_version': GRAPH_FORMAT_VERSION,
            'meta': self.meta,
            'nodes': [n.to_dict() for n in sorted(self.nodes.values(), key=lambda n: n.id)],
            # Insertion order, which a build makes deterministic, keeps columns in table order.
            'edges': [e.to_dict() for e in self._edges.values()],
            'warnings': self.warnings,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Graph:
        graph = cls()
        graph.meta = dict(data.get('meta', {}))
        for item in data.get('nodes', []):
            graph.add_node(Node.from_dict(item))
        for item in data.get('edges', []):
            edge = Edge.from_dict(item)
            graph.add_edge(edge.source, edge.target, edge.kind, **edge.attrs)
        graph.warnings = list(data.get('warnings', []))
        return graph

    def write(self, path: Path) -> None:
        """Write the graph to ``path`` as JSON (``graph.json``)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(jsonio.dumps(self.to_dict(), indent=True) + '\n', encoding='utf-8')

    @classmethod
    def read(cls, path: Path) -> Graph:
        """Load a graph from a ``graph.json`` file."""
        return cls.from_dict(jsonio.loads(path.read_bytes()))

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for node in self.nodes.values():
            counts[node.kind] = counts.get(node.kind, 0) + 1
        for edge in self._edges.values():
            counts[f'edge:{edge.kind}'] = counts.get(f'edge:{edge.kind}', 0) + 1
        return counts
