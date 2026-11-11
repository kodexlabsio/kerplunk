from __future__ import annotations

import copy
import json
import pickle
import subprocess
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from konstruct.inputs.airflow import (
    DagInfo,
    DbtInvocation,
    looks_like_dag_file,
    parse_dbt_commands,
    parse_file,
)

EXAMPLE_DAG = (
    Path(__file__).resolve().parents[1] / 'examples' / 'jaffle_shop' / 'dags' / 'jaffle_shop_daily.py'
)


def parse(tmp_path: Path, source: str, name: str = 'dag.py') -> list[DagInfo]:
    path = tmp_path / name
    path.write_text(textwrap.dedent(source))
    return parse_file(path)


def test_example_dag() -> None:
    [dag] = parse_file(EXAMPLE_DAG)
    assert dag.dag_id == 'jaffle_shop_daily'
    assert dag.schedule == '@daily'
    assert dag.tags == ['jaffle_shop']
    assert dag.description and dag.description.startswith('Daily jaffle_shop run')
    assert list(dag.tasks) == [
        'dbt_seed',
        'dbt_run_staging',
        'dbt_run_marts',
        'dbt_test',
        'build_customer_ltv',
        'notify_crm',
    ]
    assert dag.dependencies == [
        ('dbt_seed', 'dbt_run_staging'),
        ('dbt_run_staging', 'dbt_run_marts'),
        ('dbt_run_marts', 'dbt_test'),
        ('dbt_test', 'build_customer_ltv'),
        ('build_customer_ltv', 'notify_crm'),
    ]
    staging = dag.tasks['dbt_run_staging']
    assert staging.bash_command == 'cd /opt/airflow/jaffle_shop && dbt run --select staging'
    assert staging.dbt == [DbtInvocation(command='run', selectors=['staging'])]
    assert dag.tasks['dbt_run_marts'].dbt[0].selectors == ['customers', 'orders']
    sql_task = dag.tasks['build_customer_ltv']
    assert sql_task.operator == 'SQLExecuteQueryOperator'
    assert sql_task.sql_path and sql_task.sql_path.endswith('dags/sql/customer_ltv.sql')
    assert sql_task.sql and 'reporting.customer_ltv' in sql_task.sql
    notify = dag.tasks['notify_crm']
    assert notify.operator == '@task'
    assert notify.outlets == ['duckdb://jaffle_shop/reporting/customer_ltv']
    assert notify.doc == 'Tell the CRM that lifetime values have been refreshed.'
    assert dag.warnings == []


def test_round_trip_to_dict() -> None:
    [dag] = parse_file(EXAMPLE_DAG)
    assert DagInfo.from_dict(dag.to_dict()) == dag


def test_assigned_dag_with_dag_kwarg_and_airflow2_imports(tmp_path: Path) -> None:
    dags = parse(
        tmp_path,
        """
        from airflow import DAG
        from airflow.operators.bash import BashOperator
        from airflow.providers.postgres.operators.postgres import PostgresOperator

        SCHEMA = "analytics"
        first = DAG("first", schedule_interval="0 3 * * *")
        second = DAG(dag_id="second")

        load = PostgresOperator(task_id="load", sql=f"insert into {SCHEMA}.events select * from raw.events", dag=first)
        other = BashOperator(task_id="other", bash_command="echo hi", dag=second)
        report = BashOperator(task_id="report", bash_command="dbt build -s +events --exclude tag:slow", dag=first)
        load.set_downstream(report)
        """,
    )
    first, second = dags
    assert first.dag_id == 'first' and first.schedule == '0 3 * * *'
    assert list(first.tasks) == ['load', 'report']
    assert list(second.tasks) == ['other']
    assert first.tasks['load'].sql == 'insert into analytics.events select * from raw.events'
    assert first.dependencies == [('load', 'report')]
    assert first.tasks['report'].dbt == [
        DbtInvocation(command='build', selectors=['+events'], excludes=['tag:slow'])
    ]


def test_dag_decorator_taskflow_expand_and_override(tmp_path: Path) -> None:
    [dag] = parse(
        tmp_path,
        """
        from airflow.sdk import dag, task

        @dag(schedule=None, tags=["etl"])
        def pipeline():
            @task
            def extract():
                return [1, 2]

            @task(task_id="transform_rows")
            def transform(x):
                return x

            @task.bash
            def run_dbt():
                return "dbt run --select marts"

            rows = extract()
            done = transform.expand(x=rows)
            again = transform.override(task_id="transform_again")(rows)
            rows >> [done, again] >> run_dbt()

        pipeline()
        """,
    )
    assert dag.dag_id == 'pipeline'
    assert dag.tags == ['etl']
    assert set(dag.tasks) == {'extract', 'transform_rows', 'transform_again', 'run_dbt'}
    assert dag.tasks['run_dbt'].operator == '@task.bash'
    assert dag.tasks['run_dbt'].dbt == [DbtInvocation(command='run', selectors=['marts'])]
    assert set(dag.dependencies) == {
        ('extract', 'transform_rows'),
        ('extract', 'transform_again'),
        ('transform_rows', 'run_dbt'),
        ('transform_again', 'run_dbt'),
    }


def test_task_groups_prefix_ids_and_expand_in_dependencies(tmp_path: Path) -> None:
    [dag] = parse(
        tmp_path,
        """
        from airflow.sdk import DAG, TaskGroup
        from airflow.providers.standard.operators.empty import EmptyOperator

        with DAG("grouped") as dag:
            start = EmptyOperator(task_id="start")
            with TaskGroup("load") as load:
                a = EmptyOperator(task_id="a")
                with TaskGroup("inner"):
                    b = EmptyOperator(task_id="b")
            end = EmptyOperator(task_id="end")
            start >> load >> end
        """,
    )
    assert list(dag.tasks) == ['start', 'load.a', 'load.inner.b', 'end']
    assert set(dag.dependencies) == {
        ('start', 'load.a'),
        ('start', 'load.inner.b'),
        ('load.a', 'end'),
        ('load.inner.b', 'end'),
    }


def test_task_group_dependencies_use_its_first_and_last_tasks(tmp_path: Path) -> None:
    [dag] = parse_file(Path(__file__).parent / 'fixtures' / 'airflow' / 'groups.py')
    assert set(dag.dependencies) == {
        ('start', 'load.a'),
        ('load.a', 'load.inner.b'),
        ('load.inner.b', 'end'),
    }


def test_task_group_functions(tmp_path: Path) -> None:
    [dag] = parse(
        tmp_path,
        """
        from airflow.sdk import DAG, task, task_group

        @task_group
        def stage():
            @task
            def one(): ...
            @task
            def two(): ...
            one() >> two()

        @task
        def first(): ...
        @task
        def last(): ...

        with DAG("tg"):
            first() >> stage() >> last()
        """,
    )
    assert set(dag.tasks) == {'first', 'stage.one', 'stage.two', 'last'}
    assert set(dag.dependencies) == {
        ('first', 'stage.one'),
        ('stage.one', 'stage.two'),
        ('stage.two', 'last'),
    }


def test_chain_cross_downstream_and_left_shift(tmp_path: Path) -> None:
    [dag] = parse(
        tmp_path,
        """
        from airflow.sdk import DAG, chain, cross_downstream
        from airflow.providers.standard.operators.empty import EmptyOperator as E

        with DAG("deps"):
            a, b = E(task_id="a"), E(task_id="b")
            c = E(task_id="c")
            d = E(task_id="d")
            x = E(task_id="x")
            y = E(task_id="y")
            chain(a, [b, c], d)
            cross_downstream([a, b], [x, y])
            d << y
            c.set_upstream(x)
        """,
    )
    assert set(dag.dependencies) == {
        ('a', 'b'),
        ('a', 'c'),
        ('b', 'd'),
        ('c', 'd'),
        ('a', 'x'),
        ('a', 'y'),
        ('b', 'x'),
        ('b', 'y'),
        ('y', 'd'),
        ('x', 'c'),
    }


def test_cosmos_and_dbt_operators(tmp_path: Path) -> None:
    [dag] = parse(
        tmp_path,
        """
        from airflow.sdk import DAG
        from cosmos import DbtTaskGroup, RenderConfig
        from cosmos.operators import DbtRunLocalOperator

        with DAG("cosmos"):
            group = DbtTaskGroup(group_id="transform", render_config=RenderConfig(select=["path:models/marts"], exclude=["tag:wip"]))
            single = DbtRunLocalOperator(task_id="one", select="orders", project_dir="/dbt")
            group >> single
        """,
    )
    assert dag.tasks['transform'].dbt == [
        DbtInvocation(command='build', selectors=['path:models/marts'], excludes=['tag:wip'])
    ]
    assert dag.tasks['one'].dbt == [DbtInvocation(command='run', selectors=['orders'], project_dir='/dbt')]
    assert dag.dependencies == [('transform', 'one')]


def test_sql_files_from_template_searchpath_and_bigquery_jobs(tmp_path: Path) -> None:
    (tmp_path / 'queries').mkdir()
    (tmp_path / 'queries' / 'daily.sql').write_text('insert into mart.daily select * from staging.events')
    [dag] = parse(
        tmp_path,
        """
        from airflow.sdk import DAG
        from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator
        from airflow.providers.google.cloud.operators.bigquery import BigQueryInsertJobOperator
        from airflow.providers.common.sql.operators.sql import SQLTableCheckOperator

        with DAG("sqlish", template_searchpath=["queries"]):
            daily = SQLExecuteQueryOperator(task_id="daily", sql="daily.sql")
            many = SQLExecuteQueryOperator(task_id="many", sql=["delete from t1", "insert into t2 select 1"])
            missing = SQLExecuteQueryOperator(task_id="missing", sql="nope.sql")
            bq = BigQueryInsertJobOperator(
                task_id="bq",
                configuration={"query": {"query": "select a from ds.src", "destinationTable": {"projectId": "p", "datasetId": "ds", "tableId": "dst"}}},
            )
            check = SQLTableCheckOperator(task_id="check", table="mart.daily", checks={})
        """,
    )
    assert dag.tasks['daily'].sql == 'insert into mart.daily select * from staging.events'
    assert dag.tasks['many'].sql == 'delete from t1;\ninsert into t2 select 1'
    assert dag.tasks['missing'].sql is None
    assert dag.tasks['bq'].sql == 'select a from ds.src'
    assert dag.tasks['bq'].writes == ['p.ds.dst']
    assert dag.tasks['check'].reads == ['mart.daily']
    assert any('nope.sql not found' in w for w in dag.warnings)


def test_inlets_and_outlets(tmp_path: Path) -> None:
    [dag] = parse(
        tmp_path,
        """
        from airflow.sdk import DAG, Asset
        from airflow.datasets import Dataset
        from airflow.providers.standard.operators.python import PythonOperator

        RAW = Asset("postgres://db:5432/shop/public/orders")
        with DAG("assets"):
            t = PythonOperator(task_id="t", python_callable=print, inlets=[RAW], outlets=[Dataset("s3://bucket/key"), Asset(name="named")])
        """,
    )
    task = dag.tasks['t']
    assert task.inlets == ['postgres://db:5432/shop/public/orders']
    assert task.outlets == ['s3://bucket/key', 'named']


def test_unresolvable_things_become_warnings(tmp_path: Path) -> None:
    dags = parse(
        tmp_path,
        """
        from airflow.sdk import DAG
        from airflow.providers.standard.operators.bash import BashOperator

        for name in ["a", "b"]:
            with DAG(f"dynamic_{name}") as dag:
                BashOperator(task_id=f"run_{name}", bash_command="echo")
        """,
    )
    assert len(dags) == 1
    assert dags[0].dag_id.startswith('dag_line_')
    assert any('task_id' in w for w in dags[0].warnings)


def test_syntax_errors_raise(tmp_path: Path) -> None:
    with pytest.raises(SyntaxError):
        parse(tmp_path, "from airflow import DAG\nwith DAG('x') as dag:\n  BashOperator(\n")


def test_looks_like_dag_file() -> None:
    assert looks_like_dag_file('from airflow import DAG')
    assert looks_like_dag_file('from airflow.sdk import dag\n@dag()')
    assert not looks_like_dag_file('import pandas')
    assert not looks_like_dag_file('import airflow')
    assert not looks_like_dag_file('DAG')


SYNTAX = {
    'match': 'match x:\n    case [1, *rest] if rest:\n        pass\n    case {"a": 1, **kw} | None:\n        pass\n',
    'type parameters': 'type Alias[T = int] = list[T]\nclass C[T: int, *Ts, **P]:\n    def m[U](self, x: U) -> U: ...\n',
    'nested f-string quotes': 'x = f"{d["k"]} {f"{y!r:>{w}}"} {"\\n".join(z)}"\n',
    'template string': 'x = t"hello {name} {value=}"\n',
    'except without parentheses': 'try:\n    pass\nexcept ValueError, TypeError:\n    pass\n',
    'except star': 'try:\n    pass\nexcept* ValueError:\n    pass\n',
    'lazy import': 'lazy import json\nlazy from os import path\n',
    'parenthesised with': 'with (open("a") as a, open("b") as b):\n    pass\n',
    'star in subscript': 'x = a[*b]\ny = a[1:2, ::3]\n',
    'async': 'async def f():\n    async with a as b:\n        return [i async for i in z]\n',
    'positional only': 'def f(a, /, b, *, c): ...\nif (n := 10) > 5: pass\n',
    'unicode names': 'ünï = 1\n变量 = 2\n',
}


@pytest.mark.parametrize('prelude', SYNTAX.values(), ids=SYNTAX.keys())
def test_the_syntax_of_every_python_version_is_read(tmp_path: Path, prelude: str) -> None:
    [dag] = parse(
        tmp_path,
        'from airflow import DAG\n'
        + prelude
        + 'with DAG("d") as dag:\n    BashOperator(task_id="a", bash_command="dbt run")\n',
    )
    assert list(dag.tasks) == ['a']


def test_a_syntax_error_names_its_line_and_file(tmp_path: Path) -> None:
    path = tmp_path / 'broken.py'
    path.write_text('from airflow import DAG\nx = 1\nwith DAG("x") as dag:\n    oops(\n')
    with pytest.raises(SyntaxError) as raised:
        parse_file(path)
    assert raised.value.filename == str(path)
    assert raised.value.lineno in (4, 5)
    assert raised.value.msg


def test_a_file_that_cannot_be_read_raises_as_reading_it_does(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        parse_file(tmp_path / 'missing.py')
    (tmp_path / 'latin.py').write_bytes(b'x = "caf\xe9"\n')
    with pytest.raises(UnicodeDecodeError):
        parse_file(tmp_path / 'latin.py')


def test_windows_line_ends_read_as_python_reads_them(tmp_path: Path) -> None:
    path = tmp_path / 'dos.py'
    path.write_bytes(
        b'from airflow import DAG\r\nwith DAG("d") as dag:\r\n'
        b'    BashOperator(task_id="a", bash_command="""echo 1\r\ndbt run""")\r\n'
        b'    BashOperator(task_id="b")\r\n'
    )
    [dag] = parse_file(path)
    assert dag.tasks['a'].bash_command == 'echo 1\ndbt run'
    assert [dag.tasks['a'].line, dag.tasks['b'].line] == [3, 5]


def test_a_decorated_task_is_on_the_line_of_its_def(tmp_path: Path) -> None:
    [dag] = parse(
        tmp_path,
        """
        from airflow.sdk import dag, task

        @dag(schedule=None)
        def pipeline():
            @task(
                retries=1,
            )
            # between
            def extract():
                \"\"\"Pull it.\"\"\"

            extract()
        """,
    )
    assert dag.line == 5
    assert dag.tasks['extract'].line == 10
    assert dag.tasks['extract'].doc == 'Pull it.'


def test_a_long_chain_of_elifs_is_read_in_bounded_time(tmp_path: Path) -> None:
    branches = ''.join(f'elif x == {i}:\n    BashOperator(task_id=f"{{u}}{i}")\n' for i in range(60))
    started = time.perf_counter()
    [dag] = parse(
        tmp_path,
        'from airflow import DAG\nwith DAG("d") as dag:\n    if x == -1:\n        pass\n'
        + textwrap.indent(branches, '    '),
    )
    assert dag.tasks == {} and len(dag.warnings) == 60
    assert time.perf_counter() - started < 5


def test_each_else_and_finally_is_read_once(tmp_path: Path) -> None:
    """Nested try and finally took twice as long with each level, and warned once for each read."""

    def nested(depth: int, indent: str) -> str:
        if depth == 0:
            return f'{indent}BashOperator(task_id=f"{{unresolved}}")\n'
        return f'{indent}try:\n{indent}    pass\nfinally:\n'.replace('finally', f'{indent}finally') + nested(
            depth - 1, indent + '    '
        )

    started = time.perf_counter()
    [dag] = parse(tmp_path, 'from airflow import DAG\nwith DAG("d") as dag:\n' + nested(40, '    '))
    assert len(dag.warnings) == 1 and 'no constant task_id' in dag.warnings[0]
    assert time.perf_counter() - started < 5
    [dag] = parse(
        tmp_path,
        """
        from airflow import DAG
        with DAG("d") as dag:
            if a:
                pass
            else:
                BashOperator(task_id=f"{x}")
            for i in y:
                pass
            else:
                BashOperator(task_id=f"{x}")
        """,
    )
    assert len(dag.warnings) == 2


def test_thousands_of_generated_tasks_with_unpacked_keywords_are_read(tmp_path: Path) -> None:
    tasks = ''.join(
        f"    t{i} = BashOperator(task_id='t{i}', bash_command='echo {i}', **common)\n" for i in range(5100)
    )
    [dag] = parse(tmp_path, 'from airflow import DAG\ncommon = {}\nwith DAG("d") as dag:\n' + tasks)
    assert len(dag.tasks) == 5100


def test_folding_values_that_double_is_capped(tmp_path: Path) -> None:
    """A string defined from itself forty times would be 2**40 long: a value over a mebibyte is left
    unresolved, the name is unbound (not kept at its last smaller value), and the process does not run
    out of memory."""
    source = tmp_path / 'doubling.py'
    source.write_text(
        'from airflow import DAG\ns = "x"\n'
        + 's = s + s\n' * 40
        + 'f = "y"\n'
        + 'f = f"{f}{f}"\n' * 40
        + 'with DAG("d") as dag:\n    BashOperator(task_id=s, bash_command="no")\n'
        '    BashOperator(task_id="b", bash_command=f)\n    BashOperator(task_id="c", bash_command="ok")\n'
    )
    code = (
        'import sys; from konstruct.inputs.airflow import parse_file\n'
        '[dag] = parse_file(sys.argv[1])\n'
        'print({t.task_id: t.bash_command[:14] for t in dag.tasks.values()}, len(dag.warnings))'
    )
    done = subprocess.run([sys.executable, '-c', code, str(source)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-300:]
    assert done.stdout.strip() == "{'b': '{{ unresolved ', 'c': 'ok'} 1"


def _unreadable_or_read(tmp_path: Path, body: str) -> None:
    """Reads ``body`` in a process of its own, which must finish: with a result or a SyntaxError."""
    source = tmp_path / 'deep.py'
    source.write_text('from airflow import DAG\n' + body + '\n')
    code = (
        'import sys; from konstruct.inputs.airflow import parse_file\n'
        'try: parse_file(sys.argv[1])\nexcept SyntaxError: pass'
    )
    done = subprocess.run([sys.executable, '-c', code, str(source)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-300:]


def test_long_lists_of_ternaries_and_lambdas_are_read(tmp_path: Path) -> None:
    ternaries = ''.join(f"    {{'id': {i}, 'v': 1 if a else 2}},\n" for i in range(6000))
    lambdas = ''.join(f'    lambda: {i},\n' for i in range(6000))
    [dag] = parse(
        tmp_path,
        'from airflow import DAG\nCONFIG = [\n'
        + ternaries
        + ']\nHOOKS = [\n'
        + lambdas
        + ']\nwith DAG("d") as dag:\n    BashOperator(task_id="a")\n',
    )
    assert list(dag.tasks) == ['a']


def test_chains_of_ternaries_and_lambdas_100000_deep_finish(tmp_path: Path) -> None:
    _unreadable_or_read(tmp_path, 'x = ' + '1 if 2 else ' * 100_000 + '3')
    _unreadable_or_read(tmp_path, 'x = ' + 'lambda: ' * 100_000 + '3')


def test_indentation_errors_are_indentation_errors(tmp_path: Path) -> None:
    for text in ('x = 1\n  y = 2\n', 'if a:\npass\n', 'if a:\n        x\n    y\n'):
        path = tmp_path / 'indent.py'
        path.write_text(text)
        with pytest.raises(IndentationError):
            parse_file(path)
    path.write_text('x = (\n')
    with pytest.raises(SyntaxError) as raised:
        parse_file(path)
    assert not isinstance(raised.value, IndentationError)


def test_records_copy_and_pickle_like_dataclasses() -> None:
    [dag] = parse_file(EXAMPLE_DAG)
    task = dag.tasks['dbt_run_staging']
    for record in (dag, task, task.dbt[0]):
        assert copy.copy(record) == record
        assert copy.deepcopy(record) == record
        assert pickle.loads(pickle.dumps(record)) == record
    assert copy.copy(task).dbt is task.dbt
    assert copy.deepcopy(task).dbt is not task.dbt
    assert copy.deepcopy(dag).tasks['dbt_run_staging'] is not task


def test_the_tables_the_reader_is_built_from_are_still_there() -> None:
    from konstruct.inputs import airflow

    assert frozenset({'DAG'}) == airflow.DAG_CLASSES
    assert frozenset({'TaskGroup'}) == airflow.GROUP_CLASSES
    assert frozenset({'DbtTaskGroup', 'DbtDag'}) == airflow.COSMOS_CLASSES
    assert airflow.SQL_KWARGS == ('sql', 'query', 'sql_query')
    assert airflow.WRITE_KWARGS == ('target_table', 'destination_table', 'destination_project_dataset_table')
    assert airflow.READ_KWARGS == ('table', 'source_table', 'source_project_dataset_table')
    assert airflow.UNRESOLVED == '{{ unresolved }}'
    assert airflow.GROUP == '\x00group:'
    assert frozenset({'run', 'build', 'test', 'seed', 'snapshot', 'compile'}) == airflow.DBT_COMMANDS


def test_records_are_edited_in_place_like_dataclasses(tmp_path: Path) -> None:
    [dag] = parse(
        tmp_path,
        """
        from airflow import DAG
        with DAG("d") as dag:
            BashOperator(task_id="a", bash_command="dbt run -s x", sql="select 1")
        """,
    )
    task = dag.tasks['a']
    assert task is dag.tasks['a'] and task.dbt is task.dbt
    task.sql, task.sql_path = None, None
    task.dbt[0].selectors.append('y')
    dag.warnings.append('edited')
    assert dag.tasks['a'].sql is None
    assert dag.tasks['a'].dbt == [DbtInvocation(command='run', selectors=['x', 'y'])]
    assert dag.warnings == ['edited']
    assert dag != DagInfo(dag_id='d', path=dag.path, line=dag.line)
    assert 'DbtInvocation(command=' in repr(task.dbt[0])


def test_a_record_survives_json(tmp_path: Path) -> None:
    [dag] = parse_file(EXAMPLE_DAG)
    restored = DagInfo.from_dict(json.loads(json.dumps(dag.to_dict())))
    assert restored == dag
    assert restored.dependencies == dag.dependencies and isinstance(restored.dependencies[0], tuple)
    with pytest.raises(TypeError):
        DagInfo.from_dict({**dag.to_dict(), 'unknown': 1})


def test_files_are_read_from_many_threads_at_once(tmp_path: Path) -> None:
    path = tmp_path / 'dag.py'
    path.write_text(EXAMPLE_DAG.read_text())
    expected = [d.to_dict() for d in parse_file(path)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: [d.to_dict() for d in parse_file(path)], range(64)))
    assert all(result == expected for result in results)


@pytest.mark.parametrize(
    'body',
    [
        'x = ' + '-' * 200_000 + '1',
        'x = ' + ' + '.join(['1'] * 20_000),
        'x = ' + '[' * 3000 + ']' * 3000 + '\n' * 40_000,
    ],
    ids=['minus', 'sum', 'brackets'],
)
def test_nesting_too_deep_to_read_is_a_syntax_error(tmp_path: Path, body: str) -> None:
    with pytest.raises(SyntaxError):
        parse(tmp_path, 'from airflow import DAG\n' + body + '\n')


def test_scan_python_files_decides_from_the_bytes(tmp_path: Path) -> None:
    from konstruct._core import scan_python_files

    files = {
        'dag.py': b'from airflow import DAG\n',
        'taskflow.py': b'import airflow\n@dag\ndef f(): ...\n',
        'assets.py': b'from dagster import asset\n@asset\ndef a(): ...\n',
        'both.py': b'airflow DAG dagster sensor',
        'plain.py': b'import pandas\n',
        'airflow_only.py': b'# the airflow deployment\n',
        'binary.py': b'\xff\xfe airflow \xff@dag',
    }
    for name, content in files.items():
        (tmp_path / name).write_bytes(content)
    paths = [tmp_path / name for name in files] + [tmp_path / 'missing.py', tmp_path]
    found = dict(zip([*files, 'missing.py', 'directory'], scan_python_files(paths), strict=True))
    assert found == {
        'dag.py': (True, False),
        'taskflow.py': (True, False),
        'assets.py': (False, True),
        'both.py': (True, True),
        'plain.py': (False, False),
        'airflow_only.py': (False, False),
        'binary.py': (True, False),
        'missing.py': (False, False),
        'directory': (False, False),
    }


@pytest.mark.parametrize(
    ('command', 'expected'),
    [
        ('dbt run', [DbtInvocation('run')]),
        ('cd /x && dbt run -s a b --exclude c', [DbtInvocation('run', ['a', 'b'], ['c'])]),
        ('dbt build --select=tag:nightly', [DbtInvocation('build', ['tag:nightly'])]),
        ('dbt --no-write-json test -m a', [DbtInvocation('test', ['a'])]),
        ('dbt seed && dbt run --models x', [DbtInvocation('seed'), DbtInvocation('run', ['x'])]),
        ('dbt run --project-dir /p --target prod', [DbtInvocation('run', project_dir='/p')]),
        ('/venv/bin/dbt run --selector nightly', [DbtInvocation('run', unsupported=['--selector'])]),
        ('dbt run -s state:modified+', [DbtInvocation('run', unsupported=['state:modified+'])]),
        ('dbt deps', []),
        ('dbt docs generate', []),
        ('echo dbt', []),
        ("dbt run --select 'unterminated", [DbtInvocation('run', ["'unterminated"])]),
    ],
)
def test_parse_dbt_commands(command: str, expected: list[DbtInvocation]) -> None:
    assert parse_dbt_commands(command) == expected


def test_a_large_dict_literal_is_read_in_linear_time(tmp_path: Path) -> None:
    keys = ', '.join(f"'key{i}': [{i}, 'v{i}']" for i in range(40_000))
    started = time.perf_counter()
    [dag] = parse(
        tmp_path,
        f'from airflow import DAG\nCONFIG = {{{keys}}}\nwith DAG("d") as dag:\n    BashOperator(task_id="a")\n',
    )
    assert list(dag.tasks) == ['a']
    assert time.perf_counter() - started < 5
