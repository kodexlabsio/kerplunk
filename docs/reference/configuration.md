# Configuration

Configuration is optional. Put it in `konstruct.toml` at the project root, or under `[tool.konstruct]` in `pyproject.toml`. `${VAR}` and `${VAR:-default}` read the environment, so credentials stay out of the file.

```toml
dialect = "postgres"              # default: the dbt adapter, then the first warehouse
default_catalog = "analytics"     # for unqualified tables in Airflow and SQL files
default_schema = "public"
exclude = ["scratch/**"]
out_dir = "konstruct-out"
repositories = ["../marketing"]   # other repositories built into the same graph

[dbt]
projects = ["transform"]          # default: every dbt_project.yml found
run = true                        # run `dbt docs generate` when artefacts are missing or stale

[airflow]
dags = ["orchestration/dags"]     # default: Python files that look like DAGs

[sql]
paths = ["sql/**/*.sql"]          # default: .sql files outside dbt folders and not used by DAGs

[definitions]
paths = ["definitions.yml"]       # business terms; default: every definitions.yml

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

[[warehouse]]
type = "iceberg"
name = "lake"                     # the catalogue name SQL uses, e.g. lake.sales.orders
uri = "http://localhost:8181"
namespaces = ["sales"]
[warehouse.properties]            # passed to pyiceberg.catalog.load_catalog
credential = "${ICEBERG_CREDENTIAL}"
```
