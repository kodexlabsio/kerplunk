"""konstruct conflicts: one business idea computed in more than one way."""

from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path

import duckdb
import pytest

from konstruct import Project
from konstruct.analysis.conflicts import SAME_UNLESS_NULL, core, find_conflicts
from konstruct.core.graph import Graph, Node
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
    # An amount names a measure only when nothing else does; amt is short for it.
    assert core('amount') == core('total_amt') == frozenset({'value'})
    assert core('refund_amount') == core('refunds') == frozenset({'refund'})


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


def test_filters_written_differently_are_not_a_difference(tmp_path: Path) -> None:
    project = make_shop(tmp_path)
    (tmp_path / 'marketing' / 'daily.sql').write_text(
        MARKETING.replace('group by order_date', "where 'completed' = status\ngroup by order_date")
    )
    project.build()
    assert project.conflicts().conflicts == []


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


def test_conflicts_touching_columns_are_those_of_every_conflict(tmp_path: Path) -> None:
    """The gate compares only the pairs of the columns a change alters: the same conflicts, in the
    same order, as comparing every pair and keeping those."""
    project = make_shop(tmp_path, invoices=True)
    every = find_conflicts(project.graph)
    columns = sorted({side.column for c in every.conflicts for side in (c.a, c.b)})
    assert len(every.conflicts) > 1 and len(columns) > 2
    for touching in [[column] for column in columns] + [columns[:2], columns]:
        found = find_conflicts(project.graph, touching=set(touching))
        expected = [c for c in every.conflicts if c.a.column in touching or c.b.column in touching]
        assert [c.to_dict() for c in found.conflicts] == [c.to_dict() for c in expected]
        assert (found.candidates, found.groups) == (every.candidates, every.groups)
    assert find_conflicts(project.graph, touching=set()).conflicts == []


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


def test_the_page_shows_the_conflicts_without_composing_its_formulas_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """graph.html and explain.json from the build of this graph keep their formulas, SQL and
    ontology, and gain the conflicts; a page of another graph is made again in full."""
    from konstruct.outputs import html

    project = make_shop(tmp_path)
    out = tmp_path / 'konstruct-out'
    built = json.loads((out / 'explain.json').read_text())
    assert built['conflicts'] == {}

    def composed(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError('the formulas were composed again')

    with monkeypatch.context() as patch:
        patch.setattr(html, 'explanations', composed)
        project.conflicts(write=True)
    shown = json.loads((out / 'explain.json').read_text())
    assert shown['conflicts']['conflicts'][0]['idea'] == 'revenue'
    assert {k: v for k, v in shown.items() if k != 'conflicts'} == {
        k: v for k, v in built.items() if k != 'conflicts'
    }
    page = (out / 'graph.html').read_text()
    assert html._embedded(out / 'graph.html', html._embed_json(project.graph.to_dict())) == shown
    assert page == html._page(html._embed_json(shown), html._embed_json(project.graph.to_dict()))

    html.write_html(Graph(), out / 'graph.html')
    project.conflicts(write=True)
    again = html.viewer_data(project.graph, out)
    assert json.loads((out / 'explain.json').read_text()) == json.loads(json.dumps(again))
    assert html._embedded(out / 'graph.html', html._embed_json(project.graph.to_dict())) is not None


NET_FINANCE = """\
create table finance.revenue as
select order_date, sum(amount) - sum(discount) as net_revenue
from raw.orders
group by order_date
"""
NET_MARKETING = """\
create table marketing.daily as
select order_date, sum(amount - discount) as net_revenue
from raw.orders
group by order_date
"""


def net_revenue_shop(root: Path, marketing: str = NET_MARKETING) -> Project:
    con = duckdb.connect(str(root / 'shop.duckdb'))
    con.execute('create schema raw')
    con.execute(
        'create table raw.orders (id int, order_date date, amount decimal(10,2), discount decimal(10,2), '
        'status varchar)'
    )
    con.close()
    (root / 'finance').mkdir()
    (root / 'marketing').mkdir()
    (root / 'finance' / 'revenue.sql').write_text(NET_FINANCE)
    (root / 'marketing' / 'daily.sql').write_text(textwrap.dedent(marketing))
    project = Project(root)
    project.build()
    return project


def _net_revenue_graph(not_null: bool, formulas: tuple[str, str] | None = None) -> Graph:
    """Two models computing net revenue as SUM(amount) - SUM(discount) and SUM(amount - discount),
    or as ``formulas``, with or without not_null tests on the source columns."""
    graph = Graph()
    orders = graph.add_node(Node('source.raw.orders', 'source', 'orders', relation='db.raw.orders'))
    for name in ('amount', 'discount'):
        column = graph.add_column(orders, name)
        if not_null:
            test = graph.add_node(
                Node(f'test.not_null_{name}', 'test', f'not_null_{name}', attrs={'test': 'not_null'})
            )
            graph.add_edge(test.id, column.id, 'tests')
    finance, marketing = formulas or (
        'SUM(db.raw.orders.amount) - SUM(db.raw.orders.discount)',
        'SUM(db.raw.orders.amount - db.raw.orders.discount)',
    )
    for name, formula in (('finance', finance), ('marketing', marketing)):
        model = graph.add_node(Node(f'model.{name}', 'model', name, relation=f'db.s.{name}'))
        column = graph.add_column(model, 'net_revenue', attrs={'expression': formula})
        graph.add_edge(orders.id, model.id, 'feeds')
        for source in ('amount', 'discount'):
            graph.add_edge(column.id, f'{orders.id}#{source}', 'derived_from')
    return graph


def test_conflicts_count_formulas_that_are_the_same_as_one() -> None:
    """Acceptance: SUM(a) - SUM(b) and SUM(a - b) are one definition when a and b are never NULL."""
    result = find_conflicts(_net_revenue_graph(not_null=True))
    assert result.conflicts == []
    assert result.candidates == 2
    [conflict] = find_conflicts(_net_revenue_graph(not_null=False)).conflicts
    [difference] = conflict.differences
    assert difference.detail.endswith(f', {SAME_UNLESS_NULL}')


def test_formulas_the_same_only_without_nulls_are_reported_with_values(tmp_path: Path) -> None:
    result = net_revenue_shop(tmp_path).conflicts()
    [conflict] = result.conflicts
    [difference] = conflict.differences
    assert difference.kind == 'formula'
    assert difference.detail.endswith('the same unless a source value is NULL or there are no rows')
    witness = difference.witness
    assert witness is not None and witness.kind == 'values'
    assert 'NULL' in str(witness)


def test_conflicts_that_differ_by_a_filter_are_reported(tmp_path: Path) -> None:
    filtered = NET_MARKETING.replace('from raw.orders', "from raw.orders where status = 'completed'")
    [conflict] = net_revenue_shop(tmp_path, filtered).conflicts().conflicts
    assert [(d.kind, d.detail) for d in conflict.differences] == [
        ('filter', "WHERE: orders.status = 'completed'"),
        (
            'formula',
            'SUM(orders.amount) - SUM(orders.discount) against SUM(orders.amount - orders.discount), '
            + SAME_UNLESS_NULL,
        ),
    ]


def test_conflicting_formulas_show_values_that_differ(tmp_path: Path) -> None:
    other = NET_MARKETING.replace('sum(amount - discount)', 'sum(amount) - 2 * sum(discount)')
    result = net_revenue_shop(tmp_path, other).conflicts()
    [conflict] = result.conflicts
    [difference] = conflict.differences
    assert difference.kind == 'formula'
    witness = difference.witness
    assert witness is not None and witness.kind == 'values'
    assert witness.rows is not None
    assert {'orders.amount', 'orders.discount'} <= set(witness.rows[0])
    assert witness.a != witness.b
    assert f'\n        {witness}' in str(result)
    assert f'  - {witness}' in result.markdown().splitlines()
    assert result.to_dict()['conflicts'][0]['differences'][0]['witness']['kind'] == 'values'


def test_conflicting_aggregations_show_the_function(tmp_path: Path) -> None:
    other = NET_MARKETING.replace('sum(amount - discount)', 'max(amount - discount)')
    [conflict] = net_revenue_shop(tmp_path, other).conflicts().conflicts
    [difference] = [d for d in conflict.differences if d.kind == 'aggregation']
    assert difference.witness is not None
    assert difference.witness.kind == 'functions'
    assert 'MAX' in difference.witness.functions


def test_formulas_written_the_same_are_never_a_formula_difference() -> None:
    # A not_null test on one of the two makes their verdict differ, but not their formula.
    same = 'SUM(db.raw.orders.amount) * 1'
    graph = _net_revenue_graph(not_null=False, formulas=(same, same))
    test = graph.add_node(Node('test.not_null_net', 'test', 'not_null_net', attrs={'test': 'not_null'}))
    graph.add_edge(test.id, 'model.finance#net_revenue', 'tests')
    result = find_conflicts(graph)
    assert result.candidates == 2
    assert result.conflicts == []


def _amount_and_credit_not_null(graph: Graph) -> Graph:
    """The graph with not_null tests on orders.amount and customers.credit."""
    for relation, column in (('orders', 'amount'), ('customers', 'credit')):
        [tested] = [
            n for n in graph.nodes.values() if n.kind == 'column' and n.id.endswith(f'.{relation}#{column}')
        ]
        test = graph.add_node(Node(f'test.not_null_{column}', 'test', 'not_null', attrs={'test': 'not_null'}))
        graph.add_edge(test.id, tested.id, 'tests')
    return graph


@pytest.mark.parametrize('join', ['on customers.customer_id = orders.customer_id', 'using (customer_id)'])
def test_a_not_null_measure_read_through_an_outer_join_conflicts(tmp_path: Path, join: str) -> None:
    """Acceptance: amount and credit are never NULL in their own tables, but a customer with no
    order has no amount, so SUM(amount) - SUM(credit) and SUM(amount - credit) differ."""
    con = duckdb.connect(str(tmp_path / 'shop.duckdb'))
    con.execute('create schema raw')
    con.execute('create table raw.customers (customer_id int, region varchar, credit int)')
    con.execute('create table raw.orders (customer_id int, amount int)')
    con.close()
    (tmp_path / 'customer_orders.sql').write_text(
        'create table marts.customer_orders as\n'
        'select customers.region, customers.credit, orders.amount\n'
        f'from raw.customers left join raw.orders {join}\n'
    )
    for name, formula in (('apart', 'sum(amount) - sum(credit)'), ('together', 'sum(amount - credit)')):
        (tmp_path / f'{name}.sql').write_text(
            f'create table marts.{name} as\n'
            f'select region, {formula} as net_amount from marts.customer_orders group by region\n'
        )
    graph = _amount_and_credit_not_null(Project(tmp_path).build().graph)
    [conflict] = find_conflicts(graph).conflicts
    [difference] = [d for d in conflict.differences if d.kind == 'formula']
    assert difference.detail.endswith(f', {SAME_UNLESS_NULL}')
    assert difference.witness is not None and difference.witness.kind == 'values'
    # Only the outer join makes them differ.
    inner = tmp_path / 'customer_orders.sql'
    inner.write_text(inner.read_text().replace(' left join ', ' join '))
    graph = _amount_and_credit_not_null(Project(tmp_path).build().graph)
    assert find_conflicts(graph).conflicts == []


def test_an_outer_join_on_one_route_is_the_join_difference_alone(tmp_path: Path) -> None:
    # credit is read from the preserved side of a LEFT JOIN on one route and directly on the
    # other: the formulas are the same once it is taken as it is in customers, and the join is
    # reported on its own.
    con = duckdb.connect(str(tmp_path / 'shop.duckdb'))
    con.execute('create schema raw')
    con.execute('create table raw.customers (customer_id int, region varchar, credit int)')
    con.execute('create table raw.orders (customer_id int, amount int)')
    con.close()
    (tmp_path / 'a.sql').write_text(
        'create table marts.a as\n'
        'select customers.region, sum(customers.credit) as total_credit from raw.customers\n'
        'left join raw.orders on customers.customer_id = orders.customer_id group by customers.region\n'
    )
    (tmp_path / 'b.sql').write_text(
        'create table marts.b as\n'
        'select region, sum(credit * 1) as total_credit from raw.customers group by region\n'
    )
    graph = Project(tmp_path).build().graph
    [credit] = [n for n in graph.nodes.values() if n.kind == 'column' and n.id.endswith('.customers#credit')]
    graph.add_node(Node('test.not_null_credit', 'test', 'not_null', attrs={'test': 'not_null'}))
    graph.add_edge('test.not_null_credit', credit.id, 'tests')
    [conflict] = find_conflicts(graph).conflicts
    assert [d.kind for d in conflict.differences] == ['join']


def test_a_source_difference_hides_a_formula_the_same_unless_null() -> None:
    graph = _net_revenue_graph(
        not_null=False,
        formulas=('SUM(db.raw.orders.amount) + SUM(db.raw.orders.discount) * 0', 'SUM(db.raw.orders.amount)'),
    )
    [conflict] = find_conflicts(graph).conflicts
    assert [(d.kind, d.side) for d in conflict.differences] == [('source', 'a')]


def test_each_conflict_lists_the_copies_of_its_own_idea(tmp_path: Path) -> None:
    """base.revenue is one side of two ideas: revenue, where copy.revenue is derived from it, and
    the term Booked value, where nothing is. Each conflict lists the copies of its own idea."""
    (tmp_path / 'konstruct.toml').write_text('dialect = "duckdb"\n')
    (tmp_path / 'base.sql').write_text(
        'create table main.base as select customer_id, sum(amount) as revenue from raw.orders group by 1'
    )
    (tmp_path / 'copy.sql').write_text(
        'create table main.copy as select customer_id, revenue * 1 as revenue from main.base'
    )
    (tmp_path / 'other.sql').write_text(
        'create table main.other as select customer_id, sum(amount) as revenue from raw.orders '
        "where status = 'paid' group by 1"
    )
    (tmp_path / 'booked.sql').write_text(
        'create table main.booked as select customer_id, sum(total) as booked from raw.invoices group by 1'
    )
    (tmp_path / 'definitions.yml').write_text(
        'terms:\n  - name: Booked value\n    definition: What customers are billed.\n'
        '    columns: [main.base.revenue, main.booked.booked]\n'
    )
    graph = Project(tmp_path).build(run_dbt=False, warehouses=False).graph
    found = {(c.idea, c.a.label, c.b.label): c for c in find_conflicts(graph).conflicts}
    assert set(found) == {
        ('revenue', 'base.revenue', 'other.revenue'),
        ('Booked value', 'base.revenue', 'booked.booked'),
    }
    assert found['revenue', 'base.revenue', 'other.revenue'].a.carried_into == ['copy.revenue']
    assert found['Booked value', 'base.revenue', 'booked.booked'].a.carried_into == []
    assert 'carried into `copy.revenue`' in find_conflicts(graph).markdown()
    # Only the pairs of one column, as the gate compares them, give the same.
    [revenue] = find_conflicts(graph, touching={'table.main.other#revenue'}).conflicts
    assert revenue.a.carried_into == ['copy.revenue']


JOINED_ON_TRUE = (
    'create table main.pairs as select a.id, b.amount from raw.orders a left join raw.orders b on true '
    'left join raw.orders c on c.customer_id = b.customer_id'
)


def _joined_on_true(root: Path) -> Path:
    """A project whose model joins a reading on an ON that does not name it, which the build lists
    once more naming the reading."""
    root.mkdir(parents=True, exist_ok=True)
    (root / 'konstruct.toml').write_text('dialect = "duckdb"\n')
    (root / 'pairs.sql').write_text(JOINED_ON_TRUE)
    assert main(['build', str(root), '--no-warehouse', '-q']) == 0
    return root


def test_a_join_named_for_its_reading_is_one_join(tmp_path: Path) -> None:
    from konstruct.analysis.conflicts import _Finder

    graph = Project(_joined_on_true(tmp_path / 'project')).graph
    _, joins = _Finder(graph).conditions(graph.nodes['table.main.pairs'])
    assert len(joins) == 2, joins
    assert not any('"orders#1" ON' in text for _, text, _ in joins)
