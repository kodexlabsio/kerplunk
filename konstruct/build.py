"""Build the graph from every input, then write graph.json, REPORT.md, graph.html and ontology.yml."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from konstruct import __version__, airflow, dagster, dbt, definitions, jsonio
from konstruct.config import (
    Config,
    ConfigError,
    discover_dag_files,
    discover_dagster_files,
    discover_definitions_files,
    discover_ossie_files,
    discover_sql_files,
    discover_warehouses,
    load_config,
)
from konstruct.graph import RELATION_KINDS, Edge, Graph, Node, relation_key
from konstruct.sql import PLACEHOLDER, StatementLineage, analyse, dialect_for, strip_templating
from konstruct.warehouse import MissingDriver, RelationMeta

log = logging.getLogger(__name__)

CACHE_VERSION = 1


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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {'version': CACHE_VERSION, 'konstruct': __version__, 'entries': self.used}
        self.path.write_text(jsonio.dumps(payload), encoding='utf-8')


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
        self.index = RelationIndex(self.graph)
        self.dialect: str | None = config.dialect
        self.default_catalog = config.default_catalog
        self.default_schema = config.default_schema
        self.inputs: dict[str, int] = {}
        # One entry per input, for the build summary and `konstruct status`.
        self.sources: list[dict[str, str]] = []
        self.watched: set[str] = set()

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
        self, sql: str, *, kind: str, schema_key: str, target: str | None = None
    ) -> list[StatementLineage]:
        key = Cache.key(
            kind, sql, self.dialect, self.default_catalog, self.default_schema, schema_key, target
        )

        def compute() -> list[dict[str, Any]]:
            return [
                s.to_dict()
                for s in analyse(
                    sql,
                    dialect=self.dialect,
                    schema=self.schema_columns(),
                    default_catalog=self.default_catalog,
                    default_db=self.default_schema,
                    target=target,
                )
            ]

        return [StatementLineage.from_dict(item) for item in self.cache.get_or_compute(key, compute)]

    # Steps

    def run(self) -> Graph:
        projects = self.load_dbt()
        self.load_warehouses(projects)
        self.link_shared_relations()
        self.warn_missing_catalogs()
        self.index = RelationIndex(self.graph)
        self.dbt_lineage()
        schema_key = Cache.key(self.schema_columns())
        self.warehouse_view_lineage(schema_key)
        used_sql = self.load_airflow(schema_key)
        self.load_dagster(schema_key)
        self.load_sql_files(projects, used_sql, schema_key)
        self.load_definitions()
        self.load_ossie()
        self.load_usage()
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
        if self.config.dbt_projects is not None:
            projects = [p for root in self.config.dbt_projects if (p := dbt.read_project(root))]
        else:
            projects = [p for p in dbt.find_projects(self.config.root) if not self.config.excluded(p.root)]
        self.loads: list[dbt.DbtLoad] = []
        for project in projects:
            status = dbt.ensure_artefacts(project, run_dbt=self.config.run_dbt, graph=self.graph)
            self.watch(project.root / 'dbt_project.yml', *project.source_paths)
            loaded = dbt.load(project, self.graph, dialect=self.config.dialect)
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
        for warehouse in discover_warehouses(self.config, projects):
            try:
                connector = warehouse.connector(self.config.root)
                relations = connector.fetch()
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

    def merge_relation(self, meta: RelationMeta, origin: str) -> None:
        nodes = self.graph.nodes_for_relation(meta.key)
        if not nodes:
            self._merge_into(None, meta, origin)
        for node in nodes:
            self._merge_into(node, meta, origin)

    def _merge_into(self, node: Node | None, meta: RelationMeta, origin: str) -> None:
        attrs = {'table_type': meta.kind, **meta.attrs}
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
        for loaded in self.loads:
            for model in _topological(loaded.models):
                node = self.graph.nodes[model.node_id]
                upstream = [self.graph.nodes[u] for u in model.upstream if u in self.graph.nodes]
                upstream_schema = self.schema_columns(upstream)
                checksum = node.attrs.get('checksum')
                key = Cache.key('dbt', model.sql, self.dialect, model.relation, upstream_schema, checksum)
                dialect = loaded.dialect or self.dialect

                def compute(
                    model: dbt.ModelSql = model,
                    schema: dict[str, Any] = upstream_schema,
                    dialect: str | None = dialect,
                    loaded: dbt.DbtLoad = loaded,
                ) -> Any:
                    return [
                        s.to_dict()
                        for s in analyse(
                            model.sql,
                            dialect=dialect,
                            schema=schema,
                            default_catalog=loaded.default_catalog,
                            default_db=loaded.default_schema,
                            target=model.relation,
                        )
                    ]

                statements = [StatementLineage.from_dict(s) for s in self.cache.get_or_compute(key, compute)]
                selects = [s for s in statements if s.kind in ('select', 'unparsed')]
                if not model.compiled:
                    node.attrs['lineage'] = 'from uncompiled SQL'
                for statement in selects[-1:]:
                    self.apply(statement, target=node, origin='dbt', label=f'dbt model {node.name}')
                count += 1
        self.inputs['dbt_models'] = count

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
                    if task.sql_path and Path(task.sql_path).is_file():
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
        from konstruct import usage

        settings = self.config.usage
        if not settings.enabled:
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
        from konstruct import ossie

        relationships: list[dict[str, str]] = []
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
                for many, one, name in binding.relationships:
                    relationships.append({'from': many.id, 'to': one.id, 'name': name, 'path': rel})
                for metric in model.metrics:
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
        for problem in definitions.validate(terms):
            self.graph.warn(f'definitions: {problem}')
        nodes: dict[str, Node] = {}
        for term in terms:
            where = f'definitions: {term.path}:{term.line}: term {term.name!r}'
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
                    origins=['definitions'],
                    attrs={
                        k: v
                        for k, v in {
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
                            'exceptions': term.exceptions,
                            'line': term.line,
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
        self.inputs['definitions'] = len(terms)

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

    from konstruct.query import search_nodes

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
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError, ValueError):
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
    that reads the table it loads: the producer feeds the reader, column by column."""
    for _, nodes in graph.shared_relations():
        reader = nodes[0]
        for producer in nodes[1:]:
            if producer.kind not in ('model', 'snapshot', 'seed') or reader.kind != 'source':
                continue
            graph.add_edge(producer.id, reader.id, 'feeds', same_relation=True)
            for column in graph.columns_of(producer.id):
                mirror = graph.column(reader.id, column.name)
                if mirror is None:
                    mirror = graph.add_column(
                        reader,
                        column.name,
                        data_type=column.data_type,
                        description=column.description,
                        origin='dbt',
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
    sub_config = load_config(path, config.out_dir / 'repositories' / name)
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

    for node in other.nodes.values():
        node.id = new_id(node.id)
        if node.parent:
            node.parent = new_id(node.parent)
        if node.id in graph.nodes:
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


def _term_where(term: Node) -> str:
    """How the build's definitions warnings name ``term`` (see ``Builder.load_definitions``)."""
    return f'definitions: {term.path}:{term.attrs.get("line")}: term {term.name!r}'


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
            places = ', '.join(sorted(f'{t.path}:{t.attrs.get("line")}' for t in same))
            graph.warn(
                f'definitions: term {same[0].name!r} is defined in more than one repository ({places})'
            )


_FILTER_MISSING = re.compile(r": required filter .+ reads '(.+?)', which does not exist$")


def build(config: Config, *, update: bool = False) -> BuildResult:
    """Build the graph for ``config`` and write ``graph.json``, ``REPORT.md``, ``graph.html`` and ``ontology.yml``; :meth:`konstruct.Project.build` is the usual way in."""
    from konstruct.html import write_html
    from konstruct.ontology import derive as derive_ontology
    from konstruct.report import write_report

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
    graph.meta['dialect'] = builder.dialect or dialect_for(None)
    config.out_dir.mkdir(parents=True, exist_ok=True)
    graph_path = config.out_dir / 'graph.json'
    # The ontology's own rules are build warnings, so they reach graph.json and REPORT.md and
    # `--strict` fails on them; the rules it carries over from definitions.yml already are.
    ontology = derive_ontology(graph)
    for rule in ontology.rules:
        if rule.name not in ('bound-columns-exist', 'approved-terms-are-owned'):
            for violation in rule.violations:
                graph.warn(f'ontology: {violation}')
    graph.meta['build_seconds'] = round(time.perf_counter() - started, 2)
    graph.write(graph_path)
    outputs = [
        graph_path,
        write_report(graph, config.out_dir / 'REPORT.md'),
        write_html(graph, config.out_dir / 'graph.html', cache=builder.cache.quiet()),
        ontology.write(config.out_dir / 'ontology.yml'),
    ]
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
