## v0.11.9 (2026-11-09)

### Feat

- **build**: Read the SQL in PySpark jobs for column lineage
- **cli**: Add konstruct gate, the check a change passes before it is proposed

## v0.11.8 (2026-11-09)

### Feat

- **cli**: Verify lineage on a sample of Snowflake, BigQuery or Databricks

## v0.11.7 (2026-11-09)

### Feat

- **definitions**: Read and write Apache Ossie semantic models

## v0.11.6 (2026-11-09)

### Feat

- **cli**: List the inputs of other repositories in konstruct status
- **build**: Build several repositories into one graph
- **definitions**: Derive an ontology of entities, measures and relationships
- **warehouse**: Read primary keys from DuckDB, Postgres and dbt constraints

## v0.11.5 (2026-11-09)

### Feat

- **cli**: Compare the data stack with any date in konstruct diff

## v0.11.4 (2026-11-09)

### Perf

- **build**: Build about twice as fast with compiled sqlglot and fewer tree copies

## v0.11.3 (2026-11-09)

### Fix

- **deps**: Pin actions and images, bound sqlglot and mcp, test on Python 3.14

## v0.11.2 (2026-11-09)

### Fix

- **sql**: Order a column's sources the same way in every process
- **graph**: Choose the same path between equal routes on every build
- Skip fetching the base branch when the repository has no origin

## v0.11.1 (2026-11-09)

### Refactor

- Capitalise Konstruct, build the viewer to one file, match the house formatting

## v0.11.0 (2026-11-09)

### Refactor

- **html**: Rebuild the graph viewer in Svelte and TypeScript
- Rename the project to konstruct and flatten the package layout

## v0.10.2 (2026-11-09)

### Feat

- **warehouse**: Rank columns by what query history shows people read

## v0.10.1 (2026-11-09)

### Feat

- **lineage**: Find columns that compute the same business idea differently

### Fix

- **release**: Only treat a real breaking-change footer as breaking

## v0.10.0 (2026-11-09)

### Feat

- **definitions**: Record business terms in definitions.yml and attach them to the graph

## v0.9.1 (2026-11-09)

### Fix

- **ci**: Never run dbt in the GitHub Action by default

### Perf

- **lineage**: Build large projects in linear time and update in seconds

## v0.9.0 (2026-11-09)

### Feat

- **lineage**: Export the graph as OpenLineage, Mermaid, DOT and CSV

## v0.8.0 (2026-11-09)

### Feat

- **ci**: Make konstruct a GitHub Action for pull request lineage review

### Fix

- **cli**: Allow diff --format selector and add --json-file for scripts

## v0.7.0 (2026-11-09)

### Feat

- **html**: Isolate a selected column's path and show verification results
- **html**: Rebuild the graph viewer on the Kodex Labs design system
- **lineage**: Explain how a column is calculated, composed down to sources

## v0.6.0 (2026-11-06)

### Feat

- **dagster**: Parse op-based jobs, schedules and sensors

## v0.5.1 (2026-11-06)

### Fix

- **readme**: Install from GitHub, since konstruct is not on PyPI yet

## v0.5.0 (2026-11-06)

### Feat

- **lineage**: Follow lineage into list elements and map values

## v0.4.0 (2026-11-06)

### Feat

- **warehouse**: Add Snowflake, BigQuery and Databricks connectors as extras

## v0.3.0 (2026-11-06)

### Feat

- **dagster**: Parse Dagster assets, dbt assets, jobs and schedules statically
- **dbt**: Report dbt YAML changes in konstruct diff

### Fix

- **graph**: Report the tasks that read changed data in impact

## v0.2.0 (2026-11-06)

### Feat

- **lineage**: Verify lineage against Postgres as well as DuckDB
- **dbt**: Select only the dbt nodes a change affects, column by column
- **lineage**: Add konstruct diff for column-level change review
- **dbt**: Render dbt Jinja without dbt, matching compiled lineage
- **lineage**: Follow lineage into struct fields
- **lineage**: Verify column lineage against the data by perturbation

### Fix

- **ci**: Use the runner's Chrome for browser tests and fix the default diff base

## v0.1.1 (2026-11-06)

### Fix

- **ci**: Push the release tag by name

## v0.1.0 (2026-11-06)

### Feat

- **skill**: Tell agents about --refresh, status and list
- **cli**: Rework the command line and add a typed Python API
- **skill**: Install a /konstruct skill for Claude Code and Cursor
- **mcp**: Add a stdio MCP server with impact, trace, search, describe and path
- **cli**: Add the konstruct command line
- **query**: Add impact, trace, path, search, describe and query
- **html**: Add a single-file interactive graph viewer
- **build**: Build the graph, report and viewer from every input
- **warehouse**: Read DuckDB, Postgres and Iceberg catalog metadata
- **airflow**: Parse Airflow DAG files statically
- **dbt**: Load dbt projects from the manifest and catalog
- **examples**: Add the jaffle_shop example with an Airflow DAG
- **sql**: Add column-level lineage for SQL statements with sqlglot
- **graph**: Add the graph model with impact, trace and path traversal

### Fix

- **ci**: Install Chromium once and cache it, and time-limit every job
- **readme**: Format the Python example and check Markdown code blocks before commit
- **ci**: Pin setup-uv to v10.3.3 and use the publish action's release/v1 branch
- **airflow**: Link a TaskGroup at its first and last tasks, as Airflow does
- **html**: Keep the selected node in view, columns in table order and dbt source names
- **lineage**: Follow seeds into the sources over them and stop guessing star columns
- **query**: Shorten long descriptions in impact and trace headers
