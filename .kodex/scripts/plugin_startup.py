"""Measure what looking for plugins costs at start-up.

Konstruct reads installed distributions' entry points only in the commands that can run a plugin
(build, export, gate, mcp, status and plugins), and loads a plugin only when its group is used. This
script times, as the median of several fresh processes:

* the CLI's own start-up (``konstruct --version``), which never looks for plugins;
* one scan of every installed distribution's entry points (``importlib.metadata.entry_points``);
* building the registry (``Plugins.discover``), which is that scan plus Konstruct's own components;
* loading every component of the registry (``Plugins.discover().load_all()``), which imports the
  modules Konstruct's own inputs, exports and checks live in.

Usage:
    uv run .kodex/scripts/plugin_startup.py            # 15 runs of each
    uv run .kodex/scripts/plugin_startup.py --runs 30 --json startup.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from importlib.metadata import distributions
from pathlib import Path

# Each statement runs in a fresh interpreter after `import konstruct`, timed from inside it so the
# interpreter's own start-up is left out of everything but the CLI row.
SETUP = 'import time, konstruct; started = time.perf_counter(); '
FINISH = '; print(time.perf_counter() - started)'
CASES = {
    'entry point scan': 'from importlib.metadata import entry_points; entry_points()',
    'Plugins.discover()': 'from konstruct.plugins import Plugins; Plugins.discover()',
    'Plugins.discover().load_all()': 'from konstruct.plugins import Plugins; Plugins.discover().load_all()',
}


def _seconds(code: str) -> float:
    run = subprocess.run(
        [sys.executable, '-c', SETUP + code + FINISH], capture_output=True, text=True, check=True
    )
    return float(run.stdout.strip())


def _cli_seconds() -> float:
    code = (
        'import subprocess, sys, time; started = time.perf_counter(); '
        "subprocess.run([sys.executable, '-m', 'konstruct.interfaces.cli', '--version'], "
        'check=True, capture_output=True); print(time.perf_counter() - started)'
    )
    run = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True)
    return float(run.stdout.strip())


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--runs', type=int, default=15)
    parser.add_argument('--json', metavar='FILE')
    args = parser.parse_args()
    results = {
        'konstruct --version (whole process)': statistics.median(_cli_seconds() for _ in range(args.runs))
    }
    for name, code in CASES.items():
        results[name] = statistics.median(_seconds(code) for _ in range(args.runs))
    count = sum(1 for _ in distributions())
    print(f'{count} installed distributions, median of {args.runs} runs')
    for name, seconds in results.items():
        print(f'  {name:<40} {seconds * 1000:7.1f} ms')
    if args.json:
        with Path(args.json).open('w', encoding='utf-8') as handle:
            json.dump({'distributions': count, 'runs': args.runs, 'seconds': results}, handle, indent=2)


if __name__ == '__main__':
    main()
