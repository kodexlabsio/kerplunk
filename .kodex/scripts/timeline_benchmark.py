"""Measure the timeline: the world built at each commit of a range, then read from the cache.

Generates the project tests/test_scale.py's make_project makes (dbt models in layers, each joining
two earlier ones) with a primary key on each model's first column in its YAML, so each model holds
an entity, commits it, then commits three changes: a formula in one model, a key dropped from
another, and a definitions.yml with one measure. After a build it times, in one process:

* ``Project.timeline`` over the four commits with an empty cache (four worlds built);
* the same again, every world read from the cache.

Each is given as wall time and as CPU time (this process and the worker processes the builds
start), which a busy machine changes less.

The cache is a temporary directory (``XDG_CACHE_HOME``), never the user's own.

Usage:
    uv run .kodex/scripts/timeline_benchmark.py                 # 2,000 models
    uv run .kodex/scripts/timeline_benchmark.py --models 500 --json timeline.json
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from konstruct import Project  # noqa: E402
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


def cpu() -> float:
    used = [resource.getrusage(who) for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)]
    return sum(u.ru_utime + u.ru_stime for u in used)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--models', type=int, default=2000)
    parser.add_argument('--json', type=Path, help='also write the results as JSON')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / 'project'
        make_project(root, args.models)
        key = '    constraints: [{type: primary_key, columns: [c00]}]\n'
        keys = ''.join(f'  - name: m{i:03}\n{key}' for i in range(args.models))
        (root / 'models' / 'schema.yml').write_text(f'version: 2\nmodels:\n{keys}')
        (root / '.gitignore').write_text('target/\nkonstruct-out/\n')
        git(root, 'init', '-q', '-b', 'main')
        git(root, 'add', '-A')
        git(root, 'commit', '-qm', 'Add the project')
        middle, last = f'm{args.models * 3 // 4:03}', f'm{args.models - 1:03}'
        model = root / 'models' / f'{middle}.sql'
        model.write_text(model.read_text().replace('b.c01 * 2 as c01', 'b.c01 * 3 as c01'))
        git(root, 'commit', '-qam', 'Triple c01')
        schema = root / 'models' / 'schema.yml'
        schema.write_text(schema.read_text().replace(f'  - name: {last}\n{key}', ''))
        git(root, 'commit', '-qam', 'Drop a key')
        (root / 'definitions.yml').write_text(
            f'terms:\n  - name: Middle value\n    kind: measure\n    columns: [{middle}.c01]\n'
        )
        git(root, 'add', '-A')
        git(root, 'commit', '-qm', 'Define the middle value')
        os.environ['XDG_CACHE_HOME'] = str(Path(folder) / 'cache')
        os.environ.pop('CI', None)
        os.environ.pop('GITHUB_ACTIONS', None)
        project = Project(root)
        project.build(run_dbt=False, warehouses=False)
        started, used = time.perf_counter(), cpu()
        result = project.timeline()
        cold, cold_cpu = time.perf_counter() - started, cpu() - used
        started, used = time.perf_counter(), cpu()
        project.timeline()
        cached, cached_cpu = time.perf_counter() - started, cpu() - used
        results = {
            'models': args.models,
            'commits': result.commits,
            'entries': len(result.entries),
            'first_run_seconds': round(cold, 1),
            'first_run_cpu_seconds': round(cold_cpu, 1),
            'cached_seconds': round(cached, 1),
            'cached_cpu_seconds': round(cached_cpu, 1),
        }
    for name, value in results.items():
        print(f'{name:20} {value}')
    if args.json:
        args.json.write_text(json.dumps(results, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
