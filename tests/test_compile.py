"""The ontology query compiler: a metric by entities and properties, compiled to SQL that joins only
along relationships that keep each row once, with its required filters and the trust of its facts."""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
import sqlglot
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlglot import exp

from konstruct import Project
from konstruct.analysis.checksql import Checker, check_sql
from konstruct.analysis.compile import Compiled, OntologyQuery, compile_query
from konstruct.analysis.query import QueryError
from konstruct.core.graph import Graph, Node
from konstruct.semantics.ontology import derive as derive_ontology
from konstruct.semantics.world import relate

from .test_checksql import (
    MISSED_SHAPES,
    ORDER_PAYMENTS,
    SIBLING_REVENUE,
    make_shop,
    revenue_shop,
    shop_rows,
    sibling_shop,
)
from .test_ontology import _relation, _term
from .test_world import _failed_customer, _shop

# A small graph, no warehouse


def _compile(graph: Graph, metric: str, **options: Any) -> Compiled:
    dialect = options.pop('dialect', None)
    return compile_query(graph, OntologyQuery(metric, **options), dialect=dialect)


def test_a_metric_by_a_property_of_the_entity_it_refers_to() -> None:
    result = _compile(_shop(), 'revenue', by=['customer.name'])
    assert result.compiled and result.refused == []
    assert result.sql == (
        'SELECT\n'
        '  customers.name AS name,\n'
        '  SUM(orders.amount) AS revenue\n'
        'FROM db.main.orders AS orders\n'
        'LEFT JOIN db.main.customers AS customers\n'
        '  ON customers.customer_id = orders.customer_id\n'
        'GROUP BY\n'
        '  customers.name\n'
        'ORDER BY\n'
        '  customers.name'
    )
    assert (result.relation, result.grain, result.aggregation) == ('orders', 'order', 'sum')
    [hop] = result.path
    assert (hop.start, hop.end, hop.cardinality) == ('order', 'customer', 'many-to-one')
    kinds = {(f.kind, f.name): f.confidence for f in result.facts}
    assert kinds[('metric', 'Revenue')] == 'verified'
    assert kinds[('relationship', 'orders.customer_id = customers.customer_id')] == 'inferred'
    assert kinds[('key', 'customers.customer_id')] == 'declared'
    # The relationship is read from a join alone, so the whole result is only inferred, and says why.
    assert result.confidence == 'inferred'
    assert any(
        'relationship orders.customer_id = customers.customer_id is only inferred' in f for f in result.flags
    )


def _not_null(g: Graph, column: str) -> None:
    test = g.add_node(Node(f'test.shop.not_null_{column}', 'test', 'not_null', attrs={'test': 'not_null'}))
    g.add_edge(test.id, column, 'tests')


def test_the_metric_at_its_own_grain_is_its_column_as_it_is() -> None:
    g = _shop()
    # Unique but nullable, orders.order_id could hold several rows under one null group: summed.
    assert _compile(g, 'revenue', by=['order']).aggregation == 'sum'
    _not_null(g, 'model.shop.orders#order_id')
    result = _compile(g, 'revenue', by=['order'])
    assert result.aggregation == 'none'
    assert 'SUM(' not in (result.sql or '') and 'GROUP BY' not in (result.sql or '')
    assert 'orders.amount AS revenue' in (result.sql or '')
    # The read rests on the key, so it is a fact of the result.
    assert ('key', 'orders.order_id') in {(f.kind, f.name) for f in result.facts}


def test_a_join_that_would_repeat_rows_is_refused_naming_the_link() -> None:
    g = _shop()
    # A customer property is many-to-one from orders; an order property from customers fans out.
    result = _compile(g, 'customer lifetime', by=['order.amount'])
    assert not result.compiled and result.sql is None
    [refusal] = result.refused
    assert refusal.item == 'by order.amount'
    assert refusal.link == 'orders.customer_id = customers.customer_id'
    assert 'one-to-many' in refusal.reason and 'more than once' in refusal.reason
    data = result.to_dict()
    assert data['compiled'] is False and data['refused'][0]['link'] == refusal.link
    assert str(result).startswith('Refused customer lifetime by order.amount\n')


def test_a_link_verify_found_contradicted_is_never_walked() -> None:
    g = _shop()
    _failed_customer(g)
    result = _compile(g, 'revenue', by=['customer.name'])
    assert not result.compiled
    [refusal] = result.refused
    assert refusal.link == 'orders.customer_id = customers.customer_id'
    assert 'konstruct verify found the data contradicting' in refusal.reason


def test_a_required_filter_is_always_added() -> None:
    g = _shop()
    g.nodes['term.revenue'].attrs['filters'] = ['orders.amount > 0']
    for by in ([], ['order'], ['customer.name']):
        result = _compile(g, 'revenue', by=by)
        assert result.compiled, result.refused
        assert 'orders.amount > 0' in (result.sql or ''), by
        [required] = result.filters
        assert (required.filter, required.applied, required.by) == (
            'orders.amount > 0',
            'orders.amount > 0',
            'query',
        )
        assert any(f.kind == 'filter' for f in result.facts)


def test_a_required_filter_that_cannot_be_placed_refuses_the_query() -> None:
    g = _shop()
    g.nodes['term.revenue'].attrs['filters'] = ['raw_orders.id > 0']
    result = _compile(g, 'revenue')
    assert not result.compiled
    assert result.refused[0].item == 'filter raw_orders.id > 0'


def test_a_value_held_at_a_coarser_grain_is_never_added_up() -> None:
    """A per-customer value copied onto every order row repeats: summing it over orders counts
    each customer once per order."""
    g = _shop()
    copy = g.add_column(g.nodes['model.shop.orders'], 'customer_lifetime', data_type='DOUBLE')
    copy.attrs['expression'] = 'db.main.customers.lifetime'
    g.add_edge(copy.id, 'model.shop.customers#lifetime', 'derived_from', transform='direct')
    # Stated one row per customer, held on orders.
    _term(g, 'Customer value', [copy.id], kind='measure', status='approved', grain='one row per customer')
    stated = _compile(g, 'customer value', by=['customer.name'])
    assert not stated.compiled
    assert 'held one row per customer, but orders holds one row per order' in stated.refused[0].reason
    # With no grain stated, the copy from customers shows it.
    g.nodes['term.customer_value'].attrs.pop('grain')
    unstated = _compile(g, 'customer value')
    assert not unstated.compiled
    assert 'copies customers.lifetime, a value of another grain' in unstated.refused[0].reason


@pytest.mark.parametrize('label', ['JOIN ON', 'JOIN db.main."extras#2" ON'], ids=['plain', 'reading'])
@pytest.mark.parametrize('side', ['LEFT', 'RIGHT', 'FULL OUTER'])
def test_a_value_copied_across_an_outer_join_is_refused_whichever_side_it_is_on(
    side: str, label: str
) -> None:
    """An outer join's ON holds on no row of the side that keeps every row, so no column there
    lists it in ``row_conditions``; the join can repeat that side's rows all the same. So it can
    when its label names the reading it joins, as for a join whose ON does not name it."""
    g = _shop()
    extras = _relation(g, 'extras', ['order_id', 'bonus'])
    g.nodes[f'{extras.id}#bonus'].attrs['expression'] = 'SUM(db.main.raw_bonus.amount)'
    orders = g.nodes['model.shop.orders']
    # Only this join, so that it alone shows the copy crosses a join.
    orders.attrs['conditions'] = [f'{side} {label}: TRUE']
    copy = g.add_column(orders, 'bonus', data_type='DOUBLE')
    copy.attrs['expression'] = 'db.main.extras.bonus'
    copy.attrs['row_conditions'] = []
    g.add_edge(copy.id, f'{extras.id}#bonus', 'derived_from', transform='direct')
    _term(g, 'Bonus', [copy.id], kind='measure', status='approved')
    result = _compile(g, 'bonus', by=['order'])
    assert not result.compiled
    assert any('copies extras across a join' in r.reason for r in result.refused), result.refused


def _keyed_by(dialect: str, origins: list[str], enforced: bool, test: dict[str, str] | None) -> Graph:
    """The shop with customers keyed by a primary key (and ``test`` on its unique test), or by
    its unique test alone when ``test`` is given and the key is not."""
    g = _shop()
    g.meta['dialect'] = dialect
    customers = g.nodes['model.shop.customers']
    unique = g.nodes['test.shop.unique_customers_customer_id']
    if test is None:
        del g.nodes[unique.id]
        customers.attrs['primary_key'] = ['customer_id']
        customers.origins = origins
        if enforced:
            customers.attrs['primary_key_enforced'] = True
    else:
        unique.attrs.update(test)
    return g


@pytest.mark.parametrize(
    ('dialect', 'origins', 'enforced', 'test', 'joins'),
    [
        ('snowflake', ['sql'], False, None, False),
        ('bigquery', ['sql'], False, None, False),
        ('duckdb', ['dbt'], False, None, False),
        ('duckdb', ['dbt'], True, None, True),
        ('postgres', ['sql'], False, None, True),
        ('duckdb', [], False, {'severity': 'warn'}, False),
        ('duckdb', [], False, {'severity': 'WARN'}, False),
        ('duckdb', [], False, {'error_if': '>10'}, False),
        ('duckdb', [], False, {'severity': 'error', 'error_if': '!= 0'}, True),
    ],
    ids=[
        'a Snowflake primary key',
        'a BigQuery primary key',
        'a dbt primary key without an enforced contract',
        'a dbt primary key under an enforced contract',
        'a Postgres primary key',
        'a unique test that warns',
        'a unique test that warns, in capitals',
        'a unique test that allows ten failures',
        'a unique test that fails on one',
    ],
)
def test_a_key_neither_enforced_nor_tested_makes_no_join_safe(
    dialect: str, origins: list[str], enforced: bool, test: dict[str, str] | None, joins: bool
) -> None:
    """A primary key a warehouse never checks, a dbt constraint outside an enforced contract and a
    unique test whose failures do not fail it say a column is unique, but nothing makes it so: a
    join onto it could repeat each order."""
    g = _keyed_by(dialect, origins, enforced, test)
    result = _compile(g, 'revenue', by=['customer.name'])
    assert result.compiled is joins, result.refused
    try:
        cardinality = relate(g, 'order', 'customer').cardinality
    except QueryError:
        cardinality = None  # without its test, nothing makes customer_id a key of anything
    assert (cardinality == 'many-to-one') is joins, cardinality
    if not joins:
        [refusal] = result.refused
        assert 'customer' in refusal.reason, refusal.reason
    if test is None:
        # A declared key still names the entity, as its inferred key.
        [customer] = [e for e in derive_ontology(g).entities if e.name == 'customer']
        assert customer.confidence == ('declared' if joins else 'inferred'), customer


def test_a_unique_test_on_some_rows_makes_no_join_safe() -> None:
    """customers keeps a row per version, unique only where is_current: a join onto it repeats
    each order once per version."""
    g = _shop()
    g.nodes['test.shop.unique_customers_customer_id'].attrs['where'] = 'is_current'
    _term(g, 'Customer', ['model.shop.customers#customer_id'], kind='entity')
    result = _compile(g, 'revenue', by=['customer.name'])
    assert not result.compiled
    [refusal] = result.refused
    assert refusal.link == 'orders.customer_id = customers.customer_id'
    assert 'cardinality of order -> customer is not known' in refusal.reason


def test_the_range_stays_on_the_metrics_time_property() -> None:
    g = _shop()
    customers = g.nodes['model.shop.customers']
    g.add_column(customers, 'signed_up', data_type='TIMESTAMP')
    result = _compile(
        g,
        'customer count',
        by=['signed_up:month'],
        time='first_order',
        since='2024-01-01',
        until='2025-01-01',
    )
    assert result.compiled, result.refused
    sql = result.sql or ''
    assert "DATE_TRUNC('MONTH', customers.signed_up) AS signed_up_month" in sql
    assert "customers.first_order >= CAST('2024-01-01' AS DATE)" in sql
    assert 'customers.signed_up >=' not in sql
    # Two dates and none named: the range has no time property to go on.
    refused = _compile(g, 'customer count', by=['signed_up:month'], since='2024-01-01')
    assert 'several date or time columns' in refused.refused[0].reason


@pytest.mark.parametrize(
    ('dialect', 'date', 'stamp', 'since'),
    [
        (
            'duckdb',
            "DATE_TRUNC('WEEK', customers.first_order)",
            "DATE_TRUNC('WEEK', customers.signed_up)",
            "customers.signed_up >= CAST('2024-01-01' AS TIMESTAMPTZ)",
        ),
        (
            'postgres',
            "DATE_TRUNC('WEEK', customers.first_order)",
            "DATE_TRUNC('WEEK', customers.signed_up)",
            "customers.signed_up >= CAST('2024-01-01' AS TIMESTAMPTZ)",
        ),
        (
            'snowflake',
            "DATE_TRUNC('WEEK', customers.first_order)",
            "DATE_TRUNC('WEEK', customers.signed_up)",
            "customers.signed_up >= CAST('2024-01-01' AS TIMESTAMPTZ)",
        ),
        (
            'bigquery',
            'DATE_TRUNC(customers.first_order, ISOWEEK)',
            'TIMESTAMP_TRUNC(customers.signed_up, ISOWEEK)',
            "customers.signed_up >= CAST('2024-01-01' AS TIMESTAMP)",
        ),
        (
            'tsql',
            'DATETRUNC(ISO_WEEK, customers.first_order)',
            'DATETRUNC(ISO_WEEK, customers.signed_up)',
            "customers.signed_up >= CAST('2024-01-01' AS DATETIMEOFFSET)",
        ),
    ],
)
def test_weeks_start_on_monday_and_a_timestamp_compares_with_a_timestamp(
    dialect: str, date: str, stamp: str, since: str
) -> None:
    g = _shop()
    g.add_column(g.nodes['model.shop.customers'], 'signed_up', data_type='TIMESTAMP WITH TIME ZONE')
    weekly = _compile(g, 'customer count', by=['first_order:week'], dialect=dialect)
    assert date in (weekly.sql or ''), weekly.sql
    stamped = _compile(
        g, 'customer count', by=['signed_up:week'], time='signed_up', since='2024-01-01', dialect=dialect
    )
    assert stamp in (stamped.sql or ''), stamped.sql
    assert since in (stamped.sql or ''), stamped.sql


def test_what_cannot_be_compiled_is_refused_with_why() -> None:
    g = _shop()
    cases = {
        ('by', 'customer.height'): "customer has no property 'height'",
        ('by', 'nothing'): "no entity this metric reaches has a property 'nothing'",
        ('by', 'name:fortnight'): "'fortnight' is no time grain",
        ('where', 'drop table orders'): 'is not one SQL condition',
        ('where', 'amount'): 'is not one SQL condition',
        ('where', 'not amount'): 'is not one SQL condition',
        ('where', 'amount > (select 1)'): 'is not a condition on properties',
        ('where', 'sum(amount) > 1'): 'is not a condition on properties',
        ('since', '2024-13-01'): 'is not a date',
        ('dialect', 'klingon'): 'no SQL dialect',
    }
    for (option, value), reason in cases.items():
        if option in ('by', 'where'):
            result = _compile(g, 'revenue', **{option: [value]})
        else:
            result = _compile(g, 'revenue', **{option: value})
        assert not result.compiled, (option, value)
        assert reason in result.refused[0].reason, (option, value, result.refused)
    with pytest.raises(QueryError, match='no metric called'):
        _compile(g, 'no such metric at all')


def test_a_time_grain_and_range_need_one_time_property() -> None:
    g = _shop()
    result = _compile(g, 'customer count', by=['month'], since='2024-01-01', until='2025-01-01')
    assert result.compiled, result.refused
    sql = result.sql or ''
    assert "DATE_TRUNC('MONTH', customers.first_order) AS first_order_month" in sql
    assert "customers.first_order >= CAST('2024-01-01' AS DATE)" in sql
    assert "customers.first_order < CAST('2025-01-01' AS DATE)" in sql
    # orders has no date column, so a range on its metric needs the time property named.
    refused = _compile(g, 'revenue', since='2024-01-01')
    assert 'has no date or time column' in refused.refused[0].reason
    named = _compile(g, 'revenue', since='2024-01-01', time='customer.first_order')
    assert "customers.first_order >= CAST('2024-01-01' AS DATE)" in (named.sql or '')


def test_an_average_is_refused_across_its_grain() -> None:
    g = _shop()
    customers = g.nodes['model.shop.customers']
    spend = g.add_column(customers, 'average_spend', data_type='DOUBLE')
    spend.attrs['expression'] = 'AVG(db.main.payments.amount)'
    g.add_edge(spend.id, 'model.shop.payments#amount', 'derived_from', transform='transformed')
    result = _compile(g, 'customers.average_spend', by=['name'])
    assert not result.compiled
    assert 'averages do not add up across customers' in result.refused[0].reason
    _not_null(g, 'model.shop.customers#customer_id')
    assert _compile(g, 'customers.average_spend', by=['customer']).compiled


# The example project


@pytest.fixture(scope='module')
def project(jaffle_built_graph: Path) -> Project:
    return Project(jaffle_built_graph)


@pytest.fixture(scope='module')
def warehouse(jaffle_built_graph: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """The test's own read-only connection to the example's DuckDB warehouse. Konstruct never runs
    the SQL it compiles; the test does, to check the numbers."""
    con = duckdb.connect(str(jaffle_built_graph / 'jaffle_shop.duckdb'), read_only=True)
    yield con
    con.close()


def _value(compiled: Compiled) -> exp.Expr:
    """The compiled metric's expression, its columns named by their warehouse relations."""
    tree = sqlglot.parse_one(compiled.sql or '', read='duckdb')
    assert isinstance(tree, exp.Select)
    tables = {t.alias_or_name: t for t in tree.find_all(exp.Table)}
    value: exp.Expr = tree.selects[-1].this.copy()
    for column in value.find_all(exp.Column):
        table = tables[column.table]
        column.set('table', table.this.copy())
        column.set('db', table.args['db'].copy())
        column.set('catalog', table.args['catalog'].copy())
    return value


@pytest.mark.dbt
def test_lifetime_value_by_customer_is_the_models_own_column(project: Project) -> None:
    result = project.compile_metric('lifetime value', by=['customer'])
    assert (result.metric, result.relation, result.aggregation) == (
        'Customer lifetime value',
        'customers',
        'none',
    )
    checker = Checker(project.graph)
    column = project.graph.resolve('customers.customer_lifetime_value')[0]
    compiled = checker.form(_value(result).sql(dialect='duckdb'))
    model = checker.form(None, column)
    # In canonical form, composed down to source, the compiled SQL is the model's own formula.
    assert compiled.formula == model.formula == 'SUM(jaffle_shop.main.raw_payments.amount / 100)'
    assert compiled.sources == model.sources
    assert result.confidence == 'declared' and result.flags == []


@pytest.mark.dbt
def test_compiled_sql_adds_up_to_the_models_column(
    project: Project, warehouse: duckdb.DuckDBPyConnection
) -> None:
    def total(sql: str) -> Decimal:
        return Decimal(str(warehouse.execute(sql).fetchall()[0][0]))

    def summed(result: Compiled, value: str) -> Decimal:
        rows = warehouse.execute(result.sql or '').fetchall()
        assert rows
        return Decimal(str(sum(r[-1] or 0 for r in rows)))

    lifetime = total('select sum(customer_lifetime_value) from main.customers')
    for by in ([], ['customer'], ['first_name'], ['customer.last_name']):
        result = project.compile_metric('lifetime value', by=by)
        assert result.compiled, result.refused
        assert summed(result, 'customer_lifetime_value') == lifetime, by
    amount = total('select sum(amount) from main.orders')
    for by in (['customer.first_name', 'month'], ['status'], ['customer']):
        result = project.compile_metric('order amount', by=by)
        assert result.compiled, result.refused
        assert summed(result, 'order_amount') == amount, by
    # Payments reach customers through orders: each payment counts once.
    paid = total('select sum(amount) from main.stg_payments')
    result = project.compile_metric('payment amount', by=['customer', 'order.status'])
    assert result.compiled and summed(result, 'payment_amount') == paid
    completed = total("select sum(amount) from main.orders where status = 'completed'")
    result = project.compile_metric('order amount', where=["status = 'completed'"], by=['customer'])
    assert summed(result, 'order_amount') == completed


@pytest.fixture(scope='module')
def filtered_jaffle(jaffle_built_graph: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The example with an approved term that requires a filter: completed orders only."""
    project = tmp_path_factory.mktemp('filtered') / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph, project)
    definitions = project / 'definitions.yml'
    definitions.write_text(
        definitions.read_text()
        + """
  - name: Completed order amount
    definition: The amount of the orders customers received and kept.
    owner: {name: Priya Shah, role: Head of Finance}
    status: approved
    approved: 2026-11-01
    grain: one row per order
    columns: [orders.amount]
    filters: ["orders.status = 'completed'"]
"""
    )
    shutil.rmtree(project / 'konstruct-out')
    Project(project).build(run_dbt=False)
    return project


@pytest.mark.dbt
def test_an_approved_terms_filter_always_appears(filtered_jaffle: Path) -> None:
    project = Project(filtered_jaffle)
    con = duckdb.connect(str(filtered_jaffle / 'jaffle_shop.duckdb'), read_only=True)
    try:
        [(completed,)] = con.execute(
            "select sum(amount) from main.orders where status = 'completed'"
        ).fetchall()
        for by in ([], ['customer'], ['order'], ['customer.first_name', 'month'], ['status']):
            result = project.compile_metric('completed order amount', by=by)
            assert result.compiled, result.refused
            assert "orders.status = 'completed'" in (result.sql or ''), by
            [required] = result.filters
            assert (required.filter, required.by) == ("orders.status = 'completed'", 'query')
            assert check_sql(project.graph, result.sql or '').passed, by
            rows = con.execute(result.sql or '').fetchall()
            assert sum(r[-1] or 0 for r in rows) == completed, by
    finally:
        con.close()
    # Without it, check-sql finds the approved definition broken: the filter is what makes it right.
    result = project.compile_metric('completed order amount', by=['customer'])
    unfiltered = (result.sql or '').replace("WHERE\n  orders.status = 'completed'\n", '')
    assert 'missing-filter' in {f.rule for f in check_sql(project.graph, unfiltered).findings}


def test_a_filter_the_columns_own_sql_applies_is_said_so(tmp_path: Path) -> None:
    make_shop(tmp_path)
    result = Project(tmp_path).compile_metric('net revenue', by=['region'])
    assert result.compiled, result.refused
    [required] = result.filters
    assert required.applied is None
    assert required.by == 'customer_revenue (mart/customer_revenue.sql)'
    assert 'returned' not in (result.sql or '')
    assert check_sql(Project(tmp_path).graph, result.sql or '').passed
    # customer_revenue states its grain but holds no key of customer: the sum says so.
    assert any('grain customer_revenue: one row per customer is only inferred' in f for f in result.flags)
    # That the model's SQL applies the filter is Konstruct's reading of it: inferred, and flagged.
    [applied] = [f for f in result.facts if f.kind == 'filter']
    assert applied.confidence == 'inferred'
    assert any(f.startswith("the filter orders.status <> 'returned' is only inferred") for f in result.flags)


def test_a_filter_in_a_cte_beside_the_columns_is_not_applied(tmp_path: Path) -> None:
    """Net revenue computed in a CTE beside the one that leaves returns out: the filter does not
    hold on its rows, so the query is refused rather than reporting it applied."""
    sibling_shop(tmp_path)
    result = Project(tmp_path).compile_metric('net revenue', by=['region'])
    assert not result.compiled and result.filters == []
    assert result.refused[0].item == "filter orders.status <> 'returned'"
    # The test fills the warehouse and builds the model to show what the SQL would have said.
    con = duckdb.connect(str(tmp_path / 'shop.duckdb'))
    try:
        shop_rows(con)
        con.execute('create schema mart')
        con.execute(SIBLING_REVENUE)
        [(summed, net)] = con.execute(
            'select (select sum(net_revenue) from mart.customer_revenue), '
            "(select sum(amount) from raw.orders where status <> 'returned')"
        ).fetchall()
    finally:
        con.close()
    assert summed != net


@pytest.mark.parametrize('models', list(MISSED_SHAPES.values()), ids=list(MISSED_SHAPES))
def test_a_filter_that_does_not_hold_on_every_row_is_not_applied(
    tmp_path: Path, models: dict[str, str]
) -> None:
    revenue_shop(tmp_path, models)
    result = Project(tmp_path).compile_metric('net revenue', by=['region'])
    # Refused, whether for the filter or for the value it copies across the join: never applied.
    assert not result.compiled and result.filters == []


def test_a_condition_on_a_joined_property_leaves_out_the_rows_that_find_no_match(tmp_path: Path) -> None:
    """Payments of an order whose customer is missing reach no customer row: the LEFT JOIN gives
    them a NULL region, which ``region <> 'North'`` is not true of, so they are left out, and the
    result says so. A condition on the metric's own relation leaves nothing out that way."""
    make_shop(tmp_path)
    project = Project(tmp_path)
    result = project.compile_metric('paid amount', where=["customer.region <> 'North'"])
    assert result.compiled, result.refused
    [flag] = [f for f in result.flags if 'left out' in f]
    assert flag.startswith("customer.region <> 'North' is on customers, joined to the metric's rows")
    con = duckdb.connect(str(tmp_path / 'shop.duckdb'))
    try:
        shop_rows(con)
        # Two payments of an order whose customer the shop does not have.
        con.execute("insert into raw.orders values (99, 404, 100, 'completed', '2026-01-01')")
        con.execute("insert into raw.payments values (990, 99, 60, 'card'), (991, 99, 40, 'cash')")
        con.execute('create schema mart')
        con.execute(ORDER_PAYMENTS)
        [(compiled,)] = con.execute(result.sql or '').fetchall()
        # Allowing the missing value keeps them, as the guide says.
        kept = project.compile_metric(
            'paid amount', where=["customer.region <> 'North' OR customer.region IS NULL"]
        )
        [(with_missing,)] = con.execute(kept.sql or '').fetchall()
        assert not [f for f in kept.flags if 'left out' in f], kept.flags
        [(matched,)] = con.execute(
            'select sum(p.paid) from mart.order_payments p join raw.orders o on o.order_id = p.order_id '
            "join raw.customers c on c.customer_id = o.customer_id where c.region <> 'North'"
        ).fetchall()
    finally:
        con.close()
    assert compiled == matched and with_missing == matched + 100
    own = project.compile_metric('paid amount', where=['paid > 0'])
    assert own.compiled and not [f for f in own.flags if 'left out' in f], own.flags
    missing = project.compile_metric('paid amount', where=['customer.region IS NULL'])
    assert missing.compiled and not [f for f in missing.flags if 'left out' in f], missing.flags


def _left_out(result: Compiled) -> list[str]:
    return [f for f in result.flags if 'left out' in f]


def test_a_required_filter_or_a_range_on_a_joined_property_says_it_leaves_rows_out(tmp_path: Path) -> None:
    """A filter the metric's definition requires on a joined relation, and a range on a joined time
    property, are in the WHERE clause as a where is: each leaves out the rows that find no match."""
    from .test_checksql import DEFINITIONS

    definitions = DEFINITIONS.replace(
        '    columns: [order_payments.paid]\n',
        '    columns: [order_payments.paid]\n    filters: ["customers.region <> \'North\'"]\n',
    )
    make_shop(tmp_path, definitions=definitions)
    required = Project(tmp_path).compile_metric('paid amount')
    assert required.compiled, required.refused
    [flag] = _left_out(required)
    assert flag.startswith("customers.region <> 'North' is on customers")
    (tmp_path / 'ranged').mkdir()
    make_shop(tmp_path / 'ranged')
    ranged = Project(tmp_path / 'ranged').compile_metric(
        'paid amount', time='order.order_date', since='2026-01-01'
    )
    assert ranged.compiled, ranged.refused
    [flag] = _left_out(ranged)
    assert flag.startswith('since 2026-01-01 is on orders')


def test_a_relation_grouped_by_its_grain_joins_that_entity_on_its_key(tmp_path: Path) -> None:
    make_shop(tmp_path)
    project = Project(tmp_path)
    # order_payments groups payments by order: each group joins one order, and so one customer.
    result = project.compile_metric('paid amount', by=['customer.region'])
    assert result.compiled, result.refused
    assert 'ON orders.order_id = order_payments.order_id' in (result.sql or '')
    assert [(h.start, h.end) for h in result.path] == [('order', 'customer')]
    # The test fills the warehouse and builds the models itself, then runs the compiled SQL.
    con = duckdb.connect(str(tmp_path / 'shop.duckdb'))
    try:
        shop_rows(con)
        con.execute('create schema mart')
        con.execute(ORDER_PAYMENTS)
        rows = con.execute(result.sql or '').fetchall()
        [(paid,)] = con.execute('select sum(paid) from mart.order_payments').fetchall()
    finally:
        con.close()
    assert paid and sum(r[-1] or 0 for r in rows) == paid
    assert project.compile_metric('paid amount', by=['order']).aggregation == 'sum'


ORDER_FACTS = """\
create table mart.order_facts as
select o.order_id, o.customer_id, o.status, o.amount, r.net_revenue as customer_net
from raw.orders o
join mart.customer_revenue r on r.customer_id = o.customer_id
"""
CARRIED_NET = """\
create table mart.carried_net as
select o.order_id, o.customer_id, o.status, o.amount, r.net_revenue as carried_net
from raw.orders o
right join mart.customer_revenue r on r.customer_id = o.customer_id
"""
TRAPS = """\
  - name: Customer net
    kind: measure
    definition: A customer's net revenue, carried on each of their orders.
    status: approved
    owner: {name: Ana Ruiz, role: Finance}
    approved: 2026-10-01
    grain: one row per customer
    columns: [order_facts.customer_net]
  - name: Order facts amount
    kind: measure
    definition: The order amount, carried on order_facts.
    status: approved
    owner: {name: Ana Ruiz, role: Finance}
    approved: 2026-10-01
    columns: [order_facts.amount]
  - name: Sibling net revenue
    kind: measure
    definition: Net revenue as the sibling model computes it, in a CTE beside the filtered one.
    status: approved
    owner: {name: Ana Ruiz, role: Finance}
    approved: 2026-10-01
    columns: [sibling_revenue.net_revenue]
    filters: ["orders.status <> 'returned'"]
  - name: Carried net
    kind: measure
    definition: A customer's net revenue, carried on each of their orders by an outer join.
    status: approved
    owner: {name: Ana Ruiz, role: Finance}
    approved: 2026-10-01
    columns: [carried_net.carried_net]
"""


@pytest.fixture(scope='module')
def traps(tmp_path_factory: pytest.TempPathFactory) -> tuple[Project, Path]:
    """The check-sql shop with the cases that make a compiled number wrong: a per-customer value
    carried on every order row (fan-in), a required filter in a CTE beside the measure's, and a
    unique test on some rows only; and the warehouse filled and every model built by the test."""
    from .test_checksql import CUSTOMER_REVENUE, DEFINITIONS

    root = tmp_path_factory.mktemp('traps')
    make_shop(root, definitions=DEFINITIONS + TRAPS)
    sibling = SIBLING_REVENUE.replace('mart.customer_revenue', 'mart.sibling_revenue')
    (root / 'mart' / 'sibling_revenue.sql').write_text(sibling)
    (root / 'mart' / 'order_facts.sql').write_text(ORDER_FACTS)
    (root / 'mart' / 'carried_net.sql').write_text(CARRIED_NET)
    project = Project(root)
    project.build(run_dbt=False)
    con = duckdb.connect(str(root / 'shop.duckdb'))
    try:
        shop_rows(con)
        con.execute('create schema mart')
        for sql in (CUSTOMER_REVENUE, ORDER_PAYMENTS, sibling, ORDER_FACTS, CARRIED_NET):
            con.execute(sql)
    finally:
        con.close()
    return project, root / 'shop.duckdb'


def _trapped_shop() -> Graph:
    """The small shop with a dimension unique only where is_current, and a per-customer value
    carried on each order row."""
    g = _shop()
    versions = _relation(g, 'customer_versions', ['customer_id', 'segment', 'is_current'])
    test = g.add_node(
        Node('test.shop.unique_versions', 'test', 'unique', attrs={'test': 'unique', 'where': 'is_current'})
    )
    g.add_edge(test.id, f'{versions.id}#customer_id', 'tests')
    orders = g.nodes['model.shop.orders']
    orders.attrs['conditions'] = [
        *orders.attrs['conditions'],
        'LEFT JOIN ON: db.main.orders.customer_id = db.main.customer_versions.customer_id',
    ]
    copy = g.add_column(orders, 'customer_lifetime', data_type='DOUBLE')
    copy.attrs['expression'] = 'db.main.customers.lifetime'
    g.add_edge(copy.id, 'model.shop.customers#lifetime', 'derived_from', transform='direct')
    _term(g, 'Customer value', [copy.id], kind='measure', status='approved')
    return g


@settings(max_examples=60, deadline=None)
@given(
    st.sampled_from(['revenue', 'customer value', 'customer count', 'order count', 'customer lifetime']),
    st.lists(
        st.sampled_from(
            ['customer', 'customer.name', 'segment', 'customer_versions.segment', 'order', 'name']
        ),
        max_size=2,
        unique=True,
    ),
    st.lists(st.sampled_from(["segment = 'a'", "customer.name <> 'x'", 'is_current']), max_size=1),
)
def test_no_compiled_query_joins_a_key_unique_on_some_rows_or_sums_a_repeated_value(
    metric: str, by: list[str], where: list[str]
) -> None:
    result = _compile(_trapped_shop(), metric, by=by, where=where)
    sql = result.sql or ''
    assert 'customer_versions' not in sql, sql
    assert 'orders.customer_lifetime' not in sql, sql
    if result.compiled:
        assert all(h.cardinality in ('many-to-one', 'one-to-one') for h in result.path)
    if where == ['is_current']:
        assert not result.compiled


_TRAP_METRICS = (
    'net revenue',
    'sibling net revenue',
    'customer net',
    'carried net',
    'order facts amount',
    'paid amount',
    'customer count',
    'order count',
    'payment count',
)
_TRAP_BY = ('customer', 'customer.region', 'region', 'order', 'order.status', 'status', 'method', 'payment')
_TRAP_WHERE = (
    "status = 'completed'",
    "customer.region <> 'North'",
    "method in ('card', 'cash')",
    'amount > 10',
)


def test_every_compiled_trap_query_counts_each_row_once(traps: tuple[Project, Path]) -> None:
    """Over generated queries on the traps shop, a compiled query passes check-sql with no error,
    and its total does not depend on what it groups by: a join that repeated rows, or a value
    repeated from a coarser grain, would change it. Without conditions, the total is the model
    column's own. The traps themselves never compile to a sum."""
    project, warehouse = traps
    checker = Checker(project.graph)
    con = duckdb.connect(str(warehouse), read_only=True)
    truth = {
        'net_revenue': 'select sum(net_revenue) from mart.customer_revenue',
        'paid_amount': 'select sum(paid) from mart.order_payments',
        'order_facts_amount': 'select sum(amount) from mart.order_facts',
    }
    seen = {'compiled': 0, 'refused': 0}

    def total(result: Compiled) -> Decimal:
        return Decimal(str(sum((r[-1] or 0) for r in con.execute(result.sql or '').fetchall())))

    @settings(max_examples=80, deadline=None)
    @given(
        st.sampled_from(_TRAP_METRICS),
        st.lists(st.sampled_from(_TRAP_BY), max_size=2, unique=True),
        st.lists(st.sampled_from(_TRAP_WHERE), max_size=2, unique=True),
    )
    def check(metric: str, by: list[str], where: list[str]) -> None:
        result = project.compile_metric(metric, by=by, where=where)
        if metric in ('customer net', 'sibling net revenue', 'carried net'):
            assert not result.compiled, (metric, by, where, result.sql)
        if not result.compiled:
            seen['refused'] += 1
            return
        seen['compiled'] += 1
        found = checker.check(result.sql or '')
        assert found.checked and found.count('error') == 0, (metric, by, where, found.findings)
        if result.aggregation == 'formula':
            return  # a distinct count does not add up across groups
        whole = project.compile_metric(metric, where=where)
        if whole.compiled:
            assert total(result) == total(whole), (metric, by, where)
        alias = re.sub(r'\W+', '_', metric).strip('_')
        if not where and alias in truth:
            [(expected,)] = con.execute(truth[alias]).fetchall()
            assert total(result) == Decimal(str(expected)), (metric, by)

    try:
        check()
    finally:
        con.close()
    assert seen['compiled'] and seen['refused']


_METRICS = (
    'lifetime value',
    'order amount',
    'payment amount',
    'number of orders',
    'customer count',
    'order count',
    'credit card amount',
)
_BY = (
    'customer',
    'order',
    'payment',
    'customer.first_name',
    'customer.last_name',
    'order.status',
    'status',
    'payment_method',
    'first_name',
    'month',
    'order_date:year',
)
_WHERE = (
    "customer.first_name <> 'Michael'",
    "status = 'completed'",
    "order.status in ('returned', 'shipped')",
    "payment_method = 'coupon'",
    'customer.customer_id > 10',
)


@pytest.mark.dbt
def test_every_compiled_query_passes_check_sql_and_counts_each_row_once(
    project: Project, warehouse: duckdb.DuckDBPyConnection
) -> None:
    graph = project.graph
    checker = Checker(graph)
    totals = {
        'payment_amount': 'select sum(amount) from main.stg_payments',
        'order_amount': 'select sum(amount) from main.orders',
        'customer_lifetime_value': 'select sum(customer_lifetime_value) from main.customers',
        'number_of_orders': 'select sum(number_of_orders) from main.customers',
        'credit_card_amount': 'select sum(credit_card_amount) from main.orders',
    }
    seen: dict[str, int] = {'compiled': 0, 'refused': 0}

    @settings(max_examples=60, deadline=None)
    @given(
        st.sampled_from(_METRICS),
        st.lists(st.sampled_from(_BY), max_size=3, unique=True),
        st.lists(st.sampled_from(_WHERE), max_size=2, unique=True),
    )
    def check(metric: str, by: list[str], where: list[str]) -> None:
        result = project.compile_metric(metric, by=by, where=where)
        if not result.compiled:
            seen['refused'] += 1
            assert result.refused and all(r.reason for r in result.refused)
            return
        seen['compiled'] += 1
        found = checker.check(result.sql or '')
        assert found.checked and found.count('error') == 0, (metric, by, where, found.findings)
        rows = warehouse.execute(result.sql or '').fetchall()
        tree = sqlglot.parse_one(result.sql or '', read='duckdb')
        assert isinstance(tree, exp.Select)
        alias = tree.selects[-1].alias
        if not where and alias in totals and result.aggregation != 'formula':
            [(expected,)] = warehouse.execute(totals[alias]).fetchall()
            assert Decimal(str(sum(r[-1] or 0 for r in rows))) == Decimal(str(expected)), (metric, by)

    check()
    assert seen['compiled'] and seen['refused']


@pytest.mark.dbt
@pytest.mark.parametrize('dialect', ['duckdb', 'snowflake', 'bigquery'])
def test_one_query_in_each_dialect(
    project: Project, warehouse: duckdb.DuckDBPyConnection, dialect: str
) -> None:
    result = project.compile_metric(
        'order amount', by=['customer.first_name', 'month'], since='2018-01-15', dialect=dialect
    )
    assert result.compiled and result.dialect == dialect
    sql = result.sql or ''
    trunc = {
        'duckdb': "DATE_TRUNC('MONTH', orders.order_date)",
        'snowflake': "DATE_TRUNC('MONTH', orders.order_date)",
        'bigquery': 'DATE_TRUNC(orders.order_date, MONTH)',
    }[dialect]
    assert trunc in sql
    # Read back in its dialect and run on DuckDB, it gives what the DuckDB query gives.
    tree = sqlglot.parse_one(sql, read=dialect)
    rows = warehouse.execute(tree.sql(dialect='duckdb')).fetchall()
    same = project.compile_metric('order amount', by=['customer.first_name', 'month'], since='2018-01-15')
    assert sorted(rows, key=str) == sorted(warehouse.execute(same.sql or '').fetchall(), key=str)
    assert rows


@pytest.fixture(scope='module')
def undefined(jaffle_built_graph: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The example built without its definitions.yml: only the code to go on."""
    project = tmp_path_factory.mktemp('undefined') / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph, project)
    (project / 'definitions.yml').unlink()
    shutil.rmtree(project / 'konstruct-out')
    Project(project).build(run_dbt=False)
    return project


@pytest.mark.dbt
def test_without_definitions_a_proposed_metric_compiles_flagged(
    undefined: Path, warehouse: duckdb.DuckDBPyConnection
) -> None:
    result = Project(undefined).compile_metric('customer lifetime value', by=['first_name'])
    assert result.compiled, result.refused
    assert result.confidence == 'inferred'
    metric = next(f for f in result.facts if f.kind == 'metric')
    assert metric.confidence == 'inferred'
    assert 'proposed from the code: no one has defined or approved it' in metric.evidence
    assert result.flags[0].startswith('the metric customer lifetime value is only inferred')
    rows = warehouse.execute(result.sql or '').fetchall()
    [(expected,)] = warehouse.execute('select sum(customer_lifetime_value) from main.customers').fetchall()
    assert sum(r[-1] or 0 for r in rows) == expected
    text = str(result)
    assert '  confidence  inferred\n' in text and 'only inferred' in text
