"""Column usage from query history, against stub drivers that return each vendor's row shapes."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from konstruct import Project
from konstruct.inputs.usage import (
    UsageSettings,
    aggregate_queries,
    bigquery_sql,
    databricks_sql,
    snowflake_sql,
)
from konstruct.interfaces.cli import main

# The SQL each reader sends, checked against the documented views


def test_snowflake_reads_access_history() -> None:
    sql = snowflake_sql(UsageSettings(days=30, exclude_users=['dbt_svc', "O'Brien"]))
    assert 'from snowflake.account_usage.access_history ah' in sql
    assert 'lateral flatten(input => ah.direct_objects_accessed) f' in sql
    assert 'lateral flatten(input => f.value:"columns") c' in sql
    assert 'dateadd(day, -30, current_timestamp())' in sql
    assert "upper(ah.user_name) not in ('DBT_SVC', 'O''BRIEN')" in sql
    assert 'base_objects_accessed' in snowflake_sql(UsageSettings(objects='base'))


def test_databricks_reads_column_lineage() -> None:
    sql = databricks_sql(UsageSettings(days=7))
    assert 'from system.access.column_lineage' in sql
    assert 'event_date >= date_sub(current_date(), 7)' in sql
    assert 'count(distinct coalesce(statement_id, event_id)) as queries' in sql


def test_bigquery_reads_the_regional_jobs_view() -> None:
    sql = bigquery_sql('acme', 'EU', UsageSettings(days=180, exclude_users=['bot@acme.iam']))
    assert 'from `acme`.`region-eu`.INFORMATION_SCHEMA.JOBS' in sql
    assert "job_type = 'QUERY' and statement_type <> 'SCRIPT' and state = 'DONE'" in sql
    assert 'interval 180 day' in sql and "user_email not in ('bot@acme.iam')" in sql


def test_query_texts_are_parsed_once_and_counted() -> None:
    calls: list[str] = []

    def parse(sql: str) -> list[tuple[str, str]]:
        calls.append(sql)
        if 'broken' in sql:
            raise ValueError('cannot parse')
        return [('shop.orders', 'amount'), ('shop.orders', 'amount'), ('shop.orders', 'status')]

    rows = [('q1', 'ann', 3), ('q1', 'bob', 2), ('q2 broken', 'ann', 9), ('', 'ann', 1)]
    result = {(u.relation, u.column): (u.queries, u.users) for u in aggregate_queries(rows, parse)}
    assert result == {('shop.orders', 'amount'): (5, 2), ('shop.orders', 'status'): (5, 2)}
    assert calls == ['q1', 'q2 broken']


# Builds with usage, through stub drivers


class _Cursor:
    def __init__(
        self, tables: list[tuple[Any, ...]], columns: list[tuple[Any, ...]], usage: Any, log: list[str]
    ) -> None:
        self.tables, self.columns, self.usage, self.log = tables, columns, usage, log
        self.rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str) -> None:
        self.log.append(sql)
        if 'access_history' in sql or 'column_lineage' in sql:
            if isinstance(self.usage, Exception):
                raise self.usage
            self.rows = list(self.usage)
        elif '.tables' in sql:
            self.rows = self.tables
        elif '.columns' in sql:
            self.rows = self.columns
        else:
            self.rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


def _stub(monkeypatch: pytest.MonkeyPatch, module_name: str, cursor: _Cursor) -> None:
    connection = types.SimpleNamespace(cursor=lambda: cursor, close=lambda: None)
    module = types.ModuleType(module_name)
    module.connect = lambda **kwargs: connection  # type: ignore[attr-defined]
    package_name = module_name.split('.')[0]
    package = types.ModuleType(package_name)
    setattr(package, module_name.split('.')[1], module)
    monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(sys.modules, module_name, module)


SNOWFLAKE_TABLES = [
    ('SHOP', 'MART', 'ORDERS', 'BASE TABLE', None),
    ('SHOP', 'MART', 'CUSTOMERS', 'BASE TABLE', None),
]
SNOWFLAKE_COLUMNS = [
    ('SHOP', 'MART', 'ORDERS', 'ID', 'NUMBER', None, 1),
    ('SHOP', 'MART', 'ORDERS', 'AMOUNT', 'NUMBER', None, 2),
    ('SHOP', 'MART', 'ORDERS', 'STATUS', 'TEXT', None, 3),
    ('SHOP', 'MART', 'CUSTOMERS', 'ID', 'NUMBER', None, 1),
    ('SHOP', 'MART', 'CUSTOMERS', 'REGION', 'TEXT', None, 2),
]
# ACCESS_HISTORY, aggregated by the reader's query: object name, column name, queries, users.
SNOWFLAKE_USAGE = [
    ('SHOP.MART.ORDERS', 'AMOUNT', 1200, 14),
    ('SHOP.MART.ORDERS', 'STATUS', 300, 9),
    ('SHOP.MART.CUSTOMERS', 'REGION', 4, 1),
    ('SHOP.MART.DROPPED_TABLE', 'X', 10, 2),
]


def _snowflake_project(root: Path, usage: str) -> None:
    (root / 'konstruct.toml').write_text(
        f'[[warehouse]]\ntype = "snowflake"\ndatabase = "SHOP"\nschema = "MART"\n\n[usage]\n{usage}'
    )


def test_snowflake_usage_on_columns_report_and_describe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log: list[str] = []
    _stub(
        monkeypatch, 'snowflake.connector', _Cursor(SNOWFLAKE_TABLES, SNOWFLAKE_COLUMNS, SNOWFLAKE_USAGE, log)
    )
    _snowflake_project(tmp_path, 'enabled = true\ndays = 30\nexclude_users = ["DBT"]\n')
    project = Project(tmp_path)
    graph = project.build().graph
    amount = graph.nodes['table.shop.mart.orders#amount']
    assert amount.attrs['usage'] == {'queries': 1200, 'users': 14, 'score': 100, 'days': 30}
    assert 0 < graph.nodes['table.shop.mart.customers#region'].attrs['usage']['score'] < 30
    assert 'usage' not in graph.nodes['table.shop.mart.orders#id'].attrs
    assert graph.meta['usage'] == {
        'days': 30,
        'sources': ['Snowflake access history'],
        'columns': 3,
        'unmatched': 1,
    }
    assert any('access_history' in sql and "'DBT'" in sql for sql in log)
    report = (project.out_dir / 'REPORT.md').read_text()
    assert '## Most queried columns' in report
    assert 'From Snowflake access history, the last 30 days' in report
    assert '| `ORDERS.AMOUNT` | 1200 | 14 | 100 |' in report
    assert 'usage: 1200 queries by 14 users in 30 days (score 100)' in str(project.describe('orders.amount'))
    # Read once a day: an update reuses the counts without querying the history again.
    queries = sum('access_history' in sql for sql in log)
    project.build(update=True)
    assert sum('access_history' in sql for sql in log) == queries


def test_usage_is_off_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log: list[str] = []
    _stub(
        monkeypatch, 'snowflake.connector', _Cursor(SNOWFLAKE_TABLES, SNOWFLAKE_COLUMNS, SNOWFLAKE_USAGE, log)
    )
    _snowflake_project(tmp_path, '')
    graph = Project(tmp_path).build().graph
    assert not any('access_history' in sql for sql in log)
    assert 'usage' not in graph.meta
    assert not any(n.attrs.get('usage') for n in graph.nodes.values())


def test_missing_permissions_are_a_warning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    denied = RuntimeError(
        "SQL access control error: Insufficient privileges to operate on view 'ACCESS_HISTORY'"
    )
    _stub(monkeypatch, 'snowflake.connector', _Cursor(SNOWFLAKE_TABLES, SNOWFLAKE_COLUMNS, denied, []))
    _snowflake_project(tmp_path, 'enabled = true\n')
    result = Project(tmp_path).build()
    assert any(
        w.startswith('usage: snowflake') and 'cannot read query history: SQL access control error' in w
        for w in result.graph.warnings
    )
    assert result.graph.nodes['table.shop.mart.orders#amount'].kind == 'column'


def test_bad_usage_settings(tmp_path: Path) -> None:
    (tmp_path / 'konstruct.toml').write_text('[usage]\nobjects = "everything"\n')
    assert main(['build', str(tmp_path), '-q']) == 1


def test_databricks_usage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tables = [('main', 'sales', 'orders', 'MANAGED', None)]
    columns = [
        ('main', 'sales', 'orders', 'id', 'bigint', None, 1),
        ('main', 'sales', 'orders', 'amount', 'decimal(10,2)', None, 2),
    ]
    lineage = [('main.sales.orders', 'amount', 42, 3)]
    _stub(monkeypatch, 'databricks.sql', _Cursor(tables, columns, lineage, []))
    (tmp_path / 'konstruct.toml').write_text(
        '[[warehouse]]\ntype = "databricks"\nserver_hostname = "x"\ncatalogs = ["main"]\n\n[usage]\nenabled = true\n'
    )
    graph = Project(tmp_path).build().graph
    assert graph.nodes['table.main.sales.orders#amount'].attrs['usage']['queries'] == 42
    assert graph.meta['usage']['sources'] == ['Databricks column lineage']


def test_bigquery_usage_parses_query_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from konstruct.inputs.warehouse import BigQueryConnector

    field = types.SimpleNamespace
    schema = [
        field(name='id', field_type='INTEGER', mode='NULLABLE', description=None, fields=[]),
        field(name='amount', field_type='NUMERIC', mode='NULLABLE', description=None, fields=[]),
        field(name='status', field_type='STRING', mode='NULLABLE', description=None, fields=[]),
    ]
    table = field(
        table_type='TABLE',
        project='acme',
        dataset_id='shop',
        table_id='orders',
        description=None,
        schema=schema,
        view_query=None,
    )
    history = [
        {
            'query': "select sum(amount) from `acme.shop.orders` where status = 'done'",
            'user_email': 'ann@acme.io',
            'runs': 30,
        },
        {
            'query': "select sum(amount) from `acme.shop.orders` where status = 'done'",
            'user_email': 'bob@acme.io',
            'runs': 5,
        },
        {'query': 'this is not sql', 'user_email': 'ann@acme.io', 'runs': 2},
    ]
    sent: list[str] = []

    class Client:
        def list_datasets(self, project: str) -> list[Any]:
            return [field(dataset_id='shop', reference='shop')]

        def list_tables(self, reference: str) -> list[Any]:
            return [field(reference='orders')]

        def get_table(self, reference: str) -> Any:
            return table

        def query(self, sql: str) -> Any:
            sent.append(sql)
            return field(result=lambda: history)

    monkeypatch.setattr(BigQueryConnector, 'client', lambda self: Client())
    (tmp_path / 'konstruct.toml').write_text(
        '[[warehouse]]\ntype = "bigquery"\nproject = "acme"\nlocation = "EU"\n\n[usage]\nenabled = true\n'
    )
    graph = Project(tmp_path).build().graph
    assert '`acme`.`region-eu`.INFORMATION_SCHEMA.JOBS' in sent[0]
    assert graph.nodes['table.acme.shop.orders#amount'].attrs['usage']['queries'] == 35
    assert graph.nodes['table.acme.shop.orders#amount'].attrs['usage']['users'] == 2
    # Filter columns count as read.
    assert graph.nodes['table.acme.shop.orders#status'].attrs['usage']['queries'] == 35
    assert 'usage' not in graph.nodes['table.acme.shop.orders#id'].attrs
