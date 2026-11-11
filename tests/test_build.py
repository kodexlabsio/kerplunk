from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

import duckdb
import pytest

from konstruct.analysis.diff import Context
from konstruct.analysis.explain import Explainer
from konstruct.analysis.lock import Pinner
from konstruct.core.graph import Graph, Node
from konstruct.engine.build import Cache, RelationIndex, build
from konstruct.engine.config import ConfigError, load_config

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
        'explain.json',
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
        'definitions': 6,
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
    assert len(tested) == 24
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


def guessed_project(root: Path) -> Graph:
    """Without a warehouse, raw.src has the columns m1 reads, and m2's star selects only those."""
    (root / 'm1.sql').write_text('create table main.m1 as select id, amount from raw.src')
    (root / 'm2.sql').write_text('create table main.m2 as select * from raw.src')
    (root / 'm3.sql').write_text(
        'create table main.m3 as\n'
        'with q as (select * from main.m2 a join raw.other o on a.id = o.a_id) select region from q'
    )
    return build(load_config(root)).graph


def test_a_build_records_the_relations_whose_columns_it_only_guessed(tmp_path: Path) -> None:
    graph = guessed_project(tmp_path)
    # m3 also writes what a star over guessed columns passes on, and raw.other is only read.
    assert graph.guessed_relations() == ['main.m2', 'main.m3', 'raw.other', 'raw.src']
    assert {e.target for e in graph.out_edges('table.main.m3#region', 'derived_from')} == {
        'table.main.m2#region',
        'table.raw.other#region',
    }
    # diff analyses SQL with the same relations' columns taken as guessed.
    assert Context(graph, tmp_path).guessed == graph.guessed_relations()
    # The lockfile takes a relation's columns as all it has exactly when the build did.
    pinner = Pinner(graph, tmp_path, None)
    assert [pinner.known(graph.nodes[f'table.{r}']) for r in ('main.m1', 'main.m2', 'raw.src')] == [
        True,
        False,
        False,
    ]


@pytest.mark.parametrize(
    'first',
    [
        'create table m.x as select p.dept from raw.pay p',
        'create table m.x as select p.dept from raw.pay p join raw.users u on p.payment_id = u.id',
    ],
    ids=['alone', 'joined'],
)
def test_the_graph_of_sql_files_does_not_depend_on_the_order_they_are_read(
    tmp_path: Path, first: str
) -> None:
    """A relation's columns, known only from the SQL that reads it, are not all those the first
    file to read it names: a later file that reads another is traced all the same."""
    models = 'create table crm.copy as select p.dept, p.salary from raw.pay p'

    def graph_of(files: dict[str, str]) -> tuple[Graph, dict[str, Any]]:
        root = tmp_path / str(len(list(tmp_path.iterdir())))
        root.mkdir()
        for name, sql in files.items():
            (root / name).write_text(sql)
        graph = build(load_config(root)).graph
        # The same graph but for which file each relation came from, as the files swap contents.
        text = json.dumps({k: v for k, v in graph.to_dict().items() if k != 'meta'}, sort_keys=True)
        if files['a.sql'] == models:
            text = text.replace('a.sql', '<b>').replace('b.sql', 'a.sql').replace('<b>', 'b.sql')
        return graph, {
            k: sorted(map(repr, v)) if isinstance(v, list) else v for k, v in json.loads(text).items()
        }

    graph, before = graph_of({'a.sql': first, 'b.sql': models})
    _, after = graph_of({'a.sql': models, 'b.sql': first})
    assert before == after
    assert graph.warnings == []
    assert [e.target for e in graph.out_edges('table.crm.copy#salary', 'derived_from')] == [
        'table.raw.pay#salary'
    ]


def test_an_insert_of_some_columns_leaves_the_relations_columns_guessed(tmp_path: Path) -> None:
    (tmp_path / 'a.sql').write_text('insert into main.t (id) select id from raw.src')
    (tmp_path / 'b.sql').write_text(
        'create table main.u as with x as (select * from main.t a join raw.other o on a.id = o.t_id) select region from x'
    )
    graph = build(load_config(tmp_path)).graph
    assert 'main.t' in graph.guessed_relations()
    assert 'table.main.t#region' in {e.target for e in graph.out_edges('table.main.u#region', 'derived_from')}


def test_an_update_reanalyses_sql_when_the_columns_it_reads_change(tmp_path: Path) -> None:
    (tmp_path / 'a.sql').write_text('create table main.a as select id, amount from raw.src')
    (tmp_path / 'b.sql').write_text('create table main.b as select * from main.a')
    build(load_config(tmp_path))
    (tmp_path / 'a.sql').write_text('create table main.a as select id, amount, region from raw.src')
    graph = build(load_config(tmp_path), update=True).graph
    assert {c.name for c in graph.columns_of('table.main.b')} == {'id', 'amount', 'region'}


@pytest.mark.parametrize(
    ('dialect', 'name', 'key'),
    [
        ('duckdb', 'main."order items"', 'main.order items'),
        ('duckdb', 'main.t$x', 'main.t$x'),
        ('duckdb', 'main."order-items"', 'main.order-items'),
        ('bigquery', '`shop.order-items`', 'shop.order-items'),
        ('bigquery', '`my-project.shop.items`', 'my-project.shop.items'),
        ('spark', 'delta.`/lake/order-items`', 'delta./lake/order-items'),
    ],
    ids=['space', 'dollar', 'dash', 'bigquery-dash', 'bigquery-project', 'spark-path'],
)
def test_sql_reads_the_columns_of_a_relation_whose_name_is_not_a_word(
    tmp_path: Path, dialect: str, name: str, key: str
) -> None:
    # The columns another file's SQL gave the relation are those a star over it selects.
    (tmp_path / 'a.sql').write_text(f'create table {name} as select id, amount from raw.src')
    (tmp_path / 'b.sql').write_text(f'create table main.r as select * from {name}')
    (tmp_path / 'konstruct.toml').write_text(f'dialect = "{dialect}"\n')
    graph = build(load_config(tmp_path)).graph
    assert {c.name for c in graph.columns_of('table.main.r')} == {'id', 'amount'}
    assert [e.target for e in graph.out_edges('table.main.r#amount', 'derived_from')] == [
        f'table.{key}#amount'
    ]


def test_snowflake_sql_reads_the_columns_a_build_lists(tmp_path: Path) -> None:
    # The build lists names in lower case; Snowflake folds the SQL's unquoted names to upper case.
    (tmp_path / 'a.sql').write_text('create table main.orders as select id, amount, status from raw.src')
    (tmp_path / 'b.sql').write_text(
        "create table main.paid as with o as (select * from main.orders) select * from o where status = 'paid'"
    )
    (tmp_path / 'konstruct.toml').write_text('dialect = "snowflake"\n')
    graph = build(load_config(tmp_path)).graph
    assert {c.name for c in graph.columns_of('table.main.paid')} == {'id', 'amount', 'status'}
    assert [e.target for e in graph.out_edges('table.main.paid#amount', 'derived_from')] == [
        'table.main.orders#amount'
    ]
    assert graph.nodes['table.main.paid'].attrs['conditions'] == ["WHERE: main.orders.status = 'paid'"]


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


@pytest.mark.usefixtures('trust_config')
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


# A repository under review can hold any file; none of these may stop the build.
UNREADABLE_PYTHON = {
    # Folding the concatenation recurses once per term.
    'concatenation': 'sql = "select 1"' + ' + " "' * 3000 + '\n',
    # Python's own parser gives up.
    'sum': 'total = ' + ' + '.join(['1'] * 100_000) + '\n',
    # A ValueError on Python 3.11, a SyntaxError later.
    'nul': 'x = 1\x00\n',
    # Unary minus signs exhaust the parser's memory rather than its recursion limit.
    'minus': 'x = ' + '-' * 200_000 + '1\n',
}


@pytest.mark.parametrize('body', UNREADABLE_PYTHON.values(), ids=UNREADABLE_PYTHON.keys())
def test_python_that_cannot_be_read_is_a_warning(tmp_path: Path, body: str) -> None:
    (tmp_path / 'dag.py').write_text('from airflow import DAG\n' + body)
    (tmp_path / 'assets.py').write_text('from dagster import asset\n' + body)
    (tmp_path / 'job.py').write_text('from pyspark.sql import SparkSession\nspark.sql("select 1")\n' + body)
    (tmp_path / 'konstruct.toml').write_text(
        '[dagster]\npaths = ["assets.py"]\n[spark]\npaths = ["job.py"]\n'
    )
    warnings = build(load_config(tmp_path)).graph.warnings
    assert any(w.startswith('airflow: dag.py: syntax error') for w in warnings)
    assert any(w.startswith('dagster: assets.py: syntax error') for w in warnings)
    assert any(w.startswith('spark: job.py:') and 'not valid Python' in w for w in warnings)


def test_yaml_nested_too_deeply_is_reported(tmp_path: Path) -> None:
    nested = '[' * 5000 + ']' * 5000
    (tmp_path / 'definitions.yml').write_text(f'terms: {nested}\n')
    (tmp_path / 'model.ossie.yaml').write_text(f'semantic_model: {nested}\n')
    (tmp_path / 'x.sql').write_text('create table b as select a from t')
    warnings = build(load_config(tmp_path)).graph.warnings
    assert any('definitions.yml:1: not valid YAML: nested too deeply' in w for w in warnings)
    assert any('model.ossie.yaml: not valid YAML: nested too deeply' in w for w in warnings)


def test_dbt_artefacts_and_profiles_nested_too_deeply_do_not_stop_the_build(tmp_path: Path) -> None:
    (tmp_path / 'dbt_project.yml').write_text("name: shop\nprofile: shop\nversion: '1.0'\n")
    (tmp_path / 'models').mkdir()
    (tmp_path / 'models' / 'a.sql').write_text('select 1 as x')
    (tmp_path / 'target').mkdir()
    (tmp_path / 'target' / 'manifest.json').write_text('[' * 100000 + ']' * 100000)
    (tmp_path / 'profiles.yml').write_text('shop: ' + '[' * 5000 + ']' * 5000 + '\n')
    config = load_config(tmp_path)
    config.run_dbt = False
    # The manifest is reported as unreadable and the profile is skipped; the build finishes.
    result = build(config)
    assert (tmp_path / 'konstruct-out' / 'graph.json').is_file() and result.graph is not None


@pytest.mark.usefixtures('trust_config')  # dbt would run if it were on PATH
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


@pytest.mark.usefixtures('trust_config')
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


@pytest.mark.usefixtures('trust_config')
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


def test_repositories_must_be_a_list_of_paths(tmp_path: Path) -> None:
    (tmp_path / 'konstruct.toml').write_text('[repositories]\npaths = ["../other"]\n')
    with pytest.raises(ConfigError, match='repositories is a list of paths'):
        load_config(tmp_path)


def test_an_input_changed_while_the_build_runs_is_reported_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """graph.json is written after the build has read its inputs, so a file changed in between
    (an editor saving it, or one caught half-written) must still count as changed afterwards."""
    from konstruct import Project
    from konstruct.engine import build as module

    (tmp_path / 'jobs').mkdir()
    sql = tmp_path / 'jobs' / 'users.sql'
    sql.write_text('create table main.people as select id from raw.users\n')
    project = Project(tmp_path)
    project.build()
    assert project.changed_inputs() == []

    read = module.Builder.load_usage

    def change_after_reading(builder: module.Builder) -> None:
        # Every input has been read by now; the edit lands before graph.json is written.
        sql.write_text('create table main.people as select id, name from raw.users\n')
        read(builder)

    monkeypatch.setattr(module.Builder, 'load_usage', change_after_reading)
    result = project.build(update=True)
    assert 'table.main.people#name' not in result.graph.nodes  # the build read the old file
    assert project.changed_inputs() == ['jobs/users.sql']
    monkeypatch.undo()
    # The next build reads the new file, and nothing is left changed.
    assert 'table.main.people#name' in project.build(update=True).graph.nodes
    assert project.changed_inputs() == []


def test_what_counts_as_a_changed_input(tmp_path: Path) -> None:
    from konstruct import Project
    from konstruct.engine.build import changed_inputs

    (tmp_path / 'jobs').mkdir()
    sql = tmp_path / 'jobs' / 'users.sql'
    sql.write_text('create table main.people as select id from raw.users\n')
    project = Project(tmp_path)
    graph = project.build().graph
    graph.meta['watched'].append('jobs')  # a directory the build read, as dbt's model paths are
    assert changed_inputs(project.graph_path, graph) == []
    # A new file in it, and a file truncated as write_text truncates before it writes, however
    # soon after the build.
    (tmp_path / 'jobs' / 'orders.sql').write_text('select 1\n')
    sql.write_text('')
    assert changed_inputs(project.graph_path, graph) == ['jobs/orders.sql', 'jobs/users.sql']
    # A graph from before builds recorded what they found: files newer than graph.json.
    del graph.meta['watched_files']
    os.utime(project.graph_path, ns=(0, 0))
    assert changed_inputs(project.graph_path, graph) == ['jobs/orders.sql', 'jobs/users.sql']
    os.utime(project.graph_path, ns=(2**62, 2**62))
    assert changed_inputs(project.graph_path, graph) == []


def _jobs_project(root: Path) -> Path:
    (root / 'jobs').mkdir(parents=True)
    (root / 'jobs' / 'x.sql').write_text('create table main.x as select id from raw.users\n')
    return root / 'jobs' / 'x.sql'


@pytest.mark.parametrize('change', ['delete', 'rename', 'same size, time put back', 'atomic save', 'chmod'])
def test_every_change_to_a_file_read_counts(change: str, tmp_path: Path) -> None:
    """The rule the viewer's SQL uses (konstruct.core.files.identity): size, both times, device
    and inode, and a file that is gone."""
    from konstruct import Project

    x = _jobs_project(tmp_path)
    project = Project(tmp_path)
    project.build()
    assert project.changed_inputs() == []
    before = x.stat()
    same_size = 'create table main.x as select ix from raw.users\n'
    if change == 'delete':
        x.unlink()
    elif change == 'rename':
        x.rename(x.with_name('y.sql'))
    elif change == 'same size, time put back':
        x.write_text(same_size)
        os.utime(x, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif change == 'atomic save':
        temporary = x.with_name('.x.sql.tmp')
        temporary.write_text(same_size)
        os.utime(temporary, ns=(before.st_atime_ns, before.st_mtime_ns))
        temporary.replace(x)
    else:
        x.chmod(0o600)
    # A renamed file is gone under its old name, and new beside it under the other.
    assert project.changed_inputs() == ['jobs/x.sql', *(['jobs/y.sql'] if change == 'rename' else [])]


def test_only_regular_files_are_recorded(tmp_path: Path) -> None:
    from konstruct import Project

    _jobs_project(tmp_path)
    os.mkfifo(tmp_path / 'jobs' / 'pipe')
    project = Project(tmp_path)
    graph = project.build().graph
    graph.meta['watched'].append('jobs')
    project.build()
    assert [f['path'] for f in Project(tmp_path).graph.meta['watched_files']] == ['jobs/x.sql']
    assert set(Project(tmp_path).graph.meta['watched_files'][0]) == {
        'path',
        'size',
        'mtime_ns',
        'ctime_ns',
        'device',
        'inode',
    }


def test_configuration_and_dbt_project_read_before_the_build_count_from_their_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """konstruct.toml is read by load_config and dbt_project.yml when the project is found, both
    before the build could watch them: an edit after those reads still counts."""
    from konstruct import Project
    from konstruct.engine import build as module

    (tmp_path / 'dbt_project.yml').write_text('name: p\nprofile: p\n')
    (tmp_path / 'models').mkdir()
    (tmp_path / 'models' / 'a.sql').write_text('select 1 as a\n')
    (tmp_path / 'konstruct.toml').write_text('[build]\n')
    project = Project(tmp_path)
    project.build(run_dbt=False, warehouses=False)
    assert project.changed_inputs() == []
    from konstruct.engine.config import dbt_projects as found

    def edit_after_reading(config: Any) -> Any:
        projects = found(config)
        (tmp_path / 'konstruct.toml').write_text('[build]\n# edited after it was read\n')
        (tmp_path / 'dbt_project.yml').write_text('name: p\nprofile: p\nmodel-paths: ["src"]\n')
        return projects

    monkeypatch.setattr(module, 'dbt_projects', edit_after_reading)
    project.build(run_dbt=False, warehouses=False, update=True)
    assert project.changed_inputs() == ['dbt_project.yml', 'konstruct.toml']


def test_installed_packages_macros_and_the_repositorys_profiles_are_inputs(tmp_path: Path) -> None:
    from konstruct import Project

    (tmp_path / 'dbt_project.yml').write_text('name: p\nprofile: p\n')
    (tmp_path / 'models').mkdir()
    (tmp_path / 'models' / 'a.sql').write_text('select 1 as a\n')
    (tmp_path / 'profiles.yml').write_text('p:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n')
    macros = tmp_path / 'dbt_packages' / 'pkg' / 'macros'
    macros.mkdir(parents=True)
    (macros / 'm.sql').write_text('{% macro m() %}1{% endmacro %}\n')
    project = Project(tmp_path)
    project.build(run_dbt=False, warehouses=False)
    assert project.changed_inputs() == []
    (macros / 'm.sql').write_text('{% macro m() %}2{% endmacro %}\n')
    (macros / 'n.sql').write_text('{% macro n() %}3{% endmacro %}\n')
    (tmp_path / 'profiles.yml').write_text('p:\n  target: prod\n')
    assert project.changed_inputs() == [
        'dbt_packages/pkg/macros/m.sql',
        'dbt_packages/pkg/macros/n.sql',
        'profiles.yml',
    ]


def test_inputs_of_every_repository_are_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from konstruct import Project

    monkeypatch.setenv('KONSTRUCT_TRUST_CONFIG', '1')
    main, other = tmp_path / 'main', tmp_path / 'other'
    _jobs_project(main)
    (other / 'jobs').mkdir(parents=True)
    (other / 'jobs' / 'b.sql').write_text('create table main.b as select id from main.x\n')
    (other / 'dbt_project.yml').write_text('name: o\nprofile: o\n')
    (other / 'models').mkdir()
    (other / 'models' / 'm.sql').write_text('select 1 as m\n')
    (main / 'konstruct.toml').write_text('repositories = ["../other"]\n')
    project = Project(main)
    graph = project.build(run_dbt=False, warehouses=False).graph
    assert [f['path'] for f in graph.meta['watched_files']] == [
        '../other/dbt_project.yml',
        '../other/jobs/b.sql',
        '../other/models/m.sql',
        'jobs/x.sql',
        'konstruct.toml',
    ]
    assert project.changed_inputs() == []
    (other / 'jobs' / 'b.sql').write_text('create table main.b as select id, x from main.x\n')
    (other / 'models' / 'n.sql').write_text('select 1 as n\n')
    (other / 'dbt_project.yml').unlink()
    (other / 'jobs' / 'c.sql').write_text('create table main.c as select id from main.x\n')
    assert project.changed_inputs() == [
        '../other/dbt_project.yml',
        '../other/jobs/b.sql',
        '../other/jobs/c.sql',
        '../other/models/n.sql',
    ]


def test_a_new_file_beside_one_the_build_read_counts(tmp_path: Path) -> None:
    """A file with the extension of one the build read, in the same folder, is one discovery would
    now find; once a build has seen it there, it is not new any more, so --watch does not rebuild
    for it again."""
    from konstruct import Project

    _jobs_project(tmp_path)
    (tmp_path / 'top.sql').write_text('create table main.top as select id from raw.users\n')
    project = Project(tmp_path)
    graph = project.build().graph
    assert [(s['folder'], s['extension'], s['names']) for s in graph.meta['watched_siblings']] == [
        ('.', '.sql', ['top.sql']),
        ('jobs', '.sql', ['x.sql']),
    ]
    assert project.changed_inputs() == []
    (tmp_path / 'jobs' / 'z.sql').write_text('create table main.z as select id from raw.users\n')
    (tmp_path / 'jobs' / 'notes.md').write_text('not an input\n')
    (tmp_path / 'new').mkdir()
    (tmp_path / 'new' / 'w.sql').write_text('select 1\n')  # in a folder no build has read
    assert project.changed_inputs() == ['jobs/z.sql']
    assert 'table.main.z' in project.build(update=True).graph.nodes
    assert project.changed_inputs() == []
    # New once; then read, as every SQL file is, so an edit is a change to an input.
    (tmp_path / 'jobs' / 'helpers.sql').write_text('-- nothing here\n')
    assert project.changed_inputs() == ['jobs/helpers.sql']
    project.build(update=True)
    assert project.changed_inputs() == []
    (tmp_path / 'jobs' / 'helpers.sql').write_text('-- still nothing\n')
    assert project.changed_inputs() == ['jobs/helpers.sql']
    # The build's own output is never beside an input, wherever it is.
    assert not any(p.startswith('konstruct-out') for p in project.changed_inputs())


def test_a_new_dag_beside_one_the_build_read_counts(tmp_path: Path) -> None:
    from konstruct import Project

    (tmp_path / 'dags').mkdir()
    dag = 'from airflow import DAG\nfrom airflow.operators.bash import BashOperator\n'
    (tmp_path / 'dags' / 'a.py').write_text(
        dag + "with DAG('a') as d:\n    BashOperator(task_id='t', bash_command='x')\n"
    )
    project = Project(tmp_path)
    project.build()
    assert project.changed_inputs() == []
    (tmp_path / 'dags' / 'b.py').write_text(
        dag + "with DAG('b') as d:\n    BashOperator(task_id='t', bash_command='y')\n"
    )
    assert project.changed_inputs() == ['dags/b.py']
    assert 'dag.b' in project.build(update=True).graph.nodes
    assert project.changed_inputs() == []


def test_files_beside_an_input_that_are_not_inputs_change_nothing(tmp_path: Path) -> None:
    """Only a new name beside an input counts, as discovery would find it: what is in a file the
    build did not read is not watched, an excluded file is not found, and nothing else with the
    extension of a definitions file is an input."""
    from konstruct import Project

    _jobs_project(tmp_path)
    (tmp_path / 'konstruct.toml').write_text('exclude = ["jobs/scratch_*.sql"]\n')
    (tmp_path / 'jobs' / 'scratch_1.sql').write_text('select 1\n')
    (tmp_path / 'definitions.yml').write_text('terms: []\n')
    (tmp_path / 'docker-compose.yml').write_text('services: {}\n')
    project = Project(tmp_path)
    graph = project.build().graph
    assert {(e['folder'], tuple(e['exclude'])) for e in graph.meta['watched_siblings']} == {
        ('.', ('jobs/scratch_*.sql',)),
        ('jobs', ('jobs/scratch_*.sql',)),
    }
    assert project.changed_inputs() == []
    (tmp_path / 'jobs' / 'scratch_1.sql').write_text('select 2\n')
    (tmp_path / 'jobs' / 'scratch_2.sql').write_text('select 3\n')
    (tmp_path / 'docker-compose.yml').write_text('services: {db: {}}\n')
    assert project.changed_inputs() == []
    # A new YAML file beside definitions.yml is no definitions file.
    (tmp_path / 'glossary.yml').write_text('terms: []\n')
    assert project.changed_inputs() == []


def test_a_helper_module_beside_a_dag_is_not_an_input(tmp_path: Path) -> None:
    from konstruct import Project

    (tmp_path / 'dags').mkdir()
    (tmp_path / 'dags' / 'd.py').write_text(
        'from airflow import DAG\nfrom airflow.operators.bash import BashOperator\n'
        "with DAG('d') as dag:\n    BashOperator(task_id='t', bash_command='echo')\n"
    )
    (tmp_path / 'dags' / 'helpers.py').write_text('X = 1\n')
    project = Project(tmp_path)
    project.build()
    (tmp_path / 'dags' / 'helpers.py').write_text('X = 2\n')
    assert project.changed_inputs() == []


def test_a_folder_of_many_files_beside_an_input_stays_small_and_quick(tmp_path: Path) -> None:
    import time

    from konstruct import Project

    (tmp_path / 'dags').mkdir()
    (tmp_path / 'dags' / 'd.py').write_text(
        'from airflow import DAG\nfrom airflow.operators.bash import BashOperator\n'
        "with DAG('d') as dag:\n    BashOperator(task_id='t', bash_command='echo')\n"
    )
    for number in range(5000):
        (tmp_path / 'dags' / f'h{number:05}.py').write_text('X = 1\n')
    project = Project(tmp_path)
    graph = project.build().graph
    assert [f['path'] for f in graph.meta['watched_files']] == ['dags/d.py']
    # Each file beside the DAG by its name and a digest of what says it changed: under 40 bytes.
    (siblings,) = graph.meta['watched_siblings']
    assert len(siblings['found']) == 5001
    assert all(len(found) == 16 and int(found, 16) >= 0 for found in siblings['found'])
    assert len(json.dumps(graph.meta['watched_siblings'])) < 40 * 5001

    def check() -> float:
        started = time.perf_counter()
        assert project.changed_inputs() == []
        return time.perf_counter() - started

    untouched = min(check() for _ in range(5))
    assert untouched < 1.0
    # Every helper rewritten, as a branch switch does: read once, and then each check costs about
    # what one with nothing changed does, not a read of every helper again.
    for number in range(5000):
        (tmp_path / 'dags' / f'h{number:05}.py').write_text('X = 10\n' + '# padding\n' * 100)
    check()
    later = min(check() for _ in range(5))
    assert later < 1.5 * untouched + 0.01, (later, untouched)


DAG_TEXT = (
    'from airflow import DAG\nfrom airflow.operators.bash import BashOperator\n'
    "with DAG('{name}') as dag:\n    BashOperator(task_id='t', bash_command='echo')\n"
)


def _dags_project(root: Path) -> Path:
    (root / 'dags').mkdir(parents=True)
    (root / 'dags' / 'd.py').write_text(DAG_TEXT.format(name='d'))
    return root / 'dags'


def test_a_dag_written_into_a_file_created_empty_counts(tmp_path: Path) -> None:
    """An editor makes the file empty first; the watcher's build finds no DAG in it, and the DAG
    written into it afterwards still starts a build (block F of the review)."""
    from konstruct import Project

    dags = _dags_project(tmp_path)
    project = Project(tmp_path)
    project.build()
    (dags / 'new.py').write_text('')
    assert project.changed_inputs() == []  # empty, so not a DAG: no build for it yet
    project.build(update=True)
    (dags / 'new.py').write_text(DAG_TEXT.format(name='new'))
    assert project.changed_inputs() == ['dags/new.py']
    assert 'dag.new' in project.build(update=True).graph.nodes
    assert project.changed_inputs() == []


def test_a_helper_deleted_and_written_again_as_a_dag_counts(tmp_path: Path) -> None:
    """Block J of the review: a file beside a DAG that was not one, deleted and written again as
    a DAG; and a DAG deleted and written again."""
    from konstruct import Project

    dags = _dags_project(tmp_path)
    (dags / 'h.py').write_text('X = 1\n')
    project = Project(tmp_path)
    project.build()
    (dags / 'h.py').unlink()
    assert project.changed_inputs() == []
    (dags / 'h.py').write_text(DAG_TEXT.format(name='h'))
    assert project.changed_inputs() == ['dags/h.py']
    (dags / 'd.py').unlink()
    (dags / 'd.py').write_text(DAG_TEXT.format(name='d'))
    assert project.changed_inputs() == ['dags/d.py', 'dags/h.py']


def test_files_beside_an_input_follow_the_exclude_rule_discovery_applied(tmp_path: Path) -> None:
    """With [sql] paths, discovery reads what the patterns name and ignores exclude, so a new
    file there counts even when an exclude pattern matches it (block G of the review)."""
    from konstruct import Project

    _jobs_project(tmp_path)
    (tmp_path / 'konstruct.toml').write_text(
        'exclude = ["jobs/scratch_*.sql"]\n[sql]\npaths = ["jobs/*.sql"]\n'
    )
    (tmp_path / 'jobs' / 'scratch_1.sql').write_text('create table main.s1 as select id from raw.users\n')
    project = Project(tmp_path)
    assert 'table.main.s1' in project.build().graph.nodes
    (tmp_path / 'jobs' / 'scratch_2.sql').write_text('create table main.s2 as select id from raw.users\n')
    assert project.changed_inputs() == ['jobs/scratch_2.sql']
    assert 'table.main.s2' in project.build(update=True).graph.nodes


def test_each_repository_keeps_its_own_exclude_patterns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Block H of the review: the main project's exclude patterns are not another repository's."""
    from konstruct import Project

    monkeypatch.setenv('KONSTRUCT_TRUST_CONFIG', '1')
    main, other = tmp_path / 'main', tmp_path / 'other'
    _jobs_project(main)
    (other / 'jobs').mkdir(parents=True)
    (other / 'jobs' / 'b.sql').write_text('create table main.b as select id from main.x\n')
    (other / 'konstruct.toml').write_text('exclude = ["jobs/skip_*.sql"]\n')
    (main / 'konstruct.toml').write_text('repositories = ["../other"]\nexclude = ["*scratch*"]\n')
    project = Project(main)
    project.build(run_dbt=False, warehouses=False)
    (other / 'jobs' / 'scratch.sql').write_text('create table main.c as select id from main.x\n')
    (other / 'jobs' / 'skip_1.sql').write_text('create table main.d as select id from main.x\n')
    (main / 'jobs' / 'scratch.sql').write_text('create table main.e as select id from main.x\n')
    assert project.changed_inputs() == ['../other/jobs/scratch.sql']
    graph = project.build(run_dbt=False, warehouses=False, update=True).graph
    assert 'table.main.c' in graph.nodes and 'table.main.d' not in graph.nodes


def test_removing_an_exclude_pattern_is_a_change(tmp_path: Path) -> None:
    from konstruct import Project

    _jobs_project(tmp_path)
    (tmp_path / 'konstruct.toml').write_text('exclude = ["jobs/s*.sql"]\n')
    (tmp_path / 'jobs' / 's.sql').write_text('create table main.s as select id from raw.users\n')
    project = Project(tmp_path)
    project.build()
    (tmp_path / 'konstruct.toml').write_text('exclude = []\n')
    assert project.changed_inputs() == ['konstruct.toml']


def test_a_helper_rewritten_as_a_dag_of_the_same_size_counts(tmp_path: Path) -> None:
    """Block K of the review: a file beside an input is kept as the build found it, by the rule
    for every file (konstruct.core.files.identity), not by its size alone."""
    from konstruct import Project

    dags = _dags_project(tmp_path)
    dag = DAG_TEXT.format(name='k')
    helper = '#' + 'x' * (len(dag) - 2) + '\n'
    assert len(helper) == len(dag)
    (dags / 'h.py').write_text(helper)
    project = Project(tmp_path)
    project.build()
    (dags / 'h.py').write_text(dag)
    assert project.changed_inputs() == ['dags/h.py']
    assert 'dag.k' in project.build(update=True).graph.nodes
    assert project.changed_inputs() == []


def test_sql_paths_find_new_files_as_discovery_globs_them(tmp_path: Path) -> None:
    """Block L of the review: in ``[sql] paths``, ``**`` matches no folder as well as several, as
    in the glob discovery runs, so a new file at the top counts too."""
    from konstruct import Project

    (tmp_path / 'jobs').mkdir()
    (tmp_path / 'konstruct.toml').write_text('[sql]\npaths = ["**/*.sql"]\n')
    (tmp_path / 'x.sql').write_text('create table main.x as select id from raw.users\n')
    (tmp_path / 'jobs' / 'y.sql').write_text('create table main.y as select id from main.x\n')
    project = Project(tmp_path)
    project.build()
    (tmp_path / 'new.sql').write_text('create table main.n as select id from raw.users\n')
    (tmp_path / 'jobs' / 'new2.sql').write_text('create table main.n2 as select id from raw.users\n')
    assert project.changed_inputs() == ['jobs/new2.sql', 'new.sql']
    assert {'table.main.n', 'table.main.n2'} <= set(project.build(update=True).graph.nodes)
    assert project.changed_inputs() == []


GLOB_FILES = [
    'a.sql',
    '.h.sql',
    'b.SQL',
    'jobs/y.sql',
    'jobs/sub/z.sql',
    'jobs/sub/deep/u.sql',
    'jobs/.d/v.sql',
    '.hid/w.sql',
    'x/jobs/t.sql',
]


@pytest.mark.parametrize(
    'pattern',
    [
        '**/*.sql',
        '*.sql',
        '*.SQL',
        '[ab].sql',
        'jobs/*.sql',
        'jobs/*',
        './jobs/*.sql',
        'jobs//*.sql',
        '*/*.sql',
        'j?bs/**/*.sql',
        'jobs/**/*.sql',
        'jobs/**/sub/*.sql',
        '**/jobs/*.sql',
        '**/sub/*.sql',
        '**/**/*.sql',
        '**/*',
        '**',
        'jobs/**',
        'jobs/',
        'jobs/sub/../y.sql',
    ],
)
def test_the_test_for_sql_paths_takes_what_their_glob_finds(pattern: str, tmp_path: Path) -> None:
    """The check for new files beside an input and discovery agree on ``[sql] paths`` for every
    file, by this Python's pathlib, as discovery globs them and keeps what the test takes."""
    from konstruct.engine.config import FINDS

    for name in GLOB_FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text('select 1\n')
    globbed = {p.relative_to(tmp_path).as_posix() for p in tmp_path.glob(pattern) if p.is_file()}
    globbed = {os.path.normpath(p).replace(os.sep, '/') for p in globbed}
    taken = {name for name in GLOB_FILES if FINDS['globs'](tmp_path / name, name, (pattern,))}
    assert taken == globbed


def test_a_folder_as_the_build_found_it_is_seen_in_one_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check with nothing changed lists each folder beside an input and hashes the listing
    once, digesting no file and keeping nothing; after a change it keeps only the changed file."""
    from konstruct import Project
    from konstruct.core.files import digest

    dags = _dags_project(tmp_path)
    for number in range(100):
        (dags / f'h{number:02}.py').write_text('X = 1\n')
    project = Project(tmp_path)
    project.build()
    digested: list[list[int]] = []

    def counted(found: list[int]) -> str:
        digested.append(found)
        return digest(found)

    monkeypatch.setattr('konstruct.engine.build.digest', counted)
    assert project.changed_inputs() == [] and Project(tmp_path).changed_inputs() == []
    assert digested == [] and project._judged == {}
    (dags / 'h03.py').write_text('X = 2\n')
    assert project.changed_inputs() == []
    assert [list(verdicts) for _, verdicts in project._judged.values()] == [['h03.py']]


def test_a_file_beside_an_input_is_read_again_only_when_it_changes_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Block M of the review: helpers beside a DAG all rewritten (a branch switched, say) are no
    inputs and start no build, so they stay changed; each is read once, not at every check."""
    from konstruct import Project
    from konstruct.engine.config import FINDS

    dags = _dags_project(tmp_path)
    for number in range(50):
        (dags / f'h{number:02}.py').write_text('X = 1\n')
    project = Project(tmp_path)
    project.build()
    judged: list[str] = []
    real = FINDS['airflow']

    def counted(path: Path, relative: str, patterns: tuple[str, ...]) -> bool:
        judged.append(relative)
        return real(path, relative, patterns)

    monkeypatch.setitem(FINDS, 'airflow', counted)
    assert project.changed_inputs() == [] and judged == []
    for number in range(50):
        (dags / f'h{number:02}.py').write_text('X = 10\n' + '# padding\n' * 10)
    assert project.changed_inputs() == []
    assert len(judged) == 50
    judged.clear()
    assert project.changed_inputs() == [] and project.changed_inputs() == []
    assert judged == []
    (dags / 'h07.py').write_text(DAG_TEXT.format(name='h07'))
    assert project.changed_inputs() == ['dags/h07.py']
    assert judged == ['dags/h07.py']


def test_a_relation_named_with_a_hash_keeps_its_own_lineage(tmp_path: Path) -> None:
    """A relation named ``events#2`` is that relation wherever it is named, never a reading of
    ``events``."""
    (tmp_path / 'a.sql').write_text(
        'create table main.events as select id, kind from raw.web;\n'
        'create table main."events#2" as select id, kind from raw.app;\n'
    )
    (tmp_path / 'b.sql').write_text('create table main.r as select e.kind from main."events#2" e')
    graph = build(load_config(tmp_path)).graph
    events = graph.node_for_relation('main.events#2')
    assert events is not None and events.relation == 'main.events#2'
    assert graph.node_for_relation('main.events#3') is None
    [edge] = graph.out_edges('table.main.r#kind', 'derived_from')
    assert graph.parent_of(graph.nodes[edge.target]) is events
    assert Explainer(graph).explain(graph.nodes['table.main.r#kind']).sources == ['app.kind']


def test_the_example_names_each_reading_of_a_cte_two_others_read(jaffle_graph: Graph) -> None:
    """customers reads stg_orders through one CTE that two others read, each joined to customers:
    its conditions name each reading through that CTE, as moving a filter from one to the other
    changes its rows."""
    customers = jaffle_graph.nodes['model.jaffle_shop.customers']
    texts = [str(c) for c in customers.attrs['conditions']] + [
        str(c.attrs.get('expression', '')) for c in jaffle_graph.columns_of(customers.id)
    ]
    readings = {m for t in texts for m in re.findall(r'stg_orders#[\d/]+', t)}
    assert len(readings) == 2, texts
