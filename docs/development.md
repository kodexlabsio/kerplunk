# Development

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for the project)
- Docker, for the Postgres and Iceberg integration tests
- Node.js 24 (see `.nvmrc`) and [pnpm](https://pnpm.io/), only to work on the graph viewer
- [vhs](https://github.com/charmbracelet/vhs), only to re-record the demo GIFs

## Installation

1. **Clone the repository**
   ```bash
   git clone git@github.com:kodexlabsio/konstruct.git
   cd konstruct
   ```

2. **Install dependencies and the git hooks**
   ```bash
   uv sync
   uv run pre-commit install
   ```
   The hooks run Ruff and mypy before each commit (and the viewer checks when `viewer/` changed), check the commit message, and run the tests before each push.

## Tests

```bash
uv run playwright install chromium              # once, for the browser tests
uv run pytest                                   # Postgres and Iceberg REST tests skip without services
eval "$(.kodex/scripts/services.sh up)"         # start them in Docker and export their addresses
uv run pytest --cov                             # everything, with branch coverage (fails under 90%)
.kodex/scripts/services.sh down
```

| Kind | Where | What it checks |
|------|-------|----------------|
| Unit | `test_graph`, `test_sql`, `test_dbt`, `test_airflow`, `test_report`, `test_skill`, ... | Each module, including lineage through CTEs, joins, aliases, `SELECT *`, unions and window functions |
| Property-based | `test_properties` | Invariants on generated inputs with Hypothesis: impact and trace are inverse, lineage matches generated CTE chains in seven dialects, Airflow dependencies match generated DAGs, no text crashes the SQL analysis |
| Integration | `test_warehouse`, `test_build`, `test_cloud_warehouses` | DuckDB, Postgres and Iceberg (SQL and REST catalogs) for real; Snowflake against fakesnow and BigQuery against its emulator; Databricks against a stub; builds of projects with and without dbt |
| End to end | `test_build`, `test_cli`, `test_query`, `test_mcp` | The example built with real dbt, every CLI command, the Python API, and the MCP server in process and over stdio |
| Golden files | `test_golden`, `tests/golden/` | The exact text of the CLI output and REPORT.md; regenerate with `KONSTRUCT_UPDATE_GOLDEN=1` and review the diff |
| Browser | `test_browser` | graph.html in Chromium: selection, column rows and edges, formulas, deep links, isolate, direction, filters, themes, search, hostile text, a 400-relation graph |
| Usage | `test_usage` | The SQL sent to Snowflake, BigQuery and Databricks, query text parsing for BigQuery, usage on columns, in the report and describe, the daily cache, off by default, and missing permissions as a warning, against stub drivers in each vendor's row shapes |
| Conflicts | `test_conflicts` | Two revenue definitions that differ by one filter (the pair, the filter and its file), different sources, ranking, grouping by business terms, copies left out, no false conflicts on the example |
| Ontology | `test_ontology` | Entity names and keys from unique tests, primary keys, identifier fields and grains; terms that name entities; measures and their formulas; relationships, cardinality and `tested`; every rule; the same output whatever the graph's order; the build file, the CLI and the example project |
| Ossie | `test_ossie` | Both Ossie shapes, the build input and its warnings, composite keys and relationships, data types, and an export validated against the Ossie schema, including a metric read from Ossie and written back |
| Definitions | `test_definitions` | The definitions.yml format and its errors, terms in the build, `define`, describe, search, query and impact, the glossary, warnings for missing columns and filters (including a renamed bound column in the example), and `diff` on definitions |
| Formulas | `test_explain` | `explain` on dbt and Airflow SQL, and composed formulas evaluated on the data against the final model, row for row; the viewer's formula cache |
| Export | `test_export` | OpenLineage events validated against the OpenLineage JSON schemas; Mermaid, DOT and CSV |
| Scale | `test_scale` | A generated 300-model project: full build and `--update` times |
| Real world | `test_realworld` | dbt Labs' jaffle-shop at a pinned commit (needs network; `KONSTRUCT_REALWORLD=1`) |
| Lineage against data | `test_verify` | `verify` confirms true lineage and catches removed edges, false edges and dropped filters; a differential property test runs generated queries on generated data and requires the static lineage to agree with execution |
| Change review | `test_diff` | `diff` on real git repositories: formatting and CTE renames are not changes, removed columns still read downstream are breaking, conditions, DAGs, two commits, all output formats, and dbt Jinja rendering |
| Dagster | `test_dagster` | The Dagster parser, assets and op jobs joining dbt and warehouse lineage, sensors ordering work, and parity with Dagster's own asset and job graphs (needs Dagster; see the CI job) |
| Databricks | `test_databricks_live` | The connector against a Databricks workspace with Unity Catalog; runs when workspace credentials are set (see the file) |
| Parity | `test_airflow_parity` | The static Airflow parser against Airflow's own DagBag (needs Airflow; see the file for the command) |

The dev dependencies include dbt-core and dbt-duckdb, so the dbt and end-to-end tests build a copy of the example project for real. In CI, `KONSTRUCT_REQUIRE_SERVICES=1` turns a missing service or browser into a failure rather than a skip, and `HYPOTHESIS_PROFILE=ci` runs more generated examples.

## The Graph Viewer

graph.html is a Svelte 5 and TypeScript app in `viewer/`, built with Vite into one self-contained file, `konstruct/viewer.html`, with its script, styles and fonts inlined (the font licences are in `licenses/`). That file is committed, so installing Konstruct needs no Node.js; Konstruct fills its two data placeholders when it writes graph.html.

```bash
cd viewer
pnpm install
mkdir -p dev && cp ../examples/jaffle_shop/konstruct-out/graph.json dev/   # data for the dev server
pnpm dev                     # http://localhost:5173/viewer.html, reloading as you edit
pnpm check                   # svelte-check (strict TypeScript)
pnpm test                    # Vitest: layout, focus, search and the graph model
pnpm build                   # writes ../konstruct/viewer.html; commit it with the source
```

Pre-commit hooks check formatting and types and rebuild the page when `viewer/` changes, and a CI job fails if the committed build does not match the source. The browser tests (`tests/test_browser.py`) drive the built page in Chromium and fail on any network request.

## Other Commands

- `uv run ruff check`, `uv run ruff format`, `uv run mypy`: lint, format and type check (strict)
- `uv run .kodex/scripts/prepare_example.py`: build the example warehouse
- `vhs .kodex/scripts/demo-cli.tape`: re-record `docs/images/demo-cli.gif`
- `.kodex/scripts/demo_diff_setup.sh && vhs .kodex/scripts/demo-diff.tape`: re-record `docs/images/demo-diff.gif`
- `vhs .kodex/scripts/demo-claude-code.tape`: record the same question through `/konstruct` in Claude Code (needs Claude Code signed in)

## Commits

Commits follow [Conventional Commits](https://www.conventionalcommits.org/), checked by commitizen and by `.kodex/scripts/check_commit_message.py`, which applies the Kodex Labs rules: a fixed list of scopes, sentence-case subjects, lines of at most 100 characters, no AI attribution lines, and no business or fundraising terms. The scopes are `airflow`, `dagster`, `dbt`, `definitions`, `iceberg`, `sql`, `warehouse`, `build`, `graph`, `html`, `lineage`, `query`, `report`, `cli`, `mcp`, `skill`, `examples`, `readme`, `tests`, `ci`, `config`, `deps`, `deps-dev` and `release`.

## Repository Layout

| Path | Contents |
|------|----------|
| `konstruct/graph.py` | Graph model, name resolution, impact, trace and path |
| `konstruct/sql.py` | sqlglot column lineage, canonical expressions and conditions |
| `konstruct/dbt.py`, `jinja.py`, `airflow.py`, `dagster.py`, `warehouse.py`, `definitions.py` | Inputs, the sandboxed dbt renderer and business definitions |
| `konstruct/ontology.py`, `ossie.py` | The ontology derived from the graph, and Apache Ossie models read and written |
| `konstruct/config.py`, `build.py`, `usage.py` | Configuration, discovery, the build and its cache, and usage from query history |
| `konstruct/query.py`, `explain.py`, `conflicts.py`, `diff.py`, `verify.py` | Questions over the graph: impact and the rest, formulas, conflicting definitions, change review, lineage against data |
| `konstruct/report.py`, `html.py`, `export.py` | REPORT.md, writing graph.html, and the export formats |
| `konstruct/viewer.html` | The built graph viewer (generated by `pnpm build`; do not edit) |
| `viewer/` | The graph viewer's source: Svelte 5 and TypeScript, built with Vite |
| `konstruct/project.py` | The Python API |
| `konstruct/cli.py`, `console.py`, `status.py`, `mcp_server.py`, `skill.py` | Interfaces |
| `action.yml` | The GitHub Action |
| `examples/jaffle_shop` | dbt Labs' jaffle_shop_duckdb (Apache-2.0, see its NOTICE) with an Airflow DAG |
| `.kodex/scripts` | Project scripts: example preparation, test services, commit rules, benchmark, demo tapes |

## CI/CD

Build and Test runs on every push to `main` and on pull requests: Ruff and mypy, a commit message check on pull requests, the test suite on Python 3.11, 3.12, 3.13 and 3.14 against Postgres and Iceberg REST catalog service containers, an end-to-end build of the jaffle_shop example that fails if it takes a minute or more, an install of the built wheel with `uv tool install`, an install from GitHub exactly as this README describes, the real-world jaffle-shop test, and the Airflow parity test. The test job also runs the browser tests in Chromium and fails below 90% branch coverage. main is protected, and nothing pushes to it: a release is a tag and a GitHub release. Changes accumulate on a branch and merge in batches, about once an hour, by rebase, so each commit stays on `main`. The batch's last commit prepares the release with `uv run python .kodex/scripts/prepare_release.py`, which reads every releasable commit on the branch since the last tag and sets the next version in `pyproject.toml`, `uv.lock` and `konstruct/__init__.py`, with its section at the top of `CHANGELOG.md`. While the version is 0.x a release is a patch, or the next minor for a breaking change; the script and the workflow both refuse 1.0. The Release preparation job checks on every pull request that a prepared release still matches `main`. Create Release runs after Build and Test passes on a push to `main`: it tags a tested commit whose version has no tag; otherwise, when `main` has releasable commits since the last tag (a Dependabot fix, say), it writes the next version on a commit above `main` and tags that. It pushes only tags, after the build succeeds, finishes a release left half done, moves the `v0` tag (or re-points it when it lags), and publishes a GitHub release with the changelog section, wheel and sdist. Run by hand from the Actions tab, it only finishes or tags a prepared version. Publishing to PyPI is a separate job that stays off until the `PUBLISH_TO_PYPI` repository variable is `true` and a `pypi` environment with trusted publishing exists.

Every action in the workflows and in `action.yml` is pinned to a full commit SHA, with its version in a comment, and the service containers to image digests, so a run is reproducible and a moved tag cannot change what CI executes. Dependabot moves the action SHAs; the image digests, which carry the tag they track, are updated by hand. OpenSSF Scorecard runs weekly and on every push to `main`, and publishes its results to the repository's Security tab.

```mermaid
graph LR
    subgraph "Source Control"
        GitHub[GitHub<br/>Repository]
    end

    subgraph "Build & Test Workflow"
        Lint[Ruff and mypy]
        Commits[Commit messages]
        Tests[Tests<br/>Python 3.11 to 3.14]
        E2E[End to end<br/>jaffle_shop]
        Package[Wheel install]
    end

    subgraph "Services"
        Postgres[Postgres 18]
        Iceberg[Iceberg REST<br/>catalog]
    end

    subgraph "Release Workflow"
        Bump[Tag a prepared version<br/>or release from above main]
        Release[GitHub release]
        PyPI[PyPI<br/>off by default]
    end

    GitHub -->|Push/PR| Lint
    GitHub -->|PR| Commits
    GitHub -->|Push/PR| Tests
    GitHub -->|Push/PR| E2E
    GitHub -->|Push/PR| Package
    Postgres --> Tests
    Iceberg --> Tests
    E2E -->|All pass on main| Bump
    Bump --> Release
    Release --> PyPI

    style GitHub fill:#24292e,stroke:#333,stroke-width:2px,color:#fff
    style Lint fill:#2088ff,stroke:#333,stroke-width:2px,color:#fff
    style Commits fill:#2088ff,stroke:#333,stroke-width:2px,color:#fff
    style Tests fill:#2088ff,stroke:#333,stroke-width:2px,color:#fff
    style E2E fill:#2088ff,stroke:#333,stroke-width:2px,color:#fff
    style Package fill:#2088ff,stroke:#333,stroke-width:2px,color:#fff
    style Postgres fill:#336791,stroke:#333,stroke-width:2px,color:#fff
    style Iceberg fill:#4a5568,stroke:#333,stroke-width:2px,color:#fff
    style Bump fill:#6f42c1,stroke:#333,stroke-width:2px,color:#fff
    style Release fill:#6f42c1,stroke:#333,stroke-width:2px,color:#fff
    style PyPI fill:#f96,stroke:#333,stroke-width:2px
```
