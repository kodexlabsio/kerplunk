"""A generated project far larger than jaffle_shop: build time, --update time and correctness."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from konstruct import Project

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

    started = time.perf_counter()
    again = project.build(update=True, run_dbt=False, warehouses=False)
    incremental = time.perf_counter() - started
    assert again.cache_misses == 0
    assert incremental < full / 2, f'--update took {incremental:.1f}s against {full:.1f}s'

    walk = project.impact('m010.c01')
    assert walk.columns
    assert {t.label for t in walk.tasks} >= {'group0.run'}
    deepest = project.trace(f'm{MODELS - 1:03}.c00')
    assert any(c.label.startswith('raw_') for c in deepest.columns)
    print(f'\n{MODELS} models: full build {full:.1f}s, --update {incremental:.1f}s, {lineage} lineage edges')


def _graph(root: Path) -> str:
    data = json.loads((root / 'konstruct-out' / 'graph.json').read_text())
    data['meta'] = {
        k: v for k, v in data['meta'].items() if k not in ('root', 'generated_at', 'build_seconds')
    }
    return json.dumps(data, sort_keys=True).replace(str(root), 'ROOT')


def test_models_analysed_in_workers_give_the_same_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from konstruct import build

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
    from konstruct import build

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
    from konstruct import build

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

    from konstruct import build

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
    from konstruct import build

    # -P stops Python putting the working directory first on sys.path.
    assert build._worker_command()[1:] == ['-P', '-m', 'konstruct.worker']
    workers = build._Workers()
    directory = None
    try:
        assert workers.map([['select 1 as a', 'duckdb', None, None, None, None]]) is not None
        directory = workers.directory
        assert directory is not None and not any(Path(directory).iterdir())
    finally:
        workers.close()
    assert directory is not None and not Path(directory).exists()
