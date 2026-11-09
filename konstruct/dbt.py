"""dbt projects: find them, make sure their artefacts exist, and load them into the graph."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml
from sqlglot import exp
from sqlglot.errors import SqlglotError

from konstruct.graph import Graph, Node, NodeKind, column_id, relation_key
from konstruct.jinja import Renderer, render_model
from konstruct.sql import dialect_for, strip_templating

log = logging.getLogger(__name__)

SKIP_DIRS = frozenset(
    {
        '.git',
        '.venv',
        'venv',
        'node_modules',
        'dbt_packages',
        'dbt_modules',
        'target',
        'logs',
        'konstruct-out',
    }
)
RELATION_TYPES: dict[str, NodeKind] = {'model': 'model', 'seed': 'seed', 'snapshot': 'snapshot'}


@dataclass
class DbtProject:
    root: Path
    name: str
    target_path: Path
    profile: str | None = None
    source_paths: list[Path] = field(default_factory=list)

    @property
    def manifest_path(self) -> Path:
        return self.target_path / 'manifest.json'

    @property
    def catalog_path(self) -> Path:
        return self.target_path / 'catalog.json'


@dataclass
class ModelSql:
    """SQL Konstruct will analyse for one dbt node, and how good that SQL is."""

    node_id: str
    relation: str
    sql: str
    compiled: bool
    upstream: list[str]


@dataclass
class DbtLoad:
    project: DbtProject
    dialect: str | None
    models: list[ModelSql]
    default_catalog: str | None
    default_schema: str | None
    manifest_checksum: str | None
    has_catalog: bool = True
    counts: dict[str, int] = field(default_factory=dict)


def walk_dirs(root: Path) -> Iterator[Path]:
    for current, dirs, _ in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith('.'))
        yield Path(current)


def find_projects(root: Path) -> list[DbtProject]:
    projects = []
    for directory in walk_dirs(root):
        if (directory / 'dbt_project.yml').is_file():
            project = read_project(directory)
            if project:
                projects.append(project)
    return projects


def read_project(root: Path) -> DbtProject | None:
    try:
        config = yaml.safe_load((root / 'dbt_project.yml').read_text(encoding='utf-8')) or {}
    except (OSError, yaml.YAMLError) as error:
        log.warning('cannot read %s: %s', root / 'dbt_project.yml', error)
        return None
    if not isinstance(config, dict):
        return None
    paths: list[Path] = []
    for key, default in (
        ('model-paths', ['models']),
        ('seed-paths', ['seeds']),
        ('snapshot-paths', ['snapshots']),
        ('macro-paths', ['macros']),
        ('test-paths', ['tests']),
    ):
        paths.extend(root / p for p in config.get(key, default) if isinstance(p, str))
    return DbtProject(
        root=root,
        name=str(config.get('name', root.name)),
        target_path=root / str(config.get('target-path', 'target')),
        profile=config.get('profile'),
        source_paths=paths,
    )


def _newest_source_mtime(project: DbtProject) -> float:
    newest = (project.root / 'dbt_project.yml').stat().st_mtime
    for base in project.source_paths:
        if not base.is_dir():
            continue
        for path in base.rglob('*'):
            if path.suffix in ('.sql', '.yml', '.yaml', '.csv', '.py', '.md') and path.is_file():
                newest = max(newest, path.stat().st_mtime)
    return newest


def artefacts_stale(project: DbtProject) -> bool:
    if not project.manifest_path.is_file():
        return True
    return _newest_source_mtime(project) > project.manifest_path.stat().st_mtime


def ensure_artefacts(project: DbtProject, *, run_dbt: bool, graph: Graph) -> str:
    """Run ``dbt docs generate`` (or ``dbt parse``) when the artefacts are missing or stale.

    Returns what happened, for the build summary.
    """
    if not artefacts_stale(project):
        return 'artefacts up to date'
    missing = not project.manifest_path.is_file()
    state = 'missing' if missing else 'older than the project files'
    if not run_dbt:
        if missing:
            graph.warn(f'dbt: {project.name}: target/manifest.json is missing and running dbt is disabled')
            return 'no manifest; running dbt is disabled'
        graph.warn(f'dbt: {project.name}: target/manifest.json is {state}; using it anyway')
        return 'manifest is stale; running dbt is disabled'
    dbt = shutil.which('dbt')
    if dbt is None:
        graph.warn(
            f'dbt: {project.name}: target/manifest.json is {state} and dbt is not on PATH; '
            'run `dbt docs generate` in the project'
        )
        return f'manifest is {state} and dbt is not on PATH'
    env = dict(os.environ)
    if (project.root / 'profiles.yml').is_file() and 'DBT_PROFILES_DIR' not in env:
        env['DBT_PROFILES_DIR'] = str(project.root)
    for command in (['docs', 'generate'], ['parse']):
        log.info('running dbt %s in %s', ' '.join(command), project.root)
        # Konstruct only parses and compiles, so a require-dbt-version pin need not block it.
        result = subprocess.run(
            [dbt, *command, '--quiet', '--no-version-check'],
            cwd=project.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            if command == ['parse']:
                graph.warn(
                    f'dbt: {project.name}: `dbt docs generate` failed, so models are read from '
                    'uncompiled SQL and warehouse column types come from other inputs'
                )
            return f'ran dbt {" ".join(command)}'
        graph.warn(
            f'dbt: {project.name}: `dbt {" ".join(command)}` failed: {dbt_error(result.stdout + result.stderr)}'
        )
    return 'dbt failed'


_ANSI = re.compile(r'\x1b\[[0-9;]*m')
_TIMESTAMP = re.compile(r'^\d\d:\d\d:\d\d\s+')


def dbt_error(output: str, limit: int = 400) -> str:
    """The useful part of dbt's error output, on one line."""
    lines = [_TIMESTAMP.sub('', _ANSI.sub('', line)).strip() for line in output.splitlines()]
    lines = [line for line in lines if line]
    for index, line in enumerate(lines):
        if 'Encountered an error' in line:
            lines = lines[index + 1 :]
            break
    else:
        errors = [line for line in lines if 'error' in line.lower()]
        lines = errors or lines[-3:]
    text = ' '.join(line for line in lines if not line.startswith('Error encountered in '))
    text = text or (lines[-1] if lines else 'no output')
    return text if len(text) <= limit else text[: limit - 3] + '...'


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def parse_relation_name(name: str | None, dialect: str | None) -> str | None:
    if not name:
        return None
    try:
        table = exp.to_table(name, dialect=dialect)
    except (SqlglotError, ValueError):
        return relation_key(*name.replace('"', '').replace('`', '').split('.'))
    return relation_key(table.catalog, table.db, table.name)


def _relation_for(node: dict[str, Any], dialect: str | None) -> str | None:
    parsed = parse_relation_name(node.get('relation_name'), dialect)
    if parsed:
        return parsed
    name = node.get('alias') or node.get('identifier') or node.get('name')
    if not name:
        return None
    return relation_key(node.get('database'), node.get('schema'), name)


def _description(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _catalog_columns(
    catalog: dict[str, Any] | None, unique_id: str
) -> list[tuple[str, str | None, str | None]]:
    if not catalog:
        return []
    entry = catalog.get('nodes', {}).get(unique_id) or catalog.get('sources', {}).get(unique_id)
    if not entry:
        return []
    columns = sorted(entry.get('columns', {}).values(), key=lambda c: c.get('index') or 0)
    return [(c['name'], c.get('type'), _description(c.get('comment'))) for c in columns if c.get('name')]


def _primary_key(item: dict[str, Any]) -> list[str]:
    """The columns of a model's ``primary_key`` constraint, declared on the model or on its columns."""
    for constraint in item.get('constraints') or []:
        if isinstance(constraint, dict) and constraint.get('type') == 'primary_key':
            return [str(c) for c in constraint.get('columns') or []]
    return [
        str(column.get('name', name))
        for name, column in (item.get('columns') or {}).items()
        if isinstance(column, dict)
        and any(
            isinstance(c, dict) and c.get('type') == 'primary_key' for c in column.get('constraints') or []
        )
    ]


def _add_columns(graph: Graph, parent: Node, item: dict[str, Any], catalog: dict[str, Any] | None) -> None:
    primary_key = _primary_key(item)
    if primary_key:
        parent.attrs['primary_key'] = primary_key
    documented = {
        name.lower(): col for name, col in (item.get('columns') or {}).items() if isinstance(col, dict)
    }
    seen: set[str] = set()
    for name, dtype, comment in _catalog_columns(catalog, item['unique_id']):
        doc = documented.get(name.lower(), {})
        graph.add_column(
            parent,
            name,
            data_type=dtype or doc.get('data_type'),
            description=_description(doc.get('description')) or comment,
            origin='dbt',
        )
        seen.add(name.lower())
    for name, doc in documented.items():
        if name in seen:
            continue
        graph.add_column(
            parent,
            doc.get('name', name),
            data_type=doc.get('data_type'),
            description=_description(doc.get('description')),
            origin='dbt',
            attrs={'documented_only': True} if catalog else {},
        )


_REF = re.compile(
    r"\{\{\s*ref\(\s*['\"]([^'\"]+)['\"]\s*(?:,\s*['\"]([^'\"]+)['\"]\s*)?(?:,\s*v(?:ersion)?\s*=\s*[^)]*)?\)\s*\}\}"
)
_SOURCE = re.compile(r"\{\{\s*source\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")


def render_refs(raw: str, manifest: dict[str, Any]) -> str:
    """Best-effort rendering of uncompiled dbt SQL: resolve ref() and source(), drop other Jinja."""
    by_name: dict[str, str] = {}
    by_package: dict[tuple[str, str], str] = {}
    for node in manifest.get('nodes', {}).values():
        if node.get('resource_type') in RELATION_TYPES and node.get('relation_name'):
            by_name.setdefault(node['name'], node['relation_name'])
            by_package[(node['package_name'], node['name'])] = node['relation_name']
    sources: dict[tuple[str, str], str] = {
        (s['source_name'], s['name']): s['relation_name']
        for s in manifest.get('sources', {}).values()
        if s.get('relation_name')
    }

    def ref(match: re.Match[str]) -> str:
        first, second = match.group(1), match.group(2)
        if second:
            return by_package.get((first, second), second)
        return by_name.get(first, first)

    def source(match: re.Match[str]) -> str:
        return sources.get((match.group(1), match.group(2)), f'{match.group(1)}.{match.group(2)}')

    return strip_templating(_SOURCE.sub(source, _REF.sub(ref, raw)))


def load(project: DbtProject, graph: Graph, *, dialect: str | None) -> DbtLoad | None:
    """Add a dbt project's nodes, columns, tests and exposures to ``graph``."""
    manifest = _read_json(project.manifest_path)
    if manifest is None:
        graph.warn(f'dbt: {project.name}: no readable manifest at {project.manifest_path}')
        return None
    catalog = _read_json(project.catalog_path)
    metadata = manifest.get('metadata', {})
    dialect = dialect or dialect_for(metadata.get('adapter_type'))
    root = project.root

    def rel_path(item: dict[str, Any]) -> str | None:
        path = item.get('original_file_path')
        if not path:
            return None
        return os.path.relpath(root / str(path), str(graph.meta.get('root', root)))

    models: list[ModelSql] = []
    schemas: dict[tuple[str | None, str | None], int] = {}
    renderer: Renderer | None = None
    for item in manifest.get('nodes', {}).values():
        kind = RELATION_TYPES.get(item.get('resource_type', ''))
        if kind is None:
            continue
        relation = _relation_for(item, dialect)
        config = item.get('config') or {}
        node = graph.add_node(
            Node(
                id=item['unique_id'],
                kind=kind,
                name=item.get('alias') or item['name'],
                relation=relation,
                description=_description(item.get('description')),
                path=rel_path(item),
                origins=['dbt'],
                attrs={
                    'package': item.get('package_name'),
                    'fqn': item.get('fqn', []),
                    'materialized': config.get('materialized'),
                    'tags': item.get('tags', []),
                    'dbt_name': item['name'],
                    'checksum': (item.get('checksum') or {}).get('checksum'),
                },
            )
        )
        _add_columns(graph, node, item, catalog)
        upstream = [n for n in item.get('depends_on', {}).get('nodes', []) if not n.startswith('macro.')]
        for parent in upstream:
            graph.add_edge(parent, node.id, 'feeds', origin='dbt')
        key = (item.get('database'), item.get('schema'))
        schemas[key] = schemas.get(key, 0) + 1
        if kind == 'seed' or not relation:
            continue
        compiled = item.get('compiled_code')
        raw = item.get('raw_code')
        if compiled:
            models.append(ModelSql(node.id, relation, compiled, True, upstream))
        elif raw and item.get('language', 'sql') == 'sql':
            if renderer is None:
                macro_paths = [p for p in project.source_paths if 'macro' in p.name]
                renderer = Renderer(project.root, manifest, macro_paths=macro_paths or None)
            sql = render_model(raw, renderer, lambda text: render_refs(text, manifest))
            models.append(ModelSql(node.id, relation, sql, False, upstream))

    for item in manifest.get('sources', {}).values():
        node = graph.add_node(
            Node(
                id=item['unique_id'],
                kind='source',
                name=item.get('identifier') or item['name'],
                relation=_relation_for(item, dialect),
                description=_description(item.get('description'))
                or _description(item.get('source_description')),
                path=rel_path(item),
                origins=['dbt'],
                attrs={'source_name': item.get('source_name'), 'package': item.get('package_name')},
            )
        )
        _add_columns(graph, node, item, catalog)

    for item in manifest.get('exposures', {}).values():
        owner = item.get('owner') or {}
        node = graph.add_node(
            Node(
                id=item['unique_id'],
                kind='exposure',
                name=item['name'],
                description=_description(item.get('description')),
                path=rel_path(item),
                origins=['dbt'],
                attrs={
                    k: v
                    for k, v in {
                        'type': item.get('type'),
                        'label': item.get('label'),
                        'maturity': item.get('maturity'),
                        'owner': owner.get('name'),
                        'url': item.get('url'),
                    }.items()
                    if v
                },
            )
        )
        for parent in item.get('depends_on', {}).get('nodes', []):
            graph.add_edge(parent, node.id, 'feeds', origin='dbt')

    for item in manifest.get('nodes', {}).values():
        if item.get('resource_type') == 'test':
            _add_test(graph, item, rel_path(item))

    default_catalog, default_schema = max(schemas, key=lambda k: schemas[k]) if schemas else (None, None)
    return DbtLoad(
        project=project,
        dialect=dialect,
        models=models,
        default_catalog=default_catalog,
        default_schema=default_schema,
        manifest_checksum=metadata.get('invocation_id'),
        has_catalog=catalog is not None,
        counts={
            kind: sum(1 for item in group.values() if item.get('resource_type') == kind)
            for kind, group in (
                ('model', manifest.get('nodes', {})),
                ('seed', manifest.get('nodes', {})),
                ('snapshot', manifest.get('nodes', {})),
                ('test', manifest.get('nodes', {})),
                ('source', manifest.get('sources', {})),
                ('exposure', manifest.get('exposures', {})),
            )
        },
    )


def _add_test(graph: Graph, item: dict[str, Any], path: str | None) -> None:
    meta = item.get('test_metadata') or {}
    config = item.get('config') or {}
    node = graph.add_node(
        Node(
            id=item['unique_id'],
            kind='test',
            name=item['name'],
            description=_description(item.get('description')),
            path=path,
            origins=['dbt'],
            attrs={
                k: v
                for k, v in {
                    'test': meta.get('name') or 'singular',
                    'severity': config.get('severity'),
                    'column': item.get('column_name'),
                }.items()
                if v
            },
        )
    )
    targets = item.get('depends_on', {}).get('nodes', [])
    attached = item.get('attached_node') or (targets[0] if len(targets) == 1 else None)
    column = item.get('column_name')
    if attached and column and graph.column(attached, column):
        graph.add_edge(node.id, column_id(attached, column), 'tests')
    elif attached:
        graph.add_edge(node.id, attached, 'tests')
    kwargs = meta.get('kwargs') or {}
    for target in targets:
        if target == attached:
            continue
        field_name = kwargs.get('field')
        if meta.get('name') == 'relationships' and field_name and graph.column(target, field_name):
            graph.add_edge(node.id, column_id(target, field_name), 'tests', role='relationship target')
        else:
            graph.add_edge(node.id, target, 'tests')


# dbt node selection, as used by `dbt run --select` in orchestration code


def select_nodes(graph: Graph, selectors: list[str], excludes: list[str]) -> list[Node]:
    """Resolve a subset of dbt's selection syntax to graph nodes.

    Supports names, fqn and path prefixes, ``tag:``, ``path:``, ``fqn:``, ``resource_type:``,
    ``package:``, ``config.materialized:``, wildcards, ``+`` graph operators and ``,``
    intersections. Unknown methods select nothing and are reported by the caller.
    """
    nodes = [
        n for n in graph.nodes.values() if 'dbt' in n.origins and n.kind in ('model', 'seed', 'snapshot')
    ]
    if not selectors:
        chosen = {n.id for n in nodes}
    else:
        chosen = set()
        for selector in selectors:
            chosen |= _select_one(selector, nodes, graph)
    for exclude in excludes:
        chosen -= _select_one(exclude, nodes, graph)
    return [n for n in nodes if n.id in chosen]


def _select_one(selector: str, nodes: list[Node], graph: Graph) -> set[str]:
    parts = selector.split(',')
    result: set[str] | None = None
    for part in parts:
        found = _select_atom(part, nodes, graph)
        result = found if result is None else result & found
    return result or set()


_GRAPH_OP = re.compile(r'^(@)?(\d*\+)?(.*?)(\+\d*)?$')


def _select_atom(atom: str, nodes: list[Node], graph: Graph) -> set[str]:
    match = _GRAPH_OP.match(atom.strip())
    if not match:
        return set()
    at, up, body, down = match.groups()
    method, _, value = body.rpartition(':')
    method = method or 'default'
    seeds = {n.id for n in nodes if _matches(method, value, n)}
    result = set(seeds)
    if up or at:
        depth = int(up[:-1]) if up and up[:-1] else None
        result |= _relatives(graph, seeds, depth, upstream=True)
    if down or at:
        depth = int(down[1:]) if down and down[1:] else None
        downstream = _relatives(graph, seeds, depth, upstream=False)
        result |= downstream
        if at:
            result |= _relatives(graph, downstream, None, upstream=True)
    valid = {n.id for n in nodes}
    return result & valid


def downstream_of(graph: Graph, start: set[str]) -> set[str]:
    """Every node downstream of ``start`` through feeds edges (dbt's ``+`` operator)."""
    return _relatives(graph, start, None, upstream=False)


def _relatives(graph: Graph, start: set[str], depth: int | None, *, upstream: bool) -> set[str]:
    found: set[str] = set()
    frontier = set(start)
    level = 0
    while frontier and (depth is None or level < depth):
        level += 1
        following: set[str] = set()
        for node_id in frontier:
            edges = graph.in_edges(node_id, 'feeds') if upstream else graph.out_edges(node_id, 'feeds')
            for edge in edges:
                other = edge.source if upstream else edge.target
                if other not in found and other not in start:
                    following.add(other)
        found |= following
        frontier = following
    return found


def _matches(method: str, value: str, node: Node) -> bool:
    fqn: list[str] = list(node.attrs.get('fqn') or [])
    name = str(node.attrs.get('dbt_name') or node.name)
    if method == 'default':
        if '/' in value or value.endswith(('.sql', '.py', '.csv')):
            method = 'path'
        else:
            return _fqn_match(value, fqn, name)
    if method == 'fqn':
        return _fqn_match(value, fqn, name)
    if method == 'tag':
        return any(fnmatch(tag, value) for tag in node.attrs.get('tags', []))
    if method == 'path':
        path = (node.path or '').replace(os.sep, '/')
        value = value.rstrip('/')
        return any(
            fnmatch(path[i:], value) or path[i:].startswith(value + '/') or fnmatch(path[i:], value + '*')
            for i in [0, *[k + 1 for k, ch in enumerate(path) if ch == '/']]
        )
    if method == 'resource_type':
        return node.kind == value
    if method == 'package':
        return node.attrs.get('package') == value
    if method == 'config.materialized':
        return node.attrs.get('materialized') == value
    return False


def _fqn_match(value: str, fqn: list[str], name: str) -> bool:
    if fnmatch(name, value):
        return True
    parts = value.split('.')
    for start in (0, 1):
        window = fqn[start : start + len(parts)]
        if len(window) == len(parts) and all(fnmatch(f, p) for f, p in zip(window, parts, strict=True)):
            return True
    return False
