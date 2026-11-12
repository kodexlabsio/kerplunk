# Konstruct


**An ontology of a business derived from its data stack, with the column-level lineage beneath it checked against the warehouse, for people and AI agents.**

Konstruct reads a data repository and its warehouse and derives an ontology of the business they describe: the entities and their keys, the measures with their formulas composed down to source columns, the relationships between entities, and the business definitions bound to columns, with their owners and approval status. Beneath it is a graph of every table, column, model, task, test and dashboard, with column-level lineage parsed from the code that connects them, across dbt, SQL, Airflow, Dagster, Spark, Looker and Power BI. Konstruct checks the SQL lineage against the data and checks each change against the graph before it merges. It runs locally in one command, with no server and no account, and answers from the command line, a Python API, an MCP server, skills and a Claude Code plugin for coding agents, a GitHub Action, and a single offline HTML page.

What it provides:

- **An ontology derived from the code.** The business objects, their keys and relations, the measures with their verified formulas, and how the objects relate, read from the code and tests and written to `ontology.yml` on every build. `definitions.yml`, SKOS and OWL vocabularies and Apache Ossie models add what code cannot say, and 274 ontology packs bind the entities and measures of common source systems when their tables are in the warehouse. [The Ontology](guide/ontology.md)
- **It finds columns by meaning.** A search for "revenue" finds a column called `net_sales_amt`, through the abbreviations and synonyms of data work, its formula and its lineage, with exact names still first. Everything is computed locally from the graph. [Searching by Meaning](guide/search.md)
- **It shows how a column is calculated.** `konstruct explain` composes a column's SQL through every model upstream, so `customer_ltv.lifetime_value` reads as `SUM(raw_payments.amount / 100)`, with the joins and filters of each step listed beside it. A test evaluates composed formulas on the data and checks they give the model's values row for row. [Explaining a Column](guide/explain.md)
- **It checks its lineage against the data.** `konstruct verify` perturbs each input column, reruns each model and records which outputs change. On dbt Labs' jaffle-shop all 87 claimed column edges are confirmed, and nothing changed that the graph did not predict. `--mutate` measures whether a project's tests would catch a wrong model, and `--why` lists the source rows behind one value. [How Much to Trust the Lineage](guide/verify.md)
- **It finds conflicting definitions.** `konstruct conflicts` compares columns that compute the same business idea by what their formulas compute, not how they are written, and shows the sources, filters, joins and aggregations that differ. [Finding Conflicting Definitions](guide/conflicts.md)
- **It reviews changes column by column.** `uses: kodexlabsio/konstruct@v0` comments with what a change does to each column, ignoring formatting and renamed CTEs, and fails the check when a removed column is still read downstream. `konstruct gate` also fails on an approved definition whose meaning changed, and `konstruct hook` runs it after Claude Code edits SQL or dbt YAML. It also prints a `dbt build --select` for only what the change affects. [Reviewing Changes](guide/reviewing-changes.md)
- **One chain across tools.** A column computed in dbt, copied by an Airflow SQL task, written by a Dagster asset and shown on a dashboard is one lineage chain, through DuckDB, Postgres, Snowflake, BigQuery, Databricks, Trino and Iceberg.
- **It handles large projects.** 2,000 models and 52,000 column edges build in 37 seconds on a four-core cloud machine and update in 7; queries answer in milliseconds. [Performance](performance.md)
- **It exports open formats.** OpenLineage events validated against the specification, Mermaid, Graphviz, CSV and Apache Ossie. [Exporting](guide/exporting.md)

![graph.html showing the lineage of customer_ltv.lifetime_value: four columns across an Airflow SQL table, two dbt models and a seed, each verified, with the formula SUM(raw_payments.amount / 100)](images/graph-html.png)

## Start Here

- [Getting Started](getting-started.md): install Konstruct and build a graph in a minute.
- [The Ontology](guide/ontology.md): the entities, measures and relationships Konstruct derives, and the rules it holds them to.
- [The Graph Viewer](guide/viewer.md): the single-file page Konstruct writes with every build.
- [The GitHub Action](guide/github-action.md): review pull requests column by column.
- [Command Line](reference/cli.md) and [Python API](reference/python-api.md): every command and class.
- [The paper](https://kodexlabsio.github.io/konstruct/paper/konstruct.pdf): the design, verification by perturbation and its soundness, and the evaluation, rebuilt from the code with these pages.
- [The manifesto](https://github.com/kodexlabsio/konstruct/blob/main/MANIFESTO.md): what Konstruct believes about truth in data, and the commitments it makes to its users.

```{toctree}
:hidden:
:caption: Introduction

getting-started
performance
```

```{toctree}
:hidden:
:caption: Guide

guide/inputs
guide/viewer
guide/explain
guide/search
guide/verify
guide/reviewing-changes
guide/release-notes
guide/github-action
guide/definitions
guide/ontology
guide/metric-queries
guide/ontology-packs
guide/ossie
guide/conflicts
guide/sensitivity
guide/usage
guide/exporting
guide/agents
guide/receipts
guide/plugins
```

```{toctree}
:hidden:
:caption: Reference

reference/cli
reference/python-api
reference/mcp
reference/http-api
reference/configuration
reference/outputs
reference/limitations
```

```{toctree}
:hidden:
:caption: Project

development
contributing
security
changelog
```
