"""Fivetran and Airbyte: the tables they land, and Fivetran's lineage from source systems."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from konstruct import Project
from konstruct.engine.config import ConfigError, load_config
from konstruct.inputs.loaders import LineageSchema, airbyte_raw_namespace, lineage_sql, loader_of
from konstruct.interfaces.status import inspect, render_config
from konstruct.outputs.report import analyse

LOG = """
create schema fivetran_log;
create table fivetran_log.connection (connection_id varchar, connection_name varchar);
create table fivetran_log.source_schema (id varchar, connection_id varchar, name varchar);
create table fivetran_log.source_table (
    id varchar, connection_id varchar, schema_id varchar, name varchar, deleted_from_source_at timestamp
);
create table fivetran_log.source_column (
    id varchar, connection_id varchar, table_id varchar, name varchar, type varchar,
    deleted_from_source_at timestamp
);
create table fivetran_log.destination_schema (id varchar, destination_id varchar, name varchar);
create table fivetran_log.destination_table (id varchar, destination_id varchar, schema_id varchar, name varchar);
create table fivetran_log.destination_column (
    id varchar, destination_id varchar, table_id varchar, name varchar, type varchar
);
create table fivetran_log.table_lineage (source_table_id varchar, destination_table_id varchar);
create table fivetran_log.column_lineage (source_column_id varchar, destination_column_id varchar);
insert into fivetran_log.connection values
    ('c1', 'salesforce_prod'), ('c2', 'other_destination'), ('c3', 'salesforce_sandbox');
insert into fivetran_log.source_schema values ('ss1', 'c1', 'Salesforce'), ('ss2', 'c2', 'erp'), ('ss3', 'c3', 'Salesforce');
insert into fivetran_log.source_table values
    ('st1', 'c1', 'ss1', 'Account', null), ('st2', 'c1', 'ss1', 'Lead', now()), ('st3', 'c2', 'ss2', 'po', null),
    ('st4', 'c3', 'ss3', 'Account', null);
insert into fivetran_log.source_column values
    ('sc1', 'c1', 'st1', 'Id', 'id', null), ('sc2', 'c1', 'st1', 'Name', 'string', null),
    ('sc3', 'c1', 'st1', 'Gone', 'string', now()), ('sc4', 'c2', 'st3', 'id', 'int', null),
    ('sc5', 'c3', 'st4', 'Name', 'string', null);
insert into fivetran_log.destination_schema values
    ('ds0', 'd1', 'fivetran_log'), ('ds1', 'd1', 'salesforce'), ('ds2', 'd2', 'erp'), ('ds3', 'd2', 'salesforce');
insert into fivetran_log.destination_table values
    ('dt1', 'd1', 'ds1', 'account'), ('dt2', 'd2', 'ds2', 'po'), ('dt3', 'd2', 'ds3', 'account');
insert into fivetran_log.destination_column values
    ('dc1', 'd1', 'dt1', 'id', 'varchar'), ('dc2', 'd1', 'dt1', 'name', 'varchar'),
    ('dc3', 'd1', 'dt1', 'gone', 'varchar'), ('dc4', 'd2', 'dt2', 'id', 'int'), ('dc5', 'd2', 'dt3', 'name', 'varchar');
insert into fivetran_log.table_lineage values ('st1', 'dt1'), ('st2', 'dt1'), ('st3', 'dt2'), ('st4', 'dt3');
insert into fivetran_log.column_lineage values
    ('sc1', 'dc1'), ('sc2', 'dc2'), ('sc3', 'dc3'), ('sc4', 'dc4'), ('sc5', 'dc5');
"""

LANDED = """
create schema salesforce;
create table salesforce.account (id varchar, name varchar, gone varchar, _fivetran_synced timestamp);
create schema hubspot;
create table hubspot.contacts (id bigint, email varchar, _airbyte_raw_id varchar, _airbyte_extracted_at timestamp);
create schema airbyte_internal;
create table airbyte_internal.hubspot_raw__stream_contacts (
    _airbyte_raw_id varchar, _airbyte_data json, _airbyte_extracted_at timestamp, _airbyte_loaded_at timestamp
);
create schema legacy;
create table legacy.deals (id bigint, _airbyte_ab_id varchar, _airbyte_emitted_at timestamp);
create table main.plain (id bigint);
"""


def warehouse(root: Path, *scripts: str) -> Path:
    path = root / 'warehouse.duckdb'
    con = duckdb.connect(str(path))
    for script in scripts:
        con.execute(script)
    con.close()
    return path


def test_which_tool_landed_a_table() -> None:
    assert loader_of(['id', '_FIVETRAN_SYNCED']) == 'fivetran'
    assert loader_of(['_airbyte_raw_id', '_airbyte_extracted_at']) == 'airbyte'
    assert loader_of(['_airbyte_ab_id', '_airbyte_emitted_at']) == 'airbyte'
    assert loader_of(['_airbyte_raw_id']) is None
    assert airbyte_raw_namespace('airbyte_internal', 'hubspot_raw__stream_contacts') == 'hubspot'
    assert airbyte_raw_namespace('hubspot', 'contacts') is None


def test_landed_tables_get_a_loader_task_per_schema(tmp_path: Path) -> None:
    warehouse(tmp_path, LANDED)
    graph = Project(tmp_path).build(run_dbt=False).graph
    salesforce = graph.nodes['task.fivetran.warehouse.salesforce']
    assert (salesforce.kind, salesforce.attrs['loader']) == ('task', 'fivetran')
    feeds = {(e.source, e.target) for e in graph.edges if e.kind == 'feeds'}
    assert ('task.fivetran.warehouse.salesforce', 'table.warehouse.salesforce.account') in feeds
    assert ('task.airbyte.warehouse.hubspot', 'table.warehouse.hubspot.contacts') in feeds
    assert (
        'table.warehouse.airbyte_internal.hubspot_raw__stream_contacts',
        'task.airbyte.warehouse.hubspot',
    ) in feeds
    assert ('task.airbyte.warehouse.legacy', 'table.warehouse.legacy.deals') in feeds
    assert graph.nodes['table.warehouse.salesforce.account'].attrs['loaded_by'] == 'fivetran'
    assert 'loaded_by' not in graph.nodes['table.warehouse.main.plain'].attrs
    assert graph.meta['inputs']['landed_tables'] == 3


def test_fivetran_lineage_reaches_the_source_system(tmp_path: Path) -> None:
    warehouse(tmp_path, LANDED, LOG)
    (tmp_path / 'accounts.sql').write_text(
        'create table warehouse.main.accounts as select upper(name) as account_name from salesforce.account'
    )
    project = Project(tmp_path)
    graph = project.build(run_dbt=False).graph
    source = graph.nodes['fivetran.salesforce_prod.salesforce.account']
    assert (source.kind, source.name, source.attrs['system']) == (
        'table',
        'salesforce_prod.Salesforce.Account',
        'salesforce_prod',
    )
    assert source.relation is None
    assert [(c.name, c.data_type) for c in graph.columns_of(source.id)] == [('Id', 'id'), ('Name', 'string')]
    assert [c.label for c in project.trace('accounts.account_name').columns] == [
        'account.name',
        'salesforce_prod.Salesforce.Account.Name',
    ]
    sync = graph.nodes['task.fivetran.warehouse.salesforce']
    assert sync.attrs['connection'] == 'salesforce_prod'
    feeds = {(e.source, e.target) for e in graph.edges if e.kind == 'feeds'}
    assert (source.id, sync.id) in feeds
    # A deleted source table, and other destinations (one landing a schema of the same name),
    # are left out.
    assert 'fivetran.salesforce_prod.salesforce.lead' not in graph.nodes
    assert not [
        n for n in graph.nodes if n.startswith(('fivetran.other_destination', 'fivetran.salesforce_sandbox'))
    ]
    assert graph.meta['inputs']['fivetran_source_tables'] == 1
    assert {'input': 'fivetran', 'name': 'warehouse.fivetran_log', 'detail': '1 source table'} in graph.meta[
        'sources'
    ]
    assert not graph.warnings
    # Short names still mean the landed table, its formula is complete, and landed tables are
    # roots for the report's coverage.
    assert [c.label for c in project.impact('account.name').columns] == ['accounts.account_name']
    assert project.impact('salesforce.account.name').columns
    assert project.explain('accounts.account_name').complete
    findings = analyse(graph)
    assert (findings.lineage_covered, findings.lineage_total) == (1, 1)


def test_the_destination_must_be_known(tmp_path: Path) -> None:
    warehouse(
        tmp_path,
        LANDED,
        LOG,
        "insert into fivetran_log.destination_schema values ('ds9', 'd2', 'fivetran_log')",
    )
    graph = Project(tmp_path).build(run_dbt=False).graph
    assert graph.warnings == [
        'fivetran: lineage in warehouse.fivetran_log: cannot tell which Fivetran destination this warehouse is '
        '(2 destinations, and not one alone lands fivetran_log)'
    ]


def test_a_log_of_one_destination(tmp_path: Path) -> None:
    single = (
        LOG.replace("('ds0', 'd1', 'fivetran_log'), ", '')
        .replace("('ds2', 'd2', 'erp'), ('ds3', 'd2', 'salesforce')", "('ds2', 'd1', 'erp')")
        .replace("('dt2', 'd2', 'ds2', 'po'), ('dt3', 'd2', 'ds3', 'account')", "('dt2', 'd1', 'ds2', 'po')")
        .replace("('st4', 'dt3')", "('st1', 'dt1')")
        .replace("('sc5', 'dc5')", "('sc1', 'dc1')")
    )
    warehouse(tmp_path, LANDED, single)
    graph = Project(tmp_path).build(run_dbt=False).graph
    assert not graph.warnings
    assert 'fivetran.salesforce_prod.salesforce.account' in graph.nodes


def test_names_that_need_quoting(tmp_path: Path) -> None:
    path = warehouse(tmp_path, LANDED, LOG)
    path.rename(tmp_path / 'my-warehouse.duckdb')
    graph = Project(tmp_path).build(run_dbt=False).graph
    assert not graph.warnings
    assert 'fivetran.salesforce_prod.salesforce.account' in graph.nodes
    schema = LineageSchema(
        ('ANALYTICS', 'FIVETRAN_LOG'),
        {
            'source_schema': frozenset(('id', 'connection_id', 'name')),
            'source_table': frozenset(),
            'source_column': frozenset(),
        },
        {'column_lineage': 'COLUMN_LINEAGE'},
    )
    columns_sql, _ = lineage_sql(schema, 'snowflake', 'd1', ['SALESFORCE'])
    assert '"ANALYTICS"."FIVETRAN_LOG"."COLUMN_LINEAGE"' in columns_sql
    assert "lower(ds.name) in ('salesforce')" in columns_sql


def test_older_platform_connector_schemas() -> None:
    columns = {
        'source_schema': frozenset(('id', 'connector_id', 'name')),
        'connector': frozenset(('connector_id', 'connector_name')),
        'source_table': frozenset(('id', 'schema_id', 'name')),
        'source_column': frozenset(('id', 'table_id', 'name')),
    }
    old = LineageSchema(('db', 'fivetran_log'), columns, {})
    column_sql, _ = lineage_sql(old, 'snowflake', 'd1', ['salesforce'])
    assert 'coalesce(c.connector_name, ss.connector_id)' in column_sql
    assert 'left join "db"."fivetran_log"."connector" c on c.connector_id = ss.connector_id' in column_sql
    assert 'deleted_from_source_at' not in column_sql and 'null' in column_sql
    bare = LineageSchema(('p', 'fivetran_log'), {**columns, 'connector': frozenset()}, {})
    tables_sql = lineage_sql(bare, 'bigquery', 'd1', [])[1]
    assert tables_sql.startswith('select ss.connector_id, ss.name')
    assert '`p`.`fivetran_log`.`table_lineage`' in tables_sql
    assert "lower(ds.name) in ('')" in tables_sql


def test_unreadable_lineage_is_a_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    warehouse(tmp_path, LANDED, LOG)
    from konstruct.inputs import warehouse as module

    def refuse(connector: object, sql: str) -> None:
        raise RuntimeError('permission denied for schema fivetran_log')

    monkeypatch.setattr(module, 'query', refuse)
    graph = Project(tmp_path).build(run_dbt=False).graph
    assert graph.warnings == [
        'fivetran: lineage in warehouse.fivetran_log: permission denied for schema fivetran_log'
    ]


def test_lineage_can_be_turned_off(tmp_path: Path) -> None:
    warehouse(tmp_path, LANDED, LOG)
    rendered = render_config(load_config(tmp_path), inspect(tmp_path), [])
    assert '[fivetran]\n# lineage = true' in rendered
    (tmp_path / 'konstruct.toml').write_text('[fivetran]\nlineage = false\n')
    graph = Project(tmp_path).build(run_dbt=False).graph
    assert not [n for n in graph.nodes if n.startswith('fivetran.')]
    (tmp_path / 'konstruct.toml').write_text('[fivetran]\nlineage = "no"\n')
    with pytest.raises(ConfigError, match=r"\[fivetran\] lineage must be true or false, not 'no'"):
        load_config(tmp_path)
