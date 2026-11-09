"""The Databricks connector against a real workspace with Unity Catalog.

Skipped unless these are set (CI sets them from repository secrets in its own job):

    KONSTRUCT_TEST_DATABRICKS_HOST       server hostname, e.g. dbc-1234.cloud.databricks.com
    KONSTRUCT_TEST_DATABRICKS_HTTP_PATH  a SQL warehouse's HTTP path
    KONSTRUCT_TEST_DATABRICKS_TOKEN      a token for a principal that can create schemas
    KONSTRUCT_TEST_DATABRICKS_CATALOG    a catalog it may create a scratch schema in (default: main)

The test creates a uniquely named schema with a table and a view, checks that konstruct reads
them as the stub test in test_cloud_warehouses expects (struct fields, comments, the view's SQL
and the view's column lineage), and drops the schema whatever happens. It costs a few seconds of
SQL warehouse time.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from konstruct import Project
from konstruct.inputs.warehouse import DatabricksConnector

pytestmark = pytest.mark.databricks

HOST = os.environ.get('KONSTRUCT_TEST_DATABRICKS_HOST')
HTTP_PATH = os.environ.get('KONSTRUCT_TEST_DATABRICKS_HTTP_PATH')
TOKEN = os.environ.get('KONSTRUCT_TEST_DATABRICKS_TOKEN')
CATALOG = os.environ.get('KONSTRUCT_TEST_DATABRICKS_CATALOG', 'main')

if not (HOST and HTTP_PATH and TOKEN):
    pytest.skip('Databricks workspace credentials are not set', allow_module_level=True)


@pytest.fixture(scope='module')
def schema() -> Iterator[str]:
    from databricks import sql

    name = f'konstruct_ci_{uuid.uuid4().hex[:10]}'
    connection = sql.connect(server_hostname=HOST, http_path=HTTP_PATH, access_token=TOKEN)
    cursor = connection.cursor()

    def run(statement: str) -> None:
        cursor.execute(statement)

    try:
        run(f'create schema {CATALOG}.{name}')
        run(
            f'create table {CATALOG}.{name}.orders ('
            "id bigint comment 'Order id', "
            'customer struct<country: string, address: struct<city: string>>, '
            "amount decimal(10,2)) comment 'All orders'"
        )
        run(
            f'create view {CATALOG}.{name}.big_orders as '
            f'select id, customer.country as country, amount * 2 as doubled from {CATALOG}.{name}.orders'
        )
        run(
            f"insert into {CATALOG}.{name}.orders values (1, named_struct('country', 'GB', 'address', named_struct('city', 'Leeds')), 10.50)"
        )
        yield name
    finally:
        run(f'drop schema if exists {CATALOG}.{name} cascade')
        connection.close()


def _options() -> dict[str, Any]:
    return {'server_hostname': HOST, 'http_path': HTTP_PATH, 'access_token': TOKEN}


def test_metadata_matches_the_stub(schema: str) -> None:
    relations = {
        r.name: r for r in DatabricksConnector(_options(), catalogs=[CATALOG], schemas=[schema]).fetch()
    }
    assert set(relations) == {'orders', 'big_orders'}
    orders = relations['orders']
    assert orders.parts == (CATALOG, schema, 'orders')
    assert orders.comment == 'All orders'
    assert [c.name for c in orders.columns] == [
        'id',
        'customer',
        'customer.country',
        'customer.address',
        'customer.address.city',
        'amount',
    ]
    assert orders.columns[0].comment == 'Order id'
    big = relations['big_orders']
    assert big.kind == 'view'
    assert big.view_sql is not None and big.view_sql.lower().startswith(
        f'create view {CATALOG}.{schema}.big_orders'
    )


def test_build_and_view_lineage(schema: str, tmp_path: Path) -> None:
    (tmp_path / 'konstruct.toml').write_text(
        '[[warehouse]]\n'
        'type = "databricks"\n'
        f'server_hostname = "{HOST}"\n'
        f'http_path = "{HTTP_PATH}"\n'
        'access_token = "${KONSTRUCT_TEST_DATABRICKS_TOKEN}"\n'
        f'catalogs = ["{CATALOG}"]\n'
        f'schemas = ["{schema}"]\n'
    )
    project = Project(tmp_path)
    graph = project.build().graph
    assert graph.meta['dialect'] == 'databricks'
    assert [c.label for c in project.trace('big_orders.doubled').columns] == ['orders.amount']
    assert [c.label for c in project.trace('big_orders.country').columns] == ['orders.customer.country']
    assert project.explain('big_orders.doubled').expression == 'orders.amount * 2'
    assert not [w for w in graph.warnings if w.startswith('warehouse')]


def test_usage_from_system_tables(schema: str, tmp_path: Path) -> None:
    """Reads system.access.column_lineage when the principal may; skips if it may not."""
    from konstruct.inputs.usage import UsageSettings, databricks_usage

    try:
        rows = databricks_usage(DatabricksConnector(_options()), UsageSettings(days=1))
    except Exception as error:  # the principal may lack access to system tables
        pytest.skip(f'no access to system.access.column_lineage: {str(error).splitlines()[0]}')
    assert all(row.queries >= 1 for row in rows)
