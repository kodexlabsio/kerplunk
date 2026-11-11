"""Time the gate's consumer-pin-changed check on a producer of N SQL tables of five measures each,
with a consumer that pins every column of every table: checking every pin, then the check after a
change that alters no column, and after a change to one table.

Usage:
    uv run .kodex/scripts/benchmark_consumer_pins.py DIR 2000
"""

import os
import subprocess
import sys
import time
from pathlib import Path

os.environ['KONSTRUCT_TRUST_CONFIG'] = '1'
from konstruct import Project
from konstruct.analysis import gate
from konstruct.analysis.conflicts import Conflicts
from konstruct.analysis.lock import check_consumers, lock
from konstruct.plugins.api import CheckContext

base, n = Path(sys.argv[1]), int(sys.argv[2])
finance, dash = base / 'finance', base / 'dash'
finance.mkdir(parents=True)
dash.mkdir()


def git(*args: str) -> None:
    ident = ['-c', 'user.name=t', '-c', 'user.email=t@e', '-c', 'commit.gpgsign=false']
    subprocess.run(['git', '-C', str(finance), *ident, *args], check=True, capture_output=True)


(finance / 'konstruct.toml').write_text('dialect = "duckdb"\nrepositories = ["../dash"]\n')
(finance / '.gitignore').write_text('konstruct-out/\n')
(dash / 'konstruct.toml').write_text('dialect = "duckdb"\nrepositories = ["../finance"]\n')
for i in range(n):
    cols = ', '.join(f'sum(c{j}) * {j + 1} as m{j}' for j in range(5))
    (finance / f't{i}.sql').write_text(
        f'create table main.t{i} as with s as (select k, c0, c1, c2, c3, c4 from raw.r{i} '
        f"where status <> 'x') select k, {cols} from s group by k\n"
    )
    sel = ', '.join(f'm{j} + 1 as d{j}' for j in range(5))
    (dash / f'd{i}.sql').write_text(f'create table main.d{i} as select k, {sel} from main.t{i}\n')
git('init', '-q', '-b', 'main')
git('add', '-A')
git('commit', '-qm', 'base')
lock(Project(dash).build(warehouses=False, run_dbt=False).graph, dash)
producer = Project(finance)


def timed(label: str) -> None:
    producer.build(warehouses=False, run_dbt=False, update=True)
    context = CheckContext(graph=producer.graph, diff=producer.diff(base='HEAD'), conflicts=Conflicts())
    started = time.perf_counter()
    found = gate._consumer_pin_changed(context)
    print(f'{label}: {time.perf_counter() - started:.3f}s, {len(found)} findings')


producer.build(warehouses=False, run_dbt=False)
started = time.perf_counter()
pins = sum(c.pinned for c in check_consumers(producer.graph, finance, history=False))
print(f'{n} tables, {pins} pins: every pin checked {time.perf_counter() - started:.3f}s')
(finance / 'README.md').write_text('docs only\n')
timed('a change to no column')
(finance / 't7.sql').write_text((finance / 't7.sql').read_text().replace('* 1 as m0', '* 10 as m0'))
timed('a change to one table')
