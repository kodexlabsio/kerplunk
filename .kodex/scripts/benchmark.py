"""Measure Konstruct on generated dbt projects of increasing size.

Each project is the one tests/test_scale.py builds: models in layers, each joining two earlier
models through CTEs, with a manifest, a catalog and Airflow DAGs. For each size it records:

* a cold build (no cache), which parses every model and writes graph.json, REPORT.md and
  graph.html (including every column's composed formula);
* --update with nothing changed, and --update after editing one model in the middle;
* the median time of an impact, a trace and an explain on the deepest model;
* the median time to render a large impact as text (``str()``, as a pipe gets it);
* the coding agent hook (`konstruct hook post-edit`) after an edit that changes nothing and after
  one that changes a model's filter, in a git repository of the project;
* the size of graph.json and graph.html;
* the questions the Rust core answers, on the graph as a command loads it: loading graph.json,
  the first impact and the first search (each also builds its index), then the median time of a
  search, a query, a path from the 11th model to the deepest and a describe of the deepest
  model, and one run of conflicts over every candidate column.

It also measures start-up: the CPU time of ``konstruct --help`` and of ``konstruct status`` on
the largest project, each in a fresh process.

Usage:
    uv run .kodex/scripts/benchmark.py                  # 100, 500, 1000 and 2000 models
    uv run .kodex/scripts/benchmark.py 300 3000 --json results.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from konstruct import Project, __version__  # noqa: E402
from konstruct.analysis.query import search  # noqa: E402
from konstruct.core.graph import Graph  # noqa: E402
from konstruct.interfaces import hook  # noqa: E402
from tests.test_scale import make_project  # noqa: E402

COLUMNS = 20
REPEAT = 5
"""Runs of each query; its median is reported."""


def timed(func: Any, *args: Any, **kwargs: Any) -> tuple[float, Any]:
    started = time.perf_counter()
    result = func(*args, **kwargs)
    return time.perf_counter() - started, result


def median_time(func: Any, *args: Any, repeat: int = REPEAT) -> float:
    times = []
    for _ in range(repeat):
        elapsed, _ = timed(func, *args)
        times.append(elapsed)
    return statistics.median(times)


def measure(models: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix='konstruct-bench-') as directory:
        root = Path(directory)
        make_project(root, models=models, columns_per_model=COLUMNS)
        project = Project(root)
        cold, result = timed(project.build, run_dbt=False, warehouses=False)
        graph = result.graph
        unchanged, _ = timed(project.build, update=True, run_dbt=False, warehouses=False)
        middle = root / 'models' / f'm{models // 2:03}.sql'
        middle.write_text(middle.read_text().replace('where b.c01 > 0', 'where b.c01 > 1'))
        manifest = root / 'target' / 'manifest.json'
        data = json.loads(manifest.read_text())
        node = data['nodes'][f'model.big.m{models // 2:03}']
        node['compiled_code'] = node['raw_code'] = middle.read_text()
        node['checksum']['checksum'] = 'edited'
        manifest.write_text(json.dumps(data))
        one_edit, _ = timed(project.build, update=True, run_dbt=False, warehouses=False)
        hook_unchanged, hook_edit = hook_latency(root, middle)
        deepest = f'm{models - 1:03}'
        load, loaded = timed(Graph.read, project.graph_path)
        first_impact, _ = timed(loaded.impact, loaded.resolve('m010.c01')[0])
        first_search, _ = timed(search, loaded, 'm010 c01')
        conflicts, _ = timed(project.conflicts)
        walk = project.impact('raw_m008' if models > 8 else 'raw_m000')
        startup = {} if models != max(SIZES) else measure_startup(root)
        return {
            'models': models,
            'columns': sum(1 for n in graph.nodes.values() if n.kind == 'column'),
            'column_edges': sum(1 for e in graph.edges if e.kind == 'derived_from'),
            'warnings': len(graph.warnings),
            'cold_build_s': round(cold, 2),
            'update_unchanged_s': round(unchanged, 2),
            'update_one_model_s': round(one_edit, 2),
            'hook_unchanged_s': round(hook_unchanged, 2),
            'hook_one_model_s': round(hook_edit, 2),
            'impact_ms': round(1000 * median_time(project.impact, 'm010.c01'), 1),
            'trace_ms': round(1000 * median_time(project.trace, f'{deepest}.c00'), 1),
            'explain_ms': round(1000 * median_time(project.explain, f'{deepest}.c01'), 1),
            'render_ms': round(1000 * median_time(str, walk), 1),
            'render_lines': str(walk).count('\n') + 1,
            **startup,
            'graph_json_mb': round((project.out_dir / 'graph.json').stat().st_size / 1e6, 1),
            'graph_html_mb': round((project.out_dir / 'graph.html').stat().st_size / 1e6, 1),
            'load_s': round(load, 2),
            'first_impact_ms': round(1000 * first_impact, 1),
            'first_search_ms': round(1000 * first_search, 1),
            'search_ms': round(1000 * median_time(project.search, 'm010 c01'), 1),
            'query_ms': round(1000 * median_time(project.query, 'which models use m010'), 1),
            'path_ms': round(1000 * median_time(project.path_between, 'm010.c01', f'{deepest}.c00'), 1),
            'describe_ms': round(1000 * median_time(project.describe, deepest), 1),
            'conflicts_s': round(conflicts, 2),
        }


SIZES: list[int] = []


def cpu_ms(args: list[str], cwd: Path, repeat: int = 7) -> float:
    """The median CPU time of ``konstruct <args>`` in a fresh process, which wall time on a busy
    machine would not show reliably."""
    times = []
    for _ in range(repeat):
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        subprocess.run(
            [sys.executable, '-m', 'konstruct.interfaces.cli', *args],
            cwd=cwd,
            capture_output=True,
            check=False,
            env={**os.environ, 'PYTHONPATH': str(ROOT)},
        )
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        times.append((after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime))
    return round(1000 * statistics.median(times), 1)


def measure_startup(root: Path) -> dict[str, float]:
    return {'help_cpu_ms': cpu_ms(['--help'], root), 'status_cpu_ms': cpu_ms(['status'], root)}


def hook_latency(root: Path, middle: Path) -> tuple[float, float]:
    """The hook's time, after a first run, for an edit that changes no column (a blank line added)
    and for one that changes a model's filter: the time an agent waits for the gate's answer,
    less the interpreter's start."""
    git = [
        'git',
        '-C',
        str(root),
        '-c',
        'user.name=b',
        '-c',
        'user.email=b@example.com',
        '-c',
        'commit.gpgsign=false',
    ]
    (root / '.gitignore').write_text('konstruct-out/\n')
    for command in (['init', '-q', '-b', 'main'], ['add', '-A'], ['commit', '-q', '-m', 'benchmark']):
        subprocess.run([*git, *command], check=True, capture_output=True)
    event = {'cwd': str(root), 'tool_input': {'file_path': str(middle)}}
    # The first run copies the build cache into .hook and builds there once; time what follows.
    hook.post_edit(event)
    middle.write_text(middle.read_text() + '\n')
    unchanged, _ = timed(hook.post_edit, event)
    middle.write_text(middle.read_text().replace('where b.c01 > 1', 'where b.c01 > 2'))
    edited, _ = timed(hook.post_edit, event)
    return unchanged, edited


def markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        '| Models | Columns | Column edges | Cold build | `--update`, no change | `--update`, one model edited '
        '| impact | trace | explain | text of a large impact | graph.html | Hook, no change '
        '| Hook, one model edited |',
        '|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|',
    ]
    for r in rows:
        lines.append(
            f'| {r["models"]:,} | {r["columns"]:,} | {r["column_edges"]:,} | {r["cold_build_s"]} s '
            f'| {r["update_unchanged_s"]} s | {r["update_one_model_s"]} s | {r["impact_ms"]} ms '
            f'| {r["trace_ms"]} ms | {r["explain_ms"]} ms | {r["render_ms"]} ms ({r["render_lines"]:,} lines) '
            f'| {r["graph_html_mb"]} MB | {r["hook_unchanged_s"]} s | {r["hook_one_model_s"]} s |'
        )
    lines += [
        '',
        '| Models | Load graph.json | First impact | First search | search | query | path | describe '
        '| conflicts |',
        '|---:|---:|---:|---:|---:|---:|---:|---:|---:|',
    ]
    for r in rows:
        lines.append(
            f'| {r["models"]:,} | {r["load_s"]} s | {r["first_impact_ms"]} ms | {r["first_search_ms"]} ms '
            f'| {r["search_ms"]} ms | {r["query_ms"]} ms | {r["path_ms"]} ms | {r["describe_ms"]} ms '
            f'| {r["conflicts_s"]} s |'
        )
    startup = next((r for r in rows if 'help_cpu_ms' in r), None)
    if startup:
        lines += [
            '',
            f'Start-up (CPU time, {startup["models"]:,} models): `konstruct --help` {startup["help_cpu_ms"]} ms, '
            f'`konstruct status` {startup["status_cpu_ms"]} ms.',
        ]
    return '\n'.join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('sizes', nargs='*', type=int, default=[100, 500, 1000, 2000])
    parser.add_argument('--json', type=Path, help='also write the results as JSON')
    args = parser.parse_args()
    SIZES.extend(args.sizes)
    rows = []
    for size in args.sizes:
        row = measure(size)
        rows.append(row)
        print(f'{size} models: {row}', file=sys.stderr)
    machine = (
        f'{platform.machine()}, {platform.system()} {platform.release()}, Python {platform.python_version()}'
    )
    print(f'konstruct {__version__} on {machine}, {COLUMNS} columns per model\n')
    print(markdown(rows))
    if args.json:
        args.json.write_text(
            json.dumps({'konstruct': __version__, 'machine': machine, 'results': rows}, indent=1)
        )
    return 0


if __name__ == '__main__':
    sys.exit(main())
