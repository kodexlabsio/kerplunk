from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest

from konstruct.inputs.warehouse import DuckDBConnector, IcebergConnector, PostgresConnector, RelationMeta


def require(variable: str) -> str:
    """The value of a service variable; skip without it, unless CI says services must exist."""
    value = os.environ.get(variable)
    if not value:
        if os.environ.get('KONSTRUCT_REQUIRE_SERVICES'):
            pytest.fail(f'{variable} is not set but KONSTRUCT_REQUIRE_SERVICES is')
        pytest.skip(f'{variable} is not set')
    return value


def by_name(relations: list[RelationMeta]) -> dict[str, RelationMeta]:
    return {r.name: r for r in relations}


# DuckDB


@pytest.fixture
def duck(tmp_path: Path) -> Path:
    path = tmp_path / 'shop.duckdb'
    con = duckdb.connect(str(path))
    con.execute('create schema raw')
    con.execute('create table raw.orders (id integer, amount decimal(10, 2), placed_at timestamp)')
    con.execute("comment on table raw.orders is 'Orders as loaded'")
    con.execute("comment on column raw.orders.amount is 'Order value in pounds'")
    con.execute(
        'create view main.big_orders as select id, amount * 100 as pence from raw.orders where amount > 10'
    )
    con.execute('create schema scratch')
    con.execute('create table scratch.tmp (x int)')
    con.close()
    return path


def test_duckdb_tables_columns_comments_and_views(duck: Path) -> None:
    relations = by_name(DuckDBConnector(duck).fetch())
    assert set(relations) == {'orders', 'big_orders', 'tmp'}
    orders = relations['orders']
    assert orders.parts == ('shop', 'raw', 'orders')
    assert orders.key == 'shop.raw.orders'
    assert orders.kind == 'table'
    assert orders.comment == 'Orders as loaded'
    assert [(c.name, c.data_type, c.comment) for c in orders.columns] == [
        ('id', 'INTEGER', None),
        ('amount', 'DECIMAL(10,2)', 'Order value in pounds'),
        ('placed_at', 'TIMESTAMP', None),
    ]
    view = relations['big_orders']
    assert view.kind == 'view'
    assert view.view_sql and 'amount * 100' in view.view_sql
    assert [c.name for c in view.columns] == ['id', 'pence']


def test_duckdb_schema_filter_and_read_only(duck: Path) -> None:
    relations = DuckDBConnector(duck, schemas=['RAW']).fetch()
    assert [r.name for r in relations] == ['orders']
    # A read-only connection leaves the file usable by others.
    con = duckdb.connect(str(duck))
    con.execute('insert into raw.orders values (1, 2.5, now())')
    con.close()


def test_duckdb_primary_keys(tmp_path: Path) -> None:
    path = tmp_path / 'keys.duckdb'
    con = duckdb.connect(str(path))
    con.execute('create table customers (customer_id int primary key, email text unique)')
    con.execute('create table order_lines (order_id int, line int, primary key (order_id, line))')
    con.execute('create table notes (body text)')
    con.close()
    relations = by_name(DuckDBConnector(path).fetch())
    assert relations['customers'].attrs == {'primary_key': ['customer_id']}
    assert relations['order_lines'].attrs == {'primary_key': ['order_id', 'line']}
    assert relations['notes'].attrs == {}


# Postgres


@pytest.fixture
def postgres_schema() -> Iterator[tuple[str, str]]:
    import psycopg

    dsn = require('KONSTRUCT_TEST_PG_DSN')
    schema = f'kp_{uuid.uuid4().hex[:8]}'
    with psycopg.connect(dsn, autocommit=True) as con:
        con.execute(f'create schema {schema}')
        con.execute(
            f'create table {schema}.customers (id bigint primary key, email text not null, "Mixed Case" int)'
        )
        con.execute(f"comment on table {schema}.customers is 'One row per customer'")
        con.execute(f"comment on column {schema}.customers.email is 'Contact email. PII.'")
        con.execute(
            f'create view {schema}.customer_emails as select id, lower(email) as email from {schema}.customers'
        )
        con.execute(
            f'create materialized view {schema}.customer_count as select count(*) as n from {schema}.customers'
        )
        con.execute(
            f'create table {schema}.lines (order_id int, line int, sku text, primary key (line, order_id))'
        )
        con.execute(
            f'create table {schema}.events (id int, at date, primary key (id, at)) partition by range (at)'
        )
        con.execute(
            f"create table {schema}.events_2026 partition of {schema}.events for values from ('2026-01-01') to ('2027-01-01')"
        )
    try:
        yield dsn, schema
    finally:
        with psycopg.connect(dsn, autocommit=True) as con:
            con.execute(f'drop schema {schema} cascade')


@pytest.mark.postgres
def test_postgres_metadata(postgres_schema: tuple[str, str]) -> None:
    dsn, schema = postgres_schema
    relations = by_name(PostgresConnector(dsn, schemas=[schema]).fetch())
    assert set(relations) == {'customers', 'customer_emails', 'customer_count', 'events', 'lines'}
    customers = relations['customers']
    assert customers.parts[1:] == (schema, 'customers')
    assert customers.comment == 'One row per customer'
    assert [(c.name, c.data_type, c.comment) for c in customers.columns] == [
        ('id', 'bigint', None),
        ('email', 'text', 'Contact email. PII.'),
        ('Mixed Case', 'integer', None),
    ]
    assert customers.attrs == {'primary_key': ['id']}
    assert relations['lines'].attrs == {'primary_key': ['line', 'order_id']}
    assert relations['events'].attrs == {'primary_key': ['id', 'at']}
    view = relations['customer_emails']
    assert view.kind == 'view'
    assert view.view_sql is not None
    assert view.view_sql.startswith(f'create view "{schema}"."customer_emails" as')
    assert 'lower(email)' in view.view_sql
    assert relations['customer_count'].kind == 'view'


@pytest.mark.postgres
def test_postgres_view_sql_parses_to_lineage(postgres_schema: tuple[str, str]) -> None:
    from konstruct.core.sql import analyse

    dsn, schema = postgres_schema
    relations = by_name(PostgresConnector(dsn, schemas=[schema]).fetch())
    database = relations['customers'].parts[0]
    view = relations['customer_emails']
    assert view.view_sql
    [result] = analyse(view.view_sql, dialect='postgres', default_catalog=database, default_db=schema)
    assert result.target == f'{database}.{schema}.customer_emails'
    assert [(s.relation, s.column, s.transform) for s in result.columns['email']] == [
        (f'{database}.{schema}.customers', 'email', 'transformed')
    ]


# Iceberg


def _make_iceberg_tables(catalog: Any, namespace: str = 'sales') -> None:
    from pyiceberg.partitioning import PartitionField, PartitionSpec
    from pyiceberg.schema import Schema
    from pyiceberg.transforms import DayTransform
    from pyiceberg.types import (
        DoubleType,
        ListType,
        LongType,
        MapType,
        NestedField,
        StringType,
        StructType,
        TimestamptzType,
    )

    catalog.create_namespace(namespace)
    schema = Schema(
        NestedField(1, 'order_id', LongType(), required=True, doc='Order key'),
        NestedField(2, 'amount', DoubleType()),
        NestedField(3, 'ordered_at', TimestamptzType()),
        NestedField(
            4,
            'shipping',
            StructType(
                NestedField(5, 'city', StringType()),
                NestedField(6, 'postcode', StringType(), doc='UK postcode'),
            ),
        ),
        NestedField(
            7, 'items', ListType(8, StructType(NestedField(9, 'sku', StringType())), element_required=False)
        ),
        NestedField(10, 'attributes', MapType(11, StringType(), 12, StringType(), value_required=False)),
        identifier_field_ids=[1],
    )
    spec = PartitionSpec(PartitionField(source_id=3, field_id=1000, transform=DayTransform(), name='day'))
    catalog.create_table(
        f'{namespace}.orders', schema=schema, partition_spec=spec, properties={'comment': 'All orders'}
    )


def _check_iceberg(relations: list[RelationMeta], catalog_name: str, namespace: str = 'sales') -> None:
    [orders] = relations
    assert orders.parts == (catalog_name, namespace, 'orders')
    assert orders.comment == 'All orders'
    columns = {c.name: c for c in orders.columns}
    assert list(columns) == [
        'order_id',
        'amount',
        'ordered_at',
        'shipping',
        'shipping.city',
        'shipping.postcode',
        'items',
        'items.sku',
        'attributes',
    ]
    assert columns['order_id'].data_type == 'long'
    assert columns['order_id'].comment == 'Order key'
    assert columns['order_id'].attrs == {'field_id': 1, 'required': True, 'identifier': True}
    assert columns['shipping.postcode'].attrs['nested'] is True
    assert columns['shipping.postcode'].comment == 'UK postcode'
    assert columns['attributes'].data_type == 'map<string, string>'
    assert orders.attrs['identifier_fields'] == ['order_id']
    assert orders.attrs['partitioned_by'] == ['day(ordered_at)']


@pytest.mark.iceberg
def test_iceberg_sql_catalog(tmp_path: Path) -> None:
    from pyiceberg.catalog.sql import SqlCatalog

    properties = {'type': 'sql', 'uri': f'sqlite:///{tmp_path}/catalog.db', 'warehouse': f'file://{tmp_path}'}
    catalog = SqlCatalog('lake', **{k: v for k, v in properties.items() if k != 'type'})
    _make_iceberg_tables(catalog)
    connector = IcebergConnector('lake', properties)
    _check_iceberg(connector.fetch(), 'lake')
    assert IcebergConnector('lake', properties, namespaces=['sales']).fetch()[0].name == 'orders'


@pytest.mark.iceberg_rest
def test_iceberg_rest_catalog() -> None:
    from pyiceberg.catalog import load_catalog

    uri = require('KONSTRUCT_TEST_ICEBERG_URI')
    name = f'rest_{uuid.uuid4().hex[:6]}'
    namespace = f'kp_{uuid.uuid4().hex[:8]}'
    properties = {'type': 'rest', 'uri': uri, 'py-io-impl': 'pyiceberg.io.fsspec.FsspecFileIO'}
    catalog = load_catalog(name, **properties)
    _make_iceberg_tables(catalog, namespace)
    try:
        connector = IcebergConnector(name, {'type': 'rest', 'uri': uri}, namespaces=[namespace])
        _check_iceberg(connector.fetch(), name, namespace)
    finally:
        catalog.drop_table(f'{namespace}.orders')
        catalog.drop_namespace(namespace)


@pytest.mark.parametrize(
    ('dtype', 'dialect', 'expected'),
    [
        ('STRUCT(a INTEGER, b STRUCT(c VARCHAR))', 'duckdb', ['x.a', 'x.b', 'x.b.c']),
        ('STRUCT(sku VARCHAR, qty INTEGER)[]', 'duckdb', ['x.sku', 'x.qty']),
        ('MAP(VARCHAR, STRUCT(v INTEGER))', 'duckdb', ['x.v']),
        ('ARRAY<STRUCT<sku STRING>>', 'bigquery', ['x.sku']),
        ('struct<a:int,b:array<struct<c:string>>>', 'databricks', ['x.a', 'x.b', 'x.b.c']),
        ('INTEGER', 'duckdb', []),
        ('STRUCT(broken', 'duckdb', []),
    ],
)
def test_struct_fields(dtype: str, dialect: str, expected: list[str]) -> None:
    from konstruct.inputs.warehouse import struct_fields

    assert [c.name for c in struct_fields('x', dtype, dialect)] == expected
