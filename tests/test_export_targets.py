"""The ontology exported for semantic platforms: OWL in Turtle, a Snowflake semantic view and
Databricks metric views, each carrying the same entities as the Apache Ossie export."""

from __future__ import annotations

import json
import logging
import re
import shutil
import textwrap
from pathlib import Path
from typing import Any

import duckdb
import pytest
import rdflib
import sqlglot
import yaml
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS, SKOS
from sqlglot import exp
from sqlglot.tokenizer_core import Token

from konstruct import Project
from konstruct.core.graph import Graph, Node
from konstruct.interfaces.cli import main
from konstruct.outputs import metric_view, owl, semantic_view
from konstruct.outputs.compiled import Compiled, CompiledLink, CompiledTable, compile_ontology
from konstruct.outputs.export import ExportError
from konstruct.semantics import ossie
from konstruct.semantics.ontology import derive
from konstruct.semantics.vocabulary import read
from tests.test_ontology import _relation, _term, _unique, shop

KONSTRUCT = rdflib.Namespace(owl.VOCABULARY)


# OWL and Turtle


def turtle(text: str) -> rdflib.Graph:
    graph = rdflib.Graph()
    graph.parse(data=text, format='turtle')
    return graph


def counts(graph: rdflib.Graph) -> dict[str, int]:
    """Classes, data properties, object properties and measures, by kind."""
    return {
        'classes': len(set(graph.subjects(RDF.type, OWL.Class))),
        'data properties': len(set(graph.subjects(RDF.type, OWL.DatatypeProperty))),
        'object properties': len(set(graph.subjects(RDF.type, OWL.ObjectProperty))),
        'measures': len(set(graph.subjects(RDF.type, OWL.NamedIndividual))),
    }


def test_the_turtle_holds_every_entity_property_relationship_and_measure() -> None:
    g = shop()
    ontology = derive(g)
    text = owl.export(g, ontology, name='shop')
    graph = turtle(text)
    # customer_id and name of customer; order_id, customer_id and amount of order.
    assert counts(graph) == {'classes': 2, 'data properties': 5, 'object properties': 1, 'measures': 1}
    customer = rdflib.URIRef('urn:konstruct:shop#Customer')
    order = rdflib.URIRef('urn:konstruct:shop#Order')
    assert (customer, RDF.type, OWL.Class) in graph
    assert str(graph.value(customer, RDFS.label)) == 'customer'
    keys = graph.value(customer, OWL.hasKey)
    assert keys is not None
    [key] = list(Collection(graph, keys))
    assert key == rdflib.URIRef('urn:konstruct:shop#customer.customer_id')
    assert graph.value(key, RDFS.domain) == customer
    relation = rdflib.URIRef('urn:konstruct:shop#order_to_customer')
    assert (relation, RDF.type, OWL.FunctionalProperty) in graph
    assert (graph.value(relation, RDFS.domain), graph.value(relation, RDFS.range)) == (order, customer)
    assert str(graph.value(relation, KONSTRUCT.via)) == 'orders.customer_id = customers.customer_id'
    revenue = rdflib.URIRef('urn:konstruct:shop#measure/revenue')
    assert (order, KONSTRUCT.measure, revenue) in graph
    assert graph.value(revenue, KONSTRUCT.entity) == order
    assert {
        str(p): str(o) for p, o in graph.predicate_objects(revenue) if str(p).startswith(owl.VOCABULARY)
    } == {
        owl.VOCABULARY + 'formula': 'SUM(raw_orders.amount)',
        owl.VOCABULARY + 'grain': 'one row per order',
        owl.VOCABULARY + 'unit': 'currency',
        owl.VOCABULARY + 'currency': 'GBP',
        owl.VOCABULARY + 'status': 'approved',
        owl.VOCABULARY + 'owner': 'Sam Lee (Finance)',
        owl.VOCABULARY + 'approved': '2026-10-01',
        owl.VOCABULARY + 'column': 'orders.amount',
        owl.VOCABULARY + 'entity': 'urn:konstruct:shop#Order',
    }
    # raw_orders holds no entity, so its columns are only in the formula, and the file says so.
    assert '# - the columns of db.main.raw_orders, which hold no entity' in text


def test_the_turtle_round_trips_its_counts(tmp_path: Path) -> None:
    g = shop()
    text = owl.export(g, derive(g), name='shop')
    first = turtle(text)
    again = turtle(first.serialize(format='turtle'))
    assert counts(again) == counts(first)
    assert len(again) == len(first)
    # Konstruct reads the file back as a vocabulary: every class is an entity term again.
    path = tmp_path / 'shop.ttl'
    path.write_text(text)
    vocabulary = read([(path, 'shop.ttl')])
    assert vocabulary.problems == []
    assert sorted((t.name, t.kind) for t in vocabulary.terms) == [('customer', 'entity'), ('order', 'entity')]


def test_iris_are_stable_and_the_namespace_names_the_ontology() -> None:
    g = shop()
    assert owl.export(g, derive(g), name='shop') == owl.export(g, derive(g), name='shop')
    text = owl.export(g, derive(g), name='shop', iri='https://example.com/shop')
    graph = turtle(text)
    assert (rdflib.URIRef('https://example.com/shop'), RDF.type, OWL.Ontology) in graph
    assert (rdflib.URIRef('https://example.com/shop#Customer'), RDF.type, OWL.Class) in graph


def test_a_vocabulary_concept_keeps_its_iri() -> None:
    g = shop()
    _term(
        g,
        'Customer',
        ['model.shop.customers#customer_id'],
        kind='entity',
        iri='https://example.com/vocab#Client',
    )
    g.nodes['term.customer'].description = 'Someone who buys "things"\\ on a\nnew line.'
    graph = turtle(owl.export(g, derive(g), name='shop'))
    client = rdflib.URIRef('https://example.com/vocab#Client')
    assert (client, RDF.type, OWL.Class) in graph
    assert str(graph.value(client, SKOS.definition)) == 'Someone who buys "things"\\ on a\nnew line.'
    assert str(graph.value(client, SKOS.prefLabel)) == 'Customer'


def test_a_one_to_one_relationship_is_functional_both_ways() -> None:
    g = Graph()
    g.meta['dialect'] = 'duckdb'
    people = _relation(g, 'people', ['person_id'])
    passports = _relation(g, 'passports', ['passport_id', 'person_id'])
    _unique(g, people, 'person_id')
    _unique(g, passports, 'passport_id')
    _unique(g, passports, 'person_id')
    passports.attrs['conditions'] = ['JOIN ON: db.main.passports.person_id = db.main.people.person_id']
    graph = turtle(owl.export(g, derive(g), name='ids'))
    [relation] = set(graph.subjects(RDF.type, OWL.ObjectProperty))
    assert (relation, RDF.type, OWL.InverseFunctionalProperty) in graph


def test_broader_entities_are_subclasses() -> None:
    g = shop()
    _term(g, 'Party', ['model.shop.customers#customer_id'], kind='term')
    term = _term(g, 'Customer', ['model.shop.customers#customer_id'], kind='entity')
    g.add_edge(term.id, 'term.party', 'relates_to', role='broader')
    graph = turtle(owl.export(g, derive(g), name='shop'))
    assert (graph.value(rdflib.URIRef('urn:konstruct:shop#Customer'), RDFS.subClassOf)) is None
    text = owl.export(g, derive(g), name='shop')
    assert "# - 'Customer' is a kind of 'Party', which is not an entity" in text


# Snowflake semantic view


def _tokens(text: str) -> list[Token]:
    return list(sqlglot.Dialect.get_or_raise('snowflake').tokenize(text))


class SemanticView:
    """A CREATE SEMANTIC VIEW statement, read to the grammar of Snowflake's reference."""

    CLAUSES = ('TABLES', 'RELATIONSHIPS', 'FACTS', 'DIMENSIONS', 'METRICS')

    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = _tokens(text)
        self.at = 0
        for word in ('CREATE', 'OR', 'REPLACE', 'SEMANTIC VIEW'):
            self.expect(word)
        self.name = self.take().text
        self.clauses: dict[str, list[list[Token]]] = {}
        order = list(self.CLAUSES)
        while self.peek() in order:
            keyword = self.take().text.upper()
            assert order.index(keyword) >= 0
            order = order[order.index(keyword) + 1 :]  # clauses come in their order
            self.clauses[keyword] = self.entries()
        self.expect('COMMENT')
        self.expect('=')
        self.comment = self.take().text
        self.expect(';')
        assert self.at == len(self.tokens)
        self.tables = [self.table(e) for e in self.clauses['TABLES']]
        self.relationships = [self.relationship(e) for e in self.clauses.get('RELATIONSHIPS', [])]
        self.dimensions = [self.expression(e) for e in self.clauses.get('DIMENSIONS', [])]
        self.metrics = [self.expression(e) for e in self.clauses.get('METRICS', [])]

    def peek(self) -> str:
        return self.tokens[self.at].text.upper() if self.at < len(self.tokens) else ''

    def take(self) -> Token:
        self.at += 1
        return self.tokens[self.at - 1]

    def expect(self, word: str) -> None:
        assert self.take().text.upper() == word, self.tokens[self.at - 1]

    def entries(self) -> list[list[Token]]:
        self.expect('(')
        depth = 0
        found: list[list[Token]] = []
        entry: list[Token] = []
        while True:
            token = self.take()
            if token.text in ('(', '['):
                depth += 1
            elif token.text in (')', ']'):
                if depth == 0:
                    found.append(entry)
                    return found
                depth -= 1
            elif token.text == ',' and depth == 0:
                found.append(entry)
                entry = []
                continue
            entry.append(token)

    @staticmethod
    def properties(entry: list[Token]) -> tuple[list[Token], dict[str, Any]]:
        """The entry before its WITH SYNONYMS and COMMENT, and those properties."""
        words = [t.text.upper() for t in entry]
        end = len(entry)
        found: dict[str, Any] = {}
        if 'COMMENT' in words:
            index = words.index('COMMENT')
            assert words[index + 1] == '='
            found['comment'] = entry[index + 2].text
            assert index + 3 == end
            end = index
        if 'SYNONYMS' in words[:end]:
            index = words.index('SYNONYMS')
            assert words[index - 1] == 'WITH'
            found['synonyms'] = [t.text for t in entry[index:end] if t.token_type.name == 'STRING']
            end = index - 1
        return entry[:end], found

    def table(self, entry: list[Token]) -> dict[str, Any]:
        head, found = self.properties(entry)
        words = [t.text for t in head]
        assert words[1].upper() == 'AS'
        key: list[str] = []
        unique: list[list[str]] = []
        upper = [w.upper() for w in words]
        while 'UNIQUE' in upper:  # UNIQUE (columns), any number of times, after the PRIMARY KEY
            index = upper.index('UNIQUE')
            close = words.index(')', index)
            unique.append([w for w in words[index + 2 : close] if w != ','])
            del words[index : close + 1], upper[index : close + 1]
        if 'PRIMARY KEY' in upper:
            index = upper.index('PRIMARY KEY')
            assert words[index + 1] == '(' and words[-1] == ')'
            key = [w for w in words[index + 2 : -1] if w != ',']
            words = words[:index]
        return {'alias': words[0], 'source': ''.join(words[2:]), 'key': key, 'unique': unique, **found}

    @staticmethod
    def relationship(entry: list[Token]) -> dict[str, Any]:
        words = [t.text for t in entry]
        assert words[1].upper() == 'AS' and words[3] == '('
        close = words.index(')')
        references = [w.upper() for w in words].index('REFERENCES')
        assert references == close + 1
        return {
            'name': words[0],
            'from': words[2],
            'from_columns': [w for w in words[4:close] if w != ','],
            'to': words[references + 1],
            'to_columns': [w for w in words[references + 3 : -1] if w != ','],
        }

    def expression(self, entry: list[Token]) -> dict[str, Any]:
        head, found = self.properties(entry)
        assert [t.text for t in head[1:4:2]] == ['.', 'AS'] or head[3].text.upper() == 'AS'
        table, name = head[0].text, head[2].text
        sql = self.text[head[4].start : head[-1].end + 1]
        return {
            'table': table,
            'name': name,
            'sql': sql,
            'tree': sqlglot.parse_one(sql, read='snowflake'),
            **found,
        }


def check_semantic_view(text: str) -> SemanticView:
    """The statement, after checking it against Snowflake's validation rules."""
    view = SemanticView(text)
    aliases = {t['alias']: t for t in view.tables}
    assert len(aliases) == len(view.tables)
    assert view.dimensions or view.metrics  # at least one dimension or metric
    for rel in view.relationships:
        assert rel['from'] in aliases and rel['to'] in aliases and rel['from'] != rel['to']
        # A relationship refers to the target's primary key or one of its unique keys.
        target = aliases[rel['to']]
        assert rel['to_columns'] == target['key'] or rel['to_columns'] in target['unique']
    # No cycle, even through other tables.
    edges = {(r['from'], r['to']) for r in view.relationships}
    reach = {a: {b for x, b in edges if x == a} for a in aliases}
    for _ in aliases:
        reach = {a: targets | {c for b in targets for c in reach[b]} for a, targets in reach.items()}
    assert all(a not in targets for a, targets in reach.items())
    names: set[tuple[str, str]] = set()
    for item in [*view.dimensions, *view.metrics]:
        assert item['table'] in aliases
        # Unquoted names are not case sensitive, so two may not differ only by case.
        assert (item['table'], item['name'].lower()) not in names
        names.add((item['table'], item['name'].lower()))
        # An expression reads only its own table's columns.
        assert {c.table for c in item['tree'].find_all(exp.Column)} <= {item['table']}
    for metric in view.metrics:
        assert any(isinstance(n, exp.AggFunc) for n in metric['tree'].walk())  # a metric aggregates
    return view


def test_the_semantic_view_follows_snowflakes_grammar() -> None:
    g = shop()
    text = semantic_view.export(g, derive(g), 'shop')
    view = check_semantic_view(text)
    assert view.name == 'shop'
    assert [(t['alias'], t['source'], t['key']) for t in view.tables] == [
        ('customers', 'db.main.customers', ['customer_id']),
        ('orders', 'db.main.orders', ['order_id']),
        ('raw_orders', 'db.main.raw_orders', []),
    ]
    assert view.tables[0]['synonyms'] == ['customer']
    assert view.relationships == [
        {
            'name': 'order_to_customer',
            'from': 'orders',
            'from_columns': ['customer_id'],
            'to': 'customers',
            'to_columns': ['customer_id'],
        }
    ]
    [metric] = view.metrics
    assert (metric['table'], metric['name'], metric['sql']) == (
        'raw_orders',
        'revenue',
        'SUM(raw_orders.amount)',
    )
    assert metric['synonyms'] == ['Revenue']
    assert metric['comment'] == 'Grain: one row per order. Unit: currency (GBP).'
    assert {(d['table'], d['name']) for d in view.dimensions} >= {('customers', 'name'), ('raw_orders', 'id')}


def test_text_is_escaped_for_snowflake() -> None:
    g = shop()
    g.nodes['model.shop.customers'].description = "It's a \\ table\nof people"
    g.nodes['model.shop.customers#name'].description = 'The "name"'
    view = check_semantic_view(semantic_view.export(g, derive(g), 'shop'))
    assert view.tables[0]['comment'] == "It's a \\ table\nof people"


def test_a_relationship_that_closes_a_cycle_is_left_out() -> None:
    model = Compiled('m', 'duckdb')
    for a, b in (('a', 'b'), ('b', 'c'), ('c', 'a'), ('a', 'c')):
        model.links.append(CompiledLink(f'{a}_to_{b}', a, b, ['x'], ['y'], 'many-to-one'))
    kept = semantic_view._acyclic(model)
    assert [link.name for link in kept] == ['a_to_b', 'b_to_c', 'a_to_c']
    assert model.omitted == ['relationship c_to_a (c to a): Snowflake allows no cycle of relationships']


def test_relationships_in_a_cycle_compile_without_it() -> None:
    g = shop()
    customers = g.nodes['model.shop.customers']
    g.add_column(customers, 'last_order_id')
    customers.attrs['conditions'] = ['JOIN ON: db.main.customers.last_order_id = db.main.orders.order_id']
    ontology = derive(g)
    assert len(ontology.relationships) == 2
    text = semantic_view.export(g, ontology, 'shop')
    view = check_semantic_view(text)
    assert len(view.relationships) == 1
    assert '-- - relationship order_to_customer (orders to customers): Snowflake allows no cycle' in text


# Databricks metric views


VIEW = re.compile(
    r'CREATE OR REPLACE VIEW (\S+)\nWITH METRICS\nLANGUAGE YAML\nAS \$\$\n(.*?)\n\$\$;', re.DOTALL
)
TOP = {
    'version',
    'comment',
    'source',
    'parameters',
    'filter',
    'joins',
    'fields',
    'dimensions',
    'measures',
    'materialization',
}
FIELD = {'name', 'expr', 'comment', 'display_name', 'format', 'synonyms'}
MEASURE = FIELD | {'window'}
JOIN = {'name', 'source', 'on', 'using', 'cardinality', 'joins', 'rely'}


def metric_views(text: str) -> dict[str, dict[str, Any]]:
    """Each view's YAML, after checking it against the fields Databricks documents."""
    views = {name: yaml.safe_load(body) for name, body in VIEW.findall(text)}
    assert len(views) == text.count('CREATE OR REPLACE VIEW')
    for body in views.values():
        assert set(body) <= TOP and body['version'] == 1.1 and isinstance(body['source'], str)
        joins: list[tuple[list[str], dict[str, Any]]] = [([], j) for j in body.get('joins', [])]
        names = set()
        while joins:
            path, join = joins.pop()
            assert set(join) <= JOIN and set(join.get('rely', {})) <= {'at_most_one_match'}
            sqlglot.parse_one(join['on'], read='databricks')
            names.add('.'.join([*path, join['name']]))
            joins += [([*path, join['name']], j) for j in join.get('joins', [])]
        assert 'dimensions' not in body  # fields is the name version 1.1 prefers
        for item in [*body.get('fields', []), *body.get('measures', [])]:
            assert set(item) <= MEASURE
            assert len(item.get('synonyms', [])) <= 10
            assert all(len(s) <= 255 for s in [*item.get('synonyms', []), item.get('display_name', '')])
            tree = sqlglot.parse_one(item['expr'], read='databricks')
            for column in tree.find_all(exp.Column):
                qualifier = '.'.join(p.name for p in column.parts[:-1])
                assert qualifier in ('', 'source') or qualifier in names
        for measure in body.get('measures', []):
            tree = sqlglot.parse_one(measure['expr'], read='databricks')
            assert any(isinstance(n, exp.AggFunc) for n in tree.walk())
            # A measure reads its source's columns as source.column, never a field of the same name.
            assert all(c.table == 'source' for c in tree.find_all(exp.Column))
        all_names = [d['name'].lower() for d in [*body.get('fields', []), *body.get('measures', [])]]
        assert len(all_names) == len(set(all_names))
    return views


def test_the_metric_views_follow_the_databricks_yaml_reference() -> None:
    g = shop()
    views = metric_views(metric_view.export(g, derive(g), 'shop'))
    assert set(views) == {'customers_metrics', 'orders_metrics', 'raw_orders_metrics'}
    orders = views['orders_metrics']
    assert orders['source'] == 'db.main.orders'
    assert orders['joins'] == [
        {
            'name': 'customers',
            'source': 'db.main.customers',
            'on': 'source.customer_id = customers.customer_id',
            'rely': {'at_most_one_match': True},
        }
    ]
    assert {'name': 'customers_name', 'expr': 'customers.name'} in orders['fields']
    assert views['raw_orders_metrics']['measures'] == [
        {
            'name': 'revenue',
            'expr': 'SUM(source.amount)',
            'display_name': 'Revenue',
            'comment': 'Grain: one row per order. Unit: currency (GBP).',
        }
    ]


def test_a_view_joins_only_the_tables_its_source_refers_to() -> None:
    g = shop()
    lines = _relation(g, 'order_lines', ['line_id', 'order_id'])
    _unique(g, lines, 'line_id')
    lines.attrs['conditions'] = ['JOIN ON: db.main.order_lines.order_id = db.main.orders.order_id']
    views = metric_views(metric_view.export(g, derive(g), 'shop'))
    [join] = views['order_lines_metrics']['joins']
    assert (join['name'], join['on']) == ('orders', 'source.order_id = orders.order_id')
    assert 'joins' not in join
    names = [d['name'] for d in views['order_lines_metrics']['fields']]
    assert names == ['line_id', 'order_id', 'orders_order_id', 'orders_customer_id', 'orders_amount']


def test_a_relationship_to_a_key_not_known_unique_is_left_out_of_the_platforms() -> None:
    g = shop()
    regions = _relation(g, 'regions', ['region_id'])
    _term(g, 'Region', [f'{regions.id}#region_id'], kind='entity')
    _term(g, 'Shopper', ['model.shop.customers#customer_id'])  # an inferred term names the entity
    customers = g.nodes['model.shop.customers']
    g.add_column(customers, 'region_id')
    customers.attrs['conditions'] = ['JOIN ON: db.main.customers.region_id = db.main.regions.region_id']
    ontology = derive(g)
    model = compile_ontology(g, ontology, 'shop')
    assert [link.name for link in model.links] == ['order_to_customer']
    assert model.omitted == [
        "the key of 'region' (regions.region_id): it is not known to be unique",
        'relationship customer to region (customers.region_id = regions.region_id): the key it refers '
        'to is not known to be unique',
    ]
    assert model.table('customers').synonyms == ['customer', 'Shopper']
    # OWL states the relationship, without claiming it is functional.
    graph = turtle(owl.export(g, ontology, name='shop'))
    relation = rdflib.URIRef('urn:konstruct:shop#customer_to_region')
    assert (relation, RDF.type, OWL.ObjectProperty) in graph
    assert (relation, RDF.type, OWL.FunctionalProperty) not in graph


def test_an_ossie_model_compiles_with_its_dataset_metrics(tmp_path: Path) -> None:
    from tests.test_ossie import FIXTURES

    shutil.copy(FIXTURES / 'scott_model.ossie.fixture.yaml', tmp_path / 'scott.ossie.yaml')
    con = duckdb.connect(str(tmp_path / 'warehouse.duckdb'))
    con.execute('create schema scott')
    con.execute('create table scott.dept (deptno integer primary key, dname varchar, loc varchar)')
    con.execute(
        'create table scott.emp (empno integer primary key, ename varchar, job varchar, mgr integer, '
        'hiredate date, sal decimal(7, 2), comm decimal(7, 2), deptno integer)'
    )
    con.execute('create table scott.salgrade (grade integer primary key, losal integer, hisal integer)')
    con.close()
    project = Project(tmp_path)
    project.build(run_dbt=False)
    view = check_semantic_view(project.export('semantic-view'))
    metrics = {(m['table'], m['name']): m['sql'] for m in view.metrics}
    # COUNT(*) reads no column; it belongs to the dataset the Ossie model put it on.
    assert metrics[('emp', 'emp_count')] == 'COUNT(*)'
    assert metrics[('dept', 'dept_count')] == 'COUNT(*)'
    views = metric_views(project.export('metric-view'))
    assert {m['name'] for m in views['emp_metrics']['measures']} >= {'emp_count', 'sum_sal'}
    assert counts(turtle(project.export('owl')))['measures'] == len(project.ontology().measures)


# What every target carries


def _project(root: Path) -> Path:
    """A SQL project: customers and orders, a per-row measure, an aggregate over one table and one
    over two."""
    (root / 'models.sql').write_text(
        'create table main.customers as select id as customer_id, name from raw.people;\n'
        'create table main.orders as select o.id as order_id, o.customer_id, o.amount_cents / 100.0 as total\n'
        'from raw.orders o join main.customers c on o.customer_id = c.customer_id;\n'
        'create table main.spend as select customer_id, sum(amount_cents) / 100.0 as spend\n'
        'from raw.orders group by customer_id;\n'
        'create table main.reach as select sum(o.amount_cents) / count(p.id) as reach\n'
        'from raw.orders o cross join raw.people p;\n'
    )
    (root / 'definitions.yml').write_text(
        textwrap.dedent(
            """\
            version: 1
            terms:
              - name: Customer
                kind: entity
                definition: Someone who buys.
                columns: [main.customers.customer_id]
              - name: Order total
                kind: measure
                definition: What an order is worth, in pounds.
                grain: one row per order
                columns: [main.orders.total]
              - name: Customer spend
                kind: measure
                synonyms: [basket value]
                definition: What a customer has spent, in pounds.
                grain: one row per customer
                filters: [completed orders only]
                columns: [main.spend.spend]
              - name: Reach
                kind: measure
                definition: Spend per person.
                columns: [main.reach.reach]
            """
        )
    )
    assert main(['build', str(root), '--no-warehouse', '--no-dbt', '--dialect', 'duckdb', '-q']) == 0
    return root


def _carried(project: Project) -> dict[str, Any]:
    """The entities, tables and metrics of each target, read from its own format."""
    found: dict[str, Any] = {}
    document = yaml.safe_load(project.export('ossie'))
    found['ossie'] = {
        'entities': {
            json.loads(e['data'])['entity']: d['source']
            for d in document['datasets']
            for e in d.get('custom_extensions', [])
        },
        'tables': {d['source'] for d in document['datasets']},
        'metrics': {m['name'] for d in document['datasets'] for m in d.get('metrics', [])}
        | {m['name'] for m in document.get('metrics', [])},
    }
    view = check_semantic_view(project.export('semantic-view'))
    found['semantic-view'] = {
        'entities': {t['synonyms'][0]: t['source'] for t in view.tables if t.get('synonyms')},
        'keys': {t['synonyms'][0]: t['key'] for t in view.tables if t['key']},
        'tables': {t['source'] for t in view.tables},
        'metrics': {m['name'] for m in view.metrics},
    }
    views = metric_views(project.export('metric-view'))
    found['metric-view'] = {
        'tables': {v['source'] for v in views.values()},
        'metrics': {m['name'] for v in views.values() for m in v.get('measures', [])},
    }
    graph = turtle(project.export('owl'))
    found['owl'] = {
        'entities': {str(graph.value(c, RDFS.label)) for c in graph.subjects(RDF.type, OWL.Class)}
    }
    return found


def test_every_target_carries_the_same_entities(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    project = Project(_project(tmp_path))
    ontology = project.ontology()
    with caplog.at_level(logging.WARNING):
        carried = _carried(project)
    entities = {e.name for e in ontology.entities}
    assert entities == {'customer', 'order'}
    assert set(carried['ossie']['entities']) == entities
    assert carried['semantic-view']['entities'] == carried['ossie']['entities']
    assert carried['owl']['entities'] == entities
    assert (
        carried['ossie']['tables'] == carried['semantic-view']['tables'] == carried['metric-view']['tables']
    )
    # Reach reads two tables: Ossie writes it as a model metric, the platforms leave it out.
    assert carried['ossie']['metrics'] == {'customer_spend', 'reach'}
    assert carried['semantic-view']['metrics'] == carried['metric-view']['metrics'] == {'customer_spend'}
    for fmt in ('semantic-view', 'metric-view'):
        text = project.export(fmt)
        assert "-- - measure 'Order total': it is not an aggregate" in text
        assert "-- - measure 'Reach': it reads columns of more than one table (people, raw_orders)" in text
    assert 'Required filters: completed orders only.' in project.export('semantic-view')


def test_a_project_with_no_definitions_exports_its_derived_ontology(tmp_path: Path) -> None:
    con = duckdb.connect(str(tmp_path / 'shop.duckdb'))
    con.execute('create table customers (customer_id integer primary key, name varchar)')
    con.execute('create table orders (order_id integer primary key, customer_id integer, amount double)')
    con.close()
    (tmp_path / 'models.sql').write_text(
        'create table order_facts as select o.order_id, o.customer_id, c.name from orders o\n'
        'join customers c on o.customer_id = c.customer_id;\n'
    )
    (tmp_path / 'konstruct.toml').write_text('[[warehouse]]\ntype = "duckdb"\npath = "shop.duckdb"\n')
    project = Project(tmp_path)
    project.build(dialect='duckdb', run_dbt=False)
    ontology = project.ontology()
    assert {e.name for e in ontology.entities} >= {'customer', 'order'}
    graph = turtle(project.export('owl'))
    assert counts(graph)['classes'] == len(ontology.entities)
    check_semantic_view(project.export('semantic-view'))
    metric_views(project.export('metric-view'))


def test_the_ontology_formats_take_no_selection_and_need_an_ontology(tmp_path: Path) -> None:
    (tmp_path / 'x.sql').write_text('create table t2 as select a from t1')
    project = Project(tmp_path)
    project.build(dialect='duckdb', warehouses=False, run_dbt=False)
    for fmt in ('owl', 'semantic-view', 'metric-view'):
        with pytest.raises(ValueError, match='nothing to export'):
            project.export(fmt)
        with pytest.raises(ValueError, match='does not take a selection'):
            project.export(fmt, select='t2')


def test_the_compiled_model_says_what_it_holds() -> None:
    g = shop()
    model = compile_ontology(g, derive(g), 'shop')
    assert str(model) == 'shop: 3 tables, 1 relationships, 1 metrics'
    assert model.to_dict()['metrics'] == [
        {'name': 'revenue', 'table': 'raw_orders', 'expression': 'SUM(raw_orders.amount)'}
    ]


@pytest.mark.dbt
def test_the_example_exports_to_every_platform(jaffle_built_graph: Path) -> None:
    project = Project(jaffle_built_graph)
    carried = _carried(project)
    ontology = project.ontology()
    assert (
        carried['owl']['entities'] == set(carried['ossie']['entities']) == {e.name for e in ontology.entities}
    )
    assert carried['semantic-view']['metrics'] == {'customer_lifetime_value', 'order_amount'}
    graph = turtle(project.export('owl'))
    assert counts(graph)['object properties'] == len(ontology.relationships)
    assert counts(graph)['measures'] == len(ontology.measures)


# Keys, entities and relationships as the platforms require them


def test_a_key_not_known_to_be_unique_is_no_primary_key_or_owl_key() -> None:
    g = Graph()
    g.meta['dialect'] = 'duckdb'
    accounts = _relation(g, 'accounts', ['account_ref', 'name'])
    _term(g, 'Account', [f'{accounts.id}#account_ref'], kind='entity')
    ontology = derive(g)
    [entity] = ontology.entities
    assert entity.unique_ids == []
    text = semantic_view.export(g, ontology, 'shop')
    view = check_semantic_view(text)
    assert view.tables[0]['key'] == [] and view.tables[0]['synonyms'] == ['account']
    assert "-- - the key of 'account' (accounts.account_ref): it is not known to be unique" in text
    turtle_text = owl.export(g, ontology, name='shop')
    assert 'owl:hasKey' not in turtle_text
    assert "# - the key of 'account': it is not known to be unique" in turtle_text
    # A tested key is unique, and is a key in both.
    g2 = shop()
    assert [e.unique_ids for e in derive(g2).entities] == [
        ['model.shop.customers#customer_id'],
        ['model.shop.orders#order_id'],
    ]


def test_a_table_holds_the_entity_named_for_it_with_the_other_keys_unique() -> None:
    g = Graph()
    g.meta['dialect'] = 'duckdb'
    orders = _relation(g, 'orders', ['order_id', 'invoice_id', 'amount'])
    payments = _relation(g, 'payments', ['payment_id', 'invoice_id'])
    items = _relation(g, 'order_items', ['order_item_id', 'order_id'])
    _unique(g, orders, 'order_id')
    _unique(g, orders, 'invoice_id')
    _unique(g, payments, 'payment_id')
    _unique(g, items, 'order_item_id')
    payments.attrs['conditions'] = ['JOIN ON: db.main.payments.invoice_id = db.main.orders.invoice_id']
    items.attrs['conditions'] = ['JOIN ON: db.main.order_items.order_id = db.main.orders.order_id']
    ontology = derive(g)
    view = check_semantic_view(semantic_view.export(g, ontology, 'shop'))
    tables = {t['alias']: t for t in view.tables}
    assert (tables['orders']['key'], tables['orders']['unique'], tables['orders']['synonyms']) == (
        ['order_id'],
        [['invoice_id']],
        ['order'],
    )
    assert {(r['from'], r['to'], tuple(r['to_columns'])) for r in view.relationships} == {
        ('order_items', 'orders', ('order_id',)),
        ('payments', 'orders', ('invoice_id',)),
    }
    document = yaml.safe_load(ossie.export(g, ontology, 'shop'))
    datasets = {d['name']: d for d in document['datasets']}
    assert datasets['orders']['primary_key'] == ['order_id']
    assert json.loads(datasets['orders']['custom_extensions'][0]['data']) == {'entity': 'order'}


def test_a_relationship_to_a_column_the_target_does_not_key_is_left_out() -> None:
    model = Compiled('m', 'duckdb')
    model.tables = [
        CompiledTable('a', 'db.a', 'a', ['a_id']),
        CompiledTable('b', 'db.b', 'b', ['b_id']),
    ]
    model.links = [
        CompiledLink('a_to_b', 'a', 'b', ['b_ref'], ['b_id'], 'many-to-one'),
        CompiledLink('a_to_b_2', 'a', 'b', ['b_code'], ['code'], 'many-to-one'),
    ]
    assert [link.name for link in model.keyed()] == ['a_to_b']
    assert model.omitted == ['relationship a_to_b_2 (a to b): b.code is not a key of b']


# Measures a platform cannot hold as written


def test_a_measure_whose_formula_crosses_a_filter_is_left_out(tmp_path: Path) -> None:
    (tmp_path / 'models.sql').write_text(
        'create table main.completed as select id as order_id, customer_id, amount_cents from raw.orders\n'
        "where status = 'completed';\n"
        'create table main.spend as select customer_id, sum(amount_cents) / 100.0 as spend from main.completed\n'
        'group by customer_id;\n'
        'create table main.gross as select customer_id, sum(amount_cents) / 100.0 as gross from raw.orders\n'
        'group by customer_id;\n'
    )
    (tmp_path / 'definitions.yml').write_text(
        'version: 1\nterms:\n'
        '  - {name: Completed spend, kind: measure, definition: Spend on completed orders., '
        'columns: [main.spend.spend]}\n'
        '  - {name: Gross spend, kind: measure, definition: Spend on every order., columns: [main.gross.gross]}\n'
    )
    assert main(['build', str(tmp_path), '--no-warehouse', '--no-dbt', '--dialect', 'duckdb', '-q']) == 0
    project = Project(tmp_path)
    reason = "measure 'Completed spend': its composed formula would not count the rows it does (WHERE in main.completed)"
    for fmt in ('semantic-view', 'metric-view'):
        text = project.export(fmt)
        assert f'-- - {reason}' in text
        assert 'completed_spend' not in text.split('Left out:')[1].split('\n\n')[-1]
        assert 'gross_spend' in text
    document = yaml.safe_load(project.export('ossie'))
    assert [m['name'] for m in document['metrics']] == ['gross_spend']
    # OWL keeps the measure, without the formula that would count every order.
    text = project.export('owl')
    graph = turtle(text)
    formulas = {
        str(graph.value(m, RDFS.label)): graph.value(m, KONSTRUCT.formula)
        for m in graph.subjects(RDF.type, OWL.NamedIndividual)
    }
    assert formulas['Completed spend'] is None and formulas['Gross spend'] is not None
    assert f"# - the formula of 'Completed spend': {reason.split(': ', 1)[1]}" in text


def test_an_aggregate_of_an_aggregate_is_left_out() -> None:
    g = Graph()
    g.meta['dialect'] = 'duckdb'
    raw = _relation(g, 'raw_orders', ['id', 'customer_id', 'amount'], kind='source')
    customers = _relation(g, 'customers', ['customer_id', 'ltv'])
    kpis = _relation(g, 'kpis', ['avg_ltv'])
    _unique(g, customers, 'customer_id')
    g.nodes[f'{customers.id}#ltv'].attrs['expression'] = 'SUM(db.main.raw_orders.amount)'
    g.add_edge(f'{customers.id}#ltv', f'{raw.id}#amount', 'derived_from', transform='transformed')
    g.nodes[f'{kpis.id}#avg_ltv'].attrs['expression'] = 'AVG(db.main.customers.ltv)'
    g.add_edge(f'{kpis.id}#avg_ltv', f'{customers.id}#ltv', 'derived_from', transform='transformed')
    _term(g, 'Average lifetime value', [f'{kpis.id}#avg_ltv'])
    _term(g, 'Lifetime value', [f'{customers.id}#ltv'])
    ontology = derive(g)
    model = compile_ontology(g, ontology, 'shop')
    assert [m.name for m in model.metrics] == ['lifetime_value']
    assert model.omitted == [
        "measure 'Average lifetime value': it aggregates an aggregate, which a metric cannot hold"
    ]
    assert [m['name'] for m in yaml.safe_load(ossie.export(g, ontology, 'shop'))['metrics']] == [
        'lifetime_value'
    ]


def test_names_that_differ_only_by_case_stay_apart() -> None:
    g = shop()
    g.add_column(g.nodes['source.shop.raw_orders'], 'REVENUE')
    lines = _relation(g, 'order_lines', ['line_id', 'order_id', 'orders_amount'])
    _unique(g, lines, 'line_id')
    lines.attrs['conditions'] = ['JOIN ON: db.main.order_lines.order_id = db.main.orders.order_id']
    view = check_semantic_view(semantic_view.export(g, derive(g), 'shop'))
    raw = {d['name']: d['sql'] for d in view.dimensions if d['table'] == 'raw_orders'}
    assert raw['REVENUE'] == 'raw_orders.REVENUE'
    # Unquoted, revenue and REVENUE are one name, so the metric takes another.
    assert [m['name'] for m in view.metrics] == ['revenue_2']
    views = metric_views(metric_view.export(g, derive(g), 'shop'))
    assert [m['name'] for m in views['raw_orders_metrics']['measures']] == ['revenue_2']
    # The joined orders.amount would be named orders_amount, which the source already has.
    names = [f['name'] for f in views['order_lines_metrics']['fields']]
    assert names == [
        'line_id',
        'order_id',
        'orders_amount',
        'orders_order_id',
        'orders_customer_id',
        'orders_amount_2',
    ]


def test_reserved_words_are_quoted() -> None:
    g = Graph()
    g.meta['dialect'] = 'duckdb'
    table = _relation(g, 'order', ['order_id', 'group', 'select', 'from'])
    _unique(g, table, 'order_id')
    view = check_semantic_view(semantic_view.export(g, derive(g), 'shop'))
    assert view.tables[0]['alias'] == 'order'
    assert [d['sql'] for d in view.dimensions] == [
        '"order".order_id',
        '"order"."group"',
        '"order"."select"',
        '"order"."from"',
    ]
    text = metric_view.export(g, derive(g), 'shop')
    views = metric_views(text)
    assert 'CREATE OR REPLACE VIEW order_metrics' in text
    assert views['order_metrics']['source'] == 'db.main.`order`'
    assert [f['expr'] for f in views['order_metrics']['fields']] == [
        'source.order_id',
        'source.`group`',
        'source.`select`',
        'source.`from`',
    ]
    joined = Graph()
    joined.meta['dialect'] = 'duckdb'
    users = _relation(joined, 'users', ['user_id'])
    lines = _relation(joined, 'lines', ['line_id', 'user_id'])
    _unique(joined, users, 'user_id')
    _unique(joined, lines, 'line_id')
    lines.attrs['conditions'] = ['JOIN ON: db.main.lines.user_id = db.main.users.user_id']
    assert metric_views(metric_view.export(joined, derive(joined), 'shop'))


def test_two_relationships_to_one_table_are_two_joins() -> None:
    g = Graph()
    g.meta['dialect'] = 'duckdb'
    customers = _relation(g, 'customers', ['customer_id'])
    orders = _relation(g, 'orders', ['order_id', 'billing_customer_id', 'shipping_customer_id'])
    _unique(g, customers, 'customer_id')
    _unique(g, orders, 'order_id')
    orders.attrs['conditions'] = [
        'JOIN ON: db.main.orders.billing_customer_id = db.main.customers.customer_id',
        'JOIN ON: db.main.orders.shipping_customer_id = db.main.customers.customer_id',
    ]
    views = metric_views(metric_view.export(g, derive(g), 'shop'))
    joins = views['orders_metrics']['joins']
    assert [(j['name'], j['on']) for j in joins] == [
        ('billing_customer', 'source.billing_customer_id = billing_customer.customer_id'),
        ('shipping_customer', 'source.shipping_customer_id = shipping_customer.customer_id'),
    ]
    assert [f['expr'] for f in views['orders_metrics']['fields']][-2:] == [
        'billing_customer.customer_id',
        'shipping_customer.customer_id',
    ]


def test_dollar_quotes_and_databricks_limits() -> None:
    g = shop()
    g.nodes['model.shop.customers'].description = 'Spend over $$100'
    g.nodes['term.revenue'].attrs['synonyms'] = [f'name {i}' for i in range(12)] + ['x' * 256, 'a $$ b']
    text = metric_view.export(g, derive(g), 'shop')
    views = metric_views(text)
    assert 'comment' not in views['customers_metrics']
    assert (
        '-- - the comment of customers in its metric view: it holds $$, which would end the statement' in text
    )
    [measure] = views['raw_orders_metrics']['measures']
    assert measure['synonyms'] == [f'name {i}' for i in range(10)]
    assert (
        '-- - 4 synonyms of revenue in its metric view: Databricks takes ten of at most 255 characters'
        in text
    )
    long = shop()
    long.nodes['term.revenue'].name = 'R' * 300
    [measure] = metric_views(metric_view.export(long, derive(long), 'shop'))['raw_orders_metrics']['measures']
    assert 'display_name' not in measure


# OWL details


def test_date_and_time_properties_have_no_range() -> None:
    g = shop()
    g.nodes['model.shop.customers#name'].data_type = 'date'
    g.nodes['model.shop.orders#amount'].data_type = 'time'
    text = owl.export(g, derive(g), name='shop')
    graph = turtle(text)
    for name in ('customer.name', 'order.amount'):
        assert graph.value(rdflib.URIRef(f'urn:konstruct:shop#{name}'), RDFS.range) is None
    assert '# - the ranges of customer.name, order.amount: OWL 2 has no date or time datatype' in text


def test_a_namespace_that_is_not_an_iri_is_refused() -> None:
    g = shop()
    with pytest.raises(ExportError, match=re.escape("'https://example.com/a b>' is not an IRI")):
        owl.export(g, derive(g), name='shop', iri='https://example.com/a b>')


# What is left out, and when nothing is left


def test_when_every_measure_is_left_out_the_error_says_so() -> None:
    g = Graph()
    g.meta['dialect'] = 'duckdb'
    raw = _relation(g, 'raw', ['amount_cents'], kind='source')
    orders = _relation(g, 'orders', ['total'])
    g.nodes[f'{orders.id}#total'].attrs['expression'] = 'db.main.raw.amount_cents / 100.0'
    g.add_edge(f'{orders.id}#total', f'{raw.id}#amount_cents', 'derived_from', transform='transformed')
    _term(g, 'Order total', [f'{orders.id}#total'], kind='measure')
    for module in (semantic_view, metric_view):
        with pytest.raises(
            ExportError, match="nothing to export: measure 'Order total': it is not an aggregate"
        ):
            module.export(g, derive(g), 's')


def test_table_names_that_differ_only_by_case_stay_apart() -> None:
    g = Graph()
    g.meta['dialect'] = 'duckdb'
    lower = g.add_node(Node('model.shop.orders', 'model', 'orders', relation='db.main.orders'))
    upper = g.add_node(Node('model.raw.Orders', 'model', 'Orders', relation='db.raw.Orders'))
    for node, key in ((lower, 'order_id'), (upper, 'order_key')):
        g.add_column(node, key)
        _unique(g, node, key)
    names = sorted(t.name for t in compile_ontology(g, derive(g), 'shop').tables)
    assert len({n.lower() for n in names}) == 2, names
