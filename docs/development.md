# Development

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for the project)
- A stable [Rust toolchain](https://rustup.rs/), 1.88 or newer, for the Rust core; uv fetches maturin itself
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
   `uv sync` also compiles the Rust core, in release mode, into `konstruct/_core.abi3.so` (`_core.pyd` on Windows) in the source tree. uv rebuilds it whenever a file under `src/` changes, the next time `uv sync` or `uv run` runs.

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
| Ossie portability | `test_ossie_conformance` | Ossie's own M0 to M3 portability ladder (`compliance/portability`): each fixture read through a build, written back and graded element by element in the ladder's outcome vocabulary; every element of the qualified fixtures is PORTABLE and the model comes back as authored, and the bare fixtures' metrics are DROPPED_DIAGNOSED |
| Looker | `test_looker` | LookML views: tables (or the view's own name), the columns each field reads (through other fields, dimension group timeframes and the default ones, own-view references, location and duration SQL, measure filters, cycles), refinements, `extends` (added to by refinements, with a child's own table winning) and abstract views, derived tables, Liquid table names and unknown columns as warnings, column impact reaching only the views that read the column, tables only Looker names, and `[looker] paths` |
| Power BI | `test_powerbi` | TMDL tables, descriptions, columns, measures (one line, several lines, with properties and annotations after them), calculated columns, tabs or spaces, CRLF and a byte order mark; warehouse tables from `Schema`/`Item` and `Name`/`Kind` navigation (fenced M, BigQuery, merged tables), shared expressions, native queries in the connector's dialect (server names in brackets, `#(lf)`, `[Query=...]`), Direct Lake entities and incremental refresh; DAX references across tables, measures on measures (a chain of 400 resolved in time), `VAR` names, calculated columns and comments and strings left out; automatic date tables left out; partitions with no table, native queries reading none, duplicate tables and models as warnings; exposures fed column by column with the fields that read each column; impact reaching only them; unknown columns as warnings; tables only read elsewhere growing the columns named; `--update`; `[powerbi] paths`; `konstruct status` and `init` |
| Definitions | `test_definitions` | The definitions.yml format and its errors, terms in the build, `define`, describe, search, query and impact, the glossary, warnings for missing columns and filters (including a renamed bound column in the example), and `diff` on definitions |
| Formulas | `test_explain` | `explain` on dbt and Airflow SQL, and composed formulas evaluated on the data against the final model, row for row; the viewer's formula cache |
| Export | `test_export` | OpenLineage events validated against the OpenLineage JSON schemas; Mermaid, DOT and CSV |
| Scale | `test_scale` | A generated 300-model project: full build and `--update` times |
| Real world | `test_realworld` | dbt Labs' jaffle-shop at a pinned commit (needs network; `KONSTRUCT_REALWORLD=1`) |
| Lineage against data | `test_verify` | `verify` confirms true lineage and catches removed edges, false edges and dropped filters; a differential property test runs generated queries on generated data and requires the static lineage to agree with execution |
| Gate | `test_gate` | `gate` on real git repositories: a safe change passes; a breaking change, a new meaning for an approved term and a new conflicting column fail, each with its file and fix; a conflict the change does not touch does not; the CLI's exit status, JSON and markdown |
| Change review | `test_diff` | `diff` on real git repositories: formatting and CTE renames are not changes, removed columns still read downstream are breaking, conditions, DAGs, two commits, all output formats, and dbt Jinja rendering |
| PySpark | `test_spark` | The SQL a job passes to `spark.sql` (literals, f-strings without placeholders, concatenations, names bound once), names bound more than once and SQL formatted with keyword arguments as warnings, which files are jobs (Databricks notebooks too) and which objects are sessions (Snowpark files apart), `spark_catalog` left out, column lineage of the tables a job writes, `[spark] paths` and `dialect`, `konstruct status` and `init` |
| Dagster | `test_dagster` | The Dagster parser, assets and op jobs joining dbt and warehouse lineage, sensors ordering work, and parity with Dagster's own asset and job graphs (needs Dagster; see the CI job) |
| Databricks | `test_databricks_live` | The connector against a Databricks workspace with Unity Catalog; runs when workspace credentials are set (see the file) |
| Ontology packs | `test_registry` | Every pack loads with definitions and no status; matching by tables, source-only columns and a schema name or loader, singular or plural; an application's own tables matching nothing; binding to a DuckDB warehouse as drafts, with no warnings and no untested cardinality; terms whose tables are missing left out; a project's own term, entity on the same column or synonym winning; `[ontologies] packs`; a pack in two catalogues; GA4's newest daily table, not the intraday or fresh one; two repositories sharing a warehouse |
| Fivetran and Airbyte | `test_loaders` | Which tool landed a table (Fivetran, Airbyte Destinations V2 and V1), a sync task per schema, Airbyte raw tables feeding their namespace, the Platform Connector lineage read from a DuckDB warehouse (source tables and columns, column lineage into landed tables, deleted source tables and other destinations left out, even one landing a schema of the same name), short names still meaning the landed table, complete formulas and unchanged coverage, an unclear destination as a warning, names that need quoting, the query for the older `connector` schema, for Snowflake and for BigQuery, an unreadable lineage schema as a warning, and `[fivetran] lineage` |
| Vocabularies | `test_vocabulary` | SKOS concepts and OWL classes as terms (labels in other languages, synonyms, definitions, broader concepts, classes as entities); merging into `definitions.yml` terms with the project's wording winning; define, describe and the ontology's concept hierarchy; broader terms that are missing or form a cycle; bad files, other formats and a missing rdflib |
| Trino | `test_trino` | The connector against a stub of the client (comments from `system.metadata`, `row` fields, views, servers without column comments, password and JWT authentication, `konstruct.toml` and dbt profiles, samples keeping Trino's types) and, in the Trino job, against a Trino server's memory catalogue (`KONSTRUCT_TEST_TRINO_HOST`) |
| Agent benchmark | `test_agent_benchmark` | The harness on a miniature Spider 2.0-DBT tree with a scripted agent: both arms, the same prompt, the graph and skill only in the Konstruct arm, the database the profile writes, scoring, timeouts that stop what the agent started, refusing an output directory with instructions above it, and resuming |
| Survey | `test_survey` | The public-project survey on local git repositories: a dbt project built through a generated DuckDB profile with file and network access off, one that is not dbt and one that does not clone, a project that cannot write outside its database or read the surveyor's environment, pinning by commit, the timeout, `env_var` profile names, the aggregate summary naming no project, and resuming |
| Rust core | `src/`, `tests/properties.rs` (`cargo test`) | Walks, neighbours, paths and relation names against brute-force scans on generated graphs; search ranking, limits and kinds; FNV-1a 64 against its published vectors and precomputed hashes on every supported architecture |
| Parity | `test_airflow_parity` | The static Airflow parser against Airflow's own DagBag (needs Airflow; see the file for the command) |
| Fuzzing | `fuzz/` | Atheris (coverage-guided) harnesses for the readers of untrusted input: `fuzz_sql.py` (the SQL analysis, in each dialect), `fuzz_python.py` (the Airflow, Dagster and PySpark readers) and `fuzz_yaml.py` (definitions and Ossie files). Each reader must report what it cannot read and never raise. The Fuzz workflow runs each for two minutes on pull requests that change the package and twenty weekly; locally, `uv run --with atheris==3.1.0 python fuzz/fuzz_sql.py -max_total_time=60` (Linux; atheris has wheels for Linux only). A crash is fixed with a test for the input, as in `test_sql` and `test_build` |

The dev dependencies include dbt-core and dbt-duckdb, so the dbt and end-to-end tests build a copy of the example project for real. In CI, `KONSTRUCT_REQUIRE_SERVICES=1` turns a missing service or browser into a failure rather than a skip, and `HYPOTHESIS_PROFILE=ci` runs more generated examples.

## The Rust Core

Konstruct is one package of Python and Rust, built by [maturin](https://www.maturin.rs/) with [PyO3](https://pyo3.rs/). The Python package keeps the whole API; the crate at the repository root (`Cargo.toml`, `src/`) holds the parts that are measurably faster compiled, and Python calls them through the extension module `konstruct._core`, which `konstruct/_core.pyi` describes to mypy. Each part exists once: what moved to Rust was removed from Python.

| File | Contents |
|------|----------|
| `src/graph.rs` | The graph's edges in compressed sparse row form: impact and trace walks, the neighbours `describe` lists, and shortest paths (`konstruct.core.graph`) |
| `src/names.rs` | Relations by every name they answer to, for name resolution (`Graph.resolve`) |
| `src/search.rs` | Ranked word search (`search`, `query` and the hints for names that match nothing) |
| `src/fnv/` | FNV-1a 64, the hasher of the indexes' maps, hand-written in assembly for each supported architecture: x86_64, aarch64, riscv64, powerpc64le, s390x and loongarch64 |
| `src/python.rs` | The PyO3 bindings, built with the `python` feature |
| `tests/properties.rs` | Property tests against brute-force walks, paths and name scans on generated graphs |

The graph builds each index the first time a query needs it and keeps it until the graph changes through `add_node`, `add_edge` or `remove_edge`; code that edits a node or an edge in place calls `Graph.changed()`. Text is folded to lower case in Python before it reaches Rust, so matching folds case exactly as `str.lower` does.

```bash
cargo fmt
cargo clippy --all-targets --all-features -- -D warnings
cargo test                   # unit and property tests, without Python
```

The other architectures' tests run under qemu-user in CI, each in a Debian container with that architecture's cross compiler. To run one locally, for example riscv64, install `qemu-user`, `gcc-riscv64-linux-gnu` and `libc6-dev-riscv64-cross`, then:

```bash
rustup target add riscv64gc-unknown-linux-gnu
CARGO_TARGET_RISCV64GC_UNKNOWN_LINUX_GNU_LINKER=riscv64-linux-gnu-gcc \
CARGO_TARGET_RISCV64GC_UNKNOWN_LINUX_GNU_RUNNER="qemu-riscv64 -L /usr/riscv64-linux-gnu" \
cargo test --target riscv64gc-unknown-linux-gnu
```

To add a function, write it in the crate with its tests, export it in `src/python.rs`, describe it in `konstruct/_core.pyi`, delete the Python it replaces, and measure with `.kodex/scripts/benchmark.py` before and after. `uv build` makes the sdist and a wheel for the machine it runs on.

## The Graph Viewer

graph.html is a Svelte 5 and TypeScript app in `viewer/`, built with Vite into one self-contained file, `konstruct/outputs/viewer.html`, with its script, styles and fonts inlined (the font licences are in `licenses/`). That file is committed, so installing Konstruct needs no Node.js; Konstruct fills its two data placeholders when it writes graph.html.

```bash
cd viewer
pnpm install
mkdir -p dev && cp ../examples/jaffle_shop/konstruct-out/graph.json dev/   # data for the dev server
pnpm dev                     # http://localhost:5173/viewer.html, reloading as you edit
pnpm check                   # svelte-check (strict TypeScript)
pnpm test                    # Vitest: layout, focus, search and the graph model
pnpm build                   # writes ../konstruct/outputs/viewer.html; commit it with the source
```

Pre-commit hooks check formatting and types and rebuild the page when `viewer/` changes, and a CI job fails if the committed build does not match the source. The browser tests (`tests/test_browser.py`) drive the built page in Chromium and fail on any network request.

## Other Commands

- `uv run ruff check`, `uv run ruff format`, `uv run mypy`: lint, format and type check (strict)
- `uv run .kodex/scripts/prepare_example.py`: build the example warehouse
- `vhs .kodex/scripts/demo-cli.tape`: re-record `docs/images/demo-cli.gif`
- `.kodex/scripts/demo_diff_setup.sh && vhs .kodex/scripts/demo-diff.tape`: re-record `docs/images/demo-diff.gif`
- `vhs .kodex/scripts/demo-claude-code.tape`: record the same question through `/konstruct` in Claude Code (needs Claude Code signed in)

## Agent Benchmark

`.kodex/scripts/agent_benchmark.py` runs a coding agent on [Spider 2.0-DBT](https://github.com/xlang-ai/Spider2/tree/main/spider2-dbt), dbt projects on DuckDB each with an instruction and a gold database, in two arms: the project as shipped, and the same project after `konstruct build` and `konstruct install --target .`. Both arms get the same prompt, which carries the benchmark's own hints and does not mention Konstruct. As in the benchmark's protocol, the agent runs `dbt run` itself, and the database the project's dbt profile writes is scored as the agent leaves it, with the benchmark's own `duckdb_match`.

```bash
uv run .kodex/scripts/agent_benchmark.py ~/Spider2/spider2-dbt --agent 'claude -p {prompt} --permission-mode acceptEdits'
```

`{prompt}` in the agent command is replaced by the prompt, quoted. Prepare the benchmark first as its README says (download its databases, run its `setup.py`). Each project copy is its own git repository, and `--out` (by default a new temporary directory) must not be inside a git repository or under a `CLAUDE.md`, `CLAUDE.local.md`, `.claude` or `AGENTS.md`, so the agent reads no instructions from above the project. `--only` runs some instances, `--arms` one arm, `--mcp` also registers the MCP server (pass it to the agent too, such as `--mcp-config .mcp.json`; the baseline copy gets an empty `.mcp.json`, so the same command works in both arms), `--dbt-run` runs `dbt run` once more before scoring, and `--timeout` stops an agent and everything it started; whatever an agent leaves running is stopped before scoring. Each run writes the projects with their logs beside them, `results.jsonl` and `summary.md`; a run that stops resumes with the same options.

## Public Project Survey

`.kodex/scripts/survey.py` builds a list of public dbt projects and reports, in aggregate only, what Konstruct finds in them: models, the share of columns with column lineage, conflicting definitions and their kinds, the size of the ontology, warnings by kind and build time. The list is a text file with one git URL per line, optionally followed by a commit, tag or branch.

```bash
uv run .kodex/scripts/survey.py projects.txt --out survey --deps
```

Each project is cloned and built without its warehouse: the script writes a DuckDB profile under each profile name the project uses, so dbt can produce the manifest. That runs dbt on the project, which renders its Jinja and runs the queries its macros issue, against an empty DuckDB database with file and network access turned off, with a home directory of its own and none of your environment variables but those needed to run and to reach a proxy. `--deps` runs `dbt deps` first, which downloads the packages the project names, so package macros render. Run it in a disposable container with no credentials in its environment. Each project is surveyed in a child process, stopped after `--timeout` seconds (900 by default) and recorded as timed out. The script writes `projects.jsonl`, one line per project, and `summary.md`, which names no project; a run that stops resumes where it left off.

## Commits

Commits follow [Conventional Commits](https://www.conventionalcommits.org/), checked by commitizen and by `.kodex/scripts/check_commit_message.py`, which applies the Kodex Labs rules: a fixed list of scopes, sentence-case subjects, lines of at most 100 characters, no AI attribution lines, and no business or fundraising terms. The scopes are `airflow`, `dagster`, `dbt`, `definitions`, `iceberg`, `sql`, `warehouse`, `build`, `graph`, `html`, `lineage`, `query`, `report`, `cli`, `mcp`, `skill`, `examples`, `readme`, `tests`, `ci`, `config`, `deps`, `deps-dev` and `release`.

## Repository Layout

| Path | Contents |
|------|----------|
| `konstruct/core/` | `graph.py` (graph model, name resolution, impact, trace and path), `sql.py` (sqlglot column lineage, canonical expressions and conditions), `jsonio.py` |
| `konstruct/inputs/` | `dbt.py`, `jinja.py` (the sandboxed dbt renderer), `airflow.py`, `dagster.py`, `spark.py`, `looker.py`, `powerbi.py`, `loaders.py`, `warehouse.py`, `usage.py` (usage from query history) |
| `konstruct/engine/` | `config.py`, `build.py` (discovery, the build and its cache), `worker.py` (SQL analysis in worker processes) |
| `konstruct/semantics/` | `definitions.py` (business definitions), `vocabulary.py` (SKOS and OWL), `ontology.py` (the ontology derived from the graph), `ossie.py` (Apache Ossie models read and written), `registry.py` and `ontologies/` (ontology packs and binding them) |
| `konstruct/analysis/` | Questions over the graph: `query.py` (impact and the rest), `explain.py` (formulas), `conflicts.py`, `diff.py` (change review), `gate.py`, `verify.py` (lineage against data), `history.py` |
| `konstruct/outputs/` | `report.py` (REPORT.md), `html.py` (graph.html), `export.py` (the export formats), and `viewer.html`, the built graph viewer (generated by `pnpm build`; do not edit) |
| `konstruct/interfaces/` | `project.py` (the Python API), `cli.py`, `console.py`, `status.py`, `mcp_server.py`, `skill.py` |
| `Cargo.toml`, `src/` | The Rust core: graph walks and paths, name resolution, search and the FNV-1a hasher, built into `konstruct._core` |
| `viewer/` | The graph viewer's source: Svelte 5 and TypeScript, built with Vite |
| `action.yml` | The GitHub Action |
| `examples/jaffle_shop` | dbt Labs' jaffle_shop_duckdb (Apache-2.0, see its NOTICE) with an Airflow DAG |
| `.kodex/scripts` | Project scripts: example preparation, test services, commit rules, benchmarks, demo tapes |
| `fuzz/` | Atheris fuzz harnesses for the readers of untrusted input (see the test table) |

## CI/CD

Build and Test runs on every push to `main` and on pull requests: Ruff and mypy, `cargo fmt`, Clippy and the Rust tests, the Rust tests again on aarch64, riscv64, ppc64le, s390x and loongarch64 under qemu, a wheel for each platform built with maturin (Linux on all six architectures, macOS and Windows on x86_64 and arm64), a commit message check on pull requests, the test suite on Python 3.11, 3.12, 3.13 and 3.14 against Postgres and Iceberg REST catalog service containers, an end-to-end build of the jaffle_shop example that fails if it takes a minute or more, an install of the built wheel with `uv tool install`, an install from GitHub exactly as this README describes, the real-world jaffle-shop test, the Airflow parity test, and the Trino connector against a Trino server. The test job also runs the browser tests in Chromium and fails below 90% branch coverage. main is protected, and nothing pushes to it: a release is a tag and a GitHub release. Changes accumulate on a branch and merge in batches, about once an hour, by rebase, so each commit stays on `main`. The batch's last commit prepares the release with `uv run python .kodex/scripts/prepare_release.py`, which reads every releasable commit on the branch since the last tag and sets the next version in `pyproject.toml`, `uv.lock` and `konstruct/__init__.py`, with its section at the top of `CHANGELOG.md`. While the version is 0.x a release is a patch, or the next minor for a breaking change; the script and the workflow both refuse 1.0. The Release preparation job checks on every pull request that a prepared release still matches `main`. Create Release runs after Build and Test passes on a push to `main`: it tags a tested commit whose version has no tag; otherwise, when `main` has releasable commits since the last tag (a Dependabot fix, say), it writes the next version on a commit above `main` and tags that. It pushes only tags, after the build succeeds, finishes a release left half done, moves the `v0` tag (or re-points it when it lags), and publishes a GitHub release with the changelog section and sdist, a CycloneDX SBOM (`uv export --format cyclonedx1.5`) and the Sigstore bundle of a build provenance attestation for both (`actions/attest-build-provenance`, also recorded with GitHub artifact attestations), then builds the wheels from the tag and attaches them with an attestation of their own. Run by hand from the Actions tab, it only finishes or tags a prepared version. Publishing to PyPI is a separate job that stays off until the `PUBLISH_TO_PYPI` repository variable is `true` and a `pypi` environment with trusted publishing exists.

Every action in the workflows and in `action.yml` is pinned to a full commit SHA, with its version in a comment, and the service containers to image digests, so a run is reproducible and a moved tag cannot change what CI executes. Dependabot moves the action SHAs; the image digests, which carry the tag they track, are updated by hand. OpenSSF Scorecard runs weekly and on every push to `main`, and publishes its results to the repository's Security tab.

The zizmor workflow audits the workflows, `action.yml`, the Dependabot configuration and the pre-commit hooks with [zizmor](https://docs.zizmor.sh/) on every pull request (offline) and, with its online audits, on every push to `main` and weekly, and fails on any finding; a finding that is a deliberate choice carries a `# zizmor: ignore[<audit>]` comment beside it that says why. Its version is pinned in the workflow and updated by hand.

CodeQL scans the Python package, the viewer's TypeScript, the workflows and `action.yml` (and the Rust core once its sources are in the repository) with the `security-extended` queries, weekly and on every pull request and push to `main`, and reports to code scanning. It uses an advanced setup, so code scanning's default setup stays off for the repository.

The Dependency audit workflow checks every lock file in the repository (`uv.lock`, `viewer/pnpm-lock.yaml`, and a `Cargo.lock` wherever one is) against the OSV database with [osv-scanner](https://google.github.io/osv-scanner/), daily and on every pull request and push to `main`, and fails on a known vulnerability. A vulnerability that does not affect Konstruct is ignored in the `osv-scanner.toml` beside its lock file, with the reason and a date to look at it again.

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
