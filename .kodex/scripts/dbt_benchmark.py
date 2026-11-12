"""Measure Konstruct on a dbt project that dbt itself compiles, in fresh processes.

The project is the one ``make_project`` in tests/test_scale.py writes (models in layers, each
joining two earlier models through CTEs, 20 columns each, tagged ``groupN`` in groups of 50, and
an Airflow DAG per group running ``dbt run -s tag:groupN`` and ``dbt test -s tag:groupN``), with
each relation written as ``ref`` or ``source`` and each model's tag in its own config, so that dbt
writes the manifest (its nodes in dbt's own order, not the models') and the catalog: ``dbt run``
and ``dbt docs generate`` in DuckDB, over raw tables that are empty. It is made once in ``--work``
and kept there.

For each checkout given (``--checkout``, by default this one), alternating between them run by
run, it records the median over ``--repeat`` runs of the wall time, the CPU time of the command's
own process (the most a machine with idle CPUs can bring its wall time down to) and the CPU time
of the process and its worker processes together, for:

* a cold build (``konstruct build --no-dbt --no-warehouse`` with no ``konstruct-out``), and a
  build with ``--update`` after it, with nothing changed;
* ``konstruct trace`` and ``konstruct explain`` on a column of the last model, ``konstruct impact``
  on a column of the 11th;
* ``konstruct check-sql`` of a query that sums a column of the last model joined to the one
  before it;
* ``konstruct conflicts --no-build``, after the build;
* after an edit to the formula of one column of a model three quarters of the way down, in a git
  repository of the project: ``konstruct gate --no-dbt --no-warehouse``, and the MCP ``gate``
  tool's gate over its copy of the graph, made afresh (``mcp_server.gate_copy``).

Each checkout's Konstruct is imported from the checkout itself (``PYTHONPATH``), by the command
and by its worker processes, so two checkouts compare two versions on the same machine and the
same project. Run it on an otherwise idle machine for wall times that mean anything; on a busy
one, compare CPU times.

Usage:
    uv run .kodex/scripts/dbt_benchmark.py --work /tmp/dbt-bench
    uv run .kodex/scripts/dbt_benchmark.py --work /tmp/dbt-bench --models 500 --repeat 3 \\
        --checkout . --checkout ../konstruct-main --json results.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COLUMNS = 20
QUERY = (
    'select m.c00, sum(m.c02) as total_c02, count(*) as n from wh.analytics.{last} m '
    'join wh.analytics.{before} n on m.c00 = n.c00 group by m.c00\n'
)
CLI = 'import sys; from konstruct.interfaces.cli import main; sys.exit(main())'
GATE_COPY = (
    'import sys; from pathlib import Path; from konstruct.interfaces.project import Project; '
    'from konstruct.interfaces.mcp_server import gate_copy; gate_copy(Project(Path(sys.argv[1])), None)'
)
REPORT = r"""
import atexit, json, resource, sys
out = sys.argv.pop(1)
def report():
    me, kids = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    with open(out, 'w') as f:
        json.dump({'own': me.ru_utime + me.ru_stime, 'children': kids.ru_utime + kids.ru_stime}, f)
atexit.register(report)
code = sys.argv.pop(1)
exec(compile(code, '<benchmark>', 'exec'))
"""


def dbt(project: Path, home: Path, *args: str) -> None:
    env = {
        'PATH': os.environ.get('PATH', ''),
        'HOME': str(home),
        'DBT_PROFILES_DIR': str(project),
        'DBT_SEND_ANONYMOUS_USAGE_STATS': 'false',
        **{k: v for k, v in os.environ.items() if k.lower().endswith('_proxy') or k == 'SSL_CERT_FILE'},
    }
    executable = Path(sys.executable).with_name('dbt')
    subprocess.run(
        [str(executable), *args, '--quiet', '--no-version-check'], cwd=project, env=env, check=True
    )


def git(project: Path, *args: str) -> None:
    identity = [
        '-c',
        'user.name=benchmark',
        '-c',
        'user.email=benchmark@example.com',
        '-c',
        'commit.gpgsign=false',
    ]
    subprocess.run(['git', *identity, *args], cwd=project, check=True, stdout=subprocess.DEVNULL)


def prepare(work: Path, models: int) -> Path:
    """The dbt project of ``models`` models, compiled by dbt and in a git repository, made once."""
    import duckdb

    root = work / f'dbt-{models}'
    if (root / 'target' / 'catalog.json').is_file() and (root / '.git').is_dir():
        return root
    if root.exists():
        shutil.rmtree(root)
    sys.path.insert(0, str(ROOT))
    from tests.test_scale import make_project

    make_project(root, models=models, columns_per_model=COLUMNS)
    shutil.rmtree(root / 'target')
    relation = re.compile(r'"wh"\."analytics"\."(raw_)?(m\d+)"')
    for path in sorted((root / 'models').glob('*.sql')):
        sql = relation.sub(
            lambda m: f"{{{{ source('raw', 'raw_{m[2]}') }}}}" if m[1] else f"{{{{ ref('{m[2]}') }}}}",
            path.read_text(),
        )
        path.write_text(f"{{{{ config(tags=['group{int(path.stem[1:]) // 50}']) }}}}\n{sql}")
    raw = [f'raw_m{index:03}' for index in range(min(models, 10))]
    (root / 'models' / 'sources.yml').write_text(
        'version: 2\nsources:\n  - name: raw\n    database: wh\n    schema: analytics\n    tables:\n'
        + ''.join(f'      - name: {name}\n' for name in raw)
    )
    (root / 'dbt_project.yml').write_text(
        'name: big\nprofile: big\nconfig-version: 2\nmodels:\n  big:\n    +materialized: table\n'
    )
    (root / 'profiles.yml').write_text(
        'big:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: wh.duckdb\n'
        '      schema: analytics\n      threads: 4\n'
    )
    connection = duckdb.connect(str(root / 'wh.duckdb'))
    connection.execute('create schema analytics')
    columns = ', '.join(f'c{j:02} integer' for j in range(COLUMNS))
    for name in raw:
        connection.execute(f'create table analytics.{name} ({columns})')
    connection.close()
    home = work / 'home'
    home.mkdir(exist_ok=True)
    for command in (('run',), ('docs', 'generate')):
        dbt(root, home, *command)
    shutil.rmtree(root / 'target' / 'run', ignore_errors=True)
    (root / '.gitignore').write_text('konstruct-out/\nlogs/\n*.duckdb\ntarget/run/\ntarget/compiled/\n')
    subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=root, check=True)
    git(root, 'add', '-A')
    git(root, 'commit', '-q', '-m', 'The project as dbt compiled it')
    return root


def run(
    checkout: Path, project: Path, code: str, *args: str, allowed: tuple[int, ...] = (0,)
) -> dict[str, float]:
    """Wall time, own CPU time and CPU time with workers of ``code`` in a fresh interpreter, which
    must exit with a status in ``allowed`` (the gate exits 1 on a change it fails) and raise
    nothing: a command that fails early is no fast time."""
    env = {**os.environ, 'PYTHONPATH': str(checkout)}
    with tempfile.NamedTemporaryFile(suffix='.json') as out:
        started = time.perf_counter()
        status = subprocess.run(
            [sys.executable, '-c', REPORT, out.name, code, *args],
            cwd=project,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        wall = time.perf_counter() - started
        if status.returncode not in allowed or 'Traceback' in status.stderr:
            shown = ' '.join(args) or code
            raise SystemExit(f'{shown} failed in {checkout} (exit {status.returncode}):\n{status.stderr}')
        used = json.loads(Path(out.name).read_text())
    return {'wall': wall, 'own': used['own'], 'total': used['own'] + used['children']}


def measure(checkout: Path, project: Path, work: Path, models: int) -> dict[str, dict[str, float]]:
    last, before, eleventh = f'm{models - 1:03}', f'm{models - 2:03}', f'm{min(10, models - 1):03}'
    query = work / 'query.sql'
    query.write_text(QUERY.format(last=last, before=before))
    shutil.rmtree(project / 'konstruct-out', ignore_errors=True)
    build = ('build', '--no-dbt', '--no-warehouse', '-q')
    found = {
        'cold build': run(checkout, project, CLI, *build),
        '--update, no change': run(checkout, project, CLI, *build, '-u'),
        f'trace {last}.c01': run(checkout, project, CLI, 'trace', f'{last}.c01'),
        f'impact {eleventh}.c01': run(checkout, project, CLI, 'impact', f'{eleventh}.c01'),
        f'explain {last}.c01': run(checkout, project, CLI, 'explain', f'{last}.c01'),
        'check-sql': run(checkout, project, CLI, 'check-sql', str(query)),
        'conflicts': run(checkout, project, CLI, 'conflicts', '--no-build'),
    }
    edited = project / 'models' / f'm{models * 3 // 4:03}.sql'
    edited.write_text(edited.read_text().replace('b.c01 * 2 as c01', 'b.c01 * 3 as c01'))
    try:
        found['gate after an edit'] = run(
            checkout, project, CLI, 'gate', '--no-dbt', '--no-warehouse', allowed=(0, 1)
        )
        shutil.rmtree(project / 'konstruct-out' / '.gate', ignore_errors=True)
        found['MCP gate over its copy'] = run(checkout, project, GATE_COPY, str(project))
    finally:
        git(project, 'checkout', '--', str(edited.relative_to(project)))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--work', type=Path, required=True, help='where the project is made and kept')
    parser.add_argument('--models', type=int, default=2000)
    parser.add_argument('--repeat', type=int, default=5)
    parser.add_argument('--checkout', type=Path, action='append', help='a checkout of Konstruct to measure')
    parser.add_argument('--json', type=Path, help='write the medians and every run here')
    args = parser.parse_args(argv)
    checkouts = [c.resolve() for c in args.checkout or [ROOT]]
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    project = prepare(work, args.models)
    runs: dict[str, list[dict[str, dict[str, float]]]] = {str(c): [] for c in checkouts}
    for index in range(args.repeat):
        for checkout in checkouts if index % 2 == 0 else checkouts[::-1]:
            runs[str(checkout)].append(measure(checkout, project, work, args.models))
            print(f'run {index + 1} of {args.repeat} done for {checkout}', file=sys.stderr)
    medians: dict[str, dict[str, dict[str, float]]] = {}
    for checkout, found in runs.items():
        medians[checkout] = {
            name: {
                k: round(statistics.median(r[name][k] for r in found), 3) for k in ('wall', 'own', 'total')
            }
            for name in found[0]
        }
    print(f'{args.models} models compiled by dbt, medians of {args.repeat} runs (seconds)\n')
    for checkout, found in medians.items():
        print(f'{checkout}\n\n| Command | Wall | CPU, own process | CPU with workers |\n|---|---:|---:|---:|')
        for name, value in found.items():
            print(f'| {name} | {value["wall"]} | {value["own"]} | {value["total"]} |')
        print()
    if args.json:
        args.json.write_text(json.dumps({'models': args.models, 'medians': medians, 'runs': runs}, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
