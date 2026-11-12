"""Dagster: the static parser, its place in the graph, and parity with Dagster itself."""

from __future__ import annotations

import copy
import json
import pickle
import shutil
import subprocess
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from konstruct import Project
from konstruct.inputs.dagster import parse_file, parse_source

FIXTURE = Path(__file__).parent / 'fixtures' / 'dagster' / 'definitions.py'


def test_fixture_assets_dependencies_jobs_and_schedule() -> None:
    parsed = parse_file(FIXTURE)
    assets = {a.key: a for a in parsed.assets}
    assert set(assets) == {
        'jaffle_shop_dbt',
        'reporting/customer_ltv',
        'loyal_customers',
        'order_counts',
        'order_totals',
        'crm_export',
    }
    assert assets['jaffle_shop_dbt'].dbt_select == ['staging', 'customers', 'orders']
    assert assets['reporting/customer_ltv'].deps == ['customers']
    assert assets['reporting/customer_ltv'].group == 'reporting'
    assert assets['reporting/customer_ltv'].kinds == ['duckdb']
    assert assets['reporting/customer_ltv'].sql and 'main.customers' in assets['reporting/customer_ltv'].sql
    assert assets['loyal_customers'].deps == ['reporting/customer_ltv']
    assert assets['order_totals'].description == 'Total value per day'
    assert assets['order_counts'].deps == ['orders']
    assert assets['crm_export'].deps == ['loyal_customers', 'order_totals']
    jobs = {j.name: j for j in parsed.jobs}
    assert jobs['reporting_job'].selection == ['group:reporting']
    assert jobs['reporting_job'].schedule == '0 6 * * *'
    assert jobs['everything'].selection == ['*']
    assert parsed.warnings == []


def test_fixture_op_job_schedule_and_sensors() -> None:
    parsed = parse_file(FIXTURE)
    ops = {o.name: o for o in parsed.ops}
    assert set(ops) == {'copy_orders', 'total_by_day', 'notify', 'refresh_customers'}
    assert ops['total_by_day'].function == 'summarise'
    assert ops['copy_orders'].sql and 'from main.orders' in ops['copy_orders'].sql
    assert ops['refresh_customers'].dbt_commands == ['dbt run --select customers']
    job = next(j for j in parsed.jobs if j.name == 'archive_orders')
    assert job.schedule == '0 2 * * *'
    assert {c.handle: c.upstream for c in job.ops} == {
        'archive.copy_orders': [],
        'archive.total_by_day': ['archive.copy_orders'],
        'notify': ['archive.total_by_day'],
        'notify_finance': ['archive.total_by_day'],
        'archive_again.copy_orders': [],
        'archive_again.total_by_day': ['archive_again.copy_orders'],
        'refresh_customers': ['archive.total_by_day', 'archive_again.total_by_day'],
    }
    sensors = {s.name: s for s in parsed.sensors}
    assert sensors['ltv_refreshed'].assets == ['reporting/customer_ltv']
    assert sensors['ltv_refreshed'].jobs == ['archive_orders']
    assert sensors['after_archive'].monitored_jobs == ['archive_orders']
    assert sensors['after_archive'].status == 'SUCCESS'
    assert sensors['archive_failed'].status == 'FAILURE'


def test_op_composition() -> None:
    source = textwrap.dedent(
        """
        import subprocess
        from dagster import Definitions, SensorDefinition, graph, job, op, schedule, sensor

        @op
        def fan_out(): ...

        @op
        def work(item): ...

        @op
        def gather(items): ...

        @op
        def load(): subprocess.run(["dbt", "build", "--select", "tag:nightly"])

        @graph
        def pipeline():
            results = fan_out().map(work)
            gather(results.collect())
            fan_out().map(lambda item: work(item))
            first, second = load(), load()
            gather([first, second])

        @graph(name="looping")
        def looping():
            for _ in range(3):
                load()

        @graph
        def recursive():
            recursive()

        @job
        def direct():
            load.alias("a")()
            load.configured({}, name="b")()

        pipeline_job = pipeline.to_job(name="pipeline_job", description="Mapped work")

        @schedule(cron_schedule="@daily", job_name="direct")
        def daily(context): ...

        @sensor(job=pipeline_job)
        def poke(context): ...

        @sensor()
        def orphan(context): ...

        watcher = SensorDefinition(name="watcher", evaluation_fn=lambda c: None, job=direct)
        defs = Definitions(jobs=[direct, pipeline_job, looping, recursive])
        """
    )
    parsed = parse_source(Path('d.py'), source)
    jobs = {j.name: j for j in parsed.jobs}
    assert set(jobs) == {'direct', 'pipeline_job', 'looping', 'recursive'}
    assert {c.handle: c.upstream for c in jobs['pipeline_job'].ops} == {
        'fan_out': [],
        'work': ['fan_out'],
        'gather': ['work'],
        'fan_out_2': [],
        'work_2': ['fan_out_2'],
        'load': [],
        'load_2': [],
        'gather_2': ['load', 'load_2'],
    }
    assert jobs['pipeline_job'].description == 'Mapped work'
    assert [c.handle for c in jobs['direct'].ops] == ['a', 'b']
    assert jobs['direct'].schedule == '@daily'
    assert [c.handle for c in jobs['looping'].ops] == ['load']
    assert jobs['recursive'].ops == []
    assert {o.name: o.dbt_commands for o in parsed.ops}['load'] == ['dbt build --select tag:nightly']
    sensors = {s.name: s.jobs for s in parsed.sensors}
    assert sensors == {'poke': ['pipeline_job'], 'watcher': ['direct']}
    assert any('loop in graph looping' in w for w in parsed.warnings)
    assert any('graph recursive invokes itself' in w for w in parsed.warnings)


def test_keys_resources_and_selections(tmp_path: Path) -> None:
    source = textwrap.dedent(
        """
        from dagster import AssetKey, AssetSelection, asset, define_asset_job
        from dagster_duckdb import DuckDBResource

        PREFIX = "lake"

        @asset(key=AssetKey(["lake", "events"]))
        def events(warehouse: DuckDBResource):
            ...

        @asset(name="sessions", key_prefix=PREFIX, deps=[events, "lake/users", AssetKey("ref")])
        def build_sessions(context, warehouse: DuckDBResource):
            warehouse.get_connection().execute("insert into lake.sessions select * from lake.events")

        job = define_asset_job("lake_job", selection=AssetSelection.keys("lake/events", ["lake", "sessions"]))
        other = define_asset_job(f"job_{PREFIX}_{unknown}", selection="*")
        """
    )
    parsed = parse_source(tmp_path / 'd.py', source)
    assets = {a.key: a for a in parsed.assets}
    assert set(assets) == {'lake/events', 'lake/sessions'}
    assert assets['lake/events'].deps == []
    assert assets['lake/sessions'].deps == ['lake/events', 'lake/users', 'ref']
    assert assets['lake/sessions'].sql == 'insert into lake.sessions select * from lake.events'
    assert parsed.jobs[0].selection == ['lake/events', 'lake/sessions']
    assert any('not a constant' in w for w in parsed.warnings)


def test_round_trip() -> None:
    from konstruct.inputs.dagster import DagsterFile

    parsed = parse_file(FIXTURE)
    assert DagsterFile.from_dict(parsed.to_dict()) == parsed
    assert DagsterFile.from_dict(json.loads(json.dumps(parsed.to_dict()))) == parsed


def test_a_syntax_error_names_its_line_and_a_missing_file_raises(tmp_path: Path) -> None:
    path = tmp_path / 'broken.py'
    path.write_text('from dagster import asset\n@asset\ndef a(:\n    pass\n')
    with pytest.raises(SyntaxError) as raised:
        parse_file(path)
    assert raised.value.filename == str(path) and raised.value.lineno == 3
    with pytest.raises(FileNotFoundError):
        parse_file(tmp_path / 'missing.py')


def test_calls_are_visited_breadth_first_as_ast_walk_visits_them(tmp_path: Path) -> None:
    """Calls assigned to a name come first, then the others by their depth in the tree and, at the
    same depth, their place in it."""
    parsed = parse_source(
        tmp_path / 'x.py',
        textwrap.dedent(
            """
            from dagster import define_asset_job

            def f(a=define_asset_job("in_default")):
                return [define_asset_job("in_return")]

            first = define_asset_job("module_level")
            wrapped = [define_asset_job("in_list")]
            """
        ),
    )
    assert [job.name for job in parsed.jobs] == ['module_level', 'in_default', 'in_list', 'in_return']


def test_records_are_edited_in_place_like_dataclasses(tmp_path: Path) -> None:
    parsed = parse_file(FIXTURE)
    asset = parsed.assets[0]
    assert asset is parsed.assets[0] and asset.deps is asset.deps
    asset.deps.append('extra')
    asset.group = 'moved'
    assert parsed.assets[0].deps[-1] == 'extra' and parsed.assets[0].group == 'moved'
    assert 'AssetInfo(key=' in repr(asset)


def test_files_are_read_from_many_threads_at_once() -> None:
    expected = parse_file(FIXTURE).to_dict()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: parse_file(FIXTURE).to_dict(), range(64)))
    assert all(result == expected for result in results)


@pytest.mark.parametrize(
    'body',
    ['x = ' + '-' * 200_000 + '1', 'x = ' + ' + '.join(['1'] * 20_000)],
    ids=['minus', 'sum'],
)
def test_nesting_too_deep_to_read_is_a_syntax_error(tmp_path: Path, body: str) -> None:
    with pytest.raises(SyntaxError):
        parse_source(tmp_path / 'x.py', 'from dagster import asset\n' + body + '\n')


@pytest.mark.dbt
def test_dagster_assets_join_dbt_and_warehouse_lineage(built_jaffle: Path, tmp_path: Path) -> None:
    root = tmp_path / 'shop'
    shutil.copytree(built_jaffle, root)
    shutil.rmtree(root / 'dags')
    (root / 'orchestration').mkdir()
    shutil.copy(FIXTURE, root / 'orchestration' / 'definitions.py')
    from .conftest import run_dbt

    run_dbt(root, 'docs', 'generate')
    project = Project(root)
    result = project.build(run_dbt=False)
    graph = result.graph
    assert result.inputs['dagster_assets'] == 6
    assert result.inputs['dagster_ops'] == 7
    assert any(
        s['input'] == 'dagster' and s['detail'] == '6 assets, 4 ops, 3 jobs, 3 sensors'
        for s in graph.meta['sources']
    )

    dbt_task = graph.nodes['task.dagster.jaffle_shop_dbt']
    runs = {graph.nodes[e.target].name for e in graph.out_edges(dbt_task.id, 'runs')}
    assert runs == {'stg_customers', 'stg_orders', 'stg_payments', 'customers', 'orders'}

    ltv = graph.nodes['task.dagster.reporting/customer_ltv']
    warehouse_table = graph.node_for_relation('jaffle_shop.reporting.customer_ltv')
    assert warehouse_table is not None
    assert (ltv.id, warehouse_table.id) in {(e.source, e.target) for e in graph.out_edges(ltv.id, 'feeds')}
    trace = project.trace('customer_ltv.lifetime_value')
    assert [c.label for c in trace.columns][:1] == ['customers.customer_lifetime_value']

    impact = project.impact('customers.customer_lifetime_value')
    assert 'reporting/customer_ltv' in {t.name for t in impact.tasks}
    downstream = project.impact('reporting/customer_ltv')
    assert 'loyal_customers' in {t.name for t in downstream.tasks}

    job = graph.nodes['dag.reporting_job']
    assert job.attrs['schedule'] == '0 6 * * *'
    assert {graph.nodes[e.target].name for e in graph.out_edges(job.id, 'runs')} == {
        'reporting/customer_ltv',
        'loyal_customers',
        'order_counts',
        'order_totals',
    }

    # The op-based job: a task per op invocation, column lineage from the SQL each op runs.
    archive = graph.nodes['dag.archive_orders']
    assert archive.attrs['schedule'] == '0 2 * * *'
    assert archive.attrs['sensors'] == ['ltv_refreshed on asset reporting/customer_ltv']
    handles = {graph.nodes[e.target].name for e in graph.out_edges(archive.id, 'runs')}
    assert 'archive_again.total_by_day' in handles and len(handles) == 7
    totals = project.trace('daily_totals.total')
    assert [c.id for c in totals.columns] == [
        'table.jaffle_shop.archive.orders#amount',
        'model.jaffle_shop.orders#amount',
        'model.jaffle_shop.stg_payments#amount',
        'seed.jaffle_shop.raw_payments#amount',
    ]
    impact = project.impact('model.jaffle_shop.orders#amount')
    assert {'archive.copy_orders', 'archive_again.copy_orders'} <= {t.name for t in impact.tasks}
    summarise = graph.nodes['task.archive_orders.archive.total_by_day']
    assert 'op' not in summarise.attrs
    assert summarise.attrs['function'] == 'summarise'
    assert {graph.nodes[e.source].name for e in graph.in_edges(summarise.id, 'precedes')} == {
        'archive.copy_orders'
    }
    refresh = graph.nodes['task.archive_orders.refresh_customers']
    assert {graph.nodes[e.target].name for e in graph.out_edges(refresh.id, 'runs')} == {'customers'}

    # Sensors order work: the asset sensor runs the archive after customer_ltv, the success
    # sensor runs everything after the archive's last ops, the failure sensor adds nothing.
    after_ltv = {e.target for e in graph.out_edges(ltv.id, 'precedes') if e.attrs.get('sensor')}
    assert after_ltv == {
        'task.archive_orders.archive.copy_orders',
        'task.archive_orders.archive_again.copy_orders',
    }
    everything = graph.nodes['dag.everything']
    assert everything.attrs['sensors'] == [
        'after_archive on job archive_orders (success)',
        'archive_failed on job archive_orders (failure)',
    ]
    ordered = {
        (e.source, e.target)
        for e in graph.edges
        if e.kind == 'precedes' and e.attrs.get('sensor') == 'after_archive'
    }
    assert {s for s, _ in ordered} == {
        'task.archive_orders.notify',
        'task.archive_orders.notify_finance',
        'task.archive_orders.refresh_customers',
    }
    assert not any(e.attrs.get('sensor') == 'archive_failed' for e in graph.edges)
    assert project.describe('archive_orders').node.kind == 'dag'


@pytest.mark.dagster_parity
def test_static_parse_matches_dagster(built_jaffle: Path, tmp_path: Path) -> None:
    """Load the fixture with Dagster and compare asset keys, dependencies and job selections."""
    try:
        import importlib.util

        import dagster  # noqa: F401
    except ImportError:
        pytest.skip('Dagster is not installed')
    root = tmp_path / 'shop'
    shutil.copytree(built_jaffle, root)
    shutil.copy(FIXTURE, root / 'definitions.py')
    from .conftest import run_dbt

    run_dbt(root, 'parse')
    spec = importlib.util.spec_from_file_location('konstruct_fixture_definitions', root / 'definitions.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    definitions = module.defs
    asset_graph = definitions.resolve_asset_graph()
    truth = {
        key.to_user_string(): sorted(p.to_user_string() for p in asset_graph.get(key).parent_keys)
        for key in asset_graph.get_all_asset_keys()
    }
    parsed = parse_file(root / 'definitions.py')
    python_assets = {a.key: a.deps for a in parsed.assets if a.dbt_select is None}
    for key, deps in python_assets.items():
        assert sorted(deps) == truth[key], key

    project = Project(root)
    project.build(run_dbt=False)
    dbt_models = {
        project.graph.nodes[e.target].name
        for e in project.graph.out_edges('task.dagster.jaffle_shop_dbt', 'runs')
    }
    dagster_dbt = {k for k in truth if k not in python_assets and truth[k] and not k.startswith('raw_')}
    assert dbt_models == dagster_dbt

    jobs = {job.name: job for job in definitions.resolve_all_job_defs()}
    ours_ops = {c.handle: c.upstream for j in parsed.jobs if j.name == 'archive_orders' for c in j.ops}
    assert ours_ops == _op_dependencies(jobs['archive_orders'].graph)
    for sensor in parsed.sensors:
        truth_sensor = definitions.resolve_sensor_def(sensor.name)
        assert sensor.jobs == [truth_sensor.job_name], sensor.name
    assert next(j for j in parsed.jobs if j.name == 'archive_orders').schedule == (
        definitions.resolve_schedule_def('nightly_archive').cron_schedule
    )
    reporting = {k.to_user_string() for k in jobs['reporting_job'].asset_layer.selected_asset_keys}
    ours = {
        graph_node.name
        for graph_node in (
            project.graph.nodes[e.target] for e in project.graph.out_edges('dag.reporting_job', 'runs')
        )
    }
    assert ours == reporting


def _op_dependencies(graph: Any) -> dict[str, list[str]]:
    """Dagster's own answer: each op handle in a job, nested graphs flattened, and the op
    handles its inputs come from."""
    from dagster._core.definitions.graph_definition import GraphDefinition

    def output(owner: Any, node_name: str, output_name: str, prefix: str) -> list[str]:
        node = owner.node_named(node_name)
        if isinstance(node.definition, GraphDefinition):
            mapping = node.definition.get_output_mapping(output_name)
            return output(
                node.definition,
                mapping.maps_from.node_name,
                mapping.maps_from.output_name,
                f'{prefix}{node_name}.',
            )
        return [f'{prefix}{node_name}']

    found: dict[str, list[str]] = {}

    def walk(owner: Any, prefix: str, outer: dict[str, list[str]]) -> None:
        structure = owner.dependency_structure
        for node in owner.nodes:
            inputs: dict[str, list[str]] = {}
            upstream = structure.input_to_upstream_outputs_for_node(node.name)
            for definition in node.definition.input_defs:
                handles = []
                for source in upstream.get(node.get_input(definition.name), []):
                    handles += output(owner, source.node_name, source.output_def.name, prefix)
                for mapping in owner.input_mappings:
                    if (
                        mapping.maps_to.node_name == node.name
                        and mapping.maps_to.input_name == definition.name
                    ):
                        handles += outer.get(mapping.graph_input_name, [])
                inputs[definition.name] = handles
            if isinstance(node.definition, GraphDefinition):
                walk(node.definition, f'{prefix}{node.name}.', inputs)
            else:
                found[f'{prefix}{node.name}'] = sorted({h for hs in inputs.values() for h in hs})

    walk(graph, '', {})
    return found


def test_multi_asset_prefixes_selections_and_schedules(tmp_path: Path) -> None:
    source = textwrap.dedent(
        """
        from dagster import (AssetDep, AssetKey, AssetOut, AssetSelection, asset, build_schedule_from_partitioned_job,
                             define_asset_job, multi_asset)

        @multi_asset(key_prefix=["mart"], group_name="marts", outs={"a": AssetOut(), "b": AssetOut(key=AssetKey(["x", "b"]))},
                     deps=[AssetDep("raw/events")])
        def both():
            ...

        @asset(key_prefix="mart", group_name="marts")
        def c(a):
            ...

        marts = define_asset_job("marts", selection=AssetSelection.groups("marts") | AssetSelection.key_prefixes(["x"]))
        downstream = define_asset_job(name="down", selection=AssetSelection.keys("mart/a").downstream())
        partitioned = build_schedule_from_partitioned_job(marts)
        """
    )
    parsed = parse_source(tmp_path / 'd.py', source)
    assets = {a.key: a for a in parsed.assets}
    assert set(assets) == {'mart/a', 'x/b', 'mart/c'}
    assert assets['mart/a'].deps == ['raw/events']
    assert assets['mart/a'].group == 'marts'
    assert assets['mart/c'].deps == ['mart/a']
    jobs = {j.name: j for j in parsed.jobs}
    assert jobs['marts'].selection == ['group:marts', 'prefix:x']
    assert jobs['marts'].schedule == 'partitioned'
    assert jobs['down'].selection == ['mart/a']


def test_dagster_in_the_build(tmp_path: Path) -> None:
    import duckdb

    con = duckdb.connect(str(tmp_path / 'w.duckdb'))
    con.execute('create schema lake')
    con.execute('create table lake.events (id int, kind varchar)')
    con.close()
    pipelines = tmp_path / 'pipelines'
    pipelines.mkdir()
    (pipelines / 'assets.py').write_text(
        textwrap.dedent(
            """
            from dagster import AssetSelection, asset, define_asset_job

            @asset(key_prefix="lake", group_name="lake")
            def daily(events):
                conn.execute("create table lake.daily as select kind, count(*) as n from lake.events group by kind")

            @asset(key_prefix="lake", group_name="lake")
            def report(daily):
                ...

            job = define_asset_job("lake_job", selection=AssetSelection.groups("lake"))
            """
        )
    )
    (pipelines / 'broken.py').write_text('from dagster import asset\n@asset\ndef x(:\n')
    (tmp_path / 'konstruct.toml').write_text('[dagster]\npaths = ["pipelines"]\n')
    project = Project(tmp_path)
    result = project.build()
    graph = result.graph
    assert result.inputs['dagster_assets'] == 2
    assert any('dagster: pipelines/broken.py: syntax error' in w for w in graph.warnings)
    daily_table = graph.node_for_relation('w.lake.daily')
    assert daily_table is not None
    assert [e.target for e in graph.out_edges(f'{daily_table.id}#kind', 'derived_from')] == [
        'table.w.lake.events#kind'
    ]
    events = graph.node_for_relation('w.lake.events')
    assert events is not None
    assert 'task.dagster.lake/daily' in {e.target for e in graph.out_edges(events.id, 'feeds')}
    report = graph.nodes['task.dagster.lake/report']
    assert {graph.nodes[e.source].name for e in graph.in_edges(report.id, 'feeds')} == {'daily'}
    assert {graph.nodes[e.target].name for e in graph.out_edges('dag.lake_job', 'runs')} == {
        'lake/daily',
        'lake/report',
    }
    assert project.describe('lake/daily').node.kind == 'task'


def test_folding_values_that_double_is_capped(tmp_path: Path) -> None:
    source = tmp_path / 'doubling.py'
    source.write_text(
        'from dagster import asset\ns = "x"\n'
        + 's = s + s\n' * 40
        + 'l = ["a"]\n'
        + 'l = l + l\n' * 40
        + 'n = ["a"]\n'
        + 'n = [n, n]\n' * 40
        + '@asset(description=s, kinds=l, deps=n)\ndef a(): pass\n@asset(description="ok")\ndef b(): pass\n'
    )
    code = (
        'import sys; from konstruct.inputs.dagster import parse_file\n'
        'print([(a.key, a.description) for a in parse_file(sys.argv[1]).assets])'
    )
    started = time.perf_counter()
    done = subprocess.run([sys.executable, '-c', code, str(source)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-300:]
    assert done.stdout.strip() == "[('a', None), ('b', 'ok')]"
    assert time.perf_counter() - started < 5


def test_records_copy_and_pickle_like_dataclasses() -> None:
    parsed = parse_file(FIXTURE)
    records = [parsed, parsed.assets[0], parsed.jobs[0], parsed.ops[0], parsed.sensors[0]]
    records.append(next(j for j in parsed.jobs if j.ops).ops[0])
    for record in records:
        assert copy.copy(record) == record
        assert copy.deepcopy(record) == record
        assert pickle.loads(pickle.dumps(record)) == record
    assert copy.deepcopy(parsed).assets[0] is not parsed.assets[0]


def test_the_tables_the_reader_is_built_from_are_still_there() -> None:
    from konstruct.inputs import dagster

    assert (
        frozenset({'asset', 'multi_asset', 'graph_asset', 'observable_source_asset'})
        == dagster.ASSET_DECORATORS
    )
    assert frozenset({'execute', 'sql', 'query', 'run_query', 'executescript'}) == dagster.SQL_METHODS
    assert (
        frozenset({'sensor', 'asset_sensor', 'multi_asset_sensor', 'run_status_sensor', 'run_failure_sensor'})
        == dagster.SENSOR_DECORATORS
    )
    assert frozenset({'alias', 'tag', 'with_hooks', 'with_retry_policy', 'configured'}) == dagster.OP_WRAPPERS


def test_long_lists_of_ternaries_and_lambdas_are_read(tmp_path: Path) -> None:
    ternaries = ''.join(f"    {{'id': {i}, 'v': 1 if a else 2}},\n" for i in range(6000))
    lambdas = ''.join(f'    lambda: {i},\n' for i in range(6000))
    parsed = parse_source(
        tmp_path / 'x.py',
        'from dagster import asset\nCONFIG = [\n'
        + ternaries
        + ']\nHOOKS = [\n'
        + lambdas
        + ']\n@asset\ndef a(): pass\n',
    )
    assert [a.key for a in parsed.assets] == ['a']


def test_indentation_errors_are_indentation_errors(tmp_path: Path) -> None:
    with pytest.raises(IndentationError):
        parse_source(tmp_path / 'x.py', 'from dagster import asset\n@asset\ndef a():\nreturn 1\n')
