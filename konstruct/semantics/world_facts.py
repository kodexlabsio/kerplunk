"""The world model (:mod:`konstruct.semantics.world`) as facts that two builds can be compared by,
and the comparison: what a change to a data stack changed in the business's meaning.

Each kind of fact is keyed by what names it: an entity and a metric by name, a relationship by its
two entities and the columns that join them, a property by its entity and name. A formula and the
joins, filters and grouping a metric is computed over are composed down to source columns, so a
model added on the way that only passes columns on changes nothing, and evidence keeps what kind
of evidence it is but not the file or line it is in, so moving a file or editing elsewhere in it
changes nothing either.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from konstruct.core import jsonio
from konstruct.core.graph import Graph, Node

if TYPE_CHECKING:
    from konstruct.analysis.history import Commit
    from konstruct.semantics.ontology import Ontology

KINDS = ('entity', 'relationship', 'property', 'metric')
"""What a world holds, in the order changes are listed."""
_PLURAL = {
    'entity': 'entities',
    'relationship': 'relationships',
    'property': 'properties',
    'metric': 'metrics',
}
_FILE = r'[\w./\\-]+\.(?:ya?ml|sql|json|lkml|py|tmdl|bim|ttl|owl|rdf|csv|md|toml)(?::\d+)?'
_IN_FILE = re.compile(rf'\s+in\s+{_FILE}\b|\s*\({_FILE}\)')
"""Where a piece of evidence was found (``term in definitions.yml``, ``(models/schema.yml:12)``)."""
_BARE = re.compile(r'^[\w$`"\[\]]+(?:\.[\w$`"\[\]]+)*$')
"""An expression that is a column and nothing more, which passes that column on."""
_EDGES = re.compile(r'konstruct verify: \d+ of \d+ lineage edges? confirmed$')


@dataclass
class World:
    """The facts of one world that two builds are compared by."""

    entities: dict[str, dict[str, Any]] = field(default_factory=dict)
    relationships: dict[str, dict[str, Any]] = field(default_factory=dict)
    properties: dict[str, dict[str, Any]] = field(default_factory=dict)
    metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    dialect: str | None = None
    """The SQL dialect its formulas and conditions are written in."""
    notes: list[str] = field(default_factory=list)
    """What the build of this world could not read, and why."""
    digests: dict[str, str] = field(default_factory=dict)
    """Each property column's formula digest (see :class:`Formulas`), by the column's label."""
    texts: dict[str, str] = field(default_factory=dict)
    """The composed formula of a digest, for those composed: not compared, only worded."""

    def of(self, kind: str) -> dict[str, dict[str, Any]]:
        """The facts of one kind (``entity``, ``relationship``, ``property`` or ``metric``)."""
        found: dict[str, dict[str, Any]] = getattr(self, _PLURAL[kind])
        return found

    def to_dict(self) -> dict[str, Any]:
        return {
            'entities': self.entities,
            'relationships': self.relationships,
            'properties': self.properties,
            'metrics': self.metrics,
            'dialect': self.dialect,
            'notes': self.notes,
            'digests': self.digests,
            'texts': self.texts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> World:
        return cls(
            entities=dict(data.get('entities') or {}),
            relationships=dict(data.get('relationships') or {}),
            properties=dict(data.get('properties') or {}),
            metrics=dict(data.get('metrics') or {}),
            dialect=data.get('dialect'),
            notes=list(data.get('notes') or []),
            digests=dict(data.get('digests') or {}),
            texts=dict(data.get('texts') or {}),
        )


def world_of(
    graph: Graph,
    ontology: Ontology | None = None,
    root: Path | None = None,
    previous: World | None = None,
    formulas: Formulas | None = None,
    every: bool = True,
) -> World:
    """The world of ``graph`` as facts to compare: its ontology's entities and relationships, each
    entity's properties with the digest of their composed formulas, and every metric, proposals
    included, with the conditions on its rows, its trust and its approvals. ``root`` is the project
    the graph was built from, whose files the conditions' composition may read.

    A property's formula is compared by its digest (:class:`Formulas`), and its text composed only
    where it will be worded: where the digest differs from the ``previous`` world's, so the worlds
    of a range compose only what their SQL changed, or with no previous world, for every column
    unless ``every`` is false. ``formulas`` is the graph's :class:`Formulas`, when the caller keeps
    it to compose texts of this world later."""
    from konstruct.analysis.lock import Pinner
    from konstruct.semantics.ontology import derive
    from konstruct.semantics.world import all_metrics, properties

    ontology = ontology or derive(graph)
    pinner = Pinner(graph, root or Path(str(graph.meta.get('root') or '.')), None)
    formulas = formulas or Formulas(graph, pinner.explainer)

    dialect = graph.meta.get('dialect')
    world = World(dialect=dialect if isinstance(dialect, str) else None)
    for item in ontology.entities:
        world.entities[item.name] = {
            'key': item.key,
            'keys': list(item.keys),
            'relations': list(item.relations),
            'term': item.term,
            **_approval(graph, item.term),
            'description': item.description,
            'confidence': item.confidence,
            'check': item.check,
            'evidence': _evidence(item.evidence),
        }
        # A property's type is left out: dbt's catalog, which gives most types, is today's.
        holding = {graph.nodes[k].parent for k in item.key_ids if k in graph.nodes}
        by_label = {graph.label(c): c for r in holding if r for c in graph.columns_of(r)}
        for prop in properties(graph, ontology, item):
            listed = []
            for label in prop.columns:
                column = by_label.get(label)
                if column is None:
                    continue
                digest = formulas.digest(column)
                world.digests[label] = digest
                formulas.labels[label] = column
                listed.append({'column': label, 'formula': digest})
                changed = previous is not None and previous.digests.get(label) != digest
                if changed or (previous is None and every):
                    formulas.word(column, world.texts)
            world.properties[f'{item.name}.{prop.name}'] = {
                'entity': item.name,
                'name': prop.name,
                'columns': list(prop.columns),
                'formulas': listed,
                'description': prop.description,
                'terms': list(prop.terms),
                'refers_to': prop.refers_to,
            }
    for relationship in ontology.relationships:
        world.relationships[f'{relationship.source} -> {relationship.target} ({relationship.via})'] = {
            'source': relationship.source,
            'target': relationship.target,
            'via': relationship.via,
            'cardinality': relationship.cardinality,
            'tested': relationship.tested,
            'confidence': relationship.confidence,
            'check': relationship.check,
            'evidence': _evidence(relationship.evidence),
        }
    found = all_metrics(graph, ontology)
    by_name: dict[str, list[dict[str, Any]]] = {}
    for metric in found:
        by_name.setdefault(metric.name, []).append(
            {
                'name': metric.name,
                'source': metric.source,
                'proposed': metric.proposed,
                'confidence': metric.confidence,
                'definition': metric.definition,
                'status': metric.status,
                'owner': metric.owner,
                'approved': str(metric.approved) if metric.approved else None,
                'entity': metric.entity,
                'grain': metric.grain,
                'unit': metric.unit,
                'currency': metric.currency,
                'filters': list(metric.filters),
                'formula': metric.formula,
                'aggregation': metric.aggregation,
                'additive': metric.additive,
                'synonyms': list(metric.synonyms),
                'columns': sorted(c.column for c in metric.columns),
                'rows': sorted(
                    {
                        c
                        for column in metric.columns
                        if column.id in graph.nodes
                        for c in pinner.pin(graph.nodes[column.id]).conditions
                    }
                ),
                'evidence': _evidence(metric.evidence),
            }
        )
    for name, same in by_name.items():
        world.metrics.update(zip(unique_keys(name, same), same, strict=True))
    return world


def unique_keys(name: str, metrics: list[dict[str, Any]]) -> list[str]:
    """Keys for the metrics that share ``name``, made from what each is, so the same metrics get
    the same keys whatever order they are found in: the name alone for one, then with its
    source, then with its entity and columns too, then with all its facts."""
    levels: list[Callable[[dict[str, Any]], str]] = [
        lambda m: name,
        lambda m: f'{name} ({m["source"]})',
        lambda m: f'{name} ({m["source"]}: {m["entity"] or "no entity"}; {", ".join(m["columns"])})',
        lambda m: f'{name} {jsonio.dumps(m, sort_keys=True, default=str)}',
    ]
    for level in levels:
        keys = [level(m) for m in metrics]
        if len(set(keys)) == len(keys):
            return keys
    return [f'{key} #{index}' for index, key in enumerate(keys)]  # the same facts twice over


class Formulas:
    """The composed formulas of one graph's columns, by digest.

    A column's digest covers everything its composed formula depends on: the dialect, its expression and each
    edge to the columns it is derived from, with their own digests, and for a source column, its
    relation's full name and its name. Two graphs give a column the same digest exactly when its
    formula's whole way to source is the same, and a column renamed, or one that only passes
    another on, keeps its input's. The text, with full relation names, is composed only when
    asked for (:meth:`word`)."""

    def __init__(self, graph: Graph, explainer: Any = None) -> None:
        from konstruct.analysis.explain import Explainer

        self.graph = graph
        self.explainer = explainer if explainer is not None else Explainer(graph, qualified=True)
        self.digests: dict[str, str] = {}
        self.labels: dict[str, Node] = {}
        """The column each property label of the world stood for: a label can name more than one
        column of the graph (a model and the source it reads)."""
        dialect = graph.meta.get('dialect')
        self.dialect = dialect if isinstance(dialect, str) else None
        """The dialect the expressions are read in, which is part of what they mean."""

    def word(self, column: Node, texts: dict[str, str]) -> None:
        """Add ``column``'s composed formula to ``texts`` under its digest."""
        digest = self.digest(column)
        if digest not in texts:
            text = _formula(self.graph, self.explainer, column)
            if text is not None:
                texts[digest] = text

    def text(self, label: str, texts: dict[str, str]) -> None:
        """:meth:`word` for the property column a world called ``label``, when it had one."""
        column = self.labels.get(label)
        if column is not None:
            self.word(column, texts)

    def digest(self, column: Node) -> str:
        graph = self.graph
        # Depth first without recursion: a column's digest is made once its inputs' are.
        pending = [(column, False)]
        visiting: set[str] = set()
        while pending:
            node, ready = pending.pop()
            if node.id in self.digests:
                continue
            edges = sorted(graph.out_edges(node.id, 'derived_from'), key=lambda e: e.target)
            if not ready:
                if node.id in visiting:
                    continue
                visiting.add(node.id)
                pending.append((node, True))
                # A column on a cycle back to one being visited stands in by its id.
                pending += [
                    (graph.nodes[e.target], False)
                    for e in edges
                    if e.target in graph.nodes and e.target not in self.digests and e.target not in visiting
                ]
                continue
            expression = node.attrs.get('expression')
            passes = expression is None or (
                isinstance(expression, str) and _BARE.match(expression) is not None
            )
            if passes and len(edges) == 1 and edges[0].target in self.digests:
                # A column that only passes another on is that one.
                self.digests[node.id] = self.digests[edges[0].target]
                continue
            parent = graph.parent_of(node)
            parts: list[Any] = [
                self.dialect,
                expression,
                [
                    (self.digests.get(e.target, e.target), sorted((k, str(v)) for k, v in e.attrs.items()))
                    for e in edges
                ],
            ]
            if not edges:
                parts += [parent.relation or parent.id if parent is not None else '', node.name]
            self.digests[node.id] = hashlib.sha256(jsonio.dumps(parts, default=str).encode()).hexdigest()
        return self.digests[column.id]


def _formula(graph: Graph, explainer: Any, column: Any) -> str | None:
    """A column's formula composed down to source, with full relation names; for a column that
    only passes another on, the formula of the column it passes on, and at the end of that, the
    source column itself."""
    seen = set()
    while 'expression' not in column.attrs and column.id not in seen:
        seen.add(column.id)
        upstream = graph.out_edges(column.id, 'derived_from')
        if len(upstream) != 1 or upstream[0].target not in graph.nodes:
            if upstream:
                return None
            parent = graph.parent_of(column)
            return (
                f'{parent.relation or graph.label(parent)}.{column.name}' if parent is not None else column.id
            )
        column = graph.nodes[upstream[0].target]
    found: str | None = explainer.composed_text(column)
    return found


def _approval(graph: Graph, name: str | None) -> dict[str, Any]:
    """The status, owner and approval date of the business term called ``name``."""
    from konstruct.semantics import definitions

    term = graph.nodes.get(f'term.{definitions.slug(name)}') if name else None
    attrs = term.attrs if term is not None else {}
    owner = attrs.get('owner')
    return {
        'status': attrs.get('status'),
        'owner': owner.get('name') if isinstance(owner, dict) else None,
        'approved': str(attrs['approved']) if attrs.get('approved') else None,
    }


def _evidence(items: Iterable[str]) -> list[str]:
    """Evidence by its kind, without the file and line it was found in, and without the count of
    lineage edges on the way that ``konstruct verify`` confirmed: a model added on the way changes
    the count, and the metric's confidence already says what verify found."""
    return list(dict.fromkeys(_IN_FILE.sub('', i) for i in items if not _EDGES.match(i)))


# Comparing two worlds


@dataclass
class WorldChange:
    """One thing whose meaning changed between two worlds."""

    kind: str
    """``entity``, ``relationship``, ``property`` or ``metric``."""
    name: str
    """What names it in the later world (in the earlier one, for one removed)."""
    status: str
    """``added``, ``removed``, ``renamed`` (and perhaps changed besides) or ``changed``."""
    before: dict[str, Any] | None = None
    """Its facts in the earlier world; None when it was added."""
    after: dict[str, Any] | None = None
    """Its facts in the later world; None when it was removed."""
    fields: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    """For one renamed or changed, each fact that differs, with its value before and after."""
    renamed_from: str | None = None
    """For one renamed, its name in the earlier world."""
    english: list[str] = field(default_factory=list)
    """The change in plain English, one sentence for each part of it."""
    approval: Commit | None = None
    """For a metric, the first commit that approved it at or after this change, by ancestry."""
    effect: Any = None
    """For a metric whose formula or rows changed, what the change does on today's data
    (:class:`konstruct.analysis.effect.Effect`), once ``konstruct verify --effect`` ran it."""
    effect_column: str | None = None
    """The column or relation whose change ``effect`` is of."""
    worded: dict[str, list[str]] = field(default_factory=dict, repr=False, compare=False)
    """``english`` by the fact each sentence is about, and under ``note`` the release note's
    words for a change of required filters."""

    @property
    def facts(self) -> dict[str, Any]:
        """Its facts in the later world, or in the earlier one when it was removed."""
        return self.after if self.after is not None else self.before or {}

    def mirrored(self) -> WorldChange:
        """The same change read the other way, from the later world to the earlier one."""
        status = {'added': 'removed', 'removed': 'added'}.get(self.status, self.status)
        return WorldChange(
            kind=self.kind,
            name=self.renamed_from if self.renamed_from is not None else self.name,
            status=status,
            before=self.after,
            after=self.before,
            fields={k: (a, b) for k, (b, a) in self.fields.items()},
            renamed_from=self.name if self.renamed_from is not None else None,
        )

    def headline(self) -> str:
        """What changed, in a few words."""
        if self.status == 'renamed':
            return f'{self.kind} {self.name!r}: renamed from {self.renamed_from!r}'
        if self.status == 'changed':
            return f'{self.kind} {self.name!r}: {", ".join(self.fields)} changed'
        return f'{self.kind} {self.name!r}: {self.status}'

    def to_dict(self) -> dict[str, Any]:
        return {
            'kind': self.kind,
            'name': self.name,
            'status': self.status,
            'headline': self.headline(),
            'renamed_from': self.renamed_from,
            'fields': [{'name': k, 'before': b, 'after': a} for k, (b, a) in self.fields.items()],
            'before': self.before,
            'after': self.after,
            'english': self.english,
            'approval': self.approval.to_dict() if self.approval is not None else None,
            'effect': self.effect.to_dict() if self.effect is not None else None,
            'effect_column': self.effect_column,
        }


_NAMES = {'metric': 'name', 'property': 'name'}
"""The fact that holds an item's own name, which a rename changes and the diff leaves out."""


def _signature(kind: str, facts: dict[str, Any]) -> str | None:
    """What stays the same when something is renamed: an entity's keys; a metric's columns,
    formula and definition together; a property's entity and composed formulas. None for what is
    never taken as renamed (a relationship, or a property or metric with nothing to tell it by)."""
    found: Any = None
    if kind == 'entity':
        found = facts.get('keys') or None
    elif kind == 'metric' and (facts.get('columns') or facts.get('formula')):
        found = [facts.get('columns'), facts.get('formula'), facts.get('definition')]
    elif kind == 'property' and facts.get('formulas'):
        found = [facts.get('entity'), sorted({f['formula'] for f in facts['formulas']})]
    return jsonio.dumps(found, sort_keys=True, default=str) if found is not None else None


def _fields(kind: str, before: dict[str, Any], after: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    own = _NAMES.get(kind)
    return {
        k: (before.get(k), after.get(k))
        for k in sorted(set(before) | set(after))
        if k != own and before.get(k) != after.get(k)
    }


def world_diff(a: World, b: World) -> list[WorldChange]:
    """What changed from world ``a`` to world ``b``, by kind and then by name. Something that is
    gone and something new with the same signature (see :func:`_signature`), where exactly one of
    each has it, is one thing renamed. The diff of a world with itself is empty, and
    ``world_diff(b, a)`` is ``world_diff(a, b)`` with each change mirrored."""
    changes: list[WorldChange] = []
    for kind in KINDS:
        old, new = a.of(kind), b.of(kind)
        removed = sorted(set(old) - set(new))
        added = sorted(set(new) - set(old))
        by_signature: dict[str, tuple[list[str], list[str]]] = {}
        for side, names, facts in ((0, removed, old), (1, added, new)):
            for name in names:
                signature = _signature(kind, facts[name])
                if signature is not None:
                    by_signature.setdefault(signature, ([], []))[side].append(name)
        renames = {came[0]: gone[0] for gone, came in by_signature.values() if len(gone) == len(came) == 1}
        found: list[WorldChange] = []
        for name in sorted(set(old) & set(new)):
            fields = _fields(kind, old[name], new[name])
            if fields:
                found.append(WorldChange(kind, name, 'changed', old[name], new[name], fields))
        for name in added:
            was = renames.get(name)
            if was is not None:
                found.append(
                    WorldChange(
                        kind, name, 'renamed', old[was], new[name], _fields(kind, old[was], new[name]), was
                    )
                )
            else:
                found.append(WorldChange(kind, name, 'added', None, new[name]))
        taken = set(renames.values())
        found += [WorldChange(kind, n, 'removed', old[n], None) for n in removed if n not in taken]
        changes += sorted(found, key=lambda c: c.name)
    return changes
