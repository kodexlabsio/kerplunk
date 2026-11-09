# Security

## Reporting a Vulnerability

Please report vulnerabilities privately, with GitHub's **Report a vulnerability** button on the [Security tab](https://github.com/kodexlabsio/konstruct/security) of this repository, rather than in a public issue. Include the version (`konstruct --version`), what an attacker controls (a SQL file, a DAG file, a dbt project, a graph.json, warehouse metadata), and what they can achieve.

You should receive a reply within five working days. Fixes are released as a patch version and noted in the [changelog](https://github.com/kodexlabsio/konstruct/blob/main/CHANGELOG.md) and the GitHub release.

## Supported Versions

Konstruct is at 0.x: only the latest release receives fixes.

## Verifying a Release

Each GitHub release has a signed build provenance attestation for its wheel, sdist and CycloneDX SBOM, made by the release workflow. To check a downloaded file:

```bash
gh attestation verify konstruct-<version>-py3-none-any.whl -R kodexlabsio/konstruct
```

The attestation's Sigstore bundle is attached to the release as `konstruct-<version>.intoto.jsonl`, for `gh attestation verify --bundle` and other Sigstore verifiers.

## Trust Model

Konstruct reads a repository and, if configured, a warehouse. It is built to be safe to run on a repository you have not reviewed, such as a pull request branch in CI.

| Input | What Konstruct does with it |
|-------|----------------------------|
| Airflow DAG and Dagster definition files | Parses them with Python's `ast` module. It never imports or runs them. |
| dbt Jinja | Renders it in a `jinja2` `SandboxedEnvironment`, with dbt's context replaced by inert stand-ins. Macros that would query the warehouse (`run_query`, `statement`) return empty results. |
| SQL | Parses it with sqlglot. It never sends it to a warehouse, except under `konstruct verify` (below). |
| dbt itself | `konstruct build` runs `dbt docs generate` when a project's artefacts are missing or stale and `dbt` is on `PATH`. That runs the project's own macros, with the project's own profile. Pass `--no-dbt` (or set `run = false` under `[dbt]` in `konstruct.toml`) to never run dbt. The GitHub Action passes `--no-dbt` by default, so a pull request's macros never run with the workflow's secrets. |
| Warehouses | Reads metadata only (`information_schema` and catalog APIs), with the credentials in the dbt profile or `konstruct.toml`. `--no-warehouse` reads none. |
| `konstruct verify` | Copies each model's inputs into an in-memory DuckDB, or works inside a Postgres transaction that is always rolled back, and reruns the model's SQL there, so nothing it does is committed to the warehouse. It is the only command that runs a model's SQL. |
| `konstruct diff` | Runs `git` to read files at the base commit. It does not check them out or run anything from them. A base or head that starts with `-` is refused, so it cannot pass git an option. |
| graph.html | A single file that loads nothing from the network. Names, descriptions and SQL from the graph are inserted as text (`textContent`), never as HTML, and the embedded JSON is escaped so it cannot close its script tag. Tests open it with hostile names and descriptions in a real browser. |
| The MCP server | Runs on stdio for the agent that started it. Its tools are read-only queries over the built graph. |

Konstruct collects no telemetry. Apart from warehouse connections you configure, `konstruct build` makes no network requests.
