"""A generated project far larger than jaffle_shop: build time, --update time and correctness."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from konstruct import Project
from konstruct.analysis.identity import Formulas, column_fingerprints, fingerprint
from konstruct.core.graph import Graph, Node

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


def give_entities(graph: Graph) -> None:
    """Give each model of a project from :func:`make_project` an entity, with a primary key on
    ``c00``, and a measure, a ``SUM`` around ``c02``: the generated projects have neither. Used by
    .kodex/scripts/benchmark.py and map_benchmark.py."""
    for node in graph.nodes.values():
        if node.kind == 'model':
            node.attrs['primary_key'] = ['c00']
            column = graph.column(node.id, 'c02')
            if column is not None and isinstance(column.attrs.get('expression'), str):
                column.attrs['expression'] = f'SUM({column.attrs["expression"]})'
    graph.changed()


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
    # The two builds read the same files, written at different times in different places.
    data['meta']['watched_files'] = [
        {'path': f['path'], 'size': f['size']} for f in data['meta'].get('watched_files', [])
    ]
    for siblings in data['meta'].get('watched_siblings', []):
        del siblings['found'], siblings['digest']
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


DAG_TEMPLATE = '''"""Load and model the {name} feed."""

from datetime import datetime

from airflow import DAG
from airflow.decorators import task
from airflow.operators.bash import BashOperator
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator

SCHEMA = "analytics"

with DAG(
    dag_id="feed_{index}",
    schedule="@daily",
    start_date=datetime(2024, 1, 1),
    tags=["feeds", "group{group}"],
    description=f"The {name} feed, loaded into {{SCHEMA}}",
) as dag:
    seed = BashOperator(task_id="seed", bash_command="cd /opt/shop && dbt seed --select {name}")
    staged = BashOperator(
        task_id="staged",
        bash_command="dbt run --select staging.{name}+ --exclude tag:slow && dbt test --select {name}",
    )
    summary = SQLExecuteQueryOperator(
        task_id="summary",
        sql=(
            f"insert into {{SCHEMA}}.{name}_daily "
            "select day, count(*) as events, sum(amount) as revenue "
            f"from {{SCHEMA}}.{name}_events group by day"
        ),
    )

    @task
    def notify():
        """Tell the owners the {name} feed is loaded."""
        return "done"

    check = BashOperator(task_id="check", bash_command="echo ok")
    publish = BashOperator(task_id="publish", bash_command="echo ok")

    seed >> staged >> summary >> notify()
    summary >> check >> publish
'''

OTHER_TEMPLATE = '''"""A helper module of the {name} service."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)
{mention}

@dataclass
class {name}Record:
    key: str
    values: dict[str, int] = field(default_factory=dict)

    def total(self) -> int:
        return sum(self.values.values())


def load(path: Path) -> list[{name}Record]:
    """Read the records saved at ``path``."""
    found = []
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        item = json.loads(line)
        found.append({name}Record(key=item["key"], values=item.get("values", {{}})))
    log.info("read %d records from %s", len(found), path)
    return found


def merge(left: {name}Record, right: {name}Record) -> {name}Record:
    values = dict(left.values)
    for key, count in right.values.items():
        values[key] = values.get(key, 0) + count
    return {name}Record(key=left.key, values=values)


class {name}Service:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.records: dict[str, {name}Record] = {{}}

    def refresh(self) -> int:
        for path in sorted(self.root.glob("*.jsonl")):
            for record in load(path):
                known = self.records.get(record.key)
                self.records[record.key] = merge(known, record) if known else record
        return len(self.records)
'''

ASSETS_TEMPLATE = '''from dagster import AssetKey, Definitions, asset, define_asset_job


@asset(group_name="feeds")
def raw_{index}():
    """The raw {index} feed."""
    return []


@asset(group_name="feeds", deps=[AssetKey("source_{index}")])
def cleaned_{index}(raw_{index}):
    return raw_{index}


feeds_{index} = define_asset_job("feeds_{index}", selection="cleaned_{index}")
defs = Definitions(assets=[raw_{index}, cleaned_{index}], jobs=[feeds_{index}])
'''


def make_orchestrator_repo(root: Path, dags: int, others: int, assets: int = 0) -> None:
    """A repository of ``dags`` Airflow DAG files, ``assets`` Dagster definition files and
    ``others`` Python files that are neither (one in twenty mentions Airflow without defining a
    DAG), in directories of 100. Also used by .kodex/scripts/orchestrator_benchmark.py."""
    for kind, count in (('dags', dags), ('assets', assets), ('lib', others)):
        for index in range(count):
            directory = root / kind / f'part{index // 100:03}'
            directory.mkdir(parents=True, exist_ok=True)
            name = f'feed{index}'
            if kind == 'dags':
                text = DAG_TEMPLATE.format(name=name, index=index, group=index % 7)
            elif kind == 'assets':
                text = ASSETS_TEMPLATE.format(index=index)
            else:
                mention = '# Scheduled by the airflow deployment.\n' if index % 20 == 0 else ''
                text = OTHER_TEMPLATE.format(name=name.capitalize(), mention=mention)
            (directory / f'{name}.py').write_text(text)


def test_orchestrator_files_are_found_and_read_at_scale(tmp_path: Path) -> None:
    """Finding the DAG and Dagster files among thousands of Python files, and reading each DAG
    file, take seconds of wall time at most, and find exactly the generated files."""
    from konstruct.engine.config import discover_dag_files, discover_dagster_files, load_config
    from konstruct.inputs.airflow import parse_file

    dags, assets, others = 400, 100, 1000
    make_orchestrator_repo(tmp_path, dags, others, assets)
    config = load_config(tmp_path)
    started = time.perf_counter()
    found_dags = discover_dag_files(config)
    found_assets = discover_dagster_files(config)
    discovery = time.perf_counter() - started
    assert len(found_dags) == dags
    assert {p.parent.parent.name for p in found_dags} == {'dags'}
    assert len(found_assets) == assets
    started = time.perf_counter()
    parsed = [parse_file(path) for path in found_dags]
    reading = time.perf_counter() - started
    assert all(len(d) == 1 and len(d[0].tasks) == 6 and d[0].warnings == [] for d in parsed)
    seven = {d[0].dag_id: d[0] for d in parsed}['feed_7']
    assert seven.dependencies == [
        ('seed', 'staged'),
        ('staged', 'summary'),
        ('summary', 'notify'),
        ('summary', 'check'),
        ('check', 'publish'),
    ]
    assert discovery < 30, f'finding the files took {discovery:.1f}s'
    assert reading < 30, f'reading {dags} DAG files took {reading:.1f}s'
    print(
        f'\n{dags + assets + others} files: discovery {discovery:.2f}s, '
        f'reading {dags} DAG files {reading:.2f}s'
    )


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


def _measures(depth: int = 0, wide: int = 0, fresh: int = 0) -> Graph:
    """A measure that may be NULL in five ways (five CASEs whose branch may be NULL), then a
    lineage of ``depth`` models that each add a nullable source column to it, ``wide`` models
    that each add three of their own to it, as a mart's columns over one measure, and a lineage
    of ``fresh`` models that each add two CASEs of their own to the last."""
    graph = Graph()
    source = graph.add_node(Node('source.raw.src', 'source', 'src', relation='db.raw.src'))
    names = [f'{p}{i}' for p in 'ca' for i in range(5)] + [f'x{j}' for j in range(depth + 3 * wide)]
    names += [f'{p}{j}' for p in 'de' for j in range(2 * fresh)]
    for name in names:
        graph.add_column(source, name)

    def model(name: str, expression: str, inputs: list[str]) -> Node:
        node = graph.add_node(Node(f'model.{name}', 'model', name, relation=f'db.s.{name}'))
        column = graph.add_column(node, 'v', attrs={'expression': expression})
        for found in inputs:
            graph.add_edge(column.id, found, 'derived_from')
        return column

    def case(i: int, p: str = 'c', q: str = 'a') -> str:
        return f'CASE WHEN db.raw.src.{p}{i} > 0 THEN db.raw.src.{q}{i} ELSE 0 END'

    model(
        'm0',
        ' + '.join(case(i) for i in range(5)),
        [f'source.raw.src#{p}{i}' for p in 'ca' for i in range(5)],
    )
    for j in range(1, depth + 1):
        model(
            f'm{j}',
            f'db.s.m{j - 1}.v + db.raw.src.x{j - 1}',
            [f'model.m{j - 1}#v', f'source.raw.src#x{j - 1}'],
        )
    if depth:
        # The last model again, its operands the other way round.
        model(
            'last',
            f'db.raw.src.x{depth - 1} + db.s.m{depth - 1}.v',
            [f'model.m{depth - 1}#v', f'source.raw.src#x{depth - 1}'],
        )
    for j in range(wide):
        own = [f'x{depth + 3 * j + i}' for i in range(3)]
        model(
            f'w{j}',
            ' + '.join(['db.s.m0.v', *(f'db.raw.src.{x}' for x in own)]),
            ['model.m0#v', *(f'source.raw.src#{x}' for x in own)],
        )
    for j in range(1, fresh + 1):
        upstream = 'm0' if j == 1 else f'f{j - 1}'
        cases = [case(2 * j - 2, 'd', 'e'), case(2 * j - 1, 'd', 'e')]
        inputs = [f'model.{upstream}#v'] + [f'source.raw.src#{p}{2 * j - i}' for p in 'de' for i in (1, 2)]
        model(f'f{j}', ' + '.join([f'db.s.{upstream}.v', *cases]), inputs)
        if j == fresh:
            model('fresh', ' + '.join([*cases[::-1], f'db.s.{upstream}.v']), inputs)
    return graph


def test_formulas_with_presences_of_many_terms_stay_cheap() -> None:
    # The presence of five CASEs has 243 terms: a nullable column joining it, at every step of
    # a long lineage or in every column of a wide mart, costs its atoms, not terms times atoms.
    # Past the limit on terms, a lineage that adds factors at every step shares the factors of
    # the step before. CPU time, bounded at about ten times what a laptop takes.
    graph = _measures(depth=2000, wide=5000)
    started = time.process_time()
    prints = column_fingerprints(graph)
    lineage = time.process_time() - started
    assert prints['model.last#v'] == prints['model.m2000#v'] != prints['model.m1999#v']
    assert len({prints[f'model.w{j}#v'] for j in range(5000)}) == 5000
    # The core's part alone, without translating the formulas.
    graph = _measures(fresh=4000)
    formulas = Formulas(graph, fixed_key=True)
    names = ['model.f3999#v', 'model.f4000#v', 'model.fresh#v']
    definitions = [formulas.column(graph.nodes[name]) for name in names]
    started = time.process_time()
    before, last, reordered = formulas.fingerprints(definitions)
    fresh = time.process_time() - started
    assert reordered == last != before
    started = time.process_time()
    cases = [f'CASE WHEN c{i} > 0 THEN a{i} ELSE 0 END' for i in range(5)]
    columns = [f'x{j}' for j in range(2000)]
    assert fingerprint(' + '.join(cases + columns)) == fingerprint(' + '.join(columns[::-1] + cases[::-1]))
    flat = time.process_time() - started
    print(f'\nlineage and mart {lineage:.2f}s, fresh CASEs {fresh:.2f}s, flat {flat:.2f}s of CPU')
    assert lineage < 10, f'7,000 columns over a presence of 243 terms took {lineage:.1f}s of CPU'
    assert fresh < 1, f'a lineage of 4,000 models adding two CASEs each took {fresh:.1f}s of CPU'
    assert flat < 5, f'a sum of 5 CASEs and 2,000 columns took {flat:.1f}s of CPU'
