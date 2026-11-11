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

## Mutation Testing

A test suite that passes says little until it is known to fail when a model is wrong. `konstruct verify --mutate` makes small changes to each dbt model's compiled SQL, of the kind a real bug makes, and checks whether the model's dbt tests notice:

- **Mutants:** an aggregate swapped (`SUM` to `MAX`, `COUNT(DISTINCT x)` to `COUNT(x)` and back), a `WHERE`, `HAVING` or join condition dropped or negated, an inner join made a left join and back, a comparison boundary moved (`>` to `>=`) or a comparison flipped (`=` to `<>`, `<` to `>`), a date literal moved a day later, a `COALESCE` removed, or a `CASE` branch or default dropped. Up to `--max-mutants` mutants (30 by default) are drawn per model with `--seed`, from each kind in turn, so a run repeats exactly; `--operators` chooses the kinds.
- **Running:** each mutant runs in the same read-only engines as `verify`, on up to `--rows` rows of each input (1000 by default), and then every dbt data test that reads the model, compiled from `manifest.json`, runs against the mutant's output. The model's dbt unit tests run too, each on its own given rows, comparing its expected rows as dbt does. A model's SQL, and each test's, runs only when it is a single query that reads.
- **Verdicts:** a mutant is *killed* when a test fails on it, and *survived* when its output differs from the original's but every test passes. It is *equivalent* when it is the same query as the original in canonical form, so no test could kill it, and *not distinguishable on this data* when its output is the same on the sample although the query differs: a gap in the sample rather than in the tests. A mutant that does not run, or does not give the same rows twice, is *invalid*.
- **Score:** killed over killed and survived, per model and per output column; the equivalent, indistinguishable and invalid mutants are left out. A test that already fails on the original is reported and not used, and a model none of whose tests can run is skipped, with a reminder to run `dbt compile`; a model with no tests at all scores 0%.
- **Suggestions:** for each survivor, a test that passes on the original output and fails on the mutant's: `not_null`, `unique`, `accepted_values`, `relationships` (to a key from the derived ontology or the column's lineage) or `dbt_utils.expression_is_true`, as YAML to add to the column's or the model's `data_tests`. Where none does, a dbt unit test built from the fewest sample rows on which the two disagree. `--no-suggest` leaves them out.

The run writes `MUTATION.md` and `mutation.json` to `konstruct-out/`; `--markdown FILE` writes the report to another file as well, and `--json` prints the result. Survivors are listed with the most downstream reach first, since those are the changes that would break the most.

## The Rows Behind a Value

Column lineage says which columns a value is computed from; `konstruct verify --why` says which rows. `konstruct verify --why customer_ltv.lifetime_value --where customer_id=3` lists every source row that contributes to that one value, with its share, and every related source row that a filter or a join left out, with the reason:

- **The value:** `--where` names output columns of the column's model and the values that pick one row (repeat it for a key of several columns). The model's SQL is composed down to source tables, every model it reads inlined, and run read-only in the same engines as `verify`.
- **Shares:** exact for `SUM` (each addend), `COUNT` (one each, none for a null), `AVG` (each addend and the count) and `MIN` and `MAX` (the row that won, marked `*`). `MEDIAN`, `DISTINCT` aggregates and other aggregates are not decomposable, so each input value is listed instead. Where the value is the aggregate itself, the shares are checked against the value the model computes.
- **Rows left out:** a related row, one the key reaches directly or through a join equality, that a `WHERE`, `QUALIFY` or `HAVING` condition, a `DISTINCT ON`, or a join that found no match removed on the way, each with the model and the condition. A join written as a `WHERE` equality between two tables is treated as the join it is.
- **Reading only the key:** each source is copied for the key alone, the key's value followed through plain columns, group keys and join equalities to the source's own column, up to `--rows` rows (a million by default). A source the key does not reach exactly, through a window not partitioned by it, a `LIMIT`, an aggregate over other keys, a key that is not a plain column or a subquery inside an expression, is copied whole, with a note.

`-n/--limit` caps each table (50 rows by default), and `--json` prints the result. A `UNION`, a recursive CTE, `ROLLUP`, `CUBE` or `GROUPING SETS`, a `SEMI` or `ANTI` join, or a window over grouped rows on the way is refused rather than guessed at. Rows read only by a subquery inside an expression (`IN`, `EXISTS`, a scalar subquery) are not traced.

## The Effect of a Definition Change

`konstruct verify --effect --since REF` runs each changed model as it was before and after each commit in the range on today's data, and prints the release notes with the difference: the rows each version returns, the sum of a changed column in each and, with a key, how many rows changed value (see [The Effect on Today's Data](release-notes.md#the-effect-on-todays-data)). `--until`, `-m/--model`, `--rows` and `--format markdown` or `json` apply.

## Remote Warehouses

`konstruct verify --warehouse snowflake` (or `bigquery`, `databricks`, `trino`) checks the models on a sample, using the warehouse connection in `konstruct.toml` or the dbt profile; a connection the repository's own files name needs the project to be trusted (see [Trusting a Project's Connections](../reference/configuration.md#trusting-a-projects-connections)). A remote warehouse is read only when it is named this way. For each input the run reads up to `--rows` rows once: a BigQuery table through its table listing, which is not billed as a query, and a BigQuery view or external table, a Snowflake, Databricks or Trino relation with `SELECT * ... LIMIT`, which runs on the warehouse (a BigQuery view or external table is billed as a query, and Snowflake and Databricks resume a suspended warehouse). The sample is copied into an in-memory DuckDB database in types that hold its values exactly (structured values as JSON), every model that reads the input uses the same copy, and each model's SQL is translated to DuckDB and perturbed there. Nothing is written to the warehouse.

A sample is not the whole table, and without an order the warehouse may return different rows on another run, so a remote run repeats exactly only when the warehouse returns the same rows. Inputs sampled separately may no longer join, and a model that returns no rows on its sample is reported as skipped; raise `--rows` to keep more. A model whose SQL does not translate to DuckDB, or uses a function DuckDB does not have, is skipped with the error. The report names the warehouse with `(sampled)`.

Each result is a test in the suite (`test_verify`, `test_realworld`, `test_airflow_parity`, `test_dagster`), so CI checks them on every change.
