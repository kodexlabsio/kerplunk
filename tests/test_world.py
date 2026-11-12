"""The world model: one entity, metrics defined and proposed, and how two entities connect."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from konstruct import Project
from konstruct.analysis.query import QueryError
from konstruct.core.graph import Graph, Node
from konstruct.inputs.dbt import semantic_metrics
from konstruct.interfaces.cli import main
from konstruct.semantics.ontology import Ontology, Relationship, derive
from konstruct.semantics.world import _compose, all_metrics, entity, metrics, relate, shortest

from .test_ontology import _relation, _term, _unique, shop


def _shop() -> Graph:
    """The ontology tests' shop, with payments joined to orders and a few more columns."""
    g = shop()
    customers = g.nodes['model.shop.customers']
    customers.attrs['conditions'] = ['GROUP BY: db.main.customers.customer_id']
    g.add_column(customers, 'first_order', data_type='DATE')
    g.nodes['model.shop.customers#name'].description = 'Full name'
    payments = _relation(g, 'payments', ['payment_id', 'order_id', 'amount', 'tax_rate'])
    _unique(g, payments, 'payment_id')
    payments.attrs['conditions'] = ['JOIN ON: db.main.payments.order_id = db.main.orders.order_id']
    for column, dtype in (('amount', 'DECIMAL(10,2)'), ('tax_rate', 'DOUBLE')):
        g.nodes[f'{payments.id}#{column}'].data_type = dtype
    # customers.lifetime is a sum per customer, first_order a minimum: one is a metric, one is not.
    lifetime = g.add_column(customers, 'lifetime', data_type='DOUBLE')
    lifetime.attrs['expression'] = 'COALESCE(SUM(db.main.payments.amount), 0)'
    g.add_edge(lifetime.id, f'{payments.id}#amount', 'derived_from', transform='transformed')
    first = g.nodes['model.shop.customers#first_order']
    first.attrs['expression'] = 'MIN(db.main.payments.amount)'
    g.add_edge(first.id, f'{payments.id}#amount', 'derived_from', transform='transformed')
    return g


# Entities


def test_an_entity_with_its_properties_relationships_and_metrics() -> None:
    detail = entity(_shop(), 'Customers')
    assert (detail.entity.name, detail.entity.key, detail.entity.confidence) == (
        'customer',
        'customer_id',
        'declared',
    )
    assert [p.name for p in detail.properties] == ['first_order', 'lifetime', 'name']
    name = detail.properties[-1]
    assert (name.columns, name.description) == (['customers.name'], 'Full name')
    assert [(r.source, r.target) for r in detail.relationships] == [('order', 'customer')]
    assert {m.name for m in detail.metrics} == {'customer lifetime', 'customer count'}
    data = detail.to_dict()
    assert data['name'] == 'customer' and data['properties'][0]['name'] == 'first_order'


def test_an_entity_property_names_what_it_refers_to() -> None:
    detail = entity(_shop(), 'order')
    customer_id = next(p for p in detail.properties if p.name == 'customer_id')
    assert customer_id.refers_to == 'customer'
    assert next(p for p in detail.properties if p.name == 'amount').terms == ['Revenue']


def test_an_unknown_entity_suggests_the_closest() -> None:
    with pytest.raises(QueryError, match=r"no entity called 'custmer'; did you mean customer\?"):
        entity(_shop(), 'custmer')


# Relating two entities


def test_relate_gives_the_path_its_cardinality_confidence_and_joins() -> None:
    g = _shop()
    connection = relate(g, 'payment', 'customer')
    assert [(h.start, h.end, h.cardinality) for h in connection.hops] == [
        ('payment', 'order', 'many-to-one'),
        ('order', 'customer', 'many-to-one'),
    ]
    assert (connection.found, connection.cardinality, connection.confidence) == (
        True,
        'many-to-one',
        'inferred',
    )
    assert connection.join == (
        'FROM db.main.payments AS payments\n'
        'JOIN db.main.orders AS orders ON orders.order_id = payments.order_id\n'
        'JOIN db.main.customers AS customers ON customers.customer_id = orders.customer_id'
    )
    back = relate(g, 'customer', 'payment')
    assert back.cardinality == 'one-to-many'
    assert [h.cardinality for h in back.hops] == ['one-to-many', 'one-to-many']
    assert back.join is not None
    assert back.join.startswith('FROM db.main.customers AS customers\nJOIN db.main.orders AS orders ON ')
    assert 'aggregate them before joining' in str(back)


def test_an_inferred_link_is_never_read_as_verified() -> None:
    g = _shop()
    # verify confirmed the payment to order link; the order to customer link is only a join.
    g.meta['integrity'] = {
        'keys': {},
        'links': {
            'model.shop.payments#order_id|model.shop.orders#order_id': {'state': 'confirmed', 'detail': 'ok'}
        },
    }
    connection = relate(g, 'payment', 'customer')
    assert [h.confidence for h in connection.hops] == ['verified', 'inferred']
    assert connection.confidence == 'inferred'
    assert relate(g, 'payment', 'order').confidence == 'verified'
    data = connection.to_dict()
    assert data['confidence'] == 'inferred' and 'verified' not in json.dumps(data['hops'][1])


def test_unconnected_and_identical_entities() -> None:
    g = _shop()
    stores = _relation(g, 'stores', ['store_id'])
    _unique(g, stores, 'store_id')
    assert not relate(g, 'customer', 'store').found
    assert 'No relationship connects' in str(relate(g, 'customer', 'store'))
    assert relate(g, 'customer', 'customer').cardinality == 'one-to-one'
    with pytest.raises(QueryError):
        relate(g, 'customer', 'nothing')
    with pytest.raises(QueryError):
        relate(g, 'nothing', 'customer')


def test_the_most_trusted_of_the_shortest_paths() -> None:
    def link(source: str, target: str, confidence: str) -> Relationship:
        return Relationship(source, target, f'{source}.x = {target}.x', 'many-to-one', confidence=confidence)

    ontology = Ontology(
        relationships=[
            link('a', 'b', 'inferred'),
            link('b', 'd', 'inferred'),
            link('a', 'c', 'verified'),
            link('c', 'd', 'declared'),
        ]
    )
    path = shortest(ontology, 'a', 'd')
    assert path is not None and [r.target for r, _ in path] == ['c', 'd']


def test_cardinalities_compose() -> None:
    assert _compose(['many-to-one', 'one-to-one', 'many-to-one']) == 'many-to-one'
    assert _compose(['many-to-one', 'one-to-many']) == 'many-to-many'
    assert _compose(['one-to-one']) == 'one-to-one'
    assert _compose(['many-to-one', None]) is None


@settings(max_examples=60, deadline=None)
@given(
    st.lists(st.tuples(st.integers(0, 7), st.integers(0, 7)), max_size=16),
    st.integers(0, 7),
    st.integers(0, 7),
)
def test_relate_finds_the_fewest_relationships(edges: list[tuple[int, int]], start: int, end: int) -> None:
    """Against breadth-first search over the same relationships, walked either way."""
    relationships = [Relationship(f'e{a}', f'e{b}', f'e{a}.k = e{b}.k') for a, b in edges if a != b]
    ontology = Ontology(relationships=relationships)
    seen, frontier, distance = {f'e{start}'}, [f'e{start}'], 0
    while frontier and f'e{end}' not in seen:
        distance += 1
        nxt = []
        for here in frontier:
            for r in relationships:
                for a, b in ((r.source, r.target), (r.target, r.source)):
                    if a == here and b not in seen:
                        seen.add(b)
                        nxt.append(b)
        frontier = nxt
    path = shortest(ontology, f'e{start}', f'e{end}')
    if f'e{end}' not in seen:
        assert path is None
    else:
        assert path is not None and len(path) == distance
        here = f'e{start}'
        for relationship, forward in path:
            assert here == (relationship.source if forward else relationship.target)
            here = relationship.target if forward else relationship.source
        assert here == f'e{end}'


# Metrics


def test_a_defined_metric_with_its_formula_grain_and_authority() -> None:
    g = _shop()
    g.nodes['term.revenue'].attrs['synonyms'] = ['sales']
    [revenue] = metrics(g, 'sales').metrics
    assert (revenue.name, revenue.source, revenue.status, revenue.proposed) == (
        'Revenue',
        'definitions',
        'approved',
        False,
    )
    assert (revenue.formula, revenue.aggregation, revenue.additive, revenue.grain) == (
        'SUM(raw_orders.amount)',
        'sum',
        True,
        'one row per order',
    )
    assert metrics(g, 'orders.amount').metrics == [revenue]
    with pytest.raises(QueryError, match='no metric called'):
        metrics(g, 'nothing at all')


def test_proposals_are_inferred_have_no_status_and_leave_out_extremes_rates_and_keys() -> None:
    every = {m.name: m for m in metrics(_shop()).metrics}
    proposed = {name: m for name, m in every.items() if m.proposed}
    assert set(proposed) == {
        'customer lifetime',
        'payment amount',
        'customer count',
        'order count',
        'payment count',
    }
    for metric in proposed.values():
        assert (metric.confidence, metric.status, metric.owner, metric.approved) == (
            'inferred',
            None,
            None,
            None,
        )
        assert 'status' not in metric.to_dict() and metric.to_dict()['proposed'] is True
    lifetime = proposed['customer lifetime']
    assert (lifetime.source, lifetime.formula, lifetime.aggregation, lifetime.grain) == (
        'aggregate',
        'COALESCE(SUM(payments.amount), 0)',
        'sum',
        'one row per customer',
    )
    assert lifetime.evidence[0] == 'proposed from the code: aggregates with SUM in customers.lifetime'
    amount = proposed['payment amount']
    assert (amount.source, amount.grain, [c.column for c in amount.columns]) == (
        'measure column',
        'one row per payment',
        ['payments.amount'],
    )
    count = proposed['customer count']
    assert (count.formula, count.additive) == ('COUNT(DISTINCT customers.customer_id)', False)
    # A minimum, a rate and the keys are not measures, and Revenue's column is defined already.
    columns = {c.column for m in proposed.values() for c in m.columns}
    assert not columns & {
        'customers.first_order',
        'payments.tax_rate',
        'payments.payment_id',
        'orders.amount',
    }
    assert every['Revenue'].proposed is False
    text = str(metrics(_shop()))
    assert 'Metrics: 1 defined metric and 5 proposals, inferred from the code and approved by no one.' in text
    assert 'customer lifetime  proposed  inferred' in text


def test_metrics_without_composing_list_their_columns_by_id() -> None:
    graph = _shop()
    ontology = derive(graph)
    composed = all_metrics(graph, ontology)
    bare = all_metrics(graph, ontology, compose=False)
    # The same metrics, in the same order and with the same trust; only the formulas are not composed.
    assert [(m.name, m.proposed, m.confidence) for m in bare] == [
        (m.name, m.proposed, m.confidence) for m in composed
    ]
    assert any(m.proposed and m.formula for m in composed if m.source == 'aggregate')
    for plain, full in zip(bare, composed, strict=True):
        assert [c.id for c in plain.columns] == [c.id for c in full.columns]
        assert all(c.id in graph.nodes for c in plain.columns)
        if plain.proposed and plain.source != 'entity count':
            assert plain.formula is None
            assert all(c.formula is None and not c.complete for c in plain.columns)


def test_a_grain_from_grouping_and_a_column_only_in_yaml() -> None:
    g = _shop()
    daily = _relation(g, 'customer_totals', ['customer_id', 'spend', 'notes'])
    daily.attrs['conditions'] = ['GROUP BY: db.main.payments.customer_id']
    g.nodes[f'{daily.id}#spend'].attrs['expression'] = 'AVG(db.main.payments.amount)'
    g.nodes[f'{daily.id}#notes'].attrs['documented_only'] = True
    [spend] = [m for m in metrics(g).metrics if m.columns and m.columns[0].column == 'customer_totals.spend']
    assert (spend.entity, spend.aggregation, spend.additive) == ('customer', 'average', False)
    assert '  aggregates        average, not additive' in str(metrics(g, 'customer_totals.spend'))


@pytest.fixture(scope='module')
def undefined_jaffle(jaffle_built_graph: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The example built without its definitions.yml: only the code to go on."""
    project = tmp_path_factory.mktemp('undefined') / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph, project)
    (project / 'definitions.yml').unlink()
    shutil.rmtree(project / 'konstruct-out')
    Project(project).build(run_dbt=False)
    return project


@pytest.mark.dbt
def test_the_example_without_definitions_proposes_its_metrics(undefined_jaffle: Path) -> None:
    project = Project(undefined_jaffle)
    assert project.ontology().measures == []
    every = project.metric().metrics
    assert every and all(m.proposed and m.confidence == 'inferred' and m.status is None for m in every)
    by_name = {m.name: m for m in every}
    ltv = by_name['customer lifetime value']
    assert (ltv.formula, ltv.grain, ltv.aggregation) == (
        'SUM(raw_payments.amount / 100)',
        'one row per customer',
        'sum',
    )
    assert by_name['order amount'].formula == 'SUM(raw_payments.amount / 100)'
    assert by_name['order amount'].grain == 'one row per order'
    assert by_name['number of orders'].aggregation == 'count'
    assert by_name['payment count'].formula == 'COUNT(DISTINCT stg_payments.payment_id)'
    assert by_name['payment amount'].grain == 'one row per payment'
    columns = {c.column for m in every for c in m.columns}
    # The dates of a customer's first and latest order are a minimum and a maximum, not measures;
    # the keys are identifiers.
    assert not columns & {
        'customers.first_order',
        'customers.most_recent_order',
        'customers.customer_id',
        'orders.order_id',
        'stg_payments.payment_id',
        'customers.total_order_amount',
    }
    assert not any('approved' in str(m.to_dict()) for m in every)


@pytest.mark.dbt
def test_the_example_entity_metric_and_relate(
    jaffle_built_graph: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = str(jaffle_built_graph)
    assert main(['--no-color', '-C', root, 'entity', 'customer']) == 0
    out = capsys.readouterr().out
    assert out.startswith("Entity customer (term 'Customer')\n")
    assert '\nProperties (7)\n' in out and '  first_name ' in out
    assert main(['--no-color', '-C', root, 'metric', 'lifetime', 'value', '--json']) == 0
    [ltv] = json.loads(capsys.readouterr().out)['metrics']
    assert (ltv['name'], ltv['status'], ltv['confidence']) == (
        'Customer lifetime value',
        'approved',
        'declared',
    )
    assert main(['--no-color', '-C', root, 'metric']) == 0
    assert 'Metrics: 2 defined metrics and ' in capsys.readouterr().out
    assert main(['--no-color', '-C', root, 'relate', 'customer', 'payment']) == 0
    out = capsys.readouterr().out
    assert 'customer -> order  one-to-many  orders.customer_id = customers.customer_id   declared' in out
    # Orders reach payments through stg_orders, joined on the order key, never on a name alone.
    assert 'JOIN jaffle_shop.main.stg_orders AS stg_orders ON stg_orders.order_id = orders.order_id' in out
    # Like define, a name that matches nothing exits with status 3.
    assert main(['--no-color', '-C', root, 'entity', 'nothing']) == 3
    assert main(['--no-color', '-C', root, 'metric', 'nothing', 'at', 'all']) == 3
    assert main(['--no-color', '-C', root, 'relate', 'customer', 'nothing']) == 3
    assert 'no metric called' in capsys.readouterr().err


@pytest.mark.dbt
def test_the_join_relate_writes_runs_on_the_warehouse(jaffle_built_graph: Path) -> None:
    import duckdb

    join = Project(jaffle_built_graph).relate('customer', 'payment').join
    assert join is not None
    con = duckdb.connect(str(jaffle_built_graph / 'jaffle_shop.duckdb'), read_only=True)
    try:
        [(rows,)] = con.execute(f'SELECT count(*) {join}').fetchall()
        [(payments,)] = con.execute('SELECT count(*) FROM main.stg_payments').fetchall()
    finally:
        con.close()
    assert rows == payments  # each payment reaches its customer once


def test_relate_exits_3_without_a_connection(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / 'shop.sql').write_text(
        'create table main.a (a_id int primary key);\ncreate table main.b (b_id int primary key);\n'
    )
    import duckdb

    con = duckdb.connect(str(tmp_path / 'shop.duckdb'))
    con.execute('create table main.a (a_id int primary key)')
    con.execute('create table main.b (b_id int primary key)')
    con.close()
    assert main(['--no-color', '-C', str(tmp_path), 'relate', 'a', 'b']) == 3


# Metrics people define in dbt's semantic layer, Looker and Power BI


MANIFEST = {
    'semantic_models': {
        'semantic_model.shop.orders': {
            'name': 'orders',
            'original_file_path': 'models/semantic.yml',
            'depends_on': {'nodes': ['model.shop.orders']},
            'measures': [
                {
                    'name': 'order_total',
                    'agg': 'sum',
                    'expr': 'amount',
                    'non_additive_dimension': {'name': 'order_date', 'window_choice': 'max'},
                },
                {'name': 'order_count', 'agg': 'count_distinct', 'expr': 'order_id'},
                {'name': 'spread', 'agg': 'percentile', 'expr': 'amount'},
                {'name': 'unnamed', 'agg': 'sum', 'expr': 'amount'},
            ],
        }
    },
    'metrics': {
        'metric.shop.revenue': {
            'name': 'revenue',
            'label': 'Revenue',
            'description': 'Sum of payments.',
            'type': 'simple',
            'original_file_path': 'models/semantic.yml',
            'type_params': {'measure': {'name': 'order_total'}, 'input_measures': [{'name': 'order_total'}]},
            'filter': {
                'where_filters': [{'where_sql_template': "{{ Dimension('order__status') }} = 'done'"}]
            },
        },
        'metric.shop.ratio': {
            'name': 'ratio',
            'type': 'derived',
            'original_file_path': 'models/semantic.yml',
            'type_params': {'expr': 'revenue / unique_orders', 'input_measures': []},
        },
        'metric.shop.unique_orders': {
            'name': 'unique_orders',
            'type': 'simple',
            'original_file_path': 'models/semantic.yml',
            'type_params': {
                'measure': {
                    'name': 'order_count',
                    'filter': {
                        'where_filters': [
                            {'where_sql_template': "{{ Dimension('order__status') }} <> 'void'"}
                        ]
                    },
                }
            },
        },
        'metric.shop.typical': {
            'name': 'typical',
            'type': 'simple',
            'original_file_path': 'models/semantic.yml',
            'type_params': {'measure': {'name': 'spread'}},
        },
    },
}


def test_dbt_semantic_layer_metrics_are_read_and_shown() -> None:
    found = semantic_metrics(MANIFEST, lambda item: item.get('original_file_path'))
    # A measure no metric names is not a metric of the semantic layer.
    assert [m['name'] for m in found] == ['ratio', 'revenue', 'typical', 'unique_orders']
    g = _shop()
    g.meta['dbt_metrics'] = found
    every = {m.name: m for m in metrics(g).metrics}
    revenue = every['Revenue']
    assert (revenue.source, revenue.confidence, revenue.status, revenue.proposed) == (
        'dbt semantic layer',
        'declared',
        None,
        False,
    )
    assert (revenue.formula, revenue.grain, revenue.filters) == (
        'SUM(orders.amount)',
        'one row per order',
        ["{{ Dimension('order__status') }} = 'done'"],
    )
    assert (revenue.grain_confidence, revenue.additive) == ('inferred', False)
    assert 'not additive across order_date, as its measure says' in revenue.evidence
    unique = every['unique_orders']
    assert (unique.formula, unique.additive) == ('COUNT(DISTINCT orders.order_id)', False)
    assert unique.filters == ["{{ Dimension('order__status') }} <> 'void'"]
    assert every['ratio'].formula == 'revenue / unique_orders'
    assert every['typical'].formula is None  # a percentile needs its parameters to be SQL


def test_looker_and_power_bi_measures_are_metrics(tmp_path: Path) -> None:
    from .test_powerbi import write_model

    write_model(tmp_path)
    (tmp_path / 'views').mkdir()
    (tmp_path / 'views' / 'orders.view.lkml').write_text(
        'view: orders {\n'
        '  sql_table_name: analytics.mart.orders ;;\n'
        '  dimension: order_id { primary_key: yes sql: ${TABLE}.order_id ;; }\n'
        '  measure: total_amount { type: sum sql: ${TABLE}.amount ;; }\n'
        '  measure: orders { type: count }\n'
        '}\n'
    )
    graph = Project(tmp_path).build(dialect='duckdb', warehouses=False, run_dbt=False).graph
    every = {(m.source, m.name): m for m in metrics(graph).metrics}
    looker = every[('looker', 'total_amount')]
    assert (looker.confidence, looker.formula, looker.aggregation) == (
        'declared',
        'SUM(${TABLE}.amount)',
        'sum',
    )
    assert [c.column for c in looker.columns] == ['orders.amount']
    assert every[('looker', 'orders')].formula == 'COUNT(*)'
    revenue = every[('power bi', 'Revenue')]
    assert (revenue.confidence, revenue.formula, revenue.status) == ('declared', 'SUM(Orders[Amount])', None)
    assert [c.column for c in revenue.columns] == ['orders.amount']


def test_a_term_from_a_pack_is_an_inferred_metric() -> None:
    g = Graph()
    g.meta['dialect'] = 'duckdb'
    charges = _relation(g, 'charges', ['id', 'amount'], kind='source')
    term = _term(g, 'Charge amount', [f'{charges.id}#amount'], kind='measure', registry='stripe')
    term.origins = ['registry']
    [metric] = [m for m in metrics(g).metrics if not m.proposed]
    assert (metric.source, metric.confidence) == ('ontology pack', 'inferred')


def test_the_world_model_on_a_graph_without_entities() -> None:
    g = Graph()
    assert metrics(g).metrics == []
    assert derive(g).entities == []
    node = g.add_node(Node('model.x.t', 'model', 't', relation='db.main.t'))
    g.add_column(node, 'amount', data_type='INTEGER')
    # No entity holds the table, so its amount has no grain and is not proposed.
    assert metrics(g).metrics == []


# Failed checks are carried, never overlooked


def _failed_customer(g: Graph) -> None:
    g.meta['integrity'] = {
        'keys': {
            'model.shop.customers#customer_id': {
                'state': 'failed',
                'detail': 'customers.customer_id has 3 duplicate values in 10 rows',
            }
        },
        'links': {
            'model.shop.orders#customer_id|model.shop.customers#customer_id': {
                'state': 'failed',
                'detail': 'all 30 values found, but customers.customer_id is not unique',
            }
        },
    }


def test_relate_carries_a_failed_check_into_the_path() -> None:
    g = _shop()
    _failed_customer(g)
    connection = relate(g, 'order', 'customer')
    [hop] = connection.hops
    assert (hop.cardinality, hop.check) == (None, 'failed')
    assert (connection.cardinality, connection.check) == (None, 'failed')
    data = connection.to_dict()
    assert data['check'] == 'failed' and data['hops'][0]['check'] == 'failed' and 'cardinality' not in data
    text = str(connection)
    assert '  cardinality  unknown\n  confidence   inferred  check failed\n' in text
    assert 'the joins can lose or repeat rows' in text
    assert relate(_shop(), 'order', 'customer').check is None


def test_a_failed_relationship_is_walked_last() -> None:
    from konstruct.semantics.ontology import Ontology

    def link(source: str, target: str, confidence: str, check: str | None = None) -> Relationship:
        return Relationship(
            source, target, f'{source}.x = {target}.x', 'many-to-one', confidence=confidence, check=check
        )

    ontology = Ontology(
        relationships=[
            link('a', 'd', 'declared', 'failed'),  # one relationship, but the data contradicts it
            link('a', 'b', 'inferred'),
            link('b', 'd', 'inferred'),
        ]
    )
    path = shortest(ontology, 'a', 'd')
    assert path is not None and [(r.source, r.target) for r, _ in path] == [('a', 'b'), ('b', 'd')]


def test_relate_joins_another_relation_of_an_entity_on_its_key() -> None:
    g = _shop()
    # payments refer to staged orders, which hold the same order entity as orders.
    staged = _relation(g, 'stg_orders', ['order_id', 'customer_id'])
    _unique(g, staged, 'order_id')
    g.nodes['model.shop.payments'].attrs['conditions'] = [
        'JOIN ON: db.main.payments.order_id = db.main.stg_orders.order_id'
    ]
    join = relate(g, 'customer', 'payment').join
    assert join == (
        'FROM db.main.customers AS customers\n'
        'JOIN db.main.orders AS orders ON orders.customer_id = customers.customer_id\n'
        'JOIN db.main.stg_orders AS stg_orders ON stg_orders.order_id = orders.order_id\n'
        'JOIN db.main.payments AS payments ON payments.order_id = stg_orders.order_id'
    )


def test_relate_joins_another_relation_of_an_entity_only_on_a_key_shown_unique() -> None:
    """stg_orders holds the order entity, as a definition says, but nothing shows its key
    unique: joining it on that key could repeat each order, so relate writes no joins."""
    g = _shop()
    staged = _relation(g, 'stg_orders', ['order_id', 'customer_id'])
    _term(g, 'Order', ['model.shop.orders#order_id', f'{staged.id}#order_id'], kind='entity')
    g.nodes['model.shop.payments'].attrs['conditions'] = [
        'JOIN ON: db.main.payments.order_id = db.main.stg_orders.order_id'
    ]
    connection = relate(g, 'customer', 'payment')
    assert connection.found and connection.join is None


# What is and is not proposed


def _proposals(g: Graph) -> dict[str, Any]:
    return {m.name: m for m in metrics(g).metrics if m.proposed}


def test_extremes_medians_ratios_and_mixed_aggregates_are_not_proposed() -> None:
    g = _shop()
    customers = g.nodes['model.shop.customers']
    for name, expression in (
        ('largest_payment', 'MAX(db.main.payments.amount)'),
        ('smallest_amount', 'MIN(db.main.payments.amount)'),
        ('typical_spend', 'MEDIAN(db.main.payments.amount)'),
        ('revenue_per_order', 'SUM(db.main.payments.amount) / COUNT(db.main.orders.order_id)'),
        ('gross_margin_amount', 'SUM(db.main.payments.amount) - SUM(db.main.payments.amount) * 0.2'),
        ('running_total', 'SUM(db.main.payments.amount) OVER (ORDER BY db.main.payments.payment_id)'),
    ):
        column = g.add_column(customers, name, data_type='DOUBLE')
        column.attrs['expression'] = expression
        g.add_edge(column.id, 'model.shop.payments#amount', 'derived_from', transform='transformed')
    proposed = _proposals(g)
    columns = {c.column for m in proposed.values() for c in m.columns}
    assert not columns & {f'customers.{n}' for n in ('largest_payment', 'smallest_amount', 'typical_spend')}
    assert not columns & {
        'customers.revenue_per_order',
        'customers.gross_margin_amount',
        'customers.running_total',
    }


def test_a_value_repeated_from_a_coarser_grain_is_not_proposed() -> None:
    g = _shop()
    payments = g.nodes['model.shop.payments']
    column = g.add_column(payments, 'order_amount', data_type='DOUBLE')
    column.attrs['expression'] = 'db.main.orders.amount'
    g.add_edge(column.id, 'model.shop.orders#amount', 'derived_from', transform='direct')
    # Each payment of an order repeats the order's amount: summed over payments it double counts.
    assert 'order amount' not in _proposals(g)
    assert 'payment amount' in _proposals(g)


def test_a_price_or_a_balance_has_no_known_additivity() -> None:
    g = _shop()
    payments = g.nodes['model.shop.payments']
    for name in ('unit_price', 'account_balance'):
        g.add_column(payments, name, data_type='DECIMAL(10,2)')
    proposed = _proposals(g)
    for name in ('unit price', 'account balance'):
        assert proposed[name].additive is None, name
        assert 'a price or a balance: a sum of it over rows is not a total' in proposed[name].evidence
    assert proposed['payment amount'].additive is True
    assert '  additive          unknown' in str(metrics(g, 'payments.unit_price'))


def test_a_plugin_measure_is_named_for_its_input_and_only_inferred() -> None:
    g = _shop()
    node = g.add_node(
        Node(
            'tableau.workbook.sales',
            'exposure',
            'sales',
            path='sales.twb',
            origins=['tableau'],
            attrs={'type': 'tableau workbook', 'measures': {'Guessed revenue': 'SUM(amount)'}},
        )
    )
    assert node.attrs['type'] == 'tableau workbook'
    [metric] = [m for m in metrics(g).metrics if m.name == 'Guessed revenue']
    assert (metric.source, metric.confidence) == ('tableau', 'inferred')
    assert metric.evidence == ['tableau workbook sales (sales.twb), read by the tableau input']


def test_a_name_composes_only_the_metrics_it_finds(monkeypatch: pytest.MonkeyPatch) -> None:
    from konstruct.semantics import world

    g = _shop()
    composed: list[str] = []
    real = world.measure_column  # type: ignore[attr-defined]

    def counted(graph: Graph, explainer: Any, column: Node) -> Any:
        composed.append(graph.label(column))
        return real(graph, explainer, column)

    monkeypatch.setattr(world, 'measure_column', counted)
    found = metrics(g, 'payments.amount')
    assert [m.name for m in found.metrics] == ['payment amount']
    assert composed == ['payments.amount']
    every = metrics(g, limit=2)
    assert (len(every.metrics), every.total) == (2, 6)
    assert 'of 6 found' in str(every)


def test_the_project_derives_the_ontology_once_per_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from konstruct.semantics import ontology

    (tmp_path / 'shop.sql').write_text(
        'create table main.orders as select 1 as order_id, 2 as customer_id, 3.0 as amount'
    )
    project = Project(tmp_path)
    project.build(warehouses=False)
    calls: list[int] = []
    real = ontology.derive

    def counted(graph: Graph) -> Ontology:
        calls.append(1)
        return real(graph)

    monkeypatch.setattr(ontology, 'derive', counted)
    project.metric()
    project.metric()
    with pytest.raises(QueryError):
        project.entity('nothing')
    assert len(calls) == 1
    project.build(warehouses=False)  # which derives the ontology itself, for ontology.yml
    assert len(calls) == 2
    project.metric()  # a rebuilt graph is derived again
    assert len(calls) == 3


def _orders_with_invoices() -> Graph:
    """orders hold one row per order and per invoice; customers point at their last invoice."""
    g = Graph()
    g.meta['dialect'] = 'duckdb'
    orders = _relation(g, 'orders', ['order_id', 'invoice_id', 'customer_id'])
    customers = _relation(g, 'customers', ['customer_id', 'last_invoice_id'])
    payments = _relation(g, 'payments', ['payment_id', 'order_id'])
    for relation, column in ((orders, 'order_id'), (orders, 'invoice_id'), (customers, 'customer_id')):
        _unique(g, relation, column)
    _unique(g, payments, 'payment_id')
    orders.attrs['conditions'] = ['JOIN ON: db.main.orders.customer_id = db.main.customers.customer_id']
    customers.attrs['conditions'] = ['JOIN ON: db.main.customers.last_invoice_id = db.main.orders.invoice_id']
    payments.attrs['conditions'] = ['JOIN ON: db.main.payments.order_id = db.main.orders.order_id']
    return g


def test_two_entities_on_one_row_need_no_join() -> None:
    connection = relate(_orders_with_invoices(), 'order', 'invoice')
    [hop] = connection.hops
    assert (hop.via, hop.cardinality, connection.cardinality) == (
        'one row of orders',
        'one-to-one',
        'one-to-one',
    )
    assert connection.join == 'FROM db.main.orders AS orders'
    assert hop.evidence == [
        'orders holds one row per invoice (orders.invoice_id) and per order (orders.order_id), both unique there'
    ]


def test_a_path_back_through_a_relation_joins_it_again_under_another_alias() -> None:
    from konstruct.semantics.world import _joins

    g = _orders_with_invoices()
    ontology = derive(g)
    walk = {(r.source, r.target): r for r in ontology.relationships}
    # The path to the customer's last invoice comes back to orders, under a second alias.
    path = [
        (walk[('payment', 'order')], True),
        (walk[('order', 'customer')], True),
        (walk[('customer', 'invoice')], True),
    ]
    assert _joins(g, ontology, path) == (
        'FROM db.main.payments AS payments\n'
        'JOIN db.main.orders AS orders ON orders.order_id = payments.order_id\n'
        'JOIN db.main.customers AS customers ON customers.customer_id = orders.customer_id\n'
        'JOIN db.main.orders AS orders_2 ON orders_2.invoice_id = customers.last_invoice_id'
    )


def test_a_pack_measure_grain_is_inferred() -> None:
    g = _shop()
    g.nodes['term.revenue'].origins = ['registry']
    g.nodes['term.revenue'].attrs['registry'] = 'stripe'
    [revenue] = [m for m in metrics(g).metrics if m.name == 'Revenue']
    assert (revenue.source, revenue.grain, revenue.grain_confidence) == (
        'ontology pack',
        'one row per order',
        'inferred',
    )
    assert metrics(_shop(), 'Revenue').metrics[0].grain_confidence == 'declared'


def test_a_column_a_bi_measure_reads_is_not_proposed_again() -> None:
    g = _shop()
    view = g.add_node(
        Node(
            'looker.view.payments',
            'exposure',
            'payments',
            origins=['looker'],
            attrs={'type': 'looker view', 'measures': {'total': 'SUM(${TABLE}.amount)'}},
        )
    )
    g.add_edge('model.shop.payments#amount', view.id, 'feeds', fields=['total'])
    names = {m.name: m for m in metrics(g).metrics}
    assert 'payment amount' not in names
    assert names['total'].source == 'looker'


def test_the_project_never_hands_out_its_kept_ontology(tmp_path: Path) -> None:
    from .test_integrity import shop as warehouse_shop

    project = warehouse_shop(tmp_path)
    project.verify(warehouse=tmp_path / 'shop.duckdb')
    assert project.entity('customer').entity.confidence == 'verified'
    # A change to meta in memory, as a script recording its own checks makes, is seen at once,
    # by entity() and ontology() alike.
    key = next(k for k in project.graph.meta['integrity']['keys'] if 'customers' in k)
    project.graph.meta['integrity']['keys'][key] = {'state': 'failed', 'detail': 'repeats'}
    assert project.entity('customer').entity.check == 'failed'
    assert next(e for e in project.ontology().entities if e.name == 'customer').check == 'failed'
    # What a caller changes in a result changes no later answer.
    detail = project.entity('order')
    detail.entity.confidence = 'inferred'
    detail.entity.evidence.append('changed')
    detail.relationships[0].via = 'changed'
    project.ontology().entities[0].key = 'changed'
    again = project.entity('order')
    assert again.entity.confidence == 'verified' and 'changed' not in again.entity.evidence
    assert again.relationships[0].via != 'changed'
    assert 'changed' not in {e.key for e in project.ontology().entities}


def test_a_shared_row_in_the_middle_of_a_path_is_taken_before_a_detour() -> None:
    """payment reaches its own order's invoice along the order's row, not through the customer's
    last invoice, which is another invoice."""
    import duckdb

    g = _orders_with_invoices()
    connection = relate(g, 'payment', 'invoice')
    assert [(h.end, h.same_row) for h in connection.hops] == [('order', False), ('invoice', True)]
    assert connection.join == (
        'FROM db.main.payments AS payments\nJOIN db.main.orders AS orders ON orders.order_id = payments.order_id'
    )
    assert str(connection).startswith('How payment -> invoice (1 relationship, 1 shared row)')
    con = duckdb.connect()
    try:
        con.execute("attach ':memory:' as db")
        con.execute('create table db.main.payments as select 1 as payment_id, 10 as order_id')
        con.execute(
            'create table db.main.orders as select * from (values (10, 100, 7), (11, 101, 7)) '
            't(order_id, invoice_id, customer_id)'
        )
        con.execute('create table db.main.customers as select 7 as customer_id, 101 as last_invoice_id')
        assert con.execute(f'SELECT orders.invoice_id {connection.join}').fetchall() == [(100,)]
    finally:
        con.close()


def test_a_row_step_needs_both_key_columns_unique_in_that_relation() -> None:
    """A definition lists orders.customer_id as a key of customer, untested: orders rows are not
    one per customer, so no row step makes order to customer one-to-one."""
    g = Graph()
    g.meta['dialect'] = 'duckdb'
    orders = _relation(g, 'orders', ['order_id', 'customer_id'])
    customers = _relation(g, 'customers', ['customer_id'])
    _unique(g, orders, 'order_id')
    _unique(g, customers, 'customer_id')
    _term(g, 'Customer', [f'{customers.id}#customer_id', f'{orders.id}#customer_id'], kind='entity')
    assert not relate(g, 'order', 'customer').found
    test = g.add_node(Node('test.rel', 'test', 'relationships', attrs={'test': 'relationships'}))
    g.add_edge(test.id, f'{orders.id}#customer_id', 'tests')
    g.add_edge(test.id, f'{customers.id}#customer_id', 'tests', role='relationship target')
    connection = relate(g, 'order', 'customer')
    [hop] = connection.hops
    assert (hop.same_row, connection.cardinality, connection.confidence) == (False, 'many-to-one', 'declared')
    assert relate(g, 'customer', 'order').cardinality == 'one-to-many'


def test_a_row_step_is_as_trusted_as_its_two_key_columns() -> None:
    g = _orders_with_invoices()
    # The order entity has another key, untested elsewhere; the step rests on orders' own keys.
    staged = _relation(g, 'stg_orders', ['order_id'])
    _term(g, 'Order', ['model.shop.orders#order_id', f'{staged.id}#order_id'], kind='entity', registry='pack')
    assert next(e for e in derive(g).entities if e.name == 'order').confidence == 'inferred'
    [hop] = relate(g, 'order', 'invoice').hops
    assert (hop.same_row, hop.confidence) == (True, 'declared')
    g.meta['integrity'] = {
        'keys': {
            'model.shop.orders#order_id': {'state': 'confirmed', 'detail': 'ok'},
            'model.shop.orders#invoice_id': {'state': 'confirmed', 'detail': 'ok'},
        },
        'links': {},
    }
    [hop] = relate(g, 'order', 'invoice').hops
    assert (hop.confidence, hop.check) == ('verified', 'confirmed')
    g.meta['integrity']['keys']['model.shop.orders#invoice_id'] = {'state': 'failed', 'detail': 'repeats'}
    connection = relate(g, 'order', 'invoice')
    [hop] = connection.hops
    assert (hop.same_row, hop.cardinality, hop.check, connection.check) == (True, None, 'failed', 'failed')


def test_a_relation_grouped_in_two_readings_by_one_key_keeps_its_grain() -> None:
    """Two CTEs that each group a relation by the same key, joined to each other, group two readings
    of it by that key: what is built of them holds one row per that key's entity."""
    g = _shop()
    spend = _relation(g, 'spend', ['customer_id', 'total'])
    spend.attrs['conditions'] = [
        'GROUP BY: db.main."orders#1".customer_id',
        'GROUP BY: db.main."orders#2".customer_id',
    ]
    total = g.nodes[f'{spend.id}#total']
    total.attrs['expression'] = 'SUM(db.main."orders#1".amount)'
    g.add_edge(total.id, 'model.shop.orders#amount', 'derived_from', transform='transformed')
    [proposed] = [m for m in metrics(g).metrics if [c.column for c in m.columns] == ['spend.total']]
    assert proposed.grain == 'one row per customer'
