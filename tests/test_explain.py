"""konstruct explain: column formulas composed down to sources, and checked against the data."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

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
    assert 'WHERE: customers.customer_lifetime_value > 0' in text
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
