"""Perturbation-based verification: it must confirm true lineage and catch false lineage."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from konstruct import Project, warehouse
from konstruct.graph import Graph, column_id
from konstruct.query import QueryError
from konstruct.verify import (
    BigQuerySource,
    DbApiSource,
    SampledEngine,
    Verification,
    _sample_type,
    annotate,
    claims,
    verify,
)

REPORT_SQL = """
create table main.report as
with base as (
    select o.id, o.customer_id, o.amount, o.status, c.region, o.placed_at
    from raw.orders o join raw.customers c on c.id = o.customer_id
    where o.status <> 'void'
)
select
    id,
    amount * 2 as doubled,
    upper(region) as region,
    case when amount > 50 then 'big' else 'small' end as size,
    coalesce(amount, customer_id) as amount_or_customer,
    row_number() over (partition by customer_id order by placed_at) as order_number
from base
"""


def make_project(root: Path, sql: str = REPORT_SQL) -> Project:
    con = duckdb.connect(str(root / 'shop.duckdb'))
    con.execute('create schema raw')
    con.execute(
        "create table raw.customers as select i as id, ['north','south','east'][1 + i % 3] as region from range(1, 21) t(i)"
    )
    con.execute(
        'create table raw.orders as select i as id, 1 + i % 20 as customer_id, (i * 37) % 100 + 1 as amount, '
        "case when i % 9 = 0 then 'void' else 'ok' end as status, "
        "timestamp '2026-01-01' + to_hours(i * 5) as placed_at from range(1, 201) t(i)"
    )
    con.close()
    (root / 'report.sql').write_text(sql)
    project = Project(root)
    project.build()
    return project


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return make_project(tmp_path)


def run(project: Project, *, seed: int = 1729) -> Verification:
    return verify(project.graph, project.root, project.root / 'shop.duckdb', seed=seed)


def test_true_lineage_is_confirmed(project: Project) -> None:
    result = run(project)
    [model] = result.models
    assert model.skipped is None
    assert model.rows == 178
    totals = result.totals()
    assert totals['unexplained'] == 0
    confirmed = set(model.confirmed)
    assert ('doubled', 'shop.raw.orders.amount') in confirmed
    assert ('region', 'shop.raw.customers.region') in confirmed
    assert ('order_number', 'shop.raw.orders.placed_at') in confirmed
    assert ('order_number', 'shop.raw.orders.customer_id') in confirmed
    references = {s for _, s in model.through_references}
    assert {'shop.raw.orders.status', 'shop.raw.orders.customer_id', 'shop.raw.customers.id'} <= references


def test_masked_dependencies_are_not_exercised(project: Project) -> None:
    [model] = run(project).models
    # amount is never null, so customer_id never reaches amount_or_customer on this data.
    assert ('amount_or_customer', 'shop.raw.orders.customer_id') in model.not_exercised


def test_a_missing_edge_is_unexplained(project: Project) -> None:
    graph = project.graph
    node = graph.node_for_relation('shop.main.report')
    assert node is not None
    edge = next(
        e
        for e in graph.out_edges(column_id(node.id, 'doubled'), 'derived_from')
        if e.target.endswith('#amount')
    )
    graph.remove_edge(edge)
    [model] = verify(graph, project.root, project.root / 'shop.duckdb').models
    assert ('doubled', 'shop.raw.orders.amount') in model.unexplained


def test_a_false_edge_is_not_exercised(project: Project) -> None:
    graph = project.graph
    node = graph.node_for_relation('shop.main.report')
    orders = graph.node_for_relation('shop.raw.orders')
    assert node is not None and orders is not None
    graph.add_edge(column_id(node.id, 'region'), column_id(orders.id, 'amount'), 'derived_from')
    [model] = verify(graph, project.root, project.root / 'shop.duckdb').models
    assert ('region', 'shop.raw.orders.amount') in model.not_exercised


def test_a_missing_filter_reference_is_unexplained(project: Project) -> None:
    graph = project.graph
    node = graph.node_for_relation('shop.main.report')
    assert node is not None
    for edge in list(graph.in_edges(node.id, 'feeds')):
        if edge.attrs.get('role') == 'reference' and edge.source.endswith('#status'):
            graph.remove_edge(edge)
    [model] = verify(graph, project.root, project.root / 'shop.duckdb').models
    assert ('(row count)', 'shop.raw.orders.status') in model.unexplained


def test_runs_are_reproducible_and_seeded(project: Project) -> None:
    first = run(project, seed=7).to_dict()
    second = run(project, seed=7).to_dict()
    for data in (first, second):
        data.pop('seconds')
        for model in data['models']:
            model.pop('seconds')
    assert first == second
    assert first['seed'] == 7


def test_nondeterministic_and_failing_models_are_skipped(tmp_path: Path) -> None:
    project = make_project(tmp_path, 'create table main.noisy as select id, random() as r from raw.orders')
    [model] = run(project).models
    assert model.skipped and 'not deterministic' in model.skipped


def test_annotate_marks_edges(project: Project) -> None:
    result = run(project)
    graph = project.graph
    annotate(graph, result)
    states = {e.attrs.get('verified') for e in graph.edges if e.kind == 'derived_from'}
    assert {'confirmed', 'not exercised'} <= states
    assert graph.meta['verification']['unexplained'] == 0


def test_project_verify_writes_its_results_into_the_outputs(project: Project) -> None:
    project.verify(warehouse=project.root / 'shop.duckdb')
    out = project.out_dir
    assert (out / 'VERIFY.md').is_file() and (out / 'verify.json').is_file()
    assert '"verified":"confirmed"' in (out / 'graph.json').read_text().replace(' ', '')
    page = (out / 'graph.html').read_text()
    assert '"verified":"confirmed"' in page
    assert '"verification":' in page


def test_claims_ignore_relations_without_names(project: Project) -> None:
    node = project.graph.node_for_relation('shop.main.report')
    assert node is not None
    value, references = claims(project.graph, node)
    assert ('doubled', 'shop.raw.orders.amount') in value
    assert 'shop.raw.orders.status' in references


def test_text_and_markdown(project: Project) -> None:
    result = run(project)
    text = str(result)
    assert 'claimed column edges confirmed by perturbation' in text
    assert 'report' in text
    markdown = result.markdown()
    assert markdown.startswith('# Konstruct lineage verification')
    assert '| `report` | 178 |' in markdown


# Differential testing: generated queries run on generated data must agree with static lineage.

COLUMNS = ['a', 'b', 'c', 'd']


@st.composite
def queries(draw: st.DrawFn) -> str:
    expressions = []
    for index in range(draw(st.integers(1, 4))):
        kind = draw(st.sampled_from(['column', 'arith', 'case', 'window', 'concat', 'coalesce']))
        x, y = draw(st.sampled_from(COLUMNS)), draw(st.sampled_from(COLUMNS))
        expressions.append(
            {
                'column': f'{x}',
                'arith': f'{x} * 3 + {y}',
                'case': f'case when {x} > 5 then {y} else 0 end',
                'window': f'sum({x}) over (partition by {y} order by k)',
                'concat': f"cast({x} as varchar) || '-' || cast({y} as varchar)",
                'coalesce': f'coalesce(nullif({x}, {x}), {y})',
            }[kind]
            + f' as o{index}'
        )
    where = draw(st.sampled_from(['', 'where a > 2', 'where d <> 3']))
    layered = draw(st.booleans())
    inner = f'select k, {", ".join(COLUMNS)} from raw.t {where}'
    if layered:
        return f'create table main.out as with x as ({inner}) select {", ".join(expressions)} from x'
    return f'create table main.out as select {", ".join(expressions)} from raw.t {where}'


@settings(
    max_examples=int(os.environ.get('KONSTRUCT_DIFFERENTIAL_EXAMPLES', '25')),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(queries())
def test_static_lineage_agrees_with_execution(tmp_path_factory: pytest.TempPathFactory, sql: str) -> None:
    root = tmp_path_factory.mktemp('differential')
    con = duckdb.connect(str(root / 'w.duckdb'))
    con.execute('create schema raw')
    con.execute(
        'create table raw.t as select i as k, i % 7 as a, (i * 3) % 11 as b, i % 4 as c, (i * 5) % 9 as d '
        'from range(1, 61) t(i)'
    )
    con.close()
    (root / 'q.sql').write_text(sql)
    project = Project(root)
    project.build()
    graph: Graph = project.graph
    result = verify(graph, root, root / 'w.duckdb', seed=3)
    [model] = result.models
    assert model.skipped is None, model.skipped
    assert model.unexplained == [], sql


@pytest.mark.postgres
def test_verify_on_postgres_with_dbt(tmp_path: Path) -> None:
    """The jaffle_shop example built on Postgres with dbt-postgres, verified in a rolled-back
    transaction: every claimed edge confirmed, nothing unexplained, nothing left behind."""
    import shutil
    import uuid
    from urllib.parse import urlparse

    import psycopg

    from .conftest import EXAMPLE, run_dbt
    from .test_warehouse import require

    dsn = require('KONSTRUCT_TEST_PG_DSN')
    url = urlparse(dsn)
    schema = f'kp_{uuid.uuid4().hex[:8]}'
    root = tmp_path / 'shop'
    shutil.copytree(
        EXAMPLE, root, ignore=shutil.ignore_patterns('target', 'logs', 'konstruct-out', '*.duckdb', 'dags')
    )
    (root / 'profiles.yml').write_text(
        'jaffle_shop:\n  target: dev\n  outputs:\n    dev:\n      type: postgres\n'
        f'      host: {url.hostname}\n      port: {url.port or 5432}\n      user: {url.username}\n'
        f'      password: {url.password}\n      dbname: {url.path.lstrip("/")}\n      schema: {schema}\n      threads: 4\n'
    )
    try:
        run_dbt(root, 'build')
        run_dbt(root, 'docs', 'generate')
        project = Project(root)
        project.build(run_dbt=False)
        result = project.verify(warehouse=dsn)
        totals = result.totals()
        assert result.warehouse.startswith('postgres database')
        assert totals['models'] == 5 and totals['skipped'] == 0
        assert totals['confirmed'] == totals['claimed'] == 31
        assert totals['unexplained'] == 0
        with psycopg.connect(dsn) as con:
            leftovers = con.execute("select count(*) from pg_class where relname like 'kp_%'").fetchone()
        assert leftovers == (0,)
    finally:
        with psycopg.connect(dsn, autocommit=True) as con:
            con.execute(f'drop schema if exists {schema} cascade')


class _Driver:
    """A stand-in for a warehouse driver module: ``connect()`` opens the project's DuckDB file
    read only, so a sample comes from real tables through a DB-API connection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.queries: list[str] = []

    def connect(self, **options: object) -> object:
        driver = self

        class Cursor:
            def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
                self.con = con
                self.description: object = None
                self.rows: list[tuple[object, ...]] = []

            def execute(self, sql: str) -> None:
                driver.queries.append(sql)
                result = self.con.execute(sql.replace('`', '"'))
                self.description = result.description
                self.rows = result.fetchall()

            def fetchall(self) -> list[tuple[object, ...]]:
                return self.rows

            def close(self) -> None:
                pass

        class Connection:
            def __init__(self) -> None:
                self.con = duckdb.connect(str(driver.path), read_only=True)

            def cursor(self) -> Cursor:
                return Cursor(self.con)

            def close(self) -> None:
                self.con.close()

        return Connection()


def _sampled(project: Project, dialect: str = 'databricks') -> SampledEngine:
    """A sampled engine whose remote warehouse is the project's DuckDB file, read only."""
    source = DbApiSource(duckdb.connect(str(project.root / 'shop.duckdb'), read_only=True), dialect)
    return SampledEngine(source, f'{dialect} (sampled)', project.graph)


def test_a_sampled_remote_warehouse_confirms_the_same_lineage(project: Project) -> None:
    local = run(project).totals()
    result = verify(project.graph, project.root, _sampled(project))
    assert result.warehouse == 'databricks (sampled)'
    [model] = result.models
    assert model.skipped is None and model.rows == 178
    assert result.totals() == local
    # A sample is at most the row limit of each input.
    smaller = verify(project.graph, project.root, _sampled(project), max_rows=50)
    assert smaller.models[0].rows < 178


def test_each_input_is_read_once_per_run(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    (project.root / 'second.sql').write_text(
        'create table main.second as select id, amount + 1 as more from raw.orders'
    )
    project.build()
    driver = _Driver(project.root / 'shop.duckdb')
    monkeypatch.setattr(warehouse, '_driver', lambda module, extra: driver)
    (project.root / 'konstruct.toml').write_text(
        '[[warehouse]]\ntype = "databricks"\nserver_hostname = "x"\nhttp_path = "y"\n'
    )
    result = project.verify(warehouse='databricks', write=False)
    assert {m.label for m in result.models} == {'report', 'second'}
    assert all(m.skipped is None for m in result.models)
    reads = [q for q in driver.queries if 'orders' in q]
    assert len(reads) == 1 and reads[0].startswith('SELECT * FROM `shop`.`raw`.`orders` LIMIT ')


@pytest.mark.parametrize(
    ('remote', 'values', 'expected'),
    [
        ('NUMBER', [Decimal('1.37'), Decimal('2.5')], 'DECIMAL(38, 2)'),
        ('NUMBER(10,2)', [Decimal('1.37')], 'DECIMAL(10, 2)'),
        ('NUMERIC', [Decimal('123456789012345678.123456')], 'DECIMAL(38, 6)'),
        ('NUMBER', [2**40], 'BIGINT'),
        ('INTEGER', [2**40], 'BIGINT'),
        ('FLOAT', [0.1], 'DOUBLE'),
        ('TIMESTAMP', [datetime(2026, 1, 1, tzinfo=UTC)], 'TIMESTAMPTZ'),
        ('TIMESTAMP_NTZ', [datetime(2026, 1, 1)], 'TIMESTAMP'),
        ('RECORD', [{'k': 1}], 'JSON'),
        ('VARIANT', ['{"k": 1}'], 'JSON'),
        ('GEOGRAPHY', ['POINT(1 2)'], 'VARCHAR'),
        (None, [None, 2**70], 'HUGEINT'),
    ],
)
def test_sampled_types_keep_their_values(remote: str | None, values: list[object], expected: str) -> None:
    assert _sample_type(remote, 'bigquery', values) == expected


class _Rows:
    dialect = 'snowflake'

    def sample(
        self, parts: tuple[str, ...], limit: int
    ) -> tuple[list[str], list[str | None], list[tuple[object, ...]]]:
        rows = [(Decimal('1.37'), 2**40, {'k': [1, 2]}, datetime(2026, 1, 1, 9, tzinfo=UTC), 'POINT(1 2)')]
        names = ['amount', 'id', 'payload', 'at', 'place']
        return names, ['NUMBER', 'NUMBER', 'VARIANT', 'TIMESTAMP_TZ', 'GEOGRAPHY'], list(rows)

    def close(self) -> None:
        pass


def test_a_sampled_value_is_kept_exactly() -> None:
    engine = SampledEngine(_Rows(), 'snowflake (sampled)')
    engine.start()
    scratch, columns = engine.copy('db.raw.events', 0, 10)
    assert [t for _, t in columns] == [
        'DECIMAL(38,2)',
        'BIGINT',
        'JSON',
        'TIMESTAMP WITH TIME ZONE',
        'VARCHAR',
    ]
    [row] = engine.con.execute(f"SELECT amount, id, payload ->> '$.k[1]' FROM {scratch}").fetchall()
    assert row == (Decimal('1.37'), 2**40, '2')
    engine.close()


def test_sampled_columns_without_known_types_take_their_values_types(tmp_path: Path) -> None:
    make_project(tmp_path)
    project = Project(tmp_path)
    project.build(warehouses=False, dialect='duckdb')
    result = verify(project.graph, project.root, _sampled(project, 'bigquery'))
    [model] = result.models
    assert model.skipped is None
    assert ('doubled', 'raw.orders.amount') in set(model.confirmed)


def test_remote_names_are_quoted() -> None:
    assert DbApiSource(None, 'snowflake').table(('analytics', 'raw', 'order')) == '"ANALYTICS"."RAW"."ORDER"'
    assert DbApiSource(None, 'databricks').table(('raw', 'select')) == '`raw`.`select`'


def test_a_remote_warehouse_that_is_not_configured_or_has_no_driver(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(QueryError, match='no databricks warehouse is configured'):
        project.verify(warehouse='databricks')
    (project.root / 'konstruct.toml').write_text('[[warehouse]]\ntype = "snowflake"\naccount = "x"\n')

    def missing(module: str, extra: str) -> object:
        raise warehouse.MissingDriver(f"install the {extra} extra: uv tool install 'konstruct[{extra}]'")

    monkeypatch.setattr(warehouse, '_driver', missing)
    with pytest.raises(QueryError, match=r'cannot connect to snowflake: install the snowflake extra'):
        project.verify(warehouse='snowflake')


def test_bigquery_tables_are_listed_and_views_queried() -> None:
    class Field:
        def __init__(self, name: str, field_type: str, mode: str = 'NULLABLE') -> None:
            self.name, self.field_type, self.mode = name, field_type, mode

    class Row:
        def __init__(self, values: tuple[object, ...]) -> None:
            self._values = values

        def values(self) -> tuple[object, ...]:
            return self._values

    class Rows(list[Row]):
        schema = (Field('Id', 'INTEGER'), Field('Tags', 'STRING', 'REPEATED'))

    class Table:
        def __init__(self, table_type: str) -> None:
            self.table_type = table_type

    class Client:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_table(self, name: str) -> Table:
            return Table('VIEW' if name.endswith('_view') else 'TABLE')

        def list_rows(self, table: Table, max_results: int) -> Rows:
            self.calls.append(f'list {max_results}')
            return Rows([Row((1, ['a']))])

        def query(self, sql: str) -> object:
            self.calls.append(sql)
            return type('Job', (), {'result': lambda self: Rows([Row((2, ['b']))])})()

        def close(self) -> None:
            self.calls.append('closed')

    client = Client()
    source = BigQuerySource(client)
    assert source.sample(('my-proj', 'ds', 'orders'), 5) == (
        ['id', 'tags'],
        ['INTEGER', 'JSON'],
        [(1, ['a'])],
    )
    _, _, rows = source.sample(('my-proj', 'ds', 'orders_view'), 5)
    assert rows == [(2, ['b'])]
    source.close()
    assert client.calls == ['list 5', 'SELECT * FROM `my-proj.ds.orders_view` LIMIT 5', 'closed']


class _Late:
    """A source whose one column fits BIGINT until its last value; it counts its reads."""

    dialect = 'databricks'

    def __init__(self, last: object) -> None:
        self.reads = 0
        self.last = last

    def sample(
        self, parts: tuple[str, ...], limit: int
    ) -> tuple[list[str], list[str | None], list[tuple[object, ...]]]:
        self.reads += 1
        return ['n'], [None], [*[(i,) for i in range(1500)], (self.last,)]

    def close(self) -> None:
        pass


def test_a_value_that_only_fails_late_keeps_its_column_as_text() -> None:
    engine = SampledEngine(_Late(2**130), 'databricks (sampled)')
    engine.start()
    scratch, columns = engine.copy('db.raw.numbers', 0, 2000)
    assert columns == [('n', 'VARCHAR')]
    assert engine.con.execute(f'SELECT count(*) FROM {scratch}').fetchone() == (1501,)
    engine.close()


def test_an_input_that_cannot_be_read_is_read_once(project: Project) -> None:
    class Failing(_Late):
        def sample(
            self, parts: tuple[str, ...], limit: int
        ) -> tuple[list[str], list[str | None], list[tuple[object, ...]]]:
            self.reads += 1
            raise RuntimeError('access denied')

    source = Failing(0)
    engine = SampledEngine(source, 'databricks (sampled)', project.graph)
    for index in range(2):
        engine.start()
        with pytest.raises(RuntimeError, match='access denied'):
            engine.copy('shop.raw.orders', index, 100)
    assert source.reads == 1
    engine.close()


def test_arrays_from_a_driver_become_json() -> None:
    class Array:
        def __init__(self, *items: object) -> None:
            self.items = list(items)

        def tolist(self) -> list[object]:
            return self.items

    class Source(_Late):
        def sample(
            self, parts: tuple[str, ...], limit: int
        ) -> tuple[list[str], list[str | None], list[tuple[object, ...]]]:
            return ['tags', 'detail'], [None, 'STRUCT'], [(Array('x', 'y'), {'a': 1, 'b': Array(1, 2)})]

    engine = SampledEngine(Source(0), 'databricks (sampled)')
    engine.start()
    scratch, columns = engine.copy('db.raw.events', 0, 10)
    assert [t for _, t in columns] == ['JSON', 'JSON']
    assert engine.con.execute(f"SELECT tags ->> '$[1]', detail -> '$.b' FROM {scratch}").fetchone() == (
        'y',
        '[1,2]',
    )
    engine.close()


def test_db_api_precision_and_scale_are_kept() -> None:
    class Cursor:
        description = (('amount', 0, None, None, 10, 2, True), ('at', 8, None, None, 0, 9, True))

        def execute(self, sql: str) -> None:
            pass

        def fetchall(self) -> list[tuple[object, ...]]:
            return [(Decimal('1.37'), datetime(2026, 1, 1))]

        def close(self) -> None:
            pass

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

        def close(self) -> None:
            pass

    names, types, _ = DbApiSource(Connection(), 'snowflake').sample(('db', 'raw', 'orders'), 5)
    assert (names, types) == (['amount', 'at'], ['DECIMAL(10, 2)', None])


def test_each_remote_warehouse_type_makes_its_engine(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    from konstruct.verify import sampled_engine

    driver = _Driver(project.root / 'shop.duckdb')
    monkeypatch.setattr(warehouse, '_driver', lambda module, extra: driver)
    snowflake = sampled_engine(warehouse.SnowflakeConnector(options={'account': 'x'}), project.graph)
    assert (snowflake.label, snowflake.read_dialect) == ('snowflake (sampled)', 'snowflake')
    snowflake.close()

    class Client:
        def close(self) -> None:
            pass

    monkeypatch.setattr(warehouse.BigQueryConnector, 'client', lambda self: Client())
    bigquery = sampled_engine(warehouse.BigQueryConnector(project='p'), project.graph)
    assert isinstance(bigquery.source, BigQuerySource) and bigquery.label == 'bigquery (sampled)'
    bigquery.close()
    with pytest.raises(ValueError, match='is not a Snowflake, BigQuery or Databricks connector'):
        sampled_engine(object())


def test_the_warehouse_spelling_of_a_name_is_kept(tmp_path: Path) -> None:
    con = duckdb.connect(str(tmp_path / 'shop.duckdb'))
    con.execute('create schema "MyData"')
    con.execute('create table "MyData"."Orders" (id int)')
    con.execute('create table main.lower_case (id int)')
    con.close()
    graph = Project(tmp_path).build(run_dbt=False).graph
    orders = graph.node_for_relation('shop.mydata.orders')
    lower = graph.node_for_relation('shop.main.lower_case')
    assert orders is not None and orders.attrs['parts'] == 'shop.MyData.Orders'
    assert lower is not None and 'parts' not in lower.attrs

    asked: list[tuple[str, ...]] = []

    class Source(_Late):
        def sample(
            self, parts: tuple[str, ...], limit: int
        ) -> tuple[list[str], list[str | None], list[tuple[object, ...]]]:
            asked.append(parts)
            return ['id'], ['INTEGER'], [(1,)]

    engine = SampledEngine(Source(0), 'bigquery (sampled)', graph)
    engine.start()
    engine.copy('shop.mydata.orders', 0, 10)
    engine.copy('shop.main.lower_case', 1, 10)
    assert asked == [('shop', 'MyData', 'Orders'), ('shop', 'main', 'lower_case')]
    engine.close()
