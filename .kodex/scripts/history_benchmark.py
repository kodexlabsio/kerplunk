"""Measure the commands that compare versions of a project: diff and release notes.

Generates the project tests/test_scale.py's make_project makes (dbt models in layers, each joining
two earlier ones), commits it, tags it, then commits a change to ``--changed`` models (a factor and
a filter in each), builds the graph and times, in one process:

* ``Project.diff`` between the tag and the change;
* ``Project.release_notes`` since the tag;
* ``Project.diff`` again with the check of column types uncached (``_safe_type.__wrapped__``), which
  is what each analysis did before the check was cached.

Usage:
    uv run .kodex/scripts/history_benchmark.py                 # 2,000 models, 100 changed
    uv run .kodex/scripts/history_benchmark.py --models 500 --changed 20 --json history.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from konstruct import Project  # noqa: E402
from konstruct.core import sql  # noqa: E402
from tests.test_scale import make_project  # noqa: E402


def git(root: Path, *args: str) -> None:
    env = {
        **os.environ,
        'GIT_AUTHOR_NAME': 'Benchmark',
        'GIT_AUTHOR_EMAIL': 'benchmark@example.com',
        'GIT_COMMITTER_NAME': 'Benchmark',
        'GIT_COMMITTER_EMAIL': 'benchmark@example.com',
    }
    subprocess.run(['git', '-C', str(root), '-c', 'commit.gpgsign=false', *args], check=True, env=env)


def timed(work: Callable[[], object]) -> float:
    started = time.perf_counter()
    work()
    return time.perf_counter() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--models', type=int, default=2000)
    parser.add_argument('--changed', type=int, default=100)
    parser.add_argument('--json', type=Path, help='also write the results as JSON')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        make_project(root, args.models)
        (root / '.gitignore').write_text('konstruct-out/\n')
        git(root, 'init', '-q', '-b', 'main')
        git(root, 'add', '-A')
        git(root, 'commit', '-qm', 'Add the project')
        git(root, 'tag', 'v1')
        first = args.models // 2
        for index in range(first, first + args.changed):
            model = root / 'models' / f'm{index:03}.sql'
            model.write_text(model.read_text().replace('* 2 as', '* 3 as').replace('b.c01 > 0', 'b.c01 > 1'))
        git(root, 'commit', '-qam', 'Change some models')
        project = Project(root)
        project.build(run_dbt=False, warehouses=False)
        results = {
            'models': args.models,
            'changed': args.changed,
            'diff_seconds': round(timed(lambda: project.diff('v1', 'HEAD')), 1),
            'release_notes_seconds': round(timed(lambda: project.release_notes('v1')), 1),
        }
        cached = sql._safe_type
        sql._safe_type = cached.__wrapped__  # type: ignore[assignment]
        try:
            results['diff_uncached_seconds'] = round(timed(lambda: project.diff('v1', 'HEAD')), 1)
        finally:
            sql._safe_type = cached
    for key, value in results.items():
        print(f'{key:24} {value}')
    if args.json:
        args.json.write_text(json.dumps(results, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
