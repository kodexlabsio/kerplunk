# Command Line

| Command | Purpose |
|---------|---------|
| `konstruct build [path] [-u]` | Build `konstruct-out/` (`graph.json`, `REPORT.md`, `graph.html` and `ontology.yml`). `-u/--update` reuses cached analyses of anything unchanged (keyed by file hashes, compiled SQL, manifest checksums and the upstream columns each model sees). `--no-dbt`, `--no-warehouse`, `--dialect`, `--out`, `--json` (a machine-readable summary), `--strict` (exit 1 on warnings) |
| `konstruct status` | What Konstruct finds (configuration, dbt projects and whether their artefacts are current, warehouses, DAG, Dagster, PySpark, SQL, LookML and TMDL files, definitions files, vocabularies, Ossie models, and the inputs of each other repository listed in `repositories`), the ontology packs, loader lineage and usage the last build read, whether the project's own connections are trusted, whether the graph is out of date and which inputs changed, where the skills are installed, and each installed plugin with whether the project enables it |
| `konstruct trust [path]` | Trust the project, after reviewing it: its own warehouse connections and paths outside it in `konstruct.toml` and `profiles.yml` are used, and dbt is run in it, until those settings change (see [Trusting a Project's Connections](configuration.md#trusting-a-projects-connections)). Until then a build skips those connections, refuses those paths and never runs dbt. Prints the files covered, the environment variables they read and the dbt projects it runs dbt in. `--revoke` removes the record, `--json` |
| `konstruct init` | Write a commented `konstruct.toml` from what discovery found (`--stdout` to print it) |
| `konstruct impact <table.column>` | Everything downstream, drawn as a tree from the column (or, for a table, from the table through the tables it feeds): derived columns, relations, models that join, filter or group on it, tests, exposures, the DAG tasks that run them, and the business entities it touches. `--depth N` stops after N hops |
| `konstruct trace <table.column>` | Everything upstream, as a tree, each step marked `direct` (passed through or renamed) or `transformed`, and the business entities involved, its own included |
| `konstruct path <a> <b>` | Shortest connection between two tables or columns |
| `konstruct search <words>` | Find nodes by name or description, optionally `--kind model` |
| `konstruct conflicts` | Columns that compute the same business idea in different ways, with the sources, filters, joins and aggregations that differ and the file each is in, ranked by downstream reach. Writes `CONFLICTS.md` and `conflicts.json`. `--format markdown`, `--fail-on-conflict`, `-n N` (see [Finding Conflicting Definitions](../guide/conflicts.md)) |
| `konstruct define <term>` | What a business term means: its definition, owner, status, grain, required filters and the columns that hold it. Takes a term, a synonym or a column (to list the terms that define it). Exit status 3 when no term matches |
| `konstruct ontology` | The entities, measures and relationships derived from the data stack, with any broken rules (see [The Ontology](../guide/ontology.md)). `--format yaml`, `--strict` (exit 1 when a rule is broken) |
| `konstruct explain <table.column>` | How a column is calculated: its expression as SQL with CTEs and aliases inlined, the same formula composed down to source columns, and step by step each column on the way with the joins, filters and grouping that shape its rows (see [Explaining a Column](../guide/explain.md)) |
| `konstruct history <table.column>` | When the column's formula changed in git history, with each commit's author, date and subject, and every change to the status, approval date, owner or definition of the business terms bound to it; says whether a term was approved after the latest formula change (see [Reviewing Changes](../guide/reviewing-changes.md)). `-n/--limit N` commits per file (default 50), `--json` |
| `konstruct describe <name>` | Columns, types, descriptions, tests and neighbours of one node; for a task, what it runs and its neighbours in the DAG |
| `konstruct list [kind ...]` | Every node of the given kinds (`models`, `tables`, `sources`, `tasks`, ...; default all relations) |
| `konstruct open` | Open `graph.html` in a browser |
| `konstruct export <format>` | The graph as OpenLineage events, Mermaid, Graphviz DOT or CSV, or the ontology as an Apache Ossie semantic model (see [Exporting](../guide/exporting.md)). `-s NAME` for the lineage of one table or column (not for Ossie), `--columns` to draw columns, `-o FILE`. A plugin the project enables can add formats |
| `konstruct diff [--base REF]` | What the working tree (or `--head REF`) changes compared with the base (default: the merge base with main). Either can be a date, such as `--base 2026-10-01` or `--base "2026-10-01 14:30"`, for the last commit on the current branch before it (a date alone means its start, in local time): columns added, removed or changed with their expressions before and after, changed join and filter conditions, breaking changes, DAG changes, dbt tests, sources and exposures added or removed in YAML, the downstream impact of each, and a column-aware `dbt build --select` that rebuilds only what the change affects. `--format markdown` for a pull request comment, `--json` (as `--format json`), `--fail-on-breaking` for CI |
| `konstruct gate [--base REF]` | The check a change must pass before it is proposed: brings the graph up to date with the working tree and fails (exit 1) on a removed column something still reads, an approved term whose meaning changes (in `definitions.yml`, or through a column it names or one upstream of it), or a changed column that computes a business idea differently from an existing one, each with how to fix it, and on the findings of the checks plugins add (see [The Gate](../guide/github-action.md#the-gate)). `--no-update` uses the graph as it is, `--no-dbt` and `--no-warehouse` apply to the update, `--format text\|markdown\|json` (`--json` for short), `--json-file` |
| `konstruct verify` | Test the column lineage against the data by perturbation, in a DuckDB file, a Postgres database, or a sample of Snowflake, BigQuery, Databricks or Trino (`--warehouse snowflake`); writes `VERIFY.md` and `verify.json` and marks confirmed edges in the graph. `--seed`, `--rows`, `--model`, `--strict` |
| `konstruct query "<question>"` | Free-text question: ranks nodes by its words and returns them with their connections, plus impact or lineage when the question asks for it. There is no language model in Konstruct; the agent reads the result |
| `konstruct install` | Install the Konstruct skills and instructions for coding agents: `--agent claude\|cursor\|codex\|copilot\|gemini\|windsurf` (one or more; `both`, the default, or `all`), `--target DIR` for a repository rather than the home directory, `--mcp` to register the MCP server in each agent's configuration, `--force` to replace files and sections edited by hand (see [Coding Agents](../guide/agents.md)) |
| `konstruct hook post-edit` | For a Claude Code PostToolUse hook: reads the hook event on standard input and, when the edited file is SQL, dbt YAML, a definitions file or an Ossie model in a repository with a Konstruct graph, runs the gate on a copy of the graph built from files alone (no dbt, no warehouse) and prints the result as hook JSON. `--remind` only reminds the agent to run the gate |
| `konstruct mcp` | Run the MCP server on stdio. `--no-prompts` offers the tools and resources without the workflow prompts |
| `konstruct plugins` | Every installed plugin, by distribution: its inputs, export formats, gate checks, ontology packs and MCP tools, the plugin API version each targets, and whether each runs for the project and why; then Konstruct's own components. `--json` (see [Writing a Plugin](../guide/plugins.md)) |

Tables and columns can be named as `orders`, `main.orders`, `orders.amount`, `jaffle_shop.main.orders.amount`, `ecom.raw_orders` for a dbt source, `dag_id.task_id` for a task, or by dbt unique id. When nothing matches, Konstruct suggests the three closest names, by the words they share or, for a misspelling, by spelling, with a search to try; when a name is ambiguous, it lists the candidates.

Every command takes `-C DIR` (before or after the command name) to work on another directory, `--no-plugins` to run no plugin whatever `konstruct.toml` enables, and `--trust-config` to trust the project for that command, as `KONSTRUCT_TRUST_CONFIG=1` does; query commands also find `konstruct-out` in any parent directory. Query commands build the graph if there is none, warn when inputs changed since the last build, and take `--refresh` to update it first, `--json` for structured output and `--graph FILE` to read a specific graph. Exit status is 0 on success, 1 on an error, 2 on a usage error and 3 when `path` finds no connection. `konstruct --help` lists the commands by what they are for, and `konstruct <command> --help` ends with examples.

## Output

Every result starts with a line naming what it is about and a one-line summary, then sections of aligned tables; `impact` and `trace` draw a tree whose edges say how each value is derived.

| Where the output goes | What it looks like |
|-----------------------|--------------------|
| A terminal | Box-drawing connectors and colour: bold headings, the subject in cyan, green, yellow and red for state, faint text for what is secondary, and SQL keywords and literals picked out. Only the sixteen standard ANSI colours are used, so the terminal's theme keeps them readable on a light or dark background. Tables fit the window: less important columns are left out first, then long values are cut with an ellipsis; summaries and formulas wrap |
| A pipe or a file, or `TERM=dumb` | Plain ASCII text with no escape codes, no truncation and no wrapping, one record per line, so it is stable for `grep`, diffs and coding agents. It is the same text as `str(result)` in Python |
| `--json` | The result's data, unchanged by where it goes |

`NO_COLOR` or `--no-color` turns colour off and keeps the layout; `FORCE_COLOR` turns it on in a pipe. While a build, an update, `verify`, `diff` or `gate` runs, a spinner on stderr shows the stage and how many models have been analysed; it appears only on a terminal, never in CI (where `CI` is set) or with `-q`, and `-v` prints the stages as lines instead. Progress, warnings and errors go to stderr, so stdout holds only the result. An error says what went wrong on its first line, then why, the names it may have meant, and a command to try.

## Every Command and Option

Generated from the command's own argument definitions.

```{eval-rst}
.. argparse::
   :module: konstruct.interfaces.cli
   :func: parser
   :prog: konstruct
```
