"""Questions over a built graph: impact, trace, path, search, describe and free-text query.

Each function returns a typed result. ``str(result)`` is compact text written for coding agents
and people; ``result.to_dict()`` is the same content as plain data, for JSON and MCP.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from konstruct import _core
from konstruct.core.graph import RELATION_KINDS, Graph, Node, Step

if TYPE_CHECKING:
    from konstruct.interfaces.layout import Style


class QueryError(Exception):
    """A reference that matches nothing or several things, or a missing graph.

    ``suggestions`` holds the names the reference may have meant and ``hint`` a command that
    could help, for the CLI to show; the message alone says the same in one line."""

    def __init__(
        self, message: str, *, suggestions: list[str] | None = None, hint: str | None = None
    ) -> None:
        super().__init__(message)
        self.suggestions = suggestions or []
        self.hint = hint


def load_graph(out_dir: Path) -> Graph:
    path = out_dir / 'graph.json'
    if not path.is_file():
        raise QueryError(f'no graph at {path}; run `konstruct build` first')
    return Graph.read(path)


def resolve_one(graph: Graph, ref: str) -> Node:
    found = graph.resolve(ref)
    if not found:
        hints = suggest(graph, ref)
        suffix = f'; did you mean {", ".join(hints)}?' if hints else ''
        words = ' '.join(_TOKEN.findall(ref.lower())) or ref
        raise QueryError(
            f'nothing in the graph matches {ref!r}{suffix}',
            suggestions=hints,
            hint=f'`konstruct search {words}`',
        )
    if len(found) > 1:
        options = ', '.join(f'{n.id}' for n in found[:10])
        raise QueryError(f'{ref!r} is ambiguous; use one of: {options}')
    return found[0]


def suggest(graph: Graph, ref: str, limit: int = 3) -> list[str]:
    """Names ``ref`` may have meant, closest first: what a search on its words finds, and, for a
    typo the words do not match (``custmers``, ``custmers.amout``), the relations whose names are
    close and the closest columns of those relations."""
    import difflib

    needle = ref.strip().lower()
    found = [graph.label(n) for n in search_nodes(graph, ref, limit=10)]
    relations = {graph.label(n).lower(): n for n in graph.nodes.values() if n.kind in RELATION_KINDS}
    head, dot, tail = needle.rpartition('.')
    for match in difflib.get_close_matches(head if dot else needle, list(relations), n=limit, cutoff=0.6):
        relation = relations[match]
        if dot:
            columns = {c.name.lower(): graph.label(c) for c in graph.columns_of(relation.id)}
            found += [columns[c] for c in difflib.get_close_matches(tail, list(columns), n=limit, cutoff=0.6)]
        found.append(graph.label(relation))
    unique = list(dict.fromkeys(found))
    unique.sort(key=lambda label: -difflib.SequenceMatcher(None, needle, label.lower()).ratio())
    return unique[:limit]


def _drop_empty(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None}


@dataclass(frozen=True)
class Item:
    """One node in a result, with how it was reached when it came from a walk."""

    id: str
    """The node id, such as ``model.shop.orders#amount``."""
    kind: str
    """The node kind, such as ``column``, ``model``, ``table``, ``source``, ``task`` or ``term``."""
    label: str
    """The short name the CLI prints, such as ``orders.amount``."""
    name: str
    """The node's own name, such as ``amount`` for that column."""
    relation_kind: str | None = None
    """For a column, the kind of relation it belongs to."""
    path: str | None = None
    """The file that defines the node (or its relation), relative to the project."""
    data_type: str | None = None
    """Column type, when known."""
    description: str | None = None
    """Description from dbt, the warehouse or definitions.yml."""
    depth: int | None = None
    """In a walk, how many steps from the starting node."""
    via: str | None = None
    """In a walk, the kind of edge that reached this node."""
    transform: str | None = None
    """``direct`` (passed through or renamed) or ``transformed``, for column edges."""
    verified: str | None = None
    """``konstruct verify``'s result for the edge that reached it, such as ``confirmed``."""
    role: str | None = None
    """``reference`` when the node joins, filters or groups the next relation."""
    command: str | None = None
    """For an orchestration task, the dbt command that runs the node."""
    through: str | None = None
    """The task whose SQL produced the edge, when one did."""
    direction: str | None = None
    """In a path, ``forward`` or ``backward`` along data flow."""
    score: float | None = None
    """In a search, how well the node matched."""
    status: str | None = None
    """For a business term, its status (draft, agreed, approved or retired)."""

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty({f.name: getattr(self, f.name) for f in fields(self)})

    def __str__(self) -> str:
        from konstruct.interfaces.render import item_line

        return item_line(self)


def item(graph: Graph, node: Node, step: Step | None = None, **extra: Any) -> Item:
    data: dict[str, Any] = {'id': node.id, 'kind': node.kind, 'label': graph.label(node), 'name': node.name}
    if node.kind == 'column':
        parent = graph.parent_of(node)
        if parent is not None:
            data['relation_kind'] = parent.kind
            data['path'] = parent.path
    else:
        data['path'] = node.path
    data['data_type'] = node.data_type
    data['description'] = node.description
    if node.kind == 'term':
        data['status'] = node.attrs.get('status')
    if step is not None:
        data['depth'] = step.depth
        if step.via is not None:
            data['via'] = step.via.kind
            for key in ('transform', 'verified', 'role', 'command'):
                data[key] = step.via.attrs.get(key)
            through = step.via.attrs.get('via')
            if through:
                data['through'] = graph.label(graph.nodes[through]) if through in graph.nodes else through
    data.update(extra)
    return Item(**data)


def _plural(count: int, word: str) -> str:
    return f'{count} {word}{"" if count == 1 else "s"}'


# Impact and trace


@dataclass
class Walk:
    """Everything downstream (impact) or upstream (trace) of one node, grouped by what it is."""

    direction: Literal['downstream', 'upstream']
    """``downstream`` for impact, ``upstream`` for trace."""
    target: Item
    """The node the walk started from."""
    columns: list[Item] = field(default_factory=list)
    """Columns reached, nearest first."""
    relations: list[Item] = field(default_factory=list)
    """Tables and models reached."""
    joins_and_filters: list[Item] = field(default_factory=list)
    """Relations that join, filter or group on the target (impact of a column)."""
    filtered_by: list[Item] = field(default_factory=list)
    """Columns that join or filter the target's relation (trace of a column)."""
    tests: list[Item] = field(default_factory=list)
    """dbt tests on anything reached."""
    exposures: list[Item] = field(default_factory=list)
    """dbt exposures (dashboards) reached."""
    tasks: list[Item] = field(default_factory=list)
    """Orchestration tasks that read, write or run anything reached."""
    terms: list[Item] = field(default_factory=list)
    """Business terms bound to the target or, downstream, to anything reached."""
    entities: dict[str, list[str]] = field(default_factory=dict)
    """The business entities (from the ontology) whose relations the walk reaches, each with
    those relations by label: which business objects a change touches."""
    parents: dict[str, str | None] = field(default_factory=dict, repr=False)
    """For each node reached, the id of the node it was reached from: the walk as a tree, for
    rendering. Not part of ``to_dict()``."""
    owners: dict[str, str] = field(default_factory=dict, repr=False)
    """For each column reached, its relation's id, so a tree can be drawn between relations."""
    start: list[str] = field(default_factory=list, repr=False)
    """The ids the walk started from: the target and, for a relation, its columns."""

    SECTIONS = (
        'columns',
        'relations',
        'joins_and_filters',
        'filtered_by',
        'tests',
        'exposures',
        'tasks',
        'terms',
    )

    @property
    def total(self) -> int:
        return sum(len(getattr(self, key)) for key in self.SECTIONS)

    def summary(self) -> str:
        counts = [
            _plural(count, noun)
            for count, noun in (
                (len(self.columns), 'column'),
                (len(self.relations) + len(self.joins_and_filters), 'relation'),
                (len(self.tests), 'test'),
                (len(self.exposures), 'exposure'),
                (len(self.tasks), 'task'),
                (len(self.terms), 'business term'),
            )
            if count
        ]
        where = 'downstream' if self.direction == 'downstream' else 'upstream'
        if not counts:
            return f'Nothing {where}.'
        listed = counts[0] if len(counts) == 1 else f'{", ".join(counts[:-1])} and {counts[-1]}'
        return f'{listed} {where}.'

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {'direction': self.direction, 'target': self.target.to_dict()}
        for key in self.SECTIONS:
            data[key] = [i.to_dict() for i in getattr(self, key)]
        data['entities'] = self.entities
        data['total'] = self.total
        data['summary'] = self.summary()
        return data

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import show, walk_lines

        return show(walk_lines, self, style)

    def __str__(self) -> str:
        return self.text()


def _walk(graph: Graph, start: Node, steps: list[Step], direction: Literal['downstream', 'upstream']) -> Walk:
    start_ids = set(graph.expand(start))
    walk = Walk(direction=direction, target=item(graph, start), start=[start.id, *start_ids])
    relation_ids: set[str] = set()
    listed: set[str] = set()
    for step in sorted(steps, key=lambda s: (s.depth, s.node.id)):
        node = step.node
        walk.parents.setdefault(node.id, step.previous)
        if node.kind == 'column' and node.parent:
            walk.owners[node.id] = node.parent
        if node.id in start_ids:
            continue
        entry = item(graph, node, step)
        if node.kind == 'column':
            walk.columns.append(entry)
            if node.parent and node.parent not in start_ids:
                relation_ids.add(node.parent)
        elif node.kind == 'test':
            walk.tests.append(entry)
        elif node.kind == 'exposure':
            walk.exposures.append(entry)
        elif node.kind in ('task', 'dag'):
            walk.tasks.append(entry)
        elif step.via is not None and step.via.attrs.get('role') == 'reference':
            walk.joins_and_filters.append(entry)
            listed.add(node.id)
        else:
            walk.relations.append(entry)
            listed.add(node.id)
    for relation_id in sorted(relation_ids - listed):
        walk.relations.append(item(graph, graph.nodes[relation_id]))
    # Business terms bound to the node itself and, downstream, to anything a change reaches.
    reached = set(start_ids) | {start.id}
    if direction == 'downstream':
        reached |= {s.node.id for s in steps} | relation_ids
    terms = {e.source for target in reached for e in graph.in_edges(target, 'defines')}
    walk.terms = [item(graph, graph.nodes[t]) for t in sorted(terms)]
    # The business entities whose relations the walk reaches, the start's own included.
    own: str | None = start.parent if start.kind == 'column' else start.id
    touched = {own or '', *relation_ids, *listed}
    entities: dict[str, set[str]] = {}
    for relation_id in touched:
        relation = graph.nodes.get(relation_id)
        if relation is None:
            continue
        for name in relation.attrs.get('entities') or []:
            entities.setdefault(str(name), set()).add(graph.label(relation))
    walk.entities = {name: sorted(entities[name]) for name in sorted(entities)}
    return walk


def impact(graph: Graph, ref: str, max_depth: int | None = None) -> Walk:
    """Everything downstream of a relation or column."""
    node = resolve_one(graph, ref)
    return _walk(graph, node, graph.impact(node, max_depth), 'downstream')


def trace(graph: Graph, ref: str, max_depth: int | None = None) -> Walk:
    """Everything upstream of a relation or column."""
    node = resolve_one(graph, ref)
    walk = _walk(graph, node, graph.trace(node, max_depth), 'upstream')
    if node.kind == 'column' and node.parent:
        walk.filtered_by = [
            item(graph, graph.nodes[e.source])
            for e in graph.in_edges(node.parent, 'feeds')
            if e.attrs.get('role') == 'reference' and e.source in graph.nodes
        ]
    return walk


# Path


@dataclass
class PathResult:
    start: Item
    """The node the path starts from."""
    end: Item
    """The node it ends at."""
    steps: list[Item]
    """Every node on the path in order, the start first; empty when there is no path."""

    @property
    def found(self) -> bool:
        return bool(self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            'from': self.start.to_dict(),
            'to': self.end.to_dict(),
            'found': self.found,
            'steps': [s.to_dict() for s in self.steps],
        }

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import path_lines, show

        return show(path_lines, self, style)

    def __str__(self) -> str:
        return self.text()


def path(graph: Graph, start_ref: str, end_ref: str) -> PathResult:
    """The shortest connection between two nodes, following data flow where possible."""
    start = resolve_one(graph, start_ref)
    end = resolve_one(graph, end_ref)
    steps = []
    for step in graph.path(start, end):
        direction = None
        if step.via is not None:
            direction = 'forward' if step.via.source == step.previous else 'backward'
        steps.append(item(graph, step.node, step, direction=direction))
    return PathResult(item(graph, start), item(graph, end), steps)


# Search


_TOKEN = re.compile(r'[a-z0-9]+')


def search_nodes(graph: Graph, text: str, limit: int = 20, kinds: list[str] | None = None) -> list[Node]:
    return [node for node, _ in _scored(graph, text, limit, kinds)]


class _SearchIndex:
    """Every node's words for ranked search, in the native core (``konstruct._core``). Text is
    folded to lower case here, by Python, so matching folds case exactly as ``str.lower`` does."""

    def __init__(self, graph: Graph) -> None:
        self.nodes = list(graph.nodes.values())
        labels = [graph.label(n) for n in self.nodes]
        synonyms = [
            [s.lower() for s in n.attrs.get('synonyms', [])] if n.kind == 'term' else [] for n in self.nodes
        ]
        self.core = _core.SearchIndex(
            [n.id for n in self.nodes],
            [n.kind for n in self.nodes],
            [
                1 if n.kind == 'term' else 2 if n.kind in RELATION_KINDS else 3 if n.kind == 'test' else 0
                for n in self.nodes
            ],
            [n.name.lower() for n in self.nodes],
            [label.lower() for label in labels],
            # A node's names: its label, relation key and, for a business term, its synonyms.
            [
                f'{label} {n.relation or ""} {" ".join(words)}'.lower()
                for n, label, words in zip(self.nodes, labels, synonyms, strict=True)
            ],
            [(n.description or '').lower() for n in self.nodes],
            synonyms,
        )


def _scored(graph: Graph, text: str, limit: int, kinds: list[str] | None) -> list[tuple[Node, float]]:
    """Nodes ranked by the words of ``text``: a word scores by its rarity across the graph, most
    in a node's name, less in its label, a little in its description. Exact names, business terms
    and relations rank higher, and tests lower."""
    # Made from the nodes alone, so an edge added or removed keeps it.
    index = graph.indexed('search', _SearchIndex, basis='nodes')
    found = index.core.search(text.lower(), text.strip().lower(), limit if limit >= 0 else None, kinds or [])
    return [(index.nodes[ordinal], score) for ordinal, score in found][:limit]


@dataclass
class SearchResult:
    query: str
    """The words searched for."""
    results: list[Item]
    """Matching nodes, best first, each with a ``score``."""

    def to_dict(self) -> dict[str, Any]:
        return {'query': self.query, 'results': [r.to_dict() for r in self.results]}

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import search_lines, show

        return show(search_lines, self, style)

    def __str__(self) -> str:
        return self.text()


def search(graph: Graph, text: str, limit: int = 20, kinds: list[str] | None = None) -> SearchResult:
    """Nodes whose names or descriptions match the words in ``text``, best first."""
    return SearchResult(
        text, [item(graph, n, score=round(s, 2)) for n, s in _scored(graph, text, limit, kinds)]
    )


# Describe


@dataclass
class Description:
    node: Item
    """The node described."""
    relation: str | None = None
    """For a relation, its warehouse name."""
    parent: str | None = None
    """For a column, its relation's id."""
    origins: list[str] = field(default_factory=list)
    """Inputs the node came from, such as ``dbt`` and ``duckdb``."""
    attrs: dict[str, Any] = field(default_factory=dict)
    """Every other attribute, such as materialisation, schedule, operator or owner."""
    columns: list[Item] = field(default_factory=list)
    """A relation's columns, in order."""
    tests: list[str] = field(default_factory=list)
    """Names of the dbt tests on the node or its columns."""
    upstream: list[str] = field(default_factory=list)
    """Immediate upstream neighbours, as ``label (kind)``."""
    downstream: list[str] = field(default_factory=list)
    """Immediate downstream neighbours."""
    tasks: list[str] = field(default_factory=list)
    """For a DAG or job, its tasks."""
    runs: list[str] = field(default_factory=list)
    """For a task, what it runs."""
    after: list[str] = field(default_factory=list)
    """For a task, the tasks it runs after."""
    before: list[str] = field(default_factory=list)
    """For a task, the tasks that run after it."""
    expression: str | None = None
    """For a column, its SQL over physical columns (see ``konstruct explain``)."""
    conditions: list[str] = field(default_factory=list)
    """For a relation, the joins, filters and grouping that shape its rows."""

    terms: list[str] = field(default_factory=list)
    """For a column or relation, the business terms that define it."""
    defines: list[str] = field(default_factory=list)
    """For a term, the columns it is bound to."""
    related: list[str] = field(default_factory=list)
    """For a term, its related terms."""
    neighbours: dict[str, list[Item]] = field(default_factory=dict, repr=False)
    """``upstream``, ``downstream`` and ``terms`` as items rather than text, for rendering. Not part
    of ``to_dict()``."""

    LISTS = (
        ('terms', 'Defined by'),
        ('defines', 'Columns'),
        ('related', 'Related terms'),
        ('conditions', 'Joins, filters and grouping'),
        ('tests', 'Tests'),
        ('upstream', 'Upstream'),
        ('downstream', 'Downstream'),
        ('tasks', 'Tasks'),
        ('runs', 'Runs'),
        ('after', 'After'),
        ('before', 'Before'),
    )

    def to_dict(self) -> dict[str, Any]:
        data = self.node.to_dict()
        data.update(
            _drop_empty({'relation': self.relation, 'parent': self.parent, 'expression': self.expression})
        )
        data['origins'] = self.origins
        data['attrs'] = self.attrs
        if self.node.kind in RELATION_KINDS:
            data['columns'] = [c.to_dict() for c in self.columns]
        for key, _ in self.LISTS:
            data[key] = getattr(self, key)
        return data

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import description_lines, show

        return show(description_lines, self, style)

    def __str__(self) -> str:
        return self.text()


def _attr_text(value: Any) -> str:
    """Attribute values as people write them: lists comma-separated, an owner as name (role)."""
    if isinstance(value, dict) and 'queries' in value:
        return (
            f'{value["queries"]} queries by {value["users"]} users in {value.get("days", "?")} days '
            f'(score {value.get("score", "?")})'
        )
    if isinstance(value, dict) and 'name' in value:
        return f'{value["name"]} ({value["role"]})' if value.get('role') else str(value['name'])
    if isinstance(value, (list, tuple)):
        return ', '.join(_attr_text(v) for v in value)
    return str(value)


def _where(path: str | None, line: int | None, iri: str | None) -> str | None:
    if not path:
        return None
    if line:
        return f'{path}:{line}'
    return f'{path} ({iri})' if iri else path


def _term_label(graph: Graph, node: Node) -> str:
    return f'{node.name} ({node.attrs.get("status", "draft")})'


def _neighbour(graph: Graph, node_id: str) -> str:
    node = graph.nodes[node_id]
    return f'{graph.label(node)} ({node.kind})'


def describe(graph: Graph, ref: str) -> Description:
    """One node in detail: columns, tests, neighbours and, for tasks, what they run."""
    node = resolve_one(graph, ref)
    result = Description(
        node=item(graph, node),
        relation=node.relation,
        parent=node.parent,
        origins=sorted(node.origins),
        # A term's columns and related terms are listed below, with their resolved names.
        attrs={
            k: v
            for k, v in node.attrs.items()
            if k not in ('expression', 'conditions')
            and not (node.kind == 'term' and k in ('columns', 'related', 'line'))
        },
    )
    if node.attrs.get('expression') or node.attrs.get('conditions'):
        from konstruct.analysis.explain import short_sql

        if isinstance(node.attrs.get('expression'), str):
            result.expression = short_sql(graph, node.attrs['expression'])
        result.conditions = [short_sql(graph, c, condition=True) for c in node.attrs.get('conditions', [])]
    if node.kind in RELATION_KINDS:
        result.columns = [item(graph, c) for c in graph.columns_of(node.id)]
    checked = [node.id, *(c.id for c in graph.columns_of(node.id))]
    result.tests = sorted({graph.nodes[e.source].name for t in checked for e in graph.in_edges(t, 'tests')})
    upstream = {o for _, o in graph.upstream_edges(node.id) if graph.nodes[o].kind != 'dag'}
    downstream = {o for _, o in graph.downstream_edges(node.id) if graph.nodes[o].kind != 'dag'}
    terms = {e.source for e in graph.in_edges(node.id, 'defines') if e.attrs.get('role') != 'input'}

    def term(node_id: str) -> str:
        return _term_label(graph, graph.nodes[node_id])

    def neighbour(node_id: str) -> str:
        return _neighbour(graph, node_id)

    for key, ids, label in (
        ('upstream', upstream, neighbour),
        ('downstream', downstream, neighbour),
        ('terms', terms, term),
    ):
        ordered = sorted(ids, key=label)
        setattr(result, key, [label(i) for i in ordered])
        result.neighbours[key] = [item(graph, graph.nodes[i]) for i in ordered]
    if node.kind == 'term':
        result.defines = sorted(
            graph.label(graph.nodes[e.target]) for e in graph.out_edges(node.id, 'defines')
        )
        result.related = sorted(
            {
                graph.nodes[e.target].name
                for e in graph.out_edges(node.id, 'relates_to')
                if not e.attrs.get('role')
            }
            | {
                graph.nodes[e.source].name
                for e in graph.in_edges(node.id, 'relates_to')
                if not e.attrs.get('role')
            }
        )
        narrower = sorted(
            graph.nodes[e.source].name
            for e in graph.in_edges(node.id, 'relates_to')
            if e.attrs.get('role') == 'broader'
        )
        if narrower:
            result.attrs['narrower'] = narrower
    if node.kind == 'dag':
        result.tasks = [graph.nodes[e.target].name for e in graph.out_edges(node.id, 'runs')]
    if node.kind == 'task':
        result.runs = sorted(graph.label(graph.nodes[e.target]) for e in graph.out_edges(node.id, 'runs'))
        result.after = sorted(graph.nodes[e.source].name for e in graph.in_edges(node.id, 'precedes'))
        result.before = sorted(graph.nodes[e.target].name for e in graph.out_edges(node.id, 'precedes'))
    return result


# Define


@dataclass
class TermDefinition:
    """One business term: what it means, who owns it and the columns that hold it."""

    name: str
    """The term."""
    definition: str | None
    """The agreed wording."""
    status: str
    """``draft``, ``agreed``, ``approved`` or ``retired``."""
    owner: dict[str, Any] | None = None
    """``{"name": ..., "role": ...}``, when set."""
    approved: str | None = None
    """The approval date, ``YYYY-MM-DD``."""
    synonyms: list[str] = field(default_factory=list)
    """Other names for the term."""
    grain: str | None = None
    """What one row means, such as ``one row per customer``."""
    columns: list[Item] = field(default_factory=list)
    """The columns (or relations) that hold the term."""
    filters: list[str] = field(default_factory=list)
    """Filters any answer about the term must apply, as SQL."""
    unit: str | None = None
    """Unit of measure, such as ``currency``."""
    currency: str | None = None
    """ISO 4217 currency code."""
    related: list[str] = field(default_factory=list)
    """Names of related terms."""
    broader: list[str] = field(default_factory=list)
    """The wider concepts this term is a kind of."""
    narrower: list[str] = field(default_factory=list)
    """The terms that are kinds of this one."""
    exceptions: str | None = None
    """Known exceptions to the definition."""
    path: str | None = None
    """The definitions file."""
    line: int | None = None
    """The line the term starts on."""
    iri: str | None = None
    """For a term from a SKOS or OWL vocabulary, the concept's IRI."""
    id: str = ''
    """The node id, ``term.<slug>``."""

    def to_dict(self) -> dict[str, Any]:
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data['columns'] = [c.to_dict() for c in self.columns]
        return _drop_empty(data)

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import show, term_lines

        return show(term_lines, self, style)

    def __str__(self) -> str:
        return self.text()


@dataclass
class Definitions:
    """The answer to `konstruct define`: the terms a name or column refers to, approved first."""

    query: str
    """What was asked for."""
    terms: list[TermDefinition] = field(default_factory=list)
    """Matching terms, approved first and retired last."""
    column: Item | None = None
    """When the query named a column, that column."""

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty(
            {
                'query': self.query,
                'column': self.column.to_dict() if self.column else None,
                'terms': [t.to_dict() for t in self.terms],
            }
        )

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import definitions_lines, show

        return show(definitions_lines, self, style)

    def __str__(self) -> str:
        return self.text()


_STATUS_ORDER = {'approved': 0, 'agreed': 1, 'draft': 2, 'retired': 3}


def term_definition(graph: Graph, node: Node) -> TermDefinition:
    attrs = node.attrs
    columns = [item(graph, graph.nodes[e.target]) for e in graph.out_edges(node.id, 'defines')]
    return TermDefinition(
        id=node.id,
        name=node.name,
        definition=node.description,
        status=str(attrs.get('status', 'draft')),
        owner=attrs.get('owner'),
        approved=attrs.get('approved'),
        synonyms=list(attrs.get('synonyms', [])),
        grain=attrs.get('grain'),
        columns=sorted(columns, key=lambda c: c.label),
        filters=list(attrs.get('filters', [])),
        unit=attrs.get('unit'),
        currency=attrs.get('currency'),
        related=list(attrs.get('related', [])),
        broader=list(attrs.get('broader', [])),
        narrower=sorted(
            graph.nodes[e.source].name
            for e in graph.in_edges(node.id, 'relates_to')
            if e.attrs.get('role') == 'broader'
        ),
        exceptions=attrs.get('exceptions'),
        path=node.path,
        line=attrs.get('line'),
        iri=attrs.get('iri'),
    )


def define(graph: Graph, ref: str) -> Definitions:
    """The business terms ``ref`` names (by name or synonym), or the terms that define the column
    or relation ``ref`` names. Approved terms come first, retired ones last."""
    needle = ' '.join(ref.split()).lower()
    terms = [
        n
        for n in graph.nodes.values()
        if n.kind == 'term'
        and (
            needle in (n.name.lower(), n.id.lower())
            or needle in (s.lower() for s in n.attrs.get('synonyms', []))
        )
    ]
    column: Item | None = None
    if not terms:
        found = [n for n in graph.resolve(ref) if n.kind != 'term']
        if len(found) == 1:
            target = found[0]
            column = item(graph, target)
            terms = [
                graph.nodes[e.source]
                for e in graph.in_edges(target.id, 'defines')
                if e.attrs.get('role') != 'input'
            ]
        elif len(found) > 1:
            raise QueryError(f'{ref!r} is ambiguous; use one of: {", ".join(n.id for n in found[:10])}')
        else:
            hints = [n.name for n in search_nodes(graph, ref, limit=5, kinds=['term'])]
            hints = hints or [graph.label(n) for n in search_nodes(graph, ref, limit=5)]
            suffix = f'; did you mean {", ".join(hints)}?' if hints else ''
            raise QueryError(
                f'no term or column matches {ref!r}{suffix}',
                suggestions=hints,
                hint=f'`konstruct search {needle}`',
            )
    ordered = sorted(terms, key=lambda n: (_STATUS_ORDER.get(str(n.attrs.get('status')), 9), n.name))
    return Definitions(query=ref, terms=[term_definition(graph, n) for n in ordered], column=column)


# List


@dataclass
class Listing:
    kinds: list[str]
    """The node kinds listed."""
    items: list[Item]
    """Every node of those kinds."""

    def to_dict(self) -> dict[str, Any]:
        return {'kinds': self.kinds, 'items': [i.to_dict() for i in self.items]}

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import listing_lines, show

        return show(listing_lines, self, style)

    def __str__(self) -> str:
        return self.text()


def list_nodes(graph: Graph, kinds: list[str] | None = None) -> Listing:
    """Every node of the given kinds (relations by default), sorted by kind then label."""
    chosen = kinds or sorted(RELATION_KINDS)
    nodes = [n for n in graph.nodes.values() if n.kind in chosen]
    nodes.sort(key=lambda n: (chosen.index(n.kind) if n.kind in chosen else 0, graph.label(n).lower()))
    return Listing(list(chosen), [item(graph, n) for n in nodes])


# Free-text query


_DOWNSTREAM_WORDS = frozenset(
    {
        'impact',
        'break',
        'breaks',
        'affect',
        'affects',
        'affected',
        'downstream',
        'depend',
        'depends',
        'dependants',
        'dependents',
        'change',
        'rename',
        'drop',
        'remove',
    }
)
_UPSTREAM_WORDS = frozenset(
    {
        'upstream',
        'source',
        'sources',
        'lineage',
        'derived',
        'origin',
        'come',
        'comes',
        'trace',
        'built',
        'calculated',
        'computed',
    }
)


@dataclass(frozen=True)
class EdgeItem:
    source: str
    """Source node id."""
    target: str
    """Target node id."""
    kind: str
    """The edge kind, such as ``feeds``, ``derived_from``, ``runs``, ``tests`` or ``defines``."""
    source_label: str
    """Short name of the source."""
    target_label: str
    """Short name of the target."""
    transform: str | None = None
    """For ``derived_from``, ``direct`` or ``transformed``."""
    role: str | None = None
    """For ``feeds``, ``reference`` when the source joins, filters or groups."""

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty({f.name: getattr(self, f.name) for f in fields(self)})

    def __str__(self) -> str:
        from konstruct.interfaces.render import edge_line

        return edge_line(self)


@dataclass
class QueryResult:
    question: str
    """The question asked."""
    matches: list[Item] = field(default_factory=list)
    """The best-matching nodes."""
    edges: list[EdgeItem] = field(default_factory=list)
    """Edges around the matches."""
    neighbours: list[Item] = field(default_factory=list)
    """Nodes those edges reach."""
    impact: Walk | None = None
    """Everything downstream of the first match, when the question asks what breaks."""
    lineage: Walk | None = None
    """Everything upstream of the first match, when the question asks where it comes from."""
    definitions: list[TermDefinition] = field(default_factory=list)
    """Business terms among the matches, or defining them, with their agreed definitions."""

    EDGE_LIMIT = 80

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            'question': self.question,
            'matches': [m.to_dict() for m in self.matches],
            'edges': [e.to_dict() for e in self.edges],
            'neighbours': [n.to_dict() for n in self.neighbours],
        }
        if self.impact is not None:
            data['impact'] = self.impact.to_dict()
        if self.lineage is not None:
            data['lineage'] = self.lineage.to_dict()
        if self.definitions:
            data['definitions'] = [d.to_dict() for d in self.definitions]
        return data

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import query_lines, show

        return show(query_lines, self, style)

    def __str__(self) -> str:
        return self.text()


def query(graph: Graph, question: str, limit: int = 8, depth: int = 1) -> QueryResult:
    """Answer a free-text question with the most relevant nodes and their neighbourhood.

    There is no language model here: Konstruct ranks nodes by the question's words and returns
    the matching nodes, the edges around them, and impact or lineage when the question asks.
    """
    words = set(_TOKEN.findall(question.lower()))
    seeds = _scored(graph, question, limit, None)
    result = QueryResult(question)
    if not seeds:
        return result
    top_score = seeds[0][1]
    chosen = [node for node, score in seeds if score >= top_score * 0.35]
    seen = {n.id for n in chosen}
    edge_keys: set[tuple[str, str, str]] = set()
    result.matches = [item(graph, n) for n in chosen]
    for node in chosen:
        frontier = [node.id]
        for _ in range(depth):
            following = []
            for node_id in frontier:
                for edge in [*graph.out_edges(node_id), *graph.in_edges(node_id)]:
                    if edge.kind == 'contains' or edge.key in edge_keys:
                        continue
                    edge_keys.add(edge.key)
                    result.edges.append(
                        EdgeItem(
                            source=edge.source,
                            target=edge.target,
                            kind=edge.kind,
                            source_label=graph.label(graph.nodes[edge.source]),
                            target_label=graph.label(graph.nodes[edge.target]),
                            transform=edge.attrs.get('transform'),
                            role=edge.attrs.get('role'),
                        )
                    )
                    for other in (edge.source, edge.target):
                        if other not in seen:
                            seen.add(other)
                            following.append(other)
            frontier = following
    chosen_ids = {n.id for n in chosen}
    result.neighbours = [item(graph, graph.nodes[i]) for i in sorted(seen - chosen_ids)][:60]
    # Definitions: terms that match nearly as well as the best match, and the terms that define
    # the best matching column or relation.
    terms: dict[str, Node] = {n.id: n for n, score in seeds if n.kind == 'term' and score >= top_score * 0.6}
    first = next((n for n in chosen if n.kind != 'term'), None)
    if first is not None:
        for edge in graph.in_edges(first.id, 'defines'):
            terms.setdefault(edge.source, graph.nodes[edge.source])
    ordered = sorted(terms.values(), key=lambda n: (_STATUS_ORDER.get(str(n.attrs.get('status')), 9), n.name))
    result.definitions = [term_definition(graph, n) for n in ordered]
    # Impact or lineage follows the first matching column or relation, not a term.
    top = next((n for n in chosen if n.kind != 'term'), chosen[0])
    if words & _DOWNSTREAM_WORDS:
        result.impact = impact(graph, top.id)
    elif words & _UPSTREAM_WORDS:
        result.lineage = trace(graph, top.id)
    return result
