"""Measure the lineage lockfile on a generated producer and a consumer that reads it.

The producer is the dbt project tests/test_scale.py builds (models in layers, each joining two
earlier models through CTEs, with a filter). The consumer is a separate git repository of plain
SQL that lists the producer in ``repositories`` and reads every column of every tenth model. For
each size it records:

* the consumer's build, which builds and merges the producer;
* ``lock``, writing every pin, and ``lock`` again with nothing changed;
* ``check`` with nothing changed, without and with git history;
* ``check`` after a commit to one model in the middle, with git history, and how many pins fail;
* ``check_consumers`` run in the producer, which also lists the consumer;
* the size of konstruct.lock.

Usage:
    uv run .kodex/scripts/benchmark_lock.py              # 500 and 2000 models
    uv run .kodex/scripts/benchmark_lock.py 2000 --json results.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from konstruct import Project, __version__  # noqa: E402
from konstruct.analysis.lock import FILE_NAME, check, check_consumers, lock  # noqa: E402
from tests.test_scale import make_project  # noqa: E402

COLUMNS = 20
EVERY = 10


def git(root: Path, *args: str) -> None:
    env = {**os.environ, 'GIT_AUTHOR_NAME': 'Ada', 'GIT_AUTHOR_EMAIL': 'ada@example.com'}
    env |= {'GIT_COMMITTER_NAME': 'Ada', 'GIT_COMMITTER_EMAIL': 'ada@example.com'}
    subprocess.run(['git', '-C', str(root), '-c', 'commit.gpgsign=false', *args], check=True, env=env)


def timed(func: Any, *args: Any, **kwargs: Any) -> tuple[float, Any]:
    started = time.perf_counter()
    result = func(*args, **kwargs)
    return round(time.perf_counter() - started, 2), result


def build(root: Path) -> Any:
    return Project(root).build(run_dbt=False, warehouses=False).graph


def measure(models: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix='konstruct-lock-bench-') as directory:
        producer, consumer = Path(directory) / 'producer', Path(directory) / 'consumer'
        make_project(producer, models=models, columns_per_model=COLUMNS)
        (producer / 'konstruct.toml').write_text('repositories = ["../consumer"]\n')
        consumer.mkdir()
        columns = ', '.join(f'c{j:02}' for j in range(COLUMNS))
        for index in range(0, models, EVERY):
            (consumer / f'd{index:04}.sql').write_text(
                f'create table wh.dash.d{index:04} as select {columns} from wh.analytics.m{index:03}'
            )
        (consumer / 'konstruct.toml').write_text('dialect = "duckdb"\nrepositories = ["../producer"]\n')
        for root in (producer, consumer):
            git(root, 'init', '-q', '-b', 'main')
            git(root, 'add', '-A')
            git(root, 'commit', '-q', '-m', 'Start')
        consumer_build, graph = timed(build, consumer)
        first, written = timed(lock, graph, consumer)
        again, _ = timed(lock, graph, consumer)
        git(consumer, 'add', '-A')
        git(consumer, 'commit', '-q', '-m', 'Lock')
        unchanged, passed = timed(check, graph, consumer, history=False)
        unchanged_history, _ = timed(check, graph, consumer)
        assert passed.passed, str(passed)[:2000]
        middle = producer / 'models' / f'm{models // 2:03}.sql'
        middle.write_text(middle.read_text().replace('where b.c01 > 0', 'where b.c01 > 1'))
        manifest = producer / 'target' / 'manifest.json'
        data = json.loads(manifest.read_text())
        node = data['nodes'][f'model.big.m{models // 2:03}']
        node['compiled_code'] = node['raw_code'] = middle.read_text()
        node['checksum']['checksum'] = 'edited'
        manifest.write_text(json.dumps(data))
        git(producer, 'commit', '-q', '-am', 'Filter on c01 > 1')
        changed_graph = build(consumer)
        edited, failed = timed(check, changed_graph, consumer)
        producer_build, producer_graph = timed(build, producer)
        consumers, results = timed(check_consumers, producer_graph, producer, history=False)
        return {
            'models': models,
            'pins': len(written.lockfile.pins),
            'consumer_build_s': consumer_build,
            'lock_s': first,
            'lock_again_s': again,
            'check_s': unchanged,
            'check_history_s': unchanged_history,
            'check_after_edit_s': edited,
            'failing_pins': len(failed.changes),
            'producer_build_s': producer_build,
            'check_consumers_s': consumers,
            'consumer_failing_pins': sum(len(r.changes) for r in results),
            'lockfile_mb': round((consumer / FILE_NAME).stat().st_size / 1e6, 2),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('sizes', nargs='*', type=int, default=[500, 2000])
    parser.add_argument('--json', type=Path, help='also write the results as JSON')
    args = parser.parse_args()
    rows = []
    for size in args.sizes:
        row = measure(size)
        rows.append(row)
        print(f'{size} models: {row}', file=sys.stderr)
    machine = (
        f'{platform.machine()}, {platform.system()} {platform.release()}, Python {platform.python_version()}'
    )
    print(f'konstruct {__version__} on {machine}, {COLUMNS} columns per model, every {EVERY}th model read\n')
    keys = list(rows[0])
    print('| ' + ' | '.join(keys) + ' |')
    print('|' + '---:|' * len(keys))
    for row in rows:
        print('| ' + ' | '.join(str(row[k]) for k in keys) + ' |')
    if args.json:
        args.json.write_text(
            json.dumps({'konstruct': __version__, 'machine': machine, 'results': rows}, indent=1)
        )
    return 0


if __name__ == '__main__':
    sys.exit(main())
