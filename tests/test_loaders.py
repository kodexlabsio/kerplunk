"""Loaders: the tables each lands, known by its marks, and Fivetran's lineage from source systems."""

from __future__ import annotations

import time
from pathlib import Path

import duckdb
import pytest

from konstruct import Project
from konstruct.core.graph import Graph, Node
from konstruct.engine.build import Builder
from konstruct.engine.config import ConfigError, load_config
from konstruct.inputs.loaders import (
    LOADERS,
    MARKS,
    Landing,
    LineageSchema,
    Table,
    detect,
    lands_source_data,
    lineage_sql,
)
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


def one(columns: str, table: str = 'orders', schema: str = 'wh.raw') -> Landing | None:
    return detect([Table(schema, table, frozenset(columns.lower().split()))])[0]


LANDINGS = [
    # (columns, table, schema, loader, role)
    ('id _FIVETRAN_SYNCED', 'orders', 'wh.raw', 'fivetran', 'landed'),
    ('id _airbyte_raw_id _airbyte_extracted_at _airbyte_meta', 'orders', 'wh.raw', 'airbyte', 'landed'),
    ('id _airbyte_ab_id _airbyte_emitted_at', 'orders', 'wh.raw', 'airbyte', 'landed'),
    ('id _dlt_load_id _dlt_id', 'orders', 'wh.raw', 'dlt', 'landed'),
    ('value _dlt_id _dlt_parent_id _dlt_list_idx', 'orders__items', 'wh.raw', 'dlt', 'landed'),
    ('load_id schema_name status inserted_at schema_version_hash', '_dlt_loads', 'wh.raw', 'dlt', 'metadata'),
    (
        'version engine_version pipeline_name state created_at version_hash _dlt_load_id _dlt_id',
        '_dlt_pipeline_state',
        'wh.raw',
        'dlt',
        'metadata',
    ),
    (
        'version engine_version inserted_at schema_name version_hash schema',
        '_dlt_version',
        'wh.raw',
        'dlt',
        'metadata',
    ),
    (
        'id _sdc_received_at _sdc_batched_at _sdc_sequence _sdc_table_version',
        'orders',
        'wh.raw',
        'singer',
        'landed',
    ),
    ('id _SDC_EXTRACTED_AT _SDC_BATCHED_AT _SDC_DELETED_AT', 'orders', 'wh.raw', 'singer', 'landed'),
    ('id __hevo__ingested_at __hevo__loaded_at', 'orders', 'wh.raw', 'hevo', 'landed'),
    ('id __hevo_ingested_at', 'orders', 'wh.raw', 'hevo', 'landed'),
    ('id flow_published_at flow_document', 'orders', 'wh.raw', 'estuary', 'landed'),
    ('id flow_published_at _meta/op', 'orders', 'wh.raw', 'estuary', 'landed'),
    ('id __op __source_ts_ms', 'orders', 'wh.raw', 'debezium', 'landed'),
    ('id __deleted __table __lsn', 'orders', 'wh.raw', 'debezium', 'landed'),
    ('id __op __deleted __source_table', 'orders', 'wh.raw', 'debezium', 'landed'),
    ('RECORD_METADATA RECORD_CONTENT', 'orders', 'wh.raw', 'kafka_connect', 'landed'),
    (
        'TASK_NAME TABLE_OWNER TABLE_NAME ERROR_TIME STATEMENT ERROR',
        'awsdms_apply_exceptions',
        'wh.public',
        'dms',
        'metadata',
    ),
    (
        'server_name task_name task_status status_time pending_changes disk_swap_size',
        'awsdms_status',
        'wh.dmslogs',
        'dms',
        'metadata',
    ),
    (
        'server_name task_name table_owner table_name suspend_reason suspend_timestamp',
        'awsdms_suspended_tables',
        'wh.dmslogs',
        'dms',
        'metadata',
    ),
    (
        'server_name task_name timeslot_type timeslot timeslot_duration timeslot_latency',
        'awsdms_history',
        'wh.dmslogs',
        'dms',
        'metadata',
    ),
    (
        'id datastream_metadata datastream_metadata.uuid datastream_metadata.source_timestamp',
        'orders',
        'proj.public',
        'datastream',
        'landed',
    ),
    (
        'ID _SNOWFLAKE_INSERTED_AT _SNOWFLAKE_UPDATED_AT _SNOWFLAKE_DELETED',
        'orders',
        'db.public',
        'openflow',
        'landed',
    ),
    (
        'id MTLN_CDC_LAST_CHANGE_TYPE MTLN_CDC_LAST_COMMIT_TIMESTAMP MTLN_CDC_SEQUENCE_NUMBER',
        'orders',
        'wh.raw',
        'matillion',
        'landed',
    ),
    ('id mtln_cdc_deleted', 'orders', 'wh.raw', 'matillion', 'landed'),
    ('id _rivery_last_update _rivery_run_id _rivery_river_id', 'orders', 'wh.raw', 'rivery', 'landed'),
    (
        'id _polytomic_created_at _polytomic_updated_at _polytomic_deleted_at',
        'orders',
        'wh.raw',
        'polytomic',
        'landed',
    ),
    ('id _peerdb_synced_at', 'orders', 'wh.raw', 'peerdb', 'landed'),
    ('id _peerdb_is_deleted _peerdb_version', 'orders', 'wh.raw', 'peerdb', 'landed'),
    ('id _sling_loaded_at', 'orders', 'wh.raw', 'sling', 'landed'),
    ('id _sling_synced_at _sling_synced_op', 'orders', 'wh.raw', 'sling', 'landed'),
    ('id _sling_deleted_at', 'orders', 'wh.raw', 'sling', 'landed'),
    (
        'id anonymous_id user_id event event_text received_at sent_at timestamp uuid_ts context_ip',
        'tracks',
        'wh.web',
        'segment_spec',
        'landed',
    ),
    (
        'id anonymous_id event event_text received_at sent_at uuid_ts sku',
        'order_completed',
        'wh.web',
        'segment_spec',
        'landed',
    ),
    (
        'id anonymous_id user_id received_at sent_at uuid_ts email',
        'identifies',
        'wh.web',
        'segment_spec',
        'landed',
    ),
    ('id anonymous_id received_at sent_at uuid_ts path url', 'pages', 'wh.web', 'segment_spec', 'landed'),
    (
        'app_id platform etl_tstamp collector_tstamp dvce_created_tstamp event event_id v_collector v_etl '
        'domain_userid network_userid',
        'events',
        'wh.atomic',
        'snowplow',
        'landed',
    ),
    (
        'log_id sync_id sync_run_id record_identifier operation status _census_logged_at',
        'sync_log',
        'wh.census',
        'census',
        'metadata',
    ),
    (
        'sync_id sync_run_id primary_key destination model_name status started_at finished_at',
        'sync_runs',
        'wh.hightouch_audit',
        'hightouch',
        'metadata',
    ),
    (
        'sync_id sync_run_id op_type row_id status failure_reason fields',
        'sync_changelog',
        'wh.hightouch_audit',
        'hightouch',
        'metadata',
    ),
]


@pytest.mark.parametrize(('columns', 'table', 'schema', 'loader', 'role'), LANDINGS)
def test_which_tool_landed_a_table(columns: str, table: str, schema: str, loader: str, role: str) -> None:
    landing = one(columns, table, schema)
    assert landing is not None and (landing.loader, landing.role) == (loader, role)


def test_every_mark_names_a_known_loader_and_its_documentation() -> None:
    assert {m.loader for m in MARKS} <= set(LOADERS)
    assert all(m.doc.startswith('https://') for m in MARKS)
    # PeerDB documents its synced-at column, and the option that renames it, with CREATE MIRROR.
    peerdb = {m.doc for m in MARKS if m.columns == {'_peerdb_synced_at'}}
    assert peerdb == {'https://docs.peerdb.io/sql/commands/create-mirror'}
    # Every mark that stands alone is exercised above.
    tested = {loader for *_, loader, _ in LANDINGS}
    assert {m.loader for m in MARKS if m.within is None} <= tested


@pytest.mark.parametrize(
    ('columns', 'table', 'schema'),
    [
        # An application's own columns, close to a loader's but not its full set.
        ('id op deleted received_at updated_at', 'orders', 'wh.app'),
        ('id Op ts', 'orders', 'wh.app'),
        ('id deleted _deleted is_deleted', 'orders', 'wh.app'),
        ('id received_at sent_at', 'tracks', 'wh.app'),
        ('id received_at sent_at event', 'messages', 'wh.app'),
        ('id email received_at', 'identifies', 'wh.app'),
        ('id _sdc_batched_at', 'orders', 'wh.app'),
        ('id _sdc_extracted_at _sdc_batched_at', 'orders', 'wh.app'),
        ('id __op', 'orders', 'wh.app'),
        ('id __deleted', 'orders', 'wh.app'),
        # An audit table's own double-underscore columns, with no field of the change event's source.
        ('id __op __table __ts_ms', 'audit_log', 'wh.app'),
        ('id __deleted __db __table', 'orders', 'wh.app'),
        ('id record_metadata', 'orders', 'wh.app'),
        ('id _dlt_id', 'orders', 'wh.app'),
        ('id _airbyte_raw_id', 'orders', 'wh.app'),
        ('id flow_document', 'orders', 'wh.app'),
        ('id event_id collector_tstamp', 'events', 'wh.app'),
        ('id _snowflake_inserted_at _snowflake_updated_at', 'orders', 'wh.app'),
        ('id _rivery_last_update', 'orders', 'wh.app'),
        ('id _polytomic_created_at _polytomic_updated_at', 'orders', 'wh.app'),
        ('id datastream_metadata', 'orders', 'wh.app'),
        ('id mtln_cdc_last_change_type', 'orders', 'wh.app'),
        ('log_id sync_id status', 'sync_log', 'wh.app'),
        ('sync_id status started_at finished_at sync_run_id', 'sync_runs', 'wh.app'),
        ('task_name error', 'awsdms_apply_exceptions', 'wh.app'),
        ('load_id status', '_dlt_loads', 'wh.app'),
        # Companions alone, in a schema no loader lands.
        ('record reason table_name', '_sdc_rejected', 'wh.app'),
        ('id name', 'users', 'wh.app'),
        ('id payload', 'orders_journal_1705320000_1', 'wh.app'),
        ('id', 'rudder_discards', 'wh.app'),
    ],
)
def test_lookalike_columns_are_not_a_loader(columns: str, table: str, schema: str) -> None:
    assert one(columns, table, schema) is None


def test_airbyte_raw_tables_name_their_namespace() -> None:
    raw = '_airbyte_raw_id _airbyte_extracted_at _airbyte_data _airbyte_loaded_at'
    assert one(raw, 'hubspot_raw__stream_contacts', 'wh.airbyte_internal') == Landing(
        'airbyte', 'raw', 'hubspot'
    )
    assert one(raw, 'public_raw___stream_users', 'wh.airbyte_internal') == Landing('airbyte', 'raw', 'public')
    assert one(raw, 'raw_public__users', 'wh.airbyte_internal') == Landing('airbyte', 'raw', 'public')
    # The same name outside airbyte_internal, or another table in it, is landed data.
    assert one(raw, 'hubspot_raw__stream_contacts', 'wh.hubspot') == Landing('airbyte')
    assert one(raw, 'contacts', 'wh.airbyte_internal') == Landing('airbyte')


def test_a_mixed_schema_and_families_refined_by_their_companions() -> None:
    singer = '_sdc_received_at _sdc_batched_at _sdc_sequence _sdc_table_version'
    segment = 'id anonymous_id received_at sent_at uuid_ts'
    tables = [
        Table('wh.raw', 'orders', frozenset(f'id {singer}'.split())),
        Table('wh.raw', 'events', frozenset('id _dlt_load_id _dlt_id'.split())),
        Table('wh.raw', '_dlt_loads', frozenset('load_id schema_name status inserted_at'.split())),
        Table('wh.raw', 'customers', frozenset('id _fivetran_synced'.split())),
        Table('wh.raw', 'notes', frozenset('id op deleted received_at'.split())),
        Table('wh.stitch', 'orders', frozenset(f'id {singer}'.split())),
        Table('wh.stitch', '_sdc_rejected', frozenset('record reason table_name _sdc_rejected_at'.split())),
        Table('wh.web', 'identifies', frozenset(f'{segment} email'.split())),
        Table('wh.web', 'users', frozenset('id email received_at uuid_ts'.split())),
        Table('wh.app', 'identifies', frozenset(f'{segment} email'.split())),
        Table('wh.app', 'rudder_discards', frozenset('table_name column_name column_value'.split())),
        Table('wh.app', 'orders', frozenset('id total'.split())),
        Table(
            'db.public',
            'orders',
            frozenset('id _snowflake_inserted_at _snowflake_updated_at _snowflake_deleted'.split()),
        ),
        Table('db.public', 'orders_journal_1705320000_1', frozenset('id seen_at'.split())),
    ]
    found = [(f.loader, f.role) if f else None for f in detect(tables)]
    assert found == [
        ('singer', 'landed'),
        ('dlt', 'landed'),
        ('dlt', 'metadata'),
        ('fivetran', 'landed'),
        None,
        ('stitch', 'landed'),
        ('stitch', 'metadata'),
        ('segment_spec', 'landed'),
        ('segment_spec', 'landed'),
        ('rudderstack', 'landed'),
        ('rudderstack', 'metadata'),
        None,
        ('openflow', 'landed'),
        ('openflow', 'metadata'),
    ]


def test_a_declared_loader_refines_only_its_own_family() -> None:
    singer = frozenset('id _sdc_received_at _sdc_batched_at _sdc_sequence _sdc_table_version'.split())
    tables = [Table('wh.raw', 'a', singer), Table('wh.raw', 'b', singer), Table('wh.raw', 'c', singer)]
    found = detect(tables, {0: 'meltano', 1: 'fivetran', 2: 'something else'})
    assert [f.loader for f in found if f] == ['meltano', 'singer', 'singer']


def test_which_landings_count_as_evidence_for_a_pack() -> None:
    assert lands_source_data({'loaded_by': 'dlt'})
    assert lands_source_data({'loaded_by': 'debezium'})
    assert not lands_source_data({'loaded_by': 'dlt', 'loader_role': 'metadata'})
    assert not lands_source_data({'loaded_by': 'census', 'loader_role': 'metadata'})
    assert not lands_source_data({'loaded_by': 'hightouch'})
    assert not lands_source_data({'loaded_by': 'a custom script'})
    assert not lands_source_data({})


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


MIXED = """
create schema billing;
create table billing.charges (
    id varchar, amount bigint, amount_refunded bigint, captured boolean, paid boolean, currency varchar,
    livemode boolean, _dlt_load_id varchar, _dlt_id varchar
);
create table billing.customers (id varchar, delinquent boolean, livemode boolean, _dlt_load_id varchar, _dlt_id varchar);
create table billing._dlt_loads (load_id varchar, schema_name varchar, status integer, inserted_at timestamp);
create schema stitch_shop;
create table stitch_shop.orders (
    id bigint, _sdc_received_at timestamp, _sdc_batched_at timestamp, _sdc_sequence bigint, _sdc_table_version bigint
);
create table stitch_shop._sdc_rejected (record varchar, reason varchar, table_name varchar, _sdc_rejected_at timestamp);
create schema web;
create table web.tracks (
    id varchar, anonymous_id varchar, event varchar, event_text varchar, received_at timestamp, sent_at timestamp,
    uuid_ts timestamp
);
create schema census;
create table census.sync_log (
    log_id varchar, sync_id varchar, sync_run_id varchar, operation varchar, _census_logged_at timestamp
);
create schema ledger;
create table ledger.charges (
    id varchar, amount bigint, amount_refunded bigint, captured boolean, paid boolean, currency varchar,
    livemode boolean
);
create table ledger.customers (id varchar, delinquent boolean, livemode boolean);
create table ledger.awsdms_apply_exceptions (
    task_name varchar, table_owner varchar, table_name varchar, error_time timestamp, statement varchar, error varchar
);
create table main.app_events (id bigint, op varchar, deleted boolean, received_at timestamp);
"""


def test_every_loader_gets_its_tasks_and_only_landed_data_binds_packs(tmp_path: Path) -> None:
    warehouse(tmp_path, MIXED)
    graph = Project(tmp_path).build(run_dbt=False).graph
    loaded = {n.relation: n.attrs.get('loaded_by') for n in graph.relations() if n.relation}
    assert loaded == {
        'warehouse.billing.charges': 'dlt',
        'warehouse.billing.customers': 'dlt',
        'warehouse.billing._dlt_loads': 'dlt',
        'warehouse.stitch_shop.orders': 'stitch',
        'warehouse.stitch_shop._sdc_rejected': 'stitch',
        'warehouse.web.tracks': 'segment_spec',
        'warehouse.census.sync_log': 'census',
        'warehouse.ledger.charges': None,
        'warehouse.ledger.customers': None,
        'warehouse.ledger.awsdms_apply_exceptions': 'dms',
        'warehouse.main.app_events': None,
    }
    assert graph.nodes['table.warehouse.billing._dlt_loads'].attrs['loader_role'] == 'metadata'
    feeds = {(e.source, e.target) for e in graph.edges if e.kind == 'feeds'}
    for task, table in [
        ('task.dlt.warehouse.billing', 'billing.charges'),
        ('task.dlt.warehouse.billing', 'billing._dlt_loads'),
        ('task.stitch.warehouse.stitch_shop', 'stitch_shop.orders'),
        ('task.segment_spec.warehouse.web', 'web.tracks'),
        ('task.census.warehouse.census', 'census.sync_log'),
        ('task.dms.warehouse.ledger', 'ledger.awsdms_apply_exceptions'),
    ]:
        assert (task, f'table.warehouse.{table}') in feeds
    # Bookkeeping tables are not landed data.
    assert graph.meta['inputs']['landed_tables'] == 4
    # The Stripe tables dlt landed bind the pack; the same tables beside a DMS control table do not.
    bound = {
        e.target.split('#')[0]
        for n in graph.nodes.values()
        if n.attrs.get('registry')
        for e in graph.out_edges(n.id, 'defines')
    }
    assert bound and all(t.startswith('table.warehouse.billing.') for t in bound)
    findings = analyse(graph)
    assert 'table.warehouse.billing._dlt_loads' not in {n.id for n in findings.orphans}


def test_a_dbt_source_names_its_loader(tmp_path: Path) -> None:
    import json

    warehouse(
        tmp_path,
        'create schema raw; create table raw.orders (id bigint, _sdc_received_at timestamp, '
        '_sdc_batched_at timestamp, _sdc_sequence bigint, _sdc_table_version bigint);',
    )

    def source(name: str, schema: str, loader: str) -> dict[str, object]:
        return {
            'unique_id': f'source.shop.{schema}.{name}',
            'resource_type': 'source',
            'name': name,
            'source_name': schema,
            'package_name': 'shop',
            'database': 'warehouse',
            'schema': schema,
            'relation_name': f'"warehouse"."{schema}"."{name}"',
            'loader': loader,
            'columns': {},
        }

    sources = [
        source('orders', 'raw', 'Meltano'),
        source('accounts', 'crm', 'Fivetran'),
        source('notes', 'ops', 'a custom script'),
    ]
    sources.append(source('plain', 'ops', ''))
    manifest = {
        'metadata': {'adapter_type': 'duckdb'},
        'nodes': {},
        'sources': {s['unique_id']: s for s in sources},
        'exposures': {},
    }
    (tmp_path / 'dbt_project.yml').write_text('name: shop\n')
    (tmp_path / 'target').mkdir()
    (tmp_path / 'target' / 'manifest.json').write_text(json.dumps(manifest))
    graph = Project(tmp_path).build(run_dbt=False).graph
    loaded = {n.id: n.attrs.get('loaded_by') for n in graph.relations()}
    # The Singer columns say a Singer target landed it; the project says which one.
    assert loaded['source.shop.raw.orders'] == 'meltano'
    assert loaded['source.shop.crm.accounts'] == 'fivetran'
    assert loaded['source.shop.ops.notes'] == 'a_custom_script'
    assert loaded['source.shop.ops.plain'] is None
    feeds = {(e.source, e.target) for e in graph.edges if e.kind == 'feeds'}
    assert ('task.fivetran.warehouse.crm', 'source.shop.crm.accounts') in feeds
    assert ('task.meltano.warehouse.raw', 'source.shop.raw.orders') in feeds
    assert graph.nodes['source.shop.crm.accounts'].attrs['declared_loader'] == 'Fivetran'


def test_detection_over_fifty_thousand_relations_stays_fast(tmp_path: Path) -> None:
    """The landing step on 50,000 warehouse relations of twelve columns, one in ten landed."""
    graph = Graph()
    marks = [
        '_fivetran_synced',
        '_dlt_load_id _dlt_id',
        '_sdc_received_at _sdc_batched_at _sdc_sequence _sdc_table_version',
    ]
    for i in range(50_000):
        node = graph.add_node(
            Node(
                f'table.wh.s{i % 500}.t{i}',
                'table',
                f't{i}',
                relation=f'wh.s{i % 500}.t{i}',
                attrs={'in_warehouse': True},
            )
        )
        columns = [f'c{j}' for j in range(12)] + (marks[i % 3].split() if i % 10 == 0 else [])
        for column in columns:
            graph.add_column(node, column)
    tables = [
        Table(
            f'wh.s{i % 500}',
            f't{i}',
            frozenset(c.name for c in graph.columns_of(f'table.wh.s{i % 500}.t{i}')),
        )
        for i in range(50_000)
    ]
    # CPU time, so other work on the machine does not count.
    start = time.process_time()
    found = detect(tables)
    detected = time.process_time() - start
    # What any landing step costs: reading every relation's columns from the graph.
    start = time.process_time()
    for node in graph.relations():
        frozenset(c.name.lower() for c in graph.columns_of(node.id))
    reading = time.process_time() - start
    builder = Builder(load_config(tmp_path), update=False)
    builder.graph = graph
    builder.warehouse_relations = []
    start = time.process_time()
    builder.load_loaders()
    step = time.process_time() - start
    print(
        f'50,000 relations: detect {detected:.3f} s, reading columns {reading:.3f} s, load_loaders {step:.3f} s'
    )
    assert sum(f is not None for f in found) == 5_000
    assert builder.inputs['landed_tables'] == 5_000
    assert detected < 2
    assert step < 3 * reading + detected + 1


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
