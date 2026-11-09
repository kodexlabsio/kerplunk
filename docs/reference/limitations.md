# Limitations

- Lineage is static. SQL built at runtime, Jinja that dbt has not compiled, Python models, stored procedures and UDF internals are not followed; each gap is listed under Warnings in `REPORT.md`.
- Without compiled SQL, macros that query the warehouse while compiling (`run_query`, introspective `dbt_utils.get_column_values`) render as empty results, so models that depend on them may differ from dbt's compiled SQL.
- `SELECT *` over a relation whose columns Konstruct cannot learn (no warehouse, no catalog) still gets lineage for the columns the query goes on to use, and is reported as a warning; columns passed straight through by the star to the output are not known.
- Dagster ops compose statically only: a graph body built in a loop is counted once (with a warning), ops created by factories are not seen, and sensors that choose a job at run time (`RunRequest(job_name=...)` without `job=`) are not linked. Op inputs and outputs are not tracked as data; lineage comes from the SQL an op runs.
- Airflow parsing covers the common ways of writing DAGs. Task ids or SQL built in loops or from runtime values, dynamic task mapping over computed lists, and DAG factories are skipped with a warning. Selections by `--selector`, `state:`, `result:` and `source_status:` are not resolved.
- Identifiers are matched case-insensitively. Two relations whose names differ only by case are treated as one.
- `query` ranks by words, not meaning. It finds `customer_lifetime_value` from "lifetime value" but not from "how much a customer is worth"; the agent is expected to follow up with `search` or `describe`.
- Map keys are read at run time, so `attrs['colour']` on a map traces to the `attrs` column, not to a key. Positional `UNNEST ... AS t(a, b)` over rows, without field names, traces to the list column.
- `verify` runs against a local DuckDB file or a Postgres database (in a transaction that is rolled back); other warehouses are not supported yet. It perturbs columns one at a time, so influences that need two inputs to change together are not observed.
- The ontology names entities from their key columns and relations by convention (`customer_id`, `stg_customers.id`, `stg_stripe__payments.id`); a term of kind `entity` names one that convention gets wrong. A relationship from an entity to itself, such as a customer's referrer, is not recorded, and a relationship's evidence lists every test and join between columns of the same names.
- With several repositories, `SELECT *` over a relation another repository builds is not expanded, since each repository's SQL is analysed on its own; name the columns, or let the warehouse supply them, to get their lineage.
- `diff` reads dbt YAML for tests, descriptions, sources and exposures, but not for other configuration (materialisation, contracts, meta).

## Not Yet (v1)

- Column-level lineage for dbt Python models and through UDFs.
- `verify` against sampled remote warehouses (Snowflake, BigQuery, Databricks).
- Incremental warehouse metadata (it is re-read on every build; it is fast for local warehouses but not for large remote ones).
