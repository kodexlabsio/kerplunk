"""Warehouse and catalogue metadata: DuckDB, Postgres and Iceberg REST (or any PyIceberg) catalogs.

Each connector returns `RelationMeta` records; `konstruct.engine.build` merges them into the graph.
Connections are read only. Credentials come from configuration or environment variables and
are never written to the graph.

Where a warehouse says how a column is governed, its column records it: ``warehouse_tags``, the
tags on the column by name with their values (Snowflake object tags, Databricks column tags), and
``masking_policy``, the policy that masks it (a Snowflake masking policy, a Databricks column mask,
BigQuery policy tags). :mod:`konstruct.analysis.sensitivity` reads them; reading them needs a
privilege the connection may lack, and then the columns carry none.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from konstruct.core.graph import relation_key

SYSTEM_SCHEMAS = frozenset({'information_schema', 'pg_catalog', 'pg_toast'})


@dataclass
class ColumnMeta:
    name: str
    data_type: str | None = None
    comment: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class RelationMeta:
    parts: tuple[str, ...]
    kind: str
    comment: str | None = None
    columns: list[ColumnMeta] = field(default_factory=list)
    view_sql: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return relation_key(*self.parts)

    @property
    def name(self) -> str:
        return self.parts[-1]


class Connector(Protocol):
    label: str
    dialect: str | None
    default_schema: str | None

    def fetch(self) -> list[RelationMeta]: ...


def _schemas_filter(schemas: list[str] | None) -> set[str] | None:
    return {s.lower() for s in schemas} if schemas else None


@dataclass
class DuckDBConnector:
    path: Path
    schemas: list[str] | None = None
    label: str = 'duckdb'
    dialect: str | None = 'duckdb'
    default_schema: str | None = 'main'

    def fetch(self) -> list[RelationMeta]:
        import duckdb

        wanted = _schemas_filter(self.schemas)
        con = duckdb.connect(str(self.path), read_only=True)
        try:
            database = con.execute('select current_database()').fetchone()
            current = database[0] if database else self.path.stem
            tables = con.execute(
                """
                select t.table_catalog, t.table_schema, t.table_name, t.table_type, d.comment
                from information_schema.tables t
                left join duckdb_tables() d
                  on d.database_name = t.table_catalog and d.schema_name = t.table_schema
                 and d.table_name = t.table_name
                where t.table_catalog = ?
                order by 1, 2, 3
                """,
                [current],
            ).fetchall()
            views = {
                (row[0], row[1], row[2]): (row[3], row[4])
                for row in con.execute(
                    'select database_name, schema_name, view_name, sql, comment from duckdb_views() '
                    'where not internal and database_name = ?',
                    [current],
                ).fetchall()
            }
            columns: dict[tuple[str, str, str], list[ColumnMeta]] = {}
            for catalog, schema, table, name, dtype, comment in con.execute(
                """
                select table_catalog, table_schema, table_name, column_name, data_type, column_comment
                from information_schema.columns
                where table_catalog = ?
                order by table_catalog, table_schema, table_name, ordinal_position
                """,
                [current],
            ).fetchall():
                columns.setdefault((catalog, schema, table), []).extend(
                    [ColumnMeta(name, dtype, comment), *_duckdb_struct_fields(name, dtype)]
                )
            primary_keys = {
                (row[0], row[1], row[2]): [str(c) for c in row[3]]
                for row in con.execute(
                    'select database_name, schema_name, table_name, constraint_column_names '
                    "from duckdb_constraints() where constraint_type = 'PRIMARY KEY' and database_name = ?",
                    [current],
                ).fetchall()
            }
        finally:
            con.close()

        relations = []
        for catalog, schema, table, table_type, comment in tables:
            if schema.lower() in SYSTEM_SCHEMAS or (wanted and schema.lower() not in wanted):
                continue
            view = views.get((catalog, schema, table))
            relations.append(
                RelationMeta(
                    parts=(catalog, schema, table),
                    kind='view' if table_type == 'VIEW' else 'table',
                    comment=comment or (view[1] if view else None),
                    columns=columns.get((catalog, schema, table), []),
                    view_sql=view[0] if view else None,
                    attrs=_primary_key_attrs(primary_keys.get((catalog, schema, table))),
                )
            )
        return relations


def struct_fields(name: str, dtype: str | None, dialect: str = 'duckdb') -> list[ColumnMeta]:
    """Fields of a STRUCT column, of the structs in a LIST, or of a MAP's struct values, as dotted
    columns (``address.city``, ``items.sku``), recursively. Elements and values are not named in
    the path, matching how SQL reads them (``items[1].sku``, ``unnest(items).sku``)."""
    if not dtype or not any(word in dtype.upper() for word in ('STRUCT', 'RECORD', 'ROW(')):
        return []
    from sqlglot import exp
    from sqlglot.errors import SqlglotError

    try:
        built = exp.DataType.build(dtype, dialect=dialect)
    except (SqlglotError, ValueError):
        return []
    return list(_fields_of(name, built, dialect))


def _fields_of(name: str, built: Any, dialect: str) -> Iterator[ColumnMeta]:
    from sqlglot import exp

    kind = built.this
    if kind in (exp.DataType.Type.ARRAY, exp.DataType.Type.LIST) and built.expressions:
        yield from _fields_of(name, built.expressions[0], dialect)
        return
    if kind == exp.DataType.Type.MAP and len(built.expressions) == 2:
        yield from _fields_of(name, built.expressions[1], dialect)
        return
    if kind not in (exp.DataType.Type.STRUCT, exp.DataType.Type.OBJECT):
        return
    for field_def in built.expressions:
        if not isinstance(field_def, exp.ColumnDef) or not isinstance(field_def.kind, exp.DataType):
            continue
        child = f'{name}.{field_def.name}'
        yield ColumnMeta(child, field_def.kind.sql(dialect=dialect), None, {'nested': True})
        yield from _fields_of(child, field_def.kind, dialect)


def _duckdb_struct_fields(name: str, dtype: str | None) -> list[ColumnMeta]:
    return struct_fields(name, dtype, 'duckdb')


@dataclass
class PostgresConnector:
    dsn: str
    schemas: list[str] | None = None
    label: str = 'postgres'
    dialect: str | None = 'postgres'
    default_schema: str | None = 'public'

    def fetch(self) -> list[RelationMeta]:
        import psycopg

        wanted = _schemas_filter(self.schemas)
        with psycopg.connect(self.dsn, autocommit=True) as con, con.cursor() as cur:
            cur.execute('set default_transaction_read_only = on')
            cur.execute(
                """
                select current_database(), n.nspname, c.relname,
                       case c.relkind when 'v' then 'view' when 'm' then 'view' else 'table' end,
                       obj_description(c.oid, 'pg_class'),
                       case when c.relkind in ('v', 'm') then pg_get_viewdef(c.oid, true) end,
                       c.oid, c.relkind = 'm'
                from pg_class c
                join pg_namespace n on n.oid = c.relnamespace
                where c.relkind in ('r', 'p', 'v', 'm', 'f')
                  and not c.relispartition
                  and n.nspname not in ('pg_catalog', 'information_schema', 'pg_toast')
                  and n.nspname not like 'pg_temp_%' and n.nspname not like 'pg_toast_temp_%'
                order by 2, 3
                """
            )
            tables = cur.fetchall()
            cur.execute(
                """
                select a.attrelid, a.attname, format_type(a.atttypid, a.atttypmod),
                       col_description(a.attrelid, a.attnum)
                from pg_attribute a
                join pg_class c on c.oid = a.attrelid
                join pg_namespace n on n.oid = c.relnamespace
                where a.attnum > 0 and not a.attisdropped
                  and c.relkind in ('r', 'p', 'v', 'm', 'f')
                  and n.nspname not in ('pg_catalog', 'information_schema', 'pg_toast')
                order by a.attrelid, a.attnum
                """
            )
            columns: dict[int, list[ColumnMeta]] = {}
            for oid, name, dtype, comment in cur.fetchall():
                columns.setdefault(oid, []).append(ColumnMeta(name, dtype, comment))
            cur.execute(
                """
                select con.conrelid, array_agg(a.attname order by k.ordinality)
                from pg_constraint con
                cross join lateral unnest(con.conkey) with ordinality as k(attnum, ordinality)
                join pg_attribute a on a.attrelid = con.conrelid and a.attnum = k.attnum
                where con.contype = 'p'
                group by con.conrelid
                """
            )
            primary_keys: dict[int, list[str]] = {oid: list(names) for oid, names in cur.fetchall()}

        relations = []
        for database, schema, table, kind, comment, view_sql, oid, materialised in tables:
            if wanted and schema.lower() not in wanted:
                continue
            relations.append(
                RelationMeta(
                    parts=(database, schema, table),
                    kind=kind,
                    comment=comment,
                    columns=columns.get(oid, []),
                    view_sql=f'create view {_quote_pg(schema)}.{_quote_pg(table)} as {view_sql}'
                    if view_sql
                    else None,
                    attrs={
                        **_primary_key_attrs(primary_keys.get(oid)),
                        **({'materialized_view': True} if materialised else {}),
                    },
                )
            )
        return relations


def _primary_key_attrs(columns: list[str] | None) -> dict[str, Any]:
    return {'primary_key': columns} if columns else {}


Governance = dict[tuple[str, str], dict[str, Any]]
"""Per relation key and lower-case column name, the governance attributes of the column; the
column name ``''`` holds the tags on the relation itself, which each of its columns inherits."""


def _govern(relations: list[RelationMeta], governance: Governance) -> list[RelationMeta]:
    """``relations`` with each column's tags and masking policy from ``governance``: the tags on
    the relation, then the column's own, which win where both name a tag."""
    if governance:
        for relation in relations:
            inherited = governance.get((relation.key, ''), {}).get('warehouse_tags', {})
            for column in relation.columns:
                found = governance.get((relation.key, column.name.lower()), {})
                tags = {**inherited, **found.get('warehouse_tags', {})}
                if tags:
                    column.attrs['warehouse_tags'] = tags
                if 'masking_policy' in found:
                    column.attrs['masking_policy'] = found['masking_policy']
    return relations


def _tagged(governance: Governance, rows: list[tuple[Any, ...]], *, tables: bool = False) -> None:
    """Add tag rows ``(catalog, schema, table, column, tag, value)`` to ``governance``; with
    ``tables``, a row with no column is a tag on the table."""
    for row in rows:
        if len(row) != 6 or row[4] is None or (row[3] is None and not tables):
            continue
        catalog, schema, table, column, tag, value = row
        name = '' if column is None else str(column).lower()
        found = governance.setdefault((relation_key(catalog, schema, table), name), {})
        found.setdefault('warehouse_tags', {})[str(tag)] = '' if value is None else str(value)


def _masked(governance: Governance, rows: list[tuple[Any, ...]]) -> None:
    """Add masking rows ``(catalog, schema, table, column, policy...)`` to ``governance``, the
    policy's name in one part or in several (catalogue, schema and name)."""
    for row in rows:
        if len(row) < 5:
            continue
        catalog, schema, table, column = row[:4]
        policy = '.'.join(str(part) for part in row[4:] if part)
        if column is not None and policy:
            key = (relation_key(catalog, schema, table), str(column).lower())
            governance.setdefault(key, {})['masking_policy'] = policy


def _optional_rows(cursor: Any, sql: str, what: str, notes: list[str]) -> list[tuple[Any, ...]]:
    """The rows of a metadata query a role may not be allowed to run; none, with a note of
    ``what`` was not read, when it fails."""
    try:
        cursor.execute(sql)
        return list(cursor.fetchall())
    except Exception as error:  # the role lacks the privilege, or the warehouse has no such view
        first = str(error).strip().splitlines()[0][:160] if str(error).strip() else type(error).__name__
        notes.append(f'cannot read {what}, so no column is marked by them ({first})')
        return []


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _quote_pg(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass
class IcebergConnector:
    """Any PyIceberg catalog; ``properties`` are passed to ``pyiceberg.catalog.load_catalog``.

    ``name`` is the catalogue name an engine uses to query it (for example the Trino or Spark
    catalogue name), so that SQL such as ``lake.sales.orders`` resolves to these tables.
    """

    name: str
    properties: dict[str, str]
    namespaces: list[str] | None = None
    label: str = 'iceberg'
    dialect: str | None = None
    default_schema: str | None = None

    def fetch(self) -> list[RelationMeta]:
        from pyiceberg.catalog import load_catalog

        # Konstruct reads metadata only. PyIceberg still builds a FileIO for every table it loads,
        # and its default needs pyarrow for some locations; the fsspec FileIO opens nothing
        # until a file is read, so it works for any location without extra packages.
        properties = {'py-io-impl': 'pyiceberg.io.fsspec.FsspecFileIO', **self.properties}
        catalog = load_catalog(self.name, **properties)
        relations = []
        for namespace in self._namespaces(catalog):
            for identifier in catalog.list_tables(namespace):
                table = catalog.load_table(identifier)
                schema = table.schema()
                identifier_ids = set(getattr(schema, 'identifier_field_ids', []) or [])
                columns = list(_flatten_fields(schema.fields, prefix='', identifier_ids=identifier_ids))
                table_properties = dict(table.properties or {})
                attrs: dict[str, Any] = {'format_version': table.format_version}
                spec = table.spec()
                if spec.fields:
                    attrs['partitioned_by'] = [
                        f'{field.transform}({schema.find_column_name(field.source_id)})'
                        for field in spec.fields
                    ]
                if identifier_ids:
                    attrs['identifier_fields'] = [schema.find_column_name(i) for i in sorted(identifier_ids)]
                snapshot = table.current_snapshot()
                if snapshot is not None:
                    attrs['snapshot_id'] = snapshot.snapshot_id
                relations.append(
                    RelationMeta(
                        parts=(self.name, *identifier),
                        kind='table',
                        comment=table_properties.get('comment'),
                        columns=columns,
                        attrs=attrs,
                    )
                )
        return relations

    def _namespaces(self, catalog: Any) -> Iterator[tuple[str, ...]]:
        if self.namespaces:
            for configured in self.namespaces:
                yield tuple(configured.split('.'))
            return
        pending: list[tuple[str, ...]] = list(catalog.list_namespaces())
        seen: set[tuple[str, ...]] = set()
        while pending:
            namespace = tuple(pending.pop(0))
            if namespace in seen:
                continue
            seen.add(namespace)
            yield namespace
            try:
                children = catalog.list_namespaces(namespace)
            except Exception:
                children = []
            pending.extend(tuple(child) for child in children if tuple(child) != namespace)


def _flatten_fields(fields: Any, prefix: str, identifier_ids: set[int]) -> Iterator[ColumnMeta]:
    """Top-level Iceberg columns, plus nested struct fields as dotted names (``address.city``)."""
    from pyiceberg.types import ListType, MapType, StructType

    for item in fields:
        name = f'{prefix}{item.name}'
        attrs: dict[str, Any] = {'field_id': item.field_id}
        if item.required:
            attrs['required'] = True
        if item.field_id in identifier_ids:
            attrs['identifier'] = True
        if prefix:
            attrs['nested'] = True
        yield ColumnMeta(name, str(item.field_type), item.doc, attrs)
        field_type = item.field_type
        if isinstance(field_type, StructType):
            yield from _flatten_fields(field_type.fields, f'{name}.', identifier_ids)
        elif isinstance(field_type, ListType) and isinstance(field_type.element_type, StructType):
            yield from _flatten_fields(field_type.element_type.fields, f'{name}.', identifier_ids)
        elif isinstance(field_type, MapType) and isinstance(field_type.value_type, StructType):
            yield from _flatten_fields(field_type.value_type.fields, f'{name}.', identifier_ids)


class MissingDriver(Exception):
    """A warehouse needs an optional extra: pip install 'konstruct[snowflake]' and so on."""


def driver(module: str, extra: str) -> Any:
    """A warehouse's driver module, imported when first needed, or :class:`MissingDriver` naming
    the extra that installs it."""
    import importlib

    try:
        return importlib.import_module(module)
    except ImportError as error:
        raise MissingDriver(f"install the {extra} extra: uv tool install 'konstruct[{extra}]'") from error


def _information_schema(
    cursor: Any,
    catalog: str,
    schemas: set[str] | None,
    dialect: str,
    column_type: str = 'data_type',
) -> list[RelationMeta]:
    """Tables, views, columns and comments from one catalogue's information_schema."""

    def rows(sql: str) -> list[tuple[Any, ...]]:
        cursor.execute(sql)
        return list(cursor.fetchall())

    prefix = f'{catalog}.information_schema' if catalog else 'information_schema'
    tables = rows(
        f'select table_catalog, table_schema, table_name, table_type, comment from {prefix}.tables '
        "where lower(table_schema) <> 'information_schema' order by 1, 2, 3"
    )
    columns: dict[tuple[str, str, str], list[ColumnMeta]] = {}
    for table_catalog, schema, table, name, dtype, comment, _ in rows(
        f'select table_catalog, table_schema, table_name, column_name, {column_type}, comment, ordinal_position '
        f"from {prefix}.columns where lower(table_schema) <> 'information_schema' order by 1, 2, 3, 7"
    ):
        key = (str(table_catalog), str(schema), str(table))
        columns.setdefault(key, []).extend(
            [ColumnMeta(str(name), dtype, comment), *struct_fields(str(name), dtype, dialect)]
        )
    views = {
        (str(c), str(s), str(t)): definition
        for c, s, t, definition in rows(
            f'select table_catalog, table_schema, table_name, view_definition from {prefix}.views '
            "where lower(table_schema) <> 'information_schema'"
        )
    }
    listed = {(str(c), str(s), str(t)) for c, s, t, _, _ in tables}
    # Some catalogues list views only in information_schema.views.
    tables += [(c, s, t, 'VIEW', None) for (c, s, t) in views if (c, s, t) not in listed]
    relations = []
    for table_catalog, schema, table, table_type, comment in tables:
        key = (str(table_catalog), str(schema), str(table))
        if schemas and str(schema).lower() not in schemas:
            continue
        view = views.get(key)
        relations.append(
            RelationMeta(
                parts=key,
                kind='view' if 'VIEW' in str(table_type).upper() else 'table',
                comment=comment or None,
                columns=columns.get(key, []),
                view_sql=_view_statement(key, view),
                attrs=_materialised(table_type),
            )
        )
    return relations


def _materialised(table_type: Any) -> dict[str, Any]:
    """``materialized_view`` for a view that holds rows of its own, as a table does."""
    return {'materialized_view': True} if 'MATERIALIZED' in str(table_type or '').upper() else {}


def _view_statement(parts: tuple[str, ...], definition: Any) -> str | None:
    """A CREATE VIEW statement: Snowflake stores the whole statement, others only the query."""
    if not definition:
        return None
    text = str(definition).strip().rstrip(';')
    if text.lower().startswith('create'):
        return text
    return f'create view {".".join(parts)} as {text}'


@dataclass
class SnowflakeConnector:
    """Snowflake through snowflake-connector-python (the ``snowflake`` extra). ``options`` are passed
    to ``snowflake.connector.connect``; ``databases`` defaults to the connection's database."""

    options: dict[str, Any]
    databases: list[str] | None = None
    schemas: list[str] | None = None
    label: str = 'snowflake'
    dialect: str | None = 'snowflake'
    default_schema: str | None = None
    notes: list[str] = field(default_factory=list)
    """What the last fetch could not read, such as the tags a role may not see."""

    def fetch(self) -> list[RelationMeta]:
        connector = driver('snowflake.connector', 'snowflake')
        connection = connector.connect(**self.options)
        try:
            cursor = connection.cursor()
            databases = self.databases or [str(self.options.get('database') or '')]
            relations = []
            governance: Governance = {}
            for database in databases:
                relations += _information_schema(cursor, database, _schemas_filter(self.schemas), 'snowflake')
                governance.update(_snowflake_governance(cursor, database, self.notes))
            if relations:
                self.default_schema = str(self.options.get('schema') or 'public')
            return _govern(relations, governance)
        finally:
            connection.close()


def _snowflake_governance(cursor: Any, database: str, notes: list[str] | None = None) -> Governance:
    """The tags and masking policies on one database's columns, and the tags on its tables, which
    their columns inherit, from the account usage views. They need the role to have been granted
    the SNOWFLAKE database's imported privileges, and lag the account by up to two hours. Tags a
    column inherits from its schema or database are not read."""
    notes = notes if notes is not None else []
    where = f'upper(object_database) = upper({_literal(database)})' if database else 'true'
    governance: Governance = {}
    _tagged(
        governance,
        _optional_rows(
            cursor,
            'select object_database, object_schema, object_name, column_name, tag_name, tag_value '
            f"from snowflake.account_usage.tag_references where domain in ('COLUMN', 'TABLE') and {where} "
            'and object_deleted is null',
            'the tags in snowflake.account_usage.tag_references',
            notes,
        ),
        tables=True,
    )
    ref = where.replace('object_database', 'ref_database_name')
    _masked(
        governance,
        _optional_rows(
            cursor,
            'select ref_database_name, ref_schema_name, ref_entity_name, ref_column_name, policy_name '
            "from snowflake.account_usage.policy_references where policy_kind = 'MASKING_POLICY' "
            f'and ref_column_name is not null and {ref}',
            'the masking policies in snowflake.account_usage.policy_references',
            notes,
        ),
    )
    return governance


@dataclass
class DatabricksConnector:
    """Databricks SQL warehouses through databricks-sql-connector (the ``databricks`` extra), reading
    Unity Catalog's information_schema for each catalogue."""

    options: dict[str, Any]
    catalogs: list[str] | None = None
    schemas: list[str] | None = None
    label: str = 'databricks'
    dialect: str | None = 'databricks'
    default_schema: str | None = None
    notes: list[str] = field(default_factory=list)
    """What the last fetch could not read, such as the column tags a role may not see."""

    def fetch(self) -> list[RelationMeta]:
        sql = driver('databricks.sql', 'databricks')
        connection = sql.connect(**self.options)
        try:
            cursor = connection.cursor()
            catalogs = self.catalogs or [str(self.options.get('catalog') or 'main')]
            relations = []
            governance: Governance = {}
            for catalog in catalogs:
                relations += _information_schema(
                    cursor, catalog, _schemas_filter(self.schemas), 'databricks', column_type='full_data_type'
                )
                schema = f'{catalog}.information_schema'
                _tagged(
                    governance,
                    _optional_rows(
                        cursor,
                        'select catalog_name, schema_name, table_name, column_name, tag_name, tag_value '
                        f'from {schema}.column_tags',
                        f'the column tags in {schema}.column_tags',
                        self.notes,
                    ),
                )
                _masked(
                    governance,
                    _optional_rows(
                        cursor,
                        'select catalog_name, schema_name, table_name, column_name, mask_catalog, '
                        f'mask_schema, mask_name from {schema}.column_masks',
                        f'the column masks in {schema}.column_masks',
                        self.notes,
                    ),
                )
            if relations:
                self.default_schema = str(self.options.get('schema') or 'default')
            return _govern(relations, governance)
        finally:
            connection.close()


@dataclass
class TrinoConnector:
    """Trino (and Starburst) through the ``trino`` client (the ``trino`` extra), reading each
    catalogue's information_schema and the table comments in ``system.metadata``. ``options`` are
    passed to ``trino.dbapi.connect``; ``catalogs`` defaults to the connection's catalogue."""

    options: dict[str, Any]
    catalogs: list[str] | None = None
    schemas: list[str] | None = None
    password: str | None = None
    """For password (LDAP) authentication, with ``options['user']``."""
    token: str | None = None
    """For JWT authentication."""
    label: str = 'trino'
    dialect: str | None = 'trino'
    default_schema: str | None = None

    def connect(self) -> Any:
        trino = driver('trino', 'trino')
        options = dict(self.options)
        if self.token:
            options['auth'] = driver('trino.auth', 'trino').JWTAuthentication(self.token)
        elif self.password:
            options['auth'] = driver('trino.auth', 'trino').BasicAuthentication(
                str(options.get('user', '')), self.password
            )
        if 'auth' in options:
            options.setdefault('http_scheme', 'https')
        return trino.dbapi.connect(**options)

    def fetch(self) -> list[RelationMeta]:
        connection = self.connect()
        try:
            cursor = connection.cursor()
            catalogs = [c.lower() for c in self.catalogs or [str(self.options.get('catalog') or '')] if c]
            if not catalogs:
                raise ValueError('no catalogue to read: set catalog or catalogs')
            relations = []
            for catalog in catalogs:
                relations += _trino_catalog(cursor, catalog, _schemas_filter(self.schemas))
            if relations:
                self.default_schema = str(self.options.get('schema') or 'default')
            return relations
        finally:
            connection.close()


def _trino_catalog(cursor: Any, catalog: str, schemas: set[str] | None) -> list[RelationMeta]:
    """One Trino catalogue: tables, views and columns (with their comments) from its
    information_schema, and table comments from ``system.metadata``."""

    def rows(sql: str) -> list[tuple[Any, ...]]:
        cursor.execute(sql)
        return list(cursor.fetchall())

    name = '"' + catalog.replace('"', '""') + '"'
    literal = "'" + catalog.replace("'", "''") + "'"
    skip = "table_schema <> 'information_schema'"
    wanted = ''
    if schemas:
        wanted = ', '.join("'" + s.replace("'", "''") + "'" for s in sorted(schemas))
        skip += f' and table_schema in ({wanted})'
    tables = rows(
        f'select table_schema, table_name, table_type from {name}.information_schema.tables '
        f'where {skip} order by 1, 2'
    )
    # comment is a hidden column of Trino's information_schema.columns, so it is named.
    column_rows = rows(
        f'select table_schema, table_name, column_name, data_type, comment, ordinal_position '
        f'from {name}.information_schema.columns where {skip} order by 1, 2, 6'
    )
    columns: dict[tuple[str, str], list[ColumnMeta]] = {}
    for schema, table, column, dtype, comment, _ in column_rows:
        columns.setdefault((str(schema), str(table)), []).extend(
            [ColumnMeta(str(column), dtype, comment or None), *struct_fields(str(column), dtype, 'trino')]
        )
    views = {
        (str(schema), str(table)): definition
        for schema, table, definition in rows(
            f'select table_schema, table_name, view_definition from {name}.information_schema.views '
            f'where {skip}'
        )
    }
    try:
        comments = {
            (str(schema), str(table)): comment
            for schema, table, comment in rows(
                'select schema_name, table_name, comment from system.metadata.table_comments '
                f'where catalog_name = {literal} and comment is not null'
                + (f' and schema_name in ({wanted})' if wanted else '')
            )
        }
    except Exception:  # system.metadata is not readable for every user
        comments = {}
    relations = []
    for schema, table, table_type in tables:
        key = (str(schema), str(table))
        if schemas and key[0].lower() not in schemas:
            continue
        parts = (catalog, *key)
        relations.append(
            RelationMeta(
                parts=parts,
                kind='view' if 'VIEW' in str(table_type).upper() else 'table',
                attrs=_materialised(table_type),
                comment=comments.get(key) or None,
                columns=columns.get(key, []),
                view_sql=_view_statement(parts, views.get(key)),
            )
        )
    return relations


@dataclass
class BigQueryConnector:
    """BigQuery through google-cloud-bigquery (the ``bigquery`` extra), using the client API, which
    also returns nested RECORD fields and their descriptions. ``endpoint`` points it at an emulator."""

    project: str
    datasets: list[str] | None = None
    location: str | None = None
    endpoint: str | None = None
    label: str = 'bigquery'
    dialect: str | None = 'bigquery'
    default_schema: str | None = None

    def client(self) -> Any:
        bigquery = driver('google.cloud.bigquery', 'bigquery')
        if self.endpoint:
            from google.api_core.client_options import ClientOptions
            from google.auth.credentials import AnonymousCredentials

            return bigquery.Client(
                project=self.project,
                credentials=AnonymousCredentials(),  # type: ignore[no-untyped-call]
                client_options=ClientOptions(api_endpoint=self.endpoint),
            )
        return bigquery.Client(project=self.project, location=self.location)

    def fetch(self) -> list[RelationMeta]:
        client = self.client()
        wanted = _schemas_filter(self.datasets)
        relations = []
        for dataset in client.list_datasets(project=self.project):
            if wanted and dataset.dataset_id.lower() not in wanted:
                continue
            for item in client.list_tables(dataset.reference):
                table = client.get_table(item.reference)
                kind = 'view' if table.table_type in ('VIEW', 'MATERIALIZED_VIEW') else 'table'
                materialised = _materialised(table.table_type)
                parts = (table.project, table.dataset_id, table.table_id)
                relations.append(
                    RelationMeta(
                        parts=parts,
                        kind=kind,
                        comment=table.description or None,
                        columns=list(_bigquery_fields(table.schema, '')),
                        view_sql=f'create view `{".".join(parts)}` as {table.view_query}'
                        if table.view_query
                        else None,
                        attrs=materialised,
                    )
                )
        return relations


def _bigquery_fields(fields: Any, prefix: str) -> Iterator[ColumnMeta]:
    for field_def in fields:
        name = f'{prefix}{field_def.name}'
        dtype = field_def.field_type + ('[]' if field_def.mode == 'REPEATED' else '')
        attrs: dict[str, Any] = {'nested': True} if prefix else {}
        if field_def.mode == 'REQUIRED':
            attrs['required'] = True
        policy_tags = getattr(field_def, 'policy_tags', None)
        names = [str(n) for n in getattr(policy_tags, 'names', None) or []]
        if names:
            # Column-level access control: readers without the fine-grained reader role see the
            # column masked, or not at all.
            attrs['masking_policy'] = ', '.join(names)
        yield ColumnMeta(name, dtype, field_def.description or None, attrs)
        if field_def.field_type in ('RECORD', 'STRUCT') and field_def.fields:
            yield from _bigquery_fields(field_def.fields, f'{name}.')


def query(connector: Any, sql: str) -> list[tuple[Any, ...]] | None:
    """The rows of a read-only query against a connector's warehouse, or None for a connector
    that cannot run SQL (Iceberg)."""
    if isinstance(connector, DuckDBConnector):
        import duckdb

        database = duckdb.connect(str(connector.path), read_only=True)
        try:
            return [tuple(r) for r in database.execute(sql).fetchall()]
        finally:
            database.close()
    if isinstance(connector, PostgresConnector):
        import psycopg

        with psycopg.connect(connector.dsn, autocommit=True) as con, con.cursor() as cur:
            cur.execute('set default_transaction_read_only = on')
            cur.execute(sql)
            return [tuple(r) for r in cur.fetchall()]
    if isinstance(connector, BigQueryConnector):
        return [tuple(r.values()) for r in connector.client().query(sql).result()]
    if isinstance(connector, TrinoConnector):
        connection = connector.connect()
    elif isinstance(connector, SnowflakeConnector):
        connection = driver('snowflake.connector', 'snowflake').connect(**connector.options)
    elif isinstance(connector, DatabricksConnector):
        connection = driver('databricks.sql', 'databricks').connect(**connector.options)
    else:
        return None
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        return [tuple(r) for r in cursor.fetchall()]
    finally:
        connection.close()
