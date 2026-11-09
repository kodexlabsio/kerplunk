"""SKOS and OWL vocabularies, read as business terms.

A company that already keeps its vocabulary as SKOS concepts or OWL classes need not write it
again in ``definitions.yml``. Each named ``skos:Concept`` and ``owl:Class`` becomes a term: its
name is its English ``skos:prefLabel`` or ``rdfs:label`` (any English variant, then an untagged
label, then any), its other labels are synonyms, its definition is its ``skos:definition`` or
``rdfs:comment``, and its broader terms are the concepts it names with ``skos:broader`` or
``rdfs:subClassOf``, or that name it with ``skos:narrower``, in any of the vocabulary files read.
An OWL class is an entity. Anonymous classes (``owl:unionOf`` and the like) are not terms.

Files are read with rdflib (the ``rdf`` extra) as Turtle, N-Triples or RDF/XML. Nothing they
name is fetched, and an RDF/XML file that declares XML entities is refused, since entity
expansion can make a small file take hours to read.
"""

from __future__ import annotations

import xml.parsers.expat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from konstruct.semantics.definitions import Term

FORMATS = {'.ttl': 'turtle', '.nt': 'nt', '.rdf': 'xml', '.owl': 'xml', '.xml': 'xml'}
MAX_BYTES = 50_000_000


@dataclass
class Concept:
    """One concept as read, before its broader concepts are resolved across files."""

    iri: str
    term: Term
    broader: list[str] = field(default_factory=list)
    """The IRIs of its broader concepts."""
    narrower: list[str] = field(default_factory=list)
    """The IRIs of concepts it names as narrower."""


@dataclass
class Vocabulary:
    """The terms of every vocabulary file read, and the problems found."""

    terms: list[Term] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    """Concepts read from each file."""


class _Refused(Exception):
    pass


def _check_xml(path: Path) -> None:
    """Refuse an RDF/XML file that declares entities, before rdflib's parser expands them."""

    def refuse(*_: Any) -> None:
        raise _Refused('it declares XML entities, which are not read')

    parser = xml.parsers.expat.ParserCreate()
    parser.SetParamEntityParsing(xml.parsers.expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.EntityDeclHandler = refuse
    parser.UnparsedEntityDeclHandler = refuse
    try:
        with path.open('rb') as handle:
            parser.ParseFile(handle)
    except xml.parsers.expat.ExpatError:
        return  # not well-formed: rdflib reports it


def _label(graph: Any, subject: Any, predicates: tuple[Any, ...]) -> list[str]:
    """Every label of ``subject`` under ``predicates``, best first: English (any variant), then
    untagged, then the rest, across all the predicates."""
    found = []
    for rank, predicate in enumerate(predicates):
        for value in graph.objects(subject, predicate):
            text = str(value).strip()
            if not text:
                continue
            language = (getattr(value, 'language', None) or '').lower()
            order = 0 if language.split('-')[0] == 'en' else 1 if not language else 2
            found.append((order, rank, text))
    return [text for _, _, text in sorted(found)]


def _local(iri: str) -> str:
    return iri.rsplit('#', 1)[-1].rsplit('/', 1)[-1].replace('_', ' ')


def _concepts(path: Path, relative: str, problems: list[str]) -> list[Concept]:
    file_format = FORMATS.get(path.suffix.lower())
    if file_format is None:
        problems.append(f'{relative}: not a vocabulary file (use .ttl, .nt, .rdf, .owl or .xml)')
        return []
    try:
        import rdflib
        from rdflib.namespace import OWL, RDF, RDFS, SKOS
    except ImportError:
        problems.append(f"{relative}: reading vocabularies needs rdflib: uv tool install 'konstruct[rdf]'")
        return []
    if path.stat().st_size > MAX_BYTES:
        problems.append(f'{relative}: larger than {MAX_BYTES // 1_000_000} MB, so it is not read')
        return []
    if file_format == 'xml':
        try:
            _check_xml(path)
        except _Refused as error:
            problems.append(f'{relative}: {error}')
            return []
    graph = rdflib.Graph()
    try:
        graph.parse(path, format=file_format)
    except Exception as error:  # rdflib raises its parsers' own errors
        problems.append(f'{relative}: not valid {file_format}: {str(error).strip().splitlines()[0]}')
        return []
    subjects = sorted(
        {
            s
            for kind in (SKOS.Concept, OWL.Class)
            for s in graph.subjects(RDF.type, kind)
            if isinstance(s, rdflib.URIRef)  # an anonymous class has no stable name
        },
        key=str,
    )
    if not subjects:
        problems.append(f'{relative}: has no named skos:Concept or owl:Class')
    concepts = []
    for subject in subjects:
        labels = _label(graph, subject, (SKOS.prefLabel, RDFS.label))
        name = labels[0] if labels else _local(str(subject))
        others = labels[1:] + _label(graph, subject, (SKOS.altLabel,))
        definition = _label(graph, subject, (SKOS.definition, RDFS.comment))
        term = Term(
            name=name,
            definition=definition[0] if definition else '',
            path=relative,
            line=0,
            synonyms=sorted({o for o in others if o.lower() != name.lower()}),
            kind='entity' if (subject, RDF.type, OWL.Class) in graph else None,
            iri=str(subject),
        )
        # Only named concepts: an OWL restriction (a blank node) is not a broader concept.
        broader = [
            str(b)
            for p in (SKOS.broader, RDFS.subClassOf)
            for b in graph.objects(subject, p)
            if isinstance(b, rdflib.URIRef)
        ]
        narrower = [str(n) for n in graph.objects(subject, SKOS.narrower) if isinstance(n, rdflib.URIRef)]
        concepts.append(
            Concept(
                str(subject),
                term,
                sorted({b for b in broader if b != str(subject)}),
                sorted({n for n in narrower if n != str(subject)}),
            )
        )
    return concepts


def read(paths: list[tuple[Path, str]]) -> Vocabulary:
    """The terms of several vocabulary files, with broader concepts resolved across them all.
    Concepts that share a label are reported, and the first is kept."""
    result = Vocabulary()
    concepts: list[Concept] = []
    for path, relative in paths:
        found = _concepts(path, relative, result.problems)
        result.counts[relative] = len(found)
        concepts += found
    by_iri: dict[str, Concept] = {}
    by_name: dict[str, Concept] = {}
    for concept in concepts:
        if concept.iri in by_iri:
            continue  # the same concept in two files
        other = by_name.get(concept.term.name.lower())
        if other is not None:
            result.problems.append(
                f'{concept.term.path}: {concept.iri} is also named {concept.term.name!r}, like '
                f'{other.iri} ({other.term.path}); kept that one'
            )
            continue
        by_iri[concept.iri] = concept
        by_name[concept.term.name.lower()] = concept
    # A label belongs to one concept: one that names another concept is dropped from the later.
    for concept in by_iri.values():
        kept = []
        for synonym in concept.term.synonyms:
            owner = by_name.get(synonym.lower())
            if owner is not None and owner is not concept:
                result.problems.append(
                    f'{concept.term.path}: {concept.iri} has the label {synonym!r}, which names '
                    f'{owner.iri}; not used as a synonym'
                )
                continue
            by_name[synonym.lower()] = concept
            kept.append(synonym)
        concept.term.synonyms = kept
    broader: dict[str, set[str]] = {iri: set() for iri in by_iri}
    for concept in by_iri.values():
        for iri in concept.broader:
            if iri in by_iri:
                broader[concept.iri].add(iri)
            else:
                result.problems.append(
                    f'{concept.term.path}: {concept.term.name!r} has broader concept {iri}, which no '
                    'vocabulary read defines'
                )
        for iri in concept.narrower:
            if iri in by_iri:
                broader[iri].add(concept.iri)
    for concept in by_iri.values():
        concept.term.broader = sorted(by_iri[b].term.name for b in broader[concept.iri])
        result.terms.append(concept.term)
    return result


def merge(terms: list[Term], vocabulary: list[Term], problems: list[str]) -> list[Term]:
    """The vocabulary's terms that the project does not define, after filling in the project's
    own terms from the vocabulary where they leave something out: synonyms, broader terms and
    a definition. A project term matches a vocabulary term by name or synonym; a vocabulary
    name that is already another project term's is not added to it."""
    by_name = {n.lower(): t for t in terms for n in t.names}
    project = {id(t) for t in terms}
    added = []
    for term in vocabulary:
        # Only the project's own terms take in a vocabulary term.
        own = next(
            (
                by_name[n.lower()]
                for n in term.names
                if n.lower() in by_name and id(by_name[n.lower()]) in project
            ),
            None,
        )
        if own is None:
            added.append(term)
            for name in term.names:
                by_name.setdefault(name.lower(), term)
            continue
        for name in term.names:
            holder = by_name.get(name.lower())
            if holder is None:
                own.synonyms.append(name)
                by_name[name.lower()] = own
            elif holder is not own:
                problems.append(
                    f'{term.path}: {term.iri} gives {own.name!r} the name {name!r}, which is already '
                    f'{holder.name!r}; not added'
                )
        own.broader = own.broader or term.broader
        own.definition = own.definition or term.definition
        own.iri = own.iri or term.iri
    return added
