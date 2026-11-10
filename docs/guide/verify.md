# How Much to Trust the Lineage

Static column lineage is a claim about SQL that is rarely checked. `konstruct verify` checks it against the data, using perturbation (the idea behind perturbation-based provenance): for every dbt model and SQL-built table in a DuckDB file, a Postgres database, or on a sample of Snowflake, BigQuery, Databricks or Trino, it copies the model's inputs, perturbs one input column at a time, reruns the model's SQL and records which output columns change.

- **Perturbations:** each input column, and each field of a struct column, is shifted (`x * a + b`, a suffix on strings, days added to dates), set to NULL, and shuffled between rows with a seeded permutation. The seed is recorded, so a run can be repeated exactly (on a remote sample, as long as the warehouse returns the same rows). The warehouse is read but never written: a model is run only when its SQL is a single query that reads, so a model with a second statement, a data-changing CTE, `SELECT INTO` or DDL is skipped with the reason, and on Postgres each statement stops after five minutes.
- **Detecting change:** an output column changes if its multiset of values changes; if values only moved between rows (a `ROW_NUMBER()` ordered by the input still yields 1..n), it is compared paired with the columns claimed not to depend on that input; and when a column identifies rows, rows are matched across runs, so changes are seen even when a perturbation also adds or removes rows.
- **Classification:** a claimed edge is *confirmed* when its output changed, or *not exercised* when no perturbation moved it on this data (genuine but masked, as with the second argument of a `COALESCE` whose first is never null). An observed influence the graph did not claim is either *via joins and filters* (the graph recorded it as a reference) or *unexplained*, a gap in the static analysis.

Results (seed 1729):

| Project | Models | Claimed column edges | Confirmed | Influences via joins and filters | Unexplained |
|---------|--------|----------------------|-----------|----------------------------------|-------------|
| `examples/jaffle_shop` | 6 | 36 | 36 (100%) | 33 | 0 |
| dbt Labs' jaffle-shop (pinned commit) | 13 | 87 | 87 (100%) | 61 | 0 |
| `examples/jaffle_shop` on Postgres (built with dbt-postgres) | 5 | 31 | 31 (100%) | not counted | 0 |
| 300 generated queries on generated data (differential test) | 300 | not counted | not counted | not counted | 0 |

The tests also make sure the verifier catches wrong lineage, not just confirms right lineage: a deleted edge is reported as unexplained, a fabricated edge as not exercised, and a deleted filter reference as an unexplained change in row count. Two further results:

- **Rendering dbt without dbt:** on jaffle-shop, lineage from konstruct's own Jinja rendering (used when there is no compiled SQL, and by `diff`) is identical to lineage from dbt's compiled SQL: 113 of 113 column edges and 9 of 9 join and filter references.
- **Airflow parsing:** the static parser reports exactly the same DAGs, tasks and dependencies as Airflow's own DagBag on every fixture DAG, including TaskGroups, TaskFlow and every dependency syntax.
- **Dagster parsing:** the static parser gives the same asset keys, dependencies, dbt selection and job selections as Dagster's own asset graph for the fixture definitions, the same op handles and op-to-op dependencies as Dagster's job graph (nested graphs and aliases included), and the same sensor and schedule targets.

## Remote Warehouses

`konstruct verify --warehouse snowflake` (or `bigquery`, `databricks`, `trino`) checks the models on a sample, using the warehouse connection in `konstruct.toml` or the dbt profile; a connection the repository's own files name needs the project to be trusted (see [Trusting a Project's Connections](../reference/configuration.md#trusting-a-projects-connections)). A remote warehouse is read only when it is named this way. For each input the run reads up to `--rows` rows once: a BigQuery table through its table listing, which is not billed as a query, and a BigQuery view or external table, a Snowflake, Databricks or Trino relation with `SELECT * ... LIMIT`, which runs on the warehouse (a BigQuery view or external table is billed as a query, and Snowflake and Databricks resume a suspended warehouse). The sample is copied into an in-memory DuckDB database in types that hold its values exactly (structured values as JSON), every model that reads the input uses the same copy, and each model's SQL is translated to DuckDB and perturbed there. Nothing is written to the warehouse.

A sample is not the whole table, and without an order the warehouse may return different rows on another run, so a remote run repeats exactly only when the warehouse returns the same rows. Inputs sampled separately may no longer join, and a model that returns no rows on its sample is reported as skipped; raise `--rows` to keep more. A model whose SQL does not translate to DuckDB, or uses a function DuckDB does not have, is skipped with the error. The report names the warehouse with `(sampled)`.

Each result is a test in the suite (`test_verify`, `test_realworld`, `test_airflow_parity`, `test_dagster`), so CI checks them on every change.
