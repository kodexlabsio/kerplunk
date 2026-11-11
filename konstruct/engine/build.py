"""Build the graph from every input, then write graph.json, REPORT.md, graph.html and ontology.yml."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from konstruct import __version__
from konstruct.core import jsonio
from konstruct.core.graph import RELATION_KINDS, Edge, Graph, Node, relation_key
from konstruct.core.sql import (
    PARSE_ERRORS,
    PLACEHOLDER,
    StatementLineage,
    analyse,
    dialect_for,
    strip_templating,
)
from konstruct.engine.config import (
    Config,
    ConfigError,
    UntrustedConfig,
    dbt_projects,
    discover_dag_files,
    discover_dagster_files,
    discover_definitions_files,
    discover_input_files,
    discover_ossie_files,
    discover_powerbi_files,
    discover_spark_files,
    discover_sql_files,
    discover_warehouses,
    load_config,
)
from konstruct.inputs import airflow, dagster, dbt
from konstruct.inputs.warehouse import MissingDriver, RelationMeta
from konstruct.plugins.api import briefly, json_problem, one_line
from konstruct.plugins.registry import Plugin, PluginError, Plugins, describe
from konstruct.semantics import definitions

if TYPE_CHECKING:
    from konstruct.plugins.api import Endpoint, Fragment, PluginEdge

log = logging.getLogger(__name__)
progress = logging.getLogger('konstruct.progress')
"""Counts within a stage (``12 of 40 models``), for the CLI's spinner; quieter than ``log``."""

CACHE_VERSION = 1

BUILTIN_COUNTS = frozenset(
    {
        'airflow_dags', 'dagster_assets', 'dagster_ops', 'dbt_models', 'dbt_projects', 'definitions',
        'fivetran_source_tables', 'landed_tables', 'loader_source_tables', 'ontology_pack_terms', 'ossie_metrics',
        'powerbi_tables', 'spark_jobs', 'spark_statements', 'sql_files', 'warehouses',
    }
)  # fmt: skip
"""The counts the build itself gives in :attr:`BuildResult.inputs`, which an input's counts
(``<input>_<key>``) may not take, whether or not this build gives them."""


def _small_file(path: Path, limit: int) -> bool:
    """Whether a path is a regular file of at most ``limit`` bytes, as discovery reads: a named
    pipe would block a read for ever, and a file of hundreds of megabytes is not configuration."""
    try:
        return path.is_file() and path.stat().st_size <= limit
    except OSError:
        return False


class Cache:
    """Results of the expensive steps (SQL lineage, DAG parsing), keyed by a hash of their inputs."""

    def __init__(self, path: Path, enabled: bool) -> None:
        self.path = path
        self.entries: dict[str, Any] = {}
        self.used: dict[str, Any] = {}
        self.hits = 0
        self.misses = 0
        if enabled and path.is_file():
            try:
                data = jsonio.loads(path.read_bytes())
            except (OSError, ValueError):
                data = {}
            if data.get('version') == CACHE_VERSION and data.get('konstruct') == __version__:
                self.entries = dict(data.get('entries', {}))

    @staticmethod
    def key(*parts: Any) -> str:
        blob = jsonio.dumps(parts, sort_keys=True, default=str).encode('utf-8')
        return hashlib.sha256(blob).hexdigest()

    def get_or_compute(self, key: str, compute: Callable[[], Any], *, count: bool = True) -> Any:
        if key in self.entries:
            self.hits += count
            value = self.entries[key]
        else:
            self.misses += count
            value = compute()
        self.used[key] = value
        return value

    def quiet(self) -> _QuietCache:
        """The same cache for derived outputs (the viewer's formulas), which are not counted
        among the analyses a build reports reusing."""
        return _QuietCache(self)

    def save(self) -> None:
        payload = {'version': CACHE_VERSION, 'konstruct': __version__, 'entries': self.used}
        jsonio.write_text(self.path, jsonio.dumps(payload))


@dataclass
class _QuietCache:
    cache: Cache

    def get_or_compute(self, key: str, compute: Callable[[], Any]) -> Any:
        return self.cache.get_or_compute(key, compute, count=False)


@dataclass
class BuildResult:
    graph: Graph
    """The graph that was built."""
    outputs: list[Path]
    """The files written (graph.json, REPORT.md, graph.html and ontology.yml)."""
    seconds: float
    """Time taken."""
    cache_hits: int
    """Analyses reused from the previous build."""
    cache_misses: int
    """Analyses computed afresh."""
    inputs: dict[str, int] = field(default_factory=dict)
    """Counts of what was read, such as dbt projects, warehouses, DAGs and terms."""


class RelationIndex:
    """Resolves relation keys written in SQL or asset URIs to graph nodes, allowing suffixes."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        self.by_suffix: dict[str, set[str]] = {}
        for node in graph.relations():
            self.add(node)

    def add(self, node: Node) -> None:
        if not node.relation:
            return
        parts = node.relation.split('.')
        for start in range(len(parts)):
            self.by_suffix.setdefault('.'.join(parts[start:]), set()).add(node.relation)

    def find(self, key: str, *, drop_leading: bool = False) -> Node | None:
        parts = [p for p in key.lower().split('.') if p and p != PLACEHOLDER.lower()]
        while parts:
            matches = self.by_suffix.get('.'.join(parts), set())
            if len(matches) == 1:
                return self.graph.node_for_relation(next(iter(matches)))
            if matches or not drop_leading or len(parts) <= 2:
                return None
            parts = parts[1:]
        return None


class Builder:
    def __init__(self, config: Config, *, update: bool) -> None:
        self.config = config
        self.graph = Graph()
        self.graph.meta = {
            'konstruct_version': __version__,
            'root': str(config.root),
            'generated_at': datetime.now(UTC).isoformat(timespec='seconds'),
        }
        self.cache = Cache(config.out_dir / 'cache.json', enabled=update)
        self.vocabulary_files: set[str] = set()
        self.workers = _Workers()
        self.index = RelationIndex(self.graph)
        self.dialect: str | None = config.dialect
        self.default_catalog = config.default_catalog
        self.default_schema = config.default_schema
        self.inputs: dict[str, int] = {}
        # One entry per input, for the build summary and `konstruct status`.
        self.sources: list[dict[str, str]] = []
        self.watched: set[str] = set()
        self.plugins = Plugins.for_config(config)
        # Connections the repository's own configuration names but the user has not trusted.
        self.untrusted: list[str] = []
        # Relations a loader configuration file says that loader lands.
        self.declared: dict[str, str] = {}

    # Shared helpers

    def watch(self, *paths: Path) -> None:
        for path in paths:
            if path.exists():
                self.watched.add(self.rel_path(path))

    def rel_path(self, path: Path | str) -> str:
        return os.path.relpath(Path(path).resolve(), self.config.root)

    def relation(self, key: str, origin: str, *, drop_leading: bool = False) -> Node:
        """The node for a relation, creating an inferred ``table`` node if none exists."""
        found = self.index.find(key, drop_leading=drop_leading)
        if found is not None:
            if origin not in found.origins:
                found.origins.append(origin)
            return found
        normalised = relation_key(*[p for p in key.split('.') if p != PLACEHOLDER])
        existing = self.graph.node_for_relation(normalised)
        if existing is not None:
            return existing
        node = self.graph.add_node(
            Node(
                id=f'table.{normalised}',
                kind='table',
                name=normalised.rsplit('.', 1)[-1],
                relation=normalised,
                origins=[origin],
                attrs={'inferred': True},
            )
        )
        self.index.add(node)
        return node

    def columns_known(self, relation: Node) -> bool:
        """Whether a relation's columns are listed by something that defines it (the warehouse,
        dbt, or SQL that writes it), so a dashboard naming another column names a missing one. A
        table that is only read elsewhere grows the columns that are named."""
        existing = self.graph.columns_of(relation.id)
        return (
            not relation.attrs.get('inferred')
            or (bool(relation.path) and bool(existing))
            or any(not c.attrs.get('inferred') for c in existing)
        )

    def column(self, parent: Node, name: str, origin: str) -> Node:
        existing = self.graph.column(parent.id, name)
        if existing is not None:
            return existing
        if '.' in name and parent.attrs.get('in_warehouse'):
            # The warehouse knows this relation's columns: a path it does not have (a map key such
            # as attrs['colour'], read at run time) belongs to the longest column it does have.
            parts = name.split('.')
            for end in range(len(parts) - 1, 0, -1):
                prefix = self.graph.column(parent.id, '.'.join(parts[:end]))
                if prefix is not None:
                    return prefix
        return self.graph.add_column(parent, name, origin=origin, attrs={'inferred': True})

    def schema_columns(self, nodes: Iterable[Node] | None = None) -> dict[str, dict[str, str]]:
        schema: dict[str, dict[str, str]] = {}
        for node in nodes if nodes is not None else self.graph.relations():
            if node.relation and node.kind in RELATION_KINDS:
                columns = {
                    c.name: c.data_type or 'UNKNOWN'
                    for c in self.graph.columns_of(node.id)
                    if not c.attrs.get('nested') and not c.attrs.get('documented_only')
                }
                if columns:
                    schema[node.relation] = columns
        return schema

    def apply(
        self,
        statement: StatementLineage,
        *,
        target: Node | None,
        origin: str,
        label: str,
        via: str | None = None,
    ) -> None:
        """Turn one statement's lineage into feeds and derived_from edges."""
        attrs: dict[str, Any] = {'origin': origin}
        if via:
            attrs['via'] = via
        for error in statement.errors:
            self.graph.warn(f'{label}: {error}')
        if target is None and statement.target:
            target = self.relation(statement.target, origin)
        if target is None:
            return

        def record(edge: Edge | None) -> None:
            # Several tasks can write the same edge (the same op twice in a job, two DAGs
            # running one SQL file); the first is ``via``, the rest ``also_via``.
            if edge is not None and via and edge.attrs.get('via') not in (None, via):
                others = edge.attrs.setdefault('also_via', [])
                if via not in others:
                    others.append(via)

        for read in statement.reads:
            source = self.relation(read, origin)
            record(self.graph.add_edge(source.id, target.id, 'feeds', **attrs))
        if statement.conditions:
            # Joins, filters and grouping, in terms of physical columns: what shapes the rows.
            target.attrs['conditions'] = statement.conditions
        # Where the relations it reads sit, which decides where their own conditions apply.
        for key, value in (
            ('nullable', statement.nullable),
            ('read_before', statement.read_before),
            ('star_reads', statement.star_reads),
        ):
            if value:
                target.attrs[key] = value
        for name, sources in statement.columns.items():
            out = self.column(target, name, origin)
            expression = statement.expressions.get(name)
            if expression and not expression.endswith('.*'):
                # The column as SQL over physical columns, with CTEs and aliases inlined.
                out.attrs['expression'] = expression
            for item in sources:
                upstream = self.column(self.relation(item.relation, origin), item.column, origin)
                record(
                    self.graph.add_edge(
                        out.id, upstream.id, 'derived_from', transform=item.transform, **attrs
                    )
                )
        for relation, column in statement.references:
            used = self.column(self.relation(relation, origin), column, origin)
            record(self.graph.add_edge(used.id, target.id, 'feeds', role='reference', **attrs))

    def analyse_cached(
        self,
        sql: str,
        *,
        kind: str,
        schema_key: str,
        target: str | None = None,
        dialect: str | None = None,
    ) -> list[StatementLineage]:
        dialect = dialect or self.dialect
        key = Cache.key(kind, sql, dialect, self.default_catalog, self.default_schema, schema_key, target)

        def compute() -> list[dict[str, Any]]:
            return [
                s.to_dict()
                for s in analyse(
                    sql,
                    dialect=dialect,
                    schema=self.schema_columns(),
                    default_catalog=self.default_catalog,
                    default_db=self.default_schema,
                    target=target,
                )
            ]

        return [StatementLineage.from_dict(item) for item in self.cache.get_or_compute(key, compute)]

    # Steps

    def run(self) -> Graph:
        log.info('reading dbt projects')
        projects = self.load_dbt()
        log.info('reading warehouses')
        self.load_warehouses(projects)
        feeds = self.read_ingestion()
        self.load_loaders()
        self.apply_ingestion(feeds + self.read_dlt_versions())
        self.link_shared_relations()
        self.warn_missing_catalogs()
        self.index = RelationIndex(self.graph)
        log.info('analysing dbt models')
        self.dbt_lineage()
        schema_key = Cache.key(self.schema_columns())
        self.warehouse_view_lineage(schema_key)
        log.info('reading DAGs, jobs and SQL files')
        used_sql = self.load_airflow(schema_key)
        self.load_dagster(schema_key)
        self.load_spark(schema_key)
        self.load_sql_files(projects, used_sql, schema_key)
        log.info('reading BI models, definitions and usage')
        self.load_inputs(schema_key)
        self.load_powerbi()
        self.load_definitions()
        self.load_ossie()
        self.load_usage()
        if self.untrusted:
            from konstruct.engine.trust import untrusted_warning

            self.graph.warn(untrusted_warning(self.config, self.untrusted))
        for directory, links in sorted(self.config.outside_links.items()):
            # One warning per directory, however many inputs found the same links.
            self.graph.warn(
                f'{self.rel_path(directory)}: did not read {", ".join(sorted(links))}, which '
                f'{"leads" if len(links) == 1 else "lead"} outside the project; '
                'run `konstruct trust` after reviewing it'
            )
        self.graph.meta['dialect'] = self.dialect
        self.graph.meta['default_catalog'] = self.default_catalog
        self.graph.meta['default_schema'] = self.default_schema
        self.graph.meta['inputs'] = self.inputs
        self.graph.meta['sources'] = self.sources
        if self.config.config_file:
            self.watch(self.config.config_file)
        self.graph.meta['watched'] = sorted(self.watched)
        return self.graph

    def load_dbt(self) -> list[dbt.DbtProject]:
        projects = dbt_projects(self.config)
        self.loads: list[dbt.DbtLoad] = []
        for project in projects:
            run_dbt, why_not = self.config.run_dbt, 'running dbt is disabled'
            if run_dbt and dbt.artefacts_stale(project) and not self.config.trusted:
                # dbt runs the repository's macros and models with the user's environment and
                # profile, whatever the profile names, so only a trusted project runs it.
                run_dbt, why_not = False, 'dbt is not run until the project is trusted'
                self.untrusted.append(f'running dbt in {project.name}')
            status = dbt.ensure_artefacts(project, run_dbt=run_dbt, graph=self.graph, why_not=why_not)
            self.watch(project.root / 'dbt_project.yml', *project.source_paths)
            artefacts = [project.manifest_path, project.catalog_path]
            if len(self.config.inside(artefacts)) < len(artefacts):
                loaded = None  # the dbt artefacts link outside the project, which needs trust
            else:
                loaded = dbt.load(project, self.graph, dialect=self.config.dialect, keep=self.config.inside)
            entry = {
                'input': 'dbt',
                'name': project.name,
                'path': self.rel_path(project.root),
                'note': status,
            }
            self.sources.append(entry)
            if loaded is None:
                entry['status'] = 'failed'
                continue
            self.watch(project.manifest_path, project.catalog_path)
            entry['detail'] = ', '.join(
                f'{count} {kind}{"s" if count != 1 else ""}' for kind, count in loaded.counts.items() if count
            )
            self.loads.append(loaded)
            self.dialect = self.dialect or loaded.dialect
            self.default_catalog = self.default_catalog or loaded.default_catalog
            self.default_schema = self.default_schema or loaded.default_schema
        self.inputs['dbt_projects'] = len(self.loads)
        return projects

    def load_warehouses(self, projects: list[dbt.DbtProject]) -> None:
        count = 0
        self.usage_connectors: list[tuple[Any, Any]] = []
        self.warehouse_relations: list[tuple[Any, list[RelationMeta]]] = []
        for warehouse in discover_warehouses(self.config, projects):
            try:
                connector = warehouse.connector(self.config.root)
                relations = connector.fetch()
            except UntrustedConfig as error:
                if warehouse.source == 'discovered':
                    # Trust does not cover a file found in the tree, so the advice is the error's own.
                    self.graph.warn(f'warehouse {warehouse.describe(self.config.root)}: {error}')
                else:
                    self.untrusted.append(warehouse.describe(self.config.root))
                self.sources.append(
                    {
                        'input': warehouse.type,
                        'name': warehouse.describe(self.config.root),
                        'status': 'skipped',
                        'note': str(error)
                        if warehouse.source == 'discovered'
                        else 'not trusted; run `konstruct trust`',
                    }
                )
                continue
            except ConfigError:
                raise
            except MissingDriver as error:
                self.graph.warn(f'warehouse {warehouse.describe(self.config.root)}: {error}')
                self.sources.append(
                    {
                        'input': warehouse.type,
                        'name': warehouse.describe(self.config.root),
                        'status': 'failed',
                        'note': str(error),
                    }
                )
                continue
            except Exception as error:  # any driver error becomes a warning
                message = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
                self.graph.warn(f'warehouse {warehouse.describe(self.config.root)}: {message}')
                self.sources.append(
                    {
                        'input': warehouse.type,
                        'name': warehouse.describe(self.config.root),
                        'status': 'failed',
                        'note': message,
                    }
                )
                continue
            count += 1
            self.usage_connectors.append((warehouse, connector))
            self.warehouse_relations.append((connector, relations))
            self.sources.append(
                {
                    'input': warehouse.type,
                    'name': warehouse.describe(self.config.root),
                    'detail': f'{len(relations)} relation{"s" if len(relations) != 1 else ""}',
                    'note': warehouse.source,
                }
            )
            self.dialect = self.dialect or connector.dialect
            if self.default_schema is None and connector.default_schema and relations:
                # Unqualified tables in SQL files and DAGs then resolve inside this warehouse.
                self.default_catalog = self.default_catalog or relations[0].parts[0]
                self.default_schema = connector.default_schema
            for meta in relations:
                self.merge_relation(meta, connector.label)
        self.inputs['warehouses'] = count

    def loader_task(self, loader: str, name: str, **attrs: str) -> Node:
        """The task of a loader that lands tables: one per schema found in the warehouse, or one
        per pipeline or connection a configuration file names. An orchestrator's task can have
        the same id (``task.dagster.<asset>`` for ``loader: dagster``), so another loader's or
        an orchestrator's node is never reused: the id takes a number instead."""
        base = node_id = f'task.{loader}.{name}'
        number = 1
        while (existing := self.graph.nodes.get(node_id)) is not None:
            if existing.attrs.get('loader') == loader:
                return existing
            number += 1
            node_id = f'{base}~{number}'
        return self.graph.add_node(
            Node(
                id=node_id,
                kind='task',
                name=f'{loader} {name}',
                origins=[loader],
                attrs={'type': f'{loader} sync', 'loader': loader, **attrs},
            )
        )

    def read_ingestion(self) -> list[Any]:
        """The loader configuration files (see :mod:`konstruct.inputs.ingestion`): what each
        loader reads from its source systems and the tables it lands, each file parsed once and
        cached by its content."""
        from konstruct.engine.config import discover_ingestion_files
        from konstruct.inputs import ingestion

        feeds: list[ingestion.Feed] = []
        units: dict[tuple[str, Path], list[tuple[str, str]]] = {}
        companions: set[str] = set()
        for path, kind in discover_ingestion_files(self.config):
            levels = ingestion.GROUPED.get(kind, 0)
            group = path.parents[levels - 1] if levels and len(path.parents) >= levels else path
            unit = units.setdefault((kind, group), [])
            for companion in [
                path,
                *self.config.inside(
                    sorted(
                        c
                        for glob in ingestion.COMPANIONS.get(kind, ())
                        for c in path.parent.glob(glob)
                        if _small_file(c, ingestion.MAX_BYTES)
                    )
                ),
            ]:
                try:
                    text = companion.read_text(encoding='utf-8', errors='replace')
                except OSError as error:
                    self.graph.warn(
                        f'{ingestion.LOADER_OF[kind]}: cannot read {self.rel_path(companion)}: {error}'
                    )
                    continue
                self.watch(companion)
                unit.append((self.rel_path(companion), text))
                if companion != path:
                    companions.add(self.rel_path(companion))
        for (kind, _), files in units.items():
            key = Cache.key('ingestion', kind, files)

            def compute(kind: str = kind, files: list[tuple[str, str]] = files) -> dict[str, Any]:
                return ingestion.read_files(kind, files).to_dict()

            reading = ingestion.Reading.from_dict(self.cache.get_or_compute(key, compute))
            for problem in reading.problems:
                self.graph.warn(f'{ingestion.LOADER_OF[kind]}: {problem}')
            feeds.extend(reading.feeds)
            for rel, _ in files:
                landed = {f.target for f in reading.feeds if f.path == rel}
                if rel in companions or (not landed and kind in ingestion.GROUPED):
                    continue
                loaders = sorted({f.loader for f in reading.feeds if f.path == rel}) or [
                    ingestion.LOADER_OF[kind]
                ]
                self.sources.append(
                    {
                        'input': ', '.join(loaders),
                        'path': rel,
                        'detail': f'{len(landed)} landed {"table" if len(landed) == 1 else "tables"}',
                    }
                )
        # The tables the files say a loader lands, which refine the family the warehouse shows.
        self.index = RelationIndex(self.graph)
        self.declared = {}
        for feed in feeds:
            found = None if feed.stream else self.index.find(relation_key(*feed.target), drop_leading=True)
            if found is not None:
                self.declared.setdefault(found.id, feed.loader)
        return feeds

    def read_dlt_versions(self) -> list[Any]:
        """From each dataset's ``_dlt_version`` table, the newest schema dlt loaded into it: the
        resource that fills each table. One query per table, read only."""
        from konstruct.inputs import ingestion, loaders
        from konstruct.inputs.warehouse import query

        feeds: list[ingestion.Feed] = []
        if not self.config.dlt_lineage:
            return feeds
        for connector, relations in self.warehouse_relations:
            for parts in loaders.dlt_versions(relations):
                where = '.'.join(parts)
                try:
                    rows = query(connector, loaders.dlt_version_sql(parts, connector.dialect))
                except Exception as error:  # a missing permission or a changed table
                    message = (
                        str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
                    )
                    self.graph.warn(f'dlt: {where}: {message}')
                    continue
                if rows is None:
                    continue
                reading = ingestion.dlt_version_feeds(rows, parts[:-1], where)
                for problem in reading.problems:
                    self.graph.warn(f'dlt: {problem}')
                feeds.extend(reading.feeds)
                count = len(reading.feeds)
                self.sources.append(
                    {
                        'input': 'dlt',
                        'name': where,
                        'detail': f'{count} landed {"table" if count == 1 else "tables"}',
                    }
                )
        return feeds

    def apply_ingestion(self, feeds: list[Any]) -> None:
        """Each feed a configuration file describes: its source table, outside the warehouse and
        named after its system (``postgres.app.public.orders``), feeds the loader's task, which
        feeds the table it lands, column by column where the file names the columns. The task is
        the warehouse's sync task for that table when the same loader landed it.

        Only a file listed in ``[ingestion] paths`` adds a table the graph does not have: one that
        discovery found may be an application's file that looks like a loader's, so its feeds
        reach only tables the warehouse, dbt or SQL already show, and a stream (a Kafka topic, an
        Estuary collection) only when a chain of feeds leads from it to one of those tables."""
        if self.config.ingestion_paths is None:
            feeds = self.reaching_known_tables(feeds)
        applied: set[tuple[str, ...]] = set()
        for feed in feeds:
            evidence = f'{feed.path}:{feed.line}' if feed.line else feed.path
            if feed.stream:
                target = self.outside_table(feed.loader, feed.target)
            else:
                key = relation_key(*feed.target)
                target = self.relation(key, feed.loader, drop_leading=True)
                target.attrs.setdefault('loaded_by', feed.loader)
            source = self.outside_table(feed.loader, feed.source)
            applied.add(feed.source)
            sync = next(
                (
                    self.graph.nodes[e.source]
                    for e in self.graph.in_edges(target.id, 'feeds')
                    if self.graph.nodes[e.source].attrs.get('loader') == feed.loader
                ),
                None,
            ) or self.loader_task(feed.loader, feed.task, pipeline=feed.task)
            if (self.config.root / feed.path).is_file():
                sync.path = sync.path or feed.path
            sync.attrs.setdefault('pipeline', feed.task)
            self.graph.add_edge(source.id, sync.id, 'feeds', origin=feed.loader, evidence=evidence)
            self.graph.add_edge(sync.id, target.id, 'feeds', origin=feed.loader, evidence=evidence)
            known = self.columns_known(target)
            for landed, read, transform in feed.columns:
                column = self.graph.column(target.id, landed)
                if column is None and known and not feed.stream:
                    continue
                column = column or self.graph.add_column(
                    target, landed, origin=feed.loader, attrs={'inferred': True}
                )
                upstream = self.graph.column(source.id, read) or self.graph.add_column(
                    source, read, origin=feed.loader
                )
                self.graph.add_edge(
                    column.id,
                    upstream.id,
                    'derived_from',
                    transform=transform,
                    loaded=True,
                    origin=feed.loader,
                )
        if applied:
            self.inputs['loader_source_tables'] = len(applied)

    def reaching_known_tables(self, feeds: list[Any]) -> list[Any]:
        """The feeds into tables the graph has, and the streams that lead to them: from those
        tables' sources back through each feed into a stream, a topic or collection at a time."""
        kept = [
            f
            for f in feeds
            if not f.stream and self.index.find(relation_key(*f.target), drop_leading=True) is not None
        ]
        into: dict[tuple[str, ...], list[Any]] = {}
        for feed in feeds:
            if feed.stream:
                into.setdefault(feed.target, []).append(feed)
        reached = [f.source for f in kept]
        seen = set(reached)
        while reached:
            for feed in into.pop(reached.pop(), []):
                kept.append(feed)
                if feed.source not in seen:
                    seen.add(feed.source)
                    reached.append(feed.source)
        order = {id(f): i for i, f in enumerate(feeds)}
        return sorted(kept, key=lambda f: order[id(f)])

    def outside_table(self, loader: str, parts: tuple[str, ...]) -> Node:
        """A table, topic or collection of a system outside the warehouse, such as a source
        database a loader reads. It has no relation name, so SQL never resolves to it."""
        key = relation_key(*parts)
        return self.graph.nodes.get(f'system.{key}') or self.graph.add_node(
            Node(
                id=f'system.{key}',
                kind='table',
                name='.'.join(parts),
                origins=[loader],
                attrs={'system': parts[0], 'source_table': '.'.join(parts[1:]), 'outside_warehouse': True},
            )
        )

    def load_loaders(self) -> None:
        """Loaders: a loader task per schema for the tables each lands (known by the marks it
        leaves, see :mod:`konstruct.inputs.loaders`), and, from the Fivetran Platform Connector's
        lineage tables, each connection's source tables and columns with column lineage into the
        tables it lands."""
        from konstruct.inputs import loaders
        from konstruct.inputs.warehouse import query

        def task(loader: str, schema: str) -> Node:
            return self.loader_task(loader, schema, schema=schema)

        nodes: list[Node] = []
        tables: list[loaders.Table] = []
        declared: dict[int, str] = {}
        for relation in sorted(self.graph.relations(), key=lambda n: n.id):
            if not relation.relation:
                continue
            if relation.attrs.get('declared_loader'):
                declared[len(nodes)] = loaders.loader_name(str(relation.attrs['declared_loader']))
            elif not relation.attrs.get('in_warehouse'):
                continue
            elif relation.id in self.declared:
                declared[len(nodes)] = self.declared[relation.id]
            schema_key, _, name = relation.relation.rpartition('.')
            nodes.append(relation)
            tables.append(
                loaders.Table(
                    schema_key, name, frozenset(c.name.lower() for c in self.graph.columns_of(relation.id))
                )
            )
        landed = 0
        landings = loaders.detect(tables, declared)
        for index, (relation, table, landing) in enumerate(zip(nodes, tables, landings, strict=True)):
            if landing is None and index in declared:
                # Nothing in the warehouse shows a loader, or the table is not there: the project's
                # word stands.
                landing = loaders.Landing(declared[index])
            if landing is None:
                continue
            relation.attrs['loaded_by'] = landing.loader
            if landing.role != 'landed':
                relation.attrs['loader_role'] = landing.role
            if landing.namespace:
                # A raw table feeds the task that loads its namespace's tables from it.
                database = table.schema.rpartition('.')[0]
                self.graph.add_edge(
                    relation.id, task(landing.loader, relation_key(database, landing.namespace)).id, 'feeds'
                )
                continue
            self.graph.add_edge(
                task(landing.loader, table.schema).id, relation.id, 'feeds', origin=landing.loader
            )
            landed += landing.role == 'landed'
        if landed:
            self.inputs['landed_tables'] = landed
        if not self.config.fivetran_lineage:
            return
        for connector, relations in self.warehouse_relations:
            for schema in loaders.lineage_schemas(relations):
                where = f'fivetran: lineage in {".".join(schema.parts)}'
                prefix = schema.parts[:-1]
                schemas = {r.parts[-2] for r in relations if len(r.parts) >= 2 and r.parts[:-2] == prefix}
                try:
                    found = query(connector, loaders.destination_sql(schema, connector.dialect))
                    own, candidates = loaders.destination(found or [])
                    if own is None:
                        self.graph.warn(
                            f'{where}: cannot tell which Fivetran destination this warehouse is '
                            f'({candidates} destinations, and not one alone lands {schema.parts[-1]})'
                        )
                        continue
                    columns_sql, tables_sql = loaders.lineage_sql(schema, connector.dialect, own, schemas)
                    column_rows = query(connector, columns_sql)
                    table_rows = query(connector, tables_sql)
                except Exception as error:  # a missing permission or a changed schema
                    message = (
                        str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
                    )
                    self.graph.warn(f'{where}: {message}')
                    continue
                if column_rows is None or table_rows is None:
                    continue
                count = self.apply_fivetran_lineage(schema, loaders.lineage(column_rows, table_rows), task)
                self.sources.append(
                    {
                        'input': 'fivetran',
                        'name': '.'.join(schema.parts),
                        'detail': f'{count} source table{"s" if count != 1 else ""}',
                    }
                )

    def apply_fivetran_lineage(self, schema: Any, rows: list[Any], task: Callable[[str, str], Node]) -> int:
        """Source tables and columns, outside the warehouse, feeding the tables Fivetran lands
        from them. They are named after their connection and are not found by relation name, so
        a landed table keeps its own short names. Returns the number of source tables added."""
        prefix = list(schema.parts[:-1])
        sources: set[str] = set()
        for row in rows:
            landed = self.graph.node_for_relation(
                relation_key(*prefix, row.destination_schema, row.destination_table)
            )
            if landed is None:
                continue
            key = relation_key(row.connection, row.source_schema, row.source_table)
            source = self.graph.nodes.get(f'fivetran.{key}') or self.graph.add_node(
                Node(
                    id=f'fivetran.{key}',
                    kind='table',
                    name=f'{row.connection}.{row.source_schema}.{row.source_table}',
                    origins=['fivetran'],
                    attrs={
                        'system': row.connection,
                        'source_table': f'{row.source_schema}.{row.source_table}',
                        'outside_warehouse': True,
                    },
                )
            )
            sources.add(source.id)
            sync = task('fivetran', relation_key(*prefix, row.destination_schema))
            sync.attrs.setdefault('connection', row.connection)
            self.graph.add_edge(source.id, sync.id, 'feeds', origin='fivetran')
            self.graph.add_edge(sync.id, landed.id, 'feeds', origin='fivetran')
            if row.source_column and row.destination_column:
                target = self.graph.column(landed.id, row.destination_column)
                if target is None:
                    continue
                column = self.graph.column(source.id, row.source_column) or self.graph.add_column(
                    source, row.source_column, data_type=row.source_type, origin='fivetran'
                )
                self.graph.add_edge(
                    target.id, column.id, 'derived_from', transform='direct', loaded=True, origin='fivetran'
                )
        if sources:
            self.inputs['fivetran_source_tables'] = self.inputs.get('fivetran_source_tables', 0) + len(
                sources
            )
        return len(sources)

    def merge_relation(self, meta: RelationMeta, origin: str) -> None:
        nodes = self.graph.nodes_for_relation(meta.key)
        if not nodes:
            self._merge_into(None, meta, origin)
        for node in nodes:
            self._merge_into(node, meta, origin)

    def _merge_into(self, node: Node | None, meta: RelationMeta, origin: str) -> None:
        attrs = {'table_type': meta.kind, **meta.attrs}
        if '.'.join(meta.parts) != meta.key:
            # The key is lower case; a sampled verify needs the names as the warehouse spells them.
            attrs['parts'] = '.'.join(meta.parts)
        if node is None:
            node = self.graph.add_node(
                Node(
                    id=f'table.{meta.key}',
                    kind='table',
                    name=meta.name,
                    relation=meta.key,
                    description=meta.comment,
                    origins=[origin],
                    attrs=attrs,
                )
            )
        else:
            if origin not in node.origins:
                node.origins.append(origin)
            node.description = node.description or meta.comment
            for key, value in attrs.items():
                node.attrs.setdefault(key, value)
            node.attrs.pop('inferred', None)
        node.attrs['in_warehouse'] = True
        if meta.view_sql and 'dbt' not in node.origins:
            node.attrs['view_sql'] = meta.view_sql
        for column in meta.columns:
            existing = self.graph.column(node.id, column.name)
            if existing is None:
                self.graph.add_column(
                    node,
                    column.name,
                    data_type=column.data_type,
                    description=column.comment,
                    origin=origin,
                    attrs={**column.attrs, 'in_warehouse': True},
                )
            else:
                existing.data_type = existing.data_type or column.data_type
                existing.description = existing.description or column.comment
                if origin not in existing.origins:
                    existing.origins.append(origin)
                existing.attrs.pop('documented_only', None)
                existing.attrs.pop('inferred', None)
                existing.attrs['in_warehouse'] = True
                for key, value in column.attrs.items():
                    existing.attrs.setdefault(key, value)

    def link_shared_relations(self) -> None:
        """Connect nodes that describe one physical relation, such as a seed and the dbt source
        that reads the table it loads: the producer feeds the reader, column by column."""
        link_shared_relations(self.graph)

    def warn_missing_catalogs(self) -> None:
        for loaded in self.loads:
            if loaded.has_catalog:
                continue
            nodes = [
                n
                for n in self.graph.relations()
                if n.id.startswith(('model.', 'source.', 'seed.', 'snapshot.'))
            ]
            unseen = [n for n in nodes if not n.attrs.get('in_warehouse')]
            if unseen:
                self.graph.warn(
                    f'dbt: {loaded.project.name}: no catalog.json and {len(unseen)} dbt node(s) not found in '
                    'a warehouse, so their column types are unknown; run `dbt docs generate`'
                )

    def dbt_lineage(self) -> None:
        count = 0
        prepared: dict[str, tuple[str, Any]] = {}
        try:
            for loaded in self.loads:
                ordered = _topological(loaded.models)
                ids = {m.node_id for m in ordered}
                applied: set[str] = set()
                considered: set[str] = set()
                for index, model in enumerate(ordered):
                    if model.node_id not in considered:
                        # Every later model whose upstream models are all applied can be analysed
                        # now, side by side; each result is used only if the model's own key, at
                        # its turn, is the one it was analysed with.
                        ready = [
                            m
                            for m in ordered[index:]
                            if m.node_id not in considered
                            and all(u not in ids or u in applied for u in m.upstream)
                        ]
                        considered.update(m.node_id for m in ready)
                        self.prepare_models(ready, loaded, prepared)
                    node = self.graph.nodes[model.node_id]
                    key, schema, dialect = self.model_key(model, loaded)
                    early = prepared.pop(model.node_id, None)

                    def compute(
                        model: dbt.ModelSql = model,
                        schema: dict[str, Any] = schema,
                        dialect: str | None = dialect,
                        loaded: dbt.DbtLoad = loaded,
                        early: tuple[str, Any] | None = early,
                        key: str = key,
                    ) -> Any:
                        if early is not None and early[0] == key:
                            self.workers.used += 1
                            return early[1]
                        return _analyse_model(
                            (
                                model.sql,
                                dialect,
                                schema,
                                loaded.default_catalog,
                                loaded.default_schema,
                                model.relation,
                            )
                        )

                    statements = [
                        StatementLineage.from_dict(s) for s in self.cache.get_or_compute(key, compute)
                    ]
                    selects = [s for s in statements if s.kind in ('select', 'unparsed')]
                    if not model.compiled:
                        node.attrs['lineage'] = 'from uncompiled SQL'
                    for statement in selects[-1:]:
                        self.apply(statement, target=node, origin='dbt', label=f'dbt model {node.name}')
                    applied.add(model.node_id)
                    count += 1
                    progress.debug('%d of %d models', index + 1, len(ordered))
        finally:
            # Also when the build fails, so no worker outlives it.
            self.workers.close()
        self.inputs['dbt_models'] = count

    def model_key(self, model: dbt.ModelSql, loaded: dbt.DbtLoad) -> tuple[str, dict[str, Any], str | None]:
        """A model's cache key, the schema of the models it reads, and its dialect."""
        node = self.graph.nodes[model.node_id]
        upstream = [self.graph.nodes[u] for u in model.upstream if u in self.graph.nodes]
        schema = self.schema_columns(upstream)
        key = Cache.key('dbt', model.sql, self.dialect, model.relation, schema, node.attrs.get('checksum'))
        return key, schema, loaded.dialect or self.dialect

    def prepare_models(
        self, models: list[dbt.ModelSql], loaded: dbt.DbtLoad, prepared: dict[str, tuple[str, Any]]
    ) -> None:
        """Analyse the models that are not cached in worker processes, when there are enough of
        them to repay starting the workers."""
        if len(models) < PARALLEL_MINIMUM or _workers() < 2 or self.workers.failed:
            return
        work = []
        for model in models:
            key, schema, dialect = self.model_key(model, loaded)
            if key not in self.cache.entries:
                work.append(
                    (
                        model.node_id,
                        key,
                        (
                            model.sql,
                            dialect,
                            schema,
                            loaded.default_catalog,
                            loaded.default_schema,
                            model.relation,
                        ),
                    )
                )
        if len(work) < PARALLEL_MINIMUM:
            return
        results = self.workers.map([list(w[2]) for w in work])
        if results is None:
            return
        for (node_id, key, _), result in zip(work, results, strict=True):
            prepared[node_id] = (key, result)

    def warehouse_view_lineage(self, schema_key: str) -> None:
        for node in list(self.graph.relations()):
            view_sql = node.attrs.pop('view_sql', None)
            if not view_sql or not node.relation:
                continue
            parts = node.relation.split('.')
            saved = (self.default_catalog, self.default_schema)
            if len(parts) == 3:
                self.default_catalog, self.default_schema = parts[0], parts[1]
            try:
                statements = self.analyse_cached(
                    view_sql, kind='view', schema_key=schema_key, target=node.relation
                )
            finally:
                self.default_catalog, self.default_schema = saved
            for statement in statements:
                self.apply(statement, target=node, origin='warehouse', label=f'view {node.name}')

    def load_airflow(self, schema_key: str) -> set[Path]:
        used_sql: set[Path] = set()
        files = discover_dag_files(self.config)
        dags_seen = 0
        for path in files:
            self.watch(path)
            try:
                content = path.read_bytes()
            except OSError as error:
                self.graph.warn(f'airflow: cannot read {self.rel_path(path)}: {error}')
                self.sources.append({'input': 'airflow', 'path': self.rel_path(path), 'status': 'failed'})
                continue
            key = Cache.key('dag', hashlib.sha256(content).hexdigest(), str(path))

            def compute(path: Path = path) -> Any:
                try:
                    return [d.to_dict() for d in airflow.parse_file(path)]
                except SyntaxError as error:
                    return {'error': f'syntax error on line {error.lineno}: {error.msg}'}

            parsed = self.cache.get_or_compute(key, compute)
            if isinstance(parsed, dict):
                self.graph.warn(f'airflow: {self.rel_path(path)}: {parsed["error"]}')
                self.sources.append(
                    {
                        'input': 'airflow',
                        'path': self.rel_path(path),
                        'status': 'failed',
                        'note': parsed['error'],
                    }
                )
                continue
            tasks = 0
            for item in parsed:
                dag = airflow.DagInfo.from_dict(item)
                for task in dag.tasks.values():
                    if task.sql_path and not self.config.inside([Path(task.sql_path)]):
                        task.sql, task.sql_path = None, None  # a template outside the project
                    elif task.sql_path and Path(task.sql_path).is_file():
                        task.sql = Path(task.sql_path).read_text(encoding='utf-8')
                        used_sql.add(Path(task.sql_path).resolve())
                        self.watch(Path(task.sql_path))
                self.add_dag(dag, schema_key)
                dags_seen += 1
                tasks += len(dag.tasks)
            self.sources.append(
                {
                    'input': 'airflow',
                    'path': self.rel_path(path),
                    'detail': f'{len(parsed)} DAG{"s" if len(parsed) != 1 else ""}, {tasks} task{"s" if tasks != 1 else ""}',
                }
            )
        self.inputs['airflow_dags'] = dags_seen
        return used_sql

    def add_dag(self, dag: airflow.DagInfo, schema_key: str) -> None:
        path = self.rel_path(dag.path)
        dag_node = self.graph.add_node(
            Node(
                id=f'dag.{dag.dag_id}',
                kind='dag',
                name=dag.dag_id,
                description=dag.description,
                path=path,
                origins=['airflow'],
                attrs={
                    k: v
                    for k, v in {'schedule': dag.schedule, 'tags': dag.tags, 'line': dag.line}.items()
                    if v
                },
            )
        )
        for warning in dag.warnings:
            self.graph.warn(f'airflow: {path}: {warning}')
        for task in dag.tasks.values():
            task_node = self.graph.add_node(
                Node(
                    id=f'task.{dag.dag_id}.{task.task_id}',
                    kind='task',
                    name=task.task_id,
                    description=task.doc,
                    path=path,
                    origins=['airflow'],
                    attrs={
                        k: v
                        for k, v in {
                            'dag': dag.dag_id,
                            'operator': task.operator,
                            'line': task.line,
                            'bash_command': task.bash_command,
                            'sql_path': self.rel_path(task.sql_path) if task.sql_path else None,
                        }.items()
                        if v
                    },
                )
            )
            self.graph.add_edge(dag_node.id, task_node.id, 'runs')
            self.task_dbt(task, task_node, path)
            if task.sql:
                label = f'airflow task {dag.dag_id}.{task.task_id}'
                sql = strip_templating(task.sql)
                for statement in self.analyse_cached(sql, kind='task', schema_key=schema_key):
                    self.task_statement(statement, task_node, label)
            for name in task.writes:
                self.graph.add_edge(
                    task_node.id, self.relation(name, 'airflow').id, 'feeds', origin='airflow'
                )
            for name in task.reads:
                self.graph.add_edge(
                    self.relation(name, 'airflow').id, task_node.id, 'feeds', origin='airflow'
                )
            for uri in task.outlets:
                self.graph.add_edge(task_node.id, self.asset(uri).id, 'feeds', origin='airflow', asset=uri)
            for uri in task.inlets:
                self.graph.add_edge(self.asset(uri).id, task_node.id, 'feeds', origin='airflow', asset=uri)
        for upstream, downstream in dag.dependencies:
            self.graph.add_edge(
                f'task.{dag.dag_id}.{upstream}', f'task.{dag.dag_id}.{downstream}', 'precedes'
            )

    def load_dagster(self, schema_key: str) -> None:
        files: list[dagster.DagsterFile] = []
        for path in discover_dagster_files(self.config):
            self.watch(path)
            try:
                content = path.read_bytes()
            except OSError as error:
                self.graph.warn(f'dagster: cannot read {self.rel_path(path)}: {error}')
                continue
            key = Cache.key('dagster', hashlib.sha256(content).hexdigest(), str(path))

            def compute(path: Path = path) -> Any:
                try:
                    return dagster.parse_file(path).to_dict()
                except SyntaxError as error:
                    return {'error': f'syntax error on line {error.lineno}: {error.msg}'}

            parsed = self.cache.get_or_compute(key, compute)
            if 'error' in parsed:
                self.graph.warn(f'dagster: {self.rel_path(path)}: {parsed["error"]}')
                self.sources.append({'input': 'dagster', 'path': self.rel_path(path), 'status': 'failed'})
                continue
            files.append(dagster.DagsterFile.from_dict(parsed))
        assets = {a.key: (f, a) for f in files for a in f.assets}
        data_nodes: dict[str, Node] = {}
        tasks: dict[str, Node] = {}
        job_tasks: dict[str, list[Node]] = {}
        sql_statements: dict[str, list[StatementLineage]] = {}
        for file in files:
            rel = self.rel_path(file.path)
            for warning in file.warnings:
                self.graph.warn(f'dagster: {rel}: {warning}')
            for asset in file.assets:
                task = self.graph.add_node(
                    Node(
                        id=f'task.dagster.{asset.key}',
                        kind='task',
                        name=asset.key,
                        description=asset.description,
                        path=rel,
                        origins=['dagster'],
                        attrs={
                            k: v
                            for k, v in {
                                'dag': asset.group or 'dagster',
                                'orchestrator': 'dagster',
                                'operator': '@dbt_assets' if asset.dbt_select is not None else '@asset',
                                'function': asset.function,
                                'line': asset.line,
                                'kinds': asset.kinds,
                            }.items()
                            if v
                        },
                    )
                )
                tasks[asset.key] = task
                if asset.dbt_select is not None:
                    for node in dbt.select_nodes(self.graph, asset.dbt_select, asset.dbt_exclude):
                        self.graph.add_edge(task.id, node.id, 'runs', command='build')
                    continue
                statements = (
                    self.analyse_cached(strip_templating(asset.sql), kind='task', schema_key=schema_key)
                    if asset.sql
                    else []
                )
                sql_statements[asset.key] = statements
                # The asset's data is the table its SQL writes, when it writes one.
                targets = [s.target for s in statements if s.target]
                data = (
                    self.relation(targets[-1], 'dagster') if targets else self.dagster_data(asset.key, assets)
                )
                data_nodes[asset.key] = data
                self.graph.add_edge(task.id, data.id, 'feeds', origin='dagster')
            self.sources.append(
                {
                    'input': 'dagster',
                    'path': rel,
                    'detail': _dagster_detail(file),
                }
            )
        for file in files:
            for asset in file.assets:
                task = tasks[asset.key]
                for dep in asset.deps:
                    upstream = data_nodes.get(dep) or self.dagster_data(dep, assets)
                    self.graph.add_edge(upstream.id, task.id, 'feeds', origin='dagster')
                    if dep in tasks:
                        self.graph.add_edge(tasks[dep].id, task.id, 'precedes')
                for statement in sql_statements.get(asset.key, []):
                    self.task_statement(statement, task, f'dagster asset {asset.key}')
            ops = {op.name: op for op in file.ops}
            for job in file.jobs:
                dag = self.graph.add_node(
                    Node(
                        id=f'dag.{job.name}',
                        kind='dag',
                        name=job.name,
                        description=job.description,
                        path=self.rel_path(file.path),
                        origins=['dagster'],
                        attrs={
                            k: v
                            for k, v in {
                                'schedule': job.schedule,
                                'orchestrator': 'dagster',
                                'line': job.line,
                            }.items()
                            if v
                        },
                    )
                )
                job_tasks[job.name] = []
                if job.ops:
                    job_tasks[job.name] = self.dagster_ops(
                        job, dag, ops, self.rel_path(file.path), schema_key
                    )
                    continue
                for key in _selected_assets(job.selection, assets):
                    if key in tasks:
                        self.graph.add_edge(dag.id, tasks[key].id, 'runs')
                        job_tasks[job.name].append(tasks[key])
        for file in files:
            for sensor in file.sensors:
                self.dagster_sensor(sensor, tasks, job_tasks, self.rel_path(file.path))
        self.inputs['dagster_assets'] = sum(len(f.assets) for f in files)
        self.inputs['dagster_ops'] = sum(len(j.ops) for f in files for j in f.jobs)

    def usage_columns(self, sql: str) -> list[tuple[str, str]]:
        """The (relation, column) pairs one query reads: its output columns' sources and the
        columns it joins, filters and groups on."""
        found: set[tuple[str, str]] = set()
        for statement in analyse(
            sql,
            dialect=self.dialect,
            schema=self.schema_columns(),
            default_catalog=self.default_catalog,
            default_db=self.default_schema,
        ):
            for sources in statement.columns.values():
                found.update((s.relation, s.column) for s in sources)
            found.update(statement.references)
        return sorted(found)

    def load_usage(self) -> None:
        """Query history from the warehouses that have it, as usage counts on columns."""
        from konstruct.inputs import usage

        settings = self.config.usage
        if not settings.enabled:
            return
        if not self.config.trusted:
            # [usage] comes from the repository's own konstruct.toml, so it waits for trust too.
            self.untrusted.append('query history ([usage])')
            return
        totals: dict[str, dict[str, Any]] = {}
        unmatched = 0
        sources: list[str] = []
        for warehouse, connector in getattr(self, 'usage_connectors', []):
            if warehouse.type not in usage.SUPPORTED:
                continue
            name = warehouse.describe(self.config.root)
            # Query history moves slowly and costs a warehouse query, so it is read once a day.
            key = Cache.key(
                'usage',
                warehouse.type,
                name,
                settings.days,
                settings.objects,
                sorted(settings.exclude_users),
                time.strftime('%Y-%m-%d'),
            )

            def compute(connector: Any = connector) -> list[dict[str, Any]]:
                return [u.__dict__ for u in usage.fetch(connector, settings, self.usage_columns)]

            try:
                rows = self.cache.get_or_compute(key, compute, count=False)
            except usage.MissingDriver as error:
                self.graph.warn(f'usage: {name}: {error}')
                continue
            except Exception as error:  # permissions, edition or network: a warning, not a failure
                message = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
                self.graph.warn(f'usage: {name}: cannot read query history: {message}')
                continue
            sources.append(usage.SOURCES[warehouse.type])
            columns_by_relation: dict[str, dict[str, Node]] = {}
            for row in rows:
                relation = self.graph.node_for_relation(
                    str(row['relation']).replace('"', '').replace('`', '').lower()
                )
                if relation is None:
                    unmatched += 1
                    continue
                index = columns_by_relation.setdefault(
                    relation.id, {c.name.lower(): c for c in self.graph.columns_of(relation.id)}
                )
                column = index.get(str(row['column']).lower())
                if column is None:
                    unmatched += 1
                    continue
                entry = totals.setdefault(column.id, {'queries': 0, 'users': 0})
                entry['queries'] += int(row['queries'])
                entry['users'] += int(row['users'])
        if not sources:
            return
        import math

        most = max((e['queries'] for e in totals.values()), default=0)
        for column_id, entry in totals.items():
            # A 0 to 100 score on a log scale, so the long tail is still told apart.
            entry['score'] = round(100 * math.log1p(entry['queries']) / math.log1p(most)) if most else 0
            entry['days'] = settings.days
            self.graph.nodes[column_id].attrs['usage'] = entry
        self.graph.meta['usage'] = {
            'days': settings.days,
            'sources': sorted(set(sources)),
            'columns': len(totals),
            'unmatched': unmatched,
        }
        self.sources.append(
            {
                'input': 'usage',
                'path': ', '.join(sorted(set(sources))),
                'detail': f'{len(totals)} columns queried in the last {settings.days} days'
                + (f'; {unmatched} not in the graph' if unmatched else ''),
            }
        )

    def load_ossie(self) -> None:
        """Apache Ossie semantic models: each metric becomes a measure bound to the columns it reads,
        a primary key marks an entity key, and a relationship links two entities."""
        from konstruct.semantics import ossie

        relationships: list[dict[str, str]] = []
        authored_relationships: list[dict[str, Any]] = []
        for path in discover_ossie_files(self.config):
            self.watch(path)
            rel = self.rel_path(path)
            try:
                parsed = ossie.parse_file(path, rel)
            except OSError as error:
                self.graph.warn(f'ossie: cannot read {rel}: {error}')
                continue
            for problem in parsed.errors:
                self.graph.warn(f'ossie: {problem}')
            count = 0
            for model in parsed.models:
                binding = ossie.bind(self.graph, model)
                for problem in binding.problems:
                    self.graph.warn(f'ossie: {problem}')
                for key in binding.keys:
                    key.attrs.setdefault('ossie_key', rel)
                for dataset_name, relation in binding.relations.items():
                    # The first model to name a relation is the one the export writes back.
                    relation.attrs.setdefault(
                        'ossie_authored',
                        {
                            'model': model.name,
                            'model_description': model.description,
                            'path': rel,
                            'dataset': model.datasets[dataset_name].raw,
                            'metrics': [m.name for m in model.metrics if m.dataset == dataset_name],
                        },
                    )
                for raw_relationship in model.relationships:
                    authored_relationships.append({'model': model.name, 'path': rel, 'raw': raw_relationship})
                for many, one, name in binding.relationships:
                    relationships.append({'from': many.id, 'to': one.id, 'name': name, 'path': rel})
                seen_metrics: set[str] = set()
                for metric in model.metrics:
                    if metric.name in seen_metrics:
                        continue  # defined twice in the model: bind reported it and kept the first
                    seen_metrics.add(metric.name)
                    columns = binding.metrics.get(metric.name)
                    if columns is None or metric.name in binding.failed:
                        continue
                    node_id = f'term.{definitions.slug(metric.name)}'
                    if node_id in self.graph.nodes:
                        self.graph.warn(
                            f'ossie: {rel}: metric {metric.name!r} has the same name as a term already '
                            'defined; the existing term is kept'
                        )
                        continue
                    attrs: dict[str, Any] = {'kind': 'measure', 'formula': metric.sql, 'model': model.name}
                    if metric.dialect:
                        attrs['formula_dialect'] = metric.dialect
                    if metric.name in binding.portable:
                        attrs['ossie_expression'], attrs['ossie_columns'] = binding.portable[metric.name]
                    if metric.name in binding.datasets:
                        attrs['ossie_dataset'] = binding.datasets[metric.name]
                    attrs['ossie_metric'] = metric.raw
                    attrs['ossie_reads'] = binding.reads.get(metric.name, {})
                    if metric.synonyms:
                        attrs['synonyms'] = metric.synonyms
                    term = self.graph.add_node(
                        Node(
                            node_id,
                            'term',
                            metric.name,
                            description=metric.description,
                            path=rel,
                            origins=['ossie'],
                            attrs=attrs,
                        )
                    )
                    # The columns a metric reads are its inputs; none of them holds the measure.
                    for column in columns:
                        self.graph.add_edge(term.id, column.id, 'defines', role='input')
                    count += 1
            self.sources.append(
                {'input': 'ossie', 'path': rel, 'detail': f'{count} metric{"s" if count != 1 else ""}'}
            )
            self.inputs['ossie_metrics'] = self.inputs.get('ossie_metrics', 0) + count
        if relationships:
            self.graph.meta['ossie_relationships'] = relationships
        if authored_relationships:
            self.graph.meta['ossie_authored_relationships'] = authored_relationships

    def load_inputs(self, schema_key: str) -> None:
        """Every input in the plugin registry, Konstruct's own (Looker) first, then the inputs of
        the plugins the project enables, each read into a fragment and merged."""
        for plugin in self.plugins.group('inputs'):
            self.read_input(plugin, schema_key)
        for problem in self.plugins.problems:
            self.graph.warn(f'plugins: {problem}')

    def read_input(self, plugin: Plugin, schema_key: str) -> None:
        """One input: its files found by its patterns (or ``[<name>] paths``), read by the input
        and merged into the graph. A plugin that raises or returns an invalid fragment is
        reported and merges nothing; one of Konstruct's own that does is a bug and raises."""
        from konstruct.plugins.api import Fragment, InputContext, InputFile

        name, reader = plugin.name, plugin.loaded
        patterns = list(reader.patterns)
        paths = discover_input_files(self.config, name, patterns) if patterns else []
        if patterns and not paths:
            return
        files: list[InputFile] = []
        for path in paths:
            rel = self.rel_path(path)
            self.watch(path)
            try:
                files.append(InputFile(rel, path.read_bytes()))
            except OSError as error:
                self.graph.warn(f'{name}: cannot read {rel}: {error}')

        def cached(key: tuple[Any, ...], compute: Callable[[], Any]) -> Any:
            def checked() -> Any:
                value = compute()
                # The cache is written at the end of the build: a value it cannot hold must fail
                # this plugin now, inside its own error handling, and never be stored.
                problem = json_problem(value)
                if problem is not None:
                    raise PluginError(f'context.cached({briefly(key)}): the value {problem}')
                # As an update reads it back, so a cold build sees the same value (lists, not
                # tuples).
                return jsonio.loads(jsonio.dumps(value))

            # The key is hashed as JSON: a part JSON cannot hold would hash by its repr, which
            # can differ from one run to the next and miss the cache without a word.
            problem = json_problem(list(key)) if isinstance(key, tuple) else 'is not a tuple'
            if problem is not None:
                raise PluginError(f'context.cached({briefly(key)}): the key {problem}')
            stored = self.cache.get_or_compute(Cache.key(name, plugin.version, *key), checked)
            # A copy on every call: a plugin that changes what it is given must not change what
            # the cache keeps, or each --update would build on the last one's changes.
            return jsonio.loads(jsonio.dumps(stored))

        context = InputContext(
            root=self.config.root,
            dialect=self.dialect,
            options=self.config.sections.get(name, {}),
            cached=cached,
        )
        if plugin.builtin:
            fragment = reader.read(files, context)
            problems = fragment.problems()
        else:
            try:
                fragment = reader.read(files, context)
                problems = (
                    fragment.problems() if isinstance(fragment, Fragment) else ['read returned no Fragment']
                )
            except Exception as error:
                self.plugin_failed(plugin, f'raised {describe(error)}')
                return
        if problems:
            more = f' and {len(problems) - 3} more' if len(problems) > 3 else ''
            message = f'returned a fragment that is not valid: {"; ".join(problems[:3])}{more}'
            if plugin.builtin:
                raise AssertionError(f'{plugin.label} {message}')
            self.plugin_failed(plugin, message)
            return
        self.merge_fragment(plugin, fragment, schema_key)

    def plugin_failed(self, plugin: Plugin, what: str) -> None:
        self.graph.warn(f'plugins: {plugin.label} {what}; the build goes on without it')
        self.sources.append(
            {'input': plugin.name, 'name': plugin.distribution, 'status': 'failed', 'detail': what}
        )

    def merge_fragment(self, plugin: Plugin, fragment: Fragment, schema_key: str) -> None:
        """Add a valid fragment to the graph: its nodes under ``<name>.``, its relations and
        columns resolved to the graph's, its SQL analysed for lineage, all with the input's name
        as their origin."""
        from konstruct.plugins.api import Relation

        name = plugin.name
        for warning in fragment.warnings:
            self.graph.warn(f'{name}: {one_line(warning)}')
        nodes = {}
        for item in fragment.nodes:
            nodes[item.id] = self.graph.add_node(
                Node(
                    id=f'{name}.{item.id}',
                    kind=item.kind,  # type: ignore[arg-type]
                    # One line, so a name cannot start a heading in REPORT.md or break a label.
                    name=one_line(item.name),
                    description=item.description,
                    path=item.path,
                    origins=[name],
                    # As graph.json reads back (lists, not tuples), so the graph a build returns
                    # is the one it writes.
                    attrs=jsonio.loads(jsonio.dumps(item.attrs)),
                )
            )
        # Whether a relation's columns were known before a node's edges added any, per node.
        known: dict[tuple[str, str], bool] = {}

        def end(point: Endpoint, other: Endpoint, edge: PluginEdge) -> str | None:
            if isinstance(point, str):
                return nodes[point].id
            if isinstance(point, Relation):
                return self.relation(point.key, name).id
            relation = self.relation(point.relation, name)
            column = self.graph.column(relation.id, point.name)
            if column is None:
                if known.setdefault((relation.id, str(other)), self.columns_known(relation)):
                    reader = nodes.get(other) if isinstance(other, str) else None
                    where = f'{reader.path}: ' if reader is not None and reader.path else ''
                    who = repr(reader.name) if reader is not None else 'an edge'
                    default = f'{where}{who} reads {point.relation}.{point.name}, which is not a known column'
                    self.graph.warn(f'{name}: {one_line(edge.missing or default)}')
                    return None
                column = self.column(relation, point.name, name)
            return column.id

        for edge in fragment.edges:
            source = end(edge.source, edge.target, edge)
            target = end(edge.target, edge.source, edge) if source is not None else None
            if source is not None and target is not None:
                attrs = jsonio.loads(jsonio.dumps(edge.attrs))
                self.graph.add_edge(source, target, edge.kind, origin=name, **attrs)  # type: ignore[arg-type]
        for sql in fragment.sql:
            task = nodes[sql.task] if sql.task else None
            label = f'{name} {sql.path}'
            for statement in self.analyse_cached(
                sql.sql, kind=f'input:{name}', schema_key=schema_key, dialect=sql.dialect
            ):
                if task is not None:
                    for read in statement.reads:
                        self.graph.add_edge(self.relation(read, name).id, task.id, 'feeds', origin=name)
                if not statement.target:
                    for problem in statement.errors:
                        self.graph.warn(f'{label}: {problem}')
                    continue
                target_node = self.relation(statement.target, name)
                target_node.path = target_node.path or sql.path
                if task is not None:
                    self.graph.add_edge(task.id, target_node.id, 'feeds', origin=name)
                self.apply(
                    statement,
                    target=target_node,
                    origin=name,
                    label=label,
                    via=task.id if task is not None else sql.path,
                )
        for source_line in fragment.sources:
            self.sources.append(
                {'input': name, 'path': one_line(source_line.path), 'detail': one_line(source_line.detail)}
            )
        for key, count in fragment.counts.items():
            # Each count is named <input>_<key>, so a_b's c and a's b_c would be one count.
            joined = f'{name}_{key}'
            taken = (
                "one of Konstruct's own counts"
                if joined in BUILTIN_COUNTS
                else 'already a count'
                if joined in self.inputs
                else None
            )
            if taken is not None:
                self.graph.warn(f'plugins: {plugin.label} count {key!r} is left out: {joined} is {taken}')
                continue
            self.inputs[joined] = count

    def load_powerbi(self) -> None:
        """Power BI semantic models (TMDL) as exposures: each table reads the warehouse tables its
        partitions read, and each of its columns and measures the warehouse columns it reads,
        through the measures and calculated columns of the whole model."""
        from konstruct.inputs import powerbi

        files = discover_powerbi_files(self.config)
        parsed: list[powerbi.PowerBIFile] = []
        for path in files:
            rel = self.rel_path(path)
            self.watch(path)
            try:
                content = path.read_bytes()
            except OSError as error:
                self.graph.warn(f'powerbi: cannot read {rel}: {error}')
                continue
            key = Cache.key('powerbi', hashlib.sha256(content).hexdigest(), rel)

            def compute(content: bytes = content, rel: str = rel) -> Any:
                return powerbi.parse(content.decode('utf-8-sig', errors='replace'), rel).to_dict()

            parsed.append(powerbi.PowerBIFile.from_dict(self.cache.get_or_compute(key, compute)))

        def read_sql(sql: str, dialect: str | None) -> list[str] | None:
            key = Cache.key('powerbi-sql', sql, dialect or '')
            found: list[str] | None = self.cache.get_or_compute(
                key, lambda: powerbi.native_reads(sql, dialect)
            )
            return found

        tables, problems = powerbi.resolve(parsed, self.dialect, read_sql)
        for problem in problems:
            self.graph.warn(f'powerbi: {problem}')
        by_path: dict[str, list[str]] = {}
        for table in tables:
            by_path.setdefault(table.path, []).append(table.name)
            exposure = self.graph.add_node(
                Node(
                    id=f'powerbi.table.{table.model}.{table.name}',
                    kind='exposure',
                    name=table.name,
                    description=table.description,
                    path=table.path,
                    origins=['powerbi'],
                    attrs={
                        'type': 'power bi table',
                        'model': table.model,
                        'fields': sorted([c.name for c in table.columns] + list(table.measures)),
                        **({'measures': dict(sorted(table.measures.items()))} if table.measures else {}),
                    },
                )
            )
            relations = {
                key: self.relation(key.lower(), 'powerbi') for key in sorted({t for t, _ in table.reads})
            }
            for relation in relations.values():
                self.graph.add_edge(relation.id, exposure.id, 'feeds', origin='powerbi')
            for (key, name), field_names in sorted(table.reads.items()):
                if not name:
                    continue
                relation = relations[key]
                column = self.graph.column(relation.id, name)
                if column is None and self.columns_known(relation):
                    self.graph.warn(
                        f'powerbi: {table.path}: table {table.name!r} reads {key}.{name}, '
                        'which is not a known column'
                    )
                    continue
                column = column or self.column(relation, name, 'powerbi')
                self.graph.add_edge(
                    column.id, exposure.id, 'feeds', origin='powerbi', fields=sorted(field_names)
                )
        for path in files:
            rel = self.rel_path(path)
            if rel in by_path:
                self.sources.append({'input': 'powerbi', 'path': rel, 'detail': ', '.join(by_path[rel])})
        if files:
            self.inputs['powerbi_tables'] = len(tables)

    def bind_ontology_packs(self) -> None:
        """The terms of the ontology packs whose source schemas the graph has, bound to them, after
        every repository is merged. A pack term is left out when the project already has a term of
        that name or synonym, or a term bound to one of its columns: the project's own model of the
        source wins."""
        from konstruct.inputs import loaders
        from konstruct.semantics import registry

        if self.config.ontology_packs == []:
            return
        terms = [n for n in self.graph.nodes.values() if n.kind == 'term']
        names = {n.name.lower() for n in terms} | {
            str(s).lower() for n in terms for s in n.attrs.get('synonyms', [])
        }
        slugs = {definitions.slug(n.name) for n in terms}
        bound = {e.target for n in terms for e in self.graph.out_edges(n.id, 'defines')}
        schemas: dict[str, dict[str, set[str]]] = {}
        loaded: dict[str, set[str]] = {}
        for node in self.graph.relations():
            if node.relation and node.relation.count('.') >= 1 and not node.attrs.get('outside_warehouse'):
                schema, _, table = node.relation.rpartition('.')
                columns = schemas.setdefault(schema, {}).setdefault(table, set())
                columns.update(c.name.lower() for c in self.graph.columns_of(node.id))
                if loaders.lands_source_data(node.attrs):
                    loaded.setdefault(schema, set()).add(table)
        problems: list[str] = []
        packs = registry.load_packs(self.config.ontology_packs, plugins=self.plugins, problems=problems)
        for problem in [*problems, *self.plugins.problems]:
            self.graph.warn(f'plugins: {problem}')
        for name in sorted(set(self.config.ontology_packs or []) - {p.name for p in packs}):
            reason = self.plugins.off or 'no loaded pack has that name'
            self.graph.warn(f'ontologies: pack {name!r} is not bound: {reason}')
        matches = registry.find(packs, schemas, loaded)
        suffixes = registry.suffixes(matches)
        found: list[definitions.Term] = []
        joins: list[dict[str, str]] = []
        for match in matches:
            for many_ref, one_ref in registry.relationships(match, schemas):
                many, one = _bind(self.graph, many_ref)[0], _bind(self.graph, one_ref)[0]
                if many is not None and one is not None:
                    joins.append({'from': many.id, 'to': one.id, 'name': match.pack.name})
            parsed = registry.terms(match, schemas, suffixes.get(match.schema))
            for problem in parsed.errors:
                self.graph.warn(f'ontologies: {problem}')
            kept = []
            for term in parsed.terms:
                targets = [_bind(self.graph, ref)[0] for ref in term.columns]
                if (
                    {n.lower() for n in term.names} & names
                    or term.slug in slugs
                    or any(t is not None and t.id in bound for t in targets)
                ):
                    continue
                kept.append(term)
            found.extend(kept)
            self.sources.append(
                {
                    'input': 'ontology pack',
                    'name': match.pack.name,
                    'detail': f'{match.schema}: {len(kept)} term{"s" if len(kept) != 1 else ""}',
                }
            )
        if found:
            self.add_terms(found)
            self.inputs['ontology_pack_terms'] = len(found)
        if joins:
            self.graph.meta['pack_relationships'] = joins

    def vocabulary_terms(self, terms: list[definitions.Term]) -> list[definitions.Term]:
        """The terms of the configured SKOS and OWL vocabularies that the project does not define,
        after filling in the project's own terms from them."""
        from konstruct.semantics import vocabulary

        self.vocabulary_files = set()
        paths = []
        for path in self.config.inside(self.config.vocabulary_paths):
            rel = self.rel_path(path)
            self.watch(path)
            if not path.is_file():
                self.graph.warn(f'vocabularies: {rel} does not exist')
                continue
            paths.append((path, rel))
        if not paths:
            return []
        read = vocabulary.read(paths)
        problems: list[str] = []
        added = vocabulary.merge(terms, read.terms, problems)
        for problem in read.problems + problems:
            self.graph.warn(f'vocabularies: {problem}')
        for _, rel in paths:
            self.vocabulary_files.add(rel)
            count = read.counts.get(rel, 0)
            new = sum(1 for t in added if t.path == rel)
            if count:
                self.sources.append(
                    {
                        'input': 'vocabulary',
                        'path': rel,
                        'detail': f'{count} concepts, {new} new term{"s" if new != 1 else ""}',
                    }
                )
        return added

    def load_definitions(self) -> None:
        """Business terms from definitions.yml, attached to the columns they name."""
        terms: list[definitions.Term] = []
        for path in discover_definitions_files(self.config):
            self.watch(path)
            rel = self.rel_path(path)
            try:
                parsed = definitions.parse_file(path, rel)
            except OSError as error:
                self.graph.warn(f'definitions: cannot read {rel}: {error}')
                continue
            for problem in parsed.errors:
                self.graph.warn(f'definitions: {problem}')
            terms.extend(parsed.terms)
            count = len(parsed.terms)
            self.sources.append(
                {'input': 'definitions', 'path': rel, 'detail': f'{count} term{"s" if count != 1 else ""}'}
            )
        terms.extend(self.vocabulary_terms(terms))
        self.add_terms(terms)
        self.inputs['definitions'] = len(terms)

    def add_terms(self, terms: list[definitions.Term]) -> None:
        """Term nodes for ``terms``, with their columns, filters and related terms."""
        for problem in definitions.validate(terms):
            self.graph.warn(f'definitions: {problem}')
        nodes: dict[str, Node] = {}
        for term in terms:
            where = f'definitions: {term.where}: term {term.name!r}'
            node_id = f'term.{term.slug}'
            if node_id in self.graph.nodes:
                self.graph.warn(f'{where}: has the same id as another term ({node_id}); skipped')
                continue
            node = self.graph.add_node(
                Node(
                    id=node_id,
                    kind='term',
                    name=term.name,
                    description=term.definition or None,
                    path=term.path,
                    origins=[
                        'registry'
                        if term.path.startswith('ontology pack ')
                        else 'vocabulary'
                        if term.path in self.vocabulary_files
                        else 'definitions'
                    ],
                    attrs={
                        k: v
                        for k, v in {
                            'registry': term.path.removeprefix('ontology pack ')
                            if term.path.startswith('ontology pack ')
                            else None,
                            'kind': term.kind,
                            'synonyms': term.synonyms,
                            'status': term.status,
                            'owner': {'name': term.owner.name, 'role': term.owner.role}
                            if term.owner
                            else None,
                            'approved': term.approved,
                            'grain': term.grain,
                            'columns': term.columns,
                            'filters': term.filters,
                            'unit': term.unit,
                            'currency': term.currency,
                            'related': term.related,
                            'broader': term.broader,
                            'exceptions': term.exceptions,
                            'line': term.line or None,
                            'iri': term.iri,
                        }.items()
                        if v not in (None, [], '')
                    },
                )
            )
            for name in term.names:
                nodes[name.lower()] = node
            bound: list[Node] = []
            for ref in term.columns:
                target, reason = _bind(self.graph, ref)
                if target is None:
                    self.graph.warn(f'{where}: bound column {reason}')
                    continue
                bound.append(target)
                self.graph.add_edge(node.id, target.id, 'defines')
            for condition in term.filters:
                columns = _filter_columns(condition, self.dialect)
                if columns is None:
                    self.graph.warn(f'{where}: required filter {condition!r} is not valid SQL')
                    continue
                for table, column in columns:
                    candidates = (
                        [f'{table}.{column}']
                        if table
                        else [
                            f'{self.graph.label(b if b.kind != "column" else self.graph.parent_of(b) or b)}.{column}'
                            for b in bound
                        ]
                    )
                    if not any(_bind(self.graph, c)[0] is not None for c in candidates):
                        shown = f'{table}.{column}' if table else column
                        self.graph.warn(
                            f'{where}: required filter {condition!r} reads {shown!r}, which does not exist'
                        )
        for term in terms:
            source = nodes.get(term.name.lower())
            for related in term.related:
                target = nodes.get(related.lower())
                if source is not None and target is not None and target is not source:
                    self.graph.add_edge(source.id, target.id, 'relates_to')
            for wider in term.broader:
                target = nodes.get(wider.lower())
                if source is not None and target is not None and target is not source:
                    self.graph.add_edge(source.id, target.id, 'relates_to', role='broader')

    def dagster_ops(
        self,
        job: dagster.JobInfo,
        dag: Node,
        ops: dict[str, dagster.OpInfo],
        path: str,
        schema_key: str,
    ) -> list[Node]:
        """Tasks for an op-based job: one per op invocation, ordered by the data passed between them."""
        nodes: dict[str, Node] = {}
        for call in job.ops:
            op = ops.get(call.op)
            node = self.graph.add_node(
                Node(
                    id=f'task.{job.name}.{call.handle}',
                    kind='task',
                    name=call.handle,
                    description=op.description if op else None,
                    path=path,
                    origins=['dagster'],
                    attrs={
                        k: v
                        for k, v in {
                            'dag': job.name,
                            'orchestrator': 'dagster',
                            'operator': '@op',
                            'op': call.op if call.op != call.handle.rsplit('.', 1)[-1] else None,
                            'function': op.function if op else None,
                            'line': op.line if op else None,
                            'kinds': op.kinds if op else None,
                        }.items()
                        if v
                    },
                )
            )
            nodes[call.handle] = node
            self.graph.add_edge(dag.id, node.id, 'runs')
            if op is None:
                continue
            label = f'dagster op {job.name}.{call.handle}'
            commands = [i for c in op.dbt_commands for i in airflow.parse_dbt_commands(c)]
            self.run_dbt_invocations(commands, node, f'dagster: {path}: op {call.handle}')
            if op.sql:
                for statement in self.analyse_cached(
                    strip_templating(op.sql), kind='task', schema_key=schema_key
                ):
                    self.task_statement(statement, node, label)
        for call in job.ops:
            for upstream in call.upstream:
                if upstream in nodes:
                    self.graph.add_edge(nodes[upstream].id, nodes[call.handle].id, 'precedes')
        return list(nodes.values())

    def dagster_sensor(
        self,
        sensor: dagster.SensorInfo,
        tasks: dict[str, Node],
        job_tasks: dict[str, list[Node]],
        path: str,
    ) -> None:
        """Record a sensor on the jobs it starts, and order the work it connects: an asset sensor
        runs its job after the asset, a run status sensor on success runs its job after the
        monitored job. Failure sensors are recorded but add no ordering."""
        watched = [f'asset {a}' for a in sensor.assets] + [
            f'job {j}' + (f' ({sensor.status.lower()})' if sensor.status else '')
            for j in sensor.monitored_jobs
        ]
        before: list[Node] = [tasks[a] for a in sensor.assets if a in tasks]
        if sensor.status in (None, 'SUCCESS'):
            for monitored in sensor.monitored_jobs:
                members = job_tasks.get(monitored, [])
                ids = {m.id for m in members}
                before += [
                    m
                    for m in members
                    if not any(e.target in ids for e in self.graph.out_edges(m.id, 'precedes'))
                ]
        for job_name in sensor.jobs:
            dag = self.graph.nodes.get(f'dag.{job_name}')
            if dag is None:
                self.graph.warn(f'dagster: {path}: sensor {sensor.name} starts unknown job {job_name}')
                continue
            dag.attrs.setdefault('sensors', []).append(
                sensor.name + (f' on {", ".join(watched)}' if watched else '')
            )
            members = job_tasks.get(job_name, [])
            ids = {m.id for m in members}
            roots = [
                m for m in members if not any(e.source in ids for e in self.graph.in_edges(m.id, 'precedes'))
            ]
            for upstream in before:
                for root in roots:
                    if upstream.id != root.id:
                        self.graph.add_edge(upstream.id, root.id, 'precedes', sensor=sensor.name)

    def dagster_data(
        self, key: str, assets: dict[str, tuple[dagster.DagsterFile, dagster.AssetInfo]]
    ) -> Node:
        """The data an asset key stands for: a dbt node (dagster-dbt keys models by name and sources
        by source and table), a warehouse table, or else a table node for the asset itself."""
        parts = key.split('/')
        if key not in assets or assets[key][1].dbt_select is not None:
            for node in self.graph.nodes.values():
                if 'dbt' not in node.origins or node.kind not in RELATION_KINDS:
                    continue
                if (
                    node.kind == 'source'
                    and node.attrs.get('source_name') == parts[0]
                    and node.name == parts[-1]
                    and len(parts) == 2
                ):
                    return node
                if node.kind != 'source' and node.attrs.get('dbt_name') == parts[-1]:
                    return node
        found = self.index.find('.'.join(parts))
        if found is not None:
            return found
        node = self.graph.add_node(
            Node(id=f'table.asset:{key}', kind='table', name=key, origins=['dagster'], attrs={'asset': key})
        )
        return node

    def task_dbt(self, task: airflow.TaskInfo, node: Node, path: str) -> None:
        self.run_dbt_invocations(task.dbt, node, f'airflow: {path}: task {task.task_id}')

    def run_dbt_invocations(self, invocations: list[airflow.DbtInvocation], node: Node, label: str) -> None:
        """``runs`` edges from a task to the dbt nodes its dbt commands select."""
        for invocation in invocations:
            if invocation.unsupported:
                self.graph.warn(
                    f'{label}: dbt selection not resolved statically: ' + ' '.join(invocation.unsupported)
                )
            selected = dbt.select_nodes(self.graph, invocation.selectors, invocation.excludes)
            if invocation.command == 'seed':
                selected = [n for n in selected if n.kind == 'seed']
            elif invocation.command == 'snapshot':
                selected = [n for n in selected if n.kind == 'snapshot']
            elif invocation.command == 'run':
                selected = [n for n in selected if n.kind == 'model']
            elif invocation.command == 'test':
                selected = []
                tested = {
                    n.id for n in dbt.select_nodes(self.graph, invocation.selectors, invocation.excludes)
                }
                for test in (n for n in self.graph.nodes.values() if n.kind == 'test'):
                    targets = {e.target.split('#')[0] for e in self.graph.out_edges(test.id, 'tests')}
                    if targets & tested:
                        self.graph.add_edge(node.id, test.id, 'runs', command='test')
            for target in selected:
                self.graph.add_edge(node.id, target.id, 'runs', command=invocation.command)
            if invocation.selectors and not selected and invocation.command != 'test':
                self.graph.warn(
                    f'{label}: `dbt {invocation.command} --select '
                    f'{" ".join(invocation.selectors)}` matched no dbt nodes'
                )

    def task_statement(self, statement: StatementLineage, task: Node, label: str) -> None:
        for error in statement.errors:
            self.graph.warn(f'{label}: {error}')
        statement.errors = []
        if statement.target:
            target = self.relation(statement.target, 'airflow')
            self.graph.add_edge(task.id, target.id, 'feeds', origin='airflow')
            self.apply(statement, target=target, origin='airflow', label=label, via=task.id)
        for read in statement.reads:
            self.graph.add_edge(self.relation(read, 'airflow').id, task.id, 'feeds', origin='airflow')

    def asset(self, uri: str) -> Node:
        """An Airflow asset: a known relation if the URI names one, otherwise a table node for it."""
        _, _, rest = uri.partition('://')
        if rest:
            host, _, path = rest.partition('/')
            parts = [p for p in path.split('/') if p]
            key = '.'.join(parts) if parts else host
            found = self.index.find(key, drop_leading=True) or self.index.find(f'{host.split(":")[0]}.{key}')
            if found is not None:
                return found
        node = self.graph.add_node(
            Node(id=f'table.asset:{uri}', kind='table', name=uri, origins=['airflow'], attrs={'asset': uri})
        )
        return node

    def load_spark(self, schema_key: str) -> None:
        """PySpark jobs: each SQL string a job passes to ``spark.sql``, analysed in the Spark
        dialect (``[spark] dialect``), gives the tables it writes column lineage."""
        from konstruct.inputs import spark

        files = discover_spark_files(self.config)
        statements_read = 0
        for path in files:
            rel = self.rel_path(path)
            self.watch(path)
            try:
                job = spark.parse(path.read_text(encoding='utf-8'), rel)
            except OSError as error:
                self.graph.warn(f'spark: cannot read {rel}: {error}')
                continue
            for problem in job.problems:
                self.graph.warn(f'spark: {problem}')
            writes: set[str] = set()
            for item in job.statements:
                sql = spark.without_session_catalog(item.sql, self.config.spark_dialect)
                for statement in self.analyse_cached(
                    sql, kind='spark', schema_key=schema_key, dialect=self.config.spark_dialect
                ):
                    if not statement.target:
                        for problem in statement.errors:
                            self.graph.warn(f'spark: {rel}:{item.line}: {problem}')
                        continue
                    writes.add(statement.target)
                    target = self.relation(statement.target, 'spark')
                    target.path = target.path or rel
                    self.apply(
                        statement, target=target, origin='spark', label=f'spark {rel}:{item.line}', via=rel
                    )
            statements_read += len(job.statements)
            self.sources.append(
                {
                    'input': 'spark',
                    'path': rel,
                    'detail': f'writes {", ".join(sorted(writes))}' if writes else 'writes nothing',
                }
            )
        if files:
            self.inputs['spark_jobs'] = len(files)
            self.inputs['spark_statements'] = statements_read

    def load_sql_files(self, projects: list[dbt.DbtProject], used: set[Path], schema_key: str) -> None:
        files = discover_sql_files(self.config, projects, used)
        for path in files:
            try:
                text = path.read_text(encoding='utf-8')
            except OSError as error:
                self.graph.warn(f'sql: cannot read {self.rel_path(path)}: {error}')
                continue
            rel = self.rel_path(path)
            self.watch(path)
            statements = self.analyse_cached(strip_templating(text), kind='file', schema_key=schema_key)
            writes = sorted({s.target for s in statements if s.target})
            self.sources.append(
                {
                    'input': 'sql',
                    'path': rel,
                    'detail': f'writes {", ".join(writes)}' if writes else 'writes nothing',
                }
            )
            for statement in statements:
                if not statement.target:
                    for problem in statement.errors:
                        self.graph.warn(f'sql {rel}: {problem}')
                    continue
                target = self.relation(statement.target, 'sql')
                target.path = target.path or rel
                self.apply(statement, target=target, origin='sql', label=f'sql {rel}', via=rel)
        self.inputs['sql_files'] = len(files)


def _bind(graph: Graph, ref: str) -> tuple[Node | None, str | None]:
    """The column (or relation) a definition names, or None with the reason."""
    found = [n for n in graph.resolve(ref) if n.kind == 'column' or n.kind in RELATION_KINDS]
    if len(found) == 1:
        return found[0], None
    if found:
        return None, f'{ref!r} is ambiguous ({", ".join(n.id for n in found[:5])}); qualify it'
    hint_list = _similar_columns(graph, ref)
    hint = f'; did you mean {", ".join(hint_list)}?' if hint_list else ''
    return None, f'{ref!r} does not exist{hint}'


def _similar_columns(graph: Graph, ref: str, limit: int = 3) -> list[str]:
    """Columns a missing reference probably meant: in the same relation first, those sharing a
    word with the missing name or spelt like it (a rename), then the closest names anywhere."""
    import difflib
    import re

    from konstruct.analysis.query import search_nodes

    found: list[str] = []
    relation_part, _, column_part = ref.rpartition('.')
    if relation_part:
        relations = [n for n in graph.resolve(relation_part) if n.kind in RELATION_KINDS]
        if len(relations) == 1:

            def split(name: str) -> set[str]:
                return set(re.split(r'[^a-z0-9]+', name.lower())) - {''}

            names = [c.name for c in graph.columns_of(relations[0].id)]
            words = split(column_part)
            # Rare words say more: "lifetime" in two column names beats "customer" in forty.
            frequency: dict[str, int] = {}
            for node in graph.nodes.values():
                if node.kind == 'column':
                    for word in split(node.name):
                        frequency[word] = frequency.get(word, 0) + 1
            scored = sorted(
                (
                    (
                        sum(1 / frequency.get(w, 1) for w in words & split(n)),
                        difflib.SequenceMatcher(None, column_part, n).ratio(),
                        n,
                    )
                    for n in names
                    if words & split(n)
                ),
                reverse=True,
            )
            sharing = [n for _, _, n in scored]
            close = difflib.get_close_matches(column_part, names, n=limit, cutoff=0.5)
            for name in [*sharing, *close]:
                label = f'{graph.label(relations[0])}.{name}'
                if label not in found:
                    found.append(label)
    for node in search_nodes(graph, ref.replace('.', ' '), limit=limit, kinds=['column']):
        if graph.label(node) not in found:
            found.append(graph.label(node))
    return found[:limit]


def _filter_columns(sql: str, dialect: str | None) -> list[tuple[str | None, str]] | None:
    """(table, column) pairs a required filter reads, or None if it is not valid SQL."""
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except PARSE_ERRORS:
        return None
    return [(c.table or None, c.name) for c in tree.find_all(exp.Column)]


def _dagster_detail(file: dagster.DagsterFile) -> str:
    def count(n: int, noun: str) -> str:
        return f'{n} {noun}{"s" if n != 1 else ""}'

    parts = [count(len(file.assets), 'asset')]
    if file.ops:
        parts.append(count(len(file.ops), 'op'))
    parts.append(count(len(file.jobs), 'job'))
    if file.sensors:
        parts.append(count(len(file.sensors), 'sensor'))
    return ', '.join(parts)


def _selected_assets(
    selection: list[str], assets: dict[str, tuple[dagster.DagsterFile, dagster.AssetInfo]]
) -> list[str]:
    chosen: list[str] = []
    for item in selection:
        if item == '*':
            chosen.extend(assets)
        elif item.startswith('group:'):
            chosen.extend(k for k, (_, a) in assets.items() if a.group == item[6:])
        elif item.startswith('prefix:'):
            chosen.extend(k for k in assets if k.startswith(item[7:] + '/'))
        elif item in assets:
            chosen.append(item)
        else:
            # A dbt model selected by key runs as part of the @dbt_assets function.
            chosen.extend(k for k, (_, a) in assets.items() if a.dbt_select is not None)
    return list(dict.fromkeys(chosen))


def changed_inputs(graph_path: Path, graph: Graph) -> list[str]:
    """Files the last build read that have changed since it ran, relative to the project root."""
    try:
        built = graph_path.stat().st_mtime
    except OSError:
        return []
    root = Path(str(graph.meta.get('root', graph_path.parent.parent)))
    changed = []
    for name in graph.meta.get('watched', []):
        path = root / name
        candidates = path.rglob('*') if path.is_dir() else [path]
        for candidate in candidates:
            try:
                if candidate.is_file() and candidate.stat().st_mtime > built:
                    changed.append(os.path.relpath(candidate, root))
            except OSError:
                continue
    return sorted(set(changed))


PARALLEL_MINIMUM = 64
"""Fewer models than this to analyse are analysed in the build's own process."""

MAX_WORKERS = 8
"""The most worker processes a build starts unless ``KONSTRUCT_WORKERS`` says otherwise."""


def _workers() -> int:
    """How many processes analyse models: ``KONSTRUCT_WORKERS``, or the CPUs, up to ``MAX_WORKERS``."""
    configured = os.environ.get('KONSTRUCT_WORKERS', '').strip()
    if configured.isdigit():
        return max(1, int(configured))
    import sys

    if getattr(sys, 'frozen', False):
        return 1  # a frozen application cannot start a Python worker
    try:
        cpus = len(os.sched_getaffinity(0))  # the CPUs this process may use, not the host's
    except AttributeError:
        cpus = os.cpu_count() or 1
    return max(1, min(cpus, MAX_WORKERS))


def _analyse_model(job: tuple[str, str | None, dict[str, Any], str | None, str | None, str | None]) -> Any:
    """One model's lineage as plain data, in the build's own process."""
    from konstruct.engine import worker

    return worker.run(list(job))


def _worker_command() -> list[str]:
    """The command that starts a worker, read when it starts."""
    return [sys.executable, '-P', '-m', 'konstruct.engine.worker']


class _Workers:
    """Worker processes for one build, started on first use and stopped when the build ends."""

    def __init__(self) -> None:
        self.processes: list[Any] = []
        self.failed = False
        self.used = 0
        """Results from workers the build used."""
        self.directory: str | None = None
        """The workers' own empty working directory, removed when they stop."""

    def map(self, jobs: list[list[Any]]) -> list[Any] | None:
        """The results of ``jobs``, in order, analysed side by side; None when the workers cannot
        run, so the build analyses them itself."""
        import subprocess
        import tempfile
        from concurrent.futures import ThreadPoolExecutor

        import orjson

        if self.failed:
            return None
        try:
            while len(self.processes) < min(_workers(), len(jobs)):
                if self.directory is None:
                    self.directory = tempfile.mkdtemp(prefix='konstruct-workers-')
                self.processes.append(
                    # -P keeps the working directory off the worker's path, and that directory is
                    # its own and empty, so nothing in the repository or a shared directory is
                    # imported.
                    subprocess.Popen(
                        _worker_command(),
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        cwd=self.directory,
                    )
                )
            count = min(len(self.processes), len(jobs))
            chunks = [jobs[i::count] for i in range(count)]

            def run(index: int) -> list[Any]:
                process = self.processes[index]
                process.stdin.write(orjson.dumps(chunks[index]) + b'\n')
                process.stdin.flush()
                line = process.stdout.readline()
                if not line:
                    raise OSError('a worker stopped')
                result: list[Any] = orjson.loads(line)
                return result

            with ThreadPoolExecutor(max_workers=count) as threads:
                parts = list(threads.map(run, range(count)))
            if [len(p) for p in parts] != [len(c) for c in chunks]:
                raise ValueError('a worker returned the wrong number of results')
        except (OSError, ValueError) as error:
            log.debug('worker processes failed, so models are analysed in this process: %s', error)
            self.failed = True
            self.close()
            return None
        results: list[Any] = [None] * len(jobs)
        for index, part in enumerate(parts):
            results[index::count] = part
        return results

    def close(self) -> None:
        for process in self.processes:
            try:
                process.stdin.close()
                process.wait(timeout=10)
            except Exception:  # a worker that will not stop is killed
                process.kill()
        self.processes = []
        if self.directory is not None:
            import shutil

            shutil.rmtree(self.directory, ignore_errors=True)
            self.directory = None


def _topological(models: list[dbt.ModelSql]) -> list[dbt.ModelSql]:
    by_id = {m.node_id: m for m in models}
    ordered: list[dbt.ModelSql] = []
    state: dict[str, int] = {}

    def visit(model: dbt.ModelSql) -> None:
        stack: list[tuple[dbt.ModelSql, int]] = [(model, 0)]
        while stack:
            current, index = stack.pop()
            if index == 0:
                if state.get(current.node_id):
                    continue
                state[current.node_id] = 1
            deps = [by_id[u] for u in current.upstream if u in by_id]
            if index < len(deps):
                stack.append((current, index + 1))
                if not state.get(deps[index].node_id):
                    stack.append((deps[index], 0))
                continue
            state[current.node_id] = 2
            ordered.append(current)

    for model in models:
        visit(model)
    return ordered


def link_shared_relations(graph: Graph) -> None:
    """Connect nodes that describe one physical relation, such as a seed and the dbt source
    that reads the table it loads, or a dbt model another repository builds and the table this
    one's SQL reads: the producer feeds the reader, column by column. A table read under a
    shorter name than the one it is built under (``analytics.ltv`` for ``warehouse.analytics.ltv``)
    is that relation when no other relation in the graph, built or not, has a name ending so too.
    Two repositories that build one relation are warned of."""
    for key, nodes in graph.shared_relations():
        readers = [n for n in nodes if n.kind == 'source' or (n.kind == 'table' and not graph.built(n))]
        for reader in readers:
            for producer in nodes:
                if producer.kind in ('model', 'snapshot', 'seed'):
                    _link(graph, producer, reader)
        builders = sorted(
            {
                str(n.attrs.get('repository') or 'this project')
                for n in nodes
                if n not in readers and graph.built(n)
            }
        )
        if len(builders) > 1:
            graph.warn(f'repositories: {key} is built by {" and ".join(builders)}')
    ending: dict[str, set[str]] = {}
    unlinked: list[Node] = []
    for relation in list(graph.relations()):
        if not relation.relation:
            continue
        parts = relation.relation.split('.')
        for start in range(1, len(parts)):
            ending.setdefault('.'.join(parts[start:]), set()).add(relation.relation)
        if (
            relation.kind == 'table'
            and not graph.built(relation)
            and not any(
                n.kind in ('model', 'snapshot', 'seed') for n in graph.nodes_for_relation(relation.relation)
            )
        ):
            unlinked.append(relation)
    for reader in unlinked:
        matches = ending.get(reader.relation or '', set()) - {reader.relation}
        if len(matches) == 1:
            for producer in graph.nodes_for_relation(matches.pop()):
                if producer.kind != 'source' and graph.built(producer):
                    _link(graph, producer, reader)


def _link(graph: Graph, producer: Node, reader: Node) -> None:
    graph.add_edge(producer.id, reader.id, 'feeds', same_relation=True)
    for column in graph.columns_of(producer.id):
        mirror = graph.column(reader.id, column.name)
        if mirror is None:
            mirror = graph.add_column(
                reader,
                column.name,
                data_type=column.data_type,
                description=column.description,
                origin=column.origins[0] if column.origins else None,
                attrs={'from_relation': producer.id},
            )
        graph.add_edge(mirror.id, column.id, 'derived_from', transform='direct', same_relation=True)


def repository_name(config: Config, path: Path, taken: Iterable[str]) -> str:
    """A name for another repository that no earlier one has: its folder's name, or with its
    parent's when two folders share a name (``a-analytics``)."""
    used = set(taken)
    for name in (path.name, f'{path.parent.name}-{path.name}'):
        if name not in used:
            return name
    number = 2
    while f'{path.name}-{number}' in used:
        number += 1
    return f'{path.name}-{number}'


def merge_repository(
    graph: Graph,
    config: Config,
    path: Path,
    name: str,
    *,
    update: bool,
    terms: dict[str, str] | None = None,
) -> Builder | None:
    """Build another repository with its own configuration, under this build's options, and add
    its graph to ``graph``: a relation it shares with this one is the same node, and a relation
    one repository builds and the other reads as a dbt source is linked column by column. A node
    whose id this graph already uses for something else (another relation, or a test or task
    from another file) keeps its id with ``@name`` added, so the two never merge."""
    if not path.is_dir():
        graph.warn(f'repositories: {os.path.relpath(path, config.root)} is not a directory')
        return None
    # The project's trust covers the repositories it lists: its fingerprint includes theirs.
    sub_out = jsonio.real_directory(config.out_dir, 'repositories', name)
    try:
        sub_config = load_config(path, sub_out, trusted=config.trusted)
    except UntrustedConfig as error:
        # Name the repository: its konstruct.toml is not the one the user ran Konstruct on.
        raise UntrustedConfig(f'repositories: {os.path.relpath(path, config.root)}: {error}') from error
    sub_config.override(**config.options)
    builder = Builder(sub_config, update=update)
    other = builder.run()
    builder.cache.save()

    def moved(file: str) -> str:
        return os.path.relpath(path / file, config.root)

    renamed: dict[str, str] = {}
    for node in other.nodes.values():
        if node.kind == 'column':
            continue
        existing = graph.nodes.get(node.id)
        if existing is None:
            continue
        shared = (
            node.kind in RELATION_KINDS and existing.kind == node.kind and existing.relation == node.relation
        )
        if not shared and not (
            node.kind not in RELATION_KINDS and node.path and moved(node.path) == existing.path
        ):
            renamed[node.id] = f'{node.id}@{name}'

    def new_id(node_id: str) -> str:
        parent, sep, column = node_id.partition('#')
        return renamed[parent] + sep + column if parent in renamed else node_id

    # A relation one of the two builds (it has the file) and the other only reads keeps one node,
    # with the builder's definition: its file, conditions and column expressions, so formulas
    # compose across. ``defined`` are those the other repository builds, ``read`` those it reads.
    shared_relations = [
        node
        for node in other.nodes.values()
        if node.kind in RELATION_KINDS and node.id not in renamed and node.id in graph.nodes
    ]
    defined = {n.id for n in shared_relations if n.path and not graph.nodes[n.id].path}
    read = {n.id for n in shared_relations if not n.path and graph.nodes[n.id].path}
    for node in shared_relations:
        existing = graph.nodes[node.id]
        if node.path and existing.path and node.relation:
            first = existing.attrs.get('repository') or 'this project'
            graph.warn(
                f'repositories: {node.relation} is built by both {first} and {name}; the graph keeps '
                f"{first}'s definition"
            )
    for node in other.nodes.values():
        node.id = new_id(node.id)
        if node.parent:
            node.parent = new_id(node.parent)
        if node.id in graph.nodes:
            existing = graph.nodes[node.id]
            if node.id in defined or node.parent in defined:
                if node.path:
                    node.path = moved(node.path)
                _define(existing, node, existing, name)
            elif node.id in read or node.parent in read:
                _define(existing, existing, node, existing.attrs.get('repository'))
            continue
        if node.kind == 'term' and terms is not None:
            terms[node.id] = f'{name}: {_term_where(node)}'
        if node.path:
            node.path = moved(node.path)
        node.attrs['repository'] = name
        graph.add_node(node)
    for edge in other.edges:
        graph.add_edge(new_id(edge.source), new_id(edge.target), edge.kind, **edge.attrs)
    for warning in other.warnings:
        graph.warn(f'{name}: {warning}')
    for source in other.meta.get('sources', []):
        if source.get('path'):
            source['path'] = ', '.join(moved(p) for p in str(source['path']).split(', '))
        graph.meta.setdefault('sources', []).append({**source, 'repository': name})
    graph.meta['watched'] = sorted(
        {*graph.meta.get('watched', []), *(moved(w) for w in other.meta.get('watched', []))}
    )
    graph.meta.setdefault('repositories', []).append(os.path.relpath(path, config.root))
    return builder


def _define(existing: Node, builder: Node, reader: Node, repository: Any) -> None:
    """Make ``existing`` (the builder's node or the reader's) the one node for what the builder
    builds and the reader reads: the builder's file, attributes and mark, with what only the
    reader knows besides. It is inferred only when both infer it, so the graph is the same
    whichever repository is merged first."""
    attrs = {**reader.attrs, **builder.attrs}
    if not (builder.attrs.get('inferred') and reader.attrs.get('inferred')):
        attrs.pop('inferred', None)
    attrs.pop('repository', None)
    if isinstance(repository, str) and repository:
        attrs['repository'] = repository
    existing.path = builder.path
    existing.description = builder.description or reader.description
    existing.data_type = builder.data_type or reader.data_type
    existing.origins = list(dict.fromkeys([*builder.origins, *reader.origins]))
    existing.attrs = attrs


def _term_where(term: Node) -> str:
    """How the build's definitions warnings name ``term`` (see ``Builder.load_definitions``)."""
    return f'definitions: {_term_location(term)}: term {term.name!r}'


def _term_location(term: Node) -> str:
    """Where a term is defined: ``path:line``, or for a vocabulary concept its file and IRI."""
    line, iri = term.attrs.get('line'), term.attrs.get('iri')
    if line:
        return f'{term.path}:{line}'
    return f'{term.path} ({iri})' if iri else str(term.path)


def bind_across_repositories(graph: Graph, wheres: dict[str, str]) -> None:
    """Each repository binds its business terms before the graphs are merged, so a term that names
    a column another repository builds is bound here, and its own warning withdrawn; the same goes
    for a required filter that reads such a column. ``wheres`` gives, for each term from another
    repository, the prefix of its warnings. A term defined in more than one repository is a
    warning, as the repositories should share one definition."""
    terms = [n for n in graph.nodes.values() if n.kind == 'term']
    resolved: set[str] = set()  # warnings that no longer hold
    for term in terms:
        where = wheres.get(term.id) or _term_where(term)
        bound = {e.target for e in graph.out_edges(term.id, 'defines')}
        for ref in term.attrs.get('columns', []) or []:
            target, _ = _bind(graph, str(ref))
            if target is None or target.id in bound:
                continue
            graph.add_edge(term.id, target.id, 'defines')
            bound.add(target.id)
            resolved.update(w for w in graph.warnings if w.startswith(f'{where}: bound column {str(ref)!r} '))
        relations = {
            graph.label(graph.parent_of(t) or t) for t in (graph.nodes.get(i) for i in bound) if t is not None
        }
        for warning in graph.warnings:
            match = _FILTER_MISSING.match(warning[len(where) :]) if warning.startswith(where) else None
            if match is None:
                continue
            column = match[1]
            candidates = [column] if '.' in column else [f'{r}.{column}' for r in relations]
            if any(_bind(graph, c)[0] is not None for c in candidates):
                resolved.add(warning)
    graph.warnings = [w for w in graph.warnings if w not in resolved]
    by_name: dict[str, list[Node]] = {}
    for term in terms:
        by_name.setdefault(term.name.lower(), []).append(term)
    for same in by_name.values():
        if len(same) > 1:
            places = ', '.join(sorted(_term_location(t) for t in same))
            graph.warn(
                f'definitions: term {same[0].name!r} is defined in more than one repository ({places})'
            )


_FILTER_MISSING = re.compile(r": required filter .+ reads '(.+?)', which does not exist$")


def build(config: Config, *, update: bool = False) -> BuildResult:
    """Build the graph for ``config`` and write ``graph.json``, ``REPORT.md``, ``graph.html``, ``ontology.yml`` and ``explain.json``; :meth:`konstruct.Project.build` is the usual way in."""
    from konstruct.outputs.html import viewer_data, write_html, write_viewer_data
    from konstruct.outputs.report import write_report
    from konstruct.semantics.ontology import derive as derive_ontology

    started = time.perf_counter()
    builder = Builder(config, update=update)
    graph = builder.run()
    names: list[str] = []
    others: list[Builder] = []
    wheres: dict[str, str] = {}
    for repository in config.repositories:
        names.append(repository_name(config, repository, names))
        merged = merge_repository(graph, config, repository, names[-1], update=update, terms=wheres)
        if merged is not None:
            others.append(merged)
    if config.repositories:
        link_shared_relations(graph)
        bind_across_repositories(graph, wheres)
    # Packs bind once, to the merged graph, so repositories that share a warehouse share its terms.
    builder.bind_ontology_packs()
    log.info('deriving the ontology')
    graph.meta['dialect'] = builder.dialect or dialect_for(None)
    config.out_dir.mkdir(parents=True, exist_ok=True)
    graph_path = config.out_dir / 'graph.json'
    # The ontology's own rules are build warnings, so they reach graph.json and REPORT.md and
    # `--strict` fails on them; the rules it carries over from definitions.yml already are.
    ontology = derive_ontology(graph)
    # Each relation that holds one row per instance of an entity records it, so impact and trace
    # can say which business entities a change reaches without deriving the ontology again.
    held: dict[str, set[str]] = {}
    for entity in ontology.entities:
        for key in entity.key_ids:
            column = graph.nodes.get(key)
            if column is not None and column.parent in graph.nodes:
                held.setdefault(column.parent, set()).add(entity.name)
    for relation_id, entity_names in held.items():
        graph.nodes[relation_id].attrs['entities'] = sorted(entity_names)
    for rule in ontology.rules:
        if rule.name not in ('bound-columns-exist', 'approved-terms-are-owned'):
            for violation in rule.violations:
                graph.warn(f'ontology: {violation}')
    # The viewer's data, for graph.html and for explain.json, which `konstruct serve` sends the
    # page; written before graph.json, since the server reloads both when graph.json changes.
    data = viewer_data(graph, config.out_dir, builder.cache.quiet(), ontology) if config.html else None
    explain_path = write_viewer_data(data, config.out_dir / 'explain.json') if data is not None else None
    graph.meta['build_seconds'] = round(time.perf_counter() - started, 2)
    log.info('writing graph.json, REPORT.md, graph.html, ontology.yml and explain.json')
    graph.write(graph_path)
    outputs = [graph_path, write_report(graph, config.out_dir / 'REPORT.md')]
    if data is not None:
        outputs.append(write_html(graph, config.out_dir / 'graph.html', data=data))
    outputs.append(ontology.write(config.out_dir / 'ontology.yml'))
    if explain_path is not None:
        outputs.append(explain_path)
    builder.cache.save()
    return BuildResult(
        graph=graph,
        outputs=outputs,
        seconds=time.perf_counter() - started,
        cache_hits=builder.cache.hits + sum(o.cache.hits for o in others),
        cache_misses=builder.cache.misses + sum(o.cache.misses for o in others),
        inputs=_total_inputs([builder, *others]),
    )


def _total_inputs(builders: list[Builder]) -> dict[str, int]:
    total: dict[str, int] = {}
    for builder in builders:
        for key, count in builder.inputs.items():
            total[key] = total.get(key, 0) + count
    return total
