"""dbt projects: find them, make sure their artefacts exist, and load them into the graph."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from konstruct._core import Tree
from konstruct._core import file_identity as identity
from konstruct.core import jsonio, yamlio
from konstruct.core.graph import Graph, Node, NodeKind, column_id, relation_key

if TYPE_CHECKING:
    from konstruct.inputs.jinja import Renderer

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
    found: list[int] | None = None
    """``dbt_project.yml`` as it was just before it was read (``konstruct._core.file_identity``)."""
    paths: dict[str, list[Path]] = field(default_factory=dict)
    """The paths of each kind of file, by its key in dbt_project.yml without ``-paths``
    (``model``, ``seed``, ``snapshot``, ``macro``, ``test``)."""

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
    catalogued: set[str] = field(default_factory=set)
    """The nodes whose columns the catalog lists."""


def find_projects(root: Path, tree: Tree | None = None) -> list[DbtProject]:
    """The dbt projects under ``root``, in the order the walk meets their folders; ``tree`` is a
    walk of ``root`` already made."""
    tree = tree or Tree(str(root), sorted(SKIP_DIRS))
    projects = []
    for name, _ in tree.files(patterns=['dbt_project.yml']):
        project = read_project(Path(name).parent)
        if project:
            projects.append(project)
    return projects


def read_project(root: Path) -> DbtProject | None:
    found = identity(root / 'dbt_project.yml')
    try:
        config = yamlio.read(root / 'dbt_project.yml') or {}
    except (OSError, yaml.YAMLError) as error:
        log.warning('cannot read %s: %s', root / 'dbt_project.yml', error)
        return None
    if not isinstance(config, dict):
        return None
    paths: dict[str, list[Path]] = {}
    for key, default in (
        ('model-paths', ['models']),
        ('seed-paths', ['seeds']),
        ('snapshot-paths', ['snapshots']),
        ('macro-paths', ['macros']),
        ('test-paths', ['tests']),
    ):
        listed = config.get(key, default)
        paths[key.removesuffix('-paths')] = (
            [root / p for p in listed if isinstance(p, str)] if isinstance(listed, list) else []
        )
    return DbtProject(
        root=root,
        name=str(config.get('name', root.name)),
        target_path=root / str(config.get('target-path', 'target')),
        profile=config.get('profile'),
        source_paths=[p for listed in paths.values() for p in listed],
        found=found,
        paths=paths,
    )


# Tests as dbt YAML declares them


@dataclass
class TestDeclaration:
    """One generic test a dbt YAML file declares, on a model, seed, snapshot or source table, or on
    one of its columns."""

    __test__ = False  # not a pytest test class

    resource: str
    """``model``, ``seed``, ``snapshot`` or ``source``."""
    owner: str
    """The model's, seed's or snapshot's name, or for a source ``source.table``."""
    column: str | None
    """The column it tests, or None for a test of the whole relation."""
    test: str
    """The test as written, with any package (``dbt_utils.unique_combination_of_columns``)."""
    custom_name: str | None = None
    """The name a declaration gives the test (``name:``), if it gives one."""
    kwargs: dict[str, Any] = field(default_factory=dict)
    """Its arguments, with dbt 1.10's ``arguments:`` read as the arguments themselves."""
    config: dict[str, Any] = field(default_factory=dict)
    """Its configuration, ``severity`` and ``where`` included wherever they are written."""

    @property
    def short(self) -> str:
        """The test's name without its package (``unique_combination_of_columns``)."""
        return self.test.rsplit('.', 1)[-1]

    @property
    def package(self) -> str | None:
        return self.test.rsplit('.', 1)[0] if '.' in self.test else None

    @property
    def label(self) -> str:
        """The test as a change names it: its name as written, then any name it is given
        (``unique (order_key_is_unique)``)."""
        return f'{self.test} ({self.custom_name})' if self.custom_name else self.test


def yaml_tests(data: Any) -> list[TestDeclaration]:
    """Every generic test one parsed dbt YAML file declares, in the order it declares them. An
    entry that is not a test (a number, an empty mapping) is left out."""
    found: list[TestDeclaration] = []
    if not isinstance(data, dict):
        return found

    def add(resource: str, owner: str, column: str | None, entries: Any) -> None:
        for entry in entries if isinstance(entries, list) else []:
            declared = _test_declaration(resource, owner, column, entry)
            if declared is not None:
                found.append(declared)

    def declared(entry: dict[str, Any]) -> Any:
        return entry.get('data_tests') or entry.get('tests')

    for kind in ('models', 'seeds', 'snapshots'):
        for item in data.get(kind) or []:
            if not isinstance(item, dict) or not item.get('name'):
                continue
            name = str(item['name'])
            add(kind[:-1], name, None, declared(item))
            for column in item.get('columns') or []:
                if isinstance(column, dict) and column.get('name'):
                    add(kind[:-1], name, str(column['name']), declared(column))
    for source in data.get('sources') or []:
        if not isinstance(source, dict):
            continue
        for table in source.get('tables') or []:
            if isinstance(table, dict) and table.get('name'):
                owner = f'{source.get("name")}.{table["name"]}'
                add('source', owner, None, declared(table))
                for column in table.get('columns') or []:
                    if isinstance(column, dict) and column.get('name'):
                        add('source', owner, str(column['name']), declared(column))
    return found


def _test_declaration(resource: str, owner: str, column: str | None, entry: Any) -> TestDeclaration | None:
    if isinstance(entry, str):
        return TestDeclaration(resource, owner, column, entry)
    if not isinstance(entry, dict) or not entry:
        return None
    if 'test_name' in entry:
        # The long form: the test's arguments beside its own name and test_name.
        test, custom = str(entry['test_name']), entry.get('name')
        args = {k: v for k, v in entry.items() if k not in ('test_name', 'name')}
    else:
        key, value = next(iter(entry.items()))
        test = str(key)
        args = dict(value) if isinstance(value, dict) else {}
        custom = args.pop('name', None)
    config = dict(args.pop('config', None) or {})
    for key in ('severity', 'where'):
        if key in args:
            config[key] = args.pop(key)
    arguments = args.pop('arguments', None)
    kwargs = {**args, **(arguments if isinstance(arguments, dict) else {})}
    return TestDeclaration(resource, owner, column, test, str(custom) if custom else None, kwargs, config)


def _newest_source_mtime(project: DbtProject) -> float:
    newest = (project.root / 'dbt_project.yml').stat().st_mtime
    for base in project.source_paths:
        if not base.is_dir():
            continue
        for path in base.rglob('*'):
            if path.suffix in ('.sql', '.yml', '.yaml', '.csv', '.py', '.md') and path.is_file():
                newest = max(newest, path.stat().st_mtime)
    return newest


def _names_a_missing_file(project: DbtProject) -> bool:
    """Whether the manifest names a file of the project that is no longer there: a deleted or
    moved model, seed or YAML file leaves no file newer than the manifest."""
    try:
        manifest = jsonio.loads(project.manifest_path.read_bytes())
    except (OSError, ValueError):
        return True
    items = [*(manifest.get('nodes') or {}).values(), *(manifest.get('sources') or {}).values()]
    for item in items:
        if not isinstance(item, dict) or item.get('package_name') != project.name:
            continue
        path = item.get('original_file_path')
        if isinstance(path, str) and path and not (project.root / path).exists():
            return True
    return False


def files_behind(project_root: Path, manifest: dict[str, Any], item: dict[str, Any]) -> list[Path]:
    """The files besides its own that a dbt node's compiled SQL and configuration follow from:
    ``dbt_project.yml``, ``packages.yml``, the YAML that gives its properties, the YAML of the
    sources it reads, and the files of the macros it calls, directly or through other macros,
    where they are in the project or its installed packages."""
    found = [project_root / 'dbt_project.yml', project_root / 'packages.yml']
    patch = item.get('patch_path')
    if isinstance(patch, str) and patch:
        found.append(project_root / patch.split('://', 1)[-1])
    depends = item.get('depends_on') or {}
    for node_id in depends.get('nodes') or []:
        source = (manifest.get('sources') or {}).get(node_id)
        if isinstance(source, dict) and source.get('original_file_path'):
            found.append(project_root / str(source['original_file_path']))
    found += [path for _, path in macros_of(project_root, manifest, item)]
    return [f for f in dict.fromkeys(found) if f.is_file()]


def macros_of(
    project_root: Path, manifest: dict[str, Any], item: dict[str, Any]
) -> Iterator[tuple[dict[str, Any], Path]]:
    """Each macro a node calls, directly or through other macros, with the file that defines it in
    the project or an installed package."""
    every = manifest.get('macros') or {}
    pending, seen = list((item.get('depends_on') or {}).get('macros') or []), set()
    while pending:
        macro_id = pending.pop()
        macro = every.get(macro_id)
        if macro_id in seen or not isinstance(macro, dict):
            continue
        seen.add(macro_id)
        path = macro_file(project_root, item, macro)
        if path is not None:
            yield macro, path
        pending.extend((macro.get('depends_on') or {}).get('macros') or [])


def macro_file(project_root: Path, item: dict[str, Any], macro: dict[str, Any]) -> Path | None:
    """The file that defines ``macro``: in the project of ``item``, or in an installed package;
    None for dbt's own and an adapter's macros, which are not files of the project."""
    path = macro.get('original_file_path')
    if not isinstance(path, str) or not path:
        return None
    package = macro.get('package_name')
    if package == item.get('package_name'):
        return project_root / path
    installed = project_root / 'dbt_packages' / str(package) / path
    return installed if installed.is_file() else None


def artefacts_stale(project: DbtProject) -> bool:
    if not project.manifest_path.is_file():
        return True
    return _newest_source_mtime(project) > project.manifest_path.stat().st_mtime or _names_a_missing_file(
        project
    )


def ensure_artefacts(
    project: DbtProject, *, run_dbt: bool, graph: Graph, why_not: str = 'running dbt is disabled'
) -> str:
    """Run ``dbt docs generate`` (or ``dbt parse``) when the artefacts are missing or stale.
    Returns what happened, for the build summary.

    Args:
        project: The dbt project.
        run_dbt: Whether dbt may run.
        graph: The graph that receives the warnings.
        why_not: Why dbt is not run when ``run_dbt`` is false, for the warning and the summary.
    """
    if not artefacts_stale(project):
        return 'artefacts up to date'
    missing = not project.manifest_path.is_file()
    state = 'missing' if missing else 'older than the project files'
    if not run_dbt:
        if missing:
            graph.warn(f'dbt: {project.name}: target/manifest.json is missing and {why_not}')
            return f'no manifest; {why_not}'
        graph.warn(f'dbt: {project.name}: target/manifest.json is {state}; using it anyway')
        return f'manifest is stale; {why_not}'
    dbt = shutil.which('dbt')
    if dbt is None:
        graph.warn(
            f'dbt: {project.name}: target/manifest.json is {state} and dbt is not on PATH; '
            'run `dbt docs generate` in the project'
        )
        return f'manifest is {state} and dbt is not on PATH'
    env = dict(os.environ)
    # Konstruct makes no network requests of its own beyond the warehouses; dbt's anonymous usage
    # events would be one, so they are turned off for the runs Konstruct starts.
    env['DBT_SEND_ANONYMOUS_USAGE_STATS'] = 'false'
    env['DO_NOT_TRACK'] = '1'
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
    except (OSError, ValueError, RecursionError):  # a committed artefact may be nested too deeply
        return None
    return data if isinstance(data, dict) else None


def parse_relation_name(name: str | None, dialect: str | None) -> str | None:
    if not name:
        return None
    from sqlglot import exp
    from sqlglot.errors import SqlglotError

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


def _incremental(config: dict[str, Any]) -> dict[str, Any]:
    """An incremental model's key and strategy, which decide what a run keeps of earlier rows."""
    if config.get('materialized') != 'incremental':
        return {}
    found: dict[str, Any] = {k: config[k] for k in ('unique_key', 'incremental_strategy') if config.get(k)}
    key = found.get('unique_key')
    if isinstance(key, list):
        found['unique_key'] = [str(k) for k in key]
    return found


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


def _add_columns(graph: Graph, parent: Node, item: dict[str, Any], catalog: dict[str, Any] | None) -> bool:
    """Add a node's columns; whether the catalog lists them, rather than only its documentation."""
    primary_key = _primary_key(item)
    if primary_key:
        parent.attrs['primary_key'] = primary_key
        # A dbt constraint reaches the warehouse only under an enforced contract.
        if ((item.get('config') or {}).get('contract') or {}).get('enforced'):
            parent.attrs['primary_key_enforced'] = True
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
    return bool(seen)


_REF = re.compile(
    r"\{\{\s*ref\(\s*['\"]([^'\"]+)['\"]\s*(?:,\s*['\"]([^'\"]+)['\"]\s*)?(?:,\s*v(?:ersion)?\s*=\s*[^)]*)?\)\s*\}\}"
)
_SOURCE = re.compile(r"\{\{\s*source\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")


def render_refs(raw: str, manifest: dict[str, Any]) -> str:
    """Best-effort rendering of uncompiled dbt SQL: resolve ref() and source(), drop other Jinja."""
    from konstruct.core.sql import strip_templating

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


def load(
    project: DbtProject,
    graph: Graph,
    *,
    dialect: str | None,
    keep: Callable[[Iterable[Path]], list[Path]] | None = None,
) -> DbtLoad | None:
    """Add a dbt project's nodes, columns, tests and exposures to ``graph``. ``keep`` chooses
    which macro files may be read to render uncompiled models (by default, all)."""
    # The SQL parser and Jinja, which finding a project and reading its configuration do not need.
    from konstruct.core.sql import dialect_for
    from konstruct.inputs.jinja import Renderer, render_model

    manifest = _read_json(project.manifest_path)
    if manifest is None:
        graph.warn(f'dbt: {project.name}: no readable manifest at {project.manifest_path}')
        return None
    catalog = _read_json(project.catalog_path)
    metadata = manifest.get('metadata', {})
    dialect = dialect or dialect_for(metadata.get('adapter_type'))
    root = project.root
    # The project's {% docs __overview__ %} block, which dbt's documentation site opens on.
    overview = manifest.get('docs', {}).get(f'doc.{project.name}.__overview__', {}).get('block_contents')
    if isinstance(overview, str) and overview.strip():
        graph.meta.setdefault('overviews', {})[project.name] = overview.strip()

    def rel_path(item: dict[str, Any]) -> str | None:
        path = item.get('original_file_path')
        if not path:
            return None
        return os.path.relpath(root / str(path), str(graph.meta.get('root', root)))

    models: list[ModelSql] = []
    catalogued: set[str] = set()
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
                    **_incremental(config),
                },
            )
        )
        if _add_columns(graph, node, item, catalog):
            catalogued.add(node.id)
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
                renderer = Renderer(project.root, manifest, macro_paths=macro_paths or None, keep=keep)
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
                attrs={
                    'source_name': item.get('source_name'),
                    'package': item.get('package_name'),
                    # What the project says lands the table (`loader:` in sources.yml).
                    **({'declared_loader': item['loader']} if item.get('loader') else {}),
                },
            )
        )
        if _add_columns(graph, node, item, catalog):
            catalogued.add(node.id)

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

    semantic = semantic_metrics(manifest, rel_path)
    if semantic:
        graph.meta.setdefault('dbt_metrics', []).extend(semantic)

    default_catalog, default_schema = max(schemas, key=lambda k: schemas[k]) if schemas else (None, None)
    return DbtLoad(
        project=project,
        dialect=dialect,
        models=models,
        default_catalog=default_catalog,
        default_schema=default_schema,
        manifest_checksum=metadata.get('invocation_id'),
        has_catalog=catalog is not None,
        catalogued=catalogued,
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


def semantic_metrics(
    manifest: dict[str, Any], rel_path: Callable[[dict[str, Any]], str | None]
) -> list[dict[str, Any]]:
    """The metrics of dbt's semantic layer (MetricFlow), each with the aggregation, expression,
    model and non-additive dimension of the measure a simple metric names, and every filter the
    metric and its measure input apply. A measure no metric names is not a metric, so it is not
    listed. Only read, never run: the ontology's metrics show them with what they read."""
    measures: dict[str, dict[str, Any]] = {}
    for model in (manifest.get('semantic_models') or {}).values():
        if not isinstance(model, dict):
            continue
        nodes = (model.get('depends_on') or {}).get('nodes') or []
        for measure in model.get('measures') or []:
            if isinstance(measure, dict) and measure.get('name'):
                dimension = measure.get('non_additive_dimension')
                measures[str(measure['name'])] = {
                    'agg': measure.get('agg'),
                    'expr': str(measure.get('expr') or measure['name']),
                    'model': nodes[0] if nodes else None,
                    'non_additive': str(dimension.get('name'))
                    if isinstance(dimension, dict) and dimension.get('name')
                    else None,
                }
    found: list[dict[str, Any]] = []
    for metric in (manifest.get('metrics') or {}).values():
        if not isinstance(metric, dict) or not metric.get('name'):
            continue
        params = metric.get('type_params') or {}
        measure = params.get('measure') or {}
        base = measures.get(str(measure.get('name'))) if isinstance(measure, dict) else None
        entry = {
            'name': str(metric['name']),
            'label': metric.get('label'),
            'description': _description(metric.get('description')),
            'type': str(metric.get('type') or 'metric'),
            **(base or {'expr': params.get('expr')}),
            'filters': _where_filters(metric.get('filter'))
            + (_where_filters(measure.get('filter')) if isinstance(measure, dict) else []),
            'path': rel_path(metric),
        }
        found.append({k: v for k, v in entry.items() if v not in (None, '', [])})
    return sorted(found, key=lambda m: str(m['name']))


def _where_filters(value: Any) -> list[str]:
    """The SQL templates of a semantic-layer filter, as dbt writes them."""
    wheres = value.get('where_filters') if isinstance(value, dict) else None
    return [
        str(w['where_sql_template'])
        for w in wheres or []
        if isinstance(w, dict) and w.get('where_sql_template')
    ]


def _threshold(value: Any) -> str | None:
    """A test's ``error_if`` when it is not dbt's default (``!= 0``), which fails on any row."""
    text = str(value).strip() if value is not None else ''
    return text if text and text.replace(' ', '') not in ('!=0', '>0', '>=1') else None


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
                    # A threshold other than dbt's own lets some rows fail the test.
                    'error_if': _threshold(config.get('error_if')),
                    'column': item.get('column_name'),
                    'where': config.get('where'),
                    'columns': (meta.get('kwargs') or {}).get('combination_of_columns'),
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
    selectable = graph.indexed('dbt_selection', _Selectable, basis='relations')
    if not selectors:
        chosen = set(selectable.ids)
    else:
        chosen = set()
        for selector in selectors:
            chosen |= _select_one(selector, selectable, graph)
    for exclude in excludes:
        chosen -= _select_one(exclude, selectable, graph)
    return [n for n in selectable.nodes if n.id in chosen]


class _Selectable:
    """The dbt nodes a selector chooses from, in graph order, indexed by what a selector without
    wildcards names: a tag, a name or the start of an fqn, a part of a path, a resource type, a
    package or a materialisation. A selector is matched with :func:`_matches` against the nodes
    the index gives it, or every node when it has wildcards. A build gives a node a path in place
    where it had none (a SQL file that writes it), so a path selector is matched against the
    nodes that had none too; it changes no other attribute a selector reads in place, and a
    change through ``Graph.add_node`` makes the index again."""

    def __init__(self, graph: Graph) -> None:
        self.nodes = [
            n for n in graph.nodes.values() if 'dbt' in n.origins and n.kind in ('model', 'seed', 'snapshot')
        ]
        self.ids = {n.id for n in self.nodes}
        # fnmatch compares names as the file system does (os.path.normcase), so the keys do too.
        self.index: dict[tuple[str, Any], dict[str, Node]] = {}
        self.pathless: dict[str, Node] = {}
        for node in self.nodes:
            fqn = [None if part is None else os.path.normcase(part) for part in _parts(node.attrs.get('fqn'))]
            if not node.path:
                self.pathless[node.id] = node
            path = (node.path or '').replace(os.sep, '/')
            keys: set[tuple[str, Any]] = {
                ('name', os.path.normcase(str(node.attrs.get('dbt_name') or node.name))),
                ('resource_type', node.kind),
                *(('tag', os.path.normcase(tag)) for tag in _texts(node.attrs.get('tags'))),
                *(('part', os.path.normcase(part)) for part in path.split('/')),
            }
            # A manifest's value that is not text matches no selector's, so it has no key.
            for method, attribute in (('package', 'package'), ('config.materialized', 'materialized')):
                if isinstance(node.attrs.get(attribute), str):
                    keys.add((method, node.attrs[attribute]))
            for start in (0, 1):
                for end in range(start + 1, len(fqn) + 1):
                    if None in fqn[start:end]:
                        break
                    keys.add(('fqn', tuple(fqn[start:end])))
            for key in keys:
                self.index.setdefault(key, {})[node.id] = node
        self.parts = sorted(str(key[1]) for key in self.index if key[0] == 'part')

    def matching(self, method: str, value: str) -> set[str]:
        """The ids of the nodes that ``method:value`` matches."""
        return {n.id for n in self._candidates(method, value) if _matches(method, value, n)}

    def _candidates(self, method: str, value: str) -> Iterable[Node]:
        """Every node ``method:value`` can match, and perhaps others."""
        if method in ('resource_type', 'package', 'config.materialized'):
            return self.index.get((method, value), {}).values()
        if any(character in value for character in '*?['):
            return self.nodes
        if method == 'default':
            method = 'path' if '/' in value or value.endswith(('.sql', '.py', '.csv')) else 'fqn'
        if method == 'tag':
            return self.index.get(('tag', os.path.normcase(value)), {}).values()
        if method == 'fqn':
            parts = tuple(os.path.normcase(part) for part in value.split('.'))
            return {
                **self.index.get(('name', os.path.normcase(value)), {}),
                **self.index.get(('fqn', parts), {}),
            }.values()
        if method == 'path':
            # A path matches where a part of it, and what follows, starts with the value: that
            # part is the value's first, or starts with it when the value is one part.
            first, *rest = os.path.normcase(value.rstrip('/')).split(os.path.normcase('/'))
            if rest:
                return {**self.index.get(('part', first), {}), **self.pathless}.values()
            return {
                **{
                    node.id: node
                    for part in self.parts
                    if part.startswith(first)
                    for node in self.index[('part', part)].values()
                },
                **self.pathless,
            }.values()
        return []


def _select_one(selector: str, selectable: _Selectable, graph: Graph) -> set[str]:
    parts = selector.split(',')
    result: set[str] | None = None
    for part in parts:
        found = _select_atom(part, selectable, graph)
        result = found if result is None else result & found
    return result or set()


_GRAPH_OP = re.compile(r'^(@)?(\d*\+)?(.*?)(\+\d*)?$')


def _select_atom(atom: str, selectable: _Selectable, graph: Graph) -> set[str]:
    match = _GRAPH_OP.match(atom.strip())
    if not match:
        return set()
    at, up, body, down = match.groups()
    method, _, value = body.rpartition(':')
    method = method or 'default'
    seeds = selectable.matching(method, value)
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
    return result & selectable.ids


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


def _texts(value: Any) -> list[str]:
    """The names a node's attribute holds (its tags), leaving out what is not text: a manifest
    can hold ``null`` or a number there."""
    return [part for part in _parts(value) if part is not None]


def _parts(value: Any) -> list[str | None]:
    """The parts of a node's attribute in order (its fqn), each None that is not text, which
    a selector's part never matches."""
    if not value:
        return []
    try:
        return [part if isinstance(part, str) else None for part in value]
    except TypeError:
        return []


def _matches(method: str, value: str, node: Node) -> bool:
    fqn = _parts(node.attrs.get('fqn'))
    name = str(node.attrs.get('dbt_name') or node.name)
    if method == 'default':
        if '/' in value or value.endswith(('.sql', '.py', '.csv')):
            method = 'path'
        else:
            return _fqn_match(value, fqn, name)
    if method == 'fqn':
        return _fqn_match(value, fqn, name)
    if method == 'tag':
        return any(fnmatch(tag, value) for tag in _texts(node.attrs.get('tags')))
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


def _fqn_match(value: str, fqn: list[str | None], name: str) -> bool:
    if fnmatch(name, value):
        return True
    parts = value.split('.')
    for start in (0, 1):
        window = fqn[start : start + len(parts)]
        if len(window) == len(parts) and all(
            f is not None and fnmatch(f, p) for f, p in zip(window, parts, strict=True)
        ):
            return True
    return False
