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
    run = true                            # run `dbt docs generate` when stale, once trusted

    [airflow]
    dags = ["orchestration/dags"]         # files or directories; default: discovered

    [sql]
    paths = ["sql/**/*.sql"]              # default: discovered *.sql outside dbt projects

    [definitions]
    paths = ["definitions.yml"]           # business terms; default: every definitions.yml

    [ossie]
    paths = ["semantic/"]                 # Apache Ossie models; default: every *.ossie.yaml

    [plugins]
    enabled = ["konstruct-yaml-pipelines"] # installed plugins this project runs; default: none

    [ingestion]
    paths = ["pipelines/", "meltano.yml"] # loader configuration; default: discovered

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
from collections.abc import Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatch, fnmatchcase
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

INPUT_PATH_FIELDS = {'looker': 'looker_paths'}
"""The registry inputs whose ``[<name>] paths`` the configuration keeps in a field of its own."""


class ConfigError(Exception):
    pass


class UntrustedConfig(ConfigError):
    """A connection or path from the repository's own configuration that needs ``konstruct trust``."""


_ENV = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}')


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ''), value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


WAREHOUSE_TYPES = (
    'duckdb',
    'postgres',
    'postgresql',
    'iceberg',
    'snowflake',
    'databricks',
    'trino',
    'bigquery',
)


@dataclass
class WarehouseConfig:
    type: str
    options: dict[str, Any]
    source: str = 'konstruct.toml'
    trusted: bool = True
    """False for a connection from the repository's own konstruct.toml or profiles.yml that the
    user has not trusted: its environment variables are never read, and only a DuckDB file
    inside the project opens."""

    def connector(self, root: Path) -> Connector:
        """The connector for this warehouse.

        Raises:
            UntrustedConfig: The connection is untrusted and is not a DuckDB file in ``root``.
            ConfigError: The warehouse type is unknown.
        """
        if self.type not in WAREHOUSE_TYPES:
            raise ConfigError(
                f'unknown warehouse type {self.type!r} '
                '(expected duckdb, postgres, iceberg, snowflake, bigquery, databricks or trino)'
            )
        if not self.trusted:
            self._check_untrusted(root)
        options = expand_env(self.options) if self.trusted else self.options
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
        return BigQueryConnector(
            project=str(options.get('project', '')),
            datasets=options.get('datasets'),
            location=options.get('location'),
            endpoint=options.get('endpoint'),
        )

    def _check_untrusted(self, root: Path) -> None:
        where = f'the {self.type} connection in {self.source}'
        if self.type != 'duckdb':
            raise UntrustedConfig(f'{where} is not trusted; run `konstruct trust` after reviewing it')
        path = str(self.options.get('path', ''))
        if _templated(path):
            raise UntrustedConfig(
                f'{where} reads its path from the environment, which needs trust; '
                'run `konstruct trust` after reviewing it'
            )
        if not (root / path).resolve().is_relative_to(root.resolve()):
            if self.source == 'discovered':
                raise UntrustedConfig(
                    f'{path} leads outside the project; name it under [[warehouse]] in konstruct.toml '
                    'and run `konstruct trust` to read it'
                )
            raise UntrustedConfig(
                f'{where} opens {path}, outside the project, which needs trust; '
                'run `konstruct trust` after reviewing it'
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


def _templated(value: str) -> bool:
    """Whether a value would read the environment: ``${VAR}`` or dbt's Jinja."""
    return '${' in value or '{{' in value or '{%' in value


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
    dlt_lineage: bool = True
    """Read the schemas dlt keeps in each dataset's ``_dlt_version`` table, for the resource that
    fills each table."""
    looker_paths: list[Path] | None = None
    definitions_paths: list[Path] | None = None
    vocabulary_paths: list[Path] = field(default_factory=list)
    """SKOS or OWL vocabularies to read as business terms."""
    repositories: list[Path] = field(default_factory=list)
    ossie_paths: list[Path] | None = None
    ingestion_paths: list[Path] | None = None
    sql_paths: list[str] | None = None
    warehouses: list[WarehouseConfig] | None = None
    discover_warehouses: bool = True
    config_file: Path | None = None
    trusted: bool = True
    """Whether the connections and paths in the repository's own konstruct.toml and profiles.yml
    are used as written: ``konstruct trust`` recorded them unchanged, or ``KONSTRUCT_TRUST_CONFIG``
    is set. :func:`load_config` sets it; a configuration built in code is the caller's own."""
    usage: UsageSettings = field(default_factory=lambda: _usage_settings({}))
    plugins: list[str] = field(default_factory=list)
    """The installed plugins this project runs, by distribution name (``[plugins] enabled``)."""
    use_plugins: bool = True
    """Whether plugins run at all; ``--no-plugins`` turns them off."""
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Every table of the configuration file as written, such as ``[looker]``, so an input finds
    its own settings by its name."""
    html: bool = True
    """Write graph.html. A build only another tool reads, such as the coding agent hook's, skips it."""
    governance_from: Path | None = None
    """The graph.json whose warehouse tags and masking policies a build from files alone keeps:
    by default the one it replaces; for a copy of the graph, such as the gate's, the project's."""
    options: dict[str, Any] = field(default_factory=dict)
    """Build options given on the command line or to :meth:`konstruct.Project.build`, which apply
    to every repository built into the graph, not only this one."""
    outside_links: dict[Path, set[str]] = field(default_factory=dict)
    """Files left unread because they lead outside the project through a symbolic link while it
    is untrusted: each directory holding such a link, with the links' names (see :meth:`inside`)."""

    def override(
        self,
        *,
        run_dbt: bool | None = None,
        warehouses: bool = True,
        dialect: str | None = None,
        plugins: bool = True,
    ) -> None:
        """Apply build options over the configuration files: ``run_dbt=False`` never runs dbt,
        ``warehouses=False`` connects to no warehouse, ``dialect`` replaces the detected one and
        ``plugins=False`` runs no plugins."""
        if not plugins:
            self.use_plugins = False
            self.options['plugins'] = False
        if run_dbt is not None:
            self.run_dbt = run_dbt
            self.options['run_dbt'] = run_dbt
        if not warehouses:
            self.warehouses = []
            self.options['warehouses'] = False
        if dialect:
            self.dialect = dialect
            self.options['dialect'] = dialect

    def inside(self, files: Iterable[Path]) -> list[Path]:
        """``files`` without those that lead outside the project through a symbolic link, while
        the project is untrusted; each one left out is noted in :attr:`outside_links`. A trusted
        project reads them all."""
        files = list(files)
        if self.trusted:
            return files
        root = self.root.resolve()
        kept = []
        for path in files:
            if path.resolve().is_relative_to(root):
                kept.append(path)
                continue
            link = _outside_link(path, root)
            self.outside_links.setdefault(link.parent, set()).add(link.name)
        return kept

    def excluded(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self.root).as_posix()
        except ValueError:
            return False
        return any(fnmatch(relative, pattern) for pattern in self.exclude)

    def paths_for(self, name: str) -> list[Path] | None:
        """The paths ``[<name>] paths`` pins for an input, or None to discover its files. An
        input of Konstruct's own that has a field for them (``looker_paths``) reads that field."""
        if name in INPUT_PATH_FIELDS:
            paths: list[Path] | None = getattr(self, INPUT_PATH_FIELDS[name])
            return paths
        listed = self.sections.get(name, {}).get('paths')
        if listed is None:
            return None
        if not isinstance(listed, list) or not all(isinstance(p, str) for p in listed):
            raise ConfigError(f'[{name}] paths must be a list of paths')
        return [self.root / p for p in listed]


def _outside_link(path: Path, root: Path) -> Path:
    """The symbolic link through which ``path`` leads outside ``root``: the first part of it,
    from the project down, that is a link resolving outside, or else the file itself."""
    absolute = path if path.is_absolute() else root / path
    try:
        parts = absolute.relative_to(root).parts
    except ValueError:
        return absolute
    for depth in range(1, len(parts) + 1):
        candidate = root.joinpath(*parts[:depth])
        if candidate.is_symlink() and not candidate.resolve().is_relative_to(root):
            return candidate
    return absolute


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


def _packs(data: dict[str, Any], plugins: list[str]) -> list[str] | None:
    if 'packs' not in data:
        return None
    from konstruct.plugins.registry import Plugins
    from konstruct.semantics.registry import load_packs

    packs = data['packs']
    if not isinstance(packs, list) or not all(isinstance(p, str) for p in packs):
        raise ConfigError('[ontologies] packs must be a list of pack names')
    # What the project names is checked against what it enables, whether or not plugins are turned
    # off for this run (--no-plugins, KONSTRUCT_NO_PLUGINS), so the switches cannot make a valid
    # configuration invalid; a pack that is then not loaded is a build warning. Reading a pack
    # directory runs no code.
    registry = Plugins.discover(plugins, environment=False) if plugins else None
    known = {p.name for p in load_packs(plugins=registry)}
    unknown = sorted(set(packs) - known)
    if unknown:
        raise ConfigError(
            f'[ontologies] packs: no pack named {unknown[0]!r} (there are {", ".join(sorted(known))})'
        )
    return list(packs)


def _plugins(data: Any) -> list[str]:
    enabled = data.get('enabled', []) if isinstance(data, dict) else None
    if not isinstance(enabled, list) or not all(isinstance(p, str) for p in enabled):
        raise ConfigError('[plugins] enabled is a list of distribution names: enabled = ["konstruct-x"]')
    return list(enabled)


def enabled_plugins(text: str, *, pyproject: bool = False) -> list[str]:
    """The distributions ``[plugins] enabled`` lists in the text of a ``konstruct.toml``, or of a
    ``pyproject.toml``'s ``[tool.konstruct]`` with ``pyproject``.

    Raises:
        ConfigError: The text is not TOML, or the list is not one of names.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f'not TOML: {error}') from error
    if pyproject:
        tool = data.get('tool', {}).get('konstruct')
        data = tool if isinstance(tool, dict) else {}
    return _plugins(data.get('plugins', {}))


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


PATH_SETTINGS: tuple[tuple[str | None, str, str], ...] = (
    (None, 'out_dir', 'out_dir'),
    (None, 'repositories', 'repositories'),
    ('dbt', 'projects', 'dbt_projects'),
    ('airflow', 'dags', 'dag_paths'),
    ('dagster', 'paths', 'dagster_paths'),
    ('spark', 'paths', 'spark_paths'),
    ('looker', 'paths', 'looker_paths'),
    ('powerbi', 'paths', 'powerbi_paths'),
    ('definitions', 'paths', 'definitions_paths'),
    ('vocabularies', 'paths', 'vocabulary_paths'),
    ('ossie', 'paths', 'ossie_paths'),
    ('ingestion', 'paths', 'ingestion_paths'),
    ('sql', 'paths', 'sql_paths'),
)
"""The settings of ``konstruct.toml`` that name files or directories, as (table, key, the
:class:`Config` field), with None for a top-level key. ``[sql] paths`` holds globs. Each is read
relative to the project, and one that leads outside it needs trust."""

_GLOB = re.compile(r'[*?[]')


def _path_values(data: dict[str, Any], table: str | None, key: str) -> list[Any] | None:
    """The values a path setting gives, as written, or None when it is absent."""
    section = data if table is None else data.get(table, {})
    if not isinstance(section, dict) or key not in section:
        return None
    value = section[key]
    return list(value) if isinstance(value, list) else [value]


def _listed(data: dict[str, Any], root: Path, table: str, key: str) -> list[Path] | None:
    values = _path_values(data, table, key)
    return None if values is None else [root / str(v) for v in values]


def _setting_name(table: str | None, key: str, given: Any) -> str:
    if table is None and key == 'out_dir':
        return f'out_dir = {given!r}'
    return f'{key}: {given!r}' if table is None else f'[{table}] {key}: {given!r}'


def _leads_to(root: Path, glob: bool, given: Any) -> Path | None:
    """Where a path setting's value leads, resolved through symbolic links, when that is outside
    the project, or None when it stays inside. A glob is judged by its fixed leading part, and a
    ``..`` after a wildcard leads outside."""
    path = Path(str(given))
    if glob:
        parts = path.parts
        fixed = next((i for i, part in enumerate(parts) if _GLOB.search(part)), len(parts))
        if '..' in parts[fixed:]:
            return root / path
        path = Path(*parts[:fixed])
    resolved = (root / path).resolve()
    return None if resolved.is_relative_to(root) else resolved


def paths_outside(root: Path, data: dict[str, Any]) -> list[tuple[str, str, Path]]:
    """Each path setting in ``data`` (a project's Konstruct settings as written) that leads
    outside ``root``: its key, the setting as written and where it leads. Trust covers these,
    and an untrusted build refuses them."""
    root = root.resolve()
    found = []
    for table, key, _ in PATH_SETTINGS:
        for given in _path_values(data, table, key) or []:
            if (where := _leads_to(root, (table, key) == ('sql', 'paths'), given)) is not None:
                found.append(
                    (key if table is None else f'{table}.{key}', _setting_name(table, key, given), where)
                )
    return found


def duckdb_paths_outside(root: Path, data: dict[str, Any]) -> list[tuple[str, str, Path]]:
    """Each DuckDB ``path`` under ``[[warehouse]]`` in ``data`` that leads outside ``root``, with
    where it leads, so that trust covers where a symbolic link points as well as what it is called."""
    root = root.resolve()
    warehouses = data.get('warehouse')
    found = []
    for warehouse in [warehouses] if isinstance(warehouses, dict) else warehouses or []:
        if not isinstance(warehouse, dict) or str(warehouse.get('type', '')).lower() != 'duckdb':
            continue
        path = str(warehouse.get('path', ''))
        if path and not _templated(path) and (where := _leads_to(root, False, path)) is not None:
            found.append(('warehouse.path', f'[[warehouse]] path: {path!r}', where))
    return found


def load_config(root: Path, out_dir: Path | None = None, *, trusted: bool | None = None) -> Config:
    """Read ``konstruct.toml`` (or ``[tool.konstruct]`` in ``pyproject.toml``) from ``root`` over the defaults.

    Args:
        root: The project.
        out_dir: The output directory, over ``out_dir`` in the configuration.
        trusted: Whether the project's own connections and paths are trusted. By default, as
            ``konstruct trust`` recorded them (see :mod:`konstruct.engine.trust`).

    Raises:
        ConfigError: The configuration is invalid, or a path setting (see :data:`PATH_SETTINGS`)
            leads outside the project without being trusted.
    """
    from konstruct.engine import trust

    config, data = _read(root, out_dir)
    config.trusted = trust.is_trusted(config) if trusted is None else trusted
    for warehouse in config.warehouses or []:
        warehouse.trusted = config.trusted
    if config.trusted:
        return config
    default = config.root / 'konstruct-out'
    if config.out_dir == default.resolve() and not config.out_dir.is_relative_to(config.root):
        # The repository can commit konstruct-out as a link, and the build writes into it.
        hint = ', or pass --out' if out_dir is None else ''
        raise UntrustedConfig(
            f'konstruct-out is a symbolic link to {config.out_dir}, outside the project, which needs '
            f'trust; run `konstruct trust` after reviewing it{hint}'
        )
    if config.config_file is None:
        return config
    where = config.config_file.name
    for key, setting, _ in paths_outside(config.root, data):
        if key == 'out_dir' and out_dir is not None:
            continue  # the caller's own output directory replaces it
        hint = ', or pass --out' if key == 'out_dir' else ''
        raise UntrustedConfig(
            f'{setting} in {where} is outside the project, which needs trust; '
            f'run `konstruct trust` after reviewing {where}{hint}'
        )
    return config


def read_config(root: Path, out_dir: Path | None = None) -> Config:
    """The configuration as written, before trust is checked: for :mod:`konstruct.engine.trust`,
    which fingerprints it. Builds use :func:`load_config`.

    Raises:
        ConfigError: The configuration is invalid.
    """
    return _read(root, out_dir)[0]


def read_settings(path: Path) -> dict[str, Any]:
    """The Konstruct settings in a configuration file: all of ``konstruct.toml``, or the
    ``[tool.konstruct]`` table of ``pyproject.toml``.

    Raises:
        ConfigError: The file cannot be read.
    """
    data = _read_toml(path)
    if path.name != 'pyproject.toml':
        return data
    tool = data.get('tool', {}).get('konstruct')
    return tool if isinstance(tool, dict) else {}


def _read(root: Path, out_dir: Path | None) -> tuple[Config, dict[str, Any]]:
    root = root.resolve()
    data: dict[str, Any] = {}
    config_file: Path | None = None
    if (root / 'konstruct.toml').is_file():
        config_file = root / 'konstruct.toml'
        data = read_settings(config_file)
    elif (root / 'pyproject.toml').is_file():
        tool = _read_toml(root / 'pyproject.toml').get('tool', {}).get('konstruct')
        if isinstance(tool, dict):
            config_file = root / 'pyproject.toml'
            data = tool

    dbt = data.get('dbt', {})
    sql = data.get('sql', {})
    warehouses = data.get('warehouse')
    if isinstance(warehouses, dict):
        warehouses = [warehouses]
    repositories = data.get('repositories', [])
    if not isinstance(repositories, list) or not all(isinstance(p, str) for p in repositories):
        # A [repositories] table would otherwise be read as a list of its keys.
        raise ConfigError('repositories is a list of paths: repositories = ["../marketing"]')
    plugins = _plugins(data.get('plugins', {}))
    config = Config(
        root=root,
        out_dir=(out_dir or root / str(data.get('out_dir', 'konstruct-out'))).resolve(),
        dialect=data.get('dialect'),
        default_catalog=data.get('default_catalog'),
        default_schema=data.get('default_schema'),
        exclude=list(data.get('exclude', [])),
        repositories=[(root / p).resolve() for p in repositories],
        run_dbt=bool(dbt.get('run', True)),
        dbt_projects=_listed(data, root, 'dbt', 'projects'),
        dag_paths=_listed(data, root, 'airflow', 'dags'),
        dagster_paths=_listed(data, root, 'dagster', 'paths'),
        spark_paths=_listed(data, root, 'spark', 'paths'),
        spark_dialect=_spark_dialect(data.get('spark', {}).get('dialect', 'spark')),
        looker_paths=_listed(data, root, 'looker', 'paths'),
        powerbi_paths=_listed(data, root, 'powerbi', 'paths'),
        ontology_packs=_packs(data.get('ontologies', {}), plugins),
        sql_paths=list(sql['paths']) if 'paths' in sql else None,
        usage=_usage_settings(data.get('usage', {})),
        fivetran_lineage=_flag(data.get('fivetran', {}), 'lineage', '[fivetran] lineage', True),
        dlt_lineage=_flag(data.get('dlt', {}), 'lineage', '[dlt] lineage', True),
        vocabulary_paths=_listed(data, root, 'vocabularies', 'paths') or [],
        definitions_paths=_listed(data, root, 'definitions', 'paths'),
        ossie_paths=_listed(data, root, 'ossie', 'paths'),
        ingestion_paths=_listed(data, root, 'ingestion', 'paths'),
        warehouses=[
            WarehouseConfig(
                str(w.get('type', '')).lower(), {k: v for k, v in w.items() if k != 'type'}, trusted=False
            )
            for w in warehouses
        ]
        if warehouses is not None
        else None,
        discover_warehouses=bool(data.get('discover_warehouses', True)),
        config_file=config_file,
        plugins=plugins,
        sections={k: v for k, v in data.items() if isinstance(v, dict)},
        trusted=False,
    )
    return config, data


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open('rb') as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f'cannot read {path}: {error}') from error


# Discovery


def discover_input_files(config: Config, name: str, patterns: Iterable[str]) -> list[Path]:
    """The files of an input: those its ``[<name>] paths`` lists (a directory gives every file in it
    whose name matches one of ``patterns``), or every such file in the repository."""
    patterns = list(patterns)

    def wanted(path: Path) -> bool:
        return any(fnmatchcase(path.name, pattern) for pattern in patterns)

    listed = config.paths_for(name)
    if listed is not None:
        files: list[Path] = []
        for path in listed:
            if path.is_dir():
                files.extend(
                    sorted(
                        p
                        for p in path.rglob('*')
                        if wanted(p) and p.is_file() and not set(p.parts) & SKIP_DIRS
                    )
                )
            elif path.is_file():
                files.append(path)
        return config.inside(files)
    found = []
    for directory in walk_dirs(config.root):
        if directory.resolve() == config.out_dir:
            continue
        for path in sorted(directory.iterdir()):
            if wanted(path) and path.is_file() and not config.excluded(path):
                found.append(path)
    return config.inside(found)


def iter_files(config: Config, suffix: str) -> list[Path]:
    """The files ending in ``suffix`` in the project, without links leading outside it while it
    is untrusted (see :meth:`Config.inside`)."""
    return config.inside(_walk_files(config, suffix))


def _walk_files(config: Config, suffix: str) -> list[Path]:
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
        return config.inside(files)
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
        return config.inside(files)
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
        return config.inside(files)
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
    from konstruct.inputs.looker import INPUT

    return discover_input_files(config, 'looker', INPUT.patterns)


def discover_powerbi_files(config: Config) -> list[Path]:
    """Power BI TMDL files (``*.tmdl``): the configured paths, or every one in the repository."""
    if config.powerbi_paths is not None:
        files: list[Path] = []
        for path in config.powerbi_paths:
            if path.is_dir():
                files.extend(sorted(p for p in path.rglob('*.tmdl') if not set(p.parts) & SKIP_DIRS))
            elif path.is_file():
                files.append(path)
        return config.inside(files)
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
        return config.inside(files)
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
        return config.inside(files)
    return [p for p in [*iter_files(config, '.yaml'), *iter_files(config, '.yml')] if wanted(p)]


def discover_ingestion_files(config: Config) -> list[tuple[Path, str]]:
    """Loader configuration files (dlt pipelines, ``meltano.yml``, Singer catalogs and the rest),
    each with its format: the configured paths, or every file in the repository whose name and
    text show a format Konstruct reads. Larger files are not configuration and are not read."""
    from konstruct.inputs.ingestion import MAX_BYTES, SUFFIXES, kind_of

    candidates: list[Path] = []
    if config.ingestion_paths is not None:
        for path in config.ingestion_paths:
            if path.is_dir():
                candidates.extend(
                    sorted(
                        p
                        for p in path.rglob('*')
                        if p.is_file() and p.name.lower().endswith(SUFFIXES) and not set(p.parts) & SKIP_DIRS
                    )
                )
            elif path.is_file():
                candidates.append(path)
    else:
        for directory in walk_dirs(config.root):
            if directory.resolve() == config.out_dir:
                continue
            candidates.extend(
                sorted(
                    p
                    for p in directory.iterdir()
                    if p.name.lower().endswith(SUFFIXES) and p.is_file() and not config.excluded(p)
                )
            )
    # The path inside the project: a tap-<name> directory says a state file is a tap's. String
    # slicing, as pathlib's relative paths cost more than discovery's reading of most files.
    root = f'{config.root}{os.sep}'
    found = []
    for path in config.inside(candidates):
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        name = str(path)
        kind = kind_of((name[len(root) :] if name.startswith(root) else name).replace(os.sep, '/'), text)
        if kind is not None:
            found.append((path, kind))
    return found


def discover_sql_files(config: Config, projects: list[DbtProject], used: set[Path]) -> list[Path]:
    if config.sql_paths is not None:
        files: list[Path] = []
        for pattern in config.sql_paths:
            files.extend(sorted(p for p in config.root.glob(pattern) if p.is_file()))
        return [f for f in config.inside(files) if f.resolve() not in used]
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
    """The warehouses to read: those configured, or else each dbt profile's and every DuckDB file
    in the project. Each is marked trusted or not (see :class:`WarehouseConfig`); none is opened."""
    if config.warehouses is not None:
        return list(config.warehouses)
    if not config.discover_warehouses:
        return []
    found: list[WarehouseConfig] = []
    seen_paths: set[Path] = set()
    for project in projects:
        profile = _dbt_profile_target(project, config)
        if not profile:
            continue
        target, trusted = profile
        source = f'dbt profile {project.profile}'
        kind = str(target.get('type', '')).lower()
        if kind == 'duckdb' and target.get('path') and target['path'] != ':memory:':
            path = Path(str(target['path']))
            path = path if path.is_absolute() or _templated(str(path)) else project.root / path
            if _templated(str(path)):
                found.append(WarehouseConfig('duckdb', {'path': str(path)}, source=source, trusted=False))
            elif path.is_file() and path.resolve() not in seen_paths:
                seen_paths.add(path.resolve())
                found.append(WarehouseConfig('duckdb', {'path': str(path)}, source=source, trusted=trusted))
        elif kind in ('postgres', 'postgresql'):
            keys = ('host', 'port', 'user', 'password', 'pass', 'dbname', 'database', 'sslmode')
            options = {k: target.get(k) for k in keys}
            found.append(WarehouseConfig('postgres', options, source=source, trusted=trusted))
        elif kind == 'snowflake':
            keys = ('account', 'user', 'password', 'role', 'warehouse', 'database', 'schema', 'authenticator')
            options = {k: target[k] for k in keys if target.get(k)}
            if target.get('private_key_path'):
                options['private_key_file'] = target['private_key_path']
            found.append(WarehouseConfig('snowflake', options, source=source, trusted=trusted))
        elif kind == 'bigquery':
            found.append(
                WarehouseConfig(
                    'bigquery',
                    {
                        'project': target.get('project') or target.get('database'),
                        'location': target.get('location'),
                    },
                    source=source,
                    trusted=trusted,
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
                    'databricks', {k: v for k, v in options.items() if v}, source=source, trusted=trusted
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
                    'trino', {k: v for k, v in options.items() if v}, source=source, trusted=trusted
                )
            )
    for path in _walk_files(config, '.duckdb'):  # their own rule refuses links out, trusted or not
        if path.resolve() not in seen_paths:
            seen_paths.add(path.resolve())
            # No trust record names a file found later, so it is opened only inside the project,
            # which also refuses a symbolic link that leads outside it.
            found.append(WarehouseConfig('duckdb', {'path': str(path)}, source='discovered', trusted=False))
    return found


_ENV_VAR_CALL = re.compile(
    r"\{\{\s*env_var\(\s*['\"]([^'\"]+)['\"]\s*(?:,\s*['\"]([^'\"]*)['\"]\s*)?\)\s*(?:\|\s*\w+\s*)?\}\}"
)


def _render_profile_value(value: Any, expand: bool) -> Any:
    """A profile value with dbt's ``env_var()`` read from the environment. An untrusted profile
    is never expanded: a value that would read the environment is kept as written."""
    if isinstance(value, str) and expand:
        rendered = _ENV_VAR_CALL.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ''), value)
        return None if '{{' in rendered or '{%' in rendered else rendered
    return value


def _profile_files(project: DbtProject, root: Path) -> list[tuple[Path, bool]]:
    """The profiles.yml files a dbt project's profile can come from, in dbt's order, each with
    whether it is the user's own: ``DBT_PROFILES_DIR`` outside the project and
    ``~/.dbt/profiles.yml`` are, the dbt project's own file and anything else in the project are
    not."""
    own = (project.root / 'profiles.yml').resolve()

    def users(path: Path) -> bool:
        resolved = path.resolve()
        return resolved != own and not resolved.is_relative_to(root)

    files = []
    if os.environ.get('DBT_PROFILES_DIR'):
        # dbt runs in the dbt project, so a relative directory is read from there.
        path = project.root / os.environ['DBT_PROFILES_DIR'] / 'profiles.yml'
        files.append((path, users(path)))
    files += [(project.root / 'profiles.yml', False), (Path.home() / '.dbt' / 'profiles.yml', True)]
    return files


def dbt_projects(config: Config) -> list[DbtProject]:
    """The dbt projects ``config`` builds: those ``[dbt] projects`` names, or every one found."""
    from konstruct.inputs import dbt

    if config.dbt_projects is not None:
        return [p for r in config.dbt_projects if (p := dbt.read_project(r))]
    return [p for p in dbt.find_projects(config.root) if not config.excluded(p.root)]


def repository_profiles(config: Config, projects: list[DbtProject]) -> list[Path]:
    """The repository's own profiles.yml files that ``projects`` (``config``'s dbt projects) can
    read, which need trust before Konstruct connects with them."""
    found: dict[Path, None] = {}
    for project in projects:
        for path, users in _profile_files(project, config.root):
            if not users and path.is_file():
                found.setdefault(path.resolve())
    return list(found)


def _read_target(path: Path, name: str, expand: bool) -> dict[str, Any] | None:
    try:
        profiles = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    except (OSError, yaml.YAMLError, RecursionError):  # the repository's own, maybe nested too deeply
        return None
    profile = profiles.get(name) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict):
        return None
    target_name = _render_profile_value(os.environ.get('DBT_TARGET') or profile.get('target', 'dev'), expand)
    outputs = profile.get('outputs')
    target = outputs.get(target_name) if isinstance(outputs, dict) and isinstance(target_name, str) else None
    if not isinstance(target, dict):
        return None
    return {k: _render_profile_value(v, expand) for k, v in target.items()}


def _dbt_profile_target(project: DbtProject, config: Config) -> tuple[dict[str, Any], bool] | None:
    """The target of the project's dbt profile, and whether it is trusted: read from the user's
    own profiles.yml, or from the repository's once it is trusted."""
    if not project.profile:
        return None
    for path, users in _profile_files(project, config.root):
        trusted = users or config.trusted
        if path.is_file() and (target := _read_target(path, project.profile, trusted)) is not None:
            return target, trusted
    return None
