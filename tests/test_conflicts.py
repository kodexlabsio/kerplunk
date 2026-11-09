"""konstruct conflicts: one business idea computed in more than one way."""

from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path

import duckdb
import pytest

from konstruct import Project
from konstruct.analysis.conflicts import core
from konstruct.interfaces.cli import main

FINANCE = """\
create table finance.daily_revenue as
select order_date, sum(amount) as revenue
from raw.orders
where status = 'completed'
group by order_date
"""
MARKETING = """\
create table marketing.daily_performance as
select order_date, sum(amount) as total_revenue, count(*) as orders
from raw.orders
group by order_date
"""
WEEKLY_DAG = """\
from airflow.sdk import DAG
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator
with DAG("marketing_report"):
    SQLExecuteQueryOperator(
        task_id="weekly",
        sql="create table marketing.weekly as select date_trunc('week', order_date) as week, "
        "sum(total_revenue) as revenue from marketing.daily_performance group by 1",
    )
"""


def make_shop(root: Path, *, invoices: bool = False, definitions: str | None = None) -> Project:
    con = duckdb.connect(str(root / 'shop.duckdb'))
    con.execute('create schema raw')
    con.execute('create table raw.orders (id int, order_date date, amount decimal(10,2), status varchar)')
    con.execute('create table raw.invoices (id int, invoice_date date, total decimal(10,2))')
    con.close()
    (root / 'finance').mkdir()
    (root / 'marketing').mkdir()
    (root / 'dags').mkdir()
    (root / 'finance' / 'revenue.sql').write_text(FINANCE)
    (root / 'marketing' / 'daily.sql').write_text(MARKETING)
    (root / 'dags' / 'marketing.py').write_text(WEEKLY_DAG)
    if invoices:
        (root / 'finance' / 'invoiced.sql').write_text(
            'create table finance.invoiced as select invoice_date, sum(total) as sales_amount '
            'from raw.invoices group by invoice_date'
        )
    if definitions:
        (root / 'definitions.yml').write_text(textwrap.dedent(definitions))
    project = Project(root)
    project.build()
    return project


def test_names_reduce_to_the_idea() -> None:
    assert core('total_revenue') == core('revenue') == core('sales_amount') == core('NetRevenue') - {'net'}
    assert core('number_of_orders') == core('order_count') == frozenset({'count', 'order'})
    assert core('customer_lifetime_value') == frozenset({'customer', 'lifetime', 'value'})
    assert core('total') == frozenset()


def test_two_revenue_definitions_that_differ_by_one_filter(tmp_path: Path) -> None:
    """Acceptance: the pair is reported with the differing filter and its file."""
    result = make_shop(tmp_path).conflicts()
    [conflict] = result.conflicts
    assert {conflict.a.label, conflict.b.label} == {
        'daily_revenue.revenue',
        'daily_performance.total_revenue',
    }
    assert conflict.idea == 'revenue'
    [difference] = conflict.differences
    assert difference.kind == 'filter'
    assert difference.detail == "WHERE: orders.status = 'completed'"
    assert difference.path == 'finance/revenue.sql'
    side = conflict.a if difference.side == 'a' else conflict.b
    assert side.label == 'daily_revenue.revenue'
    assert conflict.a.composed == conflict.b.composed == 'SUM(orders.amount)'
    # weekly.revenue only re-aggregates total_revenue, so it is carried into, not a third side.
    marketing = conflict.b if conflict.b.label.startswith('daily_performance') else conflict.a
    assert marketing.carried_into == ['weekly.revenue']
    assert marketing.path == 'marketing/daily.sql'
    assert result.summary() == '1 conflicting definition among 5 computed columns.'


def test_different_sources_and_ranking(tmp_path: Path) -> None:
    result = make_shop(tmp_path, invoices=True).conflicts()
    pairs = [{c.a.label, c.b.label} for c in result.conflicts]
    assert {'daily_revenue.revenue', 'invoiced.sales_amount'} in pairs
    invoice = next(
        c
        for c in result.conflicts
        if {c.a.label, c.b.label} == {'daily_revenue.revenue', 'invoiced.sales_amount'}
    )
    kinds = sorted((d.kind, d.detail) for d in invoice.differences)
    assert kinds == [
        ('filter', "WHERE: orders.status = 'completed'"),
        ('source', 'invoices.total'),
        ('source', 'orders.amount'),
    ]
    # Pairs involving total_revenue, which a task reads downstream, rank above the one without.
    scores = [c.score for c in result.conflicts]
    assert scores == sorted(scores, reverse=True)
    assert 'daily_performance.total_revenue' in {result.conflicts[0].a.label, result.conflicts[0].b.label}
    assert result.conflicts[-1] is invoice


def test_terms_group_columns_named_differently(tmp_path: Path) -> None:
    project = make_shop(
        tmp_path,
        definitions="""
        terms:
          - name: Gross merchandise value
            synonyms: [GMV]
            definition: Value of everything ordered.
            columns: [daily_revenue.revenue]
        """,
    )
    (tmp_path / 'finance' / 'gmv.sql').write_text(
        'create table finance.gmv_daily as select order_date, sum(amount) * 1.2 as gmv from raw.orders group by order_date'
    )
    project.build()
    result = project.conflicts()
    by_term = [c for c in result.conflicts if c.idea == 'Gross merchandise value']
    assert by_term
    labels = {label for c in by_term for label in (c.a.label, c.b.label)}
    assert {'gmv_daily.gmv', 'daily_revenue.revenue'} <= labels
    assert any("business term 'Gross merchandise value'" in c.reason for c in by_term)
    assert any('Gross merchandise value' in c.a.terms + c.b.terms for c in by_term)


@pytest.mark.dbt
def test_no_noise_on_the_example(jaffle_built_graph: Path, tmp_path: Path) -> None:
    copy = tmp_path / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph / 'konstruct-out', copy / 'konstruct-out')
    result = Project(copy).conflicts()
    assert result.conflicts == []
    assert result.candidates > 10


def test_command_files_and_formats(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_shop(tmp_path)
    assert main(['conflicts', '-C', str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith('1 conflicting definition among 5 computed columns.')
    assert (
        "filter only in daily_revenue.revenue: WHERE orders.status = 'completed'  (finance/revenue.sql)"
        in out
    )
    report = (tmp_path / 'konstruct-out' / 'CONFLICTS.md').read_text()
    assert report.startswith('# Conflicting definitions')
    assert '## 1. revenue: `daily_performance.total_revenue` and `daily_revenue.revenue`' in report or (
        '## 1. revenue: `daily_revenue.revenue` and `daily_performance.total_revenue`' in report
    )
    assert (
        '- filter (only in ' in report
        and "`WHERE: orders.status = 'completed'`, in `finance/revenue.sql`" in report
    )
    data = json.loads((tmp_path / 'konstruct-out' / 'conflicts.json').read_text())
    assert data['conflicts'][0]['differences'][0]['path'] == 'finance/revenue.sql'
    assert main(['conflicts', '-C', str(tmp_path), '--fail-on-conflict', '--no-write', '-q']) == 1
    capsys.readouterr()
    assert main(['conflicts', '-C', str(tmp_path), '--format', 'markdown', '--no-write']) == 0
    assert capsys.readouterr().out.startswith('# Conflicting definitions')
    assert main(['conflicts', '-C', str(tmp_path), '--json', '--no-write', '-n', '0']) == 0
    assert json.loads(capsys.readouterr().out)['conflicts'] == []
