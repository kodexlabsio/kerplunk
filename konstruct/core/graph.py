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

`downstream_edges()` and `upstream_edges()` hide those directions so callers think only in data
flow.

Walks, shortest paths and name resolution run in the native core (``konstruct._core``) over an
index of the graph, made on first use and again after the graph changes through ``add_node``,
``add_edge`` or ``remove_edge``. Code that edits a node's or an edge's fields in place calls
``changed()``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

from konstruct import _core
from konstruct.core import jsonio

NodeKind = Literal[
    'source', 'table', 'column', 'model', 'seed', 'snapshot', 'dag', 'task', 'test', 'exposure', 'term'
]
EdgeKind = Literal['contains', 'feeds', 'derived_from', 'runs', 'precedes', 'tests', 'defines', 'relates_to']

RELATION_KINDS: frozenset[str] = frozenset({'source', 'table', 'model', 'seed', 'snapshot'})
# When several nodes describe one physical relation (a seed and the dbt source that reads it), SQL
# that names the relation resolves to the first kind in this order.
RELATION_PRIORITY: dict[str, int] = {'source': 0, 'model': 1, 'snapshot': 2, 'seed': 3, 'table': 4}
GRAPH_FORMAT_VERSION = 1

_Index = TypeVar('_Index')
IndexBasis = Literal['graph', 'nodes', 'relations']
"""What an index is made from, so what makes it stale (see ``Graph.indexed``)."""


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
        # For each basis an index can have, a version bumped whenever that part of the graph
        # changes; each cached index records the version it was made from.
        self._versions: dict[IndexBasis, int] = {'graph': 0, 'nodes': 0, 'relations': 0}
        self._indexes: dict[str, tuple[int, Any]] = {}

    # Construction

    def add_node(self, node: Node) -> Node:
        """Add a node, or merge it into an existing node with the same id."""
        existing = self.nodes.get(node.id)
        versions = self._versions
        versions['graph'] += 1
        versions['nodes'] += 1
        if node.kind in RELATION_KINDS or (existing is not None and existing.kind in RELATION_KINDS):
            versions['relations'] += 1
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
        self._versions['graph'] += 1
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
            self._versions['graph'] += 1
            self._out[edge.source].remove(edge)
            self._in[edge.target].remove(edge)

    def changed(self) -> None:
        """Note that nodes or edges were edited in place (a name, a relation key, an edge's
        ``role``), so the indexes queries use are made again."""
        for basis in self._versions:
            self._versions[basis] += 1

    def indexed(self, name: str, build: Callable[[Graph], _Index], *, basis: IndexBasis = 'graph') -> _Index:
        """The index ``name`` that ``build`` makes from the graph, kept until what it is made from
        changes: anything in the graph (``'graph'``), a node (``'nodes'``, for an index that reads
        no edges) or a relation (``'relations'``)."""
        version = self._versions[basis]
        cached = self._indexes.get(name)
        if cached is None or cached[0] != version:
            cached = (version, build(self))
            self._indexes[name] = cached
        return cast(_Index, cached[1])

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
        names = self.indexed('relation names', _RelationNames, basis='relations')
        matches = [names.relations[i] for i in names.core.find(needle)]
        # Nodes for one physical relation (a seed and the source over it) are one answer.
        relations = {n.relation for n in matches}
        if len(matches) > 1 and len(relations) == 1 and None not in relations:
            preferred = self.node_for_relation(str(next(iter(relations))))
            return [preferred] if preferred else matches
        return matches

    # Traversal

    def downstream_edges(self, node_id: str) -> Iterator[tuple[Edge, str]]:
        """Edges along which a change to ``node_id`` propagates, with the node they reach:
        ``feeds`` forwards, and ``derived_from``, ``tests`` and ``runs`` backwards."""
        return self._neighbours(node_id, downstream=True)

    def upstream_edges(self, node_id: str) -> Iterator[tuple[Edge, str]]:
        """Edges along which ``node_id`` depends on something, with the node they reach:
        ``feeds`` and ``runs`` backwards, and ``derived_from`` forwards."""
        return self._neighbours(node_id, downstream=False)

    def _neighbours(self, node_id: str, *, downstream: bool) -> Iterator[tuple[Edge, str]]:
        traversal = self.indexed('traversal', _Traversal)
        for ordinal in traversal.core.neighbours(node_id, downstream):
            edge = traversal.edges[ordinal]
            yield edge, edge.target if edge.source == node_id else edge.source

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
        the tasks that read or run each affected relation are reported too, and so are the tasks
        whose SQL an edge on the way came from (an Airflow SQL task, a Dagster asset). Tests and
        the tasks that run a node are reported, not walked through.
        """
        traversal = self.indexed('traversal', _Traversal)
        return traversal.steps(
            traversal.core.impact(self.expand(node), _native_depth(max_depth), node.kind == 'column')
        )

    def trace(self, node: Node, max_depth: int | None = None) -> list[Step]:
        """Everything upstream of ``node``. For a column, the tasks that build each relation on the
        way (including the column's own) are reported too."""
        traversal = self.indexed('traversal', _Traversal)
        return traversal.steps(
            traversal.core.trace(self.expand(node), _native_depth(max_depth), node.kind == 'column')
        )

    def path(self, start: Node, end: Node) -> list[Step]:
        """Shortest path from ``start`` to ``end``, following data flow in either direction: downstream
        if there is a path that way, else upstream, else either way through ``contains`` and
        ``precedes`` too. Of several paths of the same length, each step takes the neighbour whose
        id sorts first, so the same path is found whatever order the build added the edges in."""
        traversal = self.indexed('traversal', _Traversal)
        return traversal.steps(traversal.core.path(start.id, end.id))

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


def _native_depth(max_depth: int | None) -> int | None:
    """``max_depth`` in the signed 64-bit range the native core takes: any limit beyond it is as
    good as none, and a negative one stops at the start, as 0 does."""
    return None if max_depth is None else min(max(max_depth, 0), 2**63 - 1)


class _Traversal:
    """The graph's edges in the native index, with the nodes and edges its ordinals number."""

    def __init__(self, graph: Graph) -> None:
        self.nodes = list(graph.nodes.values())
        # Insertion order, which is the order of each node's adjacency lists.
        self.edges = list(graph._edges.values())
        references: list[bool] = []
        # The tasks whose SQL each edge came from, as (edge, task) in edge order.
        through: list[tuple[int, str]] = []
        for ordinal, edge in enumerate(self.edges):
            attrs = edge.attrs
            references.append(attrs.get('role') == 'reference')
            if 'via' in attrs or 'also_via' in attrs:
                for task in [attrs.get('via'), *attrs.get('also_via', [])]:
                    if isinstance(task, str):
                        through.append((ordinal, task))
        self.core = _core.GraphIndex(
            list(graph.nodes),
            [n.kind for n in self.nodes],
            [n.parent or None for n in self.nodes],
            [e.source for e in self.edges],
            [e.target for e in self.edges],
            [e.kind for e in self.edges],
            references,
            through,
        )

    def steps(self, visits: list[tuple[int, int, int | None, int | None]]) -> list[Step]:
        nodes, edges, count = self.nodes, self.edges, len(self.nodes)
        return [
            Step(
                nodes[node],
                depth,
                None if via is None else edges[via],
                None
                if previous is None
                else nodes[previous].id
                if previous < count
                else self.core.id(previous),
            )
            for node, depth, via, previous in visits
        ]


class _RelationNames:
    """The graph's relations by every name they answer to (see ``Graph.resolve``)."""

    def __init__(self, graph: Graph) -> None:
        self.relations = list(graph.relations())
        self.core = _core.RelationNames(
            [n.name.lower() for n in self.relations],
            [n.id.lower() for n in self.relations],
            [n.relation for n in self.relations],
            # A source system's table (Fivetran's) is named in full, so the short names of the
            # warehouse table it lands as keep meaning that table.
            [bool(n.attrs.get('outside_warehouse')) for n in self.relations],
        )
