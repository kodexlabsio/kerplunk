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

## Keys and Relationships

The same run checks the [ontology](ontology.md) against the data. Each key of an entity is read over its whole table: it is confirmed when no value repeats and none is null. Each relationship is confirmed when every value of its referring column is found in the key it refers to, and that key is confirmed, so its cardinality holds as well. For one claimed one-to-one, the referring column is checked for repeated values too, apart: when it repeats, the relationship is many-to-one, and verified as that when every value is found, and when verify cannot check it, the evidence says that one-to-one is declared, not verified. The queries only read, in a read-only transaction on Postgres, and a value that is null refers to nothing, so it is not counted against a relationship. A confirmed key or relationship becomes `verified` in `ontology.yml`, `konstruct ontology` and the `entity`, `metric` and `relate` results; one that fails keeps the confidence its tests and joins give it and is marked `check failed`, with what verify found in its evidence:

```text
$ konstruct verify
...
Ontology: 5 of 5 keys confirmed, 0 failed, 0 not checked; 2 of 2 relationships confirmed, 0 failed, 0 not checked.
```

With `-m`, only the keys and relationships whose tables are all among the named models are checked, nothing outside them is read, and what earlier runs found of the others is kept. `--no-ontology` leaves the checks out. They read each table whole: on a table of 1,000,000 orders referring to 100,000 customers, checking the two keys and the relationship takes 0.12 s on DuckDB and 0.38 s on Postgres, and on 10,000,000 orders 0.96 s and 5.4 s (`uv run python .kodex/scripts/integrity_benchmark.py 1000000`, with `--postgres URL` for Postgres); `verify.json` gives the time each run took as `ontology_seconds`.

A key or relationship is not checked on a remote warehouse, whose sample can miss a repeated value or the row a value refers to, on an empty table, or where the warehouse cannot compare the two columns' types; the report says which and why. `verify.json` lists every check under `keys` and `links`, and `graph.json` records the confirmed and failed ones under `meta.integrity`, which the ontology reads.

## Mutation Testing

A test suite that passes says little until it is known to fail when a model is wrong. `konstruct verify --mutate` makes small changes to each dbt model's compiled SQL, of the kind a real bug makes, and checks whether the model's dbt tests notice:

- **Mutants:** an aggregate swapped (`SUM` to `MAX`, `COUNT(DISTINCT x)` to `COUNT(x)` and back), a `WHERE`, `HAVING` or join condition dropped or negated, an inner join made a left join and back, a comparison boundary moved (`>` to `>=`) or a comparison flipped (`=` to `<>`, `<` to `>`), a date literal moved a day later, a `COALESCE` removed, or a `CASE` branch or default dropped. Up to `--max-mutants` mutants (30 by default) are drawn per model with `--seed`, from each kind in turn, so a run repeats exactly; `--operators` chooses the kinds.
- **Running:** each mutant runs in the same read-only engines as `verify`, on up to `--rows` rows of each input (1000 by default), and then every dbt data test that reads the model, compiled from `manifest.json`, runs against the mutant's output. The model's dbt unit tests run too, each on its own given rows, comparing its expected rows as dbt does. A model's SQL, and each test's, runs only when it is a single query that reads.
- **Verdicts:** a mutant is *killed* when a test fails on it, and *survived* when its output differs from the original's but every test passes. It is *equivalent* when it is the same query as the original in canonical form, so no test could kill it, and *not distinguishable on this data* when its output is the same on the sample although the query differs: a gap in the sample rather than in the tests. A mutant that does not run, or does not give the same rows twice, is *invalid*.
- **Score:** killed over killed and survived, per model and per output column; the equivalent, indistinguishable and invalid mutants are left out. A test that already fails on the original is reported and not used, and a model none of whose tests can run is skipped, with a reminder to run `dbt compile`; a model with no tests at all scores 0%.
- **Suggestions:** for each survivor, a test that passes on the original output and fails on the mutant's: `not_null`, `unique`, `accepted_values`, `relationships` (to a key from the derived ontology or the column's lineage) or `dbt_utils.expression_is_true`, as YAML to add to the column's or the model's `data_tests`. Where none does, a dbt unit test built from the fewest sample rows on which the two disagree. No suggestion quotes a value of a sensitive column (see [Sensitive Data and Erasure](sensitivity.md)): such a column gets only `not_null` and `unique`, and a model that reads one gets no unit test. `--no-suggest` leaves them out.

The run writes `MUTATION.md` and `mutation.json` to `konstruct-out/`; `--markdown FILE` writes the report to another file as well, and `--json` prints the result. Survivors are listed with the most downstream reach first, since those are the changes that would break the most.

## The Rows Behind a Value

Column lineage says which columns a value is computed from; `konstruct verify --why` says which rows. `konstruct verify --why customer_ltv.lifetime_value --where customer_id=3` lists every source row that contributes to that one value, with its share, and every related source row that a filter or a join left out, with the reason:

- **The value:** `--where` names output columns of the column's model and the values that pick one row (repeat it for a key of several columns). The model's SQL is composed down to source tables, every model it reads inlined, and run read-only in the same engines as `verify`.
- **Shares:** exact for `SUM` (each addend), `COUNT` (one each, none for a null), `AVG` (each addend and the count) and `MIN` and `MAX` (the row that won, marked `*`). `MEDIAN`, `DISTINCT` aggregates and other aggregates are not decomposable, so each input value is listed instead. Where the value is the aggregate itself, the shares are checked against the value the model computes.
- **Rows left out:** a related row, one the key reaches directly or through a join equality, that a `WHERE`, `QUALIFY` or `HAVING` condition, a `DISTINCT ON`, or a join that found no match removed on the way, each with the model and the condition. A join written as a `WHERE` equality between two tables is treated as the join it is.
- **Reading only the key:** each source is copied for the key alone, the key's value followed through plain columns, group keys and join equalities to the source's own column, up to `--rows` rows (a million by default). A source the key does not reach exactly, through a window not partitioned by it, a `LIMIT`, an aggregate over other keys, a key that is not a plain column or a subquery inside an expression, is copied whole, with a note.

`-n/--limit` caps each table (50 rows by default), and `--json` prints the result. The value of a sensitive column (see [Sensitive Data and Erasure](sensitivity.md)) is shown as `<masked: personal>` in each source row; so is every share when any column the value is computed from is sensitive, and the value itself when the column asked about is; `masked` names what was masked. A `UNION`, a recursive CTE, `ROLLUP`, `CUBE` or `GROUPING SETS`, a `SEMI` or `ANTI` join, or a window over grouped rows on the way is refused rather than guessed at. Rows read only by a subquery inside an expression (`IN`, `EXISTS`, a scalar subquery) are not traced.

## The Effect of a Definition Change

`konstruct verify --effect --since REF` runs each changed model as it was before and after each commit in the range on today's data, and prints the release notes with the difference: the rows each version returns, the sum of a changed column in each and, with a key, how many rows changed value (see [The Effect on Today's Data](release-notes.md#the-effect-on-todays-data)). `--until`, `-m/--model`, `--rows` and `--format markdown` or `json` apply.

## Data Laws

Column lineage says which columns a value comes from; the shape of each model's SQL says more. Where a model sums an amount through joins that can neither repeat nor drop a row, the total of its output must equal the total of its input. `konstruct verify --laws` reads each dbt model's compiled SQL and each SQL-built table, proposes the laws that follow, and checks them against the data, read-only:

```text
$ konstruct verify --laws
Data laws checked against jaffle_shop.duckdb (tolerance 1e-09)
16 laws checked against jaffle_shop.duckdb: 15 held on every partition, 0 broken, 1 not checked.

  holds     COUNT(*) of orders = COUNT(*) of raw_orders, per day of orders.order_date  (69 of 69 days)
  holds     orders.amount is additive: it may be summed across rows
  holds     SUM(stg_payments.amount) = SUM(raw_payments.amount) / 100  (1 of 1 partitions)
  ...

Measures with no law (7)
  orders.amount                      a join that drops rows of stg_payments with no match in stg_orders, where no relationships and not_null tests cover order_id
  ...
```

There are three kinds of law:

- **Conservation:** `SUM(daily_revenue.revenue) = SUM(raw.payments.amount) / 100`, with the factor of a unit conversion and the filters the SQL applies (a `CASE WHEN` inside a `SUM` included), followed across models for as long as each keeps the total. The total of a `UNION ALL` is the sum of its branches, each followed back to its own source, and each branch may sum or count in its own way.
- **Count:** a model that maps its input one row to one row has as many rows as its input, and a `COUNT` column adds up to the rows it counts.
- **Additivity:** whether a measure may be summed across rows (an amount), across entities but not across days (a balance), or not at all (an average or a distinct count).

A join keeps a total only where something proves it can neither repeat nor drop rows: a key on the side joined to, from a `unique` or `unique_combination_of_columns` test, a primary key the warehouse enforces (not on Snowflake, BigQuery, Redshift or Databricks, and from dbt only under an enforced contract), or in the same SQL a `GROUP BY`, `DISTINCT ON` or `QUALIFY ROW_NUMBER() ... = 1` on it; and, where unmatched rows would be dropped, `relationships` and `not_null` tests on the key that must match. A `FULL` or `RIGHT` join adds the far side's unmatched rows, which leave a sum alone and add to a row count, so it keeps sums and refuses row counts. A test with a `where` proves nothing about the whole relation, so it gives no law, and neither does an incremental model, whose table keeps rows its input no longer has. A path with any other join, a filter on another relation, `DISTINCT`, `LIMIT`, `HAVING`, `QUALIFY`, `UNION`, `INTERSECT` or `EXCEPT`, a window function, a set-returning function, a rounding cast or an integer division gives no law either; each measure with none is listed with the reason, and `--json` lists them in `blocked`.

- **Totals:** compared per day, of a date column the path carries from the source (one partition when there is none). Integer and decimal totals must agree exactly; a floating-point total may part from its source's by `--tolerance` (1e-9 by default) times the day's sum of magnitudes. A law holds when it held on every day, and a broken law gives the first days it did not hold on and the model where the totals first part. Totals need whole tables, so on a sampled remote warehouse they are left unchecked, as is a law with no rows to compare.
- **Additivity:** probed on a sample of the model's input (up to `--rows` rows), rerun through the model's SQL: every input row twice, one row twice, and the input split into two halves whose totals must add up. The probes can show only that a column is less additive than it looks, so a column the SQL makes non-additive or semi-additive, and that the probes cannot fault, is left unchecked, as is a column of a model that keeps rows by comparing them with others (`DISTINCT`, `QUALIFY`, `LIMIT`, `OFFSET`, `HAVING` or a filter with a subquery).

The checked laws are written to `konstruct-out/laws.json`. `--propose` only proposes the laws, reading no warehouse and writing nothing, so it never replaces the laws that were checked. `--strict` exits with status 1 when a law is broken.

### Laws in the Gate

`konstruct gate` judges a change against the laws as they were recorded before it: a law that held on every day and that the change no longer gives (a join on a key no test covers, a new filter, another factor, a `SUM` become an `AVG`, a removed uniqueness test, a removed model) fails the gate with `data-law-broken`, naming the model, without running anything. In a git repository the laws are the `laws.json` committed at the base, and nothing else: a copy in the output directory does not stand in for it. Only with no git base to read at is the output directory's `laws.json` used. The gate's result says where its laws came from, or that there were none and no data law was checked. To judge every pull request, run `konstruct verify --laws` on the main branch and commit `konstruct-out/laws.json`; where `konstruct-out/` is ignored, ignore `konstruct-out/*` instead and add `!konstruct-out/laws.json`, so the file can be committed.

Only what the change touches is judged. Each step of a law records the files it follows from: its model's file (for a table, the SQL file that builds it), the files of the tests it relies on, and for a dbt model the files of the macros it calls, the YAML of its properties and of the sources it reads, `dbt_project.yml` and `packages.yml`. A step is the change's when the change alters one of them, so a law broken on the base, by a change merged without recording the laws again, fails the change that broke it, not every change after it.

When the gate runs without dbt, as an agent's gate and the hook do, the manifest is from before the change. A dbt model whose file, or a macro it calls, is no longer what the manifest compiled is read from its files, rendered as `konstruct diff` renders it; a model whose file is gone is removed, unless a file of the same name moved elsewhere in the project, as dbt names a model by its file; and a test the change's YAML no longer declares, on a model or a source's table, a package's included, no longer makes a join safe. With dbt, a manifest that names a file no longer there, as after a model is deleted or moved, is stale, so dbt regenerates it.

## Remote Warehouses

`konstruct verify --warehouse snowflake` (or `bigquery`, `databricks`, `trino`) checks the models on a sample, using the warehouse connection in `konstruct.toml` or the dbt profile; a connection the repository's own files name needs the project to be trusted (see [Trusting a Project's Connections](../reference/configuration.md#trusting-a-projects-connections)). A remote warehouse is read only when it is named this way. For each input the run reads up to `--rows` rows once: a BigQuery table through its table listing, which is not billed as a query, and a BigQuery view or external table, a Snowflake, Databricks or Trino relation with `SELECT * ... LIMIT`, which runs on the warehouse (a BigQuery view or external table is billed as a query, and Snowflake and Databricks resume a suspended warehouse). The sample is copied into an in-memory DuckDB database in types that hold its values exactly (structured values as JSON), every model that reads the input uses the same copy, and each model's SQL is translated to DuckDB and perturbed there. Nothing is written to the warehouse.

A sample is not the whole table, and without an order the warehouse may return different rows on another run, so a remote run repeats exactly only when the warehouse returns the same rows. Inputs sampled separately may no longer join, and a model that returns no rows on its sample is reported as skipped; raise `--rows` to keep more. A model whose SQL does not translate to DuckDB, or uses a function DuckDB does not have, is skipped with the error. The report names the warehouse with `(sampled)`.

Each result is a test in the suite (`test_verify`, `test_realworld`, `test_airflow_parity`, `test_dagster`), so CI checks them on every change.
