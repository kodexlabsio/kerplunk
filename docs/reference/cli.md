# Command Line

| Command | Purpose |
|---------|---------|
| `konstruct build [path] [-u]` | Build `konstruct-out/`. `-u/--update` reuses cached analyses of anything unchanged (keyed by file hashes, compiled SQL, manifest checksums and the upstream columns each model sees). `--no-dbt`, `--no-warehouse`, `--dialect`, `--out`, `--json` (a machine-readable summary), `--strict` (exit 1 on warnings) |
| `konstruct status` | What Konstruct finds (configuration, dbt projects and whether their artefacts are current, warehouses, DAG and SQL files), whether the graph is out of date and which inputs changed, and whether the skill is installed |
| `konstruct init` | Write a commented `konstruct.toml` from what discovery found (`--stdout` to print it) |
| `konstruct impact <table.column>` | Everything downstream: derived columns, relations, models that join, filter or group on it, tests, exposures and the DAG tasks that run them |
| `konstruct trace <table.column>` | Everything upstream, each step marked `direct` (passed through or renamed) or `transformed` |
| `konstruct path <a> <b>` | Shortest connection between two tables or columns |
| `konstruct search <words>` | Find nodes by name or description, optionally `--kind model` |
| `konstruct conflicts` | Columns that compute the same business idea in different ways, with the sources, filters, joins and aggregations that differ and the file each is in, ranked by downstream reach. Writes `CONFLICTS.md` and `conflicts.json`. `--format markdown`, `--fail-on-conflict`, `-n N` (see [Finding Conflicting Definitions](../guide/conflicts.md)) |
| `konstruct define <term>` | What a business term means: its definition, owner, status, grain, required filters and the columns that hold it. Takes a term, a synonym or a column (to list the terms that define it). Exit status 3 when no term matches |
| `konstruct explain <table.column>` | How a column is calculated: its expression as SQL with CTEs and aliases inlined, the same formula composed down to source columns, and step by step each column on the way with the joins, filters and grouping that shape its rows (see [Explaining a Column](../guide/explain.md)) |
| `konstruct describe <name>` | Columns, types, descriptions, tests and neighbours of one node; for a task, what it runs and its neighbours in the DAG |
| `konstruct list [kind ...]` | Every node of the given kinds (`models`, `tables`, `sources`, `tasks`, ...; default all relations) |
| `konstruct open` | Open `graph.html` in a browser |
| `konstruct export <format>` | The graph as OpenLineage events, Mermaid, Graphviz DOT or CSV (see [Exporting](../guide/exporting.md)). `-s NAME` for the lineage of one table or column, `--columns` to draw columns, `-o FILE` |
| `konstruct diff [--base REF]` | What the working tree (or `--head REF`) changes compared with the base (default: the merge base with main). Either can be a date, such as `--base 2026-10-01` or `--base "2026-10-01 14:30"`, for the last commit on the current branch before it (a date alone means its start, in local time): columns added, removed or changed with their expressions before and after, changed join and filter conditions, breaking changes, DAG changes, dbt tests, sources and exposures added or removed in YAML, the downstream impact of each, and a column-aware `dbt build --select` that rebuilds only what the change affects. `--format markdown` for a pull request comment, `--fail-on-breaking` for CI |
| `konstruct verify` | Test the column lineage against the data in a local DuckDB warehouse by perturbation; writes `VERIFY.md` and `verify.json` and marks confirmed edges in the graph. `--seed`, `--rows`, `--model`, `--strict` |
| `konstruct query "<question>"` | Free-text question: ranks nodes by its words and returns them with their connections, plus impact or lineage when the question asks for it. There is no language model in Konstruct; the agent reads the result |
| `konstruct install` | Register the `/konstruct` skill (see Agent Integration) |
| `konstruct mcp` | Run the MCP server on stdio |

Tables and columns can be named as `orders`, `main.orders`, `orders.amount`, `jaffle_shop.main.orders.amount`, `ecom.raw_orders` for a dbt source, `dag_id.task_id` for a task, or by dbt unique id. When nothing matches, Konstruct suggests close names; when a name is ambiguous, it lists the candidates.

Every command takes `-C DIR` (before or after the command name) to work on another directory; query commands also find `konstruct-out` in any parent directory. Query commands build the graph if there is none, warn when inputs changed since the last build, and take `--refresh` to update it first, `--json` for structured output and `--graph FILE` to read a specific graph. Output is coloured on a terminal unless `NO_COLOR` is set or `--no-color` is given. Exit status is 0 on success, 1 on an error, 2 on a usage error and 3 when `path` finds no connection.

## Every Command and Option

Generated from the command's own argument definitions.

```{eval-rst}
.. argparse::
   :module: konstruct.cli
   :func: parser
   :prog: konstruct
```
