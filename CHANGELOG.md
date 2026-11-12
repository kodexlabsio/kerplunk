## v0.13.20 (2026-11-12)

### Perf

- **graph**: Move the graph model and graph.json writing into the native core

## v0.13.19 (2026-11-12)

### Perf

- **build**: Find files in one native walk and record what a build read in Rust

## v0.13.18 (2026-11-12)

### Fix

- **build**: Report YAML that PyYAML cannot read in every reader instead of raising

### Perf

- **dagster**: Read Dagster definitions in the Rust core
- **airflow**: Read Airflow DAG files in the Rust core

## v0.13.17 (2026-11-12)

### Fix

- **sql**: Name each reading of a relation a query reads more than once

## v0.13.16 (2026-11-11)

### Fix

- **build**: Count an input changed while the build ran as changed

## v0.13.15 (2026-11-11)

### Fix

- **lineage**: Keep a product's presence the same however its operands are grouped

## v0.13.14 (2026-11-11)

### Feat

- **viewer**: Draw the business as a world map of entities, metrics and their trust
- **lineage**: Follow sensitive data through the lineage, with a gate rule and an erasure map
- **warehouse**: Read the tags and masking policies on each column

## v0.13.13 (2026-11-11)

### Feat

- **cli**: Export the ontology as OWL, a Snowflake semantic view and Databricks metric views
- **definitions**: Name the entity each derived Ossie dataset holds

### Fix

- **definitions**: Leave out an Ossie metric whose composed formula would miscount
- **sql**: Record UNNEST, LATERAL and TABLE sources, and name every condition's tables by relation

## v0.13.12 (2026-11-11)

### Feat

- **cli**: Pin upstream meaning with konstruct lock and lock --check
- **cli**: Check data laws with verify --laws, the laws MCP tool and the gate

### Fix

- **cli**: Say in the hook's pass when no data law was checked

## v0.13.11 (2026-11-11)

### Fix

- **sql**: Report a failed lineage, not a crash, when one of sqlglot's checks rejects the SQL
- **sql**: Look a subquery's alias up in the subquery before the query around it
- **sql**: Trace a lateral subquery's columns through the select that makes them
- **sql**: Warn when a subquery's column is taken to be its own relation's
- **sql**: Match names in any case, as Snowflake's upper-case names need
- **sql**: Record the columns DISTINCT, LIMIT and set operations pick rows by as references
- **sql**: Follow a column through a SELECT * over unknown columns
- **sql**: Keep the lineage of a model that filters on a subquery over unknown columns
- **sql**: Leave functions sqlglot does not know out of the keys of GROUP BY ALL

## v0.13.10 (2026-11-11)

### Fix

- **query**: List the copies of each conflict's own idea in carried_into

### Perf

- **query**: Compare only the pairs of the columns a change alters in the gate
- **query**: Work out each column of a conflict once, not once for every pair

## v0.13.9 (2026-11-11)

### Feat

- **query**: Work through the ontology with entity, metric and relate, each fact with its trust
- **plugins**: Record the metrics dbt's semantic layer, Looker and Power BI define

### Fix

- **query**: Say when a check-sql finding rests on a key an ontology pack presumes
- **query**: Trust only unique columns when check-sql looks for a fan-out
- **definitions**: Count only confirmed edges as verified in the ontology

### Refactor

- **query**: Keep one set of unique columns for the ontology and its derivation

## v0.13.8 (2026-11-11)

### Feat

- **lineage**: Write release notes of formula changes, and their effect with verify --effect
- **lineage**: Word a column's formula in plain English with explain --english

### Fix

- **cli**: Serve every graph Graph.read reads, from one copy, and make edges reentrantly

### Refactor

- **cli**: Read graph.json for konstruct serve with the core's one reader

### Perf

- **sql**: Check each column type once rather than once per statement
- **graph**: Read graph.json in the core and make each edge only when read
- **graph**: Scan JSON for its tokens in assembly for each architecture

## v0.13.7 (2026-11-11)

### Feat

- **mcp**: Issue an answer receipt from the MCP server
- **cli**: Record what an answer rests on, and check later whether it still holds

### Fix

- **mcp**: Let one gate at a time update its copy of the graph, without graph.html
- **mcp**: Keep the project's graph when the gate updates from files alone
- **query**: Count a measure's formula in the version of its term, whatever its status

### Refactor

- **build**: Share the making of real output folders
- **query**: Share the lockfile's pinning, and say when a pin was made

## v0.13.6 (2026-11-11)

### Feat

- **cli**: Check SQL from the command line, the MCP server and the Python API
- **query**: Check ad-hoc SQL against the project's definitions, keys and relationships
- **warehouse**: Read query texts from Snowflake, Databricks and BigQuery history

### Fix

- **sql**: Compare filters however they are written, in one form analyses share

### Refactor

- **definitions**: Expose the keys an ontology rests on and the helpers analyses share

## v0.13.5 (2026-11-11)

### Feat

- **warehouse**: Read the schemas dlt keeps in _dlt_version for the resource behind each table
- **lineage**: Read Sling replications, Lakeflow Connect bundles and DMS table mappings
- **lineage**: Follow Debezium, Kafka Connect sinks and Estuary from source tables to landed ones
- **lineage**: Read Airbyte connections and Fivetran and Airbyte Terraform for source lineage
- **build**: Read dlt pipelines, meltano.yml and Singer catalogs for lineage from source systems
- **warehouse**: Recognise the tables two dozen loaders land, from one table of marks

### Fix

- **lineage**: Limit how deeply a loader configuration may nest, the same on every Python
- **sql**: Report a relation named by a parameter or a stage instead of raising
- **airflow**: Report Python too complex for its parser as unreadable, not a failed build

## v0.13.4 (2026-11-11)

### Feat

- **cli**: Serve the viewer and a versioned JSON API over the graph with konstruct serve
- **build**: Write explain.json, the viewer's data, beside graph.html

### Fix

- **cli**: Resolve each diff base and head to a commit before git is given it anywhere else

## v0.13.3 (2026-11-10)

### Fix

- **mcp**: Declare all four tool annotations on every tool

## v0.13.2 (2026-11-10)

### Refactor

- **tests**: Move the fuzz harnesses to tests/fuzz_targets/
- **skill**: Move the Claude Code plugin to claude-code-plugin/

## v0.13.1 (2026-11-10)

### Feat

- **cli**: Search by meaning from the command line, the MCP server and Project
- **query**: Search by meaning, making the index on first use
- **query**: Embed nodes by words, lineage, formula and structure, and save the index
- **query**: Add a deterministic HNSW index over quantised vectors
- **query**: Read identifiers, synonyms and formulas as hashed features
- **viewer**: Add the map page to the navigation
- **viewer**: Draw a map of the whole project with WebGPU
- **viewer**: Order the lineage graph's layers with fewer crossings
- **viewer**: Render descriptions as Markdown, with the dbt project overview
- **viewer**: Load a served graph and reload it in place, under a strict CSP
- **viewer**: Translate the viewer into thirteen more languages
- **viewer**: Rebuild graph.html as a documentation site for the data stack
- **html**: Embed the SQL, the ontology and conflicts for the graph viewer

### Fix

- **build**: Build the compatibility corpus trusted, as CI does
- **viewer**: Fit narrow and zoomed windows, translate selector errors, and smaller fixes

### Refactor

- **query**: Read names in conflicts with the core's splitter and concept table

### Perf

- **query**: Add an int8 dot-product kernel in assembly for each architecture
- **html**: Find conflicts on demand instead of in every build
- **html**: Reuse the viewer's conflicts while the graph is unchanged

## v0.13.0 (2026-11-10)

### BREAKING CHANGE

- **config**: Until a project is trusted, warehouses named in its own konstruct.toml or profiles.yml, other than a DuckDB file inside the project, are skipped, dbt is not run, [usage] reads no query history, path settings that lead outside the project are refused, and input files that a symbolic link leads outside it are not read. Run `konstruct trust` once per project after reviewing it; from Python, call Project.trust() or pass trusted=True to load_config. For one command, pass --trust-config; in CI on trusted branches set KONSTRUCT_TRUST_CONFIG=1, never for pull requests from forks.

### Feat

- **query**: Add the analysis behind a lineage lockfile for consumers of upstream data
- **lineage**: Propose and confirm data laws from each model's SQL
- **dbt**: Keep a test's where, its columns and enforced keys in the graph
- **lineage**: Count columns whose formulas compute the same thing as one definition
- **lineage**: Compare and fingerprint formulas from Python
- **lineage**: Decide in the Rust core whether two formulas compute the same thing
- **cli**: Show the source rows behind a value with verify --why, an MCP tool and the API
- **lineage**: Show the source rows behind one value, with each row's share
- **lineage**: Let a verify engine copy only the rows a condition picks
- **cli**: Mutation test the dbt tests with verify --mutate, an MCP tool and the API
- **mcp**: Add --no-prompts to serve the tools and resources alone
- **mcp**: Offer the workflows as prompts and the report and ontology as resources
- **skill**: Add a Claude Code plugin and marketplace to the repository
- **cli**: Add konstruct hook to run the gate after an agent edits SQL
- **skill**: Install workflow skills and native instructions for six coding agents
- **mcp**: Update the graph from files alone in the gate tool
- **config**: Use a repository's own connections only once they are trusted
- **graph**: Give every machine-readable output a format version

### Fix

- **sql**: Record where each condition applies, and every clause that decides a query's rows
- **lineage**: Compose formulas through a relation another repository builds
- **build**: Link the table one repository's SQL reads to the dbt model another builds
- **build**: Keep the defining repository's formulas for a relation another reads
- **lineage**: Compose a column the same whatever was composed before it
- **dbt**: Shift a date literal only when the whole literal is a date
- **build**: Require Rust 1.95, the first that builds the core on powerpc64le
- **build**: Refuse to load the core on an s390x older than z13
- **build**: Keep a rewritten file's mode and retry a rename a reader blocks

### Refactor

- **lineage**: Let the Explainer compose a free expression and stop at given columns
- **lineage**: Make the git, history and composition helpers other modules use public
- **lineage**: Share how the columns in an expression resolve to its inputs
- **graph**: Share the macro that assembles a routine

### Perf

- **graph**: Look for the key escape with the substring routine
- **query**: Test labels for a search word in assembly
- **query**: Find the words of names and descriptions in assembly
- **query**: Copy a word into the search vocabulary only when it is new

## v0.12.8 (2026-11-10)

### Feat

- **plugins**: Load inputs, exports, checks, packs and MCP tools from installed plugins

### Refactor

- **plugins**: Run Konstruct's own inputs, exports, checks and packs through one registry

## v0.12.7 (2026-11-10)

### Feat

- **dbt**: Mutation test a project's dbt tests inside verify
- **dbt**: Make semantic mutants of a model's SQL

### Refactor

- **lineage**: Make verify's quote and rewrite public

## v0.12.6 (2026-11-10)

### Feat

- **cli**: Group the commands in help, add examples, and suggest close names
- **cli**: Accept --json on diff and gate, and -n on history
- **cli**: Colour, fit and show progress on a terminal, and plain text in pipes
- **cli**: Render every result as a header, aligned tables and lineage trees
- **build**: Log each stage of the build for progress displays
- **definitions**: Ship 274 ontology packs from permissively licensed connector schemas

### Perf

- **cli**: Measure start-up and the text of a large result in the benchmark
- **cli**: Import the error types only when one is raised
- **cli**: Import the package's public names when first used

## v0.12.5 (2026-11-10)

### Feat

- **mcp**: Mark repository text in tool results as data, not instructions

### Fix

- **lineage**: Keep the input's row order in the shuffled copy verify compares
- **lineage**: Keep a verified model to the copies of its inputs

## v0.12.4 (2026-11-10)

### Fix

- **graph**: Accept ids and names that hold lone surrogates in the Rust core

### Refactor

- **graph**: Number nodes, edges and words with a checked conversion
- **graph**: Build the native indexes one node and one edge at a time

### Perf

- **graph**: Add the consumers and through tasks of a walk in the Rust core
- **query**: Rank search results in the Rust core
- **graph**: Walk, find paths and resolve relation names in the Rust core
- **html**: Escape the viewer's embedded data with str.replace

## v0.12.3 (2026-11-10)

### Fix

- **dbt**: Turn off dbt's anonymous usage statistics when Konstruct runs dbt
- **lineage**: Run a model only when its SQL is a single query that reads

## v0.12.2 (2026-11-09)

### Fix

- **build**: Report Python and YAML files nested too deeply to read instead of stopping
- **sql**: Report SQL that makes the parser fail with an AttributeError as unparsed
- **mcp**: Refuse a diff base or head that git would read as an option

## v0.12.1 (2026-11-09)

### Feat

- **cli**: Show every input and the bound ontology packs in konstruct status

### Fix

- **config**: Refuse a repositories table instead of reading its keys as paths

### Perf

- **lineage**: Compose the viewer's formulas as text instead of as trees

## v0.12.0 (2026-11-09)

### BREAKING CHANGE

- Code that imports a module directly uses its new path, such as konstruct.core.graph for konstruct.graph or konstruct.analysis.explain for konstruct.explain.

### Feat

- **definitions**: Write an Ossie model back as authored, graded on Ossie's portability ladder

### Refactor

- Group the package's modules into subpackages by role

## v0.11.13 (2026-11-09)

### Feat

- **definitions**: Read SKOS and OWL vocabularies, with a concept hierarchy

### Fix

- **sql**: Report SQL that crashes the parser as unparsed instead of stopping the build
- **cli**: Report an export with nothing to export as an error, not a traceback

### Perf

- **build**: Analyse dbt models in worker processes, a DAG level at a time

## v0.11.12 (2026-11-09)

### Feat

- **definitions**: Bind shared ontology packs for common source schemas
- **cli**: Answer when a column's formula changed and who approved it
- **graph**: Say which business entities a change touches

### Fix

- **lineage**: Keep the brackets that operator-style functions need in formulas

### Perf

- **sql**: Print canonical column formulas without copying their trees

## v0.11.11 (2026-11-09)

### Feat

- **warehouse**: Show the tables Fivetran and Airbyte land, and Fivetran's source lineage
- **build**: Read Power BI semantic models as exposures of the columns they use

## v0.11.10 (2026-11-09)

### Feat

- **warehouse**: Read Trino catalogues and sample them for verify
- **build**: Read Looker views as exposures of the columns they use

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
