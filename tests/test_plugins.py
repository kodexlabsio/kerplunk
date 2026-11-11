"""Plugins: found through installed distributions' entry points, enabled per project, isolated.

The example plugin in examples/plugin is installed as a distribution on ``sys.path``: a
``.dist-info`` directory written from its own pyproject.toml, so its entry points are exactly the
ones it declares, and ``importlib.metadata`` finds it as it finds any installed package.
"""

from __future__ import annotations

import ast
import dataclasses
import datetime
import enum
import importlib
import json
import subprocess
import sys
import textwrap
import tomllib
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from konstruct import Project
from konstruct.core.graph import Graph
from konstruct.interfaces.cli import main
from konstruct.interfaces.mcp_server import build_server
from konstruct.interfaces.status import inspect
from konstruct.outputs.export import ExportError, export
from konstruct.plugins import (
    API_VERSION,
    Column,
    Fragment,
    PluginEdge,
    PluginNode,
    Relation,
    Source,
    Sql,
)
from konstruct.plugins.registry import normalise

ROOT = Path(__file__).resolve().parents[1]
OURS = f'{API_VERSION[0]}.{API_VERSION[1]}'
# The versions this Konstruct runs, as a refusal names them: every minor of its major up to its own.
RUNS = OURS if not API_VERSION[1] else f'{API_VERSION[0]}.0 to {OURS}'
PACKS = '1.0'
"""The API the example's packs target: their plugin.toml says ``api_version = 1``."""
EXAMPLE = ROOT / 'examples' / 'plugin'
DIST = 'konstruct-yaml-pipelines'

PIPELINE = """\
pipeline: daily_revenue
owner: finance
steps:
  - name: load_orders
    sql: create table analytics.orders as select id, amount, status from raw.orders
  - name: revenue
    after: [load_orders]
    sql: |
      create table analytics.revenue as
      select sum(amount) as revenue from analytics.orders where status = 'paid'
  - name: tickets
    sql: |
      create table helpdesk.ticket as select id, status, requester_id, created_at from raw.tickets;
      create table helpdesk.user as select id, email, role from raw.users
"""

# Plugins that misbehave, each as an installed distribution's entry point would load it.
MISBEHAVING = """\
import json
from collections.abc import Callable

from konstruct.plugins import API_VERSION, Finding, Fragment, PluginEdge, PluginNode


class Raises:
    api_version = API_VERSION
    patterns = ('*.thing',)

    def read(self, files, context):
        raise ValueError('cannot read a thing')


class Invalid:
    api_version = API_VERSION
    patterns = ('*.thing',)

    def read(self, files, context):
        return Fragment(nodes=[PluginNode('a', 'column', 'a')], edges=[PluginEdge('a', 'b', 'feeds')])


class NotAFragment:
    api_version = API_VERSION
    patterns = ('*.thing',)

    def read(self, files, context):
        return {'nodes': []}


class Future:
    api_version = (API_VERSION[0] + 1, 0)
    patterns = ('*.thing',)

    def read(self, files, context):
        raise AssertionError('never run')


class Unversioned:
    patterns = ('*.thing',)


class Incomplete:
    api_version = API_VERSION


class Everywhere:
    api_version = API_VERSION
    patterns = ()

    def read(self, files, context):
        return Fragment(nodes=[PluginNode('x', 'exposure', str(context.options.get('label', 'x')))])


class Nodes:
    api_version = API_VERSION
    description = 'every node id, as JSON'

    def export(self, graph, request):
        return json.dumps(sorted(graph.nodes)) + '\\n'


class ExportRaises:
    api_version = API_VERSION
    description = 'fails'

    def export(self, graph, request):
        raise RuntimeError('no export today')


class ExportNotText:
    api_version = API_VERSION
    description = 'returns bytes'

    def export(self, graph, request):
        return b'bytes'


class CheckRaises:
    api_version = API_VERSION
    description = 'fails'

    def check(self, context):
        raise KeyError('missing')


class CheckNotFindings:
    api_version = API_VERSION
    description = 'returns text'

    def check(self, context):
        return ['a finding']


class ToolsRaise:
    api_version = API_VERSION

    def tools(self, project):
        raise RuntimeError('no tools')


class BadTools:
    api_version = API_VERSION

    def tools(self, project):
        return {'Bad Name': len, 'fine': 'not a function'}


RAISES = Raises()
INVALID = Invalid()
NOT_A_FRAGMENT = NotAFragment()
class Newer:
    api_version = (API_VERSION[0], API_VERSION[1] + 1)
    patterns = ('*.thing',)

    def read(self, files, context):
        raise AssertionError('never run')


class Whole:
    api_version = API_VERSION[0]
    patterns = ('*.thing',)

    def read(self, files, context):
        return Fragment(nodes=[PluginNode('whole', 'exposure', 'whole')])


class ToolsMixed:
    api_version = API_VERSION

    def tools(self, project):
        def fine() -> dict:
            'A tool the server can describe.'
            return {'ok': True}

        def undescribable(callback: Callable[[], int]) -> dict:
            'A parameter type with no JSON schema.'
            return {}

        return {'fine': fine, 'undescribable': undescribable}


FUTURE = Future()
NEWER = Newer()
WHOLE = Whole()
TOOLS_MIXED = ToolsMixed()
UNVERSIONED = Unversioned()
INCOMPLETE = Incomplete()
EVERYWHERE = Everywhere()
NODES = Nodes()
EXPORT_RAISES = ExportRaises()
EXPORT_NOT_TEXT = ExportNotText()
CHECK_RAISES = CheckRaises()
CHECK_NOT_FINDINGS = CheckNotFindings()
TOOLS_RAISE = ToolsRaise()
BAD_TOOLS = BadTools()
"""


def install(site: Path, name: str, version: str, entry_points: dict[str, dict[str, str]]) -> None:
    """Install a distribution's metadata in ``site``, as pip would in site-packages."""
    info = site / f'{normalise(name).replace("-", "_")}-{version}.dist-info'
    info.mkdir(parents=True)
    (info / 'METADATA').write_text(f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n')
    lines = []
    for group, entries in entry_points.items():
        lines += [f'[{group}]', *(f'{key} = {value}' for key, value in entries.items()), '']
    (info / 'entry_points.txt').write_text('\n'.join(lines))


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    site = tmp_path / 'site'
    site.mkdir()
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.delenv('KONSTRUCT_NO_PLUGINS', raising=False)
    yield site
    for module in [
        m for m in sys.modules if m.startswith(('konstruct_yaml_pipelines', 'misbehaving', 'hostile'))
    ]:
        del sys.modules[module]


@pytest.fixture
def example(site: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The example plugin, installed with the entry points its pyproject.toml declares."""
    project = tomllib.loads((EXAMPLE / 'pyproject.toml').read_text())['project']
    install(site, project['name'], project['version'], project['entry-points'])
    monkeypatch.syspath_prepend(str(EXAMPLE))
    return site


@pytest.fixture
def misbehaving(site: Path) -> Path:
    (site / 'misbehaving.py').write_text(MISBEHAVING)
    return site


def project(root: Path, *, enabled: list[str] | None = None, extra: str = '') -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / 'daily.pipeline.yml').write_text(PIPELINE)
    if enabled is not None:
        (root / 'konstruct.toml').write_text(f'[plugins]\nenabled = {json.dumps(enabled)}\n{extra}')
    return root


def build(root: Path, **options: Any) -> Graph:
    return Project(root, **options).build(warehouses=False, run_dbt=False).graph


# The example plugin


def test_the_example_plugin_reads_pipelines_into_the_graph(example: Path, tmp_path: Path) -> None:
    root = project(tmp_path / 'repo', enabled=[DIST])
    result = Project(root).build(warehouses=False, run_dbt=False)
    graph = result.graph
    dag = graph.nodes['pipelines.pipeline.daily_revenue']
    assert (dag.kind, dag.path, dag.attrs, dag.origins) == (
        'dag',
        'daily.pipeline.yml',
        {'owner': 'finance'},
        ['pipelines'],
    )
    load, revenue = 'pipelines.task.daily_revenue.load_orders', 'pipelines.task.daily_revenue.revenue'
    assert {(e.source, e.target, e.kind) for e in graph.edges if e.attrs.get('origin') == 'pipelines'} >= {
        (dag.id, load, 'runs'),
        (load, revenue, 'precedes'),
        (load, 'table.analytics.orders', 'feeds'),
        ('table.raw.orders', load, 'feeds'),
        ('table.analytics.orders', revenue, 'feeds'),
        (revenue, 'table.analytics.revenue', 'feeds'),
    }
    # Konstruct analysed the steps' SQL: column lineage through both steps, with the filter.
    trace = Project(root).trace('analytics.revenue.revenue')
    assert [c.id for c in trace.columns] == ['table.analytics.orders#amount', 'table.raw.orders#amount']
    edge = next(e for e in graph.out_edges('table.analytics.revenue#revenue', 'derived_from'))
    assert edge.attrs['via'] == revenue
    assert result.inputs['pipelines_pipelines'] == 1
    assert {
        'input': 'pipelines',
        'path': 'daily.pipeline.yml',
        'detail': 'daily_revenue: 3 steps',
    } in graph.meta['sources']
    assert 'daily.pipeline.yml' in graph.meta['watched']
    # The plugin's data-only pack binds to the schema the pipeline writes.
    assert 'term.support_ticket' in graph.nodes
    assert {'input': 'ontology pack', 'name': 'helpdesk', 'detail': 'helpdesk: 3 terms'} in graph.meta[
        'sources'
    ]
    assert not graph.warnings


def test_an_installed_plugin_runs_only_when_the_project_enables_it(example: Path, tmp_path: Path) -> None:
    root = project(tmp_path / 'repo')
    graph = build(root)
    assert not [n for n in graph.nodes if n.startswith('pipelines.')]
    assert 'term.support_ticket' not in graph.nodes
    [plugin, *_] = Project(root).plugins().installed()
    assert plugin.reason == f'not enabled: add "{DIST}" to [plugins] enabled'
    # Distribution names compare in their canonical form.
    project(root, enabled=['Konstruct_YAML.Pipelines'])
    assert 'pipelines.pipeline.daily_revenue' in build(root).nodes


def test_no_plugins_turns_every_plugin_off(
    example: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = project(tmp_path / 'repo', enabled=[DIST])
    assert 'pipelines.pipeline.daily_revenue' not in build(root, plugins=False).nodes
    assert main(['build', str(root), '--no-plugins', '--no-warehouse', '--json']) == 0
    assert not [s for s in json.loads(capsys.readouterr().out)['inputs'] if s['input'] == 'pipelines']
    assert main(['plugins', '-C', str(root), '--no-plugins']) == 0
    assert 'plugins are turned off (--no-plugins)' in capsys.readouterr().out
    monkeypatch.setenv('KONSTRUCT_NO_PLUGINS', '1')
    assert 'pipelines.pipeline.daily_revenue' not in build(root).nodes
    assert Project(root).plugins().installed()[0].reason == 'plugins are turned off (KONSTRUCT_NO_PLUGINS)'
    monkeypatch.setenv('KONSTRUCT_NO_PLUGINS', '0')
    assert 'pipelines.pipeline.daily_revenue' in build(root).nodes


def test_the_plugins_command_lists_each_plugin(
    example: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = project(tmp_path / 'repo', enabled=[DIST, 'konstruct-absent'])
    assert main(['plugins', '-C', str(root)]) == 0
    out = capsys.readouterr().out
    assert f'{DIST} 0.1.0: enabled' in out
    for group in ('inputs', 'checks', 'mcp_tools'):
        assert f'  {group:<10} {"pipelines":<24} API {OURS}' in out
    assert f'  {"packs":<10} {"pipelines":<24} API {PACKS}' in out
    assert 'konstruct-absent: enabled in konstruct.toml, but not installed' in out
    assert 'Built in: inputs looker; exports openlineage, mermaid, dot, csv, ossie;' in out
    assert main(['plugins', '-C', str(root), '--json']) == 0
    data = json.loads(capsys.readouterr().out)
    assert data['api_version'] == OURS
    mine = [p for p in data['plugins'] if p['distribution'] == DIST]
    assert {(p['group'], p['enabled'], p['api_version']) for p in mine} == {
        *((g, True, OURS) for g in ('inputs', 'checks', 'mcp_tools')),
        ('packs', True, PACKS),
    }
    assert data['problems'] == ["konstruct.toml enables 'konstruct-absent', which is not installed"]
    graph = build(root)
    assert "plugins: konstruct.toml enables 'konstruct-absent', which is not installed" in graph.warnings


def test_status_lists_plugins_without_loading_them(example: Path, tmp_path: Path) -> None:
    root = project(tmp_path / 'repo', enabled=[DIST, 'konstruct-absent'])
    status = inspect(root)
    assert 'konstruct_yaml_pipelines' not in sys.modules
    rows = [value for key, value in status.lines() if key == 'plugin']
    assert rows == [
        f'{DIST} 0.1.0 (inputs pipelines, checks pipelines, packs pipelines, mcp tools pipelines): enabled',
        'konstruct-absent: enabled in konstruct.toml, but not installed',
    ]
    assert status.to_dict()['plugins'][0]['api_version'] is None
    assert next(v for k, v in inspect(root, plugins=False).lines() if k == 'plugin').endswith(
        'plugins are turned off (--no-plugins)'
    )


def test_status_has_no_plugin_rows_when_none_are_installed(site: Path, tmp_path: Path) -> None:
    status = inspect(project(tmp_path / 'repo'))
    assert not [key for key, _ in status.lines() if key == 'plugin']
    assert status.to_dict()['plugins'] == []


def git(root: Path, *args: str) -> None:
    subprocess.run(
        [
            'git',
            '-C',
            str(root),
            '-c',
            'user.name=t',
            '-c',
            'user.email=t@example.com',
            '-c',
            'commit.gpgsign=false',
            *args,
        ],
        check=True,
        capture_output=True,
    )


def test_the_example_check_runs_in_the_gate(
    example: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = project(tmp_path / 'repo', enabled=[DIST])
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'add', '.')
    git(root, 'commit', '-q', '-m', 'Pipelines')
    assert Project(root).gate(base='HEAD', warehouses=False, run_dbt=False).passed
    (root / 'daily.pipeline.yml').write_text(PIPELINE.replace('owner: finance\n', ''))
    result = Project(root).gate(base='HEAD', warehouses=False, run_dbt=False)
    [finding] = result.findings
    assert (finding.rule, finding.message, finding.path) == (
        'pipelines.owner-required',
        "pipeline 'daily_revenue' names no owner",
        'daily.pipeline.yml',
    )
    assert finding.fix.startswith('Add an `owner:`')
    assert result.to_dict()['findings'][0]['fix'] == finding.fix
    assert Project(root, plugins=False).gate(base='HEAD', warehouses=False, run_dbt=False).passed
    assert main(['gate', '-C', str(root), '--base', 'HEAD', '--no-warehouse', '--no-dbt']) == 1
    assert 'pipelines.owner-required' in capsys.readouterr().out


def test_the_example_tools_are_served_over_mcp(example: Path, tmp_path: Path) -> None:
    root = project(tmp_path / 'repo', enabled=[DIST])
    build(root)

    async def exercise() -> tuple[list[str], Any, Any]:
        async with Client(build_server(root)) as client:
            tools = [t.name for t in (await client.list_tools()).tools]
            found = await client.call_tool('pipelines_owners', {'pipeline': 'daily_revenue'})
            async with Client(build_server(root, plugins=False)) as plain:
                others = [t.name for t in (await plain.list_tools()).tools]
            return tools, found, others

    tools, found, others = anyio.run(exercise)
    assert 'pipelines_owners' in tools and 'pipelines_owners' not in others
    assert found.structured_content == {
        'pipelines': [{'pipeline': 'daily_revenue', 'owner': 'finance', 'path': 'daily.pipeline.yml'}]
    }


def test_the_cli_finds_an_installed_plugin_in_a_fresh_process(example: Path, tmp_path: Path) -> None:
    root = project(tmp_path / 'repo', enabled=[DIST])
    env = {'PYTHONPATH': f'{example}:{EXAMPLE}', 'PATH': '/usr/bin:/bin'}
    command = [
        sys.executable,
        '-m',
        'konstruct.interfaces.cli',
        'build',
        str(root),
        '--no-warehouse',
        '--json',
    ]
    run = subprocess.run(command, capture_output=True, text=True, env=env, check=True)
    assert {
        'input': 'pipelines',
        'path': 'daily.pipeline.yml',
        'detail': 'daily_revenue: 3 steps',
    } in json.loads(run.stdout)['inputs']


# Isolation


def misbehaving_project(
    site: Path, tmp_path: Path, entries: dict[str, dict[str, str]], extra: str = ''
) -> Path:
    install(site, 'konstruct-misbehaving', '1.0', entries)
    root = project(tmp_path / 'repo', enabled=['konstruct-misbehaving'], extra=extra)
    (root / 'a.thing').write_text('thing')
    (root / 'orders.sql').write_text('create table analytics.orders as select id, amount from raw.orders')
    return root


def test_a_plugin_input_that_raises_is_reported_and_the_build_goes_on(
    misbehaving: Path, tmp_path: Path
) -> None:
    root = misbehaving_project(misbehaving, tmp_path, {'konstruct.inputs': {'things': 'misbehaving:RAISES'}})
    result = Project(root).build(warehouses=False, run_dbt=False)
    assert result.graph.warnings == [
        "plugins: input 'things' (konstruct-misbehaving 1.0) raised ValueError: cannot read a thing; "
        'the build goes on without it'
    ]
    assert {
        'input': 'things',
        'name': 'konstruct-misbehaving',
        'status': 'failed',
        'detail': 'raised ValueError: cannot read a thing',
    } in result.graph.meta['sources']
    assert 'a.thing' in result.graph.meta['watched']


@pytest.mark.parametrize(
    ('target', 'problem'),
    [
        (
            'misbehaving:INVALID',
            "returned a fragment that is not valid: node 'a': kind 'column' is not one of dag, exposure, task, test; "
            "edge 'a' -> 'b': 'b' is not a node of the fragment",
        ),
        ('misbehaving:NOT_A_FRAGMENT', 'returned a fragment that is not valid: read returned no Fragment'),
    ],
)
def test_an_invalid_fragment_merges_nothing(
    misbehaving: Path, tmp_path: Path, target: str, problem: str
) -> None:
    root = misbehaving_project(misbehaving, tmp_path, {'konstruct.inputs': {'things': target}})
    graph = build(root)
    assert not [n for n in graph.nodes if n.startswith('things.')]
    assert graph.warnings == [
        f"plugins: input 'things' (konstruct-misbehaving 1.0) {problem}; the build goes on without it"
    ]


@pytest.mark.parametrize(
    ('target', 'refusal'),
    [
        (
            'misbehaving:FUTURE',
            f'it targets plugin API {API_VERSION[0] + 1}.0; this Konstruct runs API {RUNS}',
        ),
        (
            'misbehaving:NEWER',
            f'it targets plugin API {API_VERSION[0]}.{API_VERSION[1] + 1}; this Konstruct runs API {RUNS}',
        ),
        ('misbehaving:UNVERSIONED', 'it declares no api_version'),
        ('misbehaving:INCOMPLETE', 'it has no patterns, read'),
        ('misbehaving:Raises', 'misbehaving:Raises is a class; the entry point names an instance'),
        (
            'misbehaving:MISSING',
            "cannot be loaded: AttributeError: module 'misbehaving' has no attribute 'MISSING'",
        ),
        ('not_a_module:THING', "cannot be loaded: ModuleNotFoundError: No module named 'not_a_module'"),
    ],
)
def test_a_plugin_that_cannot_run_is_refused_with_the_reason(
    misbehaving: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], target: str, refusal: str
) -> None:
    root = misbehaving_project(misbehaving, tmp_path, {'konstruct.inputs': {'things': target}})
    graph = build(root)
    assert graph.warnings == [f"plugins: input 'things' (konstruct-misbehaving 1.0) is refused: {refusal}"]
    assert main(['plugins', '-C', str(root)]) == 0
    assert f'refused: {refusal}' in capsys.readouterr().out


def test_names_that_clash_are_refused(misbehaving: Path, tmp_path: Path) -> None:
    install(misbehaving, 'konstruct-other', '2.0', {'konstruct.inputs': {'things': 'misbehaving:EVERYWHERE'}})
    root = misbehaving_project(
        misbehaving,
        tmp_path,
        {
            'konstruct.inputs': {
                'looker': 'misbehaving:EVERYWHERE',
                'table': 'misbehaving:EVERYWHERE',
                'things': 'misbehaving:EVERYWHERE',
                'Bad': 'x:Y',
            },
            'konstruct.exports': {'csv': 'misbehaving:NODES'},
        },
    )
    reasons = {(p.group, p.name, p.distribution): p.reason for p in Project(root).plugins().installed()}
    assert reasons == {
        (
            'inputs',
            'Bad',
            'konstruct-misbehaving',
        ): "refused: the name 'Bad' must be lower case letters, digits, - and _",
        ('inputs', 'looker', 'konstruct-misbehaving'): "refused: Konstruct has its own input named 'looker'",
        ('inputs', 'table', 'konstruct-misbehaving'): "refused: Konstruct reserves the input name 'table'",
        (
            'inputs',
            'things',
            'konstruct-misbehaving',
        ): 'refused: konstruct-misbehaving and konstruct-other both provide it',
        (
            'inputs',
            'things',
            'konstruct-other',
        ): 'refused: konstruct-misbehaving and konstruct-other both provide it',
        ('exports', 'csv', 'konstruct-misbehaving'): "refused: Konstruct has its own export named 'csv'",
    }
    graph = build(root)
    assert len(graph.warnings) == 5 and not [n for n in graph.nodes if n.startswith('things.')]


def test_an_input_with_no_patterns_reads_its_own_settings(misbehaving: Path, tmp_path: Path) -> None:
    root = misbehaving_project(
        misbehaving,
        tmp_path,
        {'konstruct.inputs': {'things': 'misbehaving:EVERYWHERE'}},
        extra='[things]\nlabel = "board"\n',
    )
    node = build(root).nodes['things.x']
    assert (node.kind, node.name, node.origins) == ('exposure', 'board', ['things'])


def test_export_formats_from_plugins(
    misbehaving: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = misbehaving_project(
        misbehaving,
        tmp_path,
        {
            'konstruct.exports': {
                'nodes': 'misbehaving:NODES',
                'fails': 'misbehaving:EXPORT_RAISES',
                'bytes': 'misbehaving:EXPORT_NOT_TEXT',
            }
        },
    )
    build(root)
    assert json.loads(Project(root).export('nodes'))[0] == 'table.analytics.orders'
    assert main(['export', 'nodes', '-C', str(root)]) == 0
    assert capsys.readouterr().out.startswith('["table.analytics.orders"')
    with pytest.raises(
        ExportError,
        match=r"export 'fails' \(konstruct-misbehaving 1.0\) raised RuntimeError: no export today",
    ):
        Project(root).export('fails')
    with pytest.raises(ExportError, match='returned bytes, not text'):
        Project(root).export('bytes')
    with pytest.raises(
        ExportError,
        match="export format 'nodes' comes from konstruct-misbehaving, which plugins are turned off",
    ):
        Project(root, plugins=False).export('nodes')
    assert main(['export', 'fails', '-C', str(root)]) == 1
    assert 'raised RuntimeError: no export today' in capsys.readouterr().err
    with pytest.raises(
        ExportError,
        match="unknown export format 'nope'; choose from openlineage, mermaid, dot, csv, ossie, bytes, fails, nodes",
    ):
        Project(root).export('nope')
    # A format no installed distribution provides is a usage error, as before.
    with pytest.raises(SystemExit) as exit_:
        main(['export', 'nope', '-C', str(root)])
    assert exit_.value.code == 2
    assert export(Graph(), 'csv').startswith('relation,column,')


def test_a_gate_check_that_fails_fails_the_gate(misbehaving: Path, tmp_path: Path) -> None:
    root = misbehaving_project(
        misbehaving,
        tmp_path,
        {
            'konstruct.checks': {
                'raises': 'misbehaving:CHECK_RAISES',
                'text': 'misbehaving:CHECK_NOT_FINDINGS',
                'future': 'misbehaving:FUTURE',
            }
        },
    )
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'add', '.')
    git(root, 'commit', '-q', '-m', 'Start')
    result = Project(root).gate(base='HEAD', warehouses=False, run_dbt=False)
    assert [(f.rule, f.message) for f in result.findings] == [
        ('raises.failed', "the gate check from konstruct-misbehaving 1.0 raised KeyError: 'missing'"),
        (
            'text.failed',
            'the gate check from konstruct-misbehaving 1.0 returned something other than findings, '
            'each with a rule, message and fix',
        ),
        (
            'future.failed',
            'the gate check from konstruct-misbehaving 1.0 cannot run: '
            f'it targets plugin API {API_VERSION[0] + 1}.0; this Konstruct runs API {RUNS}',
        ),
    ]
    assert all(f.fix.startswith('Fix or upgrade the plugin') for f in result.findings)


def test_tools_that_fail_are_left_out(
    misbehaving: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = misbehaving_project(
        misbehaving,
        tmp_path,
        {'konstruct.mcp_tools': {'raises': 'misbehaving:TOOLS_RAISE', 'bad': 'misbehaving:BAD_TOOLS'}},
    )
    assert Project(root).plugins().tools(Project(root)) == []
    assert "tools 'raises' (konstruct-misbehaving 1.0) raised RuntimeError: no tools" in caplog.text
    assert "tool 'Bad Name' is left out" in caplog.text and "tool 'fine' is left out" in caplog.text


# Data-only packs


def pack_plugin(
    site: Path, *, manifest: str | None = 'api_version = 1\n', pack: str = 'name: helpdesk\n'
) -> None:
    package = site / 'packs_only'
    (package / 'packs').mkdir(parents=True)
    # Finding a pack directory runs nothing of the package that holds it.
    (package / '__init__.py').write_text("raise RuntimeError('a data-only plugin was imported')\n")
    if manifest is not None:
        (package / 'packs' / 'plugin.toml').write_text(manifest)
    (package / 'packs' / 'helpdesk.yml').write_text(pack)
    install(site, 'konstruct-packs-only', '1.0', {'konstruct.packs': {'helpdesk': 'packs_only.packs'}})


def test_a_data_only_pack_is_read_without_running_code(site: Path, tmp_path: Path) -> None:
    from konstruct.semantics.registry import load_packs

    pack_plugin(site, pack=(EXAMPLE / 'konstruct_yaml_pipelines' / 'packs' / 'helpdesk.yml').read_text())
    root = project(tmp_path / 'repo', enabled=['konstruct-packs-only'])
    packs = load_packs(plugins=Project(root).plugins())
    assert [(p.name, p.source) for p in packs if p.source != 'konstruct'] == [
        ('helpdesk', 'konstruct-packs-only')
    ]
    assert 'packs_only' not in sys.modules
    # Configuration can choose a plugin's pack by name, as it does Konstruct's own.
    (root / 'konstruct.toml').write_text(
        '[plugins]\nenabled = ["konstruct-packs-only"]\n[ontologies]\npacks = ["helpdesk"]\n'
    )
    assert Project(root).config().ontology_packs == ['helpdesk']


@pytest.mark.parametrize(
    ('options', 'warning'),
    [
        ({'manifest': None}, 'cannot be loaded: PluginError: '),
        (
            {'manifest': 'api_version = 7\n'},
            f'it targets plugin API 7.0; this Konstruct runs API {RUNS}',
        ),
        ({'pack': 'name: stripe\n'}, "pack 'stripe' is already provided by konstruct"),
        ({'pack': 'name: [unclosed\n'}, 'cannot read helpdesk.yml'),
    ],
)
def test_a_pack_plugin_that_cannot_be_used_is_reported(
    site: Path, tmp_path: Path, options: dict[str, Any], warning: str
) -> None:
    pack_plugin(site, **options)
    graph = build(project(tmp_path / 'repo', enabled=['konstruct-packs-only']))
    assert [
        w
        for w in graph.warnings
        if warning in w and w.startswith("plugins: pack 'helpdesk' (konstruct-packs-only 1.0)")
    ]


def test_a_pack_entry_point_must_name_a_directory(site: Path, tmp_path: Path) -> None:
    install(
        site,
        'konstruct-packs-only',
        '1.0',
        {'konstruct.packs': {'a': 'packs_only:PACKS', 'b': 'nowhere.packs'}},
    )
    plugins = Project(project(tmp_path / 'repo', enabled=['konstruct-packs-only'])).plugins()
    plugins.group('packs')
    assert [p.reason for p in plugins.installed()] == [
        'refused: cannot be loaded: PluginError: packs_only:PACKS names an object; a pack names a package directory',
        'refused: cannot be loaded: PluginError: no directory nowhere.packs',
    ]


# Fragments


def test_fragment_validation_names_each_problem() -> None:
    fragment = Fragment(
        nodes=[
            PluginNode('a', 'task', 'a', attrs={'when': object()}),
            PluginNode('a', 'task', ''),
            PluginNode('b#c', 'model', 'b', path=3),  # type: ignore[arg-type]
            'node',  # type: ignore[list-item]
        ],
        edges=[
            PluginEdge('a', Relation(''), 'contains'),
            PluginEdge(Relation('x.y'), 'a', 'derived_from', attrs={'origin': 'me'}),
            PluginEdge(Column('x.y', ''), 3, 'feeds'),  # type: ignore[arg-type]
            'edge',  # type: ignore[list-item]
            # Each of these used to pass validation and then fail the merge or the write.
            PluginEdge('a', Relation('x.y'), 'feeds', attrs={'source': 1, 'target': 2, 'kind': 3}),
            PluginEdge(
                'a', Relation('x.y'), 'feeds', attrs={'verified': True, 'transform': 'x', 'expression': 'y'}
            ),
            PluginEdge('a', Relation('x.y'), 'feeds', attrs={'rows': 2**65}),
            PluginEdge('a', Relation('x.y'), 'feeds', attrs={1: 'one'}),  # type: ignore[dict-item]
            PluginEdge(Column('.', 'id'), 'a', 'feeds'),
        ],
        sql=[
            Sql('select 1', 'f.sql', task='missing'),
            'select 2',  # type: ignore[list-item]
            Sql('select 3', 'g.sql', dialect='no-such-dialect'),
        ],
        warnings=[1],  # type: ignore[list-item]
        sources=['f', Source(3, 'd')],  # type: ignore[list-item, arg-type]
        counts={'n': 'one'},  # type: ignore[dict-item]
    )
    problems = fragment.problems()
    assert problems[0] == "node 'a': attrs hold an object, which is not JSON data"
    assert problems[1:] == [
        "node id 'a' is used twice",
        "node 'a': name must be a non-empty string",
        "node id 'b#c' must be a non-empty string without # or a line break",
        "node 'b#c': kind 'model' is not one of dag, exposure, task, test",
        "node 'b#c': path must be a string",
        "'node' is not a PluginNode",
        "edge 'a' -> Relation(key=''): kind 'contains' is not one of derived_from, feeds, precedes, runs, tests",
        "edge 'a' -> Relation(key=''): a Relation needs a key",
        "edge Relation(key='x.y') -> 'a': derived_from joins two columns",
        "edge Relation(key='x.y') -> 'a': attrs may not set origin",
        "edge Column(relation='x.y', name='') -> 3: a Column needs a relation and a name",
        "edge Column(relation='x.y', name='') -> 3: 3 is not a node id, Relation or Column",
        "'edge' is not a PluginEdge",
        "edge 'a' -> Relation(key='x.y'): attrs may not set kind, source, target",
        "edge 'a' -> Relation(key='x.y'): attrs may not set expression, transform, verified",
        "edge 'a' -> Relation(key='x.y') is not JSON-ready data (Integer exceeds 64-bit range)",
        "edge 'a' -> Relation(key='x.y'): attrs must be a dict with string keys",
        "edge Column(relation='.', name='id') -> 'a': a Column needs a relation and a name",
        "sql in 'f.sql': task 'missing' is not a node of the fragment",
        "'select 2' is not Sql with sql and path strings",
        "sql in 'g.sql': 'no-such-dialect' is not a SQL dialect Konstruct knows",
        'warnings must be a list of strings',
        'sources must be Source objects with path and detail strings',
        'counts must map names of lower-case letters, digits and _ to 64-bit integers',
    ]
    valid = Fragment(
        nodes=[PluginNode('a', 'task', 'a')],
        edges=[PluginEdge('a', Relation('x.y'), 'feeds', attrs={'rows': 2**63 - 1})],
        sql=[Sql('select 1', 'f.sql', task='a', dialect='duckdb')],
        sources=[Source('f', 'd')],
    )
    assert valid.problems() == []


def test_column_ends_follow_what_the_graph_knows(misbehaving: Path, tmp_path: Path) -> None:
    """A Column end the graph has resolves to it; one of a relation whose columns are known is
    left out with a warning; one of a relation known only by name is created."""
    (misbehaving / 'columns.py').write_text(
        textwrap.dedent(
            """\
            from konstruct.plugins import API_VERSION, Column, Fragment, PluginEdge, PluginNode

            class Board:
                api_version = API_VERSION
                patterns = ('*.thing',)

                def read(self, files, context):
                    ends = [Column('analytics.orders', 'amount'), Column('analytics.orders', 'nope'), Column('raw.events', 'kind')]
                    return Fragment(
                        nodes=[PluginNode('board', 'exposure', 'board', path='a.thing')],
                        edges=[PluginEdge(end, 'board', 'feeds') for end in ends]
                        + [PluginEdge(Column('analytics.orders', 'gone'), 'board', 'feeds', missing='custom warning')],
                    )

            BOARD = Board()
            """
        )
    )
    root = misbehaving_project(misbehaving, tmp_path, {'konstruct.inputs': {'boards': 'columns:BOARD'}})
    graph = build(root)
    sources = {e.source for e in graph.in_edges('boards.board')}
    assert sources == {'table.analytics.orders#amount', 'table.raw.events#kind'}
    assert graph.warnings == [
        "boards: a.thing: 'board' reads analytics.orders.nope, which is not a known column",
        'boards: custom warning',
    ]
    del sys.modules['columns']


def test_commands_that_use_no_plugins_do_not_look_for_them(site: Path, tmp_path: Path) -> None:
    """Building the parser and answering a query reads no installed distribution's entry points
    and loads none of the registry's components."""
    script = textwrap.dedent(
        """\
        import importlib.metadata, sys
        calls = []
        real = importlib.metadata.entry_points
        importlib.metadata.entry_points = lambda *a, **k: calls.append(1) or real(*a, **k)
        from konstruct.interfaces.cli import main
        from konstruct.plugins.registry import Plugins
        Plugins.builtin()
        main(['list', '-C', sys.argv[1], '--no-build'])
        loaded = [m for m in ('konstruct.inputs.looker', 'konstruct.analysis.gate') if m in sys.modules]
        print(len(calls), loaded)
        """
    )
    build(project(tmp_path / 'repo'))
    run = subprocess.run(
        [sys.executable, '-c', script, str(tmp_path / 'repo')], capture_output=True, text=True, check=False
    )
    assert run.stdout.splitlines()[-1] == '0 []', run.stderr


def test_the_mcp_server_starts_when_the_configuration_cannot_be_read(
    site: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = project(tmp_path / 'repo')
    (root / 'konstruct.toml').write_text('[plugins]\nenabled = "konstruct-yaml-pipelines"\n')

    async def names() -> list[str]:
        async with Client(build_server(root)) as client:
            return [t.name for t in (await client.list_tools()).tools]

    assert 'impact' in anyio.run(names)
    assert 'no plugin tools: [plugins] enabled is a list of distribution names' in caplog.text


def test_an_api_version_runs_with_the_same_major_and_no_newer_minor(
    misbehaving: Path, tmp_path: Path
) -> None:
    from konstruct.plugins.api import api_version, supported

    root = misbehaving_project(misbehaving, tmp_path, {'konstruct.inputs': {'whole': 'misbehaving:WHOLE'}})
    assert 'whole.whole' in build(root).nodes
    assert [p.api_version for p in Project(root).plugins().load_all().installed()] == [(API_VERSION[0], 0)]
    assert api_version(1) == (1, 0) and api_version('1.2') == (1, 2) and api_version([1, 3]) == (1, 3)
    assert api_version(True) is None and api_version('one') is None and api_version((1,)) is None
    assert supported(API_VERSION) and supported((API_VERSION[0], 0))
    assert not supported((API_VERSION[0], API_VERSION[1] + 1)) and not supported((API_VERSION[0] + 1, 0))


def test_one_tool_the_server_cannot_describe_leaves_the_rest_served(
    misbehaving: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = misbehaving_project(
        misbehaving, tmp_path, {'konstruct.mcp_tools': {'mixed': 'misbehaving:TOOLS_MIXED'}}
    )

    async def names() -> list[str]:
        async with Client(build_server(root)) as client:
            return [t.name for t in (await client.list_tools()).tools]

    tools = anyio.run(names)
    assert 'impact' in tools and 'mixed_fine' in tools and 'mixed_undescribable' not in tools
    assert "tools 'mixed' (konstruct-misbehaving 1.0): tool 'mixed_undescribable' is left out" in caplog.text


def test_a_change_cannot_turn_off_the_checks_that_judge_it(example: Path, tmp_path: Path) -> None:
    """The gate runs the checks the base enables as well as the head's: taking the plugin out of
    konstruct.toml in the same change still runs its check on that change."""
    root = project(tmp_path / 'repo', enabled=[DIST])
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'add', '.')
    git(root, 'commit', '-q', '-m', 'Pipelines')
    (root / 'daily.pipeline.yml').write_text(PIPELINE.replace('owner: finance\n', ''))
    (root / 'konstruct.toml').write_text('[plugins]\nenabled = []\n')
    result = Project(root).gate(base='HEAD', warehouses=False, run_dbt=False)
    assert [f.rule for f in result.findings] == ['pipelines.owner-required']
    # The operator's switch still turns every plugin off.
    assert Project(root, plugins=False).gate(base='HEAD', warehouses=False, run_dbt=False).passed
    # With pyproject.toml holding the configuration at the base, as load_config reads it.
    (root / 'konstruct.toml').unlink()
    (root / 'daily.pipeline.yml').write_text(PIPELINE)
    (root / 'pyproject.toml').write_text(f'[tool.konstruct.plugins]\nenabled = ["{DIST}"]\n')
    git(root, 'add', '-A')
    git(root, 'commit', '-q', '-m', 'Configuration in pyproject')
    (root / 'pyproject.toml').write_text('[project]\nname = "x"\n')
    (root / 'daily.pipeline.yml').write_text(PIPELINE.replace('owner: finance\n', ''))
    assert [f.rule for f in Project(root).gate(base='HEAD', warehouses=False, run_dbt=False).findings] == [
        'pipelines.owner-required'
    ]


def test_plugins_enabled_at_a_base_that_cannot_be_read_are_none(tmp_path: Path) -> None:
    from konstruct.analysis.gate import plugins_at

    assert plugins_at(tmp_path, 'HEAD') == []
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'konstruct.toml').write_text('[plugins\n')
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'add', '.')
    git(root, 'commit', '-q', '-m', 'Broken')
    assert plugins_at(root, 'HEAD') == []
    (root / 'konstruct.toml').unlink()
    git(root, 'add', '-A')
    git(root, 'commit', '-q', '-m', 'None')
    assert plugins_at(root, 'HEAD') == []


@pytest.mark.parametrize('switch', ['flag', 'environment'])
def test_turning_plugins_off_leaves_a_configuration_that_names_their_pack_valid(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, switch: str
) -> None:
    pack_plugin(site, pack=(EXAMPLE / 'konstruct_yaml_pipelines' / 'packs' / 'helpdesk.yml').read_text())
    root = project(
        tmp_path / 'repo', enabled=['konstruct-packs-only'], extra='[ontologies]\npacks = ["helpdesk"]\n'
    )
    if switch == 'environment':
        monkeypatch.setenv('KONSTRUCT_NO_PLUGINS', '1')
    graph = build(root, plugins=switch != 'flag')
    reason = '--no-plugins' if switch == 'flag' else 'KONSTRUCT_NO_PLUGINS'
    assert f"ontologies: pack 'helpdesk' is not bound: plugins are turned off ({reason})" in graph.warnings


# Hostile plugins: each fragment, cached value and tool listing a plugin could hand Konstruct by
# mistake is refused with a warning or merged harmlessly; none fails the build or a later command.

HOSTILE = """\
from konstruct.plugins import API_VERSION

READ = None
TOOLS = {}


class Hostile:
    api_version = API_VERSION
    patterns = ('*.thing',)

    def read(self, files, context):
        return READ(context)


class Tools:
    api_version = API_VERSION

    def __init__(self, name):
        self.name = name

    def tools(self, project):
        return TOOLS[self.name]


def __getattr__(name):
    if name.startswith('TOOLS_'):
        return Tools(name[len('TOOLS_') :].lower())
    raise AttributeError(name)


INPUT = Hostile()
"""

HOSTILE_LABEL = "input 'hostile' (konstruct-hostile 1.0)"


def deep(levels: int) -> Any:
    value: Any = 1
    for _ in range(levels):
        value = {'a': value}
    return value


def task(**options: Any) -> PluginNode:
    return PluginNode(options.pop('id', 't'), 'task', options.pop('name', 't'), **options)


@pytest.fixture
def hostile(site: Path, tmp_path: Path) -> Iterator[Any]:
    """The hostile module, installed as ``konstruct-hostile`` with one input, and a repository
    that enables it: set ``READ`` on the module to what ``read`` returns."""
    (site / 'hostile.py').write_text(HOSTILE)
    install(site, 'konstruct-hostile', '1.0', {'konstruct.inputs': {'hostile': 'hostile:INPUT'}})
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'konstruct.toml').write_text('[plugins]\nenabled = ["konstruct-hostile"]\n')
    (root / 'etl.sql').write_text('create table analytics.orders as select id, amount from raw.orders;\n')
    (root / 'x.thing').write_text('x')
    module: Any = importlib.import_module('hostile')

    module.ROOT = root
    yield module


def build_and_query(root: Path, capsys: pytest.CaptureFixture[str], *build: str) -> Graph:
    """``konstruct build`` and the commands the review ran after it, each of which must succeed,
    and the graph they read, loaded from graph.json."""
    assert main(['build', str(root), '-q', '--no-warehouse', '--no-dbt', *build]) == 0
    for command in (
        ['impact', 'analytics.orders', '--json'],
        ['trace', 'analytics.orders.amount', '--json'],
        ['export', 'mermaid'],
        ['ontology', '--json'],
    ):
        assert main([*command, '-C', str(root)]) == 0, command
    capsys.readouterr()
    return Graph.from_dict(json.loads((root / 'konstruct-out' / 'graph.json').read_text()))


E = PluginEdge
ORDERS = Relation('raw.orders')


class Kind(enum.StrEnum):
    TASK = 'task'


class Text(str):
    """A str subclass, which a plugin's text fields may not be: it can print and compare as
    something else."""


REFUSED: dict[str, tuple[Any, str]] = {
    'attr_source': (
        lambda: Fragment(nodes=[task()], edges=[E('t', ORDERS, 'feeds', {'source': 'x'})]),
        "edge 't' -> Relation(key='raw.orders'): attrs may not set source",
    ),
    'attr_kind': (
        lambda: Fragment(nodes=[task()], edges=[E('t', ORDERS, 'feeds', {'kind': 'x'})]),
        "edge 't' -> Relation(key='raw.orders'): attrs may not set kind",
    ),
    'set': (
        lambda: Fragment(nodes=[task(attrs={'s': {1, 2}})]),
        "node 't': attrs hold a set, which is not JSON data",
    ),
    'bigint': (
        lambda: Fragment(nodes=[task(attrs={'s': 2**70})]),
        "node 't' is not JSON-ready data (Integer exceeds 64-bit range)",
    ),
    'object': (
        lambda: Fragment(nodes=[task(attrs={'s': object()})]),
        "node 't': attrs hold an object, which is not JSON data",
    ),
    'source_int': (
        lambda: Fragment(nodes=[task()], sources=[Source(1, 2)]),  # type: ignore[arg-type]
        'sources must be Source objects with path and detail strings',
    ),
    'dialect': (
        lambda: Fragment(nodes=[task()], sql=[Sql('select 1', 'a.sql', 't', 'nosuch')]),
        "sql in 'a.sql': 'nosuch' is not a SQL dialect Konstruct knows",
    ),
    'dot_column': (
        lambda: Fragment(nodes=[task()], edges=[E(Column('...', 'x'), 't', 'feeds')]),
        "edge Column(relation='...', name='x') -> 't': a Column needs a relation and a name",
    ),
    'dot_relation': (
        lambda: Fragment(nodes=[task()], edges=[E(Relation('..'), 't', 'feeds')]),
        "edge Relation(key='..') -> 't': a Relation needs a key",
    ),
    'nan': (
        lambda: Fragment(nodes=[task(attrs={'n': float('nan'), 'i': float('inf')})]),
        "node 't': attrs hold nan, which JSON has no number for",
    ),
    'deep_node': (
        lambda: Fragment(nodes=[task(attrs={'d': deep(252)})]),
        "node 't': attrs nest more than 64 levels deep",
    ),
    'deep_edge': (
        lambda: Fragment(nodes=[task()], edges=[E('t', ORDERS, 'feeds', {'d': deep(252)})]),
        "edge 't' -> Relation(key='raw.orders'): attrs nest more than 64 levels deep",
    ),
    'surrogate_name': (
        lambda: Fragment(nodes=[task(name='bad\udc80')]),
        "node 't' is not JSON-ready data (str is not valid UTF-8: surrogates not allowed)",
    ),
    'surrogate_path': (
        lambda: Fragment(nodes=[task(path='a\ud800.yml')]),
        "node 't' is not JSON-ready data (str is not valid UTF-8: surrogates not allowed)",
    ),
    'surrogate_description': (
        lambda: Fragment(nodes=[task(description='\udfff')]),
        "node 't' is not JSON-ready data (str is not valid UTF-8: surrogates not allowed)",
    ),
    'surrogate_warning': (
        lambda: Fragment(nodes=[task()], warnings=['w\udc80']),
        "warning 'w\\udc80' is not JSON-ready data (str is not valid UTF-8: surrogates not allowed)",
    ),
    'surrogate_source': (
        lambda: Fragment(nodes=[task()], sources=[Source('p\udc80', 'd')]),
        "source 'p\\udc80' is not JSON-ready data (str is not valid UTF-8: surrogates not allowed)",
    ),
    'surrogate_relation': (
        lambda: Fragment(nodes=[task()], edges=[E(Relation('raw.or\udc80'), 't', 'feeds')]),
        "edge Relation(key='raw.or\\udc80') -> 't' is not JSON-ready data "
        '(str is not valid UTF-8: surrogates not allowed)',
    ),
    'surrogate_column': (
        lambda: Fragment(nodes=[task()], edges=[E(Column('raw.orders', 'c\udc80'), 't', 'feeds')]),
        "edge Column(relation='raw.orders', name='c\\udc80') -> 't' is not JSON-ready data "
        '(str is not valid UTF-8: surrogates not allowed)',
    ),
    'surrogate_sql': (
        lambda: Fragment(nodes=[task()], sql=[Sql("select '\udc80' as x", 'a.sql', 't')]),
        "sql in 'a.sql' is not JSON-ready data (str is not valid UTF-8: surrogates not allowed)",
    ),
    'surrogate_id': (
        lambda: Fragment(nodes=[task(id='t\udc80')]),
        "node 't\\udc80' is not JSON-ready data (str is not valid UTF-8: surrogates not allowed)",
    ),
    'surrogate_key': (
        lambda: Fragment(nodes=[task(attrs={'k\udc80': 1})]),
        "node 't' is not JSON-ready data (str is not valid UTF-8: surrogates not allowed)",
    ),
    'surrogate_count': (
        lambda: Fragment(nodes=[task()], counts={'k\udc80': 1}),
        'counts must map names of lower-case letters, digits and _ to 64-bit integers',
    ),
    'surrogate_missing': (
        lambda: Fragment(
            nodes=[task()], edges=[E(Column('raw.orders', 'id'), 't', 'feeds', missing='\udc80')]
        ),
        "edge Column(relation='raw.orders', name='id') -> 't' is not JSON-ready data "
        '(str is not valid UTF-8: surrogates not allowed)',
    ),
    'big_count': (
        lambda: Fragment(nodes=[task()], counts={'things': 2**70}),
        'counts must map names of lower-case letters, digits and _ to 64-bit integers',
    ),
    'bool_count': (
        lambda: Fragment(nodes=[task()], counts={'things': True}),
        'counts must map names of lower-case letters, digits and _ to 64-bit integers',
    ),
    'quoted_relation': (
        lambda: Fragment(nodes=[task()], edges=[E(Relation('"."'), 't', 'feeds')]),
        "edge Relation(key='\".\"') -> 't': a Relation needs a key",
    ),
    'placeholder_relation': (
        lambda: Fragment(nodes=[task()], edges=[E(Relation('__konstruct__'), 't', 'feeds')]),
        "edge Relation(key='__konstruct__') -> 't': a Relation needs a key",
    ),
    'hash_relation': (
        lambda: Fragment(nodes=[task()], edges=[E(Relation('analytics.orders#id'), 't', 'feeds')]),
        "edge Relation(key='analytics.orders#id') -> 't': a Relation key may not contain #",
    ),
    'hash_column': (
        lambda: Fragment(nodes=[task()], edges=[E(Column('raw.newtab', 'a#b'), 't', 'feeds')]),
        "edge Column(relation='raw.newtab', name='a#b') -> 't': a Column's relation and name may not "
        'contain #',
    ),
    'hash_column_relation': (
        lambda: Fragment(nodes=[task()], edges=[E(Column('raw.new#tab', 'a'), 't', 'feeds')]),
        "edge Column(relation='raw.new#tab', name='a') -> 't': a Column's relation and name may not "
        'contain #',
    ),
    'line_break_relation': (
        lambda: Fragment(nodes=[task()], edges=[E(Relation('raw.or\nders'), 't', 'feeds')]),
        "edge Relation(key='raw.or\\nders') -> 't': a Relation key may not contain a line break",
    ),
    'line_break_column': (
        lambda: Fragment(nodes=[task()], edges=[E(Column('raw.orders', 'a\r\nb'), 't', 'feeds')]),
        "edge Column(relation='raw.orders', name='a\\r\\nb') -> 't': a Column's relation and name may "
        'not contain a line break',
    ),
    'sql_in_a_dag': (
        lambda: Fragment(nodes=[PluginNode('d', 'dag', 'd')], sql=[Sql('select 1', 'a.sql', 'd')]),
        "sql in 'a.sql': task 'd' is a dag, not a task",
    ),
    'str_enum_kind': (
        lambda: Fragment(nodes=[PluginNode('t', Kind.TASK, 't')]),
        "node 't': kind <Kind.TASK: 'task'> is not one of dag, exposure, task, test",
    ),
    'line_break_node_path': (
        lambda: Fragment(nodes=[task(path='a\n# b.yml')]),
        "node 't': path may not contain a line break",
    ),
    'line_break_node_id': (
        lambda: Fragment(nodes=[task(id='t\nu')]),
        "node id 't\\nu' must be a non-empty string without # or a line break",
    ),
    'line_break_sql_path': (
        lambda: Fragment(nodes=[task()], sql=[Sql('select 1', 'a\n# b.sql', 't')]),
        "sql in 'a\\n# b.sql': path may not contain a line break",
    ),
    'line_break_count': (
        lambda: Fragment(nodes=[task()], counts={'a\n# b': 1}),
        'counts must map names of lower-case letters, digits and _ to 64-bit integers',
    ),
    'upper_case_count': (
        lambda: Fragment(nodes=[task()], counts={'Views': 1}),
        'counts must map names of lower-case letters, digits and _ to 64-bit integers',
    ),
    'also_via': (
        lambda: Fragment(
            nodes=[task()], edges=[E(Relation('analytics.orders'), 't', 'feeds', {'also_via': 5})]
        ),
        "edge Relation(key='analytics.orders') -> 't': attrs may not set also_via",
    ),
    'via': (
        lambda: Fragment(
            nodes=[task()],
            edges=[
                E(
                    Column('analytics.orders', 'id'),
                    Column('analytics.orders', 'amount'),
                    'derived_from',
                    {'via': ['x']},
                )
            ],
        ),
        "edge Column(relation='analytics.orders', name='id') -> Column(relation='analytics.orders', "
        "name='amount'): attrs may not set via",
    ),
    'edge_role_loaded_command': (
        lambda: Fragment(
            nodes=[PluginNode('t', 'test', 't', attrs={'test': 'relationships'})],
            edges=[
                E('t', Relation('analytics.orders'), 'tests'),
                E('t', ORDERS, 'tests', {'role': 'relationship target', 'loaded': True, 'command': 'x'}),
            ],
        ),
        "edge 't' -> Relation(key='raw.orders'): attrs may not set command, loaded, role",
    ),
    'sql_path': (
        lambda: Fragment(
            nodes=[task(attrs={'sql_path': 7, 'dag': {'x': 1}})],
            edges=[E('t', Relation('analytics.orders'), 'feeds')],
        ),
        "node 't': attrs may not set dag, sql_path",
    ),
    'usage': (
        lambda: Fragment(nodes=[task(attrs={'usage': 'x', 'loader': 1, 'conditions': 5})]),
        "node 't': attrs may not set conditions, loader, usage",
    ),
    'node_attrs_konstruct_reads': (
        lambda: Fragment(
            nodes=[
                task(
                    attrs={
                        'expression': 'x',
                        'inferred': True,
                        'in_warehouse': True,
                        'loaded_by': 'x',
                        'nested': True,
                        'documented_only': True,
                    }
                )
            ]
        ),
        "node 't': attrs may not set documented_only, expression, in_warehouse, inferred, loaded_by, nested",
    ),
    'sql_task_empty': (
        lambda: Fragment(nodes=[task()], sql=[Sql('create table x.y as select 1 as a', 'a.sql', '')]),
        "sql in 'a.sql': task '' is not a node of the fragment",
    ),
    'warnings_string': (
        lambda: Fragment(nodes=[task()], warnings='abc'),  # type: ignore[arg-type]
        'warnings must be a list of strings',
    ),
    'nodes_generator': (
        # A generator passes once: the merge would find no nodes for the edges that name them.
        lambda: Fragment(nodes=(n for n in [task()]), edges=[E('t', ORDERS, 'feeds')]),  # type: ignore[arg-type]
        'nodes must be a list',
    ),
    'counts_list': (
        lambda: Fragment(nodes=[task()], counts=[('a', 1)]),  # type: ignore[arg-type]
        'counts must be a dict',
    ),
    'unhashable_kind': (
        lambda: Fragment(nodes=[PluginNode('t', ['task'], 't')]),  # type: ignore[arg-type]
        "node 't': kind ['task'] is not one of dag, exposure, task, test",
    ),
}


@pytest.mark.parametrize('name', sorted(REFUSED))
def test_a_hostile_fragment_is_refused_and_every_command_still_works(
    hostile: Any, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    make, problem = REFUSED[name]
    hostile.READ = lambda context: make()
    graph = build_and_query(hostile.ROOT, capsys)
    assert not [n for n in graph.nodes if n.startswith('hostile.')]
    assert graph.warnings == [
        f'plugins: {HOSTILE_LABEL} returned a fragment that is not valid: {problem}; '
        'the build goes on without it'
    ]


KEPT: dict[str, Any] = {
    'quote_column': lambda: Fragment(nodes=[task()], edges=[E(Column('raw.newtab', '"'), 't', 'feeds')]),
    'long_relation': lambda: Fragment(nodes=[task()], edges=[E(Relation('a.b.c.d.e.f.g'), 't', 'feeds')]),
    'self_column': lambda: Fragment(
        nodes=[task()],
        edges=[E(Column('analytics.orders', 'id'), Column('analytics.orders', 'id'), 'derived_from')],
    ),
    'unique_test_of_relation': lambda: Fragment(
        nodes=[PluginNode('t', 'test', 't', attrs={'test': 'unique'})],
        edges=[E('t', Relation('analytics.orders'), 'tests')],
    ),
    'runs_between_columns': lambda: Fragment(
        nodes=[task()],
        edges=[
            E(Column('analytics.orders', 'id'), Column('raw.orders', 'id'), 'runs'),
            E(ORDERS, Relation('analytics.orders'), 'precedes'),
        ],
    ),
    'tests_from_a_dag': lambda: Fragment(
        nodes=[task(), PluginNode('d', 'dag', 'd')],
        edges=[
            E('d', Column('analytics.orders', 'id'), 'tests'),
            E(Column('analytics.orders', 'id'), 'd', 'runs'),
        ],
    ),
    'deep_sql': lambda: Fragment(
        nodes=[task()], sql=[Sql('select ' + '(' * 3000 + '1' + ')' * 3000, 'a.sql', 't')]
    ),
    'sql_writes_unknown_column': lambda: Fragment(
        nodes=[task()], sql=[Sql('insert into analytics.orders (nope) select 1', 'a.sql', 't')]
    ),
    'long_attribute': lambda: Fragment(nodes=[task(attrs={'x': 'y' * 5_000_000})]),
    'nul': lambda: Fragment(nodes=[task(name='a\x00b', path='a\x00b')]),
    'dotted_ids': lambda: Fragment(
        nodes=[PluginNode('a.b', 'task', 'x'), PluginNode('a', 'task', 'y')],
        edges=[E('a', 'a.b', 'precedes')],
    ),
    'id_named_like_a_relation': lambda: Fragment(nodes=[PluginNode('orders', 'exposure', 'o')]),
    'attrs_at_the_depth_bound': lambda: Fragment(nodes=[task(attrs={'d': deep(64)})]),
    'attrs_konstruct_shares': lambda: Fragment(
        nodes=[
            PluginNode('d', 'dag', 'd', attrs={'owner': 'finance', 'type': 'pipeline', 'region': 'eu'}),
            PluginNode('t', 'test', 't', attrs={'test': 'not_null'}),
        ],
        edges=[E('t', Column('analytics.orders', 'id'), 'tests', {'region': 'eu'})],
    ),
}


@pytest.mark.parametrize('name', sorted(KEPT))
def test_an_odd_fragment_is_merged_and_every_command_still_works(
    hostile: Any, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    fragment = KEPT[name]()
    assert fragment.problems() == []
    hostile.READ = lambda context: fragment
    graph = build_and_query(hostile.ROOT, capsys)
    assert {n.split('.', 1)[1] for n in graph.nodes if n.startswith('hostile.')} == {
        n.id for n in fragment.nodes
    }
    assert not [w for w in graph.warnings if w.startswith('plugins:')]
    # What graph.json holds is what the build made: nothing was changed on the way to the file.
    reloaded = Graph.from_dict(json.loads(json.dumps(graph.to_dict())))
    assert reloaded.to_dict() == graph.to_dict()


def test_a_plugin_whose_error_holds_text_json_cannot_hold_is_reported(
    hostile: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    def read(context: Any) -> Fragment:
        raise ValueError('bad \udc80 ' + 'x' * 1000)

    hostile.READ = read
    graph = build_and_query(hostile.ROOT, capsys)
    [warning] = graph.warnings
    assert warning.startswith(f'plugins: {HOSTILE_LABEL} raised ValueError: bad \\udc80 xxx')
    assert len(warning) < 400


def test_a_problem_names_a_long_value_briefly() -> None:
    [problem] = Fragment(nodes=[PluginNode('#' + 'x' * 10_000, 'task', 't')]).problems()
    assert problem.startswith("node id '#xxx") and problem.endswith(
        'must be a non-empty string without # or a line break'
    )
    assert len(problem) < 200


def _python_attribute_reads(source: str) -> set[str]:
    """Every attribute name ``source`` reads from a node's or an edge's ``attrs``: through
    ``.get``, ``.pop``, ``.setdefault``, a subscript, ``in`` or a ``match`` mapping, on ``.attrs``,
    ``getattr(x, 'attrs')``, ``dict(...)`` or ``{**...}`` of it, ``... or {}``, or a name assigned
    one of those in the same function; with the key a string, a module-level string constant, or
    a loop or comprehension variable over a tuple or list of strings (or a module-level one)."""

    tree = ast.parse(source)
    constants: dict[str, list[str]] = {}
    for item in tree.body:
        if isinstance(item, ast.Assign | ast.AnnAssign) and item.value is not None:
            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
            value = item.value
            strings: list[str] | None = None
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                strings = [value.value]
            elif isinstance(value, ast.Tuple | ast.List | ast.Set):
                found_strings = [_first_string(e) for e in value.elts]
                if found_strings and all(v is not None for v in found_strings):
                    strings = [v for v in found_strings if v is not None]
            elif isinstance(value, ast.Call) and value.args and getattr(value.func, 'id', '') == 'frozenset':
                inner = value.args[0]
                if isinstance(inner, ast.Tuple | ast.List | ast.Set):
                    strings = [v for v in (_first_string(e) for e in inner.elts) if v is not None]
            for target in targets:
                if isinstance(target, ast.Name) and strings is not None:
                    constants[target.id] = strings

    def is_attrs(node: ast.AST, aliases: set[str]) -> bool:
        if isinstance(node, ast.Attribute):
            return node.attr == 'attrs'
        if isinstance(node, ast.Name):
            return node.id == 'attrs' or node.id in aliases
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == 'getattr' and len(node.args) >= 2:
                return _first_string(node.args[1]) == 'attrs'
            if isinstance(func, ast.Name) and func.id == 'dict' and node.args:
                return is_attrs(node.args[0], aliases)
            if isinstance(func, ast.Attribute) and func.attr == 'copy':
                return is_attrs(func.value, aliases)
        if isinstance(node, ast.Dict):
            return any(
                k is None and is_attrs(v, aliases) for k, v in zip(node.keys, node.values, strict=True)
            )
        if isinstance(node, ast.BoolOp):
            return any(is_attrs(v, aliases) for v in node.values)
        return False

    def keys(node: ast.AST, loops: dict[str, list[str]]) -> list[str]:
        value = _first_string(node)
        if value is not None:
            return [value]
        if isinstance(node, ast.Name):
            return loops.get(node.id) or (
                constants.get(node.id, []) if len(constants.get(node.id, [])) == 1 else []
            )
        return []

    def strings_of(node: ast.AST) -> list[str] | None:
        if isinstance(node, ast.Tuple | ast.List | ast.Set):
            return [v for v in (_first_string(e) for e in node.elts) if v is not None]
        if isinstance(node, ast.Name) and node.id in constants:
            return constants[node.id]
        return None

    def target_name(target: ast.AST) -> str | None:
        target = target.elts[0] if isinstance(target, ast.Tuple) and target.elts else target
        return target.id if isinstance(target, ast.Name) else None

    read: set[str] = set()

    def walk(node: ast.AST, aliases: set[str], loops: dict[str, list[str]]) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            aliases = set(aliases)
            for item in ast.walk(node):
                if isinstance(item, ast.Assign | ast.AnnAssign | ast.NamedExpr) and item.value is not None:
                    targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                    if is_attrs(item.value, aliases):
                        aliases |= {t.id for t in targets if isinstance(t, ast.Name)}
        if isinstance(node, ast.For):
            names, name = strings_of(node.iter), target_name(node.target)
            if names is not None and name is not None:
                loops = {**loops, name: names}
        if isinstance(node, ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp):
            for generator in node.generators:
                names, name = strings_of(generator.iter), target_name(generator.target)
                if names is not None and name is not None:
                    loops = {**loops, name: names}
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ('get', 'pop', 'setdefault')
            and is_attrs(node.func.value, aliases)
            and node.args
        ):
            read.update(keys(node.args[0], loops))
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and is_attrs(node.value, aliases)
        ):
            read.update(keys(node.slice, loops))
        elif isinstance(node, ast.Compare):
            for op, right in zip(node.ops, node.comparators, strict=True):
                if isinstance(op, ast.In | ast.NotIn) and is_attrs(right, aliases):
                    read.update(keys(node.left, loops))
        elif isinstance(node, ast.Match) and is_attrs(node.subject, aliases):
            for case in node.cases:
                for pattern in ast.walk(case.pattern):
                    if isinstance(pattern, ast.MatchMapping):
                        read.update(v for v in (_first_string(k) for k in pattern.keys) if v is not None)
        for child in ast.iter_child_nodes(node):
            walk(child, aliases, loops)

    walk(tree, set(), {})
    return read


def _first_string(node: ast.AST) -> str | None:
    """A string constant, or the first item of a tuple of them, as ``(('a', 'A'), ...)`` loops use."""

    if isinstance(node, ast.Tuple) and node.elts:
        node = node.elts[0]
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _viewer_attribute_reads(source: str) -> set[str]:
    """Every attribute name viewer source reads: ``attr(x, 'name')`` (with a type argument or
    not), ``attrs.name``, ``attrs?.name``, ``attrs['name']`` and ``attrs?.['name']``,
    ``attr(x, key)`` inside ``{#each [...] as key}``, and the first names of a list of pairs
    filtered by ``attr(x, k)``, as in ``KEYS.filter(([k]) => attr(x, k) ...)``."""
    import re

    read = set(re.findall(r"\battr(?:<[^>()]*>)?\(\s*[\w.?]+\s*,\s*'(\w+)'\s*\)", source))
    read |= set(re.findall(r'\battrs\??\.(?!\[)(\w+)', source))
    read |= set(re.findall(r"\battrs\??\.?\[\s*['\"](\w+)['\"]\s*\]", source))
    for names, variable, body in re.findall(
        r'\{#each \[([^\]]*)\] as (\w+)[^}]*\}(.*?)\{/each\}', source, re.S
    ):
        if re.search(rf'\battr(?:<[^>()]*>)?\(\s*[\w.?]+\s*,\s*{variable}\s*\)', body):
            read |= set(re.findall(r"'(\w+)'", names))
    for name, pairs in re.findall(r'const (\w+)(?::[^=]*)? = \[(.*?)\];', source, re.S):
        if re.search(rf'\b{name}\s*\.filter\(\s*\(\[(\w+)\]\)\s*=>\s*attr\(\s*[\w.?]+\s*,\s*\1\s*\)', source):
            read |= set(re.findall(r"\[\s*'(\w+)'\s*,", pairs))
    return read


def test_the_attribute_scan_sees_each_way_code_reads_an_attribute() -> None:
    """The scan that keeps the reserved names in step with the code, on each way of reading."""
    source = textwrap.dedent(
        """\
        KEY = 'constant_key'
        KEYS = ('looped_constant_a', 'looped_constant_b')


        def reads(node, edge, kind):
            meta = node.attrs
            meta.get('aliased')
            node.attrs.get(KEY)
            dict(node.attrs)['wrapped']
            getattr(node, 'attrs').get('fetched')
            match edge.attrs:
                case {'matched': value}:
                    pass
            {**edge.attrs}['spread']
            (node.attrs or {}).get('defaulted')
            'tested' in node.attrs
            for key in ('looped_a', 'looped_b'):
                node.attrs[key]
            for key in KEYS:
                node.attrs.get(key)
            [node.attrs.get(name) for name, _ in (('comprehended', 1),)]
            other = {}
            other.get('not_an_attribute')
        """
    )
    assert _python_attribute_reads(source) == {
        'aliased',
        'constant_key',
        'wrapped',
        'fetched',
        'matched',
        'spread',
        'defaulted',
        'tested',
        'looped_a',
        'looped_b',
        'looped_constant_a',
        'looped_constant_b',
        'comprehended',
    }
    viewer = textwrap.dedent(
        """\
        const a = attr<Usage>(column, 'typed');
        const b = attr(e, 'plain');
        const c = node.attrs?.optional;
        const d = node.attrs.dotted;
        const e = node.attrs['indexed'];
        {#each ['each_a', 'each_b'] as key (key)}{#if attr(relation, key)}{/if}{/each}
        const keys: [string, string][] = [
            ['paired_a', t('a')],
            ['paired_b', t('b')],
        ];
        const shown = keys.filter(([k]) => attr(n, k) !== undefined);
        const labels = [['not_read', t('c')]];
        """
    )
    assert _viewer_attribute_reads(viewer) == {
        'typed',
        'plain',
        'optional',
        'dotted',
        'indexed',
        'each_a',
        'each_b',
        'paired_a',
        'paired_b',
    }


def test_attributes_konstruct_reads_are_reserved() -> None:
    """Every attribute name Konstruct reads from a node or an edge, in the package or the viewer,
    is one a plugin may not set, bar the few a plugin sets with the meaning Konstruct gives them:
    a new reader that forgets to reserve its name fails here, and so does a reserved name nothing
    reads any more."""
    from konstruct.plugins.api import (
        PLUGIN_LIST_ATTRS,
        PLUGIN_MAP_ATTRS,
        PLUGIN_NODE_ATTRS,
        RESERVED_EDGE_ATTRS,
        RESERVED_NODE_ATTRS,
    )

    read: set[str] = set()
    for path in (ROOT / 'konstruct').rglob('*.py'):
        if path.parent.name != 'plugins':
            read |= _python_attribute_reads(path.read_text())
    viewer = {
        path: _viewer_attribute_reads(path.read_text())
        for path in [*(ROOT / 'viewer' / 'src').rglob('*.ts'), *(ROOT / 'viewer' / 'src').rglob('*.svelte')]
        if '.test.' not in path.name
    }
    from_viewer = set().union(*viewer.values())
    assert {'role', 'verified', 'sql_path', 'usage', 'sensors', 'line'} <= from_viewer
    read |= from_viewer
    assert {'conditions', 'expression', 'usage', 'role', 'command', 'primary_key'} <= read
    assert read - PLUGIN_NODE_ATTRS - PLUGIN_LIST_ATTRS - PLUGIN_MAP_ATTRS == RESERVED_NODE_ATTRS
    assert (read - PLUGIN_LIST_ATTRS) | {'origin', 'source', 'target', 'kind'} == RESERVED_EDGE_ATTRS


def test_counts_konstruct_gives_are_reserved() -> None:
    """Every count the build gives is one an input's joined count name may not take."""

    from konstruct.engine.build import BUILTIN_COUNTS

    given = {
        item.slice.value
        for item in ast.walk(ast.parse((ROOT / 'konstruct' / 'engine' / 'build.py').read_text()))
        if isinstance(item, ast.Subscript)
        and isinstance(item.value, ast.Attribute)
        and item.value.attr == 'inputs'
        and isinstance(item.slice, ast.Constant)
    }
    assert given == BUILTIN_COUNTS


def test_a_cached_value_json_cannot_hold_fails_the_plugin_not_the_build(
    hostile: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    def read(context: Any) -> Fragment:
        calls.append('read')

        # A value that is JSON-ready is kept for the next build.
        def compute() -> dict[str, Any]:
            calls.append('good')
            return {'n': [1, 2]}

        assert context.cached(('good',), compute) == {'n': [1, 2]}
        context.cached(('bad',), lambda: {1, 2})
        raise AssertionError('never reached')

    hostile.READ = read
    for options in ([], ['-u'], ['-u']):
        graph = build_and_query(hostile.ROOT, capsys, *options)
        [warning] = graph.warnings
        assert warning.startswith(f'plugins: {HOSTILE_LABEL} raised PluginError: context.cached(')
        assert 'the value holds a set, which is not JSON data' in warning
    assert calls == ['read', 'good', 'read', 'read']
    cache = json.loads(next((hostile.ROOT / 'konstruct-out').rglob('cache*.json')).read_text())
    assert {'n': [1, 2]} in cache['entries'].values()


@pytest.mark.parametrize(
    'value',
    [deep(65), float('nan'), '\udc80', 2**64],
    ids=['too-deep', 'nan', 'surrogate', 'bigint'],
)
def test_a_cached_value_is_checked_as_graph_json_is(
    hostile: Any, capsys: pytest.CaptureFixture[str], value: Any
) -> None:
    def read(context: Any) -> Fragment:
        context.cached(('k',), lambda: {'v': value})
        return Fragment(nodes=[task()])

    hostile.READ = read
    graph = build_and_query(hostile.ROOT, capsys)
    [warning] = graph.warnings
    assert warning.startswith(f'plugins: {HOSTILE_LABEL} raised PluginError: context.cached(')


def tools_project(site: Path, tmp_path: Path, tools: dict[str, Any]) -> Path:
    module: Any = importlib.import_module('hostile')

    module.TOOLS.update(tools)
    install(
        site,
        'konstruct-hostile-tools',
        '1.0',
        {'konstruct.mcp_tools': {name: f'hostile:TOOLS_{name.upper()}' for name in tools}},
    )
    root = tmp_path / 'tools'
    root.mkdir()
    (root / 'konstruct.toml').write_text('[plugins]\nenabled = ["konstruct-hostile-tools"]\n')
    return root


def good() -> dict[str, Any]:
    """A tool that works."""
    return {}


TOOLS_LABEL = "tools '{}' (konstruct-hostile-tools 1.0)"


def test_tool_listings_a_plugin_gets_wrong_leave_the_rest_served(
    hostile: Any, site: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tools_project(
        site,
        tmp_path,
        {
            # A name that is not a string cannot be sorted with the others.
            'mixed': {'ok': good, 3: good, None: good},
            # A plugin's tool is named for its plugin, so it never takes the name of one of Konstruct's.
            'list': {'terms': good, 'x': good},
            # Unless the joined name is one: Konstruct's mutation_test is kept.
            'mutation': {'test': good},
            'notmap': 5,
            'pairs': iter([('ok', good)]),
            # a_b + c and a + b_c both join to a_b_c: the first, in the listing's order, is kept.
            'a': {'b_c': good},
            'a_b': {'c': good},
        },
    )

    async def names() -> list[str]:
        async with Client(build_server(root)) as client:
            return [t.name for t in (await client.list_tools()).tools]

    tools = anyio.run(names)
    assert [t for t in tools if '_' in t] == [
        'check_sql',
        'check_history',
        'release_notes',
        'mutation_test',
        'definition_effect',
        'a_b_c',
        'list_terms',
        'list_x',
        'mixed_ok',
    ]
    assert (
        f"{TOOLS_LABEL.format('mutation')}: tool 'mutation_test' is left out: Konstruct has a tool of that name"
        in caplog.text
    )
    assert 'impact' in tools and 'terms' not in tools
    label = TOOLS_LABEL
    assert f'{label.format("mixed")}: tool 3 is left out' in caplog.text
    assert f'{label.format("mixed")}: tool None is left out' in caplog.text
    assert f'{label.format("notmap")} returned 5, not a mapping of names to functions' in caplog.text
    assert f'{label.format("pairs")} returned ' in caplog.text
    assert (
        f"{label.format('a_b')}: tool 'c' is left out: its name, a_b_c, is taken by tool 'b_c' of tools 'a'"
        in caplog.text
    )


def test_counts_whose_joined_names_clash_keep_the_first(
    hostile: Any, site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module: Any = importlib.import_module('hostile')

    def read_a(context: Any) -> Fragment:
        return Fragment(counts={'b_c': 1})

    def read_ab(context: Any) -> Fragment:
        return Fragment(counts={'c': 2, 'd': 3})

    def read_landed(context: Any) -> Fragment:
        return Fragment(counts={'tables': 4})

    (site / 'hostile_more.py').write_text(
        textwrap.dedent(
            """\
            import hostile


            class Reader:
                api_version = hostile.API_VERSION
                patterns = ()

                def __init__(self, name):
                    self.name = name

                def read(self, files, context):
                    return hostile.READERS[self.name](context)


            A, A_B, LANDED = Reader('a'), Reader('a_b'), Reader('landed')
            """
        )
    )
    module.READERS = {'a': read_a, 'a_b': read_ab, 'landed': read_landed}
    install(
        site,
        'konstruct-hostile-counts',
        '1.0',
        {
            'konstruct.inputs': {
                'a': 'hostile_more:A',
                'a_b': 'hostile_more:A_B',
                'landed': 'hostile_more:LANDED',
            }
        },
    )
    root = hostile.ROOT
    (root / 'konstruct.toml').write_text('[plugins]\nenabled = ["konstruct-hostile-counts"]\n')
    assert main(['build', str(root), '-q', '--no-warehouse', '--no-dbt']) == 0
    graph = Graph.from_dict(json.loads((root / 'konstruct-out' / 'graph.json').read_text()))
    counts = graph.meta['inputs']
    assert (counts['a_b_c'], counts['a_b_d']) == (1, 3)
    assert 'landed_tables' not in counts
    assert graph.warnings == [
        "plugins: input 'a_b' (konstruct-hostile-counts 1.0) count 'c' is left out: a_b_c is already a count",
        "plugins: input 'landed' (konstruct-hostile-counts 1.0) count 'tables' is left out: "
        "landed_tables is one of Konstruct's own counts",
    ]


# Values orjson writes but graph.json cannot hold as given: each is refused, in nodes and edges.


@dataclasses.dataclass
class Box:
    value: Any


class Colour(enum.Enum):
    RED = 'red'


class Shade(enum.StrEnum):
    DARK = 'dark'


NOT_JSON: dict[str, tuple[Any, str]] = {
    'deep_dataclass': (Box(deep(250)), 'a Box'),
    'nan_dataclass': (Box(float('nan')), 'a Box'),
    'datetime': (datetime.datetime(2026, 1, 2, 3, 4), 'a datetime'),
    'date': (datetime.date(2026, 1, 2), 'a date'),
    'enum': (Colour.RED, 'a Colour'),
    'str_enum': (Shade.DARK, 'a Shade'),
    'uuid': (uuid.UUID(int=1), 'a UUID'),
}


@pytest.mark.parametrize('where', ['node', 'edge'])
@pytest.mark.parametrize('name', sorted(NOT_JSON))
def test_an_attribute_that_is_not_json_data_is_refused(
    hostile: Any, capsys: pytest.CaptureFixture[str], name: str, where: str
) -> None:
    value, shown = NOT_JSON[name]
    if where == 'node':
        fragment = Fragment(nodes=[task(attrs={'v': [value]})])
        problem = f"node 't': attrs hold {shown}, which is not JSON data"
    else:
        fragment = Fragment(nodes=[task()], edges=[E('t', ORDERS, 'feeds', {'v': {'x': value}})])
        problem = f"edge 't' -> Relation(key='raw.orders'): attrs hold {shown}, which is not JSON data"
    hostile.READ = lambda context: fragment
    graph = build_and_query(hostile.ROOT, capsys)
    assert graph.warnings == [
        f'plugins: {HOSTILE_LABEL} returned a fragment that is not valid: {problem}; the build goes on without it'
    ]


def test_a_key_that_is_not_a_string_is_refused_at_any_depth() -> None:
    [problem] = Fragment(nodes=[task(attrs={'v': [{1: 'one'}]})]).problems()
    assert problem == "node 't': attrs hold a dict whose key 1 is not a string"


def test_a_cached_value_is_the_same_in_a_cold_build_and_an_update(
    hostile: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[Any] = []

    def read(context: Any) -> Fragment:
        seen.append(context.cached(('pair',), lambda: {'pair': (1, 2)}))
        return Fragment(nodes=[task()])

    hostile.READ = read
    build_and_query(hostile.ROOT, capsys)
    build_and_query(hostile.ROOT, capsys, '-u')
    # A tuple is a list in JSON: the cold build sees what the update reads back.
    assert seen == [{'pair': [1, 2]}, {'pair': [1, 2]}]


def test_a_cached_date_from_yaml_is_refused_in_a_cold_build_and_an_update(
    hostile: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    import yaml

    def read(context: Any) -> Fragment:
        context.cached(('yaml',), lambda: yaml.safe_load('when: 2026-01-02\n'))
        return Fragment(nodes=[task()])

    hostile.READ = read
    for options in ([], ['-u']):
        graph = build_and_query(hostile.ROOT, capsys, *options)
        assert graph.warnings == [
            f"plugins: {HOSTILE_LABEL} raised PluginError: context.cached(('yaml',)): the value holds a date, "
            'which is not JSON data; the build goes on without it'
        ]


def test_a_cache_key_json_cannot_hold_fails_the_plugin(
    hostile: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    def read(context: Any) -> Fragment:
        context.cached(({1, 2},), lambda: 1)
        return Fragment(nodes=[task()])

    hostile.READ = read
    graph = build_and_query(hostile.ROOT, capsys)
    assert graph.warnings == [
        f'plugins: {HOSTILE_LABEL} raised PluginError: context.cached(({{1, 2}},)): the key holds a set, '
        'which is not JSON data; the build goes on without it'
    ]


@pytest.mark.parametrize(
    ('attrs', 'problem'),
    [
        ({'owner': {'name': 'x'}}, "node 't': owner must be a string"),
        ({'test': 1}, "node 't': test must be a string"),
        ({'schedule': ['@daily']}, "node 't': schedule must be a string"),
    ],
)
def test_attributes_a_plugin_shares_with_konstruct_are_strings(attrs: dict[str, Any], problem: str) -> None:
    assert Fragment(nodes=[task(attrs=attrs)]).problems() == [problem]
    shared = {
        'owner': 'finance',
        'test': 'unique',
        'schedule': '@daily',
        'operator': 'x',
        'orchestrator': 'y',
    }
    assert Fragment(nodes=[task(attrs=shared)]).problems() == []


def test_sql_runs_in_a_task() -> None:
    fragment = Fragment(nodes=[PluginNode('d', 'dag', 'd')], sql=[Sql('select 1', 'a.sql', 'd')])
    assert fragment.problems() == ["sql in 'a.sql': task 'd' is a dag, not a task"]


def test_newlines_in_what_a_plugin_names_are_flattened(
    hostile: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    hostile.READ = lambda context: Fragment(
        nodes=[PluginNode('t', 'exposure', 'board\n# Injected heading', description='one\ntwo')],
        edges=[
            E(Relation('analytics.orders'), 't', 'feeds'),
            E(Column('analytics.orders', 'gone'), 't', 'feeds', missing='a\nb'),
        ],
        warnings=['first line\nsecond line'],
        sources=[Source('x.thing', 'one\r\ntwo')],
    )
    graph = build_and_query(hostile.ROOT, capsys)
    node = graph.nodes['hostile.t']
    assert node.name == 'board # Injected heading'
    assert 'hostile: first line second line' in graph.warnings and 'hostile: a b' in graph.warnings
    assert {'input': 'hostile', 'path': 'x.thing', 'detail': 'one two'} in graph.meta['sources']
    report = (hostile.ROOT / 'konstruct-out' / 'REPORT.md').read_text()
    assert '\n# Injected heading' not in report
    assert main(['export', 'mermaid', '-C', str(hostile.ROOT)]) == 0
    assert '# Injected heading' not in [line.strip() for line in capsys.readouterr().out.splitlines()]


GATE_CHECK = """\
from konstruct.plugins import API_VERSION, Finding


class Check:
    api_version = API_VERSION
    description = 'returns what the test sets'

    def __init__(self, name):
        self.name = name

    def check(self, context):
        return FINDINGS[self.name]


FINDINGS = {}


def __getattr__(name):
    if name.startswith('CHECK_'):
        return Check(name[len('CHECK_') :].lower())
    raise AttributeError(name)
"""


def test_findings_a_check_gets_wrong_fail_the_gate_as_that_check(
    site: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from konstruct.plugins import Finding

    (site / 'hostile_checks.py').write_text(GATE_CHECK)
    module: Any = importlib.import_module('hostile_checks')
    module.FINDINGS.update(
        {
            'object': [Finding('r', object(), fix='x')],  # type: ignore[arg-type]
            'mapping': [Finding('r', {'a': 1}, fix='x')],  # type: ignore[arg-type]
            'path': [Finding('r', 'm', path=3, fix='x')],  # type: ignore[arg-type]
            'empty': [Finding('', 'm', fix='x')],
            'nofix': [Finding('r', 'm')],
            'surrogate': [Finding('r', 'm\udc80', fix='x')],
            'good': [Finding('rule', 'first\nsecond', 'models/a.sql', fix='do\nthis')],
            'flat': [Finding('rule', 'm', 'models/\na.sql', fix='x')],
        }
    )
    install(
        site,
        'konstruct-hostile-checks',
        '1.0',
        {'konstruct.checks': {name: f'hostile_checks:CHECK_{name.upper()}' for name in module.FINDINGS}},
    )
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'konstruct.toml').write_text('[plugins]\nenabled = ["konstruct-hostile-checks"]\n')
    (root / 'etl.sql').write_text('create table analytics.orders as select id from raw.orders;\n')
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'add', '.')
    git(root, 'commit', '-q', '-m', 'Start')
    out = tmp_path / 'gate.json'
    assert (
        main(['gate', '-C', str(root), '--base', 'HEAD', '--no-warehouse', '--json-file', str(out), '-q'])
        == 1
    )
    capsys.readouterr()
    findings = {f['rule']: f for f in json.loads(out.read_text())['findings']}
    wrong = 'returned something other than findings, each with a rule, message and fix'
    for name in ('object', 'mapping', 'path', 'empty', 'nofix', 'surrogate'):
        assert (
            findings[f'{name}.failed']['message']
            == f'the gate check from konstruct-hostile-checks 1.0 {wrong}'
        )
    assert findings['good.rule'] == {
        'rule': 'good.rule',
        'message': 'first second',
        'path': 'models/a.sql',
        'fix': 'do this',
    }
    assert findings['flat.rule']['path'] == 'models/ a.sql'
    assert (
        main(['gate', '-C', str(root), '--base', 'HEAD', '--no-warehouse', '--format', 'markdown', '-q']) == 1
    )
    del sys.modules['hostile_checks']


def _with_text_subclass(field: str) -> Fragment:
    """A valid fragment, with ``field`` given as a str subclass."""
    t = Text
    values: dict[str, Any] = {
        'id': lambda: Fragment(nodes=[task(id=t('t'))]),
        'name': lambda: Fragment(nodes=[task(name=t('t'))]),
        'path': lambda: Fragment(nodes=[task(path=t('a.yml'))]),
        'description': lambda: Fragment(nodes=[task(description=t('d'))]),
        'edge_kind': lambda: Fragment(nodes=[task()], edges=[E('t', ORDERS, t('feeds'))]),
        'end': lambda: Fragment(nodes=[task()], edges=[E(t('t'), ORDERS, 'feeds')]),
        'relation_key': lambda: Fragment(nodes=[task()], edges=[E('t', Relation(t('raw.orders')), 'feeds')]),
        'column_relation': lambda: Fragment(
            nodes=[task()], edges=[E(Column(t('raw.orders'), 'id'), 't', 'feeds')]
        ),
        'column_name': lambda: Fragment(
            nodes=[task()], edges=[E(Column('raw.orders', t('id')), 't', 'feeds')]
        ),
        'missing': lambda: Fragment(
            nodes=[task()], edges=[E(Column('raw.orders', 'id'), 't', 'feeds', missing=t('m'))]
        ),
        'sql': lambda: Fragment(nodes=[task()], sql=[Sql(t('select 1'), 'a.sql', 't')]),
        'sql_path': lambda: Fragment(nodes=[task()], sql=[Sql('select 1', t('a.sql'), 't')]),
        'sql_task': lambda: Fragment(nodes=[task()], sql=[Sql('select 1', 'a.sql', t('t'))]),
        'sql_dialect': lambda: Fragment(nodes=[task()], sql=[Sql('select 1', 'a.sql', 't', t('duckdb'))]),
        'source_path': lambda: Fragment(nodes=[task()], sources=[Source(t('a'), 'd')]),
        'source_detail': lambda: Fragment(nodes=[task()], sources=[Source('a', t('d'))]),
        'warning': lambda: Fragment(nodes=[task()], warnings=[t('w')]),
        'count_key': lambda: Fragment(nodes=[task()], counts={t('things'): 1}),
        'attrs_key': lambda: Fragment(nodes=[task(attrs={t('k'): 1})]),
    }
    fragment: Fragment = values[field]()
    return fragment


@pytest.mark.parametrize(
    'field',
    [
        'id', 'name', 'path', 'description', 'edge_kind', 'end', 'relation_key', 'column_relation',
        'column_name', 'missing', 'sql', 'sql_path', 'sql_task', 'sql_dialect', 'source_path',
        'source_detail', 'warning', 'count_key', 'attrs_key',
    ],
)  # fmt: skip
def test_text_fields_are_plain_strings(field: str) -> None:
    assert _with_text_subclass(field).problems() != []
    # The same fragment with plain strings is valid.
    plain = Fragment(
        nodes=[task(path='a.yml', description='d', attrs={'k': 1})],
        edges=[E(Column('raw.orders', 'id'), 't', 'feeds', missing='m'), E('t', ORDERS, 'feeds')],
        sql=[Sql('select 1', 'a.sql', 't', 'duckdb')],
        sources=[Source('a', 'd')],
        warnings=['w'],
        counts={'things': 1},
    )
    assert plain.problems() == []


def test_a_cached_value_a_plugin_changes_is_not_what_the_cache_keeps(
    hostile: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    def read(context: Any) -> Fragment:
        first = context.cached(('k',), lambda: {'n': [1]})
        first['n'].append(2)
        # A second call in the same build sees the value as computed, not as changed.
        again = context.cached(('k',), lambda: {'n': [1]})
        return Fragment(nodes=[task(attrs={'first': first['n'], 'again': again['n']})])

    hostile.READ = read
    seen = [
        build_and_query(hostile.ROOT, capsys, *options).nodes['hostile.t'].attrs
        for options in ([], ['-u'], ['-u'])
    ]
    assert seen == [{'first': [1, 2], 'again': [1]}] * 3


def test_markdown_cells_keep_a_finding_whole() -> None:
    from konstruct.analysis.gate import Gate
    from konstruct.plugins import Finding

    result = Gate(base='main', findings=[Finding('p.r', 'a | b', 'x|y.sql', fix='c | d')])
    [row] = [line for line in result.markdown().splitlines() if line.startswith('| `p.r`')]
    assert row == '| `p.r` | a \\| b (`x\\|y.sql`) | c \\| d |'


def test_konstruct_reads_no_attribute_it_does_not_reserve(
    jaffle: Path, dbt_on_path: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A backstop for the scan of the code: every attribute name actually read from a node or an
    edge while the example is built and questioned is one Konstruct reserves or shares."""
    from konstruct.core import graph as graph_module
    from konstruct.plugins.api import _KONSTRUCT_ATTRS

    read: set[str] = set()

    class Recording(dict[str, Any]):
        def __getitem__(self, key: str) -> Any:
            read.add(key)
            return super().__getitem__(key)

        def get(self, key: str, default: Any = None) -> Any:
            read.add(key)
            return super().get(key, default)

        def __contains__(self, key: object) -> bool:
            read.add(str(key))
            return super().__contains__(key)

        def pop(self, key: str, *default: Any) -> Any:
            read.add(key)
            return super().pop(key, *default)

        def setdefault(self, key: str, default: Any = None) -> Any:
            read.add(key)
            return super().setdefault(key, default)

    for cls in (graph_module.Node, graph_module.Edge):
        original = cls.__init__

        def init(self: Any, *args: Any, __original: Any = original, **kwargs: Any) -> None:
            __original(self, *args, **kwargs)
            self.attrs = Recording(self.attrs)

        monkeypatch.setattr(cls, '__init__', init)
    root = str(jaffle)
    assert main(['build', root, '-q']) == 0
    for command in (
        ['impact', 'orders.amount'],
        ['impact', 'stg_payments', '--json'],
        ['trace', 'customer_ltv.lifetime_value'],
        ['trace', 'orders.amount', '--json'],
        ['explain', 'customer_ltv.lifetime_value'],
        ['describe', 'jaffle_shop_daily.build_customer_ltv'],
        ['describe', 'orders'],
        ['describe', 'orders.amount', '--json'],
        ['define', 'lifetime value'],
        ['ontology'],
        ['conflicts', '--no-write'],
        ['query', 'what breaks if I change payment_method'],
        ['search', 'amount'],
        ['path', 'stg_payments', 'customers'],
        ['list'],
        *(['export', name] for name in ('openlineage', 'mermaid', 'dot', 'csv', 'ossie')),
        ['status'],
    ):
        assert main([*command, '-C', root]) in (0, 3), command
    capsys.readouterr()
    assert {'role', 'expression', 'verified'} <= read
    # Graph.add_edge merges an edge added twice by setdefault of each attribute it brings, which
    # reads origin too: Konstruct sets it on every edge, and a plugin's edge may not.
    assert read - _KONSTRUCT_ATTRS == {'origin'}


def test_a_plugin_node_may_set_its_measures_as_a_map_of_strings() -> None:
    from konstruct.plugins.api import Fragment, PluginNode

    good = Fragment(nodes=[PluginNode('view.a', 'exposure', 'a', attrs={'measures': {'total': 'SUM(x)'}})])
    assert good.problems() == []
    bad = Fragment(nodes=[PluginNode('view.a', 'exposure', 'a', attrs={'measures': {'total': 3}})])
    assert bad.problems() == ["node 'view.a': measures must be a map of strings to strings"]
