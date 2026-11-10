"""The ontology of a data stack, derived from its graph: what the business objects are, what is
measured about them, how they relate, where each measure comes from, how far to trust it, and the
rules the build holds it to.

Most of it is read from the code rather than declared:

- **Entities** are the business objects (customer, order, payment). An identifier-like column
  (``customer_id``, ``id``, ``order_key``) with a dbt ``unique`` test, or a single-column primary
  key or Iceberg identifier field, is the key of an entity named after the key (``customer_id`` is
  the key of *customer*; a bare ``id`` takes the relation's name); the relations keyed by it hold
  one row per instance. A term of kind ``entity`` in ``definitions.yml`` names one explicitly, and
  a measure's grain (``one row per customer``) finds the key of the entity it names.
- **Measures** are business terms with a formula: a term of kind ``measure``, or one whose bound
  columns are computed. Each bound column carries its formula composed down to source columns,
  the sources it reaches and how many of the edges on the way ``konstruct verify`` confirmed.
- **Relationships** link entities: a dbt ``relationships`` test, or a join that matches one
  entity's column to another entity's key, says that an order belongs to a customer. Cardinality
  is claimed only when the key is known to be unique.
- **Rules** are what the build holds the ontology to; a violation is a build warning, so
  ``konstruct build --strict`` fails on it.

``konstruct build`` writes the result to ``ontology.yml`` beside the graph.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlglot
import yaml
from sqlglot import exp

from konstruct.core import jsonio
from konstruct.core.graph import RELATION_KINDS, Graph, Node
from konstruct.core.sql import PARSE_ERRORS

if TYPE_CHECKING:
    from konstruct.interfaces.layout import Style

ONTOLOGY_VERSION = 1

# An identifier suffix: ``_id``, ``_key``, ``_pk`` or ``_sk`` in any case, or camelCase ``Id``/``Key``.
# A bare lower-case ``id`` ending a word (``paid``, ``valid``, ``uuid``) is not one.
_ID_SUFFIX = re.compile(
    r'(?:(?i:_(?:id|key|pk|sk|uuid|guid))|(?<=[a-z0-9])(?:Id|ID|Key|Uuid|UUID|Guid|GUID))$'
)
_ID_WORDS = frozenset({'id', 'key', 'pk', 'sk', 'uuid', 'guid', 'oid', 'rowid'})
_LAYER_PREFIX = re.compile(r'^(?:stg|int|dim|fct|fact|raw|base|src|mart|rpt)_', re.IGNORECASE)
_GRAIN = re.compile(r'one row per (?:each )?([a-z][a-z0-9 _-]*)', re.IGNORECASE)
# Words that qualify a grain rather than name its entity: one row per order *per day*.
_GRAIN_END = re.compile(r'\s+(?:per|and|by|for|in|with|on|at)\s.*$', re.IGNORECASE)


@dataclass
class Entity:
    """A business object, such as a customer or an order."""

    name: str
    """The entity's name, singular and lower case (``customer``)."""
    key: str
    """The name of the column that identifies one instance (``customer_id``); when relations name
    it differently, the most common name."""
    keys: list[str] = field(default_factory=list)
    """The key column of each relation, by label (``customers.customer_id``)."""
    relations: list[str] = field(default_factory=list)
    """The relations that hold one row per instance, by label."""
    evidence: list[str] = field(default_factory=list)
    """Why it is an entity: the tests, primary keys, identifier fields, terms and grains that say so."""
    term: str | None = None
    """The business term that names it, if one does."""
    description: str | None = None
    """The term's definition, if one names it."""
    key_ids: list[str] = field(default_factory=list, repr=False)
    """The graph ids of the key columns, for tools; not written to ``ontology.yml``."""


@dataclass
class MeasureColumn:
    """One column that holds a measure, with its formula and how far to trust it."""

    column: str
    """The column, by label (``customers.customer_lifetime_value``)."""
    formula: str | None
    """The column's formula composed down to source columns, if it could be composed."""
    sources: list[str] = field(default_factory=list)
    """The source columns the formula reaches."""
    complete: bool = True
    """False when some column on the way had no expression, so the formula stops short of source."""
    verified: int = 0
    """Lineage edges on the way to source that ``konstruct verify`` confirmed."""
    edges: int = 0
    """Lineage edges on the way to source."""
    usage: int | None = None
    """The column's usage score from query history (0 to 100), when usage is on."""
    id: str | None = field(default=None, repr=False)
    """The column's graph id, for tools; not written to ``ontology.yml``."""


@dataclass
class Measure:
    """A business term with a formula: what is measured, how, and on whose authority."""

    name: str
    """The term's name."""
    definition: str | None = None
    """The agreed wording."""
    status: str = 'draft'
    """``draft``, ``agreed``, ``approved`` or ``retired``."""
    owner: str | None = None
    """Who owns the definition."""
    approved: str | None = None
    """When it was approved."""
    entity: str | None = None
    """The entity its grain names (``one row per customer`` gives ``customer``)."""
    grain: str | None = None
    """The term's grain as written."""
    unit: str | None = None
    """Its unit, such as ``currency``."""
    currency: str | None = None
    """Its currency, for a monetary measure."""
    filters: list[str] = field(default_factory=list)
    """Filters any answer must apply."""
    columns: list[MeasureColumn] = field(default_factory=list)
    """The columns that hold it, each with its formula."""
    path: str | None = None
    """The file that defines it, with the line."""
    expression: str | None = None
    """For a measure defined as a metric in an Apache Ossie model, the metric's own expression; its
    columns are then the columns the expression reads."""


@dataclass
class Relationship:
    """One entity referring to another, such as an order belonging to a customer."""

    source: str
    """The entity that refers (``order``)."""
    target: str
    """The entity referred to (``customer``)."""
    via: str
    """The columns that join them (``orders.customer_id = customers.customer_id``)."""
    cardinality: str | None = None
    """``many-to-one``, or ``one-to-one`` when both columns are unique; left out when the target
    column is not known to be unique, so the relationship is claimed without its cardinality."""
    tested: bool = False
    """Whether a dbt ``relationships`` test vouches for it, rather than only a join."""
    evidence: list[str] = field(default_factory=list)
    """The tests and joins that show it."""
    columns: tuple[str, str] | None = field(default=None, repr=False)
    """The graph ids of the joining columns in ``via``, for tools; not written to ``ontology.yml``."""


@dataclass
class Concept:
    """A business term in the concept hierarchy: what it is a kind of, and what are kinds of it."""

    name: str
    """The term's name."""
    broader: list[str] = field(default_factory=list)
    """The wider concepts it is a kind of."""
    narrower: list[str] = field(default_factory=list)
    """The concepts that are kinds of it."""


@dataclass
class Rule:
    """A rule the build holds the ontology to, with what breaks it now."""

    name: str
    """A short identifier (``one-formula-per-measure``)."""
    description: str
    """What the rule requires."""
    violations: list[str] = field(default_factory=list)
    """What breaks it in this build; each is also a build warning."""


@dataclass
class Ontology:
    """Entities, measures, relationships and rules, derived from one graph."""

    entities: list[Entity] = field(default_factory=list)
    """The business objects."""
    measures: list[Measure] = field(default_factory=list)
    """The business terms with a formula."""
    relationships: list[Relationship] = field(default_factory=list)
    """How the entities refer to each other."""
    rules: list[Rule] = field(default_factory=list)
    """The rules and their violations."""
    concepts: list[Concept] = field(default_factory=list)
    """The concept hierarchy: each term with a broader or narrower term."""

    @property
    def violations(self) -> list[str]:
        """Every rule violation, each prefixed with its rule."""
        return [f'{rule.name}: {v}' for rule in self.rules for v in rule.violations]

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import ontology_lines, show

        return show(ontology_lines, self, style)

    def __str__(self) -> str:
        return self.text()

    def to_dict(self) -> dict[str, Any]:
        return {
            'version': ONTOLOGY_VERSION,
            'entities': [_compact(asdict(e)) for e in self.entities],
            'measures': [
                {
                    **_compact({k: v for k, v in asdict(m).items() if k != 'columns'}),
                    'columns': [_compact(asdict(c)) for c in m.columns],
                }
                for m in self.measures
            ],
            'relationships': [_compact(asdict(r)) for r in self.relationships],
            **({'concepts': [_compact(asdict(c)) for c in self.concepts]} if self.concepts else {}),
            'rules': [
                {'name': r.name, 'description': r.description, 'violations': r.violations} for r in self.rules
            ],
        }

    def to_yaml(self) -> str:
        """The ontology as the YAML written to ``ontology.yml``."""
        header = '# Derived by Konstruct from the data stack; regenerated on every build.\n'
        return header + yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True, width=100)

    def write(self, path: Path) -> Path:
        """Write the ontology to ``path`` as YAML and return ``path``."""
        jsonio.write_text(path, self.to_yaml())
        return path

    def entity(self, name: str) -> Entity | None:
        """The entity called ``name``, or named by the term ``name``; case, spaces, hyphens and
        underscores are not told apart, and a plural finds the singular."""
        return next((e for e in self.entities if name_matches(name, e.name, e.term)), None)

    def measure(self, name: str) -> Measure | None:
        """The measure called ``name``, matched as :meth:`entity` matches."""
        return next((m for m in self.measures if name_matches(name, m.name)), None)


def name_matches(wanted: str, *names: str | None) -> bool:
    """Whether ``wanted`` names any of ``names``, ignoring case, the difference between spaces,
    hyphens and underscores, and a plural."""
    target = _normal(wanted)
    return any(n is not None and _normal(n) in (target, _singular(target)) for n in names)


def _normal(name: str) -> str:
    return re.sub(r'[\s_-]+', ' ', name.strip().lower())


_INTERNAL = frozenset({'key_ids', 'id', 'columns'})


def _compact(data: dict[str, Any]) -> dict[str, Any]:
    """Leave out empty and default values and graph ids, so the YAML stays readable."""
    return {
        k: v
        for k, v in data.items()
        if k not in _INTERNAL and v not in (None, [], '') and not (k == 'complete' and v)
    }


def id_like(column: str) -> bool:
    """Whether ``column`` is named like an identifier: ``id``, ``uuid``, ``customer_id``,
    ``order_key``, ``CategoryId``."""
    column = column.rsplit('.', 1)[-1]
    return column.lower() in _ID_WORDS or bool(_ID_SUFFIX.search(column))


def entity_name(column: str, relation: str | None = None) -> str:
    """The entity a key column identifies: ``customer_id`` gives ``customer``; a bare identifier
    (``id``, ``uuid``), or a key not named like one, takes the relation's name without its layer
    prefix (``stg_customers.id`` gives ``customer``)."""
    column = column.rsplit('.', 1)[-1]  # a field nested in a struct is named by its own name
    base = '' if column.lower() in _ID_WORDS else _ID_SUFFIX.sub('', column).strip('_')
    if base == column and not id_like(column):
        base = ''
    if not base and relation:
        table = relation.rsplit('.', 1)[-1]
        # stg_stripe__payments: a staging model's source__table name puts the table after the last
        # __; elsewhere (int_orders__pivoted) what follows __ is a variant, not the object.
        if re.match(r'(?:stg|base|src|raw)_', table, re.IGNORECASE) and '__' in table:
            table = table.rsplit('__', 1)[-1]
        base = _LAYER_PREFIX.sub('', table)
    if not base:
        return column.lower()
    snake = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', base).lower().replace('-', '_')
    return _singular(snake)


def _singular(word: str) -> str:
    if word.endswith('ies') and len(word) > 4:
        return word[:-3] + 'y'
    if word.endswith(('sses', 'ches', 'shes', 'xes')):
        return word[:-2]
    if word.endswith('s') and not word.endswith(('ss', 'us', 'is')):
        return word[:-1]
    return word


def derive(graph: Graph) -> Ontology:
    """The ontology of ``graph`` (see the module documentation)."""
    return _Deriver(graph).run()


class _Deriver:
    """Derives in two passes: every entity first (keys, then the terms that name them, then the
    grains that find more), so that measures and relationships see the final entity names."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        self.keys: dict[str, str] = {}  # column id of a key -> entity name
        self.entities: dict[str, Entity] = {}
        self.unique: set[str] = set()  # column ids with a uniqueness guarantee
        self.terms = sorted(
            (n for n in graph.nodes.values() if n.kind == 'term'), key=lambda n: (n.name.lower(), n.id)
        )
        self.kinds: dict[str, str] = {}
        self.declared: dict[str, str] = {}  # entity name -> the declared term that named it
        self.rules = {
            'one-formula-per-measure': Rule(
                'one-formula-per-measure',
                'Every column bound to a measure computes it the same way.',
            ),
            'entity-keys-are-unique': Rule(
                'entity-keys-are-unique',
                'The key of an entity named in definitions.yml has a uniqueness test, a primary key '
                'or is an identifier field.',
            ),
            'entity-names-are-distinct': Rule(
                'entity-names-are-distinct',
                'A term of kind entity does not give its name to an entity whose keys it does not bind.',
            ),
            'bound-columns-exist': Rule(
                'bound-columns-exist',
                'Every column a business term names exists in the data stack.',
            ),
            'approved-terms-are-owned': Rule(
                'approved-terms-are-owned',
                'An approved term has an owner and an approval date.',
            ),
        }

    def run(self) -> Ontology:
        self.find_keys()
        for term in self.terms:
            self.kinds[term.id] = self.term_kind(term)
        for term in self.terms:
            if self.kinds[term.id] == 'entity':
                self.name_entity(term, declared=term.attrs.get('kind') == 'entity')
        for term in self.terms:
            if self.kinds[term.id] == 'measure':
                self.entity_from_grain(term)
        measures = [self.measure(t) for t in self.terms if self.kinds[t.id] == 'measure']
        self.carry_warnings()
        entities = sorted(self.entities.values(), key=lambda e: e.name)
        for entity in entities:
            entity.relations = sorted(set(entity.relations))
            entity.keys = sorted(set(entity.keys))
            entity.key_ids = sorted(set(entity.key_ids))
            entity.evidence = sorted(set(entity.evidence))
            names = [self.graph.nodes[i].name.lower() for i in entity.key_ids if i in self.graph.nodes]
            names = names or [entity.key]
            entity.key = min(set(names), key=lambda n: (-names.count(n), n))
        relationships = self.relationships()
        for rule in self.rules.values():
            rule.violations.sort()
        return Ontology(
            entities=entities,
            measures=measures,
            relationships=relationships,
            rules=list(self.rules.values()),
            concepts=self.concepts(),
        )

    def concepts(self) -> list[Concept]:
        """The concept hierarchy, from the ``broader`` terms each term names."""
        found: dict[str, Concept] = {}
        for term in self.terms:
            for edge in self.graph.out_edges(term.id, 'relates_to'):
                wider = self.graph.nodes.get(edge.target)
                if edge.attrs.get('role') != 'broader' or wider is None:
                    continue
                found.setdefault(term.name, Concept(term.name)).broader.append(wider.name)
                found.setdefault(wider.name, Concept(wider.name)).narrower.append(term.name)
        for concept in found.values():
            concept.broader.sort()
            concept.narrower.sort()
        return [found[name] for name in sorted(found)]

    # Entities

    def bound_columns(self, term: Node) -> list[Node]:
        graph = self.graph
        return [
            graph.nodes[e.target]
            for e in graph.out_edges(term.id, 'defines')
            if e.target in graph.nodes and graph.nodes[e.target].kind == 'column'
        ]

    def find_keys(self) -> None:
        graph = self.graph
        found: dict[str, list[str]] = {}
        for node in graph.nodes.values():
            if node.kind == 'test' and node.attrs.get('test') == 'unique':
                for edge in graph.out_edges(node.id, 'tests'):
                    column = graph.nodes.get(edge.target)
                    if edge.attrs.get('role') is None and column is not None and column.kind == 'column':
                        self.unique.add(column.id)
                        # A unique email or name is not what identifies the business object.
                        if id_like(column.name):
                            found.setdefault(column.id, []).append(f'unique test on {graph.label(column)}')
            elif node.kind == 'column' and node.attrs.get('ossie_key'):
                self.unique.add(node.id)
                found.setdefault(node.id, []).append(f'primary key in {node.attrs["ossie_key"]}')
            elif node.kind in RELATION_KINDS:
                for attr, what in (('primary_key', 'primary key'), ('identifier_fields', 'identifier field')):
                    names = node.attrs.get(attr) or []
                    # One column of a composite key is not unique on its own.
                    if not isinstance(names, list) or len(names) != 1:
                        continue
                    key_column = graph.column(node.id, str(names[0]))
                    if key_column is not None:
                        self.unique.add(key_column.id)
                        found.setdefault(key_column.id, []).append(f'{what} {graph.label(key_column)}')
        for column_id in sorted(found):
            for evidence in found[column_id]:
                self.add_key(graph.nodes[column_id], evidence)

    def add_key(self, column: Node, evidence: str, name: str | None = None) -> Entity:
        owner = self.graph.parent_of(column)
        relation = self.graph.label(owner) if owner else None
        name = self.keys.get(column.id) or name or entity_name(column.name, relation)
        entity = self.entities.setdefault(name, Entity(name=name, key=column.name.lower()))
        if relation:
            entity.relations.append(relation)
        entity.keys.append(self.graph.label(column))
        entity.key_ids.append(column.id)
        entity.evidence.append(evidence)
        self.keys[column.id] = name
        return entity

    def merge(self, old: str, new: str) -> None:
        """Rename entity ``old`` to ``new``, merging it into ``new`` if that exists."""
        if old == new:
            return
        entity = self.entities.pop(old)
        target = self.entities.get(new)
        if target is None:
            entity.name = new
            self.entities[new] = entity
        else:
            target.relations += entity.relations
            target.keys += entity.keys
            target.key_ids += entity.key_ids
            target.evidence += entity.evidence
        for column_id, value in self.keys.items():
            if value == old:
                self.keys[column_id] = new

    def name_entity(self, term: Node, *, declared: bool) -> None:
        graph = self.graph
        where = _where(term)
        columns = self.bound_columns(term)
        if not columns:
            return
        bound = sorted({self.keys[c.id] for c in columns if c.id in self.keys})
        if not declared:
            # An inferred entity term names what is already there; it never renames it.
            for name in bound:
                entity = self.entities[name]
                if entity.term is None:
                    entity.term, entity.description = term.name, term.description
                entity.evidence.append(f'term {term.name!r} ({where})')
            return
        name = _singular(_normal(term.name).replace(' ', '_'))
        if name in self.entities and name not in bound:
            other = self.entities[name]
            self.rules['entity-names-are-distinct'].violations.append(
                f'term {term.name!r} ({where}) names {name!r}, which is already the entity keyed by '
                f'{", ".join(sorted(set(other.keys)))}; bind those columns too, or name it differently'
            )
            return
        renamed = [e for e in bound if e in self.declared and self.declared[e] != term.name]
        if renamed:
            self.rules['entity-names-are-distinct'].violations.append(
                f'term {term.name!r} ({where}) would rename {renamed[0]!r}, which term '
                f'{self.declared[renamed[0]]!r} already names, to {name!r}; one entity takes one name'
            )
            return
        for existing in bound:
            self.merge(existing, name)
        for column in columns:
            if column.id in self.keys:
                continue
            self.add_key(column, f'term {term.name!r} ({where})', name)
            # A pack's key is the source's own; the project has nothing to test, but nothing has
            # tested it either, so it is not counted as unique.
            if column.id not in self.unique and not term.attrs.get('registry'):
                self.rules['entity-keys-are-unique'].violations.append(
                    f'{graph.label(column)}, the key of {name!r} ({where}), has no uniqueness test'
                )
        entity = self.entities[name]
        entity.term, entity.description = term.name, term.description
        entity.evidence.append(f'term {term.name!r} ({where})')
        self.declared[name] = term.name

    def resolve(self, name: str) -> str | None:
        """The entity that a grain's or a request's ``name`` refers to."""
        found = next((e for e in self.entities.values() if name_matches(name, e.name, e.term)), None)
        return found.name if found else None

    def grain_entity(self, term: Node) -> str | None:
        grain = term.attrs.get('grain')
        match = _GRAIN.search(grain) if isinstance(grain, str) else None
        if not match:
            return None
        return _singular(_normal(_GRAIN_END.sub('', match[1])).replace(' ', '_'))

    def entity_from_grain(self, term: Node) -> None:
        """``one row per customer`` on a measure says that the relations holding it have one row
        per customer, so an identifier there named for customer is that entity's key."""
        wanted = self.grain_entity(term)
        if wanted is None or self.resolve(wanted) is not None:
            return
        graph = self.graph
        for column in self.bound_columns(term):
            relation = graph.parent_of(column)
            if relation is None:
                continue
            label = graph.label(relation)
            for candidate in graph.columns_of(relation.id):
                if id_like(candidate.name) and entity_name(candidate.name, label) == wanted:
                    self.add_key(candidate, f'grain of {term.name!r} ({_where(term)})', wanted)
                    break

    def term_kind(self, term: Node) -> str:
        declared = term.attrs.get('kind')
        if declared in ('entity', 'measure', 'term'):
            return str(declared)
        columns = self.bound_columns(term)
        if columns and all(c.id in self.keys for c in columns):
            return 'entity'
        if any(_computed(self.graph, c) for c in columns):
            return 'measure'
        return 'term'

    # Measures

    def measure(self, term: Node) -> Measure:
        from konstruct.analysis.conflicts import measure_conflicts
        from konstruct.analysis.explain import Explainer

        graph = self.graph
        explainer = Explainer(graph)
        owner = term.attrs.get('owner')
        grain = term.attrs.get('grain')
        wanted = self.grain_entity(term)
        result = Measure(
            name=term.name,
            definition=term.description,
            status=str(term.attrs.get('status', 'draft')),
            owner=_owner(owner),
            approved=term.attrs.get('approved'),
            entity=self.resolve(wanted) if wanted else None,
            grain=grain if isinstance(grain, str) else None,
            unit=term.attrs.get('unit'),
            currency=term.attrs.get('currency'),
            filters=list(term.attrs.get('filters', [])),
            path=_where(term),
            expression=term.attrs.get('formula'),
        )
        columns = self.bound_columns(term)
        for column in columns:
            composed = explainer.compose(column) if isinstance(column.attrs.get('expression'), str) else None
            verified, edges = _verification(graph, column)
            usage = column.attrs.get('usage')
            result.columns.append(
                MeasureColumn(
                    column=graph.label(column),
                    formula=explainer.composed_text(column) if composed is not None else None,
                    sources=sorted(graph.label(graph.nodes[s]) for s in composed.sources) if composed else [],
                    complete=composed.complete if composed else False,
                    verified=verified,
                    edges=edges,
                    usage=int(usage['score']) if isinstance(usage, dict) and 'score' in usage else None,
                    id=column.id,
                )
            )
        # A metric's columns are its inputs, not columns that each hold the measure.
        compared = [] if result.expression else columns
        result.columns.sort(key=lambda c: c.column)
        for conflict in measure_conflicts(graph, term.name, compared):
            differences = '; '.join(f'{d.kind} {d.detail}' for d in conflict.differences[:3])
            self.rules['one-formula-per-measure'].violations.append(
                f'{term.name!r} ({result.path}): {conflict.a.label} and {conflict.b.label} differ ({differences})'
            )
        return result

    # Relationships

    def relationships(self) -> list[Relationship]:
        graph = self.graph
        # (source entity, target entity, column names) -> candidate joins and their evidence
        found: dict[tuple[str, str, str], tuple[list[tuple[bool, str, str, str]], set[str]]] = {}

        def add(source_col: Node, target_col: Node, evidence: str, tested: bool) -> None:
            source_rel = graph.parent_of(source_col)
            target_entity = self.keys.get(target_col.id)
            source_entity = self.entity_of(source_rel) if source_rel else None
            if target_entity is None or source_entity is None or source_entity == target_entity:
                return
            via = f'{graph.label(source_col)} = {graph.label(target_col)}'
            key = (source_entity, target_entity, f'{source_col.name.lower()}={target_col.name.lower()}')
            candidates, evidences = found.setdefault(key, ([], set()))
            candidates.append((tested, via, source_col.id, target_col.id))
            evidences.add(evidence)

        for node in graph.nodes.values():
            if node.kind == 'test' and node.attrs.get('test') == 'relationships':
                edges = graph.out_edges(node.id, 'tests')
                targets = [
                    graph.nodes[e.target] for e in edges if e.attrs.get('role') == 'relationship target'
                ]
                sources = [graph.nodes[e.target] for e in edges if e.attrs.get('role') is None]
                for source in sources:
                    for target in targets:
                        add(source, target, f'relationships test {node.name}', True)
            elif node.kind in RELATION_KINDS:
                for condition in node.attrs.get('conditions', []) or []:
                    for left, right in self.join_columns(str(condition)):
                        evidence = f'join in {graph.label(node)}'
                        if right.id in self.keys:
                            add(left, right, evidence, False)
                        if left.id in self.keys:
                            add(right, left, evidence, False)
        for item in graph.meta.get('ossie_relationships', []) or []:
            many, one = graph.nodes.get(item.get('from', '')), graph.nodes.get(item.get('to', ''))
            if many is not None and one is not None:
                add(many, one, f'Ossie relationship {item.get("name")} ({item.get("path")})', False)
        for item in graph.meta.get('pack_relationships', []) or []:
            many, one = graph.nodes.get(item.get('from', '')), graph.nodes.get(item.get('to', ''))
            if many is not None and one is not None:
                add(many, one, f'ontology pack {item.get("name")}', False)
        result = []
        for (source_entity, target_entity, _), (candidates, evidences) in found.items():
            # The columns a relationships test vouches for come first, then the shortest join.
            tested, via, source_id, target_id = min(candidates, key=lambda c: (not c[0], len(c[1]), c[1]))
            cardinality = None
            if target_id in self.unique:
                cardinality = 'one-to-one' if source_id in self.unique else 'many-to-one'
            result.append(
                Relationship(
                    source_entity,
                    target_entity,
                    via,
                    cardinality,
                    tested,
                    sorted(evidences),
                    columns=(source_id, target_id),
                )
            )
        return sorted(result, key=lambda r: (r.source, r.target, r.via))

    def entity_of(self, relation: Node) -> str | None:
        """The entity whose instances ``relation`` holds one row each of: of its key columns, the
        one named for the relation, else the first by name."""
        names = sorted({self.keys[c.id] for c in self.graph.columns_of(relation.id) if c.id in self.keys})
        if not names:
            return None
        own = entity_name('id', self.graph.label(relation))
        return own if own in names else names[0]

    def join_columns(self, condition: str) -> list[tuple[Node, Node]]:
        label, sep, text = condition.partition(': ')
        if not sep or 'JOIN' not in label:
            return []
        dialect = self.graph.meta.get('dialect')
        try:
            tree = sqlglot.parse_one(text, read=dialect if isinstance(dialect, str) else None)
        except PARSE_ERRORS:
            return []
        pairs = []
        for eq in tree.find_all(exp.EQ):
            if isinstance(eq.this, exp.Column) and isinstance(eq.expression, exp.Column):
                left, right = self.column(eq.this), self.column(eq.expression)
                if left is not None and right is not None:
                    pairs.append((left, right))
        return pairs

    def column(self, column: exp.Column) -> Node | None:
        parts = [p.name for p in column.parts]
        for split in range(len(parts) - 1, 0, -1):
            relation = self.graph.node_for_relation('.'.join(parts[:split]))
            if relation is not None:
                return self.graph.column(relation.id, '.'.join(parts[split:]))
        return None

    # Rules carried from the build

    def carry_warnings(self) -> None:
        # Another repository's warnings carry its name first (``marketing: definitions: ...``).
        others = {
            f'{n.attrs["repository"]}: definitions: '
            for n in self.graph.nodes.values()
            if n.attrs.get('repository')
        }
        prefixes = ['definitions: ', *sorted(others)]
        for warning in self.graph.warnings:
            prefix = next((p for p in prefixes if warning.startswith(p)), None)
            if prefix is None:
                continue
            text = warning[len(prefix) :]
            if prefix != 'definitions: ':
                text = f'{prefix.split(": ", 1)[0]}: {text}'
            if ': bound column ' in text:
                self.rules['bound-columns-exist'].violations.append(text)
            elif 'approved but has no ' in text:
                self.rules['approved-terms-are-owned'].violations.append(text)


def _computed(graph: Graph, column: Node) -> bool:
    """Whether a column computes its value rather than copying one."""
    inputs = graph.out_edges(column.id, 'derived_from')
    return bool(inputs) and any(e.attrs.get('transform') != 'direct' for e in inputs)


def _verification(graph: Graph, column: Node) -> tuple[int, int]:
    """Confirmed and total lineage edges from ``column`` back to source."""
    seen: set[str] = set()
    stack = [column.id]
    verified = total = 0
    while stack:
        for edge in graph.out_edges(stack.pop(), 'derived_from'):
            total += 1
            verified += bool(edge.attrs.get('verified'))
            if edge.target not in seen:
                seen.add(edge.target)
                stack.append(edge.target)
    return verified, total


def _owner(value: Any) -> str | None:
    if isinstance(value, dict) and value.get('name'):
        role = value.get('role')
        return f'{value["name"]} ({role})' if role else str(value['name'])
    return None


def _where(term: Node) -> str:
    line = term.attrs.get('line')
    return f'{term.path}:{line}' if term.path and line else term.path or term.id
