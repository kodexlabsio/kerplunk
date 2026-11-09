"""SKOS and OWL vocabularies as business terms, and the concept hierarchy."""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import Any

import pytest

from konstruct import Project
from konstruct.semantics.definitions import parse, validate
from konstruct.semantics.vocabulary import merge, read

TURTLE = """
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex: <https://example.com/vocab#> .

ex:Measure a skos:Concept ; skos:prefLabel "Measure"@en ; skos:definition "A number the business tracks."@en .
ex:Revenue a skos:Concept ;
    skos:prefLabel "Revenue"@en , "Chiffre d'affaires"@fr ;
    skos:altLabel "Turnover"@en , "Sales"@en ;
    skos:definition "Money earned from sales before costs."@en ;
    skos:broader ex:Measure .
ex:NetRevenue a skos:Concept ; skos:prefLabel "Net revenue" ; skos:broader ex:Revenue .
ex:Party a owl:Class ; rdfs:label "Party" ; rdfs:comment "A person or organisation." .
ex:Customer a owl:Class ; rdfs:label "Customer" ; rdfs:subClassOf ex:Party .
"""


def test_concepts_and_classes_become_terms(tmp_path: Path) -> None:
    path = tmp_path / 'vocab.ttl'
    path.write_text(TURTLE)
    terms = {t.name: t for t in read([(path, 'vocab.ttl')]).terms}
    assert set(terms) == {'Measure', 'Revenue', 'Net revenue', 'Party', 'Customer'}
    revenue = terms['Revenue']
    assert (revenue.definition, revenue.synonyms, revenue.broader) == (
        'Money earned from sales before costs.',
        ["Chiffre d'affaires", 'Sales', 'Turnover'],
        ['Measure'],
    )
    assert (terms['Customer'].kind, terms['Customer'].broader) == ('entity', ['Party'])
    assert terms['Net revenue'].broader == ['Revenue']


def test_the_build_merges_a_vocabulary_into_the_definitions(tmp_path: Path) -> None:
    (tmp_path / 'vocab.ttl').write_text(TURTLE)
    (tmp_path / 'konstruct.toml').write_text('[vocabularies]\npaths = ["vocab.ttl"]\n')
    (tmp_path / 'sales.sql').write_text(
        'create table main.sales as select sum(amount) as revenue from raw.orders'
    )
    (tmp_path / 'definitions.yml').write_text(
        'terms:\n  - name: Revenue\n    definition: Ours.\n    columns: [main.sales.revenue]\n'
    )
    project = Project(tmp_path)
    graph = project.build(warehouses=False, run_dbt=False).graph
    revenue = graph.nodes['term.revenue']
    assert revenue.description == 'Ours.' and revenue.origins == ['definitions']
    assert revenue.attrs['synonyms'] == ["Chiffre d'affaires", 'Sales', 'Turnover']
    assert revenue.attrs['broader'] == ['Measure']
    assert graph.nodes['term.measure'].origins == ['vocabulary']
    definition = project.define('turnover').terms[0]
    assert (definition.name, definition.broader, definition.narrower) == (
        'Revenue',
        ['Measure'],
        ['Net revenue'],
    )
    assert '  a kind of: Measure' in str(definition) and '  kinds: Net revenue' in str(definition)
    assert project.describe('Measure').attrs['narrower'] == ['Revenue']
    ontology = project.ontology()
    concepts = {c.name: c for c in ontology.concepts}
    assert concepts['Revenue'].broader == ['Measure'] and concepts['Revenue'].narrower == ['Net revenue']
    assert '  Revenue  a kind of Measure' in str(ontology)
    assert ontology.to_dict()['concepts'][0] == {'name': 'Customer', 'broader': ['Party']}
    assert (
        str(project.define('Measure').terms[0]).count(
            'defined in: vocab.ttl (https://example.com/vocab#Measure)'
        )
        == 1
    )
    source = next(s for s in graph.meta['sources'] if s['input'] == 'vocabulary')
    assert source == {'input': 'vocabulary', 'path': 'vocab.ttl', 'detail': '5 concepts, 4 new terms'}


def test_broader_terms_in_definitions_and_their_problems() -> None:
    parsed = parse(
        'terms:\n'
        '  - {name: Rate, definition: A ratio., broader: [Number]}\n'
        '  - {name: Number, definition: A quantity., broader: [Rate]}\n'
        '  - {name: Odd, definition: Odd., broader: [Missing]}\n',
        'definitions.yml',
    )
    problems = validate(parsed.terms)
    assert "definitions.yml:4: term 'Odd': broader term 'Missing' is not defined" in problems
    assert any('broader terms form a cycle (Rate -> Number -> Rate)' in p for p in problems)


def test_vocabulary_problems(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def problems(name: str, text: str) -> list[str]:
        (tmp_path / name).write_text(text)
        return read([(tmp_path / name, name)]).problems

    assert problems('bad.ttl', 'this is not turtle')[0].startswith('bad.ttl: not valid turtle')
    assert 'not a vocabulary file' in problems('v.jsonld', '{}')[0]
    assert problems('empty.ttl', '@prefix ex: <https://example.com/#> .\nex:a ex:b ex:c .\n') == [
        'empty.ttl: has no named skos:Concept or owl:Class'
    ]
    entities = (
        '<?xml version="1.0"?>\n<!DOCTYPE rdf:RDF [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>\n'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>\n'
    )
    assert problems('bomb.rdf', entities) == ['bomb.rdf: it declares XML entities, which are not read']
    real = builtins.__import__

    def without_rdflib(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == 'rdflib' or name.startswith('rdflib.'):
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', without_rdflib)
    assert 'konstruct[rdf]' in problems('bad.ttl', 'x')[0]
    monkeypatch.undo()
    (tmp_path / 'konstruct.toml').write_text('[vocabularies]\npaths = ["missing.ttl"]\n')
    graph = Project(tmp_path).build(warehouses=False, run_dbt=False).graph
    assert 'vocabularies: missing.ttl does not exist' in graph.warnings


PREFIXES = """
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex: <https://example.com/vocab#> .
@prefix other: <https://example.com/other#> .
"""


def test_concepts_across_files_anonymous_classes_and_labels(tmp_path: Path) -> None:
    (tmp_path / 'a.ttl').write_text(
        PREFIXES
        + 'ex:Local a skos:Concept ; skos:prefLabel "Local" ; skos:broader other:Remote .\n'
        + 'ex:Parent a skos:Concept ; skos:prefLabel "Parent" ; skos:narrower ex:Child .\n'
        + 'ex:Child a skos:Concept ; skos:prefLabel "Child" .\n'
        + 'ex:Lost a skos:Concept ; skos:prefLabel "Lost" ; skos:broader other:Nowhere .\n'
        + 'ex:Party a owl:Class ; rdfs:label "Party" ; owl:equivalentClass [ a owl:Class ; owl:unionOf (ex:A ex:B) ] .\n'
        + 'ex:Kunde a skos:Concept ; skos:prefLabel "Kunde"@de , "Client"@fr , "Customer"@en-GB .\n'
        + 'ex:Client a skos:Concept ; skos:prefLabel "Client"@en .\n'
        + 'ex:Person a owl:Class ; rdfs:label "Person" ; rdfs:subClassOf ex:Party , [ a owl:Restriction ] .\n'
        + 'ex:Order a skos:Concept ; skos:prefLabel "Order" ; skos:definition "A purchase." .\n'
        + 'ex:SortOrder a skos:Concept ; skos:prefLabel "Order" ; skos:definition "A sequence." .\n'
    )
    (tmp_path / 'b.ttl').write_text(PREFIXES + 'other:Remote a skos:Concept ; skos:prefLabel "Remote" .\n')
    found = read([(tmp_path / 'a.ttl', 'a.ttl'), (tmp_path / 'b.ttl', 'b.ttl')])
    terms = {t.name: t for t in found.terms}
    assert terms['Local'].broader == ['Remote']
    assert terms['Child'].broader == ['Parent']
    assert terms['Customer'].synonyms == ['Kunde']
    assert terms['Client'].synonyms == []
    assert terms['Person'].broader == ['Party']
    assert any('ex:Kunde' not in p and 'has the label' in p and 'Client' in p for p in found.problems)
    assert not [p for p in found.problems if 'Person' in p]
    assert terms['Order'].definition == 'A purchase.'
    assert not [t for t in found.terms if t.name.startswith('N') and len(t.name) > 20]
    assert any('Lost' in p and 'Nowhere' in p for p in found.problems)
    assert any('SortOrder is also named' in p for p in found.problems)
    assert sorted(t.name for t in found.terms) == sorted(
        ['Local', 'Parent', 'Child', 'Lost', 'Party', 'Customer', 'Client', 'Person', 'Order', 'Remote']
    )


def test_merging_keeps_each_project_name_where_it_is() -> None:
    project = parse(
        'terms:\n  - {name: Revenue, definition: R.}\n  - {name: Sales, definition: S.}\n',
        'definitions.yml',
    ).terms
    from konstruct.semantics.definitions import Term

    vocabulary = [
        Term(name='Revenue', definition='', path='v.ttl', line=0, synonyms=['Sales', 'Turnover'], iri='ex:R')
    ]
    problems: list[str] = []
    assert merge(project, vocabulary, problems) == []
    assert project[0].synonyms == ['Turnover']
    assert problems == ["v.ttl: ex:R gives 'Revenue' the name 'Sales', which is already 'Sales'; not added"]
    assert validate(project) == []


def test_every_cycle_is_found_once() -> None:
    parsed = parse(
        'terms:\n'
        '  - {name: A, definition: a., broader: [B, C]}\n'
        '  - {name: B, definition: b.}\n'
        '  - {name: C, definition: c., broader: [A]}\n'
        '  - {name: E, definition: e., broader: [F]}\n'
        '  - {name: F, definition: f., broader: [E]}\n',
        'definitions.yml',
    )
    cycles = [p for p in validate(parsed.terms) if 'cycle' in p]
    assert len(cycles) == 2
    assert any('A -> C -> A' in c for c in cycles) and any('E -> F -> E' in c for c in cycles)


def test_diff_reports_a_changed_broader_term() -> None:
    from konstruct.analysis.diff import compare_definitions

    before = 'terms:\n  - {name: Rate, definition: A ratio.}\n  - {name: Number, definition: N.}\n'
    after = before.replace(
        '{name: Rate, definition: A ratio.}', '{name: Rate, definition: A ratio., broader: [Number]}'
    )
    assert any('broader' in line for line in compare_definitions('definitions.yml', before, after).lines())
