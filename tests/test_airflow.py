from __future__ import annotations

import textwrap
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
