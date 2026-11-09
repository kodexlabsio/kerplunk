"""Column usage from warehouse query history: which columns people and dashboards actually read.

REPORT.md's most used columns come from the graph's structure. Query history says what is
queried. Usage is off by default; ``[usage] enabled = true`` in konstruct.toml turns it on for every
Snowflake, BigQuery and Databricks warehouse in the build. It reads metadata only, aggregated to a
count of queries and distinct users per column over the last ``days`` days.

Sources, as documented by each vendor:

* **Snowflake**: ``SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY``, Enterprise Edition or higher. Each
  query lists the objects it read with their columns. Konstruct counts ``DIRECT_OBJECTS_ACCESSED``
  (what the query named, views included) by default, or ``BASE_OBJECTS_ACCESSED`` (the underlying
  tables, including columns used only in filters and joins) with ``objects = "base"``. The role
  needs the ``SNOWFLAKE.GOVERNANCE_VIEWER`` database role or ``IMPORTED PRIVILEGES`` on the
  SNOWFLAKE database. Latency up to three hours; one year of history.
* **BigQuery**: ``region-REGION.INFORMATION_SCHEMA.JOBS`` for the project. It records the tables a
  job referenced but no columns, so Konstruct parses each distinct query text with its own SQL
  analysis and counts the columns it reads, including those used in joins and filters. Needs
  ``bigquery.jobs.listAll`` (BigQuery Resource Viewer). 180 days of history.
* **Databricks**: ``system.access.column_lineage``, which records every column a query reads
  (reads with no target included), joined to nothing else. Needs ``USE CATALOG`` on ``system``,
  ``USE SCHEMA`` and ``SELECT`` on ``system.access``. 395 days of history.
"""

from __future__ import annotations

import contextlib
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from konstruct.inputs.warehouse import (
    BigQueryConnector,
    DatabricksConnector,
    MissingDriver,
    SnowflakeConnector,
    _driver,
)

__all__ = ['ColumnUsage', 'MissingDriver', 'UsageSettings', 'fetch']

SUPPORTED = ('snowflake', 'bigquery', 'databricks')
SNOWFLAKE_DOMAINS = ('Table', 'View', 'Materialized view', 'External table', 'Dynamic table', 'Iceberg table')


@dataclass
class UsageSettings:
    enabled: bool = False
    days: int = 90
    exclude_users: list[str] = field(default_factory=list)
    """Service accounts to leave out, such as the one dbt and the orchestrator run as."""
    objects: str = 'direct'
    """For Snowflake, count ``direct`` objects (as queried) or ``base`` objects (underlying tables)."""
    max_queries: int = 50_000
    """For BigQuery, the most distinct (query text, user) pairs parsed."""


@dataclass
class ColumnUsage:
    relation: str
    column: str
    queries: int
    users: int


def _literals(values: Iterable[str]) -> str:
    return ', '.join("'" + v.replace("'", "''") + "'" for v in values)


def snowflake_sql(settings: UsageSettings) -> str:
    source = 'base_objects_accessed' if settings.objects == 'base' else 'direct_objects_accessed'
    exclude = (
        f'\n  and upper(ah.user_name) not in ({_literals(u.upper() for u in settings.exclude_users)})'
        if settings.exclude_users
        else ''
    )
    return (
        'select f.value:"objectName"::string as object_name, c.value:"columnName"::string as column_name, '
        'count(distinct ah.query_id) as queries, count(distinct ah.user_name) as users\n'
        f'from snowflake.account_usage.access_history ah,\n'
        f'  lateral flatten(input => ah.{source}) f,\n'
        '  lateral flatten(input => f.value:"columns") c\n'
        f'where ah.query_start_time >= dateadd(day, -{int(settings.days)}, current_timestamp())\n'
        f'  and f.value:"objectDomain"::string in ({_literals(SNOWFLAKE_DOMAINS)}){exclude}\n'
        'group by 1, 2'
    )


def databricks_sql(settings: UsageSettings) -> str:
    exclude = (
        f'\n  and created_by not in ({_literals(settings.exclude_users)})' if settings.exclude_users else ''
    )
    return (
        'select source_table_full_name, source_column_name, '
        'count(distinct coalesce(statement_id, event_id)) as queries, count(distinct created_by) as users\n'
        'from system.access.column_lineage\n'
        f'where event_date >= date_sub(current_date(), {int(settings.days)})\n'
        '  and source_table_full_name is not null and source_column_name is not null'
        f'{exclude}\n'
        'group by 1, 2'
    )


def bigquery_sql(project: str, region: str, settings: UsageSettings) -> str:
    exclude = (
        f'\n  and user_email not in ({_literals(settings.exclude_users)})' if settings.exclude_users else ''
    )
    return (
        'select query, user_email, count(*) as runs\n'
        f'from `{project}`.`region-{region.lower()}`.INFORMATION_SCHEMA.JOBS\n'
        f'where creation_time >= timestamp_sub(current_timestamp(), interval {int(settings.days)} day)\n'
        "  and job_type = 'QUERY' and statement_type <> 'SCRIPT' and state = 'DONE' and error_result is null"
        f'{exclude}\n'
        'group by query, user_email\n'
        'order by runs desc\n'
        f'limit {int(settings.max_queries)}'
    )


def _cursor_rows(connection: Any, sql: str) -> list[tuple[Any, ...]]:
    cursor = connection.cursor()
    cursor.execute(sql)
    return [tuple(row) for row in cursor.fetchall()]


def snowflake_usage(connector: SnowflakeConnector, settings: UsageSettings) -> list[ColumnUsage]:
    module = _driver('snowflake.connector', 'snowflake')
    connection = module.connect(**connector.options)
    try:
        rows = _cursor_rows(connection, snowflake_sql(settings))
    finally:
        connection.close()
    return [ColumnUsage(str(o), str(c), int(q), int(u)) for o, c, q, u in rows if o and c]


def databricks_usage(connector: DatabricksConnector, settings: UsageSettings) -> list[ColumnUsage]:
    module = _driver('databricks.sql', 'databricks')
    connection = module.connect(**connector.options)
    try:
        rows = _cursor_rows(connection, databricks_sql(settings))
    finally:
        connection.close()
    return [ColumnUsage(str(t), str(c), int(q), int(u)) for t, c, q, u in rows if t and c]


Parser = Callable[[str], list[tuple[str, str]]]


def bigquery_usage(connector: BigQueryConnector, settings: UsageSettings, parse: Parser) -> list[ColumnUsage]:
    """Distinct query texts with their users and runs, parsed for the columns each reads."""
    client = connector.client()
    region = connector.location or 'us'
    rows = client.query(bigquery_sql(connector.project, region, settings)).result()
    return aggregate_queries(((r['query'], r['user_email'], int(r['runs'])) for r in rows), parse)


def aggregate_queries(rows: Iterable[tuple[str, str | None, int]], parse: Parser) -> list[ColumnUsage]:
    """Count runs and distinct users per column, parsing each distinct query text once."""
    parsed: dict[str, list[tuple[str, str]]] = {}
    runs: dict[tuple[str, str], int] = defaultdict(int)
    users: dict[tuple[str, str], set[str]] = defaultdict(set)
    for query, user, count in rows:
        if not query:
            continue
        if query not in parsed:
            columns: list[tuple[str, str]] = []
            with contextlib.suppress(Exception):  # unparseable text is skipped, not fatal
                columns = parse(query)
            parsed[query] = sorted(set(columns))
        for key in parsed[query]:
            runs[key] += count
            if user:
                users[key].add(user)
    return [ColumnUsage(r, c, runs[(r, c)], len(users[(r, c)])) for r, c in sorted(runs)]


def fetch(connector: Any, settings: UsageSettings, parse: Parser) -> list[ColumnUsage]:
    """Usage counts per column from the query history of a Snowflake, BigQuery or Databricks connector; empty for any other connector."""
    if isinstance(connector, SnowflakeConnector):
        return snowflake_usage(connector, settings)
    if isinstance(connector, DatabricksConnector):
        return databricks_usage(connector, settings)
    if isinstance(connector, BigQueryConnector):
        return bigquery_usage(connector, settings, parse)
    return []


SOURCES = {
    'snowflake': 'Snowflake access history',
    'bigquery': 'BigQuery job history',
    'databricks': 'Databricks column lineage',
}
