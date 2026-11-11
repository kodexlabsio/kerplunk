# Outputs and the Graph Format

| File | Contents |
|------|----------|
| `graph.json` | Nodes (`source`, `table`, `column`, `model`, `seed`, `snapshot`, `dag`, `task`, `test`, `exposure`, `term`) and edges (below), with descriptions, types, file paths and the inputs each came from |
| `REPORT.md` | Summary and coverage, the most queried columns (with usage on), business terms, the most used columns, orphans, untested models, columns with no lineage, columns documented in dbt but missing from the warehouse, and warnings |
| `ontology.yml` | The entities, measures, relationships and rules derived from the graph (see [The Ontology](../guide/ontology.md)); `konstruct verify` refreshes its verified counts |
| `vectors.bin` | The index of search by meaning and every node's blocks, written by the first search by meaning and brought up to date by the first after each build (see [Searching by Meaning](../guide/search.md)) |
| `graph.html` | A single offline file (open it with `konstruct open`): tables, models and tasks in dependency order; cards that expand to their columns; column lineage drawn between columns, isolated and fitted to the screen; each column's formula in terms of its sources; the conditions that shape each model; verification results; search, deep links to any table or column, dark and light themes |
| `explain.json` | What `graph.html` holds beside the graph (formulas, SQL, the ontology and conflicts), written with it, for the viewer when `konstruct serve` serves it |

| Edge | Direction | Meaning |
|------|-----------|---------|
| `contains` | relation to column | The column belongs to the relation |
| `feeds` | upstream to downstream | Relation to relation, task to the table it writes, table to the task that reads it. With `role: reference`, a column the target uses in a JOIN, WHERE, GROUP BY, HAVING or QUALIFY |
| `derived_from` | column to the column it comes from | Column-level lineage, with `transform: direct` or `transformed` |
| `runs` | DAG to task, task to dbt node | What a DAG or task executes |
| `precedes` | task to task | Airflow ordering, Dagster op and asset ordering, and sensors that start one job after another |
| `tests` | test to relation or column | What a dbt test checks |
| `defines` | term to column or relation | The column that holds a business term |
| `relates_to` | term to term | Related business terms; with `role: broader`, the wider concept the term is a kind of |

With **Columns** on, graph.html is a column-level map of the whole project:

![graph.html with every table expanded to its columns and column lineage drawn between them](../images/graph-columns.png)

## Compatibility

Scripts, agents and CI that read Konstruct's outputs keep working from one release to the next.
These are the interfaces Konstruct keeps stable:

- `graph.json`, `conflicts.json` and `verify.json` in `konstruct-out/`, and `ontology.yml`;
- the `--json` output of every command, and `--format json` and `--json-file` of `diff` and `gate`;
- the MCP tools: their names, their inputs and the shape of their results;
- the command line: commands, options, choices and exit statuses;
- the Python API: the names `konstruct` exports and the methods of {class}`konstruct.Project`;
- the keys of `konstruct.toml` (and `[tool.konstruct]` in `pyproject.toml`) that Konstruct names;
  a Snowflake, Databricks or Trino warehouse's other keys go to its driver as they are, and follow
  the driver;
- the GitHub Action's inputs and outputs, and the columns of the CSV export, in their order.

A release can add commands, options, fields, tools and keys, make a required argument optional,
and let an argument or parameter take more types. Removing or renaming one, or changing
what it means, is a breaking change: the changelog lists it under **BREAKING CHANGE**, and while
the version is 0.x it raises the minor version (0.12.x to 0.13.0). Every other release is a patch,
so `konstruct>=0.12.2,<0.13` takes every release without a breaking change. A patch release can
still find more lineage, or correct lineage it had wrong: the shape of each answer stays the same,
its content follows the code.

### Format Versions

Every JSON document Konstruct writes for programs carries `format_version`, an integer, as its
first field: `graph.json`, each command's `--json` output and the MCP tool of the same name, and
`conflicts.json` and `verify.json`. A document's format version changes only with a breaking change
to its shape, so a reader that checks it, and ignores fields it does not know, reads every later
document of that format. `ontology.yml`, and the ontology's JSON, also carry `version`, the
ontology's format from before `format_version`, which follows the same rule.

Konstruct reads every `graph.json` an earlier release wrote (one without `format_version` is format
1). It refuses one written by a newer Konstruct, with a message that says to upgrade, rather than
misread it; `konstruct status` reports such a graph rather than failing.

| Document | `format_version` |
|----------|------------------|
| `graph.json` | 1 |
| `build --json` | 1 |
| `status --json` | 1 |
| `impact` and `trace` | 1 |
| `path` | 1 |
| `search` | 1 |
| `describe` | 1 |
| `list` | 1 |
| `query` | 1 |
| `define` | 1 |
| `ontology` | 1 |
| `explain` | 1 |
| `history` | 1 |
| `conflicts` and `conflicts.json` | 1 |
| `verify` and `verify.json` | 1 |
| `diff` | 1 |
| `gate` | 1 |
| `check-sql` | 1 |
| `check-sql --history` | 1 |

The exports follow their own specifications: OpenLineage events carry their `schemaURL`, and an
Apache Ossie model its Ossie `version`. Mermaid and Graphviz DOT are drawn for people, and their
text can change in any release.
