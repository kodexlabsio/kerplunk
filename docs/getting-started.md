# Getting Started

Konstruct is not on PyPI yet, so install it from GitHub with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/kodexlabsio/konstruct
```

Or run it without installing anything, each time:

```bash
uvx --from git+https://github.com/kodexlabsio/konstruct konstruct build
```

For a warehouse extra, name it in the requirement, for example
`uv tool install "konstruct[snowflake] @ git+https://github.com/kodexlabsio/konstruct"` (or `[bigquery]`, `[databricks]`, `[all]`).
To pin a release, add its tag: `git+https://github.com/kodexlabsio/konstruct@v0.8.0`. Each [release](https://github.com/kodexlabsio/konstruct/releases) also has the wheel attached.

Then, in a repository with dbt models, SQL, DAGs or a warehouse:

```bash
cd your-data-repo
konstruct status                 # what konstruct finds here, before building anything
konstruct build                  # writes konstruct-out/: graph.json, REPORT.md, graph.html, ontology.yml
konstruct impact orders.amount   # everything downstream of a column
konstruct open                   # the interactive graph in a browser
konstruct install                # adds the /konstruct skill to Claude Code and Cursor
```

![konstruct impact analysis on the jaffle_shop example](images/demo-cli.gif)

To try it on the bundled example (jaffle_shop on DuckDB, with an Airflow DAG), from a clone of this repository:

```bash
uv sync
uv run .kodex/scripts/prepare_example.py      # builds the example's DuckDB warehouse with dbt
uv run konstruct build examples/jaffle_shop    # about a second
uv run konstruct impact stg_payments.amount -C examples/jaffle_shop
```

`konstruct build` needs no configuration: it finds dbt projects, Airflow DAG files, Dagster definitions, PySpark jobs, SQL files, DuckDB files and the warehouse each dbt profile points at (DuckDB, Postgres, Snowflake, BigQuery or Databricks), and prints what it found and what it did with each. If a dbt project's `target/manifest.json` is missing or older than the project files and `dbt` is on `PATH`, it runs `dbt docs generate` first. `konstruct init` writes a commented `konstruct.toml` recording what discovery found, for when you want to pin or change it.

```text
$ konstruct build
konstruct  /src/jaffle_shop

  dbt        jaffle_shop (.)  5 models, 3 seeds, 20 tests, 1 exposure  (artefacts up to date)
  duckdb     duckdb jaffle_shop.duckdb  9 relations  (dbt profile jaffle_shop)
  airflow    dags/jaffle_shop_daily.py  1 DAG, 6 tasks

  graph      1 dag, 1 exposure, 5 models, 3 seeds, 1 table, 6 tasks, 20 tests; 44 columns
  lineage    32 of 32 columns of models and built tables (100%)
  warnings   none
  wrote      graph.json, REPORT.md, graph.html, ontology.yml to konstruct-out in 0.7s
```
