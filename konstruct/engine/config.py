"""Configuration: ``konstruct.toml`` (or ``[tool.konstruct]`` in ``pyproject.toml``) plus discovery.

Everything is optional. Without configuration Konstruct discovers dbt projects, Airflow DAG
files, SQL files, DuckDB files and the warehouse each dbt profile points at.

Example ``konstruct.toml``::

    dialect = "postgres"                  # default: the dbt adapter, else sqlglot's default
    default_catalog = "analytics"         # for unqualified tables in Airflow and SQL files
    default_schema = "public"
    exclude = ["scratch/**"]
    repositories = ["../marketing"]       # other repositories built into the same graph

    [dbt]
    projects = ["transform"]              # default: every dbt_project.yml found
    run = true                            # run `dbt docs generate` when artefacts are stale

    [airflow]
    dags = ["orchestration/dags"]         # files or directories; default: discovered

    [sql]
    paths = ["sql/**/*.sql"]              # default: discovered *.sql outside dbt projects

    [definitions]
    paths = ["definitions.yml"]           # business terms; default: every definitions.yml

    [ossie]
    paths = ["semantic/"]                 # Apache Ossie models; default: every *.ossie.yaml

    [usage]                               # column usage from query history (off by default)
    enabled = true                        # Snowflake, BigQuery and Databricks warehouses
    days = 90
    exclude_users = ["DBT_SERVICE"]       # leave out pipeline accounts

    [[warehouse]]
    type = "postgres"
    dsn = "${WAREHOUSE_DSN}"              # ${VAR} reads the environment
    schemas = ["raw", "analytics"]

    [[warehouse]]
    type = "iceberg"
    name = "lake"                         # the catalogue name SQL uses for these tables
    uri = "http://localhost:8181"
    namespaces = ["sales"]
    [warehouse.properties]
    credential = "${ICEBERG_CREDENTIAL}"
"""

from __future__ import annotations

import contextlib
import os
import re
import tomllib
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from konstruct.inputs.usage import UsageSettings

from konstruct.inputs.dbt import SKIP_DIRS, DbtProject, walk_dirs
from konstruct.inputs.warehouse import (
    BigQueryConnector,
    Connector,
    DatabricksConnector,
    DuckDBConnector,
    IcebergConnector,
    PostgresConnector,
    SnowflakeConnector,
    TrinoConnector,
)


class ConfigError(Exception):
    pass


_ENV = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}')


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ''), value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


@dataclass
class WarehouseConfig:
    type: str
    options: dict[str, Any]
    source: str = 'konstruct.toml'

    def connector(self, root: Path) -> Connector:
        options = expand_env(self.options)
        schemas = options.get('schemas')
        if self.type == 'duckdb':
            path = Path(str(options.get('path', '')))
            if not path.is_absolute():
                path = root / path
            return DuckDBConnector(path, schemas=schemas)
        if self.type in ('postgres', 'postgresql'):
            dsn = options.get('dsn') or _dsn_from_parts(options)
            return PostgresConnector(dsn, schemas=schemas)
        if self.type == 'iceberg':
            properties = {str(k): str(v) for k, v in (options.get('properties') or {}).items()}
            for key in ('uri', 'warehouse', 'type'):
                if key in options and key not in properties:
                    properties[key] = str(options[key])
            properties.setdefault('type', 'rest')
            return IcebergConnector(
                name=str(options.get('name', 'iceberg')),
                properties=properties,
                namespaces=options.get('namespaces'),
            )
        if self.type == 'snowflake':
            options_only = {k: v for k, v in options.items() if k not in ('databases', 'schemas', 'type')}
            return SnowflakeConnector(options_only, databases=options.get('databases'), schemas=schemas)
        if self.type == 'databricks':
            options_only = {k: v for k, v in options.items() if k not in ('catalogs', 'schemas', 'type')}
            return DatabricksConnector(options_only, catalogs=options.get('catalogs'), schemas=schemas)
        if self.type == 'trino':
            reserved = ('catalogs', 'schemas', 'type', 'password', 'token', 'unsupported_method')
            if options.get('unsupported_method'):
                raise ValueError(
                    f'dbt profile method {options["unsupported_method"]!r} is not supported '
                    '(none, ldap and jwt are); set the connection in konstruct.toml'
                )
            for key in ('password', 'token'):
                if key in self.options and not options.get(key):
                    raise ValueError(f'{key} is set but empty')
            return TrinoConnector(
                {k: v for k, v in options.items() if k not in reserved},
                catalogs=options.get('catalogs'),
                schemas=schemas,
                password=options.get('password'),
                token=options.get('token'),
            )
        if self.type == 'bigquery':
            return BigQueryConnector(
                project=str(options.get('project', '')),
                datasets=options.get('datasets'),
                location=options.get('location'),
                endpoint=options.get('endpoint'),
            )
        raise ConfigError(
            f'unknown warehouse type {self.type!r} '
            '(expected duckdb, postgres, iceberg, snowflake, bigquery, databricks or trino)'
        )

    def describe(self, root: Path | None = None) -> str:
        if self.type == 'duckdb':
            path = Path(str(self.options.get('path', '')))
            if root is not None and path.is_absolute():
                with contextlib.suppress(ValueError):
                    path = path.relative_to(root)
            return f'duckdb {path}'
        if self.type == 'iceberg':
            return f'iceberg {self.options.get("name", "iceberg")} at {self.options.get("uri", "?")}'
        return f'{self.type} ({self.source})'


def _dsn_from_parts(options: dict[str, Any]) -> str:
    parts = {
        'host': options.get('host'),
        'port': options.get('port'),
        'dbname': options.get('dbname') or options.get('database'),
        'user': options.get('user'),
        'password': options.get('password') or options.get('pass'),
        'sslmode': options.get('sslmode'),
    }
    return ' '.join(f'{k}={_quote_libpq(str(v))}' for k, v in parts.items() if v not in (None, ''))


def _quote_libpq(value: str) -> str:
    if value and not re.search(r"[\s'\\]", value):
        return value
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"


@dataclass
class Config:
    root: Path
    out_dir: Path
    dialect: str | None = None
    default_catalog: str | None = None
    default_schema: str | None = None
    exclude: list[str] = field(default_factory=list)
    run_dbt: bool = True
    dbt_projects: list[Path] | None = None
    dag_paths: list[Path] | None = None
    dagster_paths: list[Path] | None = None
    spark_paths: list[Path] | None = None
    powerbi_paths: list[Path] | None = None
    spark_dialect: str = 'spark'
    ontology_packs: list[str] | None = None
    """The ontology packs to bind: None for every pack Konstruct ships, [] for none."""
    fivetran_lineage: bool = True
    """Read the Fivetran Platform Connector's lineage tables when a warehouse has them."""
    looker_paths: list[Path] | None = None
    definitions_paths: list[Path] | None = None
    vocabulary_paths: list[Path] = field(default_factory=list)
    """SKOS or OWL vocabularies to read as business terms."""
    repositories: list[Path] = field(default_factory=list)
    ossie_paths: list[Path] | None = None
    sql_paths: list[str] | None = None
    warehouses: list[WarehouseConfig] | None = None
    discover_warehouses: bool = True
    config_file: Path | None = None
    usage: UsageSettings = field(default_factory=lambda: _usage_settings({}))
    options: dict[str, Any] = field(default_factory=dict)
    """Build options given on the command line or to :meth:`konstruct.Project.build`, which apply
    to every repository built into the graph, not only this one."""

    def override(
        self, *, run_dbt: bool | None = None, warehouses: bool = True, dialect: str | None = None
    ) -> None:
        """Apply build options over the configuration files: ``run_dbt=False`` never runs dbt,
        ``warehouses=False`` connects to no warehouse, ``dialect`` replaces the detected one."""
        if run_dbt is not None:
            self.run_dbt = run_dbt
            self.options['run_dbt'] = run_dbt
        if not warehouses:
            self.warehouses = []
            self.options['warehouses'] = False
        if dialect:
            self.dialect = dialect
            self.options['dialect'] = dialect

    def excluded(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self.root).as_posix()
        except ValueError:
            return False
        return any(fnmatch(relative, pattern) for pattern in self.exclude)


def _spark_dialect(value: Any) -> str:
    from sqlglot.dialects.dialect import Dialect

    try:
        Dialect.get_or_raise(str(value))
    except ValueError:
        raise ConfigError(f'[spark] dialect {value!r} is not a SQL dialect Konstruct knows') from None
    return str(value)


def _flag(data: dict[str, Any], key: str, where: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f'{where} must be true or false, not {value!r}')
    return value


def _packs(data: dict[str, Any]) -> list[str] | None:
    if 'packs' not in data:
        return None
    from konstruct.semantics.registry import load_packs

    packs = data['packs']
    if not isinstance(packs, list) or not all(isinstance(p, str) for p in packs):
        raise ConfigError('[ontologies] packs must be a list of pack names')
    known = {p.name for p in load_packs()}
    unknown = sorted(set(packs) - known)
    if unknown:
        raise ConfigError(
            f'[ontologies] packs: no pack named {unknown[0]!r} (there are {", ".join(sorted(known))})'
        )
    return list(packs)


def _usage_settings(data: dict[str, Any]) -> UsageSettings:
    from konstruct.inputs.usage import UsageSettings

    objects = str(data.get('objects', 'direct')).lower()
    if objects not in ('direct', 'base'):
        raise ConfigError(f"[usage] objects must be 'direct' or 'base', not {objects!r}")
    return UsageSettings(
        enabled=bool(data.get('enabled', False)),
        days=int(data.get('days', 90)),
        exclude_users=[str(u) for u in data.get('exclude_users', [])],
        objects=objects,
        max_queries=int(data.get('max_queries', 50_000)),
    )


def load_config(root: Path, out_dir: Path | None = None) -> Config:
    """Read ``konstruct.toml`` (or ``[tool.konstruct]`` in ``pyproject.toml``) from ``root`` over the defaults.

    Raises:
        ConfigError: The configuration is invalid.
    """
    root = root.resolve()
    data: dict[str, Any] = {}
    config_file: Path | None = None
    if (root / 'konstruct.toml').is_file():
        config_file = root / 'konstruct.toml'
        data = _read_toml(config_file)
    elif (root / 'pyproject.toml').is_file():
        pyproject = _read_toml(root / 'pyproject.toml')
        tool = pyproject.get('tool', {}).get('konstruct')
        if isinstance(tool, dict):
            config_file = root / 'pyproject.toml'
            data = tool

    dbt = data.get('dbt', {})
    airflow = data.get('airflow', {})
    sql = data.get('sql', {})
    warehouses = data.get('warehouse')
    if isinstance(warehouses, dict):
        warehouses = [warehouses]
    config = Config(
        root=root,
        out_dir=(out_dir or root / str(data.get('out_dir', 'konstruct-out'))).resolve(),
        dialect=data.get('dialect'),
        default_catalog=data.get('default_catalog'),
        default_schema=data.get('default_schema'),
        exclude=list(data.get('exclude', [])),
        repositories=[(root / p).resolve() for p in data.get('repositories', [])],
        run_dbt=bool(dbt.get('run', True)),
        dbt_projects=[root / p for p in dbt['projects']] if 'projects' in dbt else None,
        dag_paths=[root / p for p in airflow['dags']] if 'dags' in airflow else None,
        dagster_paths=[root / p for p in data.get('dagster', {}).get('paths', [])]
        if 'paths' in data.get('dagster', {})
        else None,
        spark_paths=[root / p for p in data.get('spark', {}).get('paths', [])]
        if 'paths' in data.get('spark', {})
        else None,
        spark_dialect=_spark_dialect(data.get('spark', {}).get('dialect', 'spark')),
        looker_paths=[root / p for p in data.get('looker', {}).get('paths', [])]
        if 'paths' in data.get('looker', {})
        else None,
        powerbi_paths=[root / p for p in data.get('powerbi', {}).get('paths', [])]
        if 'paths' in data.get('powerbi', {})
        else None,
        ontology_packs=_packs(data.get('ontologies', {})),
        sql_paths=list(sql['paths']) if 'paths' in sql else None,
        usage=_usage_settings(data.get('usage', {})),
        fivetran_lineage=_flag(data.get('fivetran', {}), 'lineage', '[fivetran] lineage', True),
        vocabulary_paths=[root / p for p in data.get('vocabularies', {}).get('paths', [])],
        definitions_paths=[root / p for p in data.get('definitions', {}).get('paths', [])]
        if 'paths' in data.get('definitions', {})
        else None,
        ossie_paths=[root / p for p in data.get('ossie', {}).get('paths', [])]
        if 'paths' in data.get('ossie', {})
        else None,
        warehouses=[
            WarehouseConfig(str(w.get('type', '')).lower(), {k: v for k, v in w.items() if k != 'type'})
            for w in warehouses
        ]
        if warehouses is not None
        else None,
        discover_warehouses=bool(data.get('discover_warehouses', True)),
        config_file=config_file,
    )
    return config


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open('rb') as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f'cannot read {path}: {error}') from error


# Discovery


def iter_files(config: Config, suffix: str) -> list[Path]:
    found = []
    for directory in walk_dirs(config.root):
        if directory.resolve() == config.out_dir:
            continue
        for path in sorted(directory.glob(f'*{suffix}')):
            if path.is_file() and not config.excluded(path):
                found.append(path)
    return found


def discover_dag_files(config: Config) -> list[Path]:
    from konstruct.inputs.airflow import looks_like_dag_file

    if config.dag_paths is not None:
        files: list[Path] = []
        for path in config.dag_paths:
            if path.is_dir():
                files.extend(sorted(p for p in path.rglob('*.py') if not set(p.parts) & SKIP_DIRS))
            elif path.is_file():
                files.append(path)
        return files
    candidates = []
    for path in iter_files(config, '.py'):
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if looks_like_dag_file(text):
            candidates.append(path)
    return candidates


def discover_dagster_files(config: Config) -> list[Path]:
    from konstruct.inputs.dagster import looks_like_dagster_file

    if config.dagster_paths is not None:
        files: list[Path] = []
        for path in config.dagster_paths:
            if path.is_dir():
                files.extend(sorted(p for p in path.rglob('*.py') if not set(p.parts) & SKIP_DIRS))
            elif path.is_file():
                files.append(path)
        return files
    found = []
    for path in iter_files(config, '.py'):
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if looks_like_dagster_file(text):
            found.append(path)
    return found


def discover_spark_files(config: Config) -> list[Path]:
    """PySpark jobs: the configured paths, or every Python file that uses Spark SQL."""
    from konstruct.inputs.spark import looks_like_spark_file

    if config.spark_paths is not None:
        files: list[Path] = []
        for path in config.spark_paths:
            if path.is_dir():
                files.extend(sorted(p for p in path.rglob('*.py') if not set(p.parts) & SKIP_DIRS))
            elif path.is_file():
                files.append(path)
        return files
    found = []
    for path in iter_files(config, '.py'):
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if looks_like_spark_file(text):
            found.append(path)
    return found


def discover_looker_files(config: Config) -> list[Path]:
    """LookML files (``*.lkml``): the configured paths, or every one in the repository."""
    if config.looker_paths is not None:
        files: list[Path] = []
        for path in config.looker_paths:
            if path.is_dir():
                files.extend(sorted(p for p in path.rglob('*.lkml') if not set(p.parts) & SKIP_DIRS))
            elif path.is_file():
                files.append(path)
        return files
    return list(iter_files(config, '.lkml'))


def discover_powerbi_files(config: Config) -> list[Path]:
    """Power BI TMDL files (``*.tmdl``): the configured paths, or every one in the repository."""
    if config.powerbi_paths is not None:
        files: list[Path] = []
        for path in config.powerbi_paths:
            if path.is_dir():
                files.extend(sorted(p for p in path.rglob('*.tmdl') if not set(p.parts) & SKIP_DIRS))
            elif path.is_file():
                files.append(path)
        return files
    return list(iter_files(config, '.tmdl'))


def discover_definitions_files(config: Config) -> list[Path]:
    """``definitions.yml`` files: the configured paths, or every one in the repository."""
    from konstruct.semantics.definitions import FILE_NAMES

    if config.definitions_paths is not None:
        files: list[Path] = []
        for path in config.definitions_paths:
            if path.is_dir():
                files.extend(sorted(p for name in FILE_NAMES for p in path.rglob(name)))
            elif path.is_file():
                files.append(path)
        return files
    return [p for p in [*iter_files(config, '.yml'), *iter_files(config, '.yaml')] if p.name in FILE_NAMES]


def discover_ossie_files(config: Config) -> list[Path]:
    """Apache Ossie semantic models: the configured paths, or every ``*.ossie.yaml`` in the repository."""
    from konstruct.semantics.ossie import FILE_SUFFIXES

    def wanted(path: Path) -> bool:
        return path.name.endswith(FILE_SUFFIXES)

    if config.ossie_paths is not None:
        files: list[Path] = []
        for path in config.ossie_paths:
            if path.is_dir():
                files.extend(sorted(p for p in path.rglob('*') if p.is_file() and wanted(p)))
            elif path.is_file():
                files.append(path)
        return files
    return [p for p in [*iter_files(config, '.yaml'), *iter_files(config, '.yml')] if wanted(p)]


def discover_sql_files(config: Config, projects: list[DbtProject], used: set[Path]) -> list[Path]:
    if config.sql_paths is not None:
        files: list[Path] = []
        for pattern in config.sql_paths:
            files.extend(sorted(p for p in config.root.glob(pattern) if p.is_file()))
        return [f for f in files if f.resolve() not in used]
    owned = [p.resolve() for project in projects for p in [*project.source_paths, project.target_path]]
    owned += [(project.root / name).resolve() for project in projects for name in ('analyses', 'analysis')]
    result = []
    for path in iter_files(config, '.sql'):
        resolved = path.resolve()
        if resolved in used or any(resolved.is_relative_to(base) for base in owned):
            continue
        result.append(path)
    return result


def discover_warehouses(config: Config, projects: list[DbtProject]) -> list[WarehouseConfig]:
    if config.warehouses is not None:
        return list(config.warehouses)
    if not config.discover_warehouses:
        return []
    found: list[WarehouseConfig] = []
    seen_paths: set[Path] = set()
    for project in projects:
        target = _dbt_profile_target(project)
        if not target:
            continue
        kind = str(target.get('type', '')).lower()
        if kind == 'duckdb' and target.get('path') and target['path'] != ':memory:':
            path = Path(str(target['path']))
            path = path if path.is_absolute() else project.root / path
            if path.is_file() and path.resolve() not in seen_paths:
                seen_paths.add(path.resolve())
                found.append(
                    WarehouseConfig('duckdb', {'path': str(path)}, source=f'dbt profile {project.profile}')
                )
        elif kind in ('postgres', 'postgresql'):
            keys = ('host', 'port', 'user', 'password', 'pass', 'dbname', 'database', 'sslmode')
            options = {k: target.get(k) for k in keys}
            found.append(WarehouseConfig('postgres', options, source=f'dbt profile {project.profile}'))
        elif kind == 'snowflake':
            keys = ('account', 'user', 'password', 'role', 'warehouse', 'database', 'schema', 'authenticator')
            options = {k: target[k] for k in keys if target.get(k)}
            if target.get('private_key_path'):
                options['private_key_file'] = target['private_key_path']
            found.append(WarehouseConfig('snowflake', options, source=f'dbt profile {project.profile}'))
        elif kind == 'bigquery':
            found.append(
                WarehouseConfig(
                    'bigquery',
                    {
                        'project': target.get('project') or target.get('database'),
                        'location': target.get('location'),
                    },
                    source=f'dbt profile {project.profile}',
                )
            )
        elif kind == 'databricks':
            options = {
                'server_hostname': target.get('host'),
                'http_path': target.get('http_path'),
                'access_token': target.get('token'),
                'catalog': target.get('catalog'),
                'schema': target.get('schema'),
            }
            found.append(
                WarehouseConfig(
                    'databricks',
                    {k: v for k, v in options.items() if v},
                    source=f'dbt profile {project.profile}',
                )
            )
        elif kind == 'trino':
            options = {
                'host': target.get('host'),
                'port': int(target['port']) if str(target.get('port') or '').isdigit() else None,
                'user': target.get('user'),
                'catalog': target.get('database'),
                'schema': target.get('schema'),
                'http_scheme': target.get('http_scheme'),
            }
            method = str(target.get('method') or 'none').lower()
            if method == 'ldap' and target.get('password'):
                options['password'] = target.get('password')
            elif method == 'jwt' and target.get('jwt_token'):
                options['token'] = target.get('jwt_token')
            elif method not in ('none', 'ldap', 'jwt'):
                options['unsupported_method'] = method
            found.append(
                WarehouseConfig(
                    'trino', {k: v for k, v in options.items() if v}, source=f'dbt profile {project.profile}'
                )
            )
    for path in iter_files(config, '.duckdb'):
        if path.resolve() not in seen_paths:
            seen_paths.add(path.resolve())
            found.append(WarehouseConfig('duckdb', {'path': str(path)}, source='discovered'))
    return found


_ENV_VAR_CALL = re.compile(
    r"\{\{\s*env_var\(\s*['\"]([^'\"]+)['\"]\s*(?:,\s*['\"]([^'\"]*)['\"]\s*)?\)\s*(?:\|\s*\w+\s*)?\}\}"
)


def _render_profile_value(value: Any) -> Any:
    if isinstance(value, str):
        rendered = _ENV_VAR_CALL.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ''), value)
        return None if '{{' in rendered or '{%' in rendered else rendered
    return value


def _dbt_profile_target(project: DbtProject) -> dict[str, Any] | None:
    if not project.profile:
        return None
    candidates = []
    if os.environ.get('DBT_PROFILES_DIR'):
        candidates.append(Path(os.environ['DBT_PROFILES_DIR']) / 'profiles.yml')
    candidates += [project.root / 'profiles.yml', Path.home() / '.dbt' / 'profiles.yml']
    for path in candidates:
        if not path.is_file():
            continue
        try:
            profiles = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        except (OSError, yaml.YAMLError):
            continue
        profile = profiles.get(project.profile) if isinstance(profiles, dict) else None
        if not isinstance(profile, dict):
            continue
        target_name = _render_profile_value(os.environ.get('DBT_TARGET') or profile.get('target', 'dev'))
        target = (profile.get('outputs') or {}).get(target_name)
        if isinstance(target, dict):
            return {k: _render_profile_value(v) for k, v in target.items()}
    return None
