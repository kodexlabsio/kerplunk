"""Fivetran and Airbyte: the tables they land in the warehouse, and where those tables come from.

Both tools mark the tables they write with columns of their own: ``_fivetran_synced`` for
Fivetran, ``_airbyte_raw_id`` and ``_airbyte_extracted_at`` for Airbyte (``_airbyte_ab_id`` and
``_airbyte_emitted_at`` before Destinations V2). Konstruct reads these from the warehouse
metadata it already has and groups the tables each tool writes into one loader task per schema,
so impact and trace show which tables arrive from outside. Airbyte's raw tables
(``airbyte_internal.<namespace>_raw__stream_<stream>``) feed the task of their namespace.

When a warehouse holds the Fivetran Platform Connector's lineage tables (``source_column``,
``destination_column``, ``column_lineage`` and the rest, in ``fivetran_log`` or
``fivetran_metadata``), Konstruct reads them with one query each and adds every connection's
source tables and columns, outside the warehouse, with column lineage into the tables it lands.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlglot import exp

from konstruct.inputs.warehouse import RelationMeta

FIVETRAN = 'fivetran'
AIRBYTE = 'airbyte'
_AIRBYTE_MARKERS = (
    frozenset(('_airbyte_raw_id', '_airbyte_extracted_at')),
    frozenset(('_airbyte_ab_id', '_airbyte_emitted_at')),
)
AIRBYTE_RAW_SCHEMA = 'airbyte_internal'
AIRBYTE_RAW_INFIX = '_raw__stream_'
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


def loader_of(columns: Iterable[str]) -> str | None:
    """The tool that landed a table, from the columns it adds."""
    names = {c.lower() for c in columns}
    if '_fivetran_synced' in names:
        return FIVETRAN
    if any(marker <= names for marker in _AIRBYTE_MARKERS):
        return AIRBYTE
    return None


def airbyte_raw_namespace(schema: str, table: str) -> str | None:
    """The namespace an Airbyte raw table holds the stream of, or None for another table."""
    if schema.lower() != AIRBYTE_RAW_SCHEMA or AIRBYTE_RAW_INFIX not in table.lower():
        return None
    return table.lower().split(AIRBYTE_RAW_INFIX, 1)[0] or None


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
    parts = [*schema.parts, schema.names.get(name, name)]
    return exp.table_(
        exp.to_identifier(parts[-1], quoted=True),
        db=exp.to_identifier(parts[-2], quoted=True) if len(parts) > 1 else None,
        catalog=exp.to_identifier(parts[-3], quoted=True) if len(parts) > 2 else None,
    ).sql(dialect=dialect)


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
