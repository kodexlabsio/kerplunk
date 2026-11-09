from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from konstruct import __version__
from konstruct.interfaces.cli import find_out_dir, main
from konstruct.interfaces.console import Console, colour_enabled


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = main(['--no-color', *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.dbt
def test_build_prints_what_it_found(
    jaffle: Path, dbt_on_path: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, 'build', str(jaffle))
    assert code == 0
    assert 'building' in err
    lines = out.splitlines()
    assert lines[0] == f'konstruct  {jaffle}'
    assert (
        '  dbt        jaffle_shop (.)  5 models, 3 seeds, 20 tests, 1 exposure  (ran dbt docs generate)'
        in lines
    )
    assert '  duckdb     duckdb jaffle_shop.duckdb  9 relations  (dbt profile jaffle_shop)' in lines
    assert '  airflow    dags/jaffle_shop_daily.py  1 DAG, 6 tasks' in lines
    assert '  lineage    32 of 32 columns of models and built tables (100%)' in lines
    assert '  warnings   none' in lines
    assert any(
        line.startswith('  wrote      graph.json, REPORT.md, graph.html, ontology.yml to ') for line in lines
    )

    code, out, _ = run(capsys, 'build', str(jaffle), '--update')
    assert code == 0
    assert '  reused     7 of 7 cached analyses' in out.splitlines()

    code, out, _ = run(capsys, 'build', '-C', str(jaffle), '--json', '-u')
    data = json.loads(out)
    assert data['nodes']['model'] == 5
    assert data['lineage'] == {'covered': 32, 'columns': 32}
    assert data['cache'] == {'hits': 7, 'misses': 0}
    assert {i['input'] for i in data['inputs']} == {'dbt', 'duckdb', 'airflow', 'definitions'}


@pytest.mark.dbt
def test_query_commands(jaffle_built_graph: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = str(jaffle_built_graph)
    code, out, _ = run(capsys, 'impact', 'stg_payments.amount', '-C', project)
    assert code == 0
    assert out.splitlines()[:2] == [
        'Downstream of stg_payments.amount (model)',
        '7 columns, 3 relations, 5 tests, 1 exposure, 2 tasks and 2 business terms downstream.',
    ]
    code, out, _ = run(capsys, '-C', project, 'trace', 'orders.amount', '--json')
    assert code == 0 and json.loads(out)['direction'] == 'upstream'
    code, out, _ = run(capsys, 'path', 'raw_orders', 'orders', '--project', project)
    assert code == 0 and '-feeds-> orders (model)' in out
    code, out, _ = run(capsys, 'search', 'lifetime', 'value', '-C', project, '-n', '1')
    assert code == 0 and out.startswith('Customer lifetime value')
    graph_file = str(jaffle_built_graph / 'konstruct-out' / 'graph.json')
    code, out, _ = run(capsys, 'describe', 'customers', '--graph', graph_file)
    assert code == 0 and out.startswith('customers (model)')
    code, out, _ = run(capsys, 'list', 'models', '-C', project)
    assert code == 0 and out.splitlines()[0].startswith('customers')
    code, out, _ = run(capsys, 'list', 'tasks', 'dags', '-C', project, '--json')
    assert {i['kind'] for i in json.loads(out)['items']} == {'task', 'dag'}
    code, _, err = run(capsys, 'list', 'widgets', '-C', project)
    assert code == 2 and "unknown kind 'widgets'" in err
    code, out, _ = run(capsys, 'query', 'which', 'models', 'use', 'payments', '-C', project)
    assert code == 0 and 'stg_payments' in out
    code, out, _ = run(capsys, 'open', '--print-only', '-C', project)
    assert code == 0 and out.strip().endswith('konstruct-out/graph.html')
    code, _, err = run(capsys, 'impact', 'nope', '-C', project)
    assert code == 1 and "nothing in the graph matches 'nope'" in err


def test_path_without_a_connection_exits_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / 'a.sql').write_text('create table b as select x from a; create table d as select y from c')
    code, out, _ = run(capsys, 'path', 'b', 'd', '-C', str(tmp_path))
    assert code == 3 and out.startswith('No path between b and d')


def test_query_commands_build_a_missing_graph(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / 'x.sql').write_text('create table t2 as select a + 1 as b from t1')
    code, _, err = run(capsys, 'impact', 'x', '-C', str(tmp_path), '--no-build')
    assert code == 1 and 'run `konstruct build` first' in err
    code, out, err = run(capsys, 'trace', 't2.b', '-C', str(tmp_path))
    assert code == 0
    assert 'no graph yet; building' in err
    assert 't1.a  [table, transformed' in out


def test_stale_graphs_are_reported_and_refreshed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sql = tmp_path / 'x.sql'
    sql.write_text('create table t2 as select a from t1')
    assert run(capsys, 'build', str(tmp_path))[0] == 0
    sql.write_text('create table t2 as select a, c from t1')
    later = time.time() + 5
    os.utime(sql, (later, later))
    code, out, err = run(capsys, 'describe', 't2', '-C', str(tmp_path))
    assert code == 0 and 'older than 1 changed input(s) (x.sql)' in err
    assert 'Columns (1)' in out
    code, out, err = run(capsys, 'describe', 't2', '-C', str(tmp_path), '--refresh')
    assert 'updating the graph' in err
    assert 'Columns (2)' in out


def test_build_options_and_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / 'x.sql').write_text('create table t2 as select a from t1')
    out_dir = tmp_path / 'o'
    code, _, _ = run(
        capsys,
        'build',
        str(tmp_path),
        '--no-dbt',
        '--no-warehouse',
        '--dialect',
        'postgres',
        '--out',
        str(out_dir),
    )
    assert code == 0
    assert json.loads((out_dir / 'graph.json').read_text())['meta']['dialect'] == 'postgres'
    code, _, err = run(capsys, 'build', str(tmp_path / 'missing'))
    assert code == 1 and 'is not a directory' in err
    (tmp_path / 'bad.sql').write_text('select from where (')
    code, out, _ = run(capsys, 'build', str(tmp_path), '--strict')
    assert code == 1 and '  warnings   1' in out
    (tmp_path / 'konstruct.toml').write_text("[[warehouse]]\ntype = 'oracle'\n")
    code, _, err = run(capsys, 'build', str(tmp_path))
    assert code == 1 and "unknown warehouse type 'oracle'" in err
    (tmp_path / 'konstruct.toml').write_text('not = [toml')
    code, _, err = run(capsys, 'build', str(tmp_path))
    assert code == 1 and 'cannot read' in err


def test_empty_project(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, 'build', str(tmp_path))
    assert code == 0
    assert '  inputs     none found: no dbt project, DAG, SQL file or warehouse' in out


@pytest.mark.dbt
def test_status_and_init(
    built_jaffle: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    jaffle = tmp_path / 'jaffle_shop'
    shutil.copytree(built_jaffle, jaffle)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    code, out, _ = run(capsys, 'status', '-C', str(jaffle))
    assert code == 0
    assert '  dbt          jaffle_shop (.): no target/manifest.json yet' in out
    assert '  warehouse    duckdb jaffle_shop.duckdb (dbt profile jaffle_shop)' in out
    assert '  sql          no SQL files found' in out
    assert '  graph        not built yet; run `konstruct build`' in out
    assert '  skill        not installed; run `konstruct install`' in out
    code, out, _ = run(capsys, 'status', '-C', str(jaffle), '--json')
    assert json.loads(out)['dag_files'] == ['dags/jaffle_shop_daily.py']

    code, out, _ = run(capsys, 'init', '-C', str(jaffle))
    assert code == 0 and 'wrote' in out
    text = (jaffle / 'konstruct.toml').read_text()
    assert 'projects = ["."]' in text and 'dags = ["dags"]' in text and 'path = "jaffle_shop.duckdb"' in text
    code, _, err = run(capsys, 'init', '-C', str(jaffle))
    assert code == 1 and 'already exists' in err
    assert run(capsys, 'init', '-C', str(jaffle), '--force')[0] == 0
    code, out, _ = run(capsys, 'init', '-C', str(jaffle), '--stdout')
    assert out.startswith('# Konstruct configuration')
    assert '[spark]\n# paths = ["jobs/"]\n# dialect = "spark"' in out
    assert '[looker]\n# paths = ["lookml/"]' in out
    code, out, _ = run(capsys, 'status', '-C', str(jaffle))
    assert '  config       konstruct.toml' in out
    run(capsys, 'install', '--target', str(jaffle))
    code, out, _ = run(capsys, 'status', '-C', str(jaffle))
    assert '  skill        project (Claude Code and Cursor)' in out


def test_find_out_dir_looks_upwards(tmp_path: Path) -> None:
    (tmp_path / 'konstruct-out').mkdir()
    (tmp_path / 'konstruct-out' / 'graph.json').write_text('{}')
    nested = tmp_path / 'a' / 'b'
    nested.mkdir(parents=True)
    assert find_out_dir(nested) == tmp_path / 'konstruct-out'
    assert find_out_dir(tmp_path / 'konstruct-out') == tmp_path / 'konstruct-out'


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys)
    assert code == 2 and 'usage: konstruct' in out and 'examples:' in out


def test_colour(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv('FORCE_COLOR', '1')
    monkeypatch.delenv('NO_COLOR', raising=False)
    assert Console().style('x', 'bold') == '\x1b[1mx\x1b[0m'
    monkeypatch.setenv('NO_COLOR', '1')
    assert not colour_enabled(sys.stdout)
    assert Console(colour=False).style('x', 'red') == 'x'
    Console(quiet=True).info('hidden')
    Console(colour=False).warn('shown')
    assert capsys.readouterr().err == 'konstruct: shown\n'


def test_version_and_help() -> None:
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}

    def cli(*args: str) -> str:
        return subprocess.run(
            [sys.executable, '-m', 'konstruct.interfaces.cli', *args],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        ).stdout

    assert cli('--version').strip() == f'konstruct {__version__}'
    help_text = cli('--help')
    for command in (
        'build',
        'status',
        'init',
        'query',
        'impact',
        'trace',
        'path',
        'search',
        'describe',
        'list',
        'open',
        'install',
        'mcp',
    ):
        assert f'    {command}' in help_text
    assert '--refresh' in cli('impact', '--help') and '-C DIR' in cli('impact', '--help')


def test_verify_needs_a_duckdb_warehouse(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / 'x.sql').write_text('create table t2 as select a from t1')
    code, _, err = run(capsys, 'verify', '-C', str(tmp_path))
    assert (
        code == 1
        and 'needs the models built in a DuckDB file or a Postgres database' in err
        and '--warehouse snowflake, bigquery, databricks or trino' in err
    )


def test_status_lists_every_input_and_the_packs_bound(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import duckdb

    monkeypatch.setenv('HOME', str(tmp_path))
    con = duckdb.connect(str(tmp_path / 'w.duckdb'))
    con.execute('create schema stripe')
    con.execute(
        'create table stripe.charge (id varchar, amount bigint, amount_refunded bigint, captured boolean, '
        'paid boolean, currency varchar, livemode boolean)'
    )
    con.execute('create table stripe.customer (id varchar, delinquent boolean, livemode boolean)')
    con.close()
    (tmp_path / 'assets.py').write_text('from dagster import asset\n\n@asset\ndef a():\n    pass\n')
    (tmp_path / 'shop.ossie.yaml').write_text('version: "0.2.0.dev0"\nname: shop\ndatasets: []\n')
    (tmp_path / 'terms.ttl').write_text('')
    (tmp_path / 'vocabdir').mkdir()
    other = tmp_path.parent / f'{tmp_path.name}-other'  # beside the project, not inside it
    other.mkdir()
    (other / 'assets.py').write_text('from dagster import asset\n\n@asset\ndef b():\n    pass\n')
    (tmp_path / 'konstruct.toml').write_text(
        f'repositories = ["../{other.name}"]\n\n[vocabularies]\npaths = ["terms.ttl", "vocabdir"]\n'
    )
    code, out, _ = run(capsys, 'status', '-C', str(tmp_path))
    assert code == 0
    assert '  dagster      1 Dagster file: assets.py' in out
    assert '  ossie        1 Ossie model: shop.ossie.yaml' in out
    assert '  vocabularies 1 vocabulary file: terms.ttl' in out  # a directory is not one
    assert f'repository   {other.name} (../{other.name}): 1 Dagster file' in out
    assert 'packs' not in out  # nothing built yet
    run(capsys, 'build', '-C', str(tmp_path))
    code, out, _ = run(capsys, 'status', '-C', str(tmp_path), '--json')
    assert json.loads(out)['ontology_packs'] == [{'name': 'stripe', 'detail': 'w.stripe: 4 terms'}]
    code, out, _ = run(capsys, 'status', '-C', str(tmp_path))
    assert '  packs        stripe (w.stripe: 4 terms)' in out


def test_status_shows_what_the_last_build_read_from_the_warehouse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'x.sql').write_text('create table t2 as select a from t1')
    run(capsys, 'build', '-C', str(tmp_path))
    path = tmp_path / 'konstruct-out' / 'graph.json'
    data = json.loads(path.read_text())
    data['meta']['sources'] += [
        {'input': 'fivetran', 'name': 'stripe', 'detail': '12 tables'},
        {'input': 'usage', 'path': 'snowflake', 'detail': '12 columns queried in the last 90 days'},
    ]
    path.write_text(json.dumps(data))
    code, out, _ = run(capsys, 'status', '-C', str(tmp_path))
    assert code == 0
    assert '  fivetran     stripe: 12 tables' in out
    assert '  usage        snowflake: 12 columns queried in the last 90 days' in out
    code, out, _ = run(capsys, 'status', '-C', str(tmp_path), '--json')
    assert code == 0
    assert json.loads(out)['built_from'] == [
        {'input': 'fivetran', 'name': 'stripe', 'detail': '12 tables'},
        {'input': 'usage', 'name': 'snowflake', 'detail': '12 columns queried in the last 90 days'},
    ]
