"""The release workflow's decision (.kodex/scripts/plan_release.sh), run against throwaway
repositories with a bare origin."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / '.kodex' / 'scripts'
ENV = {
    'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_AUTHOR_NAME': 't',
    'GIT_AUTHOR_EMAIL': 't@example.com',
    'GIT_COMMITTER_NAME': 't',
    'GIT_COMMITTER_EMAIL': 't@example.com',
}


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, **ENV}
    return subprocess.run(
        ['git', *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout


def set_version(repo: Path, version: str) -> None:
    (repo / 'pyproject.toml').write_text(f'[project]\nname = "konstruct"\nversion = "{version}"\n')
    (repo / 'uv.lock').write_text(f'version = 1\n\n[[package]]\nname = "konstruct"\nversion = "{version}"\n')
    (repo / 'konstruct' / '__init__.py').write_text(f"__version__ = '{version}'\n")


def commit(repo: Path, message: str, name: str | None = None) -> None:
    if name:
        (repo / name).write_text(message)
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """main at a released v0.11.1, with an origin to push tags to."""
    origin = tmp_path / 'origin.git'
    work = tmp_path / 'work'
    (work / 'konstruct').mkdir(parents=True)
    (work / '.kodex' / 'scripts').mkdir(parents=True)
    for script in ('prepare_release.py', 'check_commit_message.py', 'plan_release.sh'):
        shutil.copy(SCRIPTS / script, work / '.kodex' / 'scripts' / script)
    set_version(work, '0.11.1')
    (work / 'CHANGELOG.md').write_text('## v0.11.1 (2026-11-08)\n\n### Fix\n\n- Older fix\n')
    git(tmp_path, 'init', '-q', '--bare', str(origin))
    git(work, 'init', '-q', '-b', 'main')
    git(work, 'remote', 'add', 'origin', str(origin))
    commit(work, 'chore: Start')
    git(work, 'tag', 'v0.11.1')
    git(work, 'tag', 'v0', 'v0.11.1')
    return work


def plan(repo: Path, released: str, event: str = 'workflow_run') -> tuple[dict[str, str], str]:
    output = repo.parent / 'output'
    output.write_text('')
    env = {
        **os.environ,
        **ENV,
        'GITHUB_OUTPUT': str(output),
        'GITHUB_EVENT_NAME': event,
        'RUNNER_TEMP': str(repo.parent),
        'KONSTRUCT_RELEASED_TAGS': released,
    }
    done = subprocess.run(
        ['bash', '.kodex/scripts/plan_release.sh'], cwd=repo, env=env, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stdout + done.stderr
    values = dict(line.split('=', 1) for line in output.read_text().splitlines())
    return values, done.stdout


def notes(repo: Path) -> str:
    return (repo.parent / 'RELEASE_NOTES.md').read_text()


def test_a_prepared_version_is_tagged_on_main(repo: Path) -> None:
    set_version(repo, '0.11.2')
    changelog = (repo / 'CHANGELOG.md').read_text()
    (repo / 'CHANGELOG.md').write_text(
        '## v0.11.2 (2026-11-09)\n\n### Fix\n\n- **sql**: Order sources\n\n' + changelog
    )
    commit(repo, 'fix(sql): Order sources')
    head = git(repo, 'rev-parse', 'HEAD')
    values, _ = plan(repo, 'v0.11.1')
    assert values == {'released': 'true', 'tag': 'v0.11.2'}
    assert git(repo, 'rev-parse', 'HEAD') == head
    assert notes(repo) == '## v0.11.2 (2026-11-09)\n\n### Fix\n\n- **sql**: Order sources\n\n'


def test_an_unprepared_fix_is_released_from_above_main(repo: Path) -> None:
    commit(repo, 'fix(deps): Bump duckdb', 'deps.txt')
    main = git(repo, 'rev-parse', 'HEAD')
    values, out = plan(repo, 'v0.11.1')
    assert values == {'released': 'true', 'tag': 'v0.11.2'}
    assert 'from a commit above main' in out
    assert git(repo, 'rev-parse', 'HEAD^') == main
    assert git(repo, 'log', '-1', '--format=%s') == 'chore(release): Release 0.11.2\n'
    assert '- **deps**: Bump duckdb' in notes(repo)
    assert 'version = "0.11.2"' in (repo / 'pyproject.toml').read_text()


def test_nothing_to_release_puts_v0_right(repo: Path) -> None:
    commit(repo, 'docs: Explain', 'docs.txt')
    git(repo, 'push', '-q', 'origin', 'main', 'v0.11.1')
    git(repo, 'tag', '-f', 'v0', 'HEAD')
    values, out = plan(repo, 'v0.11.1')
    assert values == {'released': 'false'}
    assert 'Nothing to release since v0.11.1' in out
    assert git(repo, 'rev-parse', 'v0^{commit}') == git(repo, 'rev-parse', 'v0.11.1^{commit}')


def test_a_half_done_release_is_finished(repo: Path) -> None:
    commit(repo, 'fix: More', 'more.txt')
    values, out = plan(repo, '')
    assert values == {'released': 'true', 'tag': 'v0.11.1'}
    assert 'finishing it' in out


def test_a_run_by_hand_releases_nothing_unprepared(repo: Path) -> None:
    commit(repo, 'fix: More', 'more.txt')
    values, _ = plan(repo, 'v0.11.1', event='workflow_dispatch')
    assert values == {'released': 'false'}


def test_a_stale_prepared_version_never_releases_below_the_latest(repo: Path) -> None:
    # A pull request prepared v0.11.2, but meanwhile a breaking change was released as v0.12.0
    # from a commit above main.
    commit(repo, 'refactor(graph)!: Rename nodes', 'graph.txt')
    plan(repo, 'v0.11.1')
    git(repo, 'tag', 'v0.12.0')
    git(repo, 'checkout', '-q', 'main')
    set_version(repo, '0.11.2')
    changelog = (repo / 'CHANGELOG.md').read_text()
    (repo / 'CHANGELOG.md').write_text('## v0.11.2 (2026-11-09)\n\n### Feat\n\n- Add a thing\n\n' + changelog)
    commit(repo, 'feat: Add a thing')
    values, out = plan(repo, 'v0.11.1 v0.12.0')
    assert values == {'released': 'true', 'tag': 'v0.12.1'}
    assert 'older than v0.12.0' in out
    changelog = (repo / 'CHANGELOG.md').read_text()
    headings = [line.split(' (')[0] for line in changelog.splitlines() if line.startswith('## ')]
    assert headings == ['## v0.12.1', '## v0.12.0', '## v0.11.1']
    assert '- Add a thing' in changelog.split('## v0.12.0')[0]
    assert '- **graph**: Rename nodes' in changelog.split('## v0.12.0')[1]


def test_refuses_1_0(repo: Path) -> None:
    set_version(repo, '1.0.0')
    (repo / 'CHANGELOG.md').write_text('## v1.0.0 (2026-11-09)\n\n### Feat\n\n- Stable\n')
    commit(repo, 'feat: Stable')
    output = repo.parent / 'output'
    output.write_text('')
    env = {**os.environ, **ENV, 'GITHUB_OUTPUT': str(output), 'RUNNER_TEMP': str(repo.parent)}
    env['KONSTRUCT_RELEASED_TAGS'] = 'v0.11.1'
    done = subprocess.run(
        ['bash', '.kodex/scripts/plan_release.sh'], cwd=repo, env=env, capture_output=True, text=True
    )
    assert done.returncode == 1
    assert 'refusing to release v1.0.0' in done.stdout
