from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import settings

# HYPOTHESIS_PROFILE=ci runs more examples; the default keeps local runs quick.
settings.register_profile('ci', max_examples=400)
settings.register_profile('dev', max_examples=60)
settings.load_profile(os.environ.get('HYPOTHESIS_PROFILE', 'dev'))

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / 'examples' / 'jaffle_shop'


@pytest.fixture(autouse=True, scope='session')
def _private_trust(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Trust records and the gate's graphs of base commits go to private directories, and no test
    inherits KONSTRUCT_TRUST_CONFIG."""
    os.environ['XDG_CONFIG_HOME'] = str(tmp_path_factory.mktemp('config'))
    os.environ['XDG_CACHE_HOME'] = str(tmp_path_factory.mktemp('cache'))
    os.environ.pop('KONSTRUCT_TRUST_CONFIG', None)


@pytest.fixture
def trust_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trust every project, as CI on a trusted branch does: its connections, its paths and running dbt."""
    monkeypatch.setenv('KONSTRUCT_TRUST_CONFIG', '1')


def dbt_executable() -> str | None:
    candidate = Path(sys.executable).parent / 'dbt'
    if candidate.is_file():
        return str(candidate)
    return shutil.which('dbt')


def copy_example(destination: Path) -> Path:
    project = destination / 'jaffle_shop'
    shutil.copytree(
        EXAMPLE,
        project,
        ignore=shutil.ignore_patterns('target', 'logs', 'konstruct-out', '*.duckdb', '*.duckdb.wal'),
    )
    return project


def run_dbt(project: Path, *args: str) -> None:
    dbt = dbt_executable()
    assert dbt is not None
    env = {**os.environ, 'DBT_PROFILES_DIR': str(project), 'DBT_SEND_ANONYMOUS_USAGE_STATS': 'false'}
    env['PATH'] = f'{Path(dbt).parent}{os.pathsep}{env.get("PATH", "")}'
    subprocess.run([dbt, *args, '--quiet'], cwd=project, env=env, check=True)


@pytest.fixture(scope='session')
def built_jaffle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A copy of the jaffle_shop example with its DuckDB warehouse built and the reporting table
    created, but no dbt artefacts: konstruct has to produce those itself."""
    if dbt_executable() is None:
        pytest.skip('dbt is not installed')
    project = copy_example(tmp_path_factory.mktemp('jaffle'))
    run_dbt(project, 'build')
    import duckdb

    con = duckdb.connect(str(project / 'jaffle_shop.duckdb'))
    try:
        con.execute((project / 'dags' / 'sql' / 'customer_ltv.sql').read_text())
    finally:
        con.close()
    shutil.rmtree(project / 'target')
    return project


@pytest.fixture(scope='session')
def jaffle_with_artefacts(built_jaffle: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The built example with dbt artefacts (manifest and catalog) generated. Read only."""
    project = tmp_path_factory.mktemp('jaffle_artefacts') / 'jaffle_shop'
    shutil.copytree(built_jaffle, project)
    run_dbt(project, 'docs', 'generate')
    return project


@pytest.fixture
def jaffle(built_jaffle: Path, tmp_path: Path) -> Path:
    """A private copy of the built example, so a test can change files freely."""
    project = tmp_path / 'jaffle_shop'
    shutil.copytree(built_jaffle, project)
    return project


@pytest.fixture
def dbt_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """dbt on PATH, and the project trusted, so that Konstruct runs dbt."""
    dbt = dbt_executable()
    if dbt is None:
        pytest.skip('dbt is not installed')
    monkeypatch.setenv('PATH', f'{Path(dbt).parent}{os.pathsep}{os.environ.get("PATH", "")}')
    monkeypatch.setenv('DBT_SEND_ANONYMOUS_USAGE_STATS', 'false')
    monkeypatch.setenv('KONSTRUCT_TRUST_CONFIG', '1')


@pytest.fixture(scope='session')
def jaffle_built_graph(built_jaffle: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The example after a full `konstruct build`: the project root, with konstruct-out inside."""
    from konstruct.engine.build import build
    from konstruct.engine.config import load_config

    dbt = dbt_executable()
    assert dbt is not None
    project = tmp_path_factory.mktemp('jaffle_graph') / 'jaffle_shop'
    shutil.copytree(built_jaffle, project)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('PATH', f'{Path(dbt).parent}{os.pathsep}{os.environ.get("PATH", "")}')
        patch.setenv('DBT_SEND_ANONYMOUS_USAGE_STATS', 'false')
        build(load_config(project, trusted=True))  # the example is the test suite's own
    return project
