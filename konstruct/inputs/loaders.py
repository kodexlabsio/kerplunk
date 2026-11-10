"""The tables loaders land in the warehouse, known by the marks each leaves, and where they come from.

Most ingestion tools mark what they write: metadata columns (``_fivetran_synced``, ``_sdc_batched_at``,
``_dlt_load_id``), bookkeeping tables (``_dlt_loads``, ``_sdc_rejected``) or a schema of their own
(``airbyte_internal``, ``hightouch_audit``). :data:`MARKS` lists them, one row per mark, each with
every column it requires, so an application's own ``op``, ``deleted`` or ``received_at`` column is
not taken for a loader's. :func:`detect` reads them from the warehouse metadata Konstruct already
has, and the build groups the tables each tool lands into one loader task per schema, so impact
and trace show which tables arrive from outside.

Some tools share a convention: Singer targets, Meltano and Stitch all add ``_sdc_*`` columns, and
Segment and RudderStack write the same event tables. A mark then names the family (``singer``,
``segment_spec``), and a companion mark that only one tool leaves in the same schema
(``_sdc_rejected`` for Stitch, ``rudder_discards`` for RudderStack) refines it; so does a loader
the project declares, in a dbt source's ``loader:`` or in a configuration file Konstruct reads.

When a warehouse holds the Fivetran Platform Connector's lineage tables (``source_column``,
``destination_column``, ``column_lineage`` and the rest, in ``fivetran_log`` or
``fivetran_metadata``), Konstruct reads them with one query each and adds every connection's
source tables and columns, outside the warehouse, with column lineage into the tables it lands.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlglot import exp

from konstruct.inputs.warehouse import RelationMeta

FIVETRAN = 'fivetran'
AIRBYTE = 'airbyte'
LINEAGE_TABLES = frozenset(
    (
        'source_schema',
        'source_table',
        'source_column',
        'destination_schema',
        'destination_table',
        'destination_column',
        'table_lineage',
        'column_lineage',
    )
)

Role = Literal['landed', 'raw', 'metadata']
"""What a table is to its loader: data it landed, a raw copy it then loads from (fed into the
task, as Airbyte's ``airbyte_internal`` tables), or its own bookkeeping."""


@dataclass(frozen=True)
class Loader:
    """An ingestion tool Konstruct knows."""

    name: str
    """The name in task ids and ``loaded_by``, such as ``dlt``."""
    title: str
    """The tool's own name."""
    lands_sources: bool = True
    """Whether it lands data from source systems, so the schemas it fills can bind ontology
    packs. Reverse ETL tools only log, in the warehouse, what they sent elsewhere."""
    refines: frozenset[str] = frozenset()
    """The families a declaration of this loader makes precise: ``stitch`` refines ``singer``."""


def _loader(name: str, title: str, *, lands_sources: bool = True, refines: str = '') -> Loader:
    return Loader(name, title, lands_sources, frozenset(refines.split()))


LOADERS: dict[str, Loader] = {
    loader.name: loader
    for loader in (
        _loader('fivetran', 'Fivetran'),
        _loader('airbyte', 'Airbyte'),
        _loader('singer', 'Singer (Meltano, PipelineWise and other Singer targets)'),
        _loader('meltano', 'Meltano', refines='singer'),
        _loader('stitch', 'Stitch', refines='singer'),
        _loader('dlt', 'dlt'),
        _loader('hevo', 'Hevo'),
        _loader('estuary', 'Estuary Flow'),
        _loader('debezium', 'Debezium change events'),
        _loader('kafka_connect', 'Kafka Connect', refines='debezium'),
        _loader('dms', 'AWS DMS'),
        _loader('datastream', 'Google Datastream'),
        _loader('openflow', 'Snowflake Openflow'),
        _loader('lakeflow', 'Databricks Lakeflow Connect'),
        _loader('matillion', 'Matillion Data Loader'),
        _loader('rivery', 'Rivery (Boomi Data Integration)'),
        _loader('polytomic', 'Polytomic'),
        _loader('peerdb', 'PeerDB'),
        _loader('sling', 'Sling'),
        _loader('segment_spec', 'Segment-spec event tables (Segment, RudderStack)'),
        _loader('segment', 'Segment', refines='segment_spec'),
        _loader('rudderstack', 'RudderStack', refines='segment_spec'),
        _loader('snowplow', 'Snowplow'),
        _loader('census', 'Census', lands_sources=False),
        _loader('hightouch', 'Hightouch', lands_sources=False),
    )
}


@dataclass(frozen=True)
class Mark:
    """One trace a loader leaves on a table it writes. Every condition must hold."""

    loader: str
    doc: str
    """The documentation that describes the trace."""
    columns: frozenset[str] = frozenset()
    """Columns the table must have (lower case)."""
    some: frozenset[str] = frozenset()
    """Columns of which the table must have at least ``at_least``."""
    at_least: int = 0
    table: re.Pattern[str] | None = None
    """The table's name, matched in full. A ``namespace`` group names the namespace whose task a
    raw table feeds."""
    schema: re.Pattern[str] | None = None
    """The schema's own name, matched in full."""
    role: Role = 'landed'
    within: str | None = None
    """For a companion mark, the loader that must land tables in the same schema. A companion of
    another loader's family refines that family's tables in the schema."""


def _mark(
    loader: str,
    doc: str,
    columns: str = '',
    *,
    some: str = '',
    at_least: int = 0,
    table: str | None = None,
    schema: str | None = None,
    role: Role = 'landed',
    within: str | None = None,
) -> Mark:
    return Mark(
        loader,
        doc,
        frozenset(columns.split()),
        frozenset(some.split()),
        at_least,
        re.compile(table) if table else None,
        re.compile(schema) if schema else None,
        role,
        within,
    )


_DLT = 'https://dlthub.com/docs/general-usage/destination-tables'
_STITCH = 'https://www.stitchdata.com/docs/replication/loading/system-tables-columns'
_SEGMENT = 'https://www.twilio.com/docs/segment/connections/storage/warehouses/schema'
_RUDDERSTACK = 'https://www.rudderstack.com/docs/destinations/warehouse-destinations/warehouse-schema/'
_SEGMENT_SPEC = 'received_at sent_at uuid_ts anonymous_id'
_DEBEZIUM = 'https://debezium.io/documentation/reference/stable/transformations/event-flattening.html'
_DEBEZIUM_SOURCE = (
    '__lsn __source_ts_ms __source_ts_us __source_ts_ns __source_table __source_db __source_schema '
    '__source_lsn __source_name __source_snapshot __source_connector __source_version __source_txid '
    '__source_xmin __source_file __source_pos __source_row __source_server_id __source_gtid '
    '__source_scn __source_change_lsn __source_commit_lsn __source_sequence'
)

MARKS: tuple[Mark, ...] = (
    # Earlier marks win: a loader's bookkeeping tables before its data tables, which may carry the
    # same columns.
    _mark(
        'fivetran', 'https://fivetran.com/docs/core-concepts/system-columns-and-tables', '_fivetran_synced'
    ),
    # Destinations V2 raw tables: raw_{namespace}__{stream} in the typing and deduping guide,
    # {namespace}_raw__stream_{stream} in the connectors (Oracle migration guide), with two or
    # three underscores before stream.
    _mark(
        'airbyte',
        'https://docs.airbyte.com/integrations/destinations/oracle-migrations',
        '_airbyte_raw_id _airbyte_extracted_at',
        table=r'(?P<namespace>.+?)_raw__+stream_.+',
        schema='airbyte_internal',
        role='raw',
    ),
    _mark(
        'airbyte',
        'https://docs.airbyte.com/platform/using-airbyte/core-concepts/typing-deduping',
        '_airbyte_raw_id _airbyte_extracted_at',
        table=r'raw_(?P<namespace>.+?)__.+',
        schema='airbyte_internal',
        role='raw',
    ),
    _mark(
        'airbyte',
        'https://docs.airbyte.com/platform/understanding-airbyte/airbyte-metadata-fields',
        '_airbyte_raw_id _airbyte_extracted_at',
    ),
    # Destinations V1, before typing and deduping.
    _mark(
        'airbyte',
        'https://docs.airbyte.com/platform/understanding-airbyte/airbyte-metadata-fields',
        '_airbyte_ab_id _airbyte_emitted_at',
    ),
    _mark('dlt', _DLT, 'load_id schema_name status inserted_at', table='_dlt_loads', role='metadata'),
    _mark(
        'dlt',
        _DLT,
        'version engine_version pipeline_name state created_at',
        table='_dlt_pipeline_state',
        role='metadata',
    ),
    _mark(
        'dlt',
        _DLT,
        'version engine_version inserted_at schema_name version_hash schema',
        table='_dlt_version',
        role='metadata',
    ),
    _mark('dlt', _DLT, '_dlt_load_id _dlt_id'),
    # A nested table, unpacked from a list in its parent's rows.
    _mark('dlt', _DLT, '_dlt_id _dlt_parent_id _dlt_list_idx'),
    # Stitch and the Meltano SDK's targets (with add_record_metadata) add these four together:
    # https://sdk.meltano.com/en/latest/implementation/record_metadata.html
    _mark('singer', _STITCH, '_sdc_received_at _sdc_batched_at _sdc_sequence _sdc_table_version'),
    _mark(
        'singer',
        'https://transferwise.github.io/pipelinewise/user_guide/metadata_columns.html',
        '_sdc_extracted_at _sdc_batched_at _sdc_deleted_at',
    ),
    # Stitch keeps rejected records in every schema it loads; sdc_rejected where a destination
    # refuses a leading underscore.
    _mark('stitch', _STITCH, table='_?sdc_rejected', role='metadata', within='singer'),
    # Some pages spell the column with one underscore after hevo, others with two.
    _mark('hevo', 'https://docs.hevodata.com/data-ingestion/hevo-generated-metadata/', '__hevo__ingested_at'),
    _mark('hevo', 'https://docs.hevodata.com/data-ingestion/hevo-generated-metadata/', '__hevo_ingested_at'),
    # flow_document can be left out (no_flow_document); _meta/op is there unless deletes are hard.
    _mark(
        'estuary',
        'https://docs.estuary.dev/reference/Connectors/materialization-connectors/Snowflake/',
        'flow_published_at flow_document',
    ),
    _mark(
        'estuary',
        'https://docs.estuary.dev/reference/Connectors/materialization-connectors/Snowflake/',
        'flow_published_at _meta/op',
    ),
    # The ExtractNewRecordState transformation adds the change event's fields it is asked for,
    # each with a double underscore: __op, or __deleted when deletes are rewritten, with a field of
    # the event's source block. __table, __db and __ts_ms alone are common audit columns.
    _mark('debezium', _DEBEZIUM, '__op', some=_DEBEZIUM_SOURCE, at_least=1),
    _mark('debezium', _DEBEZIUM, '__deleted', some=_DEBEZIUM_SOURCE, at_least=1),
    # A table the Snowflake connector for Kafka writes without schematisation.
    _mark(
        'kafka_connect',
        'https://docs.snowflake.com/en/user-guide/kafka-connector/how-the-connector-works',
        'record_metadata record_content',
    ),
    # The control tables DMS writes beside the tables it replicates; the columns it adds to those
    # tables are named by its transformation rules, so they are not marks.
    _mark(
        'dms',
        'https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Tasks.CustomizingTasks.TaskSettings.ControlTable.html',
        'task_name table_owner table_name error_time statement error',
        table='awsdms_apply_exceptions',
        role='metadata',
    ),
    _mark(
        'dms',
        'https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Tasks.CustomizingTasks.TaskSettings.ControlTable.html',
        'server_name task_name task_status status_time pending_changes',
        table='awsdms_status',
        role='metadata',
    ),
    _mark(
        'dms',
        'https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Tasks.CustomizingTasks.TaskSettings.ControlTable.html',
        'server_name task_name table_owner table_name suspend_reason suspend_timestamp',
        table='awsdms_suspended_tables',
        role='metadata',
    ),
    _mark(
        'dms',
        'https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Tasks.CustomizingTasks.TaskSettings.ControlTable.html',
        'server_name task_name timeslot_type timeslot timeslot_duration',
        table='awsdms_history',
        role='metadata',
    ),
    # A struct column, read as its fields.
    _mark(
        'datastream',
        'https://docs.cloud.google.com/datastream/docs/destination-bigquery',
        'datastream_metadata.uuid datastream_metadata.source_timestamp',
    ),
    _mark(
        'openflow',
        'https://docs.snowflake.com/en/user-guide/data-integration/openflow/connectors/postgres/about',
        '_snowflake_inserted_at _snowflake_updated_at _snowflake_deleted',
    ),
    _mark(
        'openflow',
        'https://docs.snowflake.com/en/user-guide/data-integration/openflow/connectors/postgres/about',
        table=r'.+_journal_[0-9]+_[0-9]+',
        role='metadata',
        within='openflow',
    ),
    _mark(
        'matillion',
        'https://docs.matillion.com/data-productivity-cloud/cdc/docs/snowflake-cdc-destination/',
        'mtln_cdc_last_change_type mtln_cdc_last_commit_timestamp mtln_cdc_sequence_number',
    ),
    _mark(
        'matillion',
        'https://docs.matillion.com/data-productivity-cloud/cdc/docs/snowflake-cdc-destination/',
        'mtln_cdc_deleted',
    ),
    _mark(
        'rivery',
        'https://help.boomi.com/docs/Atomsphere/Data_Integration/Targets/data-integration-metadata-overview',
        '_rivery_last_update _rivery_run_id _rivery_river_id',
    ),
    _mark(
        'polytomic',
        'https://docs.polytomic.com/docs/polytomic-timestamp-columns',
        '_polytomic_created_at _polytomic_updated_at _polytomic_deleted_at',
    ),
    # Named by CREATE MIRROR's synced_at_col_name, which defaults to it.
    _mark('peerdb', 'https://docs.peerdb.io/sql/commands/create-mirror', '_peerdb_synced_at'),
    _mark('peerdb', 'https://docs.peerdb.io/mirror/cdc-pg-pg', '_peerdb_is_deleted'),
    _mark('sling', 'https://docs.slingdata.io/sling-cli/variables', '_sling_loaded_at'),
    _mark('sling', 'https://docs.slingdata.io/sling-cli/variables', '_sling_synced_at _sling_synced_op'),
    _mark('sling', 'https://docs.slingdata.io/sling-cli/variables', '_sling_deleted_at'),
    # Segment and RudderStack write the same tables; each event's table (tracks included) has
    # event and event_text.
    _mark('segment_spec', _SEGMENT, f'{_SEGMENT_SPEC} event event_text'),
    _mark('segment_spec', _SEGMENT, _SEGMENT_SPEC, table='identifies|pages|screens|groups|aliases'),
    _mark('segment_spec', _SEGMENT, table='users|accounts', within='segment_spec'),
    _mark('rudderstack', _RUDDERSTACK, table='rudder_discards', role='metadata', within='segment_spec'),
    _mark(
        'snowplow',
        'https://docs.snowplow.io/docs/fundamentals/canonical-event/',
        'app_id platform etl_tstamp collector_tstamp event_id v_collector v_etl domain_userid network_userid',
    ),
    _mark(
        'census',
        'https://fivetran.com/docs/activations/syncs/sync-monitoring/warehouse-writeback',
        'log_id sync_id sync_run_id _census_logged_at',
        table='sync_log',
        role='metadata',
    ),
    _mark(
        'census',
        'https://fivetran.com/docs/activations/syncs/sync-monitoring/warehouse-writeback',
        table='source_objects|destination_objects|destinations',
        role='metadata',
        within='census',
    ),
    _mark(
        'hightouch',
        'https://docs.hightouch.com/syncs/warehouse-sync-logs',
        'sync_id op_type row_id status failure_reason fields',
        table='sync_changelog|sync_snapshot',
        schema='hightouch_audit',
        role='metadata',
    ),
    _mark(
        'hightouch',
        'https://docs.hightouch.com/syncs/warehouse-sync-logs',
        'sync_id sync_run_id status started_at finished_at',
        table='sync_runs',
        schema='hightouch_audit',
        role='metadata',
    ),
)


@dataclass(frozen=True)
class Table:
    """A warehouse table as detection sees it."""

    schema: str
    """Its schema's key, with the database before it (``analytics.raw``), in lower case."""
    name: str
    """Its name, in lower case."""
    columns: frozenset[str] = field(default_factory=frozenset)
    """Its column names, in lower case."""


@dataclass(frozen=True)
class Landing:
    """The loader that wrote a table, and what the table is to it."""

    loader: str
    role: Role = 'landed'
    namespace: str | None = None
    """For a raw table, the namespace whose task it feeds."""


def _holds(mark: Mark, schema: str, table: str, columns: frozenset[str]) -> re.Match[str] | bool:
    if not mark.columns <= columns:
        return False
    if mark.at_least and len(mark.some & columns) < mark.at_least:
        return False
    if mark.schema is not None and not mark.schema.fullmatch(schema.rsplit('.', 1)[-1]):
        return False
    if mark.table is not None:
        return mark.table.fullmatch(table) or False
    return True


def _index(marks: Sequence[Mark]) -> dict[str, list[int]]:
    """For each column, the marks that require it (or count it), so a table is only checked
    against the marks its own columns could satisfy."""
    index: dict[str, list[int]] = {}
    for position, mark in enumerate(marks):
        if mark.within is not None:
            continue
        for column in mark.columns or mark.some:
            index.setdefault(column, []).append(position)
    return index


_INDEX = _index(MARKS)
_MARKED = frozenset(_INDEX)
_COMPANIONS = [m for m in MARKS if m.within is not None]


def detect(tables: Sequence[Table], declared: Mapping[int, str] | None = None) -> list[Landing | None]:
    """The loader that landed each table, or None. ``declared`` maps a table's position to a
    loader the project names for it, which refines the family the warehouse shows."""
    found: list[Landing | None] = []
    for table in tables:
        landing = None
        for position in sorted({p for c in table.columns & _MARKED for p in _INDEX[c]}):
            mark = MARKS[position]
            held = _holds(mark, table.schema, table.name, table.columns)
            if held:
                namespace = held.groupdict().get('namespace') if isinstance(held, re.Match) else None
                landing = Landing(mark.loader, mark.role, namespace)
                break
        found.append(landing)
    # Companions, which count only in a schema the loader they belong with lands, and refine it.
    present: dict[str, set[str]] = {}
    for table, landing in zip(tables, found, strict=True):
        if landing is not None:
            present.setdefault(table.schema, set()).add(landing.loader)
    refined: dict[tuple[str, str], str] = {}
    for index, table in enumerate(tables):
        if found[index] is not None or table.schema not in present:
            continue
        for mark in _COMPANIONS:
            if mark.within in present[table.schema] and _holds(mark, table.schema, table.name, table.columns):
                found[index] = Landing(mark.loader, mark.role)
                if mark.loader != mark.within:
                    refined[(table.schema, mark.within)] = mark.loader
                break
    for index, table in enumerate(tables):
        landing = found[index]
        if landing is None:
            continue
        loader = refined.get((table.schema, landing.loader), landing.loader)
        hint = (declared or {}).get(index)
        if hint is not None and loader in LOADERS.get(hint, Loader(hint, hint)).refines:
            loader = hint
        if loader != landing.loader:
            found[index] = Landing(loader, landing.role, landing.namespace)
    return found


def loader_name(declared: str) -> str:
    """The loader a project names, in the form task ids use: ``Fivetran`` is ``fivetran``, and
    ``Kafka Connect`` is ``kafka_connect``."""
    return re.sub(r'[^a-z0-9]+', '_', declared.strip().lower()).strip('_') or 'unknown'


def lands_source_data(attrs: Mapping[str, Any]) -> bool:
    """Whether a relation with these attributes holds data a loader landed from a source system,
    which is evidence for binding an ontology pack to its schema."""
    loader = attrs.get('loaded_by')
    known = LOADERS.get(str(loader)) if loader else None
    return known is not None and known.lands_sources and attrs.get('loader_role', 'landed') == 'landed'


@dataclass(frozen=True)
class LineageSchema:
    """A schema holding the Fivetran Platform Connector's lineage tables."""

    parts: tuple[str, ...]
    """The schema, with its database or catalogue before it."""
    columns: dict[str, frozenset[str]]
    """Each of its tables (lower case) with its column names (lower case)."""
    names: dict[str, str]
    """Each of its tables (lower case) as the warehouse spells it."""


@dataclass(frozen=True)
class Lineage:
    """One row of lineage: a source column, or a source table when ``source_column`` is None,
    and what it is landed as."""

    connection: str
    source_schema: str
    source_table: str
    source_column: str | None
    destination_schema: str
    destination_table: str
    destination_column: str | None
    source_type: str | None = None


def lineage_schemas(relations: Iterable[RelationMeta]) -> list[LineageSchema]:
    """The schemas among ``relations`` that hold the Fivetran lineage tables."""
    schemas: dict[tuple[str, ...], dict[str, frozenset[str]]] = {}
    names: dict[tuple[str, ...], dict[str, str]] = {}
    for relation in relations:
        parts, name = tuple(relation.parts[:-1]), relation.parts[-1]
        schemas.setdefault(parts, {})[name.lower()] = frozenset(c.name.lower() for c in relation.columns)
        names.setdefault(parts, {})[name.lower()] = name
    return [
        LineageSchema(parts, tables, names[parts])
        for parts, tables in sorted(schemas.items())
        if set(tables) >= LINEAGE_TABLES
    ]


def _table(schema: LineageSchema, name: str, dialect: str | None) -> str:
    """A table of the lineage schema, every part quoted as the warehouse spells it, so names with
    hyphens or in mixed case work."""
    return quoted([*schema.parts, schema.names.get(name, name)], dialect)


def quoted(parts: Sequence[str], dialect: str | None) -> str:
    """A table's name with every part quoted as the warehouse spells it."""
    return exp.table_(
        exp.to_identifier(parts[-1], quoted=True),
        db=exp.to_identifier(parts[-2], quoted=True) if len(parts) > 1 else None,
        catalog=exp.to_identifier(parts[-3], quoted=True) if len(parts) > 2 else None,
    ).sql(dialect=dialect)


def dlt_versions(relations: Iterable[RelationMeta]) -> list[tuple[str, ...]]:
    """The ``_dlt_version`` tables among ``relations``, each with every part of its name: dlt
    keeps there each version of the schema it loads into the dataset, with the resource that fills
    each table."""
    wanted = {'schema_name', 'version', 'schema'}
    return [
        tuple(r.parts)
        for r in relations
        if r.parts[-1].lower() == '_dlt_version' and wanted <= {c.name.lower() for c in r.columns}
    ]


def dlt_version_sql(parts: Sequence[str], dialect: str | None) -> str:
    """The query for a ``_dlt_version`` table's schemas."""
    return f'select schema_name, version, schema from {quoted(parts, dialect)}'


def _literal(value: str, dialect: str | None = None) -> str:
    return exp.Literal.string(value).sql(dialect=dialect)


def destination_sql(schema: LineageSchema, dialect: str | None) -> str:
    """The query for the Fivetran destinations the log describes, each with whether it lands the
    lineage schema itself. A destination-level Platform Connector describes only its own."""
    name = _literal(schema.parts[-1].lower(), dialect)
    return (
        f'select destination_id, max(case when lower(name) = {name} then 1 else 0 end) '
        f'from {_table(schema, "destination_schema", dialect)} group by destination_id'
    )


def destination(rows: Iterable[tuple[object, ...]]) -> tuple[str | None, int]:
    """This warehouse's destination from :func:`destination_sql`'s rows: the only one, or the only
    one that lands the lineage schema. Also returns how many were candidates."""
    found = {str(d): bool(own) for d, own in rows if d is not None}
    if len(found) == 1:
        return next(iter(found)), 1
    own = [d for d, lands in found.items() if lands]
    return (own[0] if len(own) == 1 else None), len(own) if own else len(found)


def _connection(schema: LineageSchema, dialect: str | None) -> tuple[str, str]:
    """The expression naming a source schema's connection, and the join that gives it. Before May
    2025 the CONNECTION table was CONNECTOR, and connection_id was connector_id."""
    key = 'connection_id' if 'connection_id' in schema.columns['source_schema'] else 'connector_id'
    for table, name in (('connection', 'connection_name'), ('connector', 'connector_name')):
        columns = schema.columns.get(table, frozenset())
        if key in columns and name in columns:
            return (
                f'coalesce(c.{name}, ss.{key})',
                f'left join {_table(schema, table, dialect)} c on c.{key} = ss.{key}',
            )
    return f'ss.{key}', ''


def _live(alias: str, table: str, schema: LineageSchema) -> str:
    return (
        f' and {alias}.deleted_from_source_at is null'
        if 'deleted_from_source_at' in schema.columns[table]
        else ''
    )


def lineage_sql(
    schema: LineageSchema, dialect: str | None, destination: str, landed: Iterable[str]
) -> tuple[str, str]:
    """The queries for column lineage and table lineage into the schemas named ``landed`` of
    ``destination``, in the warehouse's dialect. One Platform Connector can hold every
    destination of the account, so the query keeps only this one."""
    name, join = _connection(schema, dialect)

    def t(table: str) -> str:
        return _table(schema, table, dialect)

    schemas = sorted({s.lower() for s in landed}) or ['']
    where = (
        f'where ds.destination_id = {_literal(destination, dialect)} '
        f'and lower(ds.name) in ({", ".join(_literal(s, dialect) for s in schemas)}){_live("ss", "source_schema", schema)}'
    )
    source_type = 'sc.type' if 'type' in schema.columns['source_column'] else 'null'
    columns = (
        f'select {name}, ss.name, st.name, sc.name, ds.name, dt.name, dc.name, {source_type} '
        f'from {t("column_lineage")} cl '
        f'join {t("source_column")} sc on sc.id = cl.source_column_id{_live("sc", "source_column", schema)} '
        f'join {t("source_table")} st on st.id = sc.table_id{_live("st", "source_table", schema)} '
        f'join {t("source_schema")} ss on ss.id = st.schema_id '
        f'join {t("destination_column")} dc on dc.id = cl.destination_column_id '
        f'join {t("destination_table")} dt on dt.id = dc.table_id '
        f'join {t("destination_schema")} ds on ds.id = dt.schema_id {join} {where}'
    )
    tables = (
        f'select {name}, ss.name, st.name, ds.name, dt.name '
        f'from {t("table_lineage")} tl '
        f'join {t("source_table")} st on st.id = tl.source_table_id{_live("st", "source_table", schema)} '
        f'join {t("source_schema")} ss on ss.id = st.schema_id '
        f'join {t("destination_table")} dt on dt.id = tl.destination_table_id '
        f'join {t("destination_schema")} ds on ds.id = dt.schema_id {join} {where}'
    )
    return columns, tables


def lineage(
    column_rows: Iterable[tuple[object, ...]], table_rows: Iterable[tuple[object, ...]]
) -> list[Lineage]:
    """Lineage rows from the two queries' results, with blank names left out."""

    def text(value: object) -> str:
        return '' if value is None else str(value)

    found = [
        Lineage(
            text(c),
            text(ss),
            text(st),
            text(sc),
            text(ds),
            text(dt),
            text(dc),
            None if ty is None else str(ty),
        )
        for c, ss, st, sc, ds, dt, dc, ty in column_rows
    ]
    found += [
        Lineage(text(c), text(ss), text(st), None, text(ds), text(dt), None)
        for c, ss, st, ds, dt in table_rows
    ]
    return sorted(
        {row for row in found if row.connection and row.source_table and row.destination_table},
        key=lambda r: (
            r.connection,
            r.source_schema,
            r.source_table,
            r.source_column or '',
            r.destination_table,
        ),
    )
