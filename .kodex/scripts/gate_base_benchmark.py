"""Measure the gate when it reads the graph of the base: built, and kept by commit.

Generates the project tests/test_scale.py's make_project makes (dbt models in layers, each joining
two earlier ones), with a sensitive term bound to a column the files do not have (as a column only
the warehouse knows is, in a build from files alone), commits it, builds the graph from files
alone and times, in one process:

* building the graph of the base, which the gate does when a term binds nothing;
* ``Project.gate`` from files alone with no change, the first time, which builds the base and keeps
  its graph in the user's cache directory (here a temporary one);
* the same gate again, which reads the kept graph.

Usage:
    uv run .kodex/scripts/gate_base_benchmark.py                 # 2,000 models
    uv run .kodex/scripts/gate_base_benchmark.py --models 500 --json gate.json
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
from konstruct.analysis.sensitivity import base_graph  # noqa: E402
from tests.test_scale import make_project  # noqa: E402

DEFINITIONS = """\
version: 1
terms:
  - name: Contact
    definition: A way to reach a person.
    sensitivity: personal
    columns: [wh.analytics.contacts.email]
"""


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
    parser.add_argument('--json', type=Path, help='also write the results as JSON')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / 'project'
        # A cache of its own, so the first gate starts from none and the user's is left alone.
        os.environ['XDG_CACHE_HOME'] = str(Path(folder) / 'cache')
        for variable in ('CI', 'GITHUB_ACTIONS'):  # where the gate keeps no graph
            os.environ.pop(variable, None)
        make_project(root, args.models)
        (root / 'definitions.yml').write_text(DEFINITIONS)
        (root / '.gitignore').write_text('konstruct-out/\n')
        git(root, 'init', '-q', '-b', 'main')
        git(root, 'add', '-A')
        git(root, 'commit', '-qm', 'Add the project')
        project = Project(root)
        project.build(run_dbt=False, warehouses=False)

        def gate() -> None:
            project.gate(base='HEAD', run_dbt=False, warehouses=False)

        results = {
            'models': args.models,
            'base_build_seconds': round(timed(lambda: base_graph(root, 'HEAD')), 1),
            'gate_first_seconds': round(timed(gate), 1),
            'gate_again_seconds': round(timed(gate), 1),
        }
    for key, value in results.items():
        print(f'{key:24} {value}')
    if args.json:
        args.json.write_text(json.dumps(results, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
