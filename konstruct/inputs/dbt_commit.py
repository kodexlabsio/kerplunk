"""A dbt project's manifest as its files are at a commit, made from the manifest as it is now.

dbt's artefacts (``target/manifest.json`` and ``catalog.json``) are not in git, and Konstruct never
runs dbt on someone's history, so the project's build at an earlier commit starts from today's
manifest and changes it to what the commit's files say:

- each model's SQL is its file at the commit, rendered with the commit's own macros (its
  ``macro-paths``) and today's refs, except that where the commit's macros are today's, a model
  whose file is today's keeps the SQL dbt compiled; a snapshot's is the body of its
  ``{% snapshot %}`` block, and one defined in YAML (dbt 1.9) keeps today's;
- a model, seed, snapshot or singular test whose file is gone is left out, and a model or seed
  whose file is new is added, with the dependencies its refs and sources name;
- documented columns, descriptions, constraints, configuration and generic tests are those of the
  commit's YAML, and a source is there only when the commit's YAML declares it;
- the catalog's entry for a model or snapshot whose file differs from today's is left out, so its
  columns are those its SQL at the commit gives rather than today's warehouse.

What it cannot know is listed in :attr:`ManifestAt.notes`.
"""

from __future__ import annotations

import ast
import copy
import csv
import functools
import hashlib
import io
import os
import re
from collections import Counter
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from konstruct.core import jsonio, yamlio
from konstruct.inputs import dbt

_CALL_REF = re.compile(r"""\bref\(\s*['"]([^'"]+)['"](?:\s*,\s*['"]([^'"]+)['"])?""")
_CALL_SOURCE = re.compile(r"""\bsource\(\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]""")
_TO_REF = re.compile(r"""^\s*ref\(\s*['"]([^'"]+)['"]\s*(?:,\s*['"]([^'"]+)['"]\s*)?\)\s*$""")
_TO_SOURCE = re.compile(r"""^\s*source\(\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]\s*\)\s*$""")
_SNAPSHOT = re.compile(r'{%-?\s*snapshot\s+(\w+)\s*-?%}(.*?){%-?\s*endsnapshot\s*-?%}', re.DOTALL)
_CONFIG = re.compile(r'{{-?\s*(config\s*\(.*?\))\s*-?}}', re.DOTALL)
_RELATIONS = ('model', 'seed', 'snapshot')


@dataclass
class ManifestAt:
    """A project's manifest and catalog as its files are at one commit."""

    manifest: dict[str, Any]
    catalog: dict[str, Any] | None
    package_macros: list[str] = field(default_factory=list)
    """The models rendered here, rather than kept as dbt compiled them, that call a macro of an
    installed package, which is not in git: they are rendered without it."""
    uncatalogued: list[str] = field(default_factory=list)
    """The models and snapshots whose catalog entry was left out, as their files differ from
    today's: their columns come from their SQL alone."""
    package_compiled: list[str] = field(default_factory=list)
    """The models of installed packages that keep the SQL dbt compiled today, which inlines an
    ephemeral model of the project that the commit changed: packages are not in git."""
    moved: list[str] = field(default_factory=list)
    """The models the commit holds in another folder than today, which keep today's configuration:
    what dbt_project.yml configures for the commit's folder is not applied to them."""
    notes: list[str] = field(default_factory=list)


def manifest_at(
    manifest: dict[str, Any],
    catalog: dict[str, Any] | None,
    project: dbt.DbtProject,
    keep: Callable[[Iterable[Path]], list[Path]] = list,
    compiled_from: dict[str, str] | None = None,
    packages: Collection[str] = (),
) -> ManifestAt:
    """The manifest and catalog of ``project``, whose files are as they are at a commit, from
    ``manifest`` and ``catalog`` as they are now. ``keep`` chooses which of the project's files
    may be read (the configuration's :meth:`~konstruct.engine.config.Config.inside`). Neither
    argument is changed.

    ``compiled_from`` is :func:`compile_inputs` as it is now: where the commit's are the same, a
    model whose own file is today's keeps today's compiled SQL, which dbt itself rendered, rather
    than being rendered again; otherwise every model is rendered with the commit's files.
    ``packages`` are the installed packages (:func:`installed_packages`), whose macros a model
    rendered here goes without, as they are not in git: those models are listed in
    :attr:`ManifestAt.package_macros`."""
    own = _own_package(manifest, project)
    nodes: dict[str, Any] = {k: dict(v) for k, v in (manifest.get('nodes') or {}).items()}
    sources: dict[str, Any] = {k: dict(v) for k, v in (manifest.get('sources') or {}).items()}
    written = {**manifest, 'nodes': nodes, 'sources': sources}
    result = ManifestAt(written, None)
    files = project_files(project, keep)
    compiled = compiled_from is not None and compile_inputs(project, keep) == compiled_from
    changed: set[str] = set()
    gone: dict[str, dict[str, Any]] = {}
    covered: set[str] = set()
    headers: dict[str, dict[str, Any]] = {}
    for node_id in list(nodes):
        item = nodes[node_id]
        kind, path = item.get('resource_type'), item.get('original_file_path')
        if item.get('package_name', own) != own or not isinstance(path, str):
            continue
        if kind == 'test' and item.get('test_metadata'):
            del nodes[node_id]  # generic tests come from the commit's YAML below
            continue
        if kind not in (*_RELATIONS, 'test'):
            continue
        covered.add(Path(path).as_posix())
        text = files.get(Path(path).as_posix())
        if text is None:
            gone[node_id] = nodes.pop(node_id)
            continue
        if kind in ('model', 'snapshot') and not path.endswith(('.yml', '.yaml')):
            # dbt keeps a model's SQL without the whitespace around it.
            raw = _snapshot_body(text, str(item.get('name'))) if kind == 'snapshot' else text.strip()
            if raw is None:
                gone[node_id] = nodes.pop(node_id)
                continue
            if raw.strip() != str(item.get('raw_code') or '').strip():
                changed.add(node_id)
                item['raw_code'] = raw
                item['depends_on'] = _depends_on(raw, nodes, sources, own, item.get('depends_on'))
                item['config'] = {**(item.get('config') or {}), **_config_call(raw)}
        if kind in _RELATIONS:
            item['columns'] = {}
            item.pop('constraints', None)
        if kind == 'seed' and path.endswith('.csv'):
            # A seed's columns are its CSV's header, which a commit may change.
            headers[node_id] = _header(text)
            listed = ((catalog or {}).get('nodes') or {}).get(node_id)
            if isinstance(listed, dict) and {c.lower() for c in listed.get('columns') or {}} != {
                c.lower() for c in headers[node_id]
            }:
                changed.add(node_id)
    # dbt compiles an ephemeral model into the SQL of the models that read it, so a model keeps
    # the SQL dbt compiled only where no model upstream of it differs from today's either.
    today = manifest.get('nodes') or {}
    stale = _downstream(today, changed | gone.keys()) if compiled else set(nodes)
    ephemeral = {
        n
        for n in changed | gone.keys()
        if ((today.get(n) or {}).get('config') or {}).get('materialized') == 'ephemeral'
    }
    result.package_compiled = sorted(
        str(nodes[n].get('name'))
        for n in _downstream(today, ephemeral) & nodes.keys()
        if nodes[n].get('package_name', own) != own and 'compiled_code' in nodes[n]
    )
    for node_id in stale & nodes.keys():
        item = nodes[node_id]
        if (
            'compiled_code' in item
            and item.get('resource_type') in ('model', 'snapshot')
            and item.get('package_name', own) == own
            # A snapshot YAML declares has no SQL of its own to render: it keeps today's.
            and not str(item.get('original_file_path') or '').endswith(('.yml', '.yaml'))
        ):
            del item['compiled_code']
            macros = (item.get('depends_on') or {}).get('macros') or []
            if any(str(m).split('.')[1:2] and str(m).split('.')[1] in packages for m in macros):
                result.package_macros.append(str(item.get('name')))
    like = _typical(nodes, own)
    if like is not None:
        _new_nodes(nodes, sources, project, files, covered, own, like)
    for node_id in gone.keys() & nodes.keys():
        # The same model in another folder: still the model dbt configured today.
        old, item = gone[node_id], nodes[node_id]
        item.update({k: old[k] for k in ('database', 'schema', 'alias', 'relation_name') if k in old})
        item['config'] = {**(old.get('config') or {}), **_config_call(str(item.get('raw_code') or ''))}
        result.moved.append(str(item.get('name')))
    result.moved.sort()
    documents = _documents(project, files)
    _sources(sources, documents, own, like)
    by_name = {
        (str(i.get('resource_type')), str(i.get('name'))): node_id
        for node_id, i in nodes.items()
        if i.get('package_name', own) == own
    }
    by_source = {(str(s.get('source_name')), str(s.get('name'))): k for k, s in sources.items()}
    for path, data in documents:
        for kind in _RELATIONS:
            for entry in data.get(f'{kind}s') or []:
                found = by_name.get((kind, str(entry.get('name')))) if isinstance(entry, dict) else None
                if found is not None:
                    _document(nodes[found], entry)
        for test in dbt.yaml_tests(data):
            if test.resource == 'source':
                source, _, table = test.owner.partition('.')
                attached = by_source.get((source, table))
            else:
                attached = by_name.get((test.resource, test.owner))
            if attached is not None:
                _test(nodes, own, path, attached, test, by_name, by_source)
    for node_id, header in headers.items():
        if node_id in nodes:
            nodes[node_id]['columns'] = {**header, **nodes[node_id].get('columns', {})}
    result.package_macros.sort()
    if catalog is not None:
        kept = copy.copy(catalog)
        kept['nodes'] = {
            k: v for k, v in (catalog.get('nodes') or {}).items() if k in nodes and k not in changed
        }
        result.catalog = kept
        result.uncatalogued = sorted(
            str(nodes[k].get('name')) for k in changed if k in (catalog.get('nodes') or {})
        )
    return result


def _downstream(nodes: dict[str, Any], changed: set[str]) -> set[str]:
    """``changed`` and every node of today's ``nodes`` that depends on one of them, however far
    down."""
    readers: dict[str, list[str]] = {}
    for node_id, item in nodes.items():
        for upstream in (item.get('depends_on') or {}).get('nodes') or []:
            readers.setdefault(str(upstream), []).append(node_id)
    found = set(changed)
    queue = list(changed)
    while queue:
        for reader in readers.get(queue.pop(), []):
            if reader not in found:
                found.add(reader)
                queue.append(reader)
    return found


def _own_package(manifest: dict[str, Any], project: dbt.DbtProject) -> str:
    """The project's package in today's manifest, which a commit may have named differently."""
    named = (manifest.get('metadata') or {}).get('project_name')
    if isinstance(named, str) and named:
        return named
    packages = Counter(
        str(i.get('package_name'))
        for i in (manifest.get('nodes') or {}).values()
        if i.get('resource_type') == 'model' and i.get('package_name')
    )
    return packages.most_common(1)[0][0] if packages else project.name


def project_files(
    project: dbt.DbtProject, keep: Callable[[Iterable[Path]], list[Path]], kind: str | None = None
) -> dict[str, str]:
    """Every file the project's paths hold (with ``kind``, only its paths of that kind; without,
    all but its macros), by its path in the project, read once. A link is not followed, so no file
    read is outside the project."""
    root = project.root.resolve()
    found: dict[str, str] = {}
    for name, paths in project.paths.items():
        if (name != kind) if kind is not None else name == 'macro':
            continue
        for base in paths:
            start = base.resolve()
            if not start.is_dir() or not start.is_relative_to(root):
                continue
            listed = []
            for folder, directories, names in os.walk(start):
                directories[:] = [d for d in directories if d not in dbt.SKIP_DIRS]
                listed += [Path(folder, n) for n in names if not Path(folder, n).is_symlink()]
            for file in keep(listed):
                try:
                    found[file.relative_to(root).as_posix()] = file.read_text(
                        encoding='utf-8', errors='replace'
                    )
                except (OSError, ValueError):
                    continue
    return found


def _snapshot_body(text: str, name: str) -> str | None:
    """The SQL of the snapshot ``name``: the body of its ``{% snapshot %}`` block, as dbt keeps it."""
    for match in _SNAPSHOT.finditer(text):
        if match[1] == name:
            return match[2]
    return None


def _config_call(raw: str) -> dict[str, Any]:
    """The literal arguments of a model's ``{{ config(...) }}``, read as Python syntax and never run."""
    found: dict[str, Any] = {}
    for match in _CONFIG.finditer(raw):
        try:
            call = ast.parse(match[1], mode='eval').body
        except (SyntaxError, ValueError, RecursionError):
            continue
        if not isinstance(call, ast.Call):
            continue
        for keyword in call.keywords:
            if keyword.arg is None:
                continue
            try:
                found[keyword.arg] = ast.literal_eval(keyword.value)
            except (ValueError, TypeError, SyntaxError, RecursionError):
                continue
    return found


def _depends_on(
    raw: str, nodes: dict[str, Any], sources: dict[str, Any], own: str, today: Any
) -> dict[str, Any]:
    """What a model's SQL refers to: the nodes its ``ref()`` calls and ``source()`` calls name, with
    the macros today's manifest says it calls."""
    names = {
        (str(i.get('package_name')), str(i.get('name'))): k
        for k, i in nodes.items()
        if i.get('resource_type') in _RELATIONS
    }
    found: list[str] = []
    for match in _CALL_REF.finditer(raw):
        package, name = (match[1], match[2]) if match[2] else (own, match[1])
        node = names.get((package, name)) or next((k for (_, n), k in names.items() if n == name), None)
        if node is not None and node not in found:
            found.append(node)
    for match in _CALL_SOURCE.finditer(raw):
        source = next(
            (k for k, s in sources.items() if (s.get('source_name'), s.get('name')) == (match[1], match[2])),
            None,
        )
        if source is not None and source not in found:
            found.append(source)
    macros = (today or {}).get('macros') if isinstance(today, dict) else None
    return {'nodes': found, 'macros': list(macros or [])}


def _typical(nodes: dict[str, Any], own: str) -> dict[str, Any] | None:
    """A model in the database and schema most of the project's models are in, for a new node."""
    models = [i for i in nodes.values() if i.get('resource_type') == 'model' and i.get('package_name') == own]
    if not models:
        return None
    schemas = Counter((m.get('database'), m.get('schema')) for m in models)
    common = schemas.most_common(1)[0][0]
    found: dict[str, Any] = next(m for m in models if (m.get('database'), m.get('schema')) == common)
    return found


def _relation_name(
    like: dict[str, Any], name: str, schema: str | None = None, database: str | None = None
) -> str | None:
    """``name`` quoted as ``like``'s relation name is, in its database and schema unless others
    are given."""
    relation = like.get('relation_name')
    if not isinstance(relation, str) or not relation:
        return None
    parts = relation.split('.')
    last = parts[-1]
    quote = last[0] if last[:1] in '"`[' else ''
    closing = {'[': ']'}.get(quote, quote)

    def quoted(value: str) -> str:
        return f'{quote}{value}{closing}'

    names = [database or like.get('database'), schema or like.get('schema'), name][-len(parts) :]
    return '.'.join(quoted(str(n)) for n in names if n)


def _new_nodes(
    nodes: dict[str, Any],
    sources: dict[str, Any],
    project: dbt.DbtProject,
    files: dict[str, str],
    covered: set[str],
    own: str,
    like: dict[str, Any],
) -> None:
    """A node for each model SQL file and seed CSV file the manifest has no node for."""
    root = project.root.resolve()
    for kind, suffix in (('model', '.sql'), ('seed', '.csv')):
        bases = [b.resolve() for b in project.paths.get(kind, [])]
        for relative, text in sorted(files.items()):
            path = root / relative
            base = next((b for b in bases if path.is_relative_to(b)), None)
            if base is None or relative in covered or not relative.endswith(suffix):
                continue
            name = path.stem
            unique_id = f'{kind}.{own}.{name}'
            if unique_id in nodes:
                continue
            item: dict[str, Any] = {
                'unique_id': unique_id,
                'resource_type': kind,
                'name': name,
                'alias': name,
                'package_name': own,
                'database': like.get('database'),
                'schema': like.get('schema'),
                'relation_name': _relation_name(like, name),
                'original_file_path': relative,
                'fqn': [own, *path.relative_to(base).with_suffix('').parts],
                'config': {'materialized': 'seed' if kind == 'seed' else 'view'},
                'columns': {},
            }
            if kind == 'model':
                item.update(
                    raw_code=text,
                    language='sql',
                    depends_on=_depends_on(text, nodes, sources, own, None),
                    config={'materialized': 'view', **_config_call(text)},
                )
            else:
                item['columns'] = _header(text)
            nodes[unique_id] = item


def _header(text: str) -> dict[str, Any]:
    """A seed's columns, from its CSV's header as a CSV reader reads it: quoted names unquoted, a
    comma inside quotes kept, and a byte order mark before it left out."""
    row = next(csv.reader(io.StringIO(text.removeprefix('\ufeff'))), [])
    return {c.strip(): {'name': c.strip()} for c in row if c.strip()}


def _documents(project: dbt.DbtProject, files: dict[str, str]) -> list[tuple[str, dict[str, Any]]]:
    found = []
    for relative, text in sorted(files.items()):
        if not relative.endswith(('.yml', '.yaml')) or relative == 'dbt_project.yml':
            continue
        data = _yaml(text)
        if isinstance(data, dict):
            found.append((relative, data))
    return found


@functools.lru_cache(maxsize=256)
def _yaml(text: str) -> Any:
    """A YAML file parsed, once for each text: most of the commits of a range share each file.
    What it gives is read, never changed."""
    try:
        return yamlio.load(text)
    except yaml.YAMLError:
        return None


def _sources(
    sources: dict[str, Any],
    documents: list[tuple[str, dict[str, Any]]],
    own: str,
    like: dict[str, Any] | None,
) -> None:
    """Today's sources of the project kept only where the commit's YAML declares them, and a
    source for each table it declares that today's manifest does not have."""
    declared: dict[tuple[str, str], tuple[str, dict[str, Any], dict[str, Any]]] = {}
    for path, data in documents:
        for source in data.get('sources') or []:
            if not isinstance(source, dict) or not source.get('name'):
                continue
            for table in source.get('tables') or []:
                if isinstance(table, dict) and table.get('name'):
                    declared[(str(source['name']), str(table['name']))] = (path, source, table)
    present: set[tuple[str, str]] = set()
    for key in list(sources):
        item = sources[key]
        name = (str(item.get('source_name')), str(item.get('name')))
        if item.get('package_name', own) != own:
            continue
        if name not in declared:
            del sources[key]
            continue
        present.add(name)
        _, source, table = declared[name]
        # Where the table is, as the commit's YAML says, quoted as today's relation name is.
        identifier = str(table.get('identifier') or name[1])
        schema = str(source.get('schema') or name[0])
        database = source.get('database') or item.get('database')
        relation = _relation_name(item, identifier, schema, database) if item.get('relation_name') else None
        item.update(
            identifier=identifier,
            schema=schema,
            database=database,
            relation_name=relation or (_relation_name(like, identifier, schema, database) if like else None),
            columns=_columns(table),
        )
    for (source_name, table_name), (path, source, table) in sorted(declared.items()):
        if (source_name, table_name) in present:
            continue
        identifier = str(table.get('identifier') or table_name)
        schema = str(source.get('schema') or source_name)
        database = source.get('database') or (like or {}).get('database')
        unique_id = f'source.{own}.{source_name}.{table_name}'
        sources[unique_id] = {
            'unique_id': unique_id,
            'resource_type': 'source',
            'source_name': source_name,
            'name': table_name,
            'identifier': identifier,
            'package_name': own,
            'database': database,
            'schema': schema,
            'relation_name': _relation_name(like, identifier, schema, database) if like else None,
            'original_file_path': path,
            'columns': _columns(table),
            'description': table.get('description') or '',
        }


def _columns(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        str(c['name']): {k: c[k] for k in ('name', 'description', 'data_type', 'constraints') if k in c}
        for c in entry.get('columns') or []
        if isinstance(c, dict) and c.get('name')
    }


def _document(item: dict[str, Any], entry: dict[str, Any]) -> None:
    """A node's description, documented columns, constraints and configuration as the commit's
    YAML has them."""
    if isinstance(entry.get('description'), str):
        item['description'] = entry['description']
    if isinstance(entry.get('constraints'), list):
        item['constraints'] = entry['constraints']
    if isinstance(entry.get('config'), dict):
        item['config'] = {**(item.get('config') or {}), **entry['config']}
    item['columns'] = _columns(entry)


def _test(
    nodes: dict[str, Any],
    own: str,
    path: str,
    attached: str,
    test: dbt.TestDeclaration,
    by_name: dict[tuple[str, str], str],
    by_source: dict[tuple[str, str], str],
) -> None:
    """A generic test the commit's YAML declares, as dbt would list it in the manifest."""
    targets = [attached]
    target = test.kwargs.get('to')
    if isinstance(target, str):
        ref, source = _TO_REF.match(target), _TO_SOURCE.match(target)
        name = (ref[2] or ref[1]) if ref else None
        found = (
            next((by_name.get((k, name)) for k in _RELATIONS if (k, name) in by_name), None)
            if name
            else by_source.get((source[1], source[2]))
            if source
            else None
        )
        if found is not None:
            targets.append(found)
    title = '_'.join(p for p in (test.short, test.owner.replace('.', '_'), test.column) if p)
    digest = hashlib.sha256(
        jsonio.dumps([path, test.label, test.kwargs], sort_keys=True, default=str).encode()
    ).hexdigest()
    unique_id = f'test.{own}.{title}.{digest[:10]}'
    nodes[unique_id] = {
        'unique_id': unique_id,
        'resource_type': 'test',
        'name': test.custom_name or title,
        'package_name': own,
        'original_file_path': path,
        'test_metadata': {
            'name': test.short,
            'namespace': test.package,
            'kwargs': {**test.kwargs, **({'column_name': test.column} if test.column else {})},
        },
        'column_name': test.column,
        'attached_node': attached,
        'depends_on': {'nodes': targets, 'macros': []},
        'config': dict(test.config),
    }


def installed_packages(project: dbt.DbtProject) -> set[str]:
    """The packages installed in the project (under ``dbt_packages``), which are not in git."""
    installed = project.root / 'dbt_packages'
    return {p.name for p in installed.iterdir() if p.is_dir()} if installed.is_dir() else set()


def compile_inputs(
    project: dbt.DbtProject, keep: Callable[[Iterable[Path]], list[Path]] = list
) -> dict[str, str]:
    """Every file of the project besides its models' SQL that dbt's compiled SQL follows from, by
    path: dbt_project.yml, the package files, every YAML properties file and every macro. Where
    a commit's are byte for byte today's, today's compiled SQL is the commit's for each model
    whose own file is today's too."""
    found = {
        name: text for name, text in project_files(project, keep).items() if name.endswith(('.yml', '.yaml'))
    }
    found.update(project_files(project, keep, 'macro'))
    for name in _PACKAGE_FILES:
        path = project.root / name
        if path.is_file() and not path.is_symlink() and keep([path]):
            found[name] = path.read_text(encoding='utf-8', errors='replace')
    return found


_PACKAGE_FILES = ('dbt_project.yml', 'packages.yml', 'dependencies.yml', 'package-lock.yml')
