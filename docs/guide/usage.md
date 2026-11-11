# Column Usage from Query History

The graph knows what depends on what; it does not know what people actually read. With usage on, Konstruct reads the warehouse's query history, counts the queries and distinct users that read each column over a window, and adds that to the graph: a usage score on each column (0 to 100 on a log scale), a "Most queried columns" table in `REPORT.md`, a line in `describe`, and a tag in graph.html. It reads metadata only, once a day, and nothing is sent anywhere.

```toml
[usage]
enabled = true                  # off by default
days = 90
exclude_users = ["DBT_SERVICE"] # leave out pipeline and orchestrator accounts
# objects = "base"              # Snowflake: count underlying tables rather than what queries named
```

| Warehouse | Source | Columns from | History | Access needed |
|-----------|--------|--------------|---------|---------------|
| Snowflake | [`SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY`](https://docs.snowflake.com/en/sql-reference/account-usage/access_history) | The columns recorded for each object the query named (`DIRECT_OBJECTS_ACCESSED`), or with `objects = "base"` the underlying tables' columns, including those used only in filters and joins | 365 days; up to 3 hours behind; Enterprise Edition or higher | The `SNOWFLAKE.GOVERNANCE_VIEWER` database role, or `IMPORTED PRIVILEGES` on the SNOWFLAKE database ([access](https://docs.snowflake.com/en/sql-reference/account-usage)) |
| BigQuery | [`region-REGION.INFORMATION_SCHEMA.JOBS`](https://cloud.google.com/bigquery/docs/information-schema-jobs) | The view records tables but not columns, so Konstruct parses each distinct query text with its own SQL analysis, counting output columns' sources and the columns used in joins, filters and grouping | 180 days | `bigquery.jobs.listAll` on the project (BigQuery Resource Viewer) |
| Databricks | [`system.access.column_lineage`](https://docs.databricks.com/aws/en/admin/system-tables/lineage) | Every column a query read, including reads with no target table | 395 days | `USE CATALOG` on `system`, `USE SCHEMA` and `SELECT` on `system.access` ([system tables](https://docs.databricks.com/aws/en/admin/system-tables/)) |

Without the access, the build carries on and warns `cannot read query history` with the warehouse's message. BigQuery takes the region from the warehouse's `location` (default `us`).

## Shadow Metrics

`konstruct check-sql --history` reads the texts of the queries people ran, and checks each as `konstruct check-sql` checks one (see [Checking the SQL an Agent Writes](agents.md#checking-the-sql-an-agent-writes)). A query that computes a measure differently from its definition, by leaving out a required filter or computing it another way, is a shadow metric. The result groups them by measure and difference, approved measures first, each with how many distinct queries, runs and people compute it that way, the fix, and the query run most often as an example; it also counts how often each term is read through its own columns.

```bash
konstruct check-sql --history --days 30 --pseudonymise
```

| Warehouse | Query texts from |
|-----------|------------------|
| Snowflake | `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY`: successful `SELECT` queries |
| BigQuery | `region-REGION.INFORMATION_SCHEMA.JOBS`: query jobs that finished without an error |
| Databricks | `system.query.history`: finished `SELECT` statements |

It exits 1 with `--strict` when a shadow metric computes an approved term. It reads `--days` days (by default `[usage] days`) and at most `-n/--limit` distinct query texts, 5,000 by default, those run most often first, leaving out the users `[usage] exclude_users` names. `[usage] enabled` is not needed. It reads history metadata only, over the project's own warehouse connections, so the project must be trusted (`konstruct trust`). Databricks shows `<REDACTED>` in place of a statement's text to anyone but account admins and members of the account-level `databricks_pii_access` group; those runs are counted and reported, not checked.

`--pseudonymise` counts people without naming them, under a pseudonym made with a new random key for each run, so two reports cannot be matched, and leaves the literals out of the example queries, since a literal can identify a customer or a person. The `check_history` MCP tool always does both.
