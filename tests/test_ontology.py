"""The ontology: entities, measures, relationships and rules derived from the graph."""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

import pytest
import yaml

from konstruct import Project
from konstruct.core.graph import Graph, Node, NodeKind
from konstruct.interfaces.cli import main
from konstruct.semantics.definitions import parse
from konstruct.semantics.ontology import derivation, derive, entity_name, id_like, link_key, name_matches


@pytest.mark.parametrize(
    ('column', 'relation', 'expected'),
    [
        ('customer_id', None, 'customer'),
        ('order_id', 'stg_orders', 'order'),
        ('CategoryId', None, 'category'),
        ('id', 'stg_customers', 'customer'),
        ('id', 'dim_categories', 'category'),
        ('address_key', None, 'address'),
        ('CUSTOMER_ID', None, 'customer'),
        ('OrderLineKey', None, 'order_line'),
        ('uuid', 'stg_events', 'event'),
        ('guid', 'raw.devices', 'device'),
        ('order_number', 'fct_orders', 'order'),
        ('id', 'stg_stripe__payments', 'payment'),
        ('id', 'orders__daily', 'orders__daily'),
        ('id', 'int_orders__pivoted', 'orders__pivoted'),
        ('customer_uuid', None, 'customer'),
        ('userUUID', None, 'user'),
        ('meta.event_id', 'events', 'event'),
        ('status', None, 'status'),
    ],
)
def test_entity_names_come_from_the_key(column: str, relation: str | None, expected: str) -> None:
    assert entity_name(column, relation) == expected


@pytest.mark.parametrize(
    ('column', 'expected'),
    [
        ('id', True),
        ('customer_id', True),
        ('uuid', True),
        ('CategoryId', True),
        ('paid', False),
        ('valid', False),
        ('email', False),
        ('squid', False),
    ],
)
def test_identifier_like_names(column: str, expected: bool) -> None:
    assert id_like(column) is expected


def _relation(graph: Graph, name: str, columns: list[str], kind: NodeKind = 'model') -> Node:
    node = graph.add_node(Node(f'{kind}.shop.{name}', kind, name, relation=f'db.main.{name}'))
    for column in columns:
        graph.add_column(node, column)
    return node


def _unique(graph: Graph, relation: Node, column: str) -> None:
    test = graph.add_node(
        Node(f'test.shop.unique_{relation.name}_{column}', 'test', 'unique', attrs={'test': 'unique'})
    )
    graph.add_edge(test.id, f'{relation.id}#{column}', 'tests')


def _term(graph: Graph, name: str, columns: list[str], **attrs: object) -> Node:
    term = graph.add_node(
        Node(
            f'term.{name.lower().replace(" ", "_")}',
            'term',
            name,
            path='definitions.yml',
            attrs={'line': 3, **attrs},
        )
    )
    for column in columns:
        graph.add_edge(term.id, column, 'defines')
    return term


def shop() -> Graph:
    """customers and orders keyed by tests, orders joined to customers, and a revenue measure."""
    g = Graph()
    g.meta['dialect'] = 'duckdb'
    raw = _relation(g, 'raw_orders', ['id', 'customer_id', 'amount'], kind='source')
    customers = _relation(g, 'customers', ['customer_id', 'name'])
    orders = _relation(g, 'orders', ['order_id', 'customer_id', 'amount'])
    _unique(g, customers, 'customer_id')
    _unique(g, orders, 'order_id')
    orders.attrs['conditions'] = ['LEFT JOIN ON: db.main.orders.customer_id = db.main.customers.customer_id']
    g.nodes[f'{orders.id}#amount'].attrs['expression'] = 'SUM(db.main.raw_orders.amount)'
    g.add_edge(
        f'{orders.id}#amount',
        f'{raw.id}#amount',
        'derived_from',
        transform='transformed',
        verified='confirmed',
    )
    _term(
        g,
        'Revenue',
        [f'{orders.id}#amount'],
        status='approved',
        owner={'name': 'Sam Lee', 'role': 'Finance'},
        approved='2026-10-01',
        grain='one row per order',
        unit='currency',
        currency='GBP',
    )
    return g


def test_entities_measures_and_relationships_from_a_graph() -> None:
    ontology = derive(shop())
    assert [(e.name, e.key, e.relations) for e in ontology.entities] == [
        ('customer', 'customer_id', ['customers']),
        ('order', 'order_id', ['orders']),
    ]
    assert ontology.entities[0].evidence == ['unique test on customers.customer_id']
    [revenue] = ontology.measures
    assert (revenue.name, revenue.status, revenue.owner, revenue.entity) == (
        'Revenue',
        'approved',
        'Sam Lee (Finance)',
        'order',
    )
    [column] = revenue.columns
    assert (column.column, column.formula, column.sources) == (
        'orders.amount',
        'SUM(raw_orders.amount)',
        ['raw_orders.amount'],
    )
    assert (column.verified, column.edges) == (1, 1)
    [rel] = ontology.relationships
    assert (rel.source, rel.target, rel.cardinality, rel.via) == (
        'order',
        'customer',
        'many-to-one',
        'orders.customer_id = customers.customer_id',
    )
    assert rel.evidence == ['join in orders']
    assert not rel.tested
    assert ontology.violations == []
    assert ontology.entity('Customer') is ontology.entities[0]
    assert ontology.measure('revenue') is revenue


def test_only_confirmed_edges_count_as_verified() -> None:
    """verify marks an edge no perturbation moved as ``not exercised``: claimed, but not shown by
    the data, so it is not counted as verified."""
    g = shop()
    raw = g.nodes['source.shop.raw_orders']
    g.add_edge('model.shop.orders#amount', f'{raw.id}#id', 'derived_from', verified='not exercised')
    [column] = derive(g).measures[0].columns
    assert (column.verified, column.edges) == (1, 2)


def test_a_measure_with_two_formulas_breaks_a_rule() -> None:
    g = shop()
    payments = _relation(g, 'raw_payments', ['amount'], kind='source')
    finance = _relation(g, 'finance', ['revenue'])
    g.nodes[f'{finance.id}#revenue'].attrs['expression'] = 'SUM(db.main.raw_payments.amount)'
    g.add_edge(f'{finance.id}#revenue', f'{payments.id}#amount', 'derived_from', transform='transformed')
    g.add_edge('term.revenue', f'{finance.id}#revenue', 'defines')
    ontology = derive(g)
    [violation] = ontology.rules[0].violations
    assert ontology.rules[0].name == 'one-formula-per-measure'
    assert "'Revenue' (definitions.yml:3)" in violation
    assert (
        'finance.revenue and orders.amount differ' in violation
        or 'orders.amount and finance.revenue differ' in violation
    )
    assert 'source raw_payments.amount' in violation


def test_a_column_derived_from_another_carries_the_measure() -> None:
    g = shop()
    report = _relation(g, 'report', ['amount'])
    g.nodes[f'{report.id}#amount'].attrs['expression'] = 'db.main.orders.amount'
    g.add_edge(f'{report.id}#amount', 'model.shop.orders#amount', 'derived_from', transform='direct')
    g.add_edge('term.revenue', f'{report.id}#amount', 'defines')
    ontology = derive(g)
    assert ontology.violations == []
    assert [c.column for c in ontology.measures[0].columns] == ['orders.amount', 'report.amount']


def test_terms_of_kind_entity_name_entities_and_need_a_unique_key() -> None:
    g = shop()
    _term(g, 'Customer', ['model.shop.customers#customer_id'], kind='entity', status='approved')
    products = _relation(g, 'products', ['sku', 'title'])
    _term(g, 'Products', [f'{products.id}#sku'], kind='entity')
    ontology = derive(g)
    names = {e.name: e for e in ontology.entities}
    assert names['customer'].term == 'Customer'
    assert names['product'].key == 'sku'
    assert names['product'].relations == ['products']
    rule = next(r for r in ontology.rules if r.name == 'entity-keys-are-unique')
    assert rule.violations == [
        "products.sku, the key of 'product' (definitions.yml:3), has no uniqueness test"
    ]


def test_kind_term_keeps_a_computed_column_out_of_the_measures() -> None:
    g = shop()
    g.nodes['term.revenue'].attrs['kind'] = 'term'
    assert derive(g).measures == []


def test_iceberg_identifier_fields_are_keys_nothing_enforces() -> None:
    """Iceberg never checks a table's identifier fields: they name its key, inferred."""
    g = Graph()
    events = _relation(g, 'events', ['event_id', 'payload'], kind='table')
    events.attrs['identifier_fields'] = ['event_id']
    ontology = derive(g)
    [entity] = ontology.entities
    assert (entity.name, entity.relations, entity.evidence, entity.confidence) == (
        'event',
        ['events'],
        ['identifier field events.event_id, not enforced'],
        'inferred',
    )
    assert ontology.unique_ids == frozenset()


def test_a_composite_key_is_unique_only_together() -> None:
    g = Graph()
    bridge = _relation(g, 'customer_segments', ['customer_id', 'segment_id'], kind='table')
    bridge.attrs['primary_key'] = ['customer_id', 'Segment_ID']
    customers = _relation(g, 'customers', ['customer_id', 'name'], kind='table')
    customers.attrs['primary_key'] = ['customer_id']
    derived = derivation(g)
    assert derived.composite == {bridge.id: [('customer_id', 'segment_id')]}
    assert derived.unique == frozenset({f'{customers.id}#customer_id'})
    assert derived.entity_of(customers) == 'customer' and derived.entity_of(bridge) is None
    assert derived.ontology == derive(g)


def test_definitions_accept_a_kind() -> None:
    parsed = parse(
        'version: 1\nterms:\n  - {name: Customer, definition: A buyer., kind: entity}\n'
        '  - {name: Thing, definition: Something., kind: widget}\n',
        'definitions.yml',
    )
    assert parsed.terms[0].kind == 'entity'
    assert parsed.terms[1].kind is None
    assert parsed.errors == [
        "definitions.yml:4: term 'Thing': kind 'widget' is not one of entity, measure, term"
    ]


def _sql_project(root: Path) -> Path:
    (root / 'models.sql').write_text(
        'create table main.customers as select id as customer_id, name from raw.people;\n'
        'create table main.orders as select o.id as order_id, o.customer_id, o.amount * 2 as amount\n'
        'from raw.orders o join main.customers c on o.customer_id = c.customer_id;\n'
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
              - name: Order value
                definition: Twice the order amount.
                columns: [main.orders.amount]
            """
        )
    )
    return root


def test_the_build_writes_ontology_yml_and_warns_on_broken_rules(tmp_path: Path) -> None:
    project = Project(_sql_project(tmp_path))
    result = project.build(dialect='duckdb', warehouses=False, run_dbt=False)
    path = tmp_path / 'konstruct-out' / 'ontology.yml'
    assert path in result.outputs
    data = yaml.safe_load(path.read_text())
    assert data['version'] == 1
    assert [e['name'] for e in data['entities']] == ['customer']
    assert data['measures'][0]['name'] == 'Order value'
    assert data['measures'][0]['columns'][0]['formula'] == 'orders.amount * 2'
    assert (
        "ontology: customers.customer_id, the key of 'customer' (definitions.yml:3), has no uniqueness test"
        in (result.graph.warnings)
    )
    assert project.ontology().to_dict() == data


def test_cli_text_yaml_json_and_strict(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _sql_project(tmp_path)
    assert main(['build', str(tmp_path), '--no-warehouse', '--no-dbt', '--dialect', 'duckdb', '-q']) == 0
    assert (
        main(['build', str(tmp_path), '--no-warehouse', '--no-dbt', '--dialect', 'duckdb', '-q', '--strict'])
        == 1
    )
    capsys.readouterr()
    assert main(['ontology', '-C', str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert text.startswith('Ontology: 1 entity, 1 measure, 0 relationships, 1 rule violation.\n')
    assert re.search(r"^  customer  key customer_id  customers  declared  term '", text, re.M)
    assert '    orders.amount  = orders.amount * 2  verified 0/1\n' in text
    assert main(['ontology', '-C', str(tmp_path), '--format', 'yaml']) == 0
    assert yaml.safe_load(capsys.readouterr().out)['entities'][0]['key'] == 'customer_id'
    assert main(['ontology', '-C', str(tmp_path), '--json']) == 0
    assert json.loads(capsys.readouterr().out)['rules'][1]['name'] == 'entity-keys-are-unique'
    assert main(['ontology', '-C', str(tmp_path), '--strict']) == 1


@pytest.mark.dbt
def test_the_example_ontology(jaffle_built_graph: Path) -> None:
    ontology = Project(jaffle_built_graph).ontology()
    entities = {e.name: e for e in ontology.entities}
    assert set(entities) == {'customer', 'order', 'payment'}
    assert entities['customer'].relations == ['customers', 'stg_customers']
    assert entities['customer'].term == 'Customer'
    assert entities['order'].relations == ['orders', 'stg_orders']
    measures = {m.name: m for m in ontology.measures}
    assert set(measures) == {'Customer lifetime value', 'Order amount'}
    ltv = measures['Customer lifetime value']
    assert ltv.entity == 'customer'
    assert {c.column: c.formula for c in ltv.columns} == {
        'customers.customer_lifetime_value': 'SUM(raw_payments.amount / 100)',
        'customer_ltv.lifetime_value': 'SUM(raw_payments.amount / 100)',
    }
    relationships = {(r.source, r.target): r for r in ontology.relationships}
    assert relationships[('order', 'customer')].via == 'orders.customer_id = customers.customer_id'
    assert relationships[('order', 'customer')].tested
    assert any(e.startswith('relationships test ') for e in relationships[('order', 'customer')].evidence)
    assert relationships[('payment', 'order')].cardinality == 'many-to-one'
    assert ontology.violations == []
    assert (jaffle_built_graph / 'konstruct-out' / 'ontology.yml').is_file()


def test_a_unique_column_not_named_like_an_identifier_is_not_a_key() -> None:
    g = shop()
    _unique(g, g.nodes['model.shop.customers'], 'name')
    ontology = derive(g)
    assert [e.keys for e in ontology.entities] == [['customers.customer_id'], ['orders.order_id']]


def test_a_unique_test_on_some_rows_is_no_key() -> None:
    """A ``unique`` test with a ``where`` config (``is_current``, on a slowly changing dimension)
    holds only on the rows it selects: the column repeats across the others, so it is no key, gives
    no cardinality and makes no join safe."""
    g = shop()
    test = g.nodes['test.shop.unique_customers_customer_id']
    test.attrs['where'] = 'is_current'
    ontology = derive(g)
    assert 'model.shop.customers#customer_id' not in ontology.unique_ids
    assert [e.name for e in ontology.entities] == ['order']
    assert all(r.cardinality is None for r in ontology.relationships if r.target == 'customer')


def test_primary_keys_are_keys_and_composite_ones_are_not() -> None:
    g = Graph()
    accounts = _relation(g, 'dim_accounts', ['account_number', 'name'], kind='table')
    accounts.attrs['primary_key'] = ['account_number']
    lines = _relation(g, 'order_lines', ['order_id', 'line_number'], kind='table')
    lines.attrs['primary_key'] = ['order_id', 'line_number']
    [entity] = derive(g).entities
    assert (entity.name, entity.key, entity.evidence) == (
        'account',
        'account_number',
        ['primary key dim_accounts.account_number'],
    )


@pytest.mark.parametrize(
    'config',
    [{'severity': 'warn'}, {'error_if': '>10'}, {'severity': 'warn', 'error_if': '!= 0'}],
    ids=['a warning', 'ten failures allowed', 'a warning on one failure'],
)
def test_a_unique_test_whose_failures_do_not_fail_it_is_no_key(config: dict[str, str]) -> None:
    """A test that only warns, or that lets some rows fail, passes on a column that repeats."""
    g = shop()
    g.nodes['test.shop.unique_customers_customer_id'].attrs.update(config)
    ontology = derive(g)
    assert 'model.shop.customers#customer_id' not in ontology.unique_ids
    assert [e.name for e in ontology.entities] == ['order']


@pytest.mark.parametrize(
    ('dialect', 'origins', 'enforced', 'unique'),
    [
        ('duckdb', ['sql'], False, True),
        ('postgres', ['sql'], False, True),
        ('snowflake', ['sql'], False, False),
        ('bigquery', ['sql'], False, False),
        ('redshift', ['sql'], False, False),
        ('databricks', ['sql'], False, False),
        ('clickhouse', ['sql'], False, False),
        ('tsql', ['sql'], False, False),
        ('hive', ['sql'], False, False),
        ('trino', ['sql'], False, False),
        ('athena', ['sql'], False, False),
        ('duckdb', ['dbt'], False, False),
        ('duckdb', ['dbt'], True, True),
        ('clickhouse', ['dbt'], True, False),
    ],
)
def test_a_primary_key_nothing_enforces_is_an_inferred_key(
    dialect: str, origins: list[str], enforced: bool, unique: bool
) -> None:
    """A primary key the warehouse never checks, or a dbt constraint outside an enforced contract,
    names the entity's key but shows it unique no more than a column's name would."""
    g = Graph()
    g.meta['dialect'] = dialect
    accounts = _relation(g, 'dim_accounts', ['account_number', 'name'], kind='table')
    accounts.origins = origins
    accounts.attrs['primary_key'] = ['account_number']
    if enforced:
        accounts.attrs['primary_key_enforced'] = True
    ontology = derive(g)
    [entity] = ontology.entities
    assert (f'{accounts.id}#account_number' in ontology.unique_ids) is unique
    assert entity.confidence == ('declared' if unique else 'inferred')
    suffix = '' if unique else ', not enforced'
    assert entity.evidence == [f'primary key dim_accounts.account_number{suffix}']


def test_iceberg_identifier_fields_must_be_one_column() -> None:
    g = Graph()
    events = _relation(g, 'events', ['event_id', 'at'], kind='table')
    events.attrs['identifier_fields'] = ['event_id', 'at']
    assert derive(g).entities == []


def test_an_inferred_entity_term_does_not_rename_the_entity() -> None:
    g = shop()
    _term(g, 'Client', ['model.shop.customers#customer_id'], definition='Someone who buys.')
    ontology = derive(g)
    customer = ontology.entity('customer')
    assert customer is not None and customer.term == 'Client'
    assert [e.name for e in ontology.entities] == ['customer', 'order']
    assert ontology.entity('client') is customer


def test_a_declared_entity_merges_the_entities_it_binds() -> None:
    g = shop()
    people = _relation(g, 'stg_people', ['id', 'email'])
    _unique(g, people, 'id')
    _term(g, 'Customer', ['model.shop.customers#customer_id', f'{people.id}#id'], kind='entity')
    ontology = derive(g)
    customer = ontology.entity('customer')
    assert customer is not None
    assert (customer.key, customer.keys, customer.relations) == (
        'customer_id',
        ['customers.customer_id', 'stg_people.id'],
        ['customers', 'stg_people'],
    )
    assert [e.name for e in ontology.entities] == ['customer', 'order']


def test_a_declared_entity_may_not_take_another_entitys_name() -> None:
    g = shop()
    clients = _relation(g, 'clients', ['client_id'])
    _unique(g, clients, 'client_id')
    _term(g, 'Customer', [f'{clients.id}#client_id'], kind='entity')
    ontology = derive(g)
    assert [e.name for e in ontology.entities] == ['client', 'customer', 'order']
    assert ontology.violations == [
        "entity-names-are-distinct: term 'Customer' (definitions.yml:3) names 'customer', which is "
        'already the entity keyed by customers.customer_id; bind those columns too, or name it differently'
    ]


def test_a_measure_grain_follows_a_renamed_entity() -> None:
    g = shop()
    _term(g, 'Purchase', ['model.shop.orders#order_id'], kind='entity')
    g.nodes['term.revenue'].attrs['grain'] = 'one row per purchase'
    ontology = derive(g)
    assert ontology.measures[0].entity == 'purchase'


def test_a_grain_finds_the_key_of_the_entity_it_names() -> None:
    g = Graph()
    g.meta['dialect'] = 'duckdb'
    stores = _relation(g, 'store_sales', ['store_id', 'revenue'])
    g.nodes[f'{stores.id}#revenue'].attrs['expression'] = 'SUM(x)'
    _term(g, 'Store revenue', [f'{stores.id}#revenue'], kind='measure', grain='one row per store')
    ontology = derive(g)
    [store] = ontology.entities
    assert (store.name, store.key, store.evidence) == (
        'store',
        'store_id',
        ["grain of 'Store revenue' (definitions.yml:3)"],
    )
    assert ontology.measures[0].entity == 'store'


def test_cardinality_needs_a_unique_key() -> None:
    g = shop()
    regions = _relation(g, 'regions', ['region_id'])
    _term(g, 'Region', [f'{regions.id}#region_id'], kind='entity')
    customers = g.nodes['model.shop.customers']
    g.add_column(customers, 'region_id')
    customers.attrs['conditions'] = ['JOIN ON: db.main.customers.region_id = db.main.regions.region_id']
    ontology = derive(g)
    rel = next(r for r in ontology.relationships if r.target == 'region')
    assert (rel.source, rel.cardinality, rel.tested) == ('customer', None, False)
    assert '  customer -> region  cardinality unknown  customers.region_id = regions.region_id' in str(
        ontology
    )


def test_the_tested_join_is_shown_whatever_the_order() -> None:
    g = shop()
    test = g.add_node(
        Node(
            'test.shop.relationships_orders_customer_id',
            'test',
            'relationships',
            attrs={'test': 'relationships'},
        )
    )
    g.add_edge(test.id, 'model.shop.orders#customer_id', 'tests')
    g.add_edge(test.id, 'model.shop.customers#customer_id', 'tests', role='relationship target')
    [rel] = derive(g).relationships
    assert rel.tested
    assert rel.evidence == ['join in orders', 'relationships test relationships']
    reordered = Graph()
    reordered.meta = g.meta
    for node in reversed(list(g.nodes.values())):
        reordered.add_node(node)
    for edge in reversed(list(g.edges)):
        reordered.add_edge(edge.source, edge.target, edge.kind, **edge.attrs)
    assert derive(reordered).to_dict() == derive(g).to_dict()


def test_names_match_across_case_spacing_and_plurals() -> None:
    assert name_matches('Customer lifetime_value', 'customer_lifetime value')
    assert name_matches('customers', 'customer')
    assert name_matches('order-line', 'order_line')
    assert not name_matches('order', 'orders line')
    ontology = derive(shop())
    assert ontology.measure('REVENUE') is ontology.measures[0]


def test_the_written_ontology_is_what_the_graph_gives(tmp_path: Path) -> None:
    project = Project(_sql_project(tmp_path))
    project.build(dialect='duckdb', warehouses=False, run_dbt=False)
    out = tmp_path / 'konstruct-out'
    assert (out / 'ontology.yml').read_text() == derive(Graph.read(out / 'graph.json')).to_yaml()


def test_carried_rules_name_missing_columns_unowned_and_undated_terms(tmp_path: Path) -> None:
    (tmp_path / 'models.sql').write_text(
        'create table main.orders as select id as order_id, amount from raw.orders;\n'
    )
    (tmp_path / 'definitions.yml').write_text(
        textwrap.dedent(
            """\
            version: 1
            terms:
              - {name: Gone, definition: x, columns: [main.orders.nothing]}
              - {name: Unowned, definition: x, status: approved, approved: 2026-10-01, columns: [main.orders.amount]}
              - {name: Undated, definition: x, status: approved, owner: Sam, columns: [main.orders.amount]}
            """
        )
    )
    project = Project(tmp_path)
    project.build(dialect='duckdb', warehouses=False, run_dbt=False)
    rules = {r.name: r.violations for r in project.ontology().rules}
    assert len(rules['bound-columns-exist']) == 1 and 'main.orders.nothing' in rules['bound-columns-exist'][0]
    owned = rules['approved-terms-are-owned']
    assert len(owned) == 2
    assert any('Unowned' in v and 'no owner' in v for v in owned)
    assert any('Undated' in v and '`approved` date' in v for v in owned)


def test_a_nested_identifier_field_is_named_by_its_own_name() -> None:
    g = Graph()
    events = _relation(g, 'events', ['meta.event_id', 'payload'], kind='table')
    events.attrs['identifier_fields'] = ['meta.event_id']
    [entity] = derive(g).entities
    assert (entity.name, entity.key, entity.keys) == ('event', 'meta.event_id', ['events.meta.event_id'])


def test_two_declared_terms_may_not_name_one_entity() -> None:
    g = shop()
    _term(g, 'Client', ['model.shop.customers#customer_id'], kind='entity')
    g.add_node(
        Node('term.customer', 'term', 'Customer', path='definitions.yml', attrs={'line': 9, 'kind': 'entity'})
    )
    g.add_edge('term.customer', 'model.shop.customers#customer_id', 'defines')
    ontology = derive(g)
    assert [e.name for e in ontology.entities] == ['client', 'order']
    assert ontology.violations == [
        "entity-names-are-distinct: term 'Customer' (definitions.yml:9) would rename 'client', which term "
        "'Client' already names, to 'customer'; one entity takes one name"
    ]


def test_a_grain_with_qualifiers_names_its_entity() -> None:
    g = shop()
    g.nodes['term.revenue'].attrs['grain'] = 'one row per order per day'
    assert derive(g).measures[0].entity == 'order'


@pytest.mark.dbt
def test_the_example_ontology_is_the_same_in_any_order(jaffle_built_graph: Path) -> None:
    graph = Graph.read(jaffle_built_graph / 'konstruct-out' / 'graph.json')
    reordered = Graph()
    reordered.meta = graph.meta
    reordered.warnings = list(graph.warnings)
    for node in reversed(list(graph.nodes.values())):
        reordered.add_node(node)
    for edge in reversed(graph.edges):
        reordered.add_edge(edge.source, edge.target, edge.kind, **edge.attrs)
    assert derive(reordered).to_yaml() == derive(graph).to_yaml()


@pytest.mark.dbt
def test_impact_and_trace_name_the_business_entities_they_reach(jaffle_built_graph: Path) -> None:
    project = Project(jaffle_built_graph)
    impact = project.impact('stg_payments.amount')
    assert impact.entities == {'customer': ['customers'], 'order': ['orders'], 'payment': ['stg_payments']}
    assert '\nBusiness entities (3)\n  customer  customers\n' in str(impact)
    assert impact.to_dict()['entities']['order'] == ['orders']
    trace = project.trace('customers.customer_lifetime_value')
    assert set(trace.entities) == {'customer', 'payment'}
    assert Project(jaffle_built_graph).impact('raw_customers').entities == {
        'customer': ['customers', 'stg_customers']
    }


def test_a_relation_can_hold_several_entities(tmp_path: Path) -> None:
    (tmp_path / 'build.sql').write_text(
        'create table main.orders as select o.order_id, o.invoice_id, o.customer_id, o.amount from raw.orders o;\n'
        'create table main.users as select user_id, name from raw.users;\n'
        'create table main.accounts as select account_id, user_id, plan from raw.accounts;\n'
    )
    (tmp_path / 'definitions.yml').write_text(
        'terms:\n'
        '  - {name: Order, kind: entity, definition: A sale., columns: [main.orders.order_id]}\n'
        '  - {name: Invoice, kind: entity, definition: A bill., columns: [main.orders.invoice_id]}\n'
        '  - {name: Account, kind: entity, definition: A login., columns: [main.accounts.account_id]}\n'
        '  - name: User\n'
        '    kind: entity\n'
        '    definition: A person.\n'
        '    columns: [main.users.user_id, main.accounts.user_id]\n'
    )
    project = Project(tmp_path)
    project.build(dialect='duckdb', warehouses=False, run_dbt=False)
    assert project.impact('raw.orders.invoice_id').entities == {'invoice': ['orders'], 'order': ['orders']}
    # A key that is also another entity's foreign key keeps the relation's own entity.
    assert project.impact('raw.accounts.plan').entities == {'account': ['accounts'], 'user': ['accounts']}


# Confidence: verified, declared or inferred, never more than the evidence supports


def test_confidence_follows_the_evidence() -> None:
    ontology = derive(shop())
    customer, order = ontology.entities
    # A unique test is a person's statement; a join is only what the SQL does.
    assert (customer.confidence, order.confidence) == ('declared', 'declared')
    assert customer.check is None
    [rel] = ontology.relationships
    assert (rel.confidence, rel.check) == ('inferred', None)
    # Revenue is defined in definitions.yml, and verify confirmed the one lineage edge it has.
    [revenue] = ontology.measures
    assert revenue.confidence == 'verified'
    assert revenue.evidence == [
        'term in definitions.yml:3',
        'formula composed from the SQL of 1 column',
        'konstruct verify: 1 of 1 lineage edge confirmed',
    ]
    assert ontology.to_dict()['relationships'][0]['confidence'] == 'inferred'


def test_an_unconfirmed_edge_leaves_a_measure_declared() -> None:
    g = shop()
    for edge in g.out_edges('model.shop.orders#amount', 'derived_from'):
        edge.attrs['verified'] = 'not exercised'
    assert derive(g).measures[0].confidence == 'declared'


def test_a_tested_relationship_is_declared_and_a_join_inferred() -> None:
    g = shop()
    test = g.add_node(Node('test.shop.rel', 'test', 'relationships', attrs={'test': 'relationships'}))
    g.add_edge(test.id, 'model.shop.orders#customer_id', 'tests')
    g.add_edge(test.id, 'model.shop.customers#customer_id', 'tests', role='relationship target')
    assert derive(g).relationships[0].confidence == 'declared'


def test_names_grains_and_packs_are_only_inferred() -> None:
    g = Graph()
    g.meta['dialect'] = 'duckdb'
    stores = _relation(g, 'store_sales', ['store_id', 'revenue'])
    g.nodes[f'{stores.id}#revenue'].attrs['expression'] = 'SUM(x)'
    _term(g, 'Store revenue', [f'{stores.id}#revenue'], kind='measure', grain='one row per store')
    charges = _relation(g, 'charges', ['id', 'amount'], kind='source')
    _term(g, 'Stripe charge', [f'{charges.id}#id'], kind='entity', registry='stripe', status='draft')
    _term(g, 'Charge amount', [f'{charges.id}#amount'], kind='measure', registry='stripe')
    ontology = derive(g)
    entities = {e.name: e for e in ontology.entities}
    # A grain names the store; a pack matched the charges table by its name. Neither is a person
    # saying so of this project.
    assert entities['store'].confidence == 'inferred'
    assert entities['stripe_charge'].confidence == 'inferred'
    measures = {m.name: m for m in ontology.measures}
    assert measures['Charge amount'].confidence == 'inferred'
    assert measures['Charge amount'].evidence == ['ontology pack stripe, bound by the names of the tables']
    assert measures['Store revenue'].confidence == 'declared'


def test_verify_checks_make_keys_and_links_verified_or_failed() -> None:
    g = shop()
    g.meta['integrity'] = {
        'keys': {
            'model.shop.customers#customer_id': {'state': 'confirmed', 'detail': 'customers.customer_id ok'},
            'model.shop.orders#order_id': {'state': 'failed', 'detail': 'orders.order_id has 2 duplicates'},
        },
        'links': {
            'model.shop.orders#customer_id|model.shop.customers#customer_id': {
                'state': 'failed',
                'detail': '1 of 3 values of orders.customer_id are not in customers.customer_id',
            }
        },
    }
    ontology = derive(g)
    customer, order = ontology.entities
    assert (customer.confidence, customer.check) == ('verified', 'confirmed')
    assert 'konstruct verify: customers.customer_id ok' in customer.evidence
    # A failed check never raises confidence: the order stays as declared as its unique test.
    assert (order.confidence, order.check) == ('declared', 'failed')
    [rel] = ontology.relationships
    assert (rel.confidence, rel.check) == ('inferred', 'failed')
    assert rel.evidence[-1].startswith('konstruct verify: 1 of 3 values')
    assert rel.columns is not None
    g.meta['integrity']['links'][link_key(*rel.columns)]['state'] = 'confirmed'
    assert derive(g).relationships[0].confidence == 'verified'


def test_an_entity_is_only_as_trusted_as_its_least_trusted_key() -> None:
    g = shop()
    # A pack binds a second key of customer by table names: no person stated that one.
    crm = _relation(g, 'crm_customers', ['customer_id'])
    _term(
        g,
        'Customer',
        ['model.shop.customers#customer_id', f'{crm.id}#customer_id'],
        kind='entity',
        registry='crm',
    )
    customer = next(e for e in derive(g).entities if e.name == 'customer')
    assert len(customer.key_ids) == 2 and customer.confidence == 'inferred'
    # verify confirmed one of its two keys: not enough to call the entity verified.
    g = shop()
    crm = _relation(g, 'crm_customers', ['customer_id'])
    _unique(g, crm, 'customer_id')
    g.meta['integrity'] = {
        'keys': {'model.shop.customers#customer_id': {'state': 'confirmed', 'detail': 'customers ok'}},
        'links': {},
    }
    customer = next(e for e in derive(g).entities if e.name == 'customer')
    assert (customer.confidence, customer.check) == ('declared', None)
    assert customer.evidence[-1] == 'konstruct verify did not check crm_customers.customer_id'
    g.meta['integrity']['keys']['model.shop.crm_customers#customer_id'] = {'state': 'failed', 'detail': 'dup'}
    customer = next(e for e in derive(g).entities if e.name == 'customer')
    assert (customer.confidence, customer.check) == ('declared', 'failed')


def test_a_refuted_key_takes_away_the_cardinality_it_gave() -> None:
    g = shop()
    g.meta['integrity'] = {
        'keys': {
            'model.shop.customers#customer_id': {
                'state': 'failed',
                'detail': 'customers.customer_id has 3 duplicate values in 10 rows',
            }
        },
        'links': {
            'model.shop.orders#customer_id|model.shop.customers#customer_id': {
                'state': 'confirmed',
                'detail': 'all found',
            }
        },
    }
    [rel] = derive(g).relationships
    # Every order has its customer, but a repeated customer_id is no key: the cardinality is gone,
    # and the relationship is not verified.
    assert (rel.cardinality, rel.check, rel.confidence) == (None, 'failed', 'inferred')
    assert 'so its cardinality is unknown' in rel.evidence[-2]


def test_a_refuted_referring_key_makes_one_to_one_many_to_one() -> None:
    g = shop()
    _unique(g, g.nodes['model.shop.orders'], 'customer_id')
    assert derive(g).relationships[0].cardinality == 'one-to-one'
    g.meta['integrity'] = {
        'keys': {
            'model.shop.orders#customer_id': {'state': 'failed', 'detail': 'orders.customer_id repeats'}
        },
        'links': {},
    }
    [rel] = derive(g).relationships
    assert rel.cardinality == 'many-to-one'
    assert rel.evidence[-1].endswith('so it is many-to-one, not one-to-one')


def test_lineage_never_verifies_a_pack_binding_an_expression_or_a_conflict() -> None:
    # Revenue's one lineage edge is confirmed; bound by a pack, it stays inferred.
    g = shop()
    g.nodes['term.revenue'].attrs['registry'] = 'stripe'
    assert derive(g).measures[0].confidence == 'inferred'
    # An Ossie metric's own expression is not what verify checks.
    g = shop()
    g.nodes['term.revenue'].attrs['formula'] = 'SUM(orders.amount) * 1.2'
    revenue = derive(g).measures[0]
    assert revenue.confidence == 'declared'
    assert 'its own expression is not checked by konstruct verify' in revenue.evidence
    # Two columns that compute the measure differently, each with confirmed lineage.
    g = shop()
    payments = _relation(g, 'raw_payments', ['amount'], kind='source')
    finance = _relation(g, 'finance', ['revenue'])
    g.nodes[f'{finance.id}#revenue'].attrs['expression'] = 'SUM(db.main.raw_payments.amount)'
    g.add_edge(
        f'{finance.id}#revenue',
        f'{payments.id}#amount',
        'derived_from',
        transform='transformed',
        verified='confirmed',
    )
    g.add_edge('term.revenue', f'{finance.id}#revenue', 'defines')
    revenue = derive(g).measures[0]
    assert revenue.confidence == 'declared'
    assert 'its columns compute it in different ways (one-formula-per-measure)' in revenue.evidence


@pytest.mark.parametrize('marked', ['history', 'raw'])
def test_a_pack_presumes_no_key_where_its_identifier_repeats(marked: str) -> None:
    from konstruct.semantics.ontology import derivation

    g = Graph()
    charges = _relation(g, 'charges', ['id', 'amount'], kind='source')
    _term(g, 'Stripe charge', [f'{charges.id}#id'], kind='entity', registry='stripe')
    assert derivation(g).presumed == {f'{charges.id}#id': 'stripe'}
    if marked == 'history':
        g.add_column(charges, '_fivetran_active')  # a row for each version of a charge
    else:
        charges.attrs['loader_role'] = 'raw'
    assert derivation(g).presumed == {}
