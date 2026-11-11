"""Measure `konstruct serve` on a generated dbt project (tests/test_scale.py's `make_project`).

It records:

* cold start: from starting `konstruct serve --no-build` to its first answer, and the time to
  read graph.json and make its encodings;
* p50 and p99 latency and requests per second of `/api/v1/impact/{id}` and `/api/v1/search`, at
  several concurrencies, from load.rs beside this script (built with `rustc`), answered from the
  server's cache once Python has answered each the first time;
* `/api/v1/explain/{ref}`: the first answer for each column, which Python composes, and the same
  answers again from the server's cache, one request at a time and under load;
* the server's memory: resident size after the load, and its peak.

The client and the server share the machine, so the numbers are a floor for the server alone.

Usage:
    uv run .kodex/scripts/benchmark_serve.py                   # 2,000 models
    uv run .kodex/scripts/benchmark_serve.py 500 --json results.json
    uv run .kodex/scripts/benchmark_serve.py --project DIR     # a project built already
"""

from __future__ import annotations

import argparse
import http.client
import json
import platform
import re
import shutil
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
from tests.test_scale import make_project  # noqa: E402

COLUMNS = 20
CONCURRENCY = (1, 16, 64)
SECONDS = 5.0


def start(project: Path) -> tuple[subprocess.Popen[str], int, float]:
    """Start the server and wait for its first answer: the process, its port and the time taken."""
    started = time.perf_counter()
    process = subprocess.Popen(
        [
            sys.executable,
            '-m',
            'konstruct.interfaces.cli',
            'serve',
            str(project),
            '--no-build',
            '--port',
            '0',
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert process.stdout is not None
    found = re.search(r':(\d+)/', process.stdout.readline())
    if not found:
        process.kill()
        raise SystemExit('konstruct serve did not start')
    port = int(found.group(1))
    while True:
        try:
            status, _ = get(port, '/api/v1/search?q=c01')
            if status == 200:
                return process, port, time.perf_counter() - started
        except OSError:
            time.sleep(0.005)


def get(port: int, path: str) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=120)
    try:
        connection.request('GET', path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def memory(pid: int) -> dict[str, float]:
    """Resident and peak memory in MB (Linux's /proc; elsewhere `ps` gives the resident size)."""
    status = Path(f'/proc/{pid}/status')
    if status.is_file():
        fields = dict(line.split(':', 1) for line in status.read_text().splitlines() if ':' in line)
        return {
            'rss_mb': round(int(fields['VmRSS'].split()[0]) / 1024, 1),
            'peak_mb': round(int(fields['VmHWM'].split()[0]) / 1024, 1),
        }
    rss = subprocess.run(['ps', '-o', 'rss=', '-p', str(pid)], capture_output=True, text=True).stdout
    return {'rss_mb': round(int(rss.strip() or 0) / 1024, 1)}


def build_client(directory: Path) -> Path:
    rustc = shutil.which('rustc')
    if rustc is None:
        raise SystemExit('the load client needs rustc')
    client = directory / 'load'
    source = Path(__file__).with_name('load.rs')
    subprocess.run([rustc, '-O', '--edition', '2024', '-o', str(client), str(source)], check=True)
    return client


def load(client: Path, port: int, concurrency: int, paths: list[str]) -> dict[str, Any]:
    output = subprocess.run(
        [str(client), f'127.0.0.1:{port}', str(concurrency), str(SECONDS), *paths],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result: dict[str, Any] = json.loads(output)
    return result


def measure(project: Path, client: Path) -> dict[str, Any]:
    graph = Project(project).graph
    models = sum(1 for n in graph.nodes.values() if n.kind == 'model')
    starts = []
    for _ in range(3):
        process, port, seconds = start(project)
        starts.append(seconds)
        process.terminate()
        process.wait()
    process, port, _ = start(project)
    try:
        # Columns of the early models, whose impact reaches far downstream, as benchmark.py's
        # impact of m010.c01 does.
        early = {f'model.big.m{index:03}' for index in range(10, 60)}
        sample = sorted(n.id for n in graph.nodes.values() if n.kind == 'column' and n.parent in early)[:200]
        impact = [f'/api/v1/impact/{c.replace("#", "%23")}' for c in sample]
        words = sorted({n.name for n in graph.nodes.values() if n.kind == 'model'})
        search = [f'/api/v1/search?q={w}' for w in words[:: max(1, len(words) // 100)]]
        rows: list[dict[str, Any]] = []
        for name, paths in (('impact', impact), ('search', search)):
            for concurrency in CONCURRENCY:
                rows.append(
                    {'route': name, 'concurrency': concurrency, **load(client, port, concurrency, paths)}
                )
        explained = [f'/api/v1/explain/{graph.label(graph.nodes[c])}' for c in sample[:50]]
        cold = []
        for path in explained:
            began = time.perf_counter()
            status, _ = get(port, path)
            cold.append((time.perf_counter() - began) * 1000)
            assert status == 200, path
        cached = []
        for path in explained:
            began = time.perf_counter()
            get(port, path)
            cached.append((time.perf_counter() - began) * 1000)
        for concurrency in CONCURRENCY:
            rows.append(
                {
                    'route': 'explain (cached)',
                    'concurrency': concurrency,
                    **load(client, port, concurrency, explained),
                }
            )
        used = memory(process.pid)
    finally:
        process.terminate()
        process.wait()
    return {
        'models': models,
        'nodes': len(graph.nodes),
        'edges': len(graph.edges),
        'graph_json_mb': round((Project(project).graph_path).stat().st_size / 1e6, 1),
        'cold_start_s': round(statistics.median(starts), 2),
        'explain_cold_ms': round(statistics.median(cold), 1),
        'explain_cached_ms': round(statistics.median(cached), 2),
        'memory': used,
        'load': rows,
    }


def markdown(result: dict[str, Any]) -> str:
    lines = [
        f'{result["models"]:,} models: {result["nodes"]:,} nodes, {result["edges"]:,} edges, '
        f'graph.json {result["graph_json_mb"]} MB',
        '',
        f'- Cold start to first answer: {result["cold_start_s"]} s (median of 3)',
        f'- explain, first answer per column: {result["explain_cold_ms"]} ms median; '
        f'from the cache: {result["explain_cached_ms"]} ms',
        f'- Memory after the load: {result["memory"].get("rss_mb")} MB resident, '
        f'{result["memory"].get("peak_mb", "?")} MB peak',
        '',
        '| Route | Concurrency | Requests/s | p50 | p99 | Errors |',
        '|---|---:|---:|---:|---:|---:|',
    ]
    for row in result['load']:
        lines.append(
            f'| {row["route"]} | {row["concurrency"]} | {row["rps"]:,} | {row["p50_ms"]} ms '
            f'| {row["p99_ms"]} ms | {row["errors"]} |'
        )
    return '\n'.join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('models', nargs='?', type=int, default=2000)
    parser.add_argument('--project', type=Path, help='serve this project, built already, instead')
    parser.add_argument('--json', type=Path, help='also write the results as JSON')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='konstruct-serve-bench-') as directory:
        scratch = Path(directory)
        client = build_client(scratch)
        project = args.project
        if project is None:
            project = scratch / 'project'
            make_project(project, models=args.models, columns_per_model=COLUMNS)
        built = Project(project)
        if not built.has_graph() or not (built.out_dir / 'explain.json').is_file():
            print('konstruct: building the project', file=sys.stderr)
            built.build(update=True, run_dbt=False, warehouses=False)
        result = measure(project, client)
    machine = (
        f'{platform.machine()}, {platform.system()} {platform.release()}, Python {platform.python_version()}'
    )
    print(f'konstruct {__version__} serve on {machine}\n')
    print(markdown(result))
    if args.json:
        args.json.write_text(json.dumps({'konstruct': __version__, 'machine': machine, **result}, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
