from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

import duckdb
import pytest

from konstruct.build import Cache, RelationIndex, build
from konstruct.config import load_config
from konstruct.graph import Graph, Node

from .test_warehouse import require


@pytest.fixture
def jaffle_graph(jaffle_built_graph: Path) -> Graph:
    return Graph.read(jaffle_built_graph / 'konstruct-out' / 'graph.json')


def edges(graph: Graph, kind: str) -> set[tuple[str, str]]:
    return {(e.source, e.target) for e in graph.edges if e.kind == kind}


# The jaffle_shop example, end to end


@pytest.mark.dbt
def test_example_builds_every_output(jaffle_built_graph: Path, jaffle_graph: Graph) -> None:
    out = jaffle_built_graph / 'konstruct-out'
    assert {p.name for p in out.iterdir()} == {
        'graph.json',
        'REPORT.md',
        'graph.html',
        'ontology.yml',
        'cache.json',
    }
    assert jaffle_graph.warnings == []
    assert jaffle_graph.meta['dialect'] == 'duckdb'
    assert jaffle_graph.meta['inputs'] == {
        'dbt_projects': 1,
        'warehouses': 1,
        'dbt_models': 5,
        'airflow_dags': 1,
        'dagster_assets': 0,
        'dagster_ops': 0,
        'definitions': 5,
        'sql_files': 0,
    }
    assert jaffle_graph.meta['build_seconds'] < 60


@pytest.mark.dbt
def test_example_column_lineage(jaffle_graph: Graph) -> None:
    derived = {
        (e.source, e.target): e.attrs.get('transform') for e in jaffle_graph.edges if e.kind == 'derived_from'
    }
    assert (
        derived[('model.jaffle_shop.orders#amount', 'model.jaffle_shop.stg_payments#amount')] == 'transformed'
    )
    assert (
        derived[('model.jaffle_shop.stg_payments#amount', 'seed.jaffle_shop.raw_payments#amount')]
        == 'transformed'
    )
    assert derived[('model.jaffle_shop.stg_orders#order_id', 'seed.jaffle_shop.raw_orders#id')] == 'direct'
    assert (
        derived[('model.jaffle_shop.customers#first_name', 'model.jaffle_shop.stg_customers#first_name')]
        == 'direct'
    )
    # Every column of every model has lineage.
    for model in (n for n in jaffle_graph.nodes.values() if n.kind == 'model'):
        for column in jaffle_graph.columns_of(model.id):
            if not column.attrs.get('documented_only'):
                assert jaffle_graph.out_edges(column.id, 'derived_from'), column.id


@pytest.mark.dbt
def test_example_join_keys_are_references(jaffle_graph: Graph) -> None:
    references = {
        (e.source, e.target)
        for e in jaffle_graph.edges
        if e.kind == 'feeds' and e.attrs.get('role') == 'reference'
    }
    assert ('model.jaffle_shop.stg_payments#order_id', 'model.jaffle_shop.orders') in references
    assert ('model.jaffle_shop.stg_orders#customer_id', 'model.jaffle_shop.customers') in references


@pytest.mark.dbt
def test_example_warehouse_merges_into_dbt_nodes(jaffle_graph: Graph) -> None:
    orders = jaffle_graph.nodes['model.jaffle_shop.orders']
    assert orders.origins == ['dbt', 'duckdb']
    assert orders.attrs['in_warehouse'] is True
    stg = jaffle_graph.nodes['model.jaffle_shop.stg_orders']
    assert stg.attrs['table_type'] == 'view'
    # dbt views are not analysed a second time from the warehouse definition.
    assert not any(e.attrs.get('origin') == 'warehouse' for e in jaffle_graph.edges)
    missing = jaffle_graph.column('model.jaffle_shop.customers', 'total_order_amount')
    assert missing is not None and missing.attrs.get('documented_only')


@pytest.mark.dbt
def test_example_airflow_tasks_run_dbt_nodes(jaffle_graph: Graph) -> None:
    runs = edges(jaffle_graph, 'runs')
    task = 'task.jaffle_shop_daily.'
    assert (task + 'dbt_seed', 'seed.jaffle_shop.raw_orders') in runs
    assert (task + 'dbt_run_staging', 'model.jaffle_shop.stg_payments') in runs
    assert (task + 'dbt_run_staging', 'model.jaffle_shop.orders') not in runs
    assert (task + 'dbt_run_marts', 'model.jaffle_shop.orders') in runs
    tested = {target for source, target in runs if source == task + 'dbt_test'}
    assert len(tested) == 20
    assert ('dag.jaffle_shop_daily', task + 'notify_crm') in runs
    assert (task + 'dbt_test', task + 'build_customer_ltv') in edges(jaffle_graph, 'precedes')


@pytest.mark.dbt
def test_example_airflow_sql_task_lineage(jaffle_graph: Graph) -> None:
    ltv = jaffle_graph.node_for_relation('jaffle_shop.reporting.customer_ltv')
    assert ltv is not None and ltv.kind == 'table'
    assert sorted(ltv.origins) == ['airflow', 'duckdb']
    task = 'task.jaffle_shop_daily.build_customer_ltv'
    feeds = edges(jaffle_graph, 'feeds')
    assert (task, ltv.id) in feeds
    assert ('model.jaffle_shop.customers', task) in feeds
    assert ('model.jaffle_shop.customers', ltv.id) in feeds
    lineage = jaffle_graph.out_edges(f'{ltv.id}#lifetime_value', 'derived_from')
    assert [(e.target, e.attrs['transform'], e.attrs['via']) for e in lineage] == [
        ('model.jaffle_shop.customers#customer_lifetime_value', 'direct', task)
    ]
    segment = [e.target for e in jaffle_graph.out_edges(f'{ltv.id}#segment', 'derived_from')]
    assert segment == ['model.jaffle_shop.customers#number_of_orders']
    # The asset URI in the TaskFlow outlet resolves to the same table.
    assert ('task.jaffle_shop_daily.notify_crm', ltv.id) in feeds


# --update


@pytest.mark.dbt
def test_update_reuses_unchanged_work_and_redoes_changed_models(jaffle: Path, dbt_on_path: None) -> None:
    first = build(load_config(jaffle), update=False)
    assert first.cache_hits == 0
    again = build(load_config(jaffle), update=True)
    assert again.cache_misses == 0
    assert again.cache_hits == first.cache_misses
    assert again.graph.to_dict()['edges'] == first.graph.to_dict()['edges']

    model = jaffle / 'models' / 'orders.sql'
    model.write_text(
        model.read_text().replace(
            'order_payments.total_amount as amount', 'order_payments.total_amount as amount_total'
        )
    )
    changed = build(load_config(jaffle), update=True)
    # orders changed, and customers is unaffected; dbt regenerated the manifest because it was stale.
    assert 1 <= changed.cache_misses <= 2
    assert changed.graph.column('model.jaffle_shop.orders', 'amount_total') is not None


def test_cache_ignores_other_versions(tmp_path: Path) -> None:
    path = tmp_path / 'cache.json'
    path.write_text(json.dumps({'version': 1, 'konstruct': '0.0.0', 'entries': {'k': 1}}))
    assert Cache(path, enabled=True).entries == {}
    path.write_text('not json')
    assert Cache(path, enabled=True).entries == {}
    cache = Cache(path, enabled=False)
    assert cache.get_or_compute('a', lambda: 5) == 5
    cache.save()
    reloaded = Cache(path, enabled=True)
    assert reloaded.get_or_compute('a', lambda: 6) == 5
    assert (reloaded.hits, reloaded.misses) == (1, 0)


# Projects without dbt, and configuration


def make_warehouse(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute('create schema raw')
    con.execute('create table raw.events (id int, user_id int, kind varchar, occurred_at timestamp)')
    con.execute('create table raw.users (id int, email varchar)')
    con.execute(
        'create view main.user_events as select u.email, count(*) as events '
        'from raw.events e join raw.users u on u.id = e.user_id group by 1'
    )
    con.close()


def test_sql_files_and_views_without_dbt(tmp_path: Path) -> None:
    make_warehouse(tmp_path / 'app.duckdb')
    (tmp_path / 'sql').mkdir()
    (tmp_path / 'sql' / 'daily.sql').write_text(
        "create table main.daily as select date_trunc('day', occurred_at) as day, kind, count(*) as n "
        "from raw.events where kind <> 'test' group by all"
    )
    result = build(load_config(tmp_path))
    graph = result.graph
    assert result.inputs['sql_files'] == 1 and result.inputs['warehouses'] == 1
    view = graph.node_for_relation('app.main.user_events')
    assert view is not None and view.attrs['table_type'] == 'view'
    assert [e.target for e in graph.out_edges(f'{view.id}#email', 'derived_from')] == [
        'table.app.raw.users#email'
    ]
    daily = graph.node_for_relation('app.main.daily')
    assert daily is not None and daily.path == 'sql/daily.sql' and daily.attrs.get('inferred')
    assert {e.target for e in graph.out_edges(f'{daily.id}#day', 'derived_from')} == {
        'table.app.raw.events#occurred_at'
    }
    assert ('table.app.raw.events#kind', daily.id) in {
        (e.source, e.target) for e in graph.edges if e.attrs.get('role') == 'reference'
    }


def test_konstruct_toml_configures_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / 'data').mkdir()
    make_warehouse(tmp_path / 'data' / 'app.duckdb')
    (tmp_path / 'ignored.duckdb').write_bytes(b'')
    (tmp_path / 'jobs').mkdir()
    (tmp_path / 'jobs' / 'a.sql').write_text('create table main.a as select id from raw.users')
    (tmp_path / 'jobs' / 'scratch.sql').write_text('this is not sql')
    monkeypatch.setenv('APP_DB', 'data/app.duckdb')
    (tmp_path / 'konstruct.toml').write_text(
        """
dialect = "duckdb"
exclude = ["jobs/scratch.sql"]
out_dir = "out"

[sql]
paths = ["jobs/*.sql"]

[[warehouse]]
type = "duckdb"
path = "${APP_DB}"
schemas = ["raw"]
"""
    )
    config = load_config(tmp_path)
    assert config.out_dir == tmp_path / 'out'
    result = build(config)
    graph = result.graph
    assert result.outputs[0] == tmp_path / 'out' / 'graph.json'
    assert graph.node_for_relation('app.main.user_events') is None
    assert graph.node_for_relation('app.raw.users') is not None
    a = graph.node_for_relation('app.main.a')
    assert a is not None
    assert [e.target for e in graph.out_edges(f'{a.id}#id', 'derived_from')] == ['table.app.raw.users#id']
    # The explicit SQL path list still honours nothing but itself, so scratch.sql is analysed and warned about.
    assert any('jobs/scratch.sql' in w for w in graph.warnings) or result.inputs['sql_files'] == 2


def test_pyproject_tool_table_and_bad_warehouse(tmp_path: Path) -> None:
    (tmp_path / 'pyproject.toml').write_text(
        '[tool.konstruct]\n[[tool.konstruct.warehouse]]\ntype = "duckdb"\npath = "missing.duckdb"\n'
    )
    result = build(load_config(tmp_path))
    assert result.inputs['warehouses'] == 0
    assert any(w.startswith('warehouse duckdb missing.duckdb') for w in result.graph.warnings)


def test_broken_dag_file_is_a_warning(tmp_path: Path) -> None:
    (tmp_path / 'dag.py').write_text("from airflow import DAG\nwith DAG('x') as dag:\n    oops(\n")
    result = build(load_config(tmp_path))
    assert any('syntax error' in w for w in result.graph.warnings)


def test_dbt_project_without_artefacts_or_dbt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shutil.copytree(
        Path(__file__).resolve().parents[1] / 'examples' / 'jaffle_shop',
        tmp_path / 'shop',
        ignore=shutil.ignore_patterns('target', '*.duckdb', 'konstruct-out', 'logs'),
    )
    monkeypatch.setenv('PATH', str(tmp_path))
    result = build(load_config(tmp_path))
    assert any('dbt is not on PATH' in w for w in result.graph.warnings)
    # Airflow still parses; dbt tasks cannot be resolved without a manifest.
    assert result.inputs['airflow_dags'] == 1
    assert any('matched no dbt nodes' in w for w in result.graph.warnings)


def test_relation_index_suffixes_and_placeholders() -> None:
    graph = Graph()
    graph.add_node(Node('a', 'table', 'orders', relation='db.sales.orders'))
    graph.add_node(Node('b', 'table', 'orders', relation='db.archive.orders'))
    graph.add_node(Node('c', 'table', 'users', relation='db.sales.users'))
    index = RelationIndex(graph)
    assert index.find('sales.orders') is graph.nodes['a']
    assert index.find('orders') is None
    assert index.find('users') is graph.nodes['c']
    assert index.find('__konstruct__.users') is graph.nodes['c']
    assert index.find('other.sales.users') is None
    assert index.find('host.db.sales.users', drop_leading=True) is graph.nodes['c']


# Services


@pytest.mark.postgres
def test_postgres_warehouse_end_to_end(tmp_path: Path) -> None:
    import psycopg

    dsn = require('KONSTRUCT_TEST_PG_DSN')
    schema = f'kp_{uuid.uuid4().hex[:8]}'
    with psycopg.connect(dsn, autocommit=True) as con:
        con.execute(f'create schema {schema}')
        con.execute(f'create table {schema}.orders (id int, amount numeric, customer_id int)')
        con.execute(f"comment on column {schema}.orders.amount is 'Order value'")
        con.execute(
            f'create view {schema}.customer_totals as select customer_id, sum(amount) as total '
            f'from {schema}.orders group by customer_id'
        )
    try:
        os.environ['KP_TEST_DSN'] = dsn
        (tmp_path / 'konstruct.toml').write_text(
            f'[[warehouse]]\ntype = "postgres"\ndsn = "${{KP_TEST_DSN}}"\nschemas = ["{schema}"]\n'
        )
        graph = build(load_config(tmp_path)).graph
        totals = next(n for n in graph.relations() if n.name == 'customer_totals')
        orders = next(n for n in graph.relations() if n.name == 'orders')
        assert graph.column(orders.id, 'amount') is not None
        assert graph.column(orders.id, 'amount').description == 'Order value'  # type: ignore[union-attr]
        assert [
            (e.target, e.attrs['transform']) for e in graph.out_edges(f'{totals.id}#total', 'derived_from')
        ] == [(f'{orders.id}#amount', 'transformed')]
        assert graph.warnings == []
    finally:
        os.environ.pop('KP_TEST_DSN', None)
        with psycopg.connect(dsn, autocommit=True) as con:
            con.execute(f'drop schema {schema} cascade')


@pytest.mark.iceberg
def test_iceberg_tables_join_sql_lineage(tmp_path: Path) -> None:
    from pyiceberg.catalog.sql import SqlCatalog
    from pyiceberg.schema import Schema
    from pyiceberg.types import DoubleType, LongType, NestedField

    catalog = SqlCatalog('lake', uri=f'sqlite:///{tmp_path}/catalog.db', warehouse=f'file://{tmp_path}')
    catalog.create_namespace('sales')
    catalog.create_table(
        'sales.orders',
        schema=Schema(
            NestedField(1, 'id', LongType(), required=True), NestedField(2, 'amount', DoubleType())
        ),
    )
    (tmp_path / 'report.sql').write_text(
        'create table lake.marts.big as select id, amount * 2 as doubled from lake.sales.orders'
    )
    (tmp_path / 'konstruct.toml').write_text(
        f"""
dialect = "trino"
[[warehouse]]
type = "iceberg"
name = "lake"
[warehouse.properties]
type = "sql"
uri = "sqlite:///{tmp_path}/catalog.db"
warehouse = "file://{tmp_path}"
"""
    )
    graph = build(load_config(tmp_path)).graph
    orders = graph.node_for_relation('lake.sales.orders')
    assert orders is not None and orders.origins == ['iceberg', 'sql']
    big = graph.node_for_relation('lake.marts.big')
    assert big is not None
    assert [
        (e.target, e.attrs['transform']) for e in graph.out_edges(f'{big.id}#doubled', 'derived_from')
    ] == [(f'{orders.id}#amount', 'transformed')]


def test_struct_fields_from_duckdb_trace_and_verify(tmp_path: Path) -> None:
    from konstruct import Project

    con = duckdb.connect(str(tmp_path / 'shop.duckdb'))
    con.execute('create schema raw')
    con.execute(
        'create table raw.orders as select i as id, '
        "{'country': ['UK','FR','DE'][1 + i % 3], 'address': {'city': 'c' || i, 'postcode': 'p' || i}} as customer "
        'from range(1, 50) t(i)'
    )
    con.close()
    (tmp_path / 'eu.sql').write_text(
        'create table main.eu as select id, o.customer.country as country, upper(o.customer.address.city) as city '
        "from raw.orders o where o.customer['country'] <> 'DE'"
    )
    project = Project(tmp_path)
    project.build()
    orders = project.describe('raw.orders')
    assert [c.name for c in orders.columns] == [
        'id',
        'customer',
        'customer.country',
        'customer.address',
        'customer.address.city',
        'customer.address.postcode',
    ]
    trace = project.trace('eu.city')
    assert [(c.label, c.transform) for c in trace.columns] == [
        ('orders.customer.address.city', 'transformed')
    ]
    impact = project.impact('orders.customer.country')
    assert 'eu.country' in {c.label for c in impact.columns}
    assert 'eu' in {r.label for r in impact.joins_and_filters}
    result = project.verify(warehouse=tmp_path / 'shop.duckdb')
    assert result.totals()['unexplained'] == 0
    assert result.totals()['confirmed'] == result.totals()['claimed'] == 3


def test_lists_of_structs_and_maps_from_duckdb(tmp_path: Path) -> None:
    from konstruct import Project

    con = duckdb.connect(str(tmp_path / 'shop.duckdb'))
    con.execute('create schema raw')
    con.execute(
        'create table raw.orders as select i as id, '
        "[{'sku': 'a' || i, 'qty': i % 4 + 1}, {'sku': 'b' || i, 'qty': 2}] as items, "
        "map(['colour'], ['red' || (i % 3)]) as attrs from range(1, 40) t(i)"
    )
    con.close()
    (tmp_path / 'lines.sql').write_text(
        'create table main.lines as select o.id, o.items[1].sku as first_sku, o.items[1].qty * 10 as first_qty, '
        "o.attrs['colour'] as colour from raw.orders o"
    )
    project = Project(tmp_path)
    project.build()
    names = [c.name for c in project.describe('raw.orders').columns]
    assert names == ['id', 'items', 'items.sku', 'items.qty', 'attrs']
    assert [(c.label, c.transform) for c in project.trace('lines.first_sku').columns] == [
        ('orders.items.sku', 'direct')
    ]
    assert [(c.label, c.transform) for c in project.trace('lines.first_qty').columns] == [
        ('orders.items.qty', 'transformed')
    ]
    # attrs is a map: its keys are read at run time, so lineage stops at the map column.
    assert [c.label for c in project.trace('lines.colour').columns] == ['orders.attrs']
    result = project.verify(warehouse=tmp_path / 'shop.duckdb')
    totals = result.totals()
    assert totals['unexplained'] == 0
    assert totals['confirmed'] == totals['claimed'] == 4
