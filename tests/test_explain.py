"""konstruct explain: column formulas composed down to sources, and checked against the data."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import duckdb
import pytest

from konstruct import Project, QueryError
from konstruct.interfaces.cli import main


@pytest.fixture
def project(jaffle_built_graph: Path, tmp_path: Path) -> Project:
    copy = tmp_path / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph / 'konstruct-out', copy / 'konstruct-out')
    return Project(copy)


@pytest.mark.dbt
def test_a_dbt_column_composes_through_staging_to_the_seed(project: Project) -> None:
    result = project.explain('orders.credit_card_amount')
    assert result.expression == (
        "SUM(CASE WHEN stg_payments.payment_method = 'credit_card' THEN stg_payments.amount ELSE 0 END)"
    )
    assert result.composed == (
        "SUM(CASE WHEN raw_payments.payment_method = 'credit_card' THEN raw_payments.amount / 100 ELSE 0 END)"
    )
    assert result.sources == ['raw_payments.amount', 'raw_payments.payment_method']
    assert result.complete
    steps = {s.label: s for s in result.steps}
    assert steps['orders.credit_card_amount'].conditions == [
        'GROUP BY: stg_payments.order_id',
        'LEFT JOIN ON: stg_orders.order_id = stg_payments.order_id',
    ]
    assert steps['stg_payments.amount'].expression == 'raw_payments.amount / 100'


@pytest.mark.dbt
def test_composition_crosses_from_an_airflow_sql_file_into_dbt(project: Project) -> None:
    result = project.explain('customer_ltv.lifetime_value')
    assert result.expression == 'customers.customer_lifetime_value'
    assert result.composed == 'SUM(raw_payments.amount / 100)'
    assert result.steps[0].conditions == ['WHERE: customers.customer_lifetime_value > 0']
    text = str(result)
    assert 'In terms of sources\n  = SUM(raw_payments.amount / 100)' in text
    assert '\n      WHERE  customers.customer_lifetime_value > 0\n' in text
    data = result.to_dict()
    assert data['composed'] == 'SUM(raw_payments.amount / 100)'
    assert json.loads(json.dumps(data)) == data


@pytest.mark.dbt
def test_a_source_column_has_no_formula(project: Project) -> None:
    result = project.explain('raw_payments.amount')
    assert result.expression is None and result.composed is None
    assert 'No expression recorded' in str(result)


@pytest.mark.dbt
def test_explain_takes_a_column(project: Project) -> None:
    with pytest.raises(QueryError, match=r'explain takes a column such as orders\.<column>'):
        project.explain('orders')


@pytest.mark.dbt
def test_describe_shows_the_expression_and_the_conditions(project: Project) -> None:
    column = project.describe('orders.amount')
    assert column.expression == 'SUM(stg_payments.amount)'
    assert 'expression' not in column.attrs
    model = project.describe('orders')
    assert model.conditions == [
        'GROUP BY: stg_payments.order_id',
        'LEFT JOIN ON: stg_orders.order_id = stg_payments.order_id',
    ]
    assert 'Joins, filters and grouping (2)' in str(model)


@pytest.mark.dbt
def test_cli_explain(project: Project, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(['explain', 'orders.amount', '-C', str(project.path), '--json']) == 0
    assert json.loads(capsys.readouterr().out)['composed'] == 'SUM(raw_payments.amount / 100)'


# Composed formulas checked against the data

CHAIN = [
    'create table raw.events (id int, price_cents int, qty int, discount double, kind varchar)',
    'create view mart.priced as select id, price_cents / 100.0 as price, qty, coalesce(discount, 0) as discount,'
    ' kind from raw.events',
    'create view mart.lines as with base as (select id, price * qty as gross, discount, kind from mart.priced)'
    " select id, gross * (1 - discount) as net, case when kind = 'refund' then -1 else 1 end as sign from base",
    'create view mart.signed as select l.id, l.net * l.sign as amount from mart.lines as l',
]


def _chain_warehouse(root: Path) -> Path:
    path = root / 'w.duckdb'
    con = duckdb.connect(str(path))
    con.execute('create schema raw')
    con.execute('create schema mart')
    for statement in CHAIN:
        con.execute(statement)
    con.execute(
        'insert into raw.events select i, (i * 37) % 1000, i % 7, case when i % 5 = 0 then null'
        " else (i % 4) / 10.0 end, case when i % 3 = 0 then 'refund' else 'sale' end from range(200) t(i)"
    )
    con.close()
    return path


def test_the_composed_formula_gives_the_same_values_as_the_model(tmp_path: Path) -> None:
    """Row-level chains (no joins, filters or aggregation) must compose exactly: evaluating the
    composed formula over the source table gives the final view's values, row for row."""
    path = _chain_warehouse(tmp_path)
    project = Project(tmp_path)
    project.build()
    result = project.explain('signed.amount')
    assert result.complete
    assert result.sources == [
        'events.discount',
        'events.kind',
        'events.price_cents',
        'events.qty',
    ]
    assert result.composed is not None and 'mart.' not in result.composed
    con = duckdb.connect(str(path), read_only=True)
    formula = result.composed.replace('events.', 'e.')
    composed = con.execute(f'select e.id, {formula} from raw.events as e order by e.id').fetchall()
    actual = con.execute('select id, amount from mart.signed order by id').fetchall()
    con.close()
    assert len(actual) == 200
    assert [(i, round(v, 9)) for i, v in composed] == [(i, round(v, 9)) for i, v in actual]


def test_steps_list_each_column_on_the_way(tmp_path: Path) -> None:
    _chain_warehouse(tmp_path)
    project = Project(tmp_path)
    project.build()
    steps = {s.label: s.expression for s in project.explain('signed.amount').steps}
    assert steps['signed.amount'] == 'lines.net * lines.sign'
    assert steps['lines.net'] == '(priced.price * priced.qty) * (1 - priced.discount)'
    assert steps['priced.price'] == 'events.price_cents / 100.0'
    assert steps['events.qty'] is None


def _viewer_formulas(project: Project) -> dict[str, dict[str, object]]:
    import re

    page = (project.out_dir / 'graph.html').read_text()
    match = re.search(r'<script type="application/json" id="konstruct-explain">(.*?)</script>', page, re.S)
    assert match is not None
    return dict(json.loads(match.group(1))['columns'])


def test_viewer_formulas_are_cached_and_recomputed_downstream_of_a_change(tmp_path: Path) -> None:
    path = _chain_warehouse(tmp_path)
    project = Project(tmp_path)
    project.build()
    amount = 'table.w.mart.signed#amount'
    before = _viewer_formulas(project)[amount]
    assert 'events.price_cents / 100.0' in str(before['c'])
    again = project.build(update=True)
    assert again.cache_misses == 0
    assert _viewer_formulas(project)[amount] == before
    # Change a view two steps upstream: the formula of every column below it must follow.
    con = duckdb.connect(str(path))
    con.execute(CHAIN[1].replace('create view', 'create or replace view').replace('100.0', '1000.0'))
    con.close()
    project.build(update=True)
    after = _viewer_formulas(project)
    assert 'events.price_cents / 1000.0' in str(after[amount]['c'])
    assert after['table.w.mart.priced#qty'] == _viewer_formulas(project)['table.w.mart.priced#qty']


def test_a_composed_formula_does_not_depend_on_what_was_explained_first(tmp_path: Path) -> None:
    from konstruct.analysis.explain import Explainer

    con = duckdb.connect(str(tmp_path / 'w.duckdb'))
    for statement in (
        'create schema raw',
        'create schema mart',
        'create table raw.t (id int, a int)',
        'create view mart.v1_g as select id, coalesce((a / 100), 0) + 1 as g from raw.t',
        'create view mart.v2_c as select id, g as c from mart.v1_g',
    ):
        con.execute(statement)
    con.close()
    project = Project(tmp_path)
    project.build()
    graph = project.graph
    upstream, downstream = graph.nodes['table.w.mart.v1_g#g'], graph.nodes['table.w.mart.v2_c#c']
    alone = Explainer(graph).explain(downstream).composed
    explainer = Explainer(graph)
    explainer.summary(upstream)
    explainer.explain(upstream)
    assert explainer.summary(downstream) == Explainer(graph).summary(downstream)
    assert explainer.explain(downstream).composed == alone


def test_tidying_brackets_keeps_meaning_and_is_the_same_alone_or_inside() -> None:
    import sqlglot
    from sqlglot import exp

    from konstruct.analysis.explain import _unparen

    xor = exp.and_(exp.Paren(this=exp.Xor(this=exp.column('a'), expression=exp.column('b'))), exp.column('c'))
    assert _unparen(xor).sql('mysql') == '(a XOR b) AND c'
    assert _unparen(sqlglot.parse_one('(a || b) || c')).sql() == '(a || b) || c'
    assert _unparen(sqlglot.parse_one('((a + b)) * c')).sql() == '(a + b) * c'
    alone = _unparen(sqlglot.parse_one('coalesce((a / 100), 0) + 1')).sql()
    inside = _unparen(sqlglot.parse_one('(coalesce((a / 100), 0) + 1)')).sql()
    assert alone == inside == 'COALESCE(a / 100, 0) + 1'


def test_brackets_that_still_matter_are_kept() -> None:
    import sqlglot

    from konstruct.analysis.explain import _unparen

    assert _unparen(sqlglot.parse_one('(addr).city', read='duckdb')).sql('duckdb') == '(addr).city'
    assert (
        _unparen(sqlglot.parse_one('((a > 0) OR (b > 0))', read='duckdb')).sql('duckdb') == 'a > 0 OR b > 0'
    )
    # Built directly: sqlglot's own parser recurses too deeply for such a formula on Python 3.11.
    from sqlglot import exp

    deep: exp.Expr = exp.column('a')
    for i in range(1500):
        deep = exp.Paren(this=exp.Add(this=deep, expression=exp.column(f'c{i}')))
    tidied = _unparen(deep)
    assert isinstance(tidied, exp.Add) and tidied.expression.name == 'c1499'


def test_an_or_composed_into_an_and_keeps_its_brackets(tmp_path: Path) -> None:
    con = duckdb.connect(str(tmp_path / 'w.duckdb'))
    for statement in (
        'create schema raw',
        'create schema mart',
        'create table raw.t (id int, a int, b int, c int)',
        'create view mart.v1 as select id, a > 0 or b > 0 as flag, c from raw.t',
        'create view mart.v2 as select id, flag and c > 0 as both_ from mart.v1',
    ):
        con.execute(statement)
    con.close()
    project = Project(tmp_path)
    project.build()
    assert project.explain('v2.both_').composed == '(t.a > 0 OR t.b > 0) AND t.c > 0'


def _tree_text(explainer: Any, node: Any) -> str | None:
    from konstruct.analysis.explain import _unparen

    composed = explainer.compose(node)
    if composed.tree is None:
        return None
    text: str = _unparen(composed.tree.copy()).sql(dialect=explainer.namer.sqlglot, copy=False)
    return text


def test_composed_text_is_the_composed_tree_printed(tmp_path: Path) -> None:
    from konstruct.analysis.explain import Explainer
    from tests.test_scale import make_project

    con = duckdb.connect(str(tmp_path / 'w.duckdb'))
    for statement in (
        'create schema raw',
        'create schema mart',
        'create table raw.t (id int, a int, b int, c int, s struct(city varchar))',
        'create view mart.v1 as select id, (a + b) as total, a > 0 or b > 0 as flag, (s) as s, '
        'coalesce((a / 100), 0) + 1 as g, -(a - b) as neg, ((a)) as a, c from raw.t',
        'create view mart.v2 as select id, total * 2 as twice, flag and c > 0 as both_, (s).city as city, '
        '(g) * (total) as product, neg + 1 as n1, a - neg as diff, (((total))) as again from mart.v1',
        'create view mart.v3 as select twice / product as ratio, not both_ as neither, '
        'case when (diff > 0) then (again) else (twice) end as pick from mart.v2',
    ):
        con.execute(statement)
    con.close()
    (tmp_path / 'big').mkdir()
    make_project(tmp_path / 'big', 60, 6)
    for root in (tmp_path, tmp_path / 'big'):
        project = Project(root)
        project.build(run_dbt=False)
        explainer = Explainer(project.graph)
        columns = [n for n in project.graph.nodes.values() if n.kind == 'column']
        assert columns
        for column in columns:
            assert explainer.composed_text(column) == _tree_text(Explainer(project.graph), column), column.id


TEXT_INPUTS = {
    'col': 'db.s.r0.a',
    'lit': '1',
    'add': 'db.s.r0.a + db.s.r0.b',
    'fn': 'UPPER(db.s.r0.a)',
    'cmp': 'db.s.r0.a > 0',
    'or': 'db.s.r0.a > 0 OR db.s.r0.b > 0',
    'cast': 'CAST(db.s.r0.a AS INT)',
    'mod': 'db.s.r0.a % 7',
}
TEXT_CONTEXTS = [
    'x + y',
    'x * (y - 1)',
    'MOD(x, 7)',
    'x % y',
    'COALESCE(x, y)',
    'CASE WHEN x > 0 THEN y ELSE 0 END',
    'CASE WHEN x THEN y END',
    'COUNTIF(x)',
    'UPPER(x)',
    'x = ANY(ARRAY[y])',
    'x + INTERVAL x DAY',
    'NOT x',
    'x AND y',
    'SUM(x) OVER (PARTITION BY y)',
    'x IN (y)',
    'x BETWEEN y AND 1',
    'ROUND(x / y, 2)',
]


@pytest.mark.parametrize(
    'dialect', ['duckdb', 'bigquery', 'snowflake', 'databricks', 'postgres', 'tsql', 'oracle']
)
def test_composed_text_matches_the_tree_in_every_context(dialect: str) -> None:
    """Each input shape in each context gives the text the composed tree prints, in dialects
    whose printers look at an operand's kind (BigQuery's MOD, T-SQL's conditions)."""
    import re

    import sqlglot

    from konstruct.analysis.explain import Explainer
    from konstruct.core.graph import Graph, Node
    from konstruct.core.sql import _unbracket

    graph = Graph()
    graph.meta['dialect'] = dialect
    r0 = graph.add_node(Node(id='table.db.s.r0', kind='table', name='r0', relation='db.s.r0'))
    r1 = graph.add_node(Node(id='table.db.s.r1', kind='table', name='r1', relation='db.s.r1'))
    r2 = graph.add_node(Node(id='table.db.s.r2', kind='table', name='r2', relation='db.s.r2'))
    graph.add_column(r0, 'a')
    graph.add_column(r0, 'b')

    def written(sql: str) -> str | None:
        try:
            return _unbracket(sqlglot.parse_one(sql, read=dialect).sql(dialect=dialect))
        except Exception:  # a form this dialect does not have
            return None

    for name, sql in TEXT_INPUTS.items():
        expression = written(sql)
        if expression is not None:
            column = graph.add_column(r1, name, attrs={'expression': expression})
            graph.add_edge(column.id, 'table.db.s.r0#a', 'derived_from')
            graph.add_edge(column.id, 'table.db.s.r0#b', 'derived_from')
    count = 0
    for context in TEXT_CONTEXTS:
        for x in TEXT_INPUTS:
            sql = re.sub(r'\by\b', 'db.s.r1.col', re.sub(r'\bx\b', f'db.s.r1.{x}', context))
            expression = written(sql)
            if expression is None or f'table.db.s.r1#{x}' not in graph.nodes:
                continue
            count += 1
            column = graph.add_column(r2, f'z{count}', attrs={'expression': expression})
            graph.add_edge(column.id, f'table.db.s.r1#{x}', 'derived_from')
            if 'table.db.s.r1#col' in graph.nodes:
                graph.add_edge(column.id, 'table.db.s.r1#col', 'derived_from')
    assert count > 50
    for column in [n for n in graph.nodes.values() if n.kind == 'column']:
        assert Explainer(graph).composed_text(column) == _tree_text(Explainer(graph), column), (
            column.attrs.get('expression')
        )


def test_a_placeholder_in_the_sql_text_is_not_mistaken_for_one(tmp_path: Path) -> None:
    from konstruct.analysis.explain import Explainer

    con = duckdb.connect(str(tmp_path / 'w.duckdb'))
    con.execute('create table t (s varchar, a int, b int)')
    con.execute('create view v1 as select upper(s) as su, a + b as total from t')
    con.execute("create view v2 as select case when su = 'KSLOT1KSLOT' then total end as pick from v1")
    con.close()
    project = Project(tmp_path)
    project.build(run_dbt=False)
    graph = project.graph
    [pick] = [n for n in graph.nodes.values() if n.kind == 'column' and n.name == 'pick']
    text = Explainer(graph).composed_text(pick)
    assert text == _tree_text(Explainer(graph), pick)
    assert text is not None and "'KSLOT1KSLOT'" in text


def test_conflicts_see_one_formula_written_two_ways_in_bigquery(tmp_path: Path) -> None:
    # MOD over a derived input prints as the composed tree does, so the two are one formula.
    (tmp_path / 'konstruct.toml').write_text('dialect = "bigquery"\n')
    (tmp_path / 'a.sql').write_text('create table p.s.base as select gross, refunds from p.s.raw')
    (tmp_path / 'b.sql').write_text(
        'create table p.s.net as select gross - refunds as net_cents from p.s.base'
    )
    (tmp_path / 'c.sql').write_text(
        'create table p.s.finance as select mod(gross - refunds, 100) as revenue_remainder from p.s.base'
    )
    (tmp_path / 'd.sql').write_text(
        'create table p.s.marketing as select mod(net_cents, 100) as revenue_remainder from p.s.net'
    )
    project = Project(tmp_path)
    project.build(run_dbt=False, warehouses=False)
    assert project.conflicts().conflicts == []


@pytest.mark.parametrize('dialect', ['bigquery', 'tsql'])
def test_a_cycle_through_a_context_composed_as_a_tree_stops_where_the_text_does(dialect: str) -> None:
    from konstruct.analysis.explain import Explainer
    from konstruct.core.graph import Graph, Node

    graph = Graph()
    graph.meta['dialect'] = dialect
    tables = {
        n: graph.add_node(Node(id=f'table.db.s.{n}', kind='table', name=n, relation=f'db.s.{n}'))
        for n in ('r0', 'r1', 'r2', 'r3')
    }
    graph.add_column(tables['r0'], 'x')
    graph.add_column(tables['r0'], 'k')
    inner = 'MOD(db.s.r1.a, db.s.r3.k)' if dialect == 'bigquery' else 'IIF(db.s.r1.a, 1, db.s.r3.k)'
    columns = {
        'r1.a': ('db.s.r2.b + db.s.r0.x', ['r2.b', 'r0.x']),
        'r2.b': (inner, ['r1.a', 'r3.k']),
        'r3.k': ('db.s.r0.k', ['r0.k']),
    }
    for ref, (expression, _) in columns.items():
        table, name = ref.split('.')
        graph.add_column(tables[table], name, attrs={'expression': expression})
    for ref, (_, inputs) in columns.items():
        for source in inputs:
            graph.add_edge(
                f'table.db.s.{ref.replace(".", "#")}',
                f'table.db.s.{source.replace(".", "#")}',
                'derived_from',
            )
    for ref in columns:
        node = graph.nodes[f'table.db.s.{ref.replace(".", "#")}']
        assert Explainer(graph).composed_text(node) == _tree_text(Explainer(graph), node), ref
