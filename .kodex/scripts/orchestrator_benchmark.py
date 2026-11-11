"""Measure how Konstruct finds and reads Airflow DAG files and Dagster definitions.

It generates a repository of Python files (the one tests/test_scale.py builds: DAG files, Dagster
definition files and other Python files, one in twenty of which mentions Airflow without defining
a DAG) and reports the median of each step over several runs:

* the walk over the repository, which lists its Python files;
* the scan: the look at every Python file that decides whether it holds a DAG or Dagster
  definitions (both questions together, so each file is read once);
* discovery as the build runs it: the walk and the scan, once for DAG files and once for Dagster
  files;
* reading the DAG files and the Dagster files found: the tasks, dependencies, SQL, assets and
  jobs they declare, without importing them.

The script runs against any version of Konstruct: before the reader of these files was part of the
native core (v0.13.13 and earlier), the scan is the loop that read each file's text and asked
``looks_like_dag_file`` and ``looks_like_dagster_file``.

Usage:
    uv run .kodex/scripts/orchestrator_benchmark.py                   # 2000 DAG files, 5000 others
    uv run .kodex/scripts/orchestrator_benchmark.py 500 1000 --assets 200
"""

from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from konstruct import __version__  # noqa: E402
from konstruct.engine.config import (  # noqa: E402
    discover_dag_files,
    discover_dagster_files,
    iter_files,
    load_config,
)
from konstruct.inputs import airflow, dagster  # noqa: E402
from tests.test_scale import make_orchestrator_repo  # noqa: E402

REPEAT = 5


def median_of(function: Callable[[], Any]) -> tuple[float, Any]:
    times = []
    result = None
    for _ in range(REPEAT):
        started = time.perf_counter()
        result = function()
        times.append(time.perf_counter() - started)
    return statistics.median(times), result


def scanner() -> Callable[[list[Path]], object]:
    """The scan of a list of Python files, as the build does it in this version."""
    try:
        from konstruct._core import scan_python_files
    except ImportError:
        scan_python_files = None  # type: ignore[assignment]
    if scan_python_files is not None:
        return scan_python_files

    def read_each(paths: list[Path]) -> list[tuple[bool, bool]]:
        found = []
        for path in paths:
            text = path.read_text(encoding='utf-8', errors='replace')
            found.append((airflow.looks_like_dag_file(text), dagster.looks_like_dagster_file(text)))
        return found

    return read_each


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('dags', nargs='?', type=int, default=2000, help='DAG files (default 2000)')
    parser.add_argument('others', nargs='?', type=int, default=5000, help='other Python files (5000)')
    parser.add_argument('--assets', type=int, default=500, help='Dagster definition files (500)')
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='konstruct-orchestrators-') as directory:
        root = Path(directory)
        make_orchestrator_repo(root, arguments.dags, arguments.others, arguments.assets)
        config = load_config(root)
        walking, python_files = median_of(lambda: iter_files(config, '.py'))
        scan = scanner()
        scanning, _ = median_of(lambda: scan(python_files))
        discovery, found = median_of(lambda: (discover_dag_files(config), discover_dagster_files(config)))
        dag_files, dagster_files = found
        reading_dags, dags = median_of(lambda: [airflow.parse_file(path) for path in dag_files])
        reading_assets, files = median_of(lambda: [dagster.parse_file(path) for path in dagster_files])
    tasks = sum(len(d.tasks) for ds in dags for d in ds)
    total = arguments.dags + arguments.others + arguments.assets
    print(f'konstruct {__version__}, Python {sys.version.split()[0]}\n')
    print(
        '| Python files | DAG files | Dagster files | Walk | Scan | Discovery | Reading DAG files | Reading Dagster files |'
    )
    print('|---:|---:|---:|---:|---:|---:|---:|---:|')
    print(
        f'| {total:,} | {len(dag_files):,} | {len(dagster_files):,} | {walking:.3f} s | {scanning:.3f} s '
        f'| {discovery:.3f} s | {reading_dags:.3f} s | {reading_assets:.3f} s |'
    )
    print(f'\n{tasks:,} tasks and {sum(len(f.assets) for f in files):,} assets read.')


if __name__ == '__main__':
    main()
