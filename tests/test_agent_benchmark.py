"""The agent benchmark harness, on a miniature Spider 2.0-DBT tree with a scripted agent."""

from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
import time
from pathlib import Path
from types import ModuleType

import duckdb
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / '.kodex' / 'scripts' / 'agent_benchmark.py'

# The benchmark's evaluator compares chosen columns of chosen tables; this stand-in keeps that
# signature and compares whole rows, which is all the harness relies on.
EVAL_UTILS = """
import duckdb


def duckdb_match(result, gold, condition_tabs=None, condition_cols=None, ignore_orders=None):
    def rows(db, table):
        con = duckdb.connect(db, read_only=True)
        try:
            return sorted(con.execute(f'select * from {table}').fetchall())
        finally:
            con.close()

    return 1 if all(rows(result, t) == rows(gold, t) for t in condition_tabs) else 0
"""


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location('agent_benchmark', SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules['agent_benchmark'] = module
    spec.loader.exec_module(module)
    return module


def spider(root: Path) -> Path:
    """Two instances: shop001, which the scripted agent solves, and shop002, which it cannot."""
    tasks, golds = [], []
    for instance in ('shop001', 'shop002'):
        project = root / 'examples' / instance
        (project / 'models').mkdir(parents=True)
        (project / 'dbt_project.yml').write_text(f'name: {instance}\nprofile: shop\n')
        con = duckdb.connect(str(project / 'shop.duckdb'))
        con.execute('create table orders as select * from (values (1, 10), (2, 20)) t(id, amount)')
        con.close()
        gold = root / 'evaluation_suite' / 'gold' / instance
        gold.mkdir(parents=True)
        con = duckdb.connect(str(gold / 'shop.duckdb'))
        con.execute(
            'create table totals as select 30 as total'
            if instance == 'shop001'
            else 'create table totals as select 99 as total'
        )
        con.close()
        tasks.append({'instance_id': instance, 'instruction': 'Add a totals model.', 'type': 'DBT'})
        golds.append(
            {
                'instance_id': instance,
                'evaluation': {
                    'func': 'duckdb_match',
                    'parameters': {
                        'gold': 'shop.duckdb',
                        'condition_tabs': ['totals'],
                        'condition_cols': [[0]],
                        'ignore_orders': [True],
                    },
                },
            }
        )
    golds.append({'instance_id': 'other001', 'evaluation': {'func': 'table_match', 'parameters': {}}})
    (root / 'examples' / 'spider2-dbt.jsonl').write_text('\n'.join(json.dumps(t) for t in tasks) + '\n')
    (root / 'evaluation_suite' / 'gold' / 'spider2_eval.jsonl').write_text(
        '\n'.join(json.dumps(g) for g in golds) + '\n'
    )
    (root / 'evaluation_suite' / 'eval_utils.py').write_text(EVAL_UTILS)
    return root


def test_both_arms_are_run_and_scored(tmp_path: Path) -> None:
    root = spider(tmp_path / 'spider2-dbt')
    agent = tmp_path / 'agent.py'
    # The scripted agent records its prompt and what Konstruct left in the project, then builds
    # the totals table that the gold database of shop001 holds.
    agent.write_text(
        textwrap.dedent(
            """\
            import pathlib, sys, duckdb
            here = pathlib.Path('.')
            (here / 'seen.txt').write_text(sys.argv[1] + '\\n' + str((here / 'konstruct-out' / 'graph.json').is_file()))
            con = duckdb.connect('shop.duckdb')
            con.execute('create table totals as select sum(amount) as total from orders')
            con.close()
            """
        )
    )
    benchmark = load()
    out = tmp_path / 'runs'
    command = f'{sys.executable} {agent} {{prompt}}'
    assert benchmark.main([str(root), '--agent', command, '--out', str(out)]) == 0
    results = [json.loads(line) for line in (out / 'results.jsonl').read_text().splitlines()]
    assert [(r['instance_id'], r['arm'], r['score']) for r in results] == [
        ('shop001', 'baseline', 1.0),
        ('shop001', 'konstruct', 1.0),
        ('shop002', 'baseline', 0.0),
        ('shop002', 'konstruct', 0.0),
    ]
    baseline = (out / 'baseline' / 'shop001' / 'seen.txt').read_text()
    konstruct = (out / 'konstruct' / 'shop001' / 'seen.txt').read_text()
    # The same prompt in both arms, which does not mention Konstruct; only the konstruct arm has the graph.
    assert baseline.splitlines()[:-1] == konstruct.splitlines()[:-1]
    assert baseline.startswith('Add a totals model.') and 'onstruct' not in baseline.rsplit('\n', 1)[0]
    assert baseline.endswith('False') and konstruct.endswith('True')
    assert (out / 'konstruct' / 'shop001' / '.claude' / 'skills' / 'konstruct' / 'SKILL.md').is_file()
    summary = (out / 'summary.md').read_text()
    assert '| baseline | 1 | 2 | 50.0% |' in summary and '| shop002 | failed | failed |' in summary
    # A second run resumes: nothing is run again.
    agent.write_text('raise SystemExit(1)\n')
    assert benchmark.main([str(root), '--agent', command, '--out', str(out)]) == 0
    assert len((out / 'results.jsonl').read_text().splitlines()) == 4


def test_the_database_is_the_one_the_profile_writes(tmp_path: Path) -> None:
    root = spider(tmp_path / 'spider2-dbt')
    project = root / 'examples' / 'shop001'
    (project / 'shop.duckdb').rename(project / 'shop_dev.duckdb')
    (project / 'profiles.yml').write_text(
        'shop:\n  target: dev\n  outputs:\n    dev: {type: duckdb, path: ./shop_dev.duckdb}\n'
    )
    agent = tmp_path / 'agent.py'
    agent.write_text(
        'import duckdb, sys\n'
        "con = duckdb.connect('shop_dev.duckdb')\n"
        "con.execute('create table totals as select sum(amount) as total from orders')\n"
        'con.close()\n'
        "open('prompt.txt', 'w').write(sys.argv[1])\n"
    )
    benchmark = load()
    out = tmp_path / 'runs'
    command = f'{sys.executable} {agent} {{prompt}}'
    benchmark.main(
        [str(root), '--agent', command, '--out', str(out), '--only', 'shop001', '--arms', 'baseline']
    )
    [result] = [json.loads(line) for line in (out / 'results.jsonl').read_text().splitlines()]
    assert result['score'] == 1.0
    assert 'DuckDB (shop_dev.duckdb)' in (out / 'baseline' / 'shop001' / 'prompt.txt').read_text()


def test_a_missing_database_scores_zero(tmp_path: Path) -> None:
    root = spider(tmp_path / 'spider2-dbt')
    benchmark = load()
    out = tmp_path / 'runs'
    command = f'{sys.executable} -c "import pathlib; pathlib.Path(\'shop.duckdb\').unlink()" {{prompt}}'
    benchmark.main(
        [str(root), '--agent', command, '--out', str(out), '--only', 'shop001', '--arms', 'baseline']
    )
    [result] = [json.loads(line) for line in (out / 'results.jsonl').read_text().splitlines()]
    assert (result['score'], result['error']) == (0.0, 'no shop.duckdb after the run')


def test_a_timed_out_agent_is_stopped_with_what_it_started(tmp_path: Path) -> None:
    root = spider(tmp_path / 'spider2-dbt')
    benchmark = load()
    out = tmp_path / 'runs'
    marker = tmp_path / 'late.txt'
    stubborn = tmp_path / 'stubborn.sh'
    stubborn.write_text(f"trap '' TERM\nsleep 3\ntouch {tmp_path / 'stubborn.txt'}\n")
    # The agent starts a grandchild that would write the marker after the timeout, and another
    # that ignores SIGTERM.
    command = f"sh -c 'sleep 3 && touch {marker}' & sh {stubborn} & sleep 30 # {{prompt}}"
    benchmark.main(
        [
            str(root),
            '--agent',
            command,
            '--out',
            str(out),
            '--only',
            'shop001',
            '--arms',
            'baseline',
            '--timeout',
            '1',
        ]
    )
    [result] = [json.loads(line) for line in (out / 'results.jsonl').read_text().splitlines()]
    assert result['agent_exit'] is None
    time.sleep(4)
    assert not marker.exists() and not (tmp_path / 'stubborn.txt').exists()


def test_what_an_agent_leaves_running_is_stopped(tmp_path: Path) -> None:
    root = spider(tmp_path / 'spider2-dbt')
    benchmark = load()
    marker = tmp_path / 'late.txt'
    command = f'(sleep 3; touch {marker}) & exit 0 # {{prompt}}'
    benchmark.main(
        [
            str(root),
            '--agent',
            command,
            '--out',
            str(tmp_path / 'runs'),
            '--only',
            'shop001',
            '--arms',
            'baseline',
        ]
    )
    time.sleep(4)
    assert not marker.exists()


def test_with_mcp_both_arms_have_an_mcp_configuration(tmp_path: Path) -> None:
    root = spider(tmp_path / 'spider2-dbt')
    benchmark = load()
    out = tmp_path / 'runs'
    # An agent that refuses to start without the configuration it is given.
    checker = tmp_path / 'needs_mcp.py'
    checker.write_text('import json\njson.load(open(".mcp.json"))\n')
    command = f'{sys.executable} {checker} {{prompt}}'
    benchmark.main([str(root), '--agent', command, '--out', str(out), '--only', 'shop001', '--mcp'])
    results = [json.loads(line) for line in (out / 'results.jsonl').read_text().splitlines()]
    assert [(r['arm'], r['agent_exit']) for r in results] == [('baseline', 0), ('konstruct', 0)]
    servers = json.loads((out / 'konstruct' / 'shop001' / '.mcp.json').read_text())['mcpServers']
    assert 'konstruct' in servers and json.loads(
        (out / 'baseline' / 'shop001' / '.mcp.json').read_text()
    ) == {'mcpServers': {}}


def test_runs_that_an_agent_could_contaminate_are_refused(tmp_path: Path) -> None:
    root = spider(tmp_path / 'spider2-dbt')
    benchmark = load()
    command = f'{sys.executable} -c "" {{prompt}}'
    (tmp_path / 'repo' / '.git').mkdir(parents=True)
    with pytest.raises(SystemExit, match='whose instructions an agent would read'):
        benchmark.main([str(root), '--agent', command, '--out', str(tmp_path / 'repo' / 'runs')])
    with pytest.raises(SystemExit, match='must contain'):
        benchmark.main([str(root), '--agent', 'true', '--out', str(tmp_path / 'runs')])
    with pytest.raises(SystemExit, match='not instances scored on a DuckDB file: nope'):
        benchmark.main([str(root), '--agent', command, '--out', str(tmp_path / 'runs'), '--only', 'nope'])
    out = tmp_path / 'runs'
    benchmark.main(
        [str(root), '--agent', command, '--out', str(out), '--only', 'shop001', '--arms', 'baseline']
    )
    with pytest.raises(SystemExit, match='holds a run with other options'):
        benchmark.main([str(root), '--agent', command, '--out', str(out), '--dbt-run'])
    # Each copy is its own git repository, so lookups for instructions stop there.
    assert (out / 'baseline' / 'shop001' / '.git').is_dir()
