"""The ontology as OWL 2 in Turtle, for tools that read RDF.

- Each **entity** is an ``owl:Class``, labelled with its name and the term that names it, defined by
  the term's definition, with its key as ``owl:hasKey`` when the ontology knows it to be unique,
  and the relations that hold it and the evidence for it as annotations.
- Each **property** (a column of a relation that holds the entity, one row per instance) is an
  ``owl:DatatypeProperty`` of the entity's class, with a range from the OWL 2 datatype map where
  every column of that name has the same known type, its description, and the columns it is read
  from. The map has no date or time datatype, so a date or time property has no range.
- Each **relationship** is an ``owl:ObjectProperty`` from one class to the other: functional for a
  many-to-one relationship, and inverse functional as well for a one-to-one one; the columns that
  join them, whether a test vouches for it and its evidence are annotations.
- Each **measure** is annotated: a named individual with its definition, synonyms, formula
  composed down to source columns, grain, unit, currency, filters, status, owner and columns, and
  its entity's class names it with ``konstruct:measure``.
- A broader term that names another entity's class is its ``rdfs:subClassOf``.

IRIs are stable: a class, property or measure is named by the ontology IRI (``urn:konstruct:``
and the project's name, unless the export is given one) and its own name, and an entity or measure
whose term was read from a SKOS or OWL vocabulary keeps that concept's IRI. Konstruct's own
annotation properties are declared in the file, under ``https://kodexlabsio.github.io/konstruct/vocabulary#``.

Left out, and said so at the top of the file: the columns of relations that hold no entity (they
appear only in measure formulas), a key not known to be unique, the ranges of date and time
properties, the ontology's rules and their violations, and broader terms that are not both
entities.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import quote

from konstruct.analysis.explain import Explainer
from konstruct.core.graph import Graph, Node
from konstruct.outputs.compiled import compile_ontology
from konstruct.semantics.ontology import Entity, Measure, Ontology
from konstruct.semantics.ossie import composed_column, composed_reason, measure_tree, placeholder
from konstruct.semantics.rows import Rows

VOCABULARY = 'https://kodexlabsio.github.io/konstruct/vocabulary#'
PREFIXES = {
    'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
    'rdfs': 'http://www.w3.org/2000/01/rdf-schema#',
    'owl': 'http://www.w3.org/2002/07/owl#',
    'xsd': 'http://www.w3.org/2001/XMLSchema#',
    'skos': 'http://www.w3.org/2004/02/skos/core#',
    'konstruct': VOCABULARY,
}
# Ossie's logical types, which name a column's physical type in any dialect, as the XML Schema
# types of the OWL 2 datatype map (https://www.w3.org/TR/owl2-syntax/#Datatype_Maps). The map has no
# xsd:date or xsd:time, so a date or time has no range.
_UNMAPPED = frozenset({'Date', 'Time'})
_XSD = {
    'String': 'xsd:string',
    'Integer': 'xsd:integer',
    'Decimal': 'xsd:decimal',
    'Float': 'xsd:double',
    'Boolean': 'xsd:boolean',
    'DateTime': 'xsd:dateTime',
    'DateTimeTz': 'xsd:dateTimeStamp',
}
ANNOTATIONS = {
    'relation': 'A relation that holds one row per instance of the class.',
    'column': 'A physical column the property is read from.',
    'evidence': 'A test, key, join or term that shows the class or relationship.',
    'via': 'The columns that join the two classes of a relationship.',
    'cardinality': 'many-to-one or one-to-one.',
    'tested': 'Whether a dbt relationships test vouches for the relationship.',
    'measure': 'A measure of the class.',
    'entity': 'The class a measure is measured at the grain of.',
    'formula': "The measure's formula, composed down to source columns.",
    'grain': "The measure's grain, as written.",
    'unit': "The measure's unit.",
    'currency': "The measure's currency.",
    'filter': 'A filter any answer for the measure must apply.',
    'status': 'draft, agreed, approved or retired.',
    'owner': 'Who owns the definition.',
    'approved': 'When the definition was approved.',
}


def export(graph: Graph, ontology: Ontology, name: str | None = None, iri: str | None = None) -> str:
    """The ontology as OWL 2 in Turtle. ``iri`` is the ontology's IRI (default
    ``urn:konstruct:<name>``); the classes, properties and measures are named under it."""
    from konstruct.outputs.export import ExportError

    if not ontology.entities and not ontology.measures:
        raise ExportError('nothing to export: the ontology has no entities or measures')
    if iri is not None and not _IRI.fullmatch(iri):
        raise ExportError(f'{iri!r} is not an IRI; give an absolute one, such as https://example.com/shop')
    title = name or 'konstruct'
    base = iri or f'urn:konstruct:{quote(title, safe="")}'
    namespace = base if base.endswith(('#', '/')) else base + '#'
    writer = _Writer(namespace)
    left_out: list[str] = []

    classes: dict[str, str] = {}  # entity name -> class IRI
    by_term: dict[str, str] = {}  # term name -> class IRI
    for entity in ontology.entities:
        term = _term(graph, entity.term)
        own = term.attrs.get('iri') if term is not None else None
        classes[entity.name] = writer.iri(_camel(entity.name), own if isinstance(own, str) else None)
        if entity.term:
            by_term[entity.term] = classes[entity.name]
    measures: dict[str, str] = {}
    for measure in ontology.measures:
        term = _term(graph, measure.name)
        own = term.attrs.get('iri') if term is not None else None
        measures[measure.name] = writer.iri(
            f'measure/{_snake(measure.name)}', own if isinstance(own, str) else None
        )

    # The tables the platform exports write that hold no entity: here only measure formulas name them.
    others = [t.source for t in compile_ontology(graph, ontology, name).tables if t.entity is None]
    if others:
        left_out.append(f'the columns of {", ".join(others)}, which hold no entity')
    if any(rule.violations for rule in ontology.rules):
        left_out.append('the rules and their violations (konstruct ontology lists them)')

    blocks = [_ontology_block(base, title)]
    blocks += [_annotation(key, comment) for key, comment in ANNOTATIONS.items()]
    timed: list[str] = []  # the properties of a date or time type, which have no range
    for entity in ontology.entities:
        properties, dated = _properties(graph, entity)
        timed += [f'{entity.name}.{column}' for column in sorted(dated)]
        names = {column: writer.iri(f'{entity.name}.{column}') for column in properties}
        key = [names[entity.key]] if entity.key in names and entity.unique_ids else []
        if entity.key in names and not entity.unique_ids:
            left_out.append(f'the key of {entity.name!r}: it is not known to be unique')
        broader = []
        for concept in ontology.concepts:
            if concept.name == entity.term:
                for wider in concept.broader:
                    if wider in by_term:
                        broader.append(by_term[wider])
                    else:
                        left_out.append(f'{entity.term!r} is a kind of {wider!r}, which is not an entity')
        statements = [
            ('a', ['owl:Class']),
            ('rdfs:label', [_literal(entity.name)]),
            ('skos:prefLabel', [_literal(entity.term)] if entity.term else []),
            ('skos:definition', [_literal(entity.description)] if entity.description else []),
            ('rdfs:subClassOf', broader),
            ('owl:hasKey', [f'( {key[0]} )'] if key else []),
            ('konstruct:relation', [_literal(r) for r in entity.relations]),
            ('konstruct:evidence', [_literal(e) for e in entity.evidence]),
            (
                'konstruct:measure',
                [measures[m.name] for m in ontology.measures if m.entity == entity.name],
            ),
        ]
        blocks.append(_block(classes[entity.name], statements))
        for column, (labels, types, descriptions) in properties.items():
            range_ = sorted(types)
            blocks.append(
                _block(
                    names[column],
                    [
                        ('a', ['owl:DatatypeProperty']),
                        ('rdfs:label', [_literal(column)]),
                        ('rdfs:domain', [classes[entity.name]]),
                        ('rdfs:range', range_ if len(range_) == 1 else []),
                        ('rdfs:comment', [_literal(d) for d in sorted(descriptions)]),
                        ('konstruct:column', [_literal(label) for label in labels]),
                    ],
                )
            )
    if timed:
        left_out.append(f'the ranges of {", ".join(timed)}: OWL 2 has no date or time datatype')
    for rel in ontology.relationships:
        kinds = ['owl:ObjectProperty']
        if rel.cardinality in ('many-to-one', 'one-to-one'):
            kinds.append('owl:FunctionalProperty')
        if rel.cardinality == 'one-to-one':
            kinds.append('owl:InverseFunctionalProperty')
        blocks.append(
            _block(
                writer.iri(f'{rel.source}_to_{rel.target}'),
                [
                    ('a', kinds),
                    ('rdfs:label', [_literal(f'{rel.source} to {rel.target}')]),
                    ('rdfs:domain', [classes[rel.source]]),
                    ('rdfs:range', [classes[rel.target]]),
                    ('konstruct:via', [_literal(rel.via)]),
                    ('konstruct:cardinality', [_literal(rel.cardinality)] if rel.cardinality else []),
                    ('konstruct:tested', ['true' if rel.tested else 'false']),
                    ('konstruct:evidence', [_literal(e) for e in rel.evidence]),
                ],
            )
        )
    explainer, rows = Explainer(graph, qualified=True), Rows(graph)
    for measure in ontology.measures:
        term = _term(graph, measure.name)
        synonyms = [str(s) for s in term.attrs.get('synonyms', [])] if term is not None else []
        formula, why = _formula(graph, explainer, rows, measure)
        blocks.append(
            _block(
                measures[measure.name],
                [
                    ('a', ['owl:NamedIndividual']),
                    ('rdfs:label', [_literal(measure.name)]),
                    ('skos:definition', [_literal(measure.definition)] if measure.definition else []),
                    ('skos:altLabel', [_literal(s) for s in synonyms]),
                    ('konstruct:entity', [classes[measure.entity]] if measure.entity in classes else []),
                    ('konstruct:formula', [_literal(formula)] if formula else []),
                    ('konstruct:grain', [_literal(measure.grain)] if measure.grain else []),
                    ('konstruct:unit', [_literal(measure.unit)] if measure.unit else []),
                    ('konstruct:currency', [_literal(measure.currency)] if measure.currency else []),
                    ('konstruct:filter', [_literal(f) for f in measure.filters]),
                    ('konstruct:status', [_literal(measure.status)]),
                    ('konstruct:owner', [_literal(measure.owner)] if measure.owner else []),
                    ('konstruct:approved', [_literal(str(measure.approved))] if measure.approved else []),
                    ('konstruct:column', [_literal(c.column) for c in measure.columns]),
                ],
            )
        )
        if why is not None:
            left_out.append(f'the formula of {measure.name!r}: {why}')

    lines = [f'@prefix {prefix}: <{iri_}> .' for prefix, iri_ in PREFIXES.items()]
    lines += ['', f'# The ontology of {title} as OWL 2, written by Konstruct.']
    if left_out:
        lines.append('# Left out:')
        lines += [f'# - {item}' for item in dict.fromkeys(left_out)]
    return '\n'.join(lines) + '\n\n' + '\n\n'.join(blocks) + '\n'


class _Writer:
    """Names under one namespace, each local name used once."""

    def __init__(self, namespace: str) -> None:
        self.namespace = namespace
        self.used: set[str] = set()

    def iri(self, local: str, own: str | None = None) -> str:
        if own and _IRI.fullmatch(own):
            return f'<{own}>'
        candidate, index = local, 2
        while candidate in self.used:
            candidate = f'{local}_{index}'
            index += 1
        self.used.add(candidate)
        return f'<{self.namespace}{quote(candidate, safe="/._-")}>'


# An absolute IRI that Turtle writes between angle brackets as it is.
_IRI = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*:[^\s<>"{}|^`\\]*')


def _ontology_block(base: str, title: str) -> str:
    return _block(
        f'<{base}>',
        [
            ('a', ['owl:Ontology']),
            ('rdfs:label', [_literal(title)]),
            (
                'rdfs:comment',
                [_literal('Entities, relationships and measures derived by Konstruct from the data stack.')],
            ),
        ],
    )


def _annotation(key: str, comment: str) -> str:
    return _block(
        f'konstruct:{key}',
        [
            ('a', ['owl:AnnotationProperty']),
            ('rdfs:label', [_literal(key)]),
            ('rdfs:comment', [_literal(comment)]),
        ],
    )


def _block(subject: str, statements: Iterable[tuple[str, list[str]]]) -> str:
    parts = [f'    {predicate} {", ".join(objects)}' for predicate, objects in statements if objects]
    return subject + '\n' + ' ;\n'.join(parts) + ' .'


def _literal(text: str | None) -> str:
    value = str(text or '')
    escaped = (
        value.replace('\\', '\\\\')
        .replace('"', '\\"')
        .replace('\n', '\\n')
        .replace('\r', '\\r')
        .replace('\t', '\\t')
    )
    return f'"{escaped}"'


def _camel(name: str) -> str:
    return (
        ''.join(part[:1].upper() + part[1:] for part in re.split(r'[^A-Za-z0-9]+', name) if part) or 'Entity'
    )


def _snake(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_') or 'measure'


def _term(graph: Graph, name: str | None) -> Node | None:
    from konstruct.semantics.definitions import slug

    return graph.nodes.get(f'term.{slug(name)}') if name else None


def _properties(
    graph: Graph, entity: Entity
) -> tuple[dict[str, tuple[list[str], set[str], set[str]]], set[str]]:
    """Each column name of the entity's relations: the columns of that name, their XML Schema
    types and their descriptions; and the names some column of which is a date or a time."""
    from konstruct.semantics.ossie import datatype

    found: dict[str, tuple[list[str], set[str], set[str]]] = {}
    unknown: set[str] = set()  # names some column of which has no type the datatype map names
    dated: set[str] = set()
    owners = {graph.nodes[i].parent for i in entity.key_ids if i in graph.nodes}
    for owner in sorted(o for o in owners if o):
        for column in graph.columns_of(owner):
            labels, types, descriptions = found.setdefault(column.name.lower(), ([], set(), set()))
            labels.append(graph.label(column))
            logical = datatype(column.data_type) or ''
            xsd = _XSD.get(logical)
            if logical in _UNMAPPED:
                dated.add(column.name.lower())
            if xsd is None:
                unknown.add(column.name.lower())
            else:
                types.add(xsd)
            if column.description:
                descriptions.add(column.description)
    for name in unknown:
        found[name][1].clear()
    return found, dated


def _formula(
    graph: Graph, explainer: Explainer, rows: Rows, measure: Measure
) -> tuple[str | None, str | None]:
    """The measure's formula, or why it has none: its own expression, else the formula composed
    down to source of the column the other exports compose, unless that would count other rows
    than the measure does (see :func:`konstruct.semantics.ossie.composed_reason`)."""
    if measure.expression:
        return measure.expression, None
    column = composed_column(graph, measure, rows)
    held = next((c for c in measure.columns if column is not None and c.id == column.id), None)
    if held is None or held.formula is None:
        return None, 'it does not reach source columns'
    tree = measure_tree(graph, explainer, measure, placeholder, rows)
    why = composed_reason(graph, measure, tree, rows) if tree is not None else None
    return (None, why) if why else (held.formula, None)
