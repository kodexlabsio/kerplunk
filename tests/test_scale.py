"""A generated project far larger than jaffle_shop: build time, --update time and correctness."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from konstruct import Project

from .test_gate import git

pytestmark = pytest.mark.scale

MODELS = 300
COLUMNS = 15


def make_project(root: Path, models: int = MODELS, columns_per_model: int = COLUMNS) -> None:
    """A dbt project of ``models`` models in layers (each joins two earlier models through CTEs),
    its manifest and catalog, and Airflow DAGs that run it in groups of 50. Also used by
    .kodex/scripts/benchmark.py."""
    (root / 'models').mkdir(parents=True)
    (root / 'dbt_project.yml').write_text('name: big\nprofile: big\n')
    nodes: dict[str, Any] = {}
    catalog: dict[str, Any] = {}

    def relation(name: str) -> str:
        return f'"wh"."analytics"."{name}"'

    for index in range(models):
        name = f'm{index:03}'
        unique_id = f'model.big.{name}'
        columns = [f'c{j:02}' for j in range(columns_per_model)]
        if index < 10:
            sql = f'select {", ".join(columns)} from {relation("raw_" + name)}'
            depends: list[str] = []
        else:
            left, right = f'm{index // 2:03}', f'm{index // 3:03}'
            depends = [f'model.big.{left}', f'model.big.{right}']
            picks = ', '.join(
                f'a.{c}'
                if j % 3 == 0
                else (f'b.{c} * 2 as {c}' if j % 3 == 1 else f'coalesce(a.{c}, b.{c}) as {c}')
                for j, c in enumerate(columns)
            )
            sql = (
                f'with a as (select * from {relation(left)}), b as (select * from {relation(right)}), '
                f'joined as (select {picks} from a join b on a.c00 = b.c00 where b.c01 > 0) '
                'select * from joined'
            )
        nodes[unique_id] = {
            'unique_id': unique_id,
            'resource_type': 'model',
            'name': name,
            'alias': name,
            'package_name': 'big',
            'database': 'wh',
            'schema': 'analytics',
            'relation_name': relation(name),
            'original_file_path': f'models/{name}.sql',
            'fqn': ['big', f'group{index // 50}', name],
            'raw_code': sql,
            'compiled_code': sql,
            'language': 'sql',
            'depends_on': {'nodes': depends, 'macros': []},
            'config': {'materialized': 'table'},
            'tags': [f'group{index // 50}'],
            'columns': {},
            'checksum': {'checksum': str(index)},
        }
        catalog[unique_id] = {
            'unique_id': unique_id,
            'metadata': {},
            'columns': {c: {'name': c, 'type': 'INTEGER', 'index': j + 1} for j, c in enumerate(columns)},
        }
        (root / 'models' / f'{name}.sql').write_text(sql)
    target = root / 'target'
    target.mkdir()
    (target / 'manifest.json').write_text(
        json.dumps({'metadata': {'adapter_type': 'duckdb'}, 'nodes': nodes, 'sources': {}, 'exposures': {}})
    )
    (target / 'catalog.json').write_text(json.dumps({'nodes': catalog, 'sources': {}}))
    dags = root / 'dags'
    dags.mkdir()
    for group in range(max(1, models // 50)):
        (dags / f'group{group}.py').write_text(
            'from airflow.sdk import DAG\n'
            'from airflow.providers.standard.operators.bash import BashOperator\n'
            f"with DAG('group{group}'):\n"
            f"    run = BashOperator(task_id='run', bash_command='dbt run -s tag:group{group}')\n"
            f"    test = BashOperator(task_id='test', bash_command='dbt test -s tag:group{group}')\n"
            '    run >> test\n'
        )


def test_large_project(tmp_path: Path) -> None:
    make_project(tmp_path)
    project = Project(tmp_path)
    started = time.perf_counter()
    result = project.build(run_dbt=False, warehouses=False)
    full = time.perf_counter() - started
    graph = result.graph
    assert graph.warnings == []
    assert sum(1 for n in graph.nodes.values() if n.kind == 'model') == MODELS
    lineage = sum(1 for e in graph.edges if e.kind == 'derived_from')
    assert lineage >= (MODELS - 10) * COLUMNS
    assert full < 60, f'full build took {full:.1f}s'

    # The best of three, as a single run on a busy hosted runner can take half a full build.
    timings = []
    for _ in range(3):
        started = time.perf_counter()
        again = project.build(update=True, run_dbt=False, warehouses=False)
        timings.append(time.perf_counter() - started)
        assert again.cache_misses == 0
    incremental = min(timings)
    assert incremental < full / 2, f'--update took {incremental:.1f}s against {full:.1f}s'

    walk = project.impact('m010.c01')
    assert walk.columns
    assert {t.label for t in walk.tasks} >= {'group0.run'}
    deepest = project.trace(f'm{MODELS - 1:03}.c00')
    assert any(c.label.startswith('raw_') for c in deepest.columns)
    print(f'\n{MODELS} models: full build {full:.1f}s, --update {incremental:.1f}s, {lineage} lineage edges')


def test_the_world_model_at_scale(tmp_path: Path) -> None:
    """Every metric, one entity and a relate on the generated project, once each model has an
    entity and a measure (as .kodex/scripts/benchmark.py gives them)."""
    import importlib.util

    script = Path(__file__).resolve().parents[1] / '.kodex' / 'scripts' / 'benchmark.py'
    spec = importlib.util.spec_from_file_location('benchmark', script)
    assert spec is not None and spec.loader is not None
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    make_project(tmp_path)
    graph = Project(tmp_path).build(run_dbt=False, warehouses=False).graph
    found = benchmark.world_model(graph, MODELS)
    assert found['entities'] == MODELS and found['metrics'] == 2 * MODELS
    assert found['relate_hops'] > 1
    assert found['metrics_s'] < 10 and found['entity_ms'] < 1000 and found['relate_ms'] < 500, found
    print(f'\n{MODELS} models: {found}')


def _graph(root: Path) -> str:
    data = json.loads((root / 'konstruct-out' / 'graph.json').read_text())
    data['meta'] = {
        k: v for k, v in data['meta'].items() if k not in ('root', 'generated_at', 'build_seconds')
    }
    return json.dumps(data, sort_keys=True).replace(str(root), 'ROOT')


def test_models_analysed_in_workers_give_the_same_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from konstruct.engine import build

    monkeypatch.setattr(build, 'PARALLEL_MINIMUM', 4)
    used: list[int] = []
    real_close = build._Workers.close

    def close(self: Any) -> None:
        used.append(self.used)
        real_close(self)

    monkeypatch.setattr(build._Workers, 'close', close)
    for catalog in (True, False):
        graphs = []
        for workers in ('1', '3'):
            monkeypatch.setenv('KONSTRUCT_WORKERS', workers)
            root = tmp_path / f'{catalog}-{workers}'
            root.mkdir()
            make_project(root, 80, 6)
            if not catalog:
                # Columns are then inferred onto shared tables as models are applied.
                (root / 'target' / 'catalog.json').unlink()
            Project(root).build(warehouses=False, run_dbt=False)
            graphs.append(_graph(root))
        assert graphs[0] == graphs[1]
    assert used[1] > 0 and used[3] > 0  # the three-worker builds used results from workers


def test_workers_import_nothing_from_the_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from konstruct.engine import build

    monkeypatch.setattr(build, 'PARALLEL_MINIMUM', 4)
    monkeypatch.setenv('KONSTRUCT_WORKERS', '2')
    make_project(tmp_path, 40, 4)
    marker = tmp_path / 'ran'
    for module in ('orjson', 'sqlglot', 'konstruct'):
        (tmp_path / f'{module}.py').write_text(f'open({str(marker)!r}, "w").write("{module}")\n')
    monkeypatch.chdir(tmp_path)
    graph = Project(tmp_path).build(warehouses=False, run_dbt=False).graph
    assert not marker.exists()
    assert graph.meta['inputs']['dbt_models'] == 40


def test_a_failing_build_stops_its_workers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from konstruct.engine import build

    monkeypatch.setattr(build, 'PARALLEL_MINIMUM', 4)
    monkeypatch.setenv('KONSTRUCT_WORKERS', '2')
    make_project(tmp_path, 40, 4)
    started: list[Any] = []
    real_map = build._Workers.map

    def map_(self: Any, jobs: list[Any]) -> Any:
        result = real_map(self, jobs)
        started.extend(self.processes)
        return result

    calls = {'n': 0}
    real_apply = build.Builder.apply

    def apply(self: Any, *args: Any, **kwargs: Any) -> None:
        calls['n'] += 1
        if calls['n'] == 30:
            raise RuntimeError('a failure in the middle of the build')
        real_apply(self, *args, **kwargs)

    monkeypatch.setattr(build._Workers, 'map', map_)
    monkeypatch.setattr(build.Builder, 'apply', apply)
    with pytest.raises(RuntimeError):
        Project(tmp_path).build(warehouses=False, run_dbt=False)
    assert started and all(p.poll() is not None for p in started)


def test_workers_that_cannot_start_leave_the_build_to_do_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from konstruct.engine import build

    monkeypatch.setattr(build, 'PARALLEL_MINIMUM', 4)
    monkeypatch.setenv('KONSTRUCT_WORKERS', '2')
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'no-python'))
    make_project(tmp_path, 40, 4)
    seen: list[tuple[bool, int]] = []
    real_close = build._Workers.close

    def close(self: Any) -> None:
        seen.append((self.failed, self.used))
        real_close(self)

    monkeypatch.setattr(build._Workers, 'close', close)
    graph = Project(tmp_path).build(warehouses=False, run_dbt=False).graph
    assert graph.meta['inputs']['dbt_models'] == 40
    assert not graph.warnings
    # The workers did fail to start, and the build analysed every model itself.
    assert seen and seen[0] == (True, 0)


def test_workers_keep_their_directory_off_the_path() -> None:
    from konstruct.engine import build

    # -P stops Python putting the working directory first on sys.path.
    assert build._worker_command()[1:] == ['-P', '-m', 'konstruct.engine.worker']
    workers = build._Workers()
    directory = None
    try:
        assert workers.map([['select 1 as a', 'duckdb', None, None, None, None, []]]) is not None
        directory = workers.directory
        assert directory is not None and not any(Path(directory).iterdir())
    finally:
        workers.close()
    assert directory is not None and not Path(directory).exists()


MEASURES = 5


def make_measures(root: Path, tables: int) -> None:
    """A repository of ``tables`` SQL tables with the same five measures, each table reading its own
    source: every two tables compute each measure from another source, so there is a conflict for
    every pair of tables and measure. Also used by .kodex/scripts/benchmark.py."""
    root.mkdir(parents=True)
    (root / 'konstruct.toml').write_text('dialect = "duckdb"\n')
    (root / '.gitignore').write_text('konstruct-out/\n')
    for index in range(tables):
        measures = ', '.join(f'sum(c{j}) * {j + 1} as m{j}' for j in range(MEASURES))
        (root / f't{index}.sql').write_text(
            f'create table main.t{index} as with s as (select k, c0, c1, c2, c3, c4 from raw.r{index} '
            f"where status <> 'x') select k, {measures} from s group by k\n"
        )


def test_conflicts_and_the_gate_stay_fast(tmp_path: Path) -> None:
    """Each column's side of a pair is worked out once, and the gate compares only the pairs of
    the columns a change alters. CPU time, bounded at about ten times what a laptop takes."""
    from konstruct.analysis.conflicts import find_conflicts
    from konstruct.analysis.gate import gate

    tables = 120
    root = tmp_path / 'repository'
    make_measures(root, tables)
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'add', '-A')
    git(root, 'commit', '-qm', 'base')
    project = Project(root)
    graph = project.build(run_dbt=False, warehouses=False).graph
    started = time.process_time()
    every = find_conflicts(graph)
    whole = time.process_time() - started
    assert len(every.conflicts) == MEASURES * tables * (tables - 1) // 2
    assert whole < 10, f'conflicts over {len(every.conflicts)} pairs took {whole:.1f}s of CPU'

    edited = root / 't7.sql'
    edited.write_text(edited.read_text().replace('* 1 as m0', '* 10 as m0'))
    graph = project.build(update=True, run_dbt=False, warehouses=False).graph
    diff = project.diff(base='HEAD')
    started = time.process_time()
    result = gate(graph, diff)
    gated = time.process_time() - started
    assert {f.rule for f in result.findings} == {'conflicting-definition'}
    assert len(result.findings) == tables - 1
    assert gated < 1, f'the gate took {gated:.1f}s of CPU'
    # The same findings as from every conflict, which is what the gate is given when a plugin's check runs.
    assert gate(graph, diff, find_conflicts(graph)).findings == result.findings
    print(f'\n{tables} tables: conflicts {whole:.2f}s of CPU, the gate after one edit {gated:.2f}s')
