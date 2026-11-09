"""The query API, through konstruct.Project, on the built jaffle_shop example."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import konstruct
from konstruct import Item, Project, QueryError, Walk

pytestmark = pytest.mark.dbt


@pytest.fixture
def project(jaffle_built_graph: Path, tmp_path: Path) -> Project:
    copy = tmp_path / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph / 'konstruct-out', copy / 'konstruct-out')
    return Project(copy)


def labels(items: list[Item]) -> list[str]:
    return [i.label for i in items]


def test_package_exports_the_api() -> None:
    assert konstruct.Project is Project
    assert set(konstruct.__all__) >= {'Project', 'Walk', 'Item', 'QueryError', 'Graph'}


def test_project_finds_the_graph_from_a_subdirectory(jaffle_built_graph: Path) -> None:
    nested = Project(jaffle_built_graph / 'models' / 'staging')
    assert nested.out_dir == jaffle_built_graph / 'konstruct-out'
    assert nested.root == jaffle_built_graph
    assert repr(nested).startswith('Project(')


def test_impact_of_a_staging_column(project: Project) -> None:
    walk = project.impact('stg_payments.amount')
    assert isinstance(walk, Walk)
    assert walk.direction == 'downstream'
    assert walk.target.id == 'model.jaffle_shop.stg_payments#amount'
    assert set(labels(walk.columns)) == {
        'customers.customer_lifetime_value',
        'orders.amount',
        'orders.credit_card_amount',
        'orders.coupon_amount',
        'orders.bank_transfer_amount',
        'orders.gift_card_amount',
        'customer_ltv.lifetime_value',
    }
    assert labels(walk.relations) == ['customers', 'orders']
    assert labels(walk.joins_and_filters) == ['customer_ltv']
    assert labels(walk.exposures) == ['customer_dashboard']
    assert set(labels(walk.tasks)) == {
        'jaffle_shop_daily.dbt_run_marts',
        'jaffle_shop_daily.build_customer_ltv',
    }
    assert 'not_null_orders_amount' in labels(walk.tests)
    ltv = next(c for c in walk.columns if c.label == 'customer_ltv.lifetime_value')
    assert (ltv.depth, ltv.transform, ltv.through) == (2, 'direct', 'jaffle_shop_daily.build_customer_ltv')
    assert walk.total == 7 + 2 + 1 + 5 + 1 + 2 + 2
    assert (
        walk.summary()
        == '7 columns, 3 relations, 5 tests, 1 exposure, 2 tasks and 2 business terms downstream.'
    )


def test_impact_of_a_join_key_and_of_a_relation(project: Project) -> None:
    walk = project.impact('stg_orders.customer_id')
    assert 'customers' in labels(walk.joins_and_filters)
    assert 'orders.customer_id' in labels(walk.columns)
    whole = project.impact('stg_orders')
    assert {'orders', 'customers', 'customer_ltv'} <= set(labels(whole.relations))
    shallow = project.impact('stg_orders', max_depth=1)
    assert 'customer_ltv' not in labels(shallow.relations)


def test_trace_to_the_seed(project: Project) -> None:
    walk = project.trace('customer_ltv.lifetime_value')
    assert labels(walk.columns) == [
        'customers.customer_lifetime_value',
        'stg_payments.amount',
        'raw_payments.amount',
    ]
    assert [c.transform for c in walk.columns] == ['direct', 'transformed', 'transformed']
    assert 'jaffle_shop_daily.build_customer_ltv' in labels(walk.tasks)
    assert labels(walk.filtered_by) == ['customers.customer_lifetime_value']
    assert project.trace('raw_payments.amount').summary() == '1 task upstream.'


def test_path(project: Project) -> None:
    result = project.path_between('raw_orders.id', 'customers.number_of_orders')
    assert result.found
    assert labels(result.steps) == ['raw_orders.id', 'stg_orders.order_id', 'customers.number_of_orders']
    assert [s.direction for s in result.steps] == [None, 'backward', 'backward']
    assert str(project.path_between('raw_payments', 'customer_dashboard')).splitlines() == [
        'Path from raw_payments to customer_dashboard (3 steps)',
        '  raw_payments        seed      seeds/raw_payments.csv',
        '  v feeds',
        '  stg_payments        model     models/staging/stg_payments.sql',
        '  v feeds',
        '  customers           model     models/customers.sql',
        '  v feeds',
        '  customer_dashboard  exposure  models/exposures.yml',
    ]


def test_search_and_list(project: Project) -> None:
    result = project.search('lifetime value')
    # The business term, then the columns that hold it.
    assert [r.label for r in result.results[:2]] == [
        'Customer lifetime value',
        'customers.customer_lifetime_value',
    ]
    assert result.results[0].score is not None
    assert project.search('payments', kinds=['seed']).results[0].label == 'raw_payments'
    assert str(project.search('zzz')) == "Nothing matches 'zzz'."
    models = labels(project.list(['model']).items)
    assert models == ['customers', 'orders', 'stg_customers', 'stg_orders', 'stg_payments']
    everything = project.list()
    assert {i.kind for i in everything.items} == {'model', 'seed', 'table'}
    assert str(project.list(['snapshot'])) == 'No snapshot nodes.'


def test_describe(project: Project) -> None:
    described = project.describe('orders')
    assert described.relation == 'jaffle_shop.main.orders'
    assert [c.name for c in described.columns][:2] == ['order_id', 'customer_id']
    assert 'unique_orders_order_id' in described.tests
    assert 'customer_dashboard (exposure)' in described.downstream
    text = str(described)
    assert text.startswith('orders (model)\n  This table has basic information about orders')
    assert '\n  id            model.jaffle_shop.orders\n' in text
    assert 'Columns (9)' in text and '  order_id  ' in text
    task = project.describe('jaffle_shop_daily.dbt_run_marts')
    assert task.runs == ['customers', 'orders']
    assert (task.after, task.before, task.upstream, task.downstream) == (
        ['dbt_run_staging'],
        ['dbt_test'],
        [],
        [],
    )
    dag = project.describe('jaffle_shop_daily')
    assert dag.tasks[0] == 'dbt_seed'


def test_query(project: Project) -> None:
    result = project.query('what breaks if I change the payment method in stg_payments?')
    assert [m.label for m in result.matches[:2]] == ['Payment method', 'stg_payments.payment_method']
    assert [d.name for d in result.definitions] == ['Payment method']
    # Impact follows the column, not the term.
    assert result.impact is not None and result.lineage is None
    assert result.impact.target.label == 'stg_payments.payment_method'
    lineage = project.query('where does customer lifetime value come from')
    assert lineage.lineage is not None
    assert lineage.lineage.target.label == 'customers.customer_lifetime_value'
    assert str(project.query('zzz qqq')).startswith('Nothing in the graph matches')
    assert str(result).startswith('Definitions\n  Payment method  draft\n')
    assert '\n    owner       no owner\n' in str(result)
    assert '\nBest matches\n' in str(result)


def test_results_serialise_to_json(project: Project) -> None:
    for result in (
        project.impact('orders.amount'),
        project.trace('orders.amount'),
        project.path_between('raw_orders', 'orders'),
        project.search('orders'),
        project.describe('orders'),
        project.list(),
        project.query('orders amount'),
    ):
        assert json.loads(json.dumps(result.to_dict()))
    walk = project.impact('orders.amount').to_dict()
    assert walk['summary'].endswith('downstream.')
    assert all('label' in c and 'id' in c for c in walk['columns'])


def test_errors_suggest_and_disambiguate(project: Project) -> None:
    with pytest.raises(QueryError, match='did you mean'):
        project.impact('payment')
    with pytest.raises(QueryError, match='nothing in the graph'):
        project.trace('zzz')
    project.graph.add_node(konstruct.Node('table.x.y.orders', 'table', 'orders', relation='x.y.orders'))
    with pytest.raises(QueryError, match='ambiguous'):
        project.describe('orders')


def test_graph_is_reread_after_a_rebuild(project: Project) -> None:
    before = project.graph
    assert project.graph is before
    data = json.loads(project.graph_path.read_text())
    data['nodes'].append({'id': 'table.new', 'kind': 'table', 'name': 'brand_new'})
    project.graph_path.write_text(json.dumps(data))
    assert project.graph is not before
    assert project.describe('brand_new').node.kind == 'table'


def test_no_graph(tmp_path: Path) -> None:
    project = Project(tmp_path)
    assert not project.has_graph()
    assert project.changed_inputs() == []
    with pytest.raises(QueryError, match='run `konstruct build` first'):
        project.impact('x')


def test_build_through_the_api(tmp_path: Path) -> None:
    (tmp_path / 'x.sql').write_text('create table t2 as select a * 2 as b from t1')
    project = Project(tmp_path)
    result = project.build(dialect='duckdb', warehouses=False, run_dbt=False)
    assert result.outputs[0] == tmp_path / 'konstruct-out' / 'graph.json'
    assert project.has_graph()
    assert project.trace('t2.b').columns[0].label == 't1.a'
    again = project.build(update=True, dialect='duckdb', warehouses=False, run_dbt=False)
    assert again.cache_misses == 0
