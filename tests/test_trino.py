"""Trino: metadata from each catalogue's information_schema and system.metadata, and samples for
verify.

The connector is tested against a stub of the ``trino`` client everywhere, and against a Trino
server (its memory catalogue) when KONSTRUCT_TEST_TRINO_HOST is set: the ``trino`` CI job.
"""

from __future__ import annotations

import os
import sys
import types
import uuid
from pathlib import Path
from typing import Any

import pytest

from konstruct import Project
from konstruct.config import discover_warehouses, load_config
from konstruct.dbt import read_project
from konstruct.verify import DbApiSource, _for_duckdb, _sample_type, sampled_engine
from konstruct.warehouse import MissingDriver, TrinoConnector


class _Cursor:
    def __init__(self, server: _Server) -> None:
        self.server = server
        self.rows: list[tuple[Any, ...]] = []
        self.description: list[tuple[Any, ...]] = []

    def execute(self, sql: str) -> None:
        self.server.log.append(sql)
        if '.information_schema.tables' in sql:
            self.rows = [
                ('sales', 'orders', 'BASE TABLE'),
                ('sales', 'big_orders', 'VIEW'),
                ('ops', 'audit', 'BASE TABLE'),
            ]
        elif '.information_schema.columns' in sql:
            rows = [
                ('sales', 'orders', 'id', 'bigint', 'Order id', 1),
                ('sales', 'orders', 'customer', 'row(country varchar, address row(city varchar))', None, 2),
                ('sales', 'orders', 'amount', 'decimal(10,2)', None, 3),
                ('sales', 'big_orders', 'id', 'bigint', None, 1),
                ('sales', 'big_orders', 'doubled', 'decimal(11,2)', None, 2),
                ('ops', 'audit', 'at', 'timestamp(3) with time zone', None, 1),
            ]
            self.rows = rows
        elif '.information_schema.views' in sql:
            self.rows = [('sales', 'big_orders', 'SELECT id, amount * 2 AS doubled FROM lake.sales.orders')]
        elif 'system.metadata.table_comments' in sql:
            if self.server.old:
                raise RuntimeError('Access Denied')
            self.rows = [('sales', 'orders', 'All orders')]
        elif sql.startswith('SELECT * FROM'):
            self.description = [('id', 'bigint'), ('at', 'timestamp(3) with time zone')]
            self.rows = [(1, None)]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def close(self) -> None:
        pass


class _Server:
    def __init__(self) -> None:
        self.log: list[str] = []
        self.options: list[dict[str, Any]] = []
        self.old = False

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def close(self) -> None:
        pass


@pytest.fixture
def trino_stub(monkeypatch: pytest.MonkeyPatch) -> _Server:
    server = _Server()

    def connect(**options: Any) -> _Server:
        server.options.append(options)
        return server

    package = types.ModuleType('trino')
    dbapi = types.ModuleType('trino.dbapi')
    auth = types.ModuleType('trino.auth')
    dbapi.connect = connect  # type: ignore[attr-defined]
    auth.BasicAuthentication = lambda user, password: ('basic', user, password)  # type: ignore[attr-defined]
    auth.JWTAuthentication = lambda token: ('jwt', token)  # type: ignore[attr-defined]
    package.dbapi = dbapi  # type: ignore[attr-defined]
    package.auth = auth  # type: ignore[attr-defined]
    for name, module in (('trino', package), ('trino.dbapi', dbapi), ('trino.auth', auth)):
        monkeypatch.setitem(sys.modules, name, module)
    return server


def test_trino_metadata(trino_stub: _Server) -> None:
    connector = TrinoConnector({'host': 'h', 'catalog': 'lake'}, schemas=['sales'])
    relations = {r.name: r for r in connector.fetch()}
    assert set(relations) == {'orders', 'big_orders'}
    orders = relations['orders']
    assert orders.parts == ('lake', 'sales', 'orders') and orders.comment == 'All orders'
    assert [(c.name, c.comment) for c in orders.columns] == [
        ('id', 'Order id'),
        ('customer', None),
        ('customer.country', None),
        ('customer.address', None),
        ('customer.address.city', None),
        ('amount', None),
    ]
    assert relations['big_orders'].kind == 'view'
    assert relations['big_orders'].view_sql == (
        'create view lake.sales.big_orders as SELECT id, amount * 2 AS doubled FROM lake.sales.orders'
    )
    assert connector.default_schema == 'default'
    assert all('"lake".information_schema' in s or 'system.metadata' in s for s in trino_stub.log)


def test_unreadable_table_comments(trino_stub: _Server) -> None:
    trino_stub.old = True
    relations = {r.name: r for r in TrinoConnector({'catalog': 'lake'}).fetch()}
    assert relations['orders'].comment is None
    assert relations['orders'].columns[0].comment == 'Order id'


def test_schemas_and_catalogues(trino_stub: _Server) -> None:
    TrinoConnector({'catalog': 'Lake'}, schemas=['Sales']).fetch()
    assert all('"lake".' in sql or "catalog_name = 'lake'" in sql for sql in trino_stub.log)
    assert all("in ('sales')" in sql for sql in trino_stub.log if 'views' not in sql)
    with pytest.raises(ValueError, match='no catalogue to read'):
        TrinoConnector({'host': 'h'}).fetch()


def test_authentication_options(trino_stub: _Server) -> None:
    TrinoConnector({'user': 'me', 'catalog': 'lake'}, password='pw').fetch()
    TrinoConnector({'catalog': 'lake', 'http_scheme': 'https'}, token='t').fetch()
    TrinoConnector({'catalog': 'lake'}).fetch()
    assert trino_stub.options == [
        {'user': 'me', 'catalog': 'lake', 'auth': ('basic', 'me', 'pw'), 'http_scheme': 'https'},
        {'catalog': 'lake', 'http_scheme': 'https', 'auth': ('jwt', 't')},
        {'catalog': 'lake'},
    ]


def test_trino_in_konstruct_toml_builds_view_lineage(trino_stub: _Server, tmp_path: Path) -> None:
    (tmp_path / 'konstruct.toml').write_text(
        '[[warehouse]]\ntype = "trino"\nhost = "h"\nuser = "me"\npassword = "pw"\ncatalogs = ["lake"]\n'
    )
    (warehouse,) = load_config(tmp_path).warehouses or []
    connector = warehouse.connector(tmp_path)
    assert isinstance(connector, TrinoConnector)
    assert (connector.catalogs, connector.password, 'password' in connector.options) == (
        ['lake'],
        'pw',
        False,
    )
    project = Project(tmp_path)
    graph = project.build().graph
    assert graph.meta['dialect'] == 'trino'
    assert project.trace('big_orders.doubled').columns[0].label == 'orders.amount'
    assert graph.warnings == []


def test_dbt_trino_profiles(tmp_path: Path) -> None:
    cases = (
        ('plain', ''),
        ('ldap', '\n      method: ldap\n      password: pw'),
        ('kerberos', '\n      method: kerberos'),
    )
    for name, extra in cases:
        root = tmp_path / name
        root.mkdir()
        (root / 'dbt_project.yml').write_text(f'name: {name}\nprofile: {name}\n')
        (root / 'profiles.yml').write_text(
            f'{name}:\n  target: dev\n  outputs:\n    dev:\n      type: trino\n      host: t.example.com\n'
            f'      port: "8443"\n      user: me\n      database: lake\n      schema: sales\n'
            f'      http_scheme: https{extra}\n'
        )
    config = load_config(tmp_path)
    projects = [p for p in (read_project(tmp_path / n) for n in ('plain', 'ldap', 'kerberos')) if p]
    plain, ldap, kerberos = discover_warehouses(config, projects)
    assert plain.options == {
        'host': 't.example.com',
        'port': 8443,
        'user': 'me',
        'catalog': 'lake',
        'schema': 'sales',
        'http_scheme': 'https',
    }
    assert ldap.options['password'] == 'pw'
    with pytest.raises(ValueError, match="dbt profile method 'kerberos' is not supported"):
        kerberos.connector(tmp_path)
    (tmp_path / 'konstruct.toml').write_text('[[warehouse]]\ntype = "trino"\ncatalog = "lake"\ntoken = ""\n')
    (empty,) = load_config(tmp_path).warehouses or []
    with pytest.raises(ValueError, match='token is set but empty'):
        empty.connector(tmp_path)


def test_samples_keep_trino_types(trino_stub: _Server) -> None:
    assert _sample_type('timestamp(3) with time zone', 'trino', []) == 'TIMESTAMPTZ'
    assert _sample_type('time(3) with time zone', 'trino', []) == 'TIMETZ'
    assert _sample_type('row(a bigint)', 'trino', []) == 'JSON'
    assert _sample_type('decimal(10,2)', 'trino', []) == 'DECIMAL(10, 2)'
    engine = sampled_engine(TrinoConnector({'catalog': 'lake'}))
    assert isinstance(engine.source, DbApiSource) and engine.source.dialect == 'trino'
    names, types_, rows = engine.source.sample(('lake', 'sales', 'orders'), 5)
    assert (names, types_, rows) == (['id', 'at'], ['bigint', 'timestamp(3) with time zone'], [(1, None)])
    assert trino_stub.log[-1] == 'SELECT * FROM "lake"."sales"."orders" LIMIT 5'


def test_rows_and_maps_are_kept_as_json() -> None:
    class Row(tuple[Any, ...]):
        _names = ('country', 'size')

    import decimal

    assert _for_duckdb(Row(('NZ', 7)), 'JSON') == '{"country": "NZ", "size": 7}'
    assert _for_duckdb({decimal.Decimal('1.25'): 2}, 'JSON') == '{"1.25": 2}'

    class Anonymous(tuple[Any, ...]):
        _names = (None, None)

    assert _for_duckdb(Anonymous((1, 2)), 'JSON') == '{"field0": 1, "field1": 2}'


def test_a_missing_driver_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, 'trino', None)
    with pytest.raises(MissingDriver, match=r'konstruct\[trino\]'):
        TrinoConnector({'catalog': 'lake'}).fetch()


@pytest.mark.trino
def test_against_a_trino_server(tmp_path: Path) -> None:
    host = os.environ.get('KONSTRUCT_TEST_TRINO_HOST')
    if not host:
        pytest.skip('KONSTRUCT_TEST_TRINO_HOST is not set')
    port = int(os.environ.get('KONSTRUCT_TEST_TRINO_PORT', '8080'))
    schema = f'k_{uuid.uuid4().hex[:8]}'
    options = {'host': host, 'port': port, 'user': 'konstruct', 'catalog': 'memory'}
    TrinoConnector(options).connect().cursor().execute(f'create schema memory.{schema}').fetchall()
    connection = TrinoConnector({**options, 'schema': schema}).connect()
    cursor = connection.cursor()

    def run(sql: str) -> None:
        cursor.execute(sql)
        cursor.fetchall()

    try:
        run(
            f'create table memory.{schema}.orders (id bigint, amount decimal(10,2), '
            'customer row(country varchar))'
        )
        run(f"comment on table memory.{schema}.orders is 'All orders'")
        run(f"insert into memory.{schema}.orders values (1, 2.50, row('NZ')), (2, 4.00, row('UK'))")
        run(f'create view memory.{schema}.big_orders as select id, amount * 2 as doubled from orders')
        connector = TrinoConnector({**options, 'schema': schema}, schemas=[schema])
        relations = {r.name: r for r in connector.fetch()}
        assert set(relations) == {'orders', 'big_orders'}
        assert relations['orders'].comment == 'All orders'
        assert [c.name for c in relations['orders'].columns] == [
            'id',
            'amount',
            'customer',
            'customer.country',
        ]
        assert relations['big_orders'].kind == 'view'
        assert relations['big_orders'].view_sql is not None
        source = sampled_engine(connector).source
        names, _, rows = source.sample(('memory', schema, 'orders'), 10)
        assert names == ['id', 'amount', 'customer'] and len(rows) == 2
        source.close()
        (tmp_path / 'konstruct.toml').write_text(
            f'[[warehouse]]\ntype = "trino"\nhost = "{host}"\nport = {port}\nuser = "konstruct"\n'
            f'catalog = "memory"\nschema = "{schema}"\nschemas = ["{schema}"]\n'
        )
        project = Project(tmp_path)
        project.build()
        assert project.trace(f'memory.{schema}.big_orders.doubled').columns[0].label == 'orders.amount'
    finally:
        for name in ('big_orders',):
            run(f'drop view if exists memory.{schema}.{name}')
        run(f'drop table if exists memory.{schema}.orders')
        run(f'drop schema if exists memory.{schema}')
