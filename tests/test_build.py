from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

import duckdb
import pytest

from konstruct.analysis.diff import Context
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
