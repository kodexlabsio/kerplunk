"""PySpark jobs: the SQL they pass to spark.sql, read without importing them."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from konstruct import Project, build
from konstruct.config import ConfigError, load_config
from konstruct.spark import looks_like_spark_file, parse, without_session_catalog
from konstruct.sql import analyse as real_analyse
from konstruct.status import inspect

JOB = textwrap.dedent(
    '''\
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    DAILY = """
    insert overwrite table mart.daily
    select order_date, sum(amount) as revenue from raw.orders group by order_date
    """
    TARGET = "mart.extra"


    def build(session):
        session.sql("create table if not exists " + "mart.customers as select id as customer_id, name from raw.people")
        spark.sql(DAILY)
        spark.sql(sqlQuery=f"insert into mart.flags select id, amount > 100 as big from raw.orders")
        spark.sql(f"select * from raw.orders where id = {1}")
        spark.sql("create table " + TARGET + " as select 1 as x")
        frame.sql("not spark")
    '''
)


def test_the_sql_a_job_runs() -> None:
    job = parse(JOB, 'jobs/daily.py')
    assert [(s.line, s.sql.split()[0]) for s in job.statements] == [
        (12, 'create'),
        (13, 'insert'),
        (14, 'insert'),
        (16, 'create'),
    ]
    assert job.problems == ['jobs/daily.py:15: spark.sql with SQL built at run time is skipped']
    assert parse('def broken(:\n', 'x.py').problems == ['x.py:1: not valid Python: invalid syntax']


def test_names_bound_more_than_once_are_not_constants() -> None:
    text = textwrap.dedent(
        """\
        Q = "insert into a.t select 1 as x"
        Q2 = "insert into a.u select 1 as x"
        Q2 = Q2.replace("u", "v")
        ONCE = "insert into a.w select 1 as x"
        CAUGHT = "insert into a.c select 1 as x"
        try:
            pass
        except Exception as CAUGHT:
            pass

        def f(x):
            Q = f"insert into a.t select {x} as x"
            spark.sql(Q)

        spark.sql(Q2)
        spark.sql(ONCE)
        spark.sql(CAUGHT)
        """
    )
    job = parse(text, 'j.py')
    assert [s.sql for s in job.statements] == ['insert into a.w select 1 as x']
    assert job.problems == [
        'j.py:13: spark.sql with SQL built at run time is skipped',
        'j.py:15: spark.sql with SQL built at run time is skipped',
        'j.py:17: spark.sql with SQL built at run time is skipped',
    ]


def test_sql_formatted_with_keyword_arguments_is_skipped() -> None:
    job = parse('spark.sql("insert into mart.k select id from {t}", t="raw.orders")\n', 'k.py')
    assert job.statements == []
    assert job.problems == ['k.py:1: spark.sql formats its SQL with keyword arguments (t), so it is skipped']
    assert len(parse('spark.sql("select :x", args={"x": 1})\n', 'a.py').statements) == 1


def test_which_files_are_jobs_and_which_objects_are_sessions() -> None:
    assert looks_like_spark_file(JOB)
    assert looks_like_spark_file('# Databricks notebook source\ndf = spark.sql("select 1")\n')
    assert looks_like_spark_file('def run(spark):\n    spark.sql("select 1")\n')
    assert not looks_like_spark_file('import pandas\nframe.sql("x")\n')
    snowpark = 'from snowflake.snowpark import Session\nsession.sql("insert into a.b select 1 as x")\n'
    assert parse(snowpark, 's.py').statements == []


def test_the_session_catalogue_is_left_out() -> None:
    assert without_session_catalog(
        'insert into spark_catalog.mart.t select x from spark_catalog.raw.s', 'spark'
    ) == ('INSERT INTO mart.t SELECT x FROM raw.s')
    assert without_session_catalog('select 1', 'spark') == 'select 1'


def test_a_job_gives_its_tables_column_lineage(tmp_path: Path) -> None:
    (tmp_path / 'jobs').mkdir()
    (tmp_path / 'jobs' / 'daily.py').write_text(JOB)
    project = Project(tmp_path)
    graph = project.build(warehouses=False, run_dbt=False).graph
    assert [c.label for c in project.trace('mart.daily.revenue').columns] == ['orders.amount']
    assert [c.label for c in project.trace('mart.customers.customer_id').columns] == ['people.id']
    assert [c.label for c in project.trace('mart.flags.big').columns] == ['orders.amount']
    daily = next(n for n in graph.relations() if n.relation == 'mart.daily')
    assert daily.path == 'jobs/daily.py' and 'spark' in daily.origins
    assert 'spark: jobs/daily.py:15: spark.sql with SQL built at run time is skipped' in graph.warnings
    source = next(s for s in graph.meta['sources'] if s['input'] == 'spark')
    assert source == {
        'input': 'spark',
        'path': 'jobs/daily.py',
        'detail': 'writes mart.customers, mart.daily, mart.extra, mart.flags',
    }
    assert graph.meta['inputs']['spark_statements'] == 4
    assert inspect(tmp_path).spark_files == ['jobs/daily.py']


def test_both_spellings_of_a_table_are_one_table(tmp_path: Path) -> None:
    (tmp_path / 'job.py').write_text(
        'spark.sql("create table mart.a as select id, amount from spark_catalog.raw.orders")\n'
        'spark.sql("create table mart.b as select id, amount * 2 as doubled from raw.orders")\n'
    )
    project = Project(tmp_path)
    graph = project.build(warehouses=False, run_dbt=False).graph
    assert sorted(n.relation for n in graph.relations() if n.relation and 'orders' in n.relation) == [
        'raw.orders'
    ]
    assert not [w for w in graph.warnings if 'lineage failed' in w]
    assert [c.label for c in project.trace('mart.b.doubled').columns] == ['orders.amount']


def test_spark_paths_and_dialect_in_konstruct_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / 'jobs').mkdir()
    (tmp_path / 'jobs' / 'a.py').write_text('spark.sql("create table t.a as select x from t.src")\n')
    (tmp_path / 'other.py').write_text(JOB)
    (tmp_path / 'konstruct.toml').write_text('[spark]\npaths = ["jobs"]\ndialect = "databricks"\n')
    assert load_config(tmp_path).spark_dialect == 'databricks'
    dialects: list[Any] = []
    real = real_analyse

    def analyse(sql: str, **kwargs: Any) -> Any:
        dialects.append(kwargs.get('dialect'))
        return real(sql, **kwargs)

    monkeypatch.setattr(build, 'analyse', analyse)
    graph = Project(tmp_path).build(warehouses=False, run_dbt=False).graph
    assert [s['path'] for s in graph.meta['sources'] if s['input'] == 'spark'] == ['jobs/a.py']
    assert dialects == ['databricks']
    (tmp_path / 'konstruct.toml').write_text('[spark]\ndialect = "nonsense"\n')
    with pytest.raises(ConfigError, match=r"\[spark\] dialect 'nonsense' is not a SQL dialect"):
        load_config(tmp_path)
