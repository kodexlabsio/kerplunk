<p align="center">
<a target="_blank" href="https://kodexlabs.io/"><img width="150" height="150" src="./docs/images/logo-icon.svg"></a>
</p>
<p align="center">
<a href="https://github.com/kodexlabsio/konstruct/actions/workflows/build-and-test.yml"><img src="https://github.com/kodexlabsio/konstruct/actions/workflows/build-and-test.yml/badge.svg" alt="Build and Test"></a>&nbsp;<a href="https://github.com/kodexlabsio/konstruct/actions/workflows/release.yml"><img src="https://github.com/kodexlabsio/konstruct/actions/workflows/release.yml/badge.svg" alt="Create Release"></a>&nbsp;<a href="./LICENSE"><img src="https://img.shields.io/badge/licence-MIT-blue" alt="MIT licence"></a>&nbsp;<img src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue" alt="Python 3.11 to 3.14">
<br />
<a href="https://kodexlabs.io/" target="_blank">Kodex Labs</a> Ltd.
</p>


<h1 align="center">Konstruct</h1>

<p align="center">
<strong>Column-level lineage for dbt, SQL, Airflow and Dagster.</strong><br />
Explained as formulas, checked against the data, and reviewed on every pull request.
</p>

<p align="center">
<a href="https://kodexlabsio.github.io/konstruct/"><strong>Documentation</strong></a>
&nbsp;&middot;&nbsp;
<a href="https://kodexlabsio.github.io/konstruct/getting-started.html">Getting started</a>
&nbsp;&middot;&nbsp;
<a href="https://kodexlabsio.github.io/konstruct/guide/github-action.html">GitHub Action</a>
&nbsp;&middot;&nbsp;
<a href="https://kodexlabsio.github.io/konstruct/reference/cli.html">Commands</a>
&nbsp;&middot;&nbsp;
<a href="CHANGELOG.md">Changelog</a>
</p>

<p align="center">
<img src="docs/images/graph-html.png" alt="The lineage of customer_ltv.lifetime_value in the Konstruct viewer: four columns across an Airflow SQL table, two dbt models and a seed, each verified on data, with the formula SUM(raw_payments.amount / 100)" width="100%" />
</p>

Konstruct reads a data repository and its warehouse and builds a graph of every table, column, model, task, test, dashboard and business term, with column-level lineage parsed from the SQL that connects them. It runs locally in one command, with no server and no account, and answers from the command line, a Python API, an MCP server for coding agents, a GitHub Action and a single offline HTML page.

## Why Konstruct

<table>
<tr>
<td width="50%" valign="top">

### How a column is calculated

`konstruct explain` composes a column's SQL through every model upstream, so `customer_ltv.lifetime_value` reads as `SUM(raw_payments.amount / 100)`, with the joins and filters of each step beside it.

<sub><a href="https://kodexlabsio.github.io/konstruct/guide/explain.html">Explaining a column</a></sub>

</td>
<td width="50%" valign="top">

### Lineage checked against the data

`konstruct verify` perturbs each input column, reruns each model and records what changes. On dbt Labs' jaffle-shop, all 87 claimed column edges are confirmed and nothing changed that the graph did not predict.

<sub><a href="https://kodexlabsio.github.io/konstruct/guide/verify.html">How much to trust the lineage</a></sub>

</td>
</tr>
<tr>
<td width="50%" valign="top">

### Pull requests reviewed column by column

`uses: kodexlabsio/konstruct@v0` comments with what a change does to each column, ignores formatting and renamed CTEs, and fails the check when a removed column is still read downstream.

<sub><a href="https://kodexlabsio.github.io/konstruct/guide/github-action.html">The GitHub Action</a></sub>

</td>
<td width="50%" valign="top">

### Business definitions beside the lineage

`definitions.yml` records what each term means, who owns it and which columns hold it. Agents get the approved definition, and the build warns when a bound column disappears. Konstruct derives an ontology of entities, measures and relationships from them, and reads and writes Apache Ossie semantic models: a model it reads is written back as authored, with each metric's lineage and trust added.

<sub><a href="https://kodexlabsio.github.io/konstruct/guide/definitions.html">Business definitions</a></sub>

</td>
</tr>
<tr>
<td width="50%" valign="top">

### Conflicting definitions found

`konstruct conflicts` finds the same measure computed in different ways, such as two revenue columns that differ by one filter, and shows exactly what differs and in which file.

<sub><a href="https://kodexlabsio.github.io/konstruct/guide/conflicts.html">Finding conflicting definitions</a></sub>

</td>
<td width="50%" valign="top">

### One chain across tools

A column computed in dbt, copied by an Airflow SQL task, written by a Dagster asset and shown on a dashboard is one chain, through DuckDB, Postgres, Snowflake, BigQuery, Databricks, Trino and Iceberg, and across repositories.

<sub><a href="https://kodexlabsio.github.io/konstruct/guide/inputs.html">Inputs and how lineage works</a></sub>

</td>
</tr>
</table>

## Quick Start

Konstruct is not on PyPI yet; install it from GitHub with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/kodexlabsio/konstruct
```

Then, in a repository with dbt models, SQL, DAGs or a warehouse:

```bash
konstruct build                         # writes konstruct-out/: graph.json, REPORT.md, graph.html, ontology.yml
konstruct impact stg_payments.amount    # everything downstream of a column
konstruct explain orders.amount         # how a column is calculated, down to its sources
konstruct history orders.amount         # when its formula changed, and who approved it
konstruct open                          # the interactive graph in a browser
konstruct ontology                      # entities, measures and how they relate
konstruct install                       # the /konstruct skill for Claude Code and Cursor
```

No configuration is needed: Konstruct finds dbt projects, Airflow and Dagster files, SQL, definitions and the warehouse each dbt profile points at. [Getting started](https://kodexlabsio.github.io/konstruct/getting-started.html) covers extras, pinning a release and the bundled example.

<p align="center">
<img src="docs/images/demo-cli.gif" alt="konstruct build and konstruct impact on the jaffle_shop example" width="100%" />
</p>

## The Evidence

Lineage tools usually assert their lineage. Konstruct measures it, and every number here is a test that runs on every commit.

| Lineage checked against the data (seed 1729) | Claimed column edges | Confirmed | Unexplained |
|----------------------------------------------|---------------------:|----------:|------------:|
| The bundled `jaffle_shop` example | 36 | 36 | 0 |
| dbt Labs' jaffle-shop, at a pinned commit | 87 | 87 | 0 |
| The example on Postgres | 31 | 31 | 0 |
| 300 generated queries on generated data | | | 0 |

| Generated dbt project | Column edges | Cold build | Update, one model edited | Query |
|----------------------:|-------------:|-----------:|-------------------------:|------:|
| 500 models | 12,940 | 5.4 s | 1.1 s | 3 ms |
| 2,000 models | 51,940 | 20.7 s | 5.9 s | 9 ms |

Composed formulas are evaluated on the data and must match the final model row for row; the static Airflow and Dagster parsers are compared with Airflow's and Dagster's own graphs; and OpenLineage exports are validated against the specification. [Performance](https://kodexlabsio.github.io/konstruct/performance.html) and [verification](https://kodexlabsio.github.io/konstruct/guide/verify.html) give the details.

## Inputs

| | |
|---|---|
| **Transformation** | dbt (manifest, catalog, or rendered without dbt), standalone SQL files, the SQL in PySpark jobs, warehouse views |
| **Orchestration** | Airflow (DAGs, TaskGroups, TaskFlow, assets, Cosmos) and Dagster (assets, op jobs, schedules, sensors), parsed without running them |
| **Warehouses** | DuckDB, Postgres, Snowflake, BigQuery, Databricks, Trino, and Iceberg through any PyIceberg catalogue |
| **Dashboards** | dbt exposures, Looker views field by field from LookML, and Power BI semantic models from TMDL |
| **Ingestion** | The tables Fivetran and Airbyte land, and column lineage from each Fivetran source system |
| **Meaning** | Business terms in `definitions.yml` and SKOS or OWL vocabularies, with a concept hierarchy, ontology packs for Stripe, Shopify, Salesforce, HubSpot and GA4 bound automatically, and column usage from Snowflake, BigQuery and Databricks query history |

## Features

Try the [live demo](https://kodexlabsio.github.io/konstruct/demo/): the viewer for the jaffle_shop example, rebuilt from main with the documentation.

**Lineage and impact**

- `konstruct impact` and `konstruct trace`: everything downstream or upstream of a table or column, through dbt models, SQL files, warehouse views, Airflow and Dagster tasks, PySpark jobs, loaders and dashboards, with the transform at each step.
- `konstruct path`: how two tables or columns are connected.
- `konstruct build --update`: an incremental rebuild that reanalyses only what changed.
- `konstruct search`, `konstruct describe`, `konstruct list` and `konstruct query`: find anything in the graph by name, inspect one node, list a kind, or ask in plain words.
- One graph across repositories that share a warehouse, and across warehouses.

**How a value is calculated**

- `konstruct explain`: a column's formula composed through every model upstream, down to source columns, with the joins and filters on the way.
- `konstruct history`: when a column's formula changed, in which commit, and whether its definition was approved after the change.

**How far to trust it**

- `konstruct verify`: lineage checked against the data by perturbation, on DuckDB, Postgres and samples of Snowflake, BigQuery, Databricks and Trino, read only ([verify](https://kodexlabsio.github.io/konstruct/guide/verify.html)).
- Column usage from Snowflake, BigQuery and Databricks query history, so the columns people read stand out ([usage](https://kodexlabsio.github.io/konstruct/guide/usage.html)).

**Change review**

- `konstruct diff`: what a branch does to each column, ignoring formatting and renamed CTEs, and the `dbt build --select` that rebuilds exactly what it changes ([reviewing changes](https://kodexlabsio.github.io/konstruct/guide/reviewing-changes.html)).
- `konstruct gate`: the check a coding agent passes before it proposes a change, and CI before it merges. It fails on a removed column something still reads, an approved definition whose meaning changed, and a new column that computes a business idea differently from an existing one.
- A [GitHub Action](https://kodexlabsio.github.io/konstruct/guide/github-action.html) that comments on each pull request, annotates breaking changes and fails the check when a removed column is still read.

**Meaning**

- `konstruct define`: the approved business definition of a term from `definitions.yml`, with its owner, status, grain, required filters and bound columns ([business definitions](https://kodexlabsio.github.io/konstruct/guide/definitions.html)).
- SKOS and OWL vocabularies read as terms, with a concept hierarchy.
- `konstruct ontology`: the entities and their keys, the measures with their composed formulas, and the relationships between entities, with cardinality where a key is tested, checked against rules that `--strict` turns into a failing build ([the ontology](https://kodexlabsio.github.io/konstruct/guide/ontology.html)).
- `konstruct conflicts`: the same measure computed in different ways, and exactly what differs ([conflicts](https://kodexlabsio.github.io/konstruct/guide/conflicts.html)).
- Ontology packs for Stripe, Shopify, Salesforce, HubSpot and GA4, bound automatically when their tables are in the warehouse.
- Apache Ossie semantic models read as an input and written as an export, with each metric's lineage and trust added ([Ossie](https://kodexlabsio.github.io/konstruct/guide/ossie.html)).

**Where to use it**

- The command line: `konstruct init`, `konstruct build`, `konstruct status`, `konstruct open` and every command above, the commands that answer a question with `--json` output for scripts ([every command](https://kodexlabsio.github.io/konstruct/reference/cli.html)).
- Coding agents: `konstruct install` adds the skill for Claude Code and Cursor, and `konstruct mcp` runs the [MCP server](https://kodexlabsio.github.io/konstruct/reference/mcp.html), whose tools are `impact`, `trace`, `path`, `search`, `describe`, `query`, `explain`, `history`, `define`, `conflicts`, `ontology`, `diff` and `gate`.
- Python: `konstruct.Project`, with [typed results](https://kodexlabsio.github.io/konstruct/reference/python-api.html) for every question.
- A browser: `graph.html`, [one offline file](https://kodexlabsio.github.io/konstruct/guide/viewer.html) with the lineage, formulas, business terms, verification results and usage, with `REPORT.md` and `ontology.yml` beside it for reading in a repository.
- Other tools: `konstruct export` writes OpenLineage events, Mermaid, Graphviz DOT, CSV and Apache Ossie ([exporting](https://kodexlabsio.github.io/konstruct/guide/exporting.html)).

## Documentation

The full documentation is at **[kodexlabsio.github.io/konstruct](https://kodexlabsio.github.io/konstruct/)**:

- **Guide:** [inputs and how lineage works](https://kodexlabsio.github.io/konstruct/guide/inputs.html), [the viewer](https://kodexlabsio.github.io/konstruct/guide/viewer.html), [explain](https://kodexlabsio.github.io/konstruct/guide/explain.html), [verify](https://kodexlabsio.github.io/konstruct/guide/verify.html), [reviewing changes](https://kodexlabsio.github.io/konstruct/guide/reviewing-changes.html), [business definitions](https://kodexlabsio.github.io/konstruct/guide/definitions.html), [the ontology](https://kodexlabsio.github.io/konstruct/guide/ontology.html), [Apache Ossie models](https://kodexlabsio.github.io/konstruct/guide/ossie.html), [conflicts](https://kodexlabsio.github.io/konstruct/guide/conflicts.html), [usage](https://kodexlabsio.github.io/konstruct/guide/usage.html), [coding agents](https://kodexlabsio.github.io/konstruct/guide/agents.html)
- **Reference:** [commands](https://kodexlabsio.github.io/konstruct/reference/cli.html), [Python API](https://kodexlabsio.github.io/konstruct/reference/python-api.html), [MCP server](https://kodexlabsio.github.io/konstruct/reference/mcp.html), [configuration](https://kodexlabsio.github.io/konstruct/reference/configuration.html), [outputs](https://kodexlabsio.github.io/konstruct/reference/outputs.html), [limitations](https://kodexlabsio.github.io/konstruct/reference/limitations.html)
- **Project:** [development](https://kodexlabsio.github.io/konstruct/development.html), [contributing](CONTRIBUTING.md), [security](SECURITY.md), [changelog](CHANGELOG.md)

## Star History

<p align="center">
<a href="https://www.star-history.com/#kodexlabsio/konstruct&Date">
<picture>
<source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=kodexlabsio/konstruct&type=Date&theme=dark" />
<source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=kodexlabsio/konstruct&type=Date" />
<img alt="Star history of kodexlabsio/konstruct" src="https://api.star-history.com/svg?repos=kodexlabsio/konstruct&type=Date" width="100%" />
</picture>
</a>
</p>

## Contributing and Security

Bug reports with a small failing case are the most useful contribution; [CONTRIBUTING.md](CONTRIBUTING.md) explains how to report one or propose a change. [SECURITY.md](SECURITY.md) covers reporting a vulnerability and what Konstruct executes, reads and writes: it never imports DAG code, renders dbt Jinja in a sandbox, makes no requests beyond the warehouses you configure, and collects no telemetry.

## Licence

MIT. See [LICENSE](LICENSE). The jaffle_shop example keeps its own Apache-2.0 licence, and the Manrope and JetBrains Mono fonts in the viewer are under the SIL Open Font License ([licenses/](licenses/)).
