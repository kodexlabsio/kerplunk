"""`konstruct hook post-edit`: the gate after an agent edits a data file, without touching the
user's graph and without running dbt or reaching a warehouse."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from konstruct.interfaces import hook
from konstruct.interfaces.cli import main
from konstruct.interfaces.mcp_server import UNTRUSTED_CONTENT


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


def event(root: Path, path: Path | str) -> dict[str, Any]:
    return {
        'cwd': str(root),
        'hook_event_name': 'PostToolUse',
        'tool_name': 'Edit',
        'tool_input': {'file_path': str(path)},
    }


def test_which_files_are_data_files(tmp_path: Path) -> None:
    (tmp_path / 'dbt' / 'models').mkdir(parents=True)
    (tmp_path / 'dbt' / 'dbt_project.yml').write_text('name: x\n')
    data = ['a.sql', 'dbt/models/schema.yml', 'definitions.yml', 'dbt/dbt_project.yml', 'm.ossie.yaml']
    other = ['ci.yml', 'dags/daily.py', 'dbt/target/compiled/a.sql', 'README.md', 'konstruct-out/x.sql']
    assert all(hook.is_data_file(tmp_path / p, tmp_path) for p in data)
    assert not any(hook.is_data_file(tmp_path / p, tmp_path) for p in other)
    assert not hook.is_data_file(tmp_path.parent / 'elsewhere.sql', tmp_path)
    assert hook.edited_path({'tool_input': {'file_path': 'a.sql'}}) == Path('a.sql')
    assert hook.edited_path({'tool_input': 'nothing'}) is None
    assert hook.edited_path({}) is None


def test_yaml_counts_only_under_the_dbt_projects_resource_paths(tmp_path: Path) -> None:
    # A dbt project at the repository root, the usual layout: its CI and tool files are YAML too.
    (tmp_path / 'dbt_project.yml').write_text("name: shop\nmodel-paths: ['transform']\n")
    data = [
        'transform/marts/schema.yml',
        'seeds/properties.yml',
        'snapshots/s.yml',
        'macros/m.yml',
        'tests/t.yml',
        'analyses/a.yml',
        'dbt_project.yml',
        'definitions.yaml',
        'semantic.ossie.yml',
        'scripts/load.sql',
    ]
    other = [
        '.github/workflows/ci.yml',
        'docker-compose.yml',
        'models/schema.yml',  # not a model path in this project
        'profiles.yml',
        '.sqlfluff/x.sql',
        'transform/.hidden/a.sql',
        '.pre-commit-config.yaml',
    ]
    assert [p for p in data if not hook.is_data_file(tmp_path / p, tmp_path)] == []
    assert [p for p in other if hook.is_data_file(tmp_path / p, tmp_path)] == []
    (tmp_path / 'dbt_project.yml').write_text('name: [broken\n')
    assert hook.is_data_file(tmp_path / 'models' / 'schema.yml', tmp_path)  # dbt's defaults


def test_quiet_on_other_files_and_bad_input(tmp_path: Path) -> None:
    assert hook.post_edit(event(tmp_path, tmp_path / 'README.md')) is None
    assert hook.post_edit({'cwd': str(tmp_path)}) is None
    out = io.StringIO()
    assert hook.main('post-edit', stdin=io.StringIO('not json'), stdout=out) == 0
    assert hook.main('post-edit', stdin=io.StringIO('[]'), stdout=out) == 0
    assert out.getvalue() == ''


def test_quiet_in_a_repository_konstruct_does_not_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A plugin installed for every project must not nag an application about its migrations.
    monkeypatch.delenv('CLAUDE_PROJECT_DIR', raising=False)
    (tmp_path / 'migrations').mkdir()
    assert hook.post_edit(event(tmp_path, tmp_path / 'migrations' / '001.sql')) is None


@pytest.mark.parametrize('marker', ['dbt_project.yml', 'konstruct.toml'])
def test_reminds_a_data_repository_with_no_graph(
    tmp_path: Path, marker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv('CLAUDE_PROJECT_DIR', raising=False)
    (tmp_path / marker).write_text('name: x\n' if marker.endswith('.yml') else '')
    answer = hook.post_edit(event(tmp_path, 'models/a.sql'))
    assert answer is not None
    context = answer['hookSpecificOutput']['additionalContext']
    assert 'no Konstruct graph yet' in context and 'models/a.sql' in context
    assert context.endswith(f'({UNTRUSTED_CONTENT})')


@pytest.fixture
def repo(jaffle_built_graph: Path, tmp_path: Path) -> Path:
    project = tmp_path / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph, project)
    (project / '.gitignore').write_text('konstruct-out/\ntarget/\nlogs/\n*.duckdb*\n.user.yml\n')
    git(project, 'init', '-q', '-b', 'main')
    git(project, 'add', '-A')
    git(project, 'commit', '-q', '-m', 'Example')
    return project


def no_dbt_or_warehouse(monkeypatch: pytest.MonkeyPatch) -> list[list[Any]]:
    """Make running dbt or opening any warehouse connection fail the test; return the warehouses
    each build discovered, which must be none."""
    import duckdb
    import psycopg

    from konstruct.engine import build, config
    from konstruct.inputs import dbt, warehouse

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f'the hook must not run dbt or connect to a warehouse: {args}')

    class Subprocess:
        run = staticmethod(refuse)

    monkeypatch.setattr(dbt, 'subprocess', Subprocess)
    monkeypatch.setattr(duckdb, 'connect', refuse)
    monkeypatch.setattr(psycopg, 'connect', refuse)
    monkeypatch.setattr(warehouse, '_driver', refuse)
    monkeypatch.setattr(config.WarehouseConfig, 'connector', refuse)
    discovered: list[list[Any]] = []
    discover = config.discover_warehouses

    def recorded(*args: Any) -> Any:
        found = discover(*args)
        discovered.append(found)
        return found

    monkeypatch.setattr(build, 'discover_warehouses', recorded)
    return discovered


@pytest.mark.dbt
def test_runs_the_gate_after_an_edit(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv('CLAUDE_PROJECT_DIR', raising=False)
    discovered = no_dbt_or_warehouse(monkeypatch)
    graph = (repo / 'konstruct-out' / 'graph.json').read_bytes()
    staging = repo / 'models' / 'staging' / 'stg_payments.sql'
    assert hook.post_edit(event(repo, staging)) == {
        'hookSpecificOutput': {
            'hookEventName': 'PostToolUse',
            'additionalContext': (
                f'Konstruct gate passed after the edit to models/staging/stg_payments.sql. ({UNTRUSTED_CONTENT})'
            ),
        }
    }
    staging.write_text(staging.read_text().replace('amount / 100 as amount', 'amount / 100 as amount_aud'))
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(event(repo, staging))))
    assert main(['hook', 'post-edit']) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer['decision'] == 'block'
    assert 'breaking: stg_payments.amount is removed but still read by customers, orders' in answer['reason']
    assert answer['reason'].endswith(f'\n\n{UNTRUSTED_CONTENT}')
    # The user's graph is left as it was; the hook builds into its own directory.
    assert (repo / 'konstruct-out' / 'graph.json').read_bytes() == graph
    assert (repo / 'konstruct-out' / '.hook' / 'graph.json').is_file()
    assert not (repo / 'konstruct-out' / '.hook' / 'graph.html').exists()
    assert (repo / 'konstruct-out' / '.hook' / '.gitignore').read_text() == '*\n'
    assert discovered and all(found == [] for found in discovered)

    reminder = hook.post_edit(event(repo, staging), remind=True)
    assert (
        reminder is not None
        and 'Run `konstruct gate`' in reminder['hookSpecificOutput']['additionalContext']
        and reminder['hookSpecificOutput']['additionalContext'].endswith(f'({UNTRUSTED_CONTENT})')
    )


@pytest.mark.dbt
def test_works_from_the_project_root_whatever_the_sessions_directory(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv('CLAUDE_PROJECT_DIR', raising=False)
    no_dbt_or_warehouse(monkeypatch)
    staging = repo / 'models' / 'staging' / 'stg_payments.sql'
    staging.write_text(staging.read_text().replace('amount / 100 as amount', 'amount / 100 as amount_aud'))
    # The session runs in models/, and the event's path is relative to it.
    answer = hook.post_edit(event(repo / 'models', 'staging/stg_payments.sql'))
    assert answer is not None and answer['decision'] == 'block', answer
    assert 'stg_payments.amount is removed' in answer['reason']
    built = json.loads((repo / 'konstruct-out' / '.hook' / 'graph.json').read_text())
    assert Path(built['meta']['root']).resolve() == repo.resolve()
    # Claude Code's project directory wins over the session's.
    monkeypatch.setenv('CLAUDE_PROJECT_DIR', str(repo))
    answer = hook.post_edit(event(repo / 'models' / 'staging', staging))
    assert answer is not None and answer['decision'] == 'block'


@pytest.mark.dbt
def test_finds_the_output_directory_konstruct_toml_names(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('CLAUDE_PROJECT_DIR', raising=False)
    (repo / 'konstruct-out').rename(repo / 'lineage')
    (repo / 'konstruct.toml').write_text('out_dir = "lineage"\n')
    answer = hook.post_edit(event(repo, repo / 'models' / 'orders.sql'))
    assert answer is not None and 'gate passed' in answer['hookSpecificOutput']['additionalContext'], answer
    assert (repo / 'lineage' / '.hook' / 'graph.json').is_file()


@pytest.mark.dbt
def test_says_when_the_gate_cannot_run(
    jaffle_built_graph: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv('CLAUDE_PROJECT_DIR', raising=False)
    project = tmp_path / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph, project)  # not a git repository, so there is no base
    answer = hook.post_edit(event(project, project / 'models' / 'orders.sql'))
    assert answer is not None
    assert 'could not run the gate' in answer['hookSpecificOutput']['additionalContext']


@pytest.mark.dbt
def test_a_file_konstruct_cannot_read_is_never_a_pass(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('CLAUDE_PROJECT_DIR', raising=False)
    staging = repo / 'models' / 'staging' / 'stg_payments.sql'
    staging.write_text('select from where ((( not sql')
    answer = hook.post_edit(event(repo, staging))
    assert answer is not None and answer['decision'] == 'block'
    assert 'could not read all of the edited file' in answer['reason']
    assert 'stg_payments: could not compare the after version' in answer['reason']
    git(repo, 'checkout', '--', '.')
    schema = repo / 'models' / 'schema.yml'
    schema.write_text(schema.read_text() + '\n  - name: [unclosed\n')
    answer = hook.post_edit(event(repo, schema))
    assert answer is not None and answer['decision'] == 'block'
    assert 'schema.yml is not valid YAML' in answer['reason'] and 'Gate passed' not in answer['reason']


@pytest.mark.dbt
def test_hooks_for_parallel_edits_take_turns(repo: Path) -> None:
    staging = repo / 'models' / 'staging' / 'stg_payments.sql'
    staging.write_text(staging.read_text().replace('amount / 100 as amount', 'amount / 100 as amount_aud'))
    payload = json.dumps(event(repo, staging))
    env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_PROJECT_DIR'}
    command = [sys.executable, '-m', 'konstruct.interfaces.cli', 'hook', 'post-edit']
    runs = [
        subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
        )
        for _ in range(3)
    ]
    outputs = [run.communicate(payload, timeout=300) for run in runs]
    assert [run.returncode for run in runs] == [0, 0, 0], outputs
    for out, _ in outputs:
        assert json.loads(out)['decision'] == 'block'
    assert not list((repo / 'konstruct-out' / '.hook').glob('.graph.json.*'))


@pytest.mark.dbt
def test_an_unchanged_tree_reuses_the_last_answer(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('CLAUDE_PROJECT_DIR', raising=False)
    staging = repo / 'models' / 'staging' / 'stg_payments.sql'
    staging.write_text(staging.read_text().replace('amount / 100 as amount', 'amount / 100 as amount_aud'))
    first = hook.post_edit(event(repo, staging))
    assert first is not None and first['decision'] == 'block'
    builds: list[object] = []
    monkeypatch.setattr('konstruct.interfaces.project.Project.build', lambda *a, **k: builds.append(a))
    again = hook.post_edit(event(repo, staging))
    assert again == first and builds == []
    # Another edit changes what the hook builds from, so it builds again.
    staging.write_text(staging.read_text() + '\n-- note\n')
    with contextlib.suppress(Exception):  # the stand-in build leaves the old graph to gate
        hook.post_edit(event(repo, staging))
    assert builds


def test_the_lock_wait_leaves_time_to_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = tmp_path / 'konstruct-out' / '.hook'
    scratch.mkdir(parents=True)
    (scratch / 'last.json').write_text(json.dumps({'seconds': hook.TIMEOUT}))
    waits: list[float] = []

    @contextlib.contextmanager
    def locked(directory: Path, seconds: float) -> Iterator[bool]:
        waits.append(seconds)
        yield False

    monkeypatch.setattr(hook, '_locked', locked)
    answer = hook._gate(tmp_path, tmp_path / 'konstruct-out', tmp_path / 'a.sql', 'a.sql')
    assert waits == [0.0]
    context = answer['hookSpecificOutput']['additionalContext']
    assert 'another hook is still running the gate' in context and context.endswith(f'({UNTRUSTED_CONTENT})')


def test_a_busy_hook_directory_gives_a_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = tmp_path / 'konstruct-out' / '.hook'
    scratch.mkdir(parents=True)
    with hook._locked(scratch, 1.0) as first:
        assert first
        # Another process holding the lock: this one gives up after the wait.
        monkeypatch.setattr(hook, '_try_lock', lambda file: False)
        with hook._locked(scratch, seconds=0.3) as second:
            assert second is False


def test_any_failure_is_a_note_not_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(event: dict[str, Any], *, remind: bool = False) -> None:
        raise RuntimeError('disk on fire')

    monkeypatch.setattr(hook, 'post_edit', broken)
    out = io.StringIO()
    assert hook.main('post-edit', stdin=io.StringIO(json.dumps(event(tmp_path, 'a.sql'))), stdout=out) == 0
    context = json.loads(out.getvalue())['hookSpecificOutput']['additionalContext']
    assert 'could not run the gate' in context and 'RuntimeError: disk on fire' in context
    assert context.endswith(f'({UNTRUSTED_CONTENT})')


def test_other_files_are_decided_before_any_graph_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no(*args: Any) -> None:
        raise AssertionError('looked for a graph')

    monkeypatch.setattr(hook, 'find_graph', no)
    assert hook.post_edit(event(tmp_path, tmp_path / 'notes.txt')) is None
    assert hook.post_edit(event(tmp_path, tmp_path / 'dags' / 'daily.py')) is None


@pytest.mark.dbt
def test_a_project_in_a_subdirectory_is_checked_again_after_each_edit(
    jaffle_built_graph: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv('CLAUDE_PROJECT_DIR', raising=False)
    top = tmp_path / 'monorepo'
    project = top / 'analytics'
    shutil.copytree(jaffle_built_graph, project)
    (top / '.gitignore').write_text('konstruct-out/\ntarget/\nlogs/\n*.duckdb*\n.user.yml\n')
    git(top, 'init', '-q', '-b', 'main')
    git(top, 'add', '-A')
    git(top, 'commit', '-q', '-m', 'Example')
    staging = project / 'models' / 'staging' / 'stg_payments.sql'
    original = staging.read_text()
    # Two edits of the same size, the second given the first's modification time: only their
    # content tells them apart.
    breaking = original.replace('amount / 100 as amount', 'amount / 100 as amounz')
    harmless = original.replace('stored in cents', 'stored in pence')
    assert len(breaking) == len(harmless) == len(original) and harmless != original
    staging.write_text(breaking)
    first = hook.post_edit(event(project, staging))
    assert first is not None and first['decision'] == 'block'
    stamp = staging.stat()
    staging.write_text(harmless)
    os.utime(staging, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    second = hook.post_edit(event(project, staging))
    assert second is not None and 'decision' not in second, second
    assert 'gate passed' in second['hookSpecificOutput']['additionalContext']


def test_the_fingerprint_reads_content_from_the_repositorys_top_level(tmp_path: Path) -> None:
    top = tmp_path / 'repo'
    project = top / 'project'
    (project / 'models').mkdir(parents=True)
    model = project / 'models' / 'a.sql'
    model.write_text('select 1 as x')
    git(top, 'init', '-q', '-b', 'main')
    git(top, 'add', '-A')
    git(top, 'commit', '-q', '-m', 'Base')
    model.write_text('select 2 as x')
    stamp = model.stat()
    first = hook.fingerprint(project)
    model.write_text('select 3 as x')
    os.utime(model, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert hook.fingerprint(project) != first
    # A manifest at any depth, at the target-path its dbt project names, counts too.
    nested = project / 'dbt' / 'shop'
    (nested / 'out').mkdir(parents=True)
    (nested / 'dbt_project.yml').write_text('name: shop\ntarget-path: out\n')
    git(top, 'add', '-A')
    git(top, 'commit', '-q', '-m', 'A dbt project')
    (nested / 'out' / 'manifest.json').write_text('{"a": 1}')
    before = hook.fingerprint(project)
    (nested / 'out' / 'manifest.json').write_text('{"a": 2}')
    assert hook.fingerprint(project) != before
    assert hook.fingerprint(tmp_path / 'not-a-repository') is None


def test_yaml_at_the_configured_definitions_and_ossie_paths_counts(tmp_path: Path) -> None:
    (tmp_path / 'konstruct.toml').write_text(
        '[definitions]\npaths = ["meta/terms.yml"]\n\n[ossie]\npaths = ["semantic"]\n'
    )
    paths = hook.data_paths(tmp_path)
    assert hook.is_data_file(tmp_path / 'meta' / 'terms.yml', tmp_path, paths)
    assert hook.is_data_file(tmp_path / 'semantic' / 'shop.yaml', tmp_path, paths)
    assert not hook.is_data_file(tmp_path / 'meta' / 'other.yml', tmp_path, paths)
    assert not hook.is_data_file(tmp_path / 'meta' / 'terms.yml', tmp_path)
    (tmp_path / 'konstruct.toml').write_text('[broken\n')
    assert hook.data_paths(tmp_path) == []
