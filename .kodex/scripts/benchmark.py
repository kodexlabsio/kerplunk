"""Measure Konstruct on generated dbt projects of increasing size.

Each project is the one tests/test_scale.py builds: models in layers, each joining two earlier
models through CTEs, with a manifest, a catalog and Airflow DAGs. For each size it records:

* a cold build (no cache), which parses every model and writes graph.json, REPORT.md and
  graph.html (including every column's composed formula);
* --update with nothing changed, and --update after editing one model in the middle;
* discovery: the time to find every input's files in the project, as a build does it, over one
  walk of the repository;
* the median time of an impact, a trace and an explain on the deepest model;
* the median time to render a large impact as text (``str()``, as a pipe gets it);
* the coding agent hook (`konstruct hook post-edit`) after an edit that changes nothing and after
  one that changes a model's filter, in a git repository of the project;
* the size of graph.json and graph.html;
* the questions the Rust core answers, on the graph as a command loads it: loading graph.json,
  the first impact and the first search (each also builds its index), then the median time of a
  search, a query, a path from the 11th model to the deepest and a describe of the deepest
  model, and one run of conflicts over every candidate column;
* the formula identity test: translating every column's composed formula into the core's
  program, then fingerprinting all of them and grouping them by fingerprint;
* how long graph.html takes to show its overview and to draw its full lineage graph in headless
  Chromium, when Playwright's Chromium is installed;
* search by meaning: making its index on a loaded graph with no vectors.bin, in wall and CPU
  time (it uses several threads), the size of the vectors.bin it saves, and the median time of a
  search by meaning after it.

With ``--tables``, it measures conflicts and the gate on a repository of that many SQL tables with
the same five measures, each table reading its own source, so that every two tables conflict on
every measure (``make_measures`` in tests/test_scale.py): the CPU time of ``konstruct conflicts``
over every pair, and of the gate's checks after an edit to one measure of one table.

It also measures SQL over relations whose columns no warehouse or catalog lists: the median time
to analyse a chain of 20, 40 and 80 CTEs, each a ``SELECT *`` of the one before with a filter,
over a relation with unknown columns, selecting 20 of its columns at the end; and a cold build and
the best of three ``--update`` builds of a project of 400 SQL files with no warehouse.

It also measures start-up: the CPU time of ``konstruct --help``, of ``konstruct status`` and of
``konstruct impact`` on a column on the largest project, each in a fresh process, and in the same way ``konstruct search`` with and
without ``--meaning``, which reads the saved vectors.bin.

Usage:
    uv run .kodex/scripts/benchmark.py                  # 100, 500, 1000 and 2000 models
    uv run .kodex/scripts/benchmark.py 300 3000 --json results.json
    uv run .kodex/scripts/benchmark.py --tables 100 400  # conflicts and the gate alone
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
from konstruct.analysis import semantic  # noqa: E402
from konstruct.analysis.conflicts import find_conflicts  # noqa: E402
from konstruct.analysis.gate import gate  # noqa: E402
from konstruct.analysis.identity import Formulas  # noqa: E402
from konstruct.analysis.query import search  # noqa: E402
from konstruct.core.graph import Graph  # noqa: E402
from konstruct.interfaces import hook  # noqa: E402
from tests.test_gate import git  # noqa: E402
from tests.test_scale import give_entities, make_measures, make_project  # noqa: E402

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


def identity(graph: Graph) -> tuple[int, float, float]:
    """The formulas with an expression, the time to translate them, and the time to fingerprint
    and group them."""
    columns = [
        n for n in graph.nodes.values() if n.kind == 'column' and isinstance(n.attrs.get('expression'), str)
    ]
    started = time.perf_counter()
    formulas = Formulas(graph, fixed_key=True)
    definitions = [formulas.column(n) for n in columns]
    translated = time.perf_counter() - started
    started = time.perf_counter()
    groups: dict[str, list[str]] = {}
    for column, value in zip(columns, formulas.fingerprints(definitions), strict=True):
        groups.setdefault(value, []).append(column.id)
    return len(columns), translated, time.perf_counter() - started


def world_model(graph: Graph, models: int) -> dict[str, float]:
    """The world model's questions on ``graph`` once each model has an entity, a primary key on
    ``c00``, and a measure, a ``SUM`` around ``c02``: every metric (with a proposal from each
    model), one entity, and how the middle model's entity relates to the 11th's, through the
    joins of the models that read them."""
    from konstruct.semantics import world
    from konstruct.semantics.ontology import derive

    give_entities(graph)
    ontology_s, ontology = timed(derive, graph)
    last, eleventh = f'm{(models - 1) // 2:03}', f'm{min(10, models - 1):03}'
    metrics_s, found = timed(world.metrics, graph, None, ontology)
    named = f'{last}.c02'
    return {
        'entities': len(ontology.entities),
        'relationships': len(ontology.relationships),
        'metrics': len(found.metrics),
        'ontology_s': round(ontology_s, 2),
        'metrics_s': round(metrics_s, 2),
        'metric_ms': round(1000 * median_time(world.metrics, graph, named, ontology), 1),
        'entity_ms': round(1000 * median_time(world.entity, graph, last, ontology), 1),
        'relate_ms': round(1000 * median_time(world.relate, graph, last, eleventh, ontology), 1),
        'relate_hops': len(world.relate(graph, last, eleventh, ontology).hops),
    }


def render_times(page_path: Path) -> tuple[float | None, float | None]:
    """Seconds for graph.html to show its overview, and its full lineage graph, in headless
    Chromium (medians of three loads); None when Playwright's Chromium is not installed."""
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        return None, None
    opened: list[float] = []
    drawn: list[float] = []
    with sync_playwright() as playwright:
        try:
            # KONSTRUCT_BROWSER_CHANNEL=chrome uses an installed Google Chrome, as the tests do.
            browser = playwright.chromium.launch(channel=os.environ.get('KONSTRUCT_BROWSER_CHANNEL') or None)
        except Error:
            return None, None
        for _ in range(3):
            page = browser.new_page(viewport={'width': 1600, 'height': 900})
            started = time.perf_counter()
            page.goto(page_path.as_uri())
            page.wait_for_selector('#page h1', timeout=120_000)
            opened.append(time.perf_counter() - started)
            page.close()
            page = browser.new_page(viewport={'width': 1600, 'height': 900})
            started = time.perf_counter()
            page.goto(page_path.as_uri() + '#/lineage')
            page.wait_for_selector('#nodes [data-id]', timeout=120_000)
            drawn.append(time.perf_counter() - started)
            page.close()
        browser.close()
    return statistics.median(opened), statistics.median(drawn)


def discover_everything(config: Any) -> None:
    """Every discovery a build makes, over the one walk it shares."""
    from konstruct.engine import config as discovery

    with config.shared_walk():
        projects = discovery.dbt_projects(config)
        discovery.discover_dag_files(config)
        discovery.discover_dagster_files(config)
        discovery.discover_spark_files(config)
        discovery.discover_looker_files(config)
        discovery.discover_powerbi_files(config)
        discovery.discover_definitions_files(config)
        discovery.discover_ossie_files(config)
        discovery.discover_ingestion_files(config)
        discovery.discover_sql_files(config, projects, set())
        discovery.discover_warehouses(config, projects)


def measure(models: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix='konstruct-bench-') as directory:
        root = Path(directory)
        make_project(root, models=models, columns_per_model=COLUMNS)
        project = Project(root)
        cold, result = timed(project.build, run_dbt=False, warehouses=False)
        graph = result.graph
        unchanged, _ = timed(project.build, update=True, run_dbt=False, warehouses=False)
        discovery = min(timed(discover_everything, project.config())[0] for _ in range(3))
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
        formulas, translate, fingerprint = identity(loaded)
        opened, drawn = render_times(project.out_dir / 'graph.html')
        unindexed = Graph.read(project.graph_path)
        cpu = time.process_time()
        index_build, _ = timed(semantic.vector_index, unindexed, project.out_dir)
        index_cpu = time.process_time() - cpu
        # With vectors.bin saved, as every search by meaning after the first finds it.
        fresh = {} if models != max(SIZES) else measure_fresh_search(root)
        meaning = median_time(search, unindexed, 'customer lifetime value', 20, None, 'semantic')
        modelled = {f'world_{k}': v for k, v in world_model(Graph.read(project.graph_path), models).items()}
        return {
            'models': models,
            'columns': sum(1 for n in graph.nodes.values() if n.kind == 'column'),
            'column_edges': sum(1 for e in graph.edges if e.kind == 'derived_from'),
            'warnings': len(graph.warnings),
            'cold_build_s': round(cold, 2),
            'update_unchanged_s': round(unchanged, 2),
            'discovery_ms': round(1000 * discovery),
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
            'load_s': round(load, 3),
            'first_impact_ms': round(1000 * first_impact, 1),
            'first_search_ms': round(1000 * first_search, 1),
            'search_ms': round(1000 * median_time(project.search, 'm010 c01'), 1),
            'query_ms': round(1000 * median_time(project.query, 'which models use m010'), 1),
            'path_ms': round(1000 * median_time(project.path_between, 'm010.c01', f'{deepest}.c00'), 1),
            'describe_ms': round(1000 * median_time(project.describe, deepest), 1),
            'conflicts_s': round(conflicts, 2),
            'formulas': formulas,
            'identity_translate_s': round(translate, 2),
            'identity_fingerprint_ms': round(1000 * fingerprint, 1),
            'viewer_open_s': None if opened is None else round(opened, 2),
            'viewer_lineage_s': None if drawn is None else round(drawn, 2),
            'vector_index_s': round(index_build, 2),
            'vector_index_cpu_s': round(index_cpu, 2),
            'vectors_bin_mb': round((project.out_dir / semantic.VECTOR_FILE).stat().st_size / 1e6, 1),
            'meaning_ms': round(1000 * meaning, 1),
            **fresh,
            **modelled,
        }


SIZES: list[int] = []


def measure_measures(tables: int) -> dict[str, Any]:
    """CPU time of conflicts over every pair of a repository of ``tables`` tables of the same
    measures, and of the gate's checks (given the diff) after an edit to one measure."""
    with tempfile.TemporaryDirectory(prefix='konstruct-bench-') as directory:
        root = Path(directory) / 'repository'
        make_measures(root, tables)
        git(root, 'init', '-q', '-b', 'main')
        git(root, 'add', '-A')
        git(root, 'commit', '-qm', 'base')
        project = Project(root)
        graph = project.build(run_dbt=False, warehouses=False).graph
        cpu = time.process_time()
        found = find_conflicts(graph)
        every = time.process_time() - cpu
        edited = root / 't7.sql'
        edited.write_text(edited.read_text().replace('* 1 as m0', '* 10 as m0'))
        graph = project.build(update=True, run_dbt=False, warehouses=False).graph
        diff = project.diff(base='HEAD')
        cpu = time.process_time()
        result = gate(graph, diff)
        checks = time.process_time() - cpu
        return {
            'tables': tables,
            'conflicts': len(found.conflicts),
            'conflicts_cpu_s': round(every, 2),
            'gate_findings': len(result.findings),
            'gate_checks_cpu_s': round(checks, 2),
        }


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
    return {
        'help_cpu_ms': cpu_ms(['--help'], root),
        'status_cpu_ms': cpu_ms(['status'], root),
        'impact_cpu_ms': cpu_ms(['impact', 'm010.c01'], root),
    }


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


def measure_fresh_search(root: Path) -> dict[str, float]:
    return {
        'search_cpu_ms': cpu_ms(['search', 'revenue'], root),
        'meaning_cpu_ms': cpu_ms(['search', '--meaning', 'revenue'], root),
    }


def star_chain(length: int, width: int = 20) -> str:
    """A chain of ``length`` CTEs, each a ``SELECT *`` of the one before, over raw.src."""
    ctes = ['c0 as (select * from raw.src)'] + [
        f'c{i} as (select * from c{i - 1} where k{i % width} > 0)' for i in range(1, length)
    ]
    columns = ', '.join(f'k{j}' for j in range(width))
    return f'with {", ".join(ctes)} select {columns} from c{length - 1}'


def measure_star_chains() -> dict[int, float]:
    """Milliseconds to analyse each chain of stars over unknown columns."""
    from konstruct.core.sql import analyse

    return {
        length: round(
            1000 * median_time(lambda sql: analyse(sql, dialect='duckdb', schema={}), star_chain(length)), 1
        )
        for length in (20, 40, 80)
    }


def self_joins() -> dict[str, tuple[str, dict[str, dict[str, str]]]]:
    """Statements that read one relation twice, with their schemas: the scale test's model template
    over one model joined to itself, and 400 columns of one table set against its parent row's."""
    model = {f'c{j:02}': 'int' for j in range(COLUMNS)}
    picks = ', '.join(
        f'a.{c}' if j % 3 == 0 else (f'b.{c} * 2 as {c}' if j % 3 == 1 else f'coalesce(a.{c}, b.{c}) as {c}')
        for j, c in enumerate(model)
    )
    template = (
        'with a as (select * from wh.analytics.m), b as (select * from wh.analytics.m), '
        f'joined as (select {picks} from a join b on a.c00 = b.c00 where b.c01 > 0) select * from joined'
    )
    wide = {'id': 'int', 'parent_id': 'int', **{f'c{j}': 'int' for j in range(400)}}
    differences = ', '.join(f'a.c{j} - b.c{j} as d{j}' for j in range(400))
    return {
        'template': (template, {'wh.analytics.m': model}),
        'wide': (
            f'select a.id, {differences} from raw.wide a join raw.wide b on b.parent_id = a.id',
            {'raw.wide': wide},
        ),
    }


def measure_self_joins() -> dict[str, float]:
    """Milliseconds to analyse each statement of :func:`self_joins`."""
    from konstruct.core.sql import analyse

    return {
        name: round(
            1000 * median_time(lambda s, known: analyse(s, dialect='duckdb', schema=known), sql, schema), 1
        )
        for name, (sql, schema) in self_joins().items()
    }


def sql_files_project(root: Path, files: int = 400) -> None:
    """``files`` SQL files and no warehouse: each table joins the one before to a raw relation,
    filters it, and every tenth selects through a star, so the columns the build lists grow as the
    files are read."""
    for i in range(files):
        source = f'main.t{i - 1:03}' if i else 'raw.src'
        select = 'a.*' if i % 10 == 5 else ', '.join(f'a.c{j}' for j in range(10))
        (root / f'f{i:03}.sql').write_text(
            f'create table main.t{i:03} as select {select}, s.k{i % 7} as extra from {source} a '
            f'join raw.side{i % 20} s on s.id = a.c0 where s.k{i % 5} > 0\n'
        )


def measure_sql_files(files: int = 400) -> dict[str, float]:
    """Seconds for a cold build and the best of three ``--update`` builds of a project of SQL files."""
    with tempfile.TemporaryDirectory(prefix='konstruct-bench-sql-') as directory:
        root = Path(directory)
        sql_files_project(root, files)
        project = Project(root)
        cold, _ = timed(project.build)
        update = min(timed(project.build, update=True)[0] for _ in range(3))
    return {'sql_files': files, 'sql_cold_build_s': round(cold, 2), 'sql_update_s': round(update, 2)}


def markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        '| Models | Columns | Column edges | Cold build | `--update`, no change | `--update`, one model edited '
        '| impact | trace | explain | text of a large impact | graph.html | Hook, no change '
        '| Hook, one model edited | Page opens | Full lineage drawn |',
        '|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|',
    ]

    def seconds(value: float | None) -> str:
        return 'n/a' if value is None else f'{value} s'

    for r in rows:
        lines.append(
            f'| {r["models"]:,} | {r["columns"]:,} | {r["column_edges"]:,} | {r["cold_build_s"]} s '
            f'| {r["update_unchanged_s"]} s | {r["update_one_model_s"]} s | {r["impact_ms"]} ms '
            f'| {r["trace_ms"]} ms | {r["explain_ms"]} ms | {r["render_ms"]} ms ({r["render_lines"]:,} lines) '
            f'| {r["graph_html_mb"]} MB | {r["hook_unchanged_s"]} s | {r["hook_one_model_s"]} s '
            f'| {seconds(r["viewer_open_s"])} | {seconds(r["viewer_lineage_s"])} |'
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
            f'`konstruct status` {startup["status_cpu_ms"]} ms, `konstruct impact m010.c01` '
            f'{startup["impact_cpu_ms"]} ms.',
        ]
    lines += [
        '',
        '| Models | Formulas | Translate | Fingerprint and group |',
        '|---:|---:|---:|---:|',
    ]
    for r in rows:
        lines.append(
            f'| {r["models"]:,} | {r["formulas"]:,} | {r["identity_translate_s"]} s '
            f'| {r["identity_fingerprint_ms"]} ms |'
        )
    lines += [
        '',
        '| Models | Index, made | Index, CPU | vectors.bin | Search by meaning |',
        '|---:|---:|---:|---:|---:|',
    ]
    for r in rows:
        lines.append(
            f'| {r["models"]:,} | {r["vector_index_s"]} s | {r["vector_index_cpu_s"]} s '
            f'| {r["vectors_bin_mb"]} MB | {r["meaning_ms"]} ms |'
        )
    lines += [
        '',
        '| Models | Entities | Relationships | Metrics | Ontology | Every metric | One metric | One entity '
        '| relate |',
        '|---:|---:|---:|---:|---:|---:|---:|---:|---:|',
    ]
    for r in rows:
        lines.append(
            f'| {r["models"]:,} | {r["world_entities"]:,} | {r["world_relationships"]:,} '
            f'| {r["world_metrics"]:,} | {r["world_ontology_s"]} s | {r["world_metrics_s"]} s '
            f'| {r["world_metric_ms"]} ms '
            f'| {r["world_entity_ms"]} ms | {r["world_relate_ms"]} ms ({r["world_relate_hops"]} relationships) |'
        )
    fresh = next((r for r in rows if 'meaning_cpu_ms' in r), None)
    if fresh:
        lines += [
            '',
            f'Search in a fresh process (CPU time, {fresh["models"]:,} models): `konstruct search revenue` '
            f'{fresh["search_cpu_ms"]} ms, `konstruct search --meaning revenue` {fresh["meaning_cpu_ms"]} ms.',
        ]
    return '\n'.join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('sizes', nargs='*', type=int, help='models (default: 100, 500, 1000 and 2000)')
    parser.add_argument(
        '--tables', nargs='+', type=int, default=[], help='also measure conflicts and the gate at these sizes'
    )
    parser.add_argument('--json', type=Path, help='also write the results as JSON')
    args = parser.parse_args()
    # Without sizes, every size is measured, unless --tables asks for conflicts and the gate alone.
    sizes = args.sizes or ([] if args.tables else [100, 500, 1000, 2000])
    SIZES.extend(sizes)
    rows = []
    for size in sizes:
        row = measure(size)
        rows.append(row)
        print(f'{size} models: {row}', file=sys.stderr)
    measured = []
    for tables in args.tables:
        row = measure_measures(tables)
        measured.append(row)
        print(f'{tables} tables: {row}', file=sys.stderr)
    # SQL over unknown columns is measured with the model sizes, as it was before --tables.
    chains = measure_star_chains() if rows else {}
    joined = measure_self_joins() if rows else {}
    sql_files = measure_sql_files() if rows else {}
    machine = (
        f'{platform.machine()}, {platform.system()} {platform.release()}, Python {platform.python_version()}'
    )
    print(f'konstruct {__version__} on {machine}\n')
    if rows:
        print(f'{COLUMNS} columns per model\n')
        print(markdown(rows))
        print(
            '\nA chain of CTEs, each a SELECT * of the one before, over unknown columns: '
            + ', '.join(f'{length} CTEs {ms} ms' for length, ms in chains.items())
            + '.'
        )
        print(
            f'A model of the scale test joined to itself: {joined["template"]} ms; 400 columns of a table set '
            f"against its parent row's: {joined['wide']} ms."
        )
        print(
            'Discovery of every input: '
            + ', '.join(f'{r["models"]} models {r["discovery_ms"]} ms' for r in rows)
            + '.'
        )
        print(
            f'{sql_files["sql_files"]} SQL files and no warehouse: cold build {sql_files["sql_cold_build_s"]} s, '
            f'`--update` {sql_files["sql_update_s"]} s.'
        )
    if measured:
        print('\n| Tables | Conflicts | conflicts, CPU | Gate checks after one edit, CPU |')
        print('|---:|---:|---:|---:|')
        for r in measured:
            print(
                f'| {r["tables"]:,} | {r["conflicts"]:,} | {r["conflicts_cpu_s"]} s '
                f'| {r["gate_checks_cpu_s"]} s ({r["gate_findings"]} findings) |'
            )
    if args.json:
        data = {
            'konstruct': __version__,
            'machine': machine,
            'results': rows,
            'tables': measured,
            'star_chains_ms': chains,
            'self_joins_ms': joined,
            **sql_files,
        }
        args.json.write_text(json.dumps(data, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
