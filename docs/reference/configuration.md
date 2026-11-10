# Configuration

Configuration is optional. Put it in `konstruct.toml` at the project root, or under `[tool.konstruct]` in `pyproject.toml`. `${VAR}` and `${VAR:-default}` read the environment, so credentials stay out of the file. Warehouses, and paths that lead outside the project, are used only once the project is trusted, and Konstruct runs dbt only in a trusted project (see [Trusting a Project's Connections](#trusting-a-projects-connections)).

```toml
dialect = "postgres"              # default: the dbt adapter, then the first warehouse
default_catalog = "analytics"     # for unqualified tables in Airflow and SQL files
default_schema = "public"
exclude = ["scratch/**"]
out_dir = "konstruct-out"
repositories = ["../marketing"]   # other repositories built into the same graph

[dbt]
projects = ["transform"]          # default: every dbt_project.yml found
run = true                        # run `dbt docs generate` when artefacts are missing or stale (trusted projects only)

[airflow]
dags = ["orchestration/dags"]     # default: Python files that look like DAGs

[spark]
paths = ["jobs/"]                 # PySpark jobs; default: Python files that use spark.sql
dialect = "spark"                 # the SQL dialect of their spark.sql strings (or "databricks")

[looker]
paths = ["lookml/"]               # LookML; default: every *.lkml file

[powerbi]
paths = ["reports/Sales.SemanticModel"]  # TMDL; default: every *.tmdl file

[sql]
paths = ["sql/**/*.sql"]          # default: .sql files outside dbt folders and not used by DAGs

[definitions]
paths = ["definitions.yml"]       # business terms; default: every definitions.yml

[ontologies]
packs = ["stripe", "shopify"]     # ontology packs to bind; default: all; [] for none

[vocabularies]
paths = ["vocabulary/business.ttl"]  # SKOS or OWL, read as terms; needs konstruct[rdf]

[ossie]
paths = ["semantic/"]             # Apache Ossie models; default: every *.ossie.yaml

[plugins]
enabled = ["konstruct-yaml-pipelines"]  # installed plugins this project runs; default: none

[fivetran]
lineage = false                   # read the Platform Connector's lineage tables; default true

[usage]                           # column usage from query history; off by default
enabled = true
days = 90
exclude_users = ["DBT_SERVICE"]

[[warehouse]]                     # setting any warehouse turns discovery off
type = "postgres"
dsn = "${WAREHOUSE_DSN}"
schemas = ["raw", "analytics"]

[[warehouse]]
type = "duckdb"
path = "data/local.duckdb"

[[warehouse]]                     # needs konstruct[snowflake]; options go to snowflake.connector.connect
type = "snowflake"
account = "acme-xy12345"
user = "${SNOWFLAKE_USER}"
password = "${SNOWFLAKE_PASSWORD}"
database = "ANALYTICS"
databases = ["RAW", "ANALYTICS"]  # optional: more than one database

[[warehouse]]                     # needs konstruct[bigquery]; uses application default credentials
type = "bigquery"
project = "my-project"
datasets = ["shop", "marts"]

[[warehouse]]                     # needs konstruct[databricks]
type = "databricks"
server_hostname = "${DATABRICKS_HOST}"
http_path = "${DATABRICKS_HTTP_PATH}"
access_token = "${DATABRICKS_TOKEN}"
catalogs = ["main"]

[[warehouse]]                     # needs konstruct[trino]; Starburst too
type = "trino"                    # other options are passed to trino.dbapi.connect
host = "trino.example.com"
port = 443
user = "me"
password = "${TRINO_PASSWORD}"    # or token = "${TRINO_JWT}"; either one means https
catalog = "lake"                  # default for catalogs
schema = "sales"                  # for unqualified tables
catalogs = ["lake", "hive"]

[[warehouse]]
type = "iceberg"
name = "lake"                     # the catalogue name SQL uses, e.g. lake.sales.orders
uri = "http://localhost:8181"
namespaces = ["sales"]
[warehouse.properties]            # passed to pyiceberg.catalog.load_catalog
credential = "${ICEBERG_CREDENTIAL}"
```

## Trusting a Project's Connections

A repository's own `konstruct.toml` and `profiles.yml` can name a warehouse, read environment variables into its connection, and point Konstruct at paths outside the project, and running dbt runs the repository's macros and SQL. Konstruct uses those settings, and runs dbt, only once you trust the project:

```bash
konstruct trust             # after reviewing konstruct.toml and profiles.yml
konstruct trust --revoke    # stop trusting them
```

`konstruct trust` records a SHA-256 of the settings that carry connections and paths: the `[[warehouse]]` and `[usage]` tables in `konstruct.toml` (or `[tool.konstruct]`), each path setting that leads outside the project together with where it leads, every `profiles.yml` in the repository that a dbt project reads, and the same for each repository listed in `repositories`. The path settings are `out_dir`, `repositories`, `[dbt] projects`, `[airflow] dags`, and `paths` under `[dagster]`, `[spark]`, `[looker]`, `[powerbi]`, `[definitions]`, `[vocabularies]`, `[ossie]` and `[sql]`; a path leads where its symbolic links resolve, and a `[sql]` glob is judged by its part before the first wildcard. It prints the files it covers, the environment variables they read and the dbt projects it lets Konstruct run dbt in. The record is kept outside the repository, in `$XDG_CONFIG_HOME/konstruct/trusted.json`; without `XDG_CONFIG_HOME`, in `~/.config/konstruct/` (`~/Library/Application Support/konstruct/` on macOS, `%APPDATA%\konstruct\` on Windows). A change to any of those settings no longer matches the record, so the project is untrusted again until you review it and run `konstruct trust` once more. Other settings, such as `exclude`, and paths inside the project can change freely. `konstruct status` shows whether the project is trusted.

The record does not cover the dbt project's models, macros and hooks. Trusting a project lets Konstruct run `dbt docs generate` in it, which runs the repository's Jinja and SQL with your environment and your profile's credentials, now and after any later change to that code. Trust a project only when you would run dbt in it yourself.

Until the project is trusted:

| Setting | What Konstruct does |
|---------|---------------------|
| A warehouse in `konstruct.toml` or in the repository's `profiles.yml` | Skips it without reading the environment variables it names, and warns once per build: ``the project is not trusted; run `konstruct trust` after reviewing it``, naming what it skipped. The build still succeeds from the files. A DuckDB file inside the project is read |
| A DuckDB `path` outside the project, or one that reads the environment | Skips it, with the same warning |
| Running dbt | Never runs dbt, whatever the profile names. The build reads the `target/manifest.json` already there, even when it is older than the models, and without one warns that dbt was not run and leaves that dbt project out of the graph. Run `dbt docs generate` yourself, or trust the project |
| A path setting that leads outside the project | Stops with an error that names the setting. `--out` sets the output directory instead |
| A `konstruct-out` that is a symbolic link leading outside the project | Stops with an error. `--out` sets the output directory instead |
| A file found in an input directory, discovered or configured, that leads outside the project through a symbolic link, including dbt artefacts, macros and Airflow SQL templates | Does not read it, and warns once for each directory that holds such links |
| `[usage] enabled = true` | Does not read query history, with the same warning as for a warehouse |
| `konstruct verify` without `--warehouse` | Uses only a DuckDB file or Postgres database that the last build read and that the configuration still allows under the trust recorded now |

Each file Konstruct writes in the output directory replaces what is there, trusted or not, so a symbolic link committed in `konstruct-out` is replaced rather than followed. A DuckDB file that discovery finds in the project is never covered by the record, so it is read only when it is inside the project, trusted or not; a symbolic link that leads outside is skipped with a warning. To read such a file, name it under `[[warehouse]]` and trust the project.

Your own settings are always trusted: `~/.dbt/profiles.yml`, a `profiles.yml` in a `DBT_PROFILES_DIR` outside the project (a relative `DBT_PROFILES_DIR` is read from the dbt project's directory, as dbt reads it), and options given on the command line or to `Project.build`. They connect to warehouses in an untrusted project, but do not make Konstruct run dbt there.

In CI on a branch whose content is already trusted, such as the default branch after review, `KONSTRUCT_TRUST_CONFIG=1` or `--trust-config` trusts the project without a record. Never set either for a pull request from a fork, or for any branch whose `konstruct.toml` and `profiles.yml` have not been reviewed: the change under review would choose the connections and the environment variables sent to them. To read a warehouse for pull requests, have the workflow write the profile outside the checkout and point `DBT_PROFILES_DIR` at it; that profile is trusted without trusting the repository.

## Environment

| Variable | Effect |
|----------|--------|
| `KONSTRUCT_NO_PLUGINS` | `1` runs no plugin, whatever `[plugins] enabled` says, as `--no-plugins` does (see [Writing a Plugin](../guide/plugins.md)) |
| `KONSTRUCT_TRUST_CONFIG` | `1` trusts every project, as `--trust-config` does (see [Trusting a Project's Connections](#trusting-a-projects-connections)) |
| `XDG_CONFIG_HOME` | Where `konstruct trust` keeps its record, in `konstruct/trusted.json` |
| `KONSTRUCT_WORKERS` | How many worker processes analyse dbt models in a build: by default one per CPU, up to eight; `1` analyses them in the build's own process |
