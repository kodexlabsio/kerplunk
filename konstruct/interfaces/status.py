"""`konstruct status` and `konstruct init`: what Konstruct finds in a project, without building."""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from konstruct.core.formats import FormatError
from konstruct.core.graph import Graph
from konstruct.engine.config import (
    Config,
    UntrustedConfig,
    WarehouseConfig,
    discover_dag_files,
    discover_dagster_files,
    discover_definitions_files,
    discover_ingestion_files,
    discover_looker_files,
    discover_ossie_files,
    discover_powerbi_files,
    discover_spark_files,
    discover_sql_files,
    discover_warehouses,
    load_config,
    read_config,
)
from konstruct.inputs import dbt

if TYPE_CHECKING:
    from konstruct.interfaces.layout import Style


@dataclass
class Status:
    root: Path
    config_file: str | None
    dbt_projects: list[dict[str, str]] = field(default_factory=list)
    dbt_on_path: bool = False
    warehouses: list[dict[str, str]] = field(default_factory=list)
    dag_files: list[str] = field(default_factory=list)
    spark_files: list[str] = field(default_factory=list)
    sql_files: list[str] = field(default_factory=list)
    looker_files: list[str] = field(default_factory=list)
    powerbi_files: list[str] = field(default_factory=list)
    definitions_files: list[str] = field(default_factory=list)
    dagster_files: list[str] = field(default_factory=list)
    ossie_files: list[str] = field(default_factory=list)
    ingestion_files: list[str] = field(default_factory=list)
    """Loader configuration files: dlt pipelines, meltano.yml, Singer catalogs and the rest."""
    vocabulary_files: list[str] = field(default_factory=list)
    ontology_packs: list[dict[str, str]] = field(default_factory=list)
    """The ontology packs the last build bound: each pack's name and its schema and term count."""
    built_from: list[dict[str, str]] = field(default_factory=list)
    """Inputs the last build read from the warehouse rather than files: Fivetran and dlt lineage, and usage."""
    graph: dict[str, Any] = field(default_factory=dict)
    skills: list[str] = field(default_factory=list)
    """Where the general skill is installed, such as ``project (Claude Code and Cursor)``."""
    skill_directories: list[dict[str, Any]] = field(default_factory=list)
    """Each directory with Konstruct's skills: its scope, its path and how many skills it holds."""
    repositories: list[dict[str, Any]] = field(default_factory=list)
    plugins: list[dict[str, Any]] = field(default_factory=list)
    """Each installed plugin's components, with whether it runs for this project and why."""
    missing_plugins: list[str] = field(default_factory=list)
    """Distributions ``[plugins] enabled`` names that are not installed."""
    trust: dict[str, Any] = field(default_factory=dict)
    """Whether the project's own connections and paths are trusted (see ``konstruct trust``)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            'root': str(self.root),
            'config_file': self.config_file,
            'dbt_projects': self.dbt_projects,
            'dbt_on_path': self.dbt_on_path,
            'warehouses': self.warehouses,
            'dag_files': self.dag_files,
            'spark_files': self.spark_files,
            'sql_files': self.sql_files,
            'looker_files': self.looker_files,
            'powerbi_files': self.powerbi_files,
            'definitions_files': self.definitions_files,
            'dagster_files': self.dagster_files,
            'ossie_files': self.ossie_files,
            'ingestion_files': self.ingestion_files,
            'vocabulary_files': self.vocabulary_files,
            'ontology_packs': self.ontology_packs,
            'built_from': self.built_from,
            'graph': self.graph,
            'skills': self.skills,
            'skill_directories': self.skill_directories,
            'repositories': self.repositories,
            'plugins': self.plugins,
            'missing_plugins': self.missing_plugins,
            'trust': self.trust,
        }

    def lines(self) -> list[tuple[str, str]]:
        """Each finding as a label and a sentence."""
        return [(key, value) for key, value, _ in self.rows()]

    def rows(self) -> list[tuple[str, str, str]]:
        """Each finding as a label, a sentence and its state: ``ok``, ``attention`` (something to
        do, such as a stale graph or a missing skill), or empty."""
        rows: list[tuple[str, str]] = [('config', self.config_file or 'none; inputs are discovered')]
        if self.trust.get('refused'):
            # The configuration needs trust before anything else can be found.
            return [*((k, v, '') for k, v in rows), ('trust', self.trust['refused'], 'attention')]
        if self.dbt_projects:
            for project in self.dbt_projects:
                rows.append(('dbt', f'{project["name"]} ({project["path"]}): {project["artefacts"]}'))
        else:
            rows.append(('dbt', 'no dbt projects found'))
        rows.append(
            ('dbt on PATH', 'yes' if self.dbt_on_path else 'no (Konstruct uses existing target/ artefacts)')
        )
        states = {len(rows) - 1: 'ok' if self.dbt_on_path else ''}
        for warehouse in self.warehouses or [{'name': 'none found', 'source': ''}]:
            source = f' ({warehouse["source"]})' if warehouse.get('source') else ''
            rows.append(('warehouse', f'{warehouse["name"]}{source}'))
        if self.trust.get('files') or self.trust.get('dbt_projects'):
            covered = [*self.trust['files'], *(f'dbt in {p}' for p in self.trust.get('dbt_projects', []))]
            files = ', '.join(covered)
            state = {
                'trusted': f'trusted: {files}',
                'environment': f'trusted by KONSTRUCT_TRUST_CONFIG: {files}',
                'changed': f'not trusted, changed since it was trusted: {files}; review them and run `konstruct trust`',
            }.get(self.trust['state'], f'not trusted: {files}; run `konstruct trust` after reviewing them')
            rows.append(('trust', state))
            states[len(rows) - 1] = 'ok' if self.trust.get('trusted') else 'attention'
        rows.append(('airflow', _files(self.dag_files, 'DAG file')))
        if self.dagster_files:
            rows.append(('dagster', _files(self.dagster_files, 'Dagster file')))
        if self.spark_files:
            rows.append(('spark', _files(self.spark_files, 'PySpark job')))
        rows.append(('sql', _files(self.sql_files, 'SQL file')))
        if self.looker_files:
            rows.append(('looker', _files(self.looker_files, 'LookML file')))
        if self.powerbi_files:
            rows.append(('power bi', _files(self.powerbi_files, 'TMDL file')))
        rows.append(('definitions', _files(self.definitions_files, 'definitions file')))
        if self.vocabulary_files:
            rows.append(('vocabularies', _files(self.vocabulary_files, 'vocabulary file')))
        if self.ossie_files:
            rows.append(('ossie', _files(self.ossie_files, 'Ossie model')))
        if self.ingestion_files:
            rows.append(('ingestion', _files(self.ingestion_files, 'loader configuration file')))
        if self.graph.get('exists') and 'unreadable' not in self.graph:
            shown = [f'{p["name"]} ({p["detail"]})' for p in self.ontology_packs]
            packs = ', '.join(shown[:4]) + (f' and {len(shown) - 4} more' if len(shown) > 4 else '')
            rows.append(('packs', packs or 'none bound in the last build'))
            for item in self.built_from:
                rows.append(
                    (item['input'], f'{item["name"]}: {item["detail"]}' if item['name'] else item['detail'])
                )
        for repository in self.repositories:
            rows.append(('repository', f'{repository["name"]} ({repository["path"]}): {repository["found"]}'))
        by_distribution: dict[str, list[dict[str, Any]]] = {}
        for plugin in self.plugins:
            by_distribution.setdefault(plugin['distribution'], []).append(plugin)
        for distribution, parts in by_distribution.items():
            provides = ', '.join(f'{p["group"].replace("_", " ")} {p["name"]}' for p in parts)
            state = 'enabled' if any(p['enabled'] for p in parts) else parts[0]['reason']
            rows.append(('plugin', f'{distribution} {parts[0]["version"]} ({provides}): {state}'))
        for name in self.missing_plugins:
            rows.append(('plugin', f'{name}: enabled in konstruct.toml, but not installed'))
        graph = self.graph
        if graph.get('exists'):
            state = (
                graph['unreadable']
                if 'unreadable' in graph
                else 'up to date'
                if not graph['changed']
                else f'{len(graph["changed"])} input(s) changed since: ' + ', '.join(graph['changed'][:5])
            )
            rows.append(('graph', f'{graph["path"]}, built {graph["age"]}; {state}'))
            states[len(rows) - 1] = 'attention' if graph['changed'] else 'ok'
        else:
            rows.append(('graph', 'not built yet; run `konstruct build`'))
            states[len(rows) - 1] = 'attention'
        rows.append(
            ('skills', ', '.join(self.skills) if self.skills else 'not installed; run `konstruct install`')
        )
        states[len(rows) - 1] = '' if self.skills else 'attention'
        return [(key, value, states.get(index, '')) for index, (key, value) in enumerate(rows)]

    def text(self, style: Style | None = None) -> str:
        """The result as the CLI prints it: plain text, as ``str()`` gives, or drawn for a
        terminal with ``style``."""
        from konstruct.interfaces.render import show, status_lines

        return show(status_lines, self, style)

    def __str__(self) -> str:
        return self.text()


def _files(files: list[str], noun: str) -> str:
    if not files:
        return f'no {noun}s found'
    shown = ', '.join(files[:4]) + (f' and {len(files) - 4} more' if len(files) > 4 else '')
    return f'{len(files)} {noun}{"s" if len(files) != 1 else ""}: {shown}'


_INPUT_KEYS = (
    'dbt_projects',
    'warehouses',
    'dag_files',
    'dagster_files',
    'spark_files',
    'sql_files',
    'looker_files',
    'powerbi_files',
    'definitions_files',
    'vocabulary_files',
    'ingestion_files',
    'ossie_files',
)


def _count(number: int, noun: str) -> str:
    return f'{number} {noun}{"" if number == 1 else "s"}' if number else ''


def _age(timestamp: float) -> str:
    seconds = max(0, int(time.time() - timestamp))
    for size, unit in ((86400, 'day'), (3600, 'hour'), (60, 'minute')):
        if seconds >= size:
            count = seconds // size
            return f'{count} {unit}{"s" if count != 1 else ""} ago'
    return 'just now'


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path)


def _sql_used_by(dag_files: list[Path]) -> set[Path]:
    """SQL files that DAG tasks run, which are analysed with their task rather than on their own."""
    from konstruct.inputs.airflow import parse_file

    used: set[Path] = set()
    for path in dag_files:
        try:
            dags = parse_file(path)
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        used |= {Path(t.sql_path).resolve() for d in dags for t in d.tasks.values() if t.sql_path}
    return used


def inspect(
    path: Path,
    out_dir: Path | None = None,
    home: Path | None = None,
    *,
    follow: bool = True,
    plugins: bool = True,
) -> Status:
    """What Konstruct finds at ``path``; with ``follow``, also in each repository it lists, though
    not in theirs, as the build does not follow them. Installed plugins are listed with whether
    the project enables them, without loading any (``konstruct plugins`` loads them)."""
    from konstruct.engine.build import repository_name
    from konstruct.engine.watch import changed_inputs
    from konstruct.interfaces.project import find_out_dir
    from konstruct.plugins.registry import Plugins

    try:
        config = load_config(path, out_dir)
    except UntrustedConfig as error:
        from konstruct.engine.trust import check

        written = read_config(path, out_dir)
        status = Status(
            root=written.root,
            config_file=_relative(written.config_file, written.root) if written.config_file else None,
        )
        status.trust = {**check(path).to_dict(), 'refused': f'not trusted: {error}'}
        return status
    config.override(plugins=plugins)
    registry = Plugins.for_config(config)
    root = config.root
    status = Status(
        root=root, config_file=_relative(config.config_file, root) if config.config_file else None
    )
    projects = (
        [p for r in config.dbt_projects if (p := dbt.read_project(r))]
        if config.dbt_projects is not None
        else dbt.find_projects(root)
    )
    for project in projects:
        if not project.manifest_path.is_file():
            artefacts = 'no target/manifest.json yet'
        elif dbt.artefacts_stale(project):
            artefacts = 'target/manifest.json is older than the project files'
        else:
            artefacts = 'artefacts up to date' + (
                '' if project.catalog_path.is_file() else ' (no catalog.json)'
            )
        status.dbt_projects.append(
            {'name': project.name, 'path': _relative(project.root, root), 'artefacts': artefacts}
        )
    status.dbt_on_path = shutil.which('dbt') is not None
    if follow:
        from konstruct.engine.trust import check

        status.trust = check(root).to_dict()
    status.warehouses = [
        {'type': w.type, 'name': w.describe(root), 'source': w.source}
        for w in discover_warehouses(config, projects)
    ]
    dags = discover_dag_files(config)
    status.dag_files = [_relative(p, root) for p in dags]
    status.spark_files = [_relative(p, root) for p in discover_spark_files(config)]
    status.sql_files = [_relative(p, root) for p in discover_sql_files(config, projects, _sql_used_by(dags))]
    status.definitions_files = [_relative(p, root) for p in discover_definitions_files(config)]
    status.looker_files = [_relative(p, root) for p in discover_looker_files(config)]
    status.powerbi_files = [_relative(p, root) for p in discover_powerbi_files(config)]
    status.dagster_files = [_relative(p, root) for p in discover_dagster_files(config)]
    status.ossie_files = [_relative(p, root) for p in discover_ossie_files(config)]
    status.ingestion_files = [_relative(p, root) for p, _ in discover_ingestion_files(config)]
    status.vocabulary_files = [_relative(p, root) for p in config.vocabulary_paths if p.is_file()]
    status.plugins = [p.to_dict() for p in registry.installed()]
    status.missing_plugins = registry.missing

    out = out_dir or find_out_dir(root)
    names: list[str] = []
    for repository in config.repositories if follow else []:
        names.append(repository_name(config, repository, names))
        entry: dict[str, Any] = {'name': names[-1], 'path': os.path.relpath(repository, root)}
        if not repository.is_dir():
            entry['found'] = 'not a directory'
        else:
            other = inspect(repository, out / 'repositories' / names[-1], home, follow=False)
            counts = [
                _count(len(other.dbt_projects), 'dbt project'),
                _count(len(other.warehouses), 'warehouse'),
                _count(len(other.dag_files), 'DAG file'),
                _count(len(other.dagster_files), 'Dagster file'),
                _count(len(other.spark_files), 'PySpark job'),
                _count(len(other.sql_files), 'SQL file'),
                _count(len(other.looker_files), 'LookML file'),
                _count(len(other.powerbi_files), 'TMDL file'),
                _count(len(other.definitions_files), 'definitions file'),
                _count(len(other.vocabulary_files), 'vocabulary file'),
                _count(len(other.ossie_files), 'Ossie model'),
                _count(len(other.ingestion_files), 'loader configuration file'),
            ]
            entry['found'] = ', '.join(c for c in counts if c) or 'no inputs found'
            entry['inputs'] = {k: v for k, v in other.to_dict().items() if k in _INPUT_KEYS}
        status.repositories.append(entry)
    graph_path = out / 'graph.json'
    if graph_path.is_file():
        unreadable: dict[str, str] = {}
        try:
            graph = Graph.read(graph_path)
        except FormatError as error:
            # Status is where people look when something is wrong: report it, as an empty graph.
            graph, unreadable = Graph(), {'unreadable': str(error)}
        status.graph = {
            'exists': True,
            'path': _relative(graph_path, root),
            'built_at': datetime.fromtimestamp(graph_path.stat().st_mtime).isoformat(timespec='seconds'),
            'age': _age(graph_path.stat().st_mtime),
            'changed': changed_inputs(graph_path, graph),
            'nodes': len(graph.nodes),
            'warnings': len(graph.warnings),
            **unreadable,
        }
        sources = [s for s in graph.meta.get('sources') or [] if isinstance(s, dict)]
        status.ontology_packs = [
            {'name': str(s['name']), 'detail': str(s.get('detail', ''))}
            for s in sources
            if s.get('input') == 'ontology pack'
        ]
        # Loader lineage and usage come from the warehouse at build time, not from files.
        status.built_from = [
            {
                'input': str(s['input']),
                'name': str(s.get('name') or s.get('path') or ''),
                'detail': str(s.get('detail', '')),
            }
            for s in sources
            if s.get('input') in ('fivetran', 'usage') or (s.get('input') == 'dlt' and not s.get('path'))
        ]
    else:
        status.graph = {'exists': False, 'path': _relative(graph_path, root)}

    home = home or Path.home()
    for scope, base in (('project', root), ('user', home)):
        for directory, readers in _SKILL_READERS.items():
            skills = base / directory
            if (skills / 'konstruct' / 'SKILL.md').is_file():
                # The value's shape is the one `status --json` has always given.
                status.skills.append(f'{scope} ({readers})')
                status.skill_directories.append(
                    {
                        'scope': scope,
                        'path': directory if scope == 'project' else f'~/{directory}',
                        'skills': sum(1 for _ in skills.glob('konstruct*/SKILL.md')),
                    }
                )
    return status


# The agents that read each skill directory, as `konstruct status` names them.
_SKILL_READERS = {
    '.claude/skills': 'Claude Code and Cursor',
    '.cursor/skills': 'Cursor',
    '.agents/skills': 'Codex, Gemini CLI, GitHub Copilot, Windsurf and Cursor',
}


def render_config(config: Config, status: Status, warehouses: list[WarehouseConfig]) -> str:
    """A commented konstruct.toml that spells out what discovery found, ready to edit."""
    lines = [
        '# Konstruct configuration. Everything is optional: without this file Konstruct discovers its',
        '# inputs, and this file records what it found so you can pin or change it.',
        '# See https://github.com/kodexlabsio/konstruct#configuration',
        '',
        '# dialect = "duckdb"              # SQL dialect; default: the dbt adapter, then the warehouse',
        '# default_catalog = "analytics"   # for unqualified tables in Airflow and SQL files',
        '# default_schema = "public"',
        '# exclude = ["scratch/**"]',
        '',
        '[dbt]',
    ]
    projects = [p['path'] for p in status.dbt_projects]
    lines.append(f'projects = {_toml_list(projects)}' if projects else '# projects = []')
    lines += ['run = true                        # run `dbt docs generate` when artefacts are stale', '']
    lines += ['[airflow]']
    dag_dirs = sorted({str(Path(f).parent) if str(Path(f).parent) != '.' else f for f in status.dag_files})
    lines.append(f'dags = {_toml_list(dag_dirs)}' if dag_dirs else '# dags = []')
    lines += ['', '[spark]']
    lines.append(f'paths = {_toml_list(status.spark_files)}' if status.spark_files else '# paths = ["jobs/"]')
    lines.append('# dialect = "spark"')
    lines += ['', '[sql]']
    lines.append(
        f'paths = {_toml_list(status.sql_files)}' if status.sql_files else '# paths = ["sql/**/*.sql"]'
    )
    lines += ['', '[looker]']
    looker_dirs = sorted(
        {str(Path(f).parent) if str(Path(f).parent) != '.' else f for f in status.looker_files}
    )
    lines.append(f'paths = {_toml_list(looker_dirs)}' if looker_dirs else '# paths = ["lookml/"]')
    lines += ['', '[powerbi]']
    models = sorted({_semantic_model(f) for f in status.powerbi_files})
    lines.append(f'paths = {_toml_list(models)}' if models else '# paths = ["reports/Sales.SemanticModel"]')
    lines += ['', '[ingestion]']
    lines.append(
        f'paths = {_toml_list(status.ingestion_files)}'
        if status.ingestion_files
        else '# paths = ["pipelines/", "meltano.yml"]  # loader configuration; default: discovered'
    )
    lines += [
        '',
        '[fivetran]',
        '# lineage = true                 # read the Fivetran Platform Connector lineage tables',
        '',
        '[dlt]',
        "# lineage = true                 # read the schemas dlt keeps in each dataset's _dlt_version",
    ]
    lines += ['', '[definitions]']
    lines.append(
        f'paths = {_toml_list(status.definitions_files)}'
        if status.definitions_files
        else '# paths = ["definitions.yml"]        # business terms; default: every definitions.yml'
    )
    lines.append('')
    if warehouses:
        lines.append('# Listing any warehouse here turns warehouse discovery off.')
    for warehouse in warehouses:
        lines += ['[[warehouse]]', f'type = "{warehouse.type}"']
        if warehouse.type == 'duckdb':
            lines.append(f'path = "{_relative(Path(str(warehouse.options["path"])), config.root)}"')
        elif warehouse.type in ('postgres', 'postgresql'):
            lines.append(
                'dsn = "${WAREHOUSE_DSN}"           # read from the environment; never commit secrets'
            )
            lines.append(f'# found in {warehouse.source}')
        lines.append('')
    if not warehouses:
        lines += [
            '# [[warehouse]]',
            '# type = "postgres"                # duckdb, postgres or iceberg',
            '# dsn = "${WAREHOUSE_DSN}"',
            '',
        ]
    return '\n'.join(lines).rstrip() + '\n'


def _semantic_model(path: str) -> str:
    """The ``<name>.SemanticModel`` folder a TMDL file is in, or its own folder."""
    parts = Path(path).parts
    for index in range(len(parts) - 1, -1, -1):
        if parts[index].endswith('.SemanticModel'):
            return Path(*parts[: index + 1]).as_posix()
    return Path(path).parent.as_posix()


def _toml_list(values: list[str]) -> str:
    return '[' + ', '.join(f'"{v}"' for v in values) + ']'
