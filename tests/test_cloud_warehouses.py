"""Snowflake, BigQuery and Databricks connectors (the optional extras).

* Snowflake runs against fakesnow, an emulator of snowflake-connector-python backed by DuckDB:
  `uv run --with fakesnow --with "snowflake-connector-python<5" python -m pytest -m snowflake`.
* BigQuery runs against the BigQuery emulator (ghcr.io/goccy/bigquery-emulator) at
  KONSTRUCT_TEST_BIGQUERY_ENDPOINT, through the official client.
* Databricks has no emulator, so its connector is tested against a stub driver that returns
  information_schema rows in the shape Unity Catalog does.
"""

from __future__ import annotations

import sys
import types
import uuid
from pathlib import Path
from typing import Any

import pytest

from konstruct import Project
from konstruct.engine.config import load_config
from konstruct.inputs.dbt import read_project
from konstruct.inputs.warehouse import (
    BigQueryConnector,
    DatabricksConnector,
    MissingDriver,
    SnowflakeConnector,
)

from .test_warehouse import require

# These projects connect with their own konstruct.toml and profiles.yml, as CI on a trusted branch does.
pytestmark = pytest.mark.usefixtures('trust_config')

# Databricks, against a stub driver


class _Cursor:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str) -> None:
        self.log.append(sql)
        if '.tables' in sql:
            self.rows = [
                ('main', 'sales', 'orders', 'MANAGED', 'All orders'),
                ('main', 'sales', 'big_orders', 'VIEW', None),
                ('main', 'ops', 'audit', 'MANAGED', None),
            ]
        elif '.columns' in sql:
            self.rows = [
                ('main', 'sales', 'orders', 'id', 'bigint', 'Order id', 1),
                (
                    'main',
                    'sales',
                    'orders',
                    'customer',
                    'struct<country:string,address:struct<city:string>>',
                    None,
                    2,
                ),
                ('main', 'sales', 'orders', 'amount', 'decimal(10,2)', None, 3),
                ('main', 'sales', 'big_orders', 'id', 'bigint', None, 1),
                ('main', 'sales', 'big_orders', 'doubled', 'decimal(11,2)', None, 2),
                ('main', 'ops', 'audit', 'at', 'timestamp', None, 1),
            ]
        elif '.views' in sql:
            self.rows = [
                ('main', 'sales', 'big_orders', 'select id, amount * 2 as doubled from main.sales.orders')
            ]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class _Connection:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.closed = False

    def cursor(self) -> _Cursor:
        return _Cursor(self.log)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def databricks_stub(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    log: list[str] = []
    module = types.ModuleType('databricks.sql')
    module.connect = lambda **kwargs: _Connection(log)  # type: ignore[attr-defined]
    package = types.ModuleType('databricks')
    package.sql = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, 'databricks', package)
    monkeypatch.setitem(sys.modules, 'databricks.sql', module)
    return log


def test_databricks_metadata(databricks_stub: list[str]) -> None:
    relations = {r.name: r for r in DatabricksConnector({'catalog': 'main'}, schemas=['sales']).fetch()}
    assert set(relations) == {'orders', 'big_orders'}
    orders = relations['orders']
    assert orders.parts == ('main', 'sales', 'orders') and orders.comment == 'All orders'
    assert [c.name for c in orders.columns] == [
        'id',
        'customer',
        'customer.country',
        'customer.address',
        'customer.address.city',
        'amount',
    ]
    assert relations['big_orders'].kind == 'view'
    assert relations['big_orders'].view_sql == (
        'create view main.sales.big_orders as select id, amount * 2 as doubled from main.sales.orders'
    )
    assert all('main.information_schema' in sql for sql in databricks_stub)
    assert any('full_data_type' in sql for sql in databricks_stub)


def test_databricks_build_and_view_lineage(databricks_stub: list[str], tmp_path: Path) -> None:
    (tmp_path / 'konstruct.toml').write_text(
        '[[warehouse]]\ntype = "databricks"\nserver_hostname = "x"\ncatalogs = ["main"]\n'
    )
    project = Project(tmp_path)
    graph = project.build().graph
    assert graph.meta['dialect'] == 'databricks'
    assert project.trace('big_orders.doubled').columns[0].label == 'orders.amount'
    assert graph.warnings == []


def test_missing_drivers_are_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(sys.modules, 'databricks', None)
    monkeypatch.setitem(sys.modules, 'databricks.sql', None)
    with pytest.raises(MissingDriver, match=r'konstruct\[databricks\]'):
        DatabricksConnector({}).fetch()
    (tmp_path / 'konstruct.toml').write_text('[[warehouse]]\ntype = "databricks"\n')
    graph = Project(tmp_path).build().graph
    assert any('install the databricks extra' in w for w in graph.warnings)


def test_dbt_profiles_for_cloud_warehouses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from konstruct.engine.config import discover_warehouses

    monkeypatch.setenv('SF_PASSWORD', 'secret')
    for name, target in {
        'snow': 'type: snowflake\n      account: acme\n      user: me\n      password: "{{ env_var(\'SF_PASSWORD\') }}"\n      database: SHOP\n      schema: RAW\n      warehouse: wh',
        'bq': 'type: bigquery\n      project: my-project\n      dataset: shop\n      method: oauth',
        'dbx': 'type: databricks\n      host: x.cloud.databricks.com\n      http_path: /sql/1\n      token: t\n      catalog: main\n      schema: sales',
    }.items():
        root = tmp_path / name
        root.mkdir()
        (root / 'dbt_project.yml').write_text(f'name: {name}\nprofile: {name}\n')
        (root / 'profiles.yml').write_text(f'{name}:\n  target: dev\n  outputs:\n    dev:\n      {target}\n')
    config = load_config(tmp_path)
    projects = [p for p in (read_project(tmp_path / n) for n in ('snow', 'bq', 'dbx')) if p]
    found = {w.type: w for w in discover_warehouses(config, projects)}
    assert found['snowflake'].options['password'] == 'secret'
    assert found['snowflake'].options['database'] == 'SHOP'
    assert found['bigquery'].options['project'] == 'my-project'
    assert found['databricks'].options['server_hostname'] == 'x.cloud.databricks.com'
    assert isinstance(found['snowflake'].connector(tmp_path), SnowflakeConnector)
    assert isinstance(found['bigquery'].connector(tmp_path), BigQueryConnector)
    assert isinstance(found['databricks'].connector(tmp_path), DatabricksConnector)


# Snowflake, against fakesnow


@pytest.mark.snowflake
def test_snowflake_with_fakesnow(tmp_path: Path) -> None:
    fakesnow = pytest.importorskip('fakesnow')
    import snowflake.connector

    with fakesnow.patch():
        conn = snowflake.connector.connect(database='SHOP', schema='RAW')
        cur = conn.cursor()
        cur.execute('create database if not exists SHOP')
        cur.execute('create schema if not exists SHOP.RAW')
        cur.execute(
            "create table SHOP.RAW.ORDERS (ID int, AMOUNT number(10,2), STATUS varchar) comment = 'All orders'"
        )
        cur.execute("comment on column SHOP.RAW.ORDERS.AMOUNT is 'Order value'")
        cur.execute(
            'create view SHOP.RAW.BIG_ORDERS as select ID, AMOUNT * 2 as DOUBLED from SHOP.RAW.ORDERS'
        )
        relations = {r.name: r for r in SnowflakeConnector({'database': 'SHOP', 'schema': 'RAW'}).fetch()}
        assert relations['ORDERS'].comment == 'All orders'
        assert [(c.name, c.comment) for c in relations['ORDERS'].columns][1] == ('AMOUNT', 'Order value')
        assert relations['BIG_ORDERS'].kind == 'view'

        (tmp_path / 'konstruct.toml').write_text(
            '[[warehouse]]\ntype = "snowflake"\ndatabase = "SHOP"\nschema = "RAW"\n'
        )
        (tmp_path / 'report.sql').write_text(
            'create table SHOP.MART.REPORT as select ID, AMOUNT / 100 as DOLLARS from SHOP.RAW.ORDERS'
        )
        project = Project(tmp_path)
        graph = project.build().graph
        assert graph.meta['dialect'] == 'snowflake'
        assert project.trace('big_orders.doubled').columns[0].label == 'ORDERS.AMOUNT'
        assert project.trace('report.dollars').columns[0].label == 'ORDERS.AMOUNT'


# BigQuery, against the emulator


@pytest.mark.bigquery
def test_bigquery_with_the_emulator(tmp_path: Path) -> None:
    endpoint = require('KONSTRUCT_TEST_BIGQUERY_ENDPOINT')
    from google.cloud import bigquery

    dataset = f'kp_{uuid.uuid4().hex[:8]}'
    connector = BigQueryConnector(project='test', datasets=[dataset], endpoint=endpoint)
    client = connector.client()
    client.create_dataset(dataset)
    try:
        table = bigquery.Table(
            f'test.{dataset}.orders',
            schema=[
                bigquery.SchemaField('id', 'INTEGER', mode='REQUIRED', description='Order id'),
                bigquery.SchemaField('amount', 'NUMERIC'),
                bigquery.SchemaField(
                    'customer',
                    'RECORD',
                    fields=[bigquery.SchemaField('country', 'STRING', description='ISO country')],
                ),
            ],
        )
        table.description = 'All orders'
        client.create_table(table)
        view = bigquery.Table(f'test.{dataset}.big_orders')
        view.view_query = (
            f'select id, customer.country as country, amount * 2 as doubled from `test.{dataset}.orders`'
        )
        client.create_table(view)

        relations = {r.name: r for r in connector.fetch()}
        orders = relations['orders']
        assert orders.comment == 'All orders'
        assert [(c.name, c.comment) for c in orders.columns] == [
            ('id', 'Order id'),
            ('amount', None),
            ('customer', None),
            ('customer.country', 'ISO country'),
        ]
        assert orders.columns[0].attrs == {'required': True}
        assert relations['big_orders'].kind == 'view'

        (tmp_path / 'konstruct.toml').write_text(
            f'[[warehouse]]\ntype = "bigquery"\nproject = "test"\ndatasets = ["{dataset}"]\nendpoint = "{endpoint}"\n'
        )
        project = Project(tmp_path)
        graph = project.build().graph
        assert graph.meta['dialect'] == 'bigquery'
        assert project.trace('big_orders.doubled').columns[0].label == 'orders.amount'
        assert project.trace('big_orders.country').columns[0].label == 'orders.customer.country'
    finally:
        client.delete_dataset(dataset, delete_contents=True)
