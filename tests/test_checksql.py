"""konstruct check-sql: ad-hoc SQL checked against the project's definitions and ontology."""

from __future__ import annotations

import ast
import itertools
import json
import sys
import time
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from konstruct import Project
from konstruct.analysis.checksql import (
    RULES,
    Adoption,
    Checker,
    Finding,
    HistoryCheck,
    SqlCheck,
    check_history,
    check_sql,
    pseudonym,
    reachable,
)
from konstruct.core.graph import Graph
from konstruct.inputs.usage import HistoryQuery, QueryHistory, UsageSettings, fetch_history

WAREHOUSE = """
create schema raw;
create table raw.customers (customer_id int primary key, name varchar, region varchar);
create table raw.orders (
    order_id int primary key, customer_id int, amount decimal(10, 2), status varchar, order_date date
);
create table raw.payments (payment_id int primary key, order_id int, amount decimal(10, 2), method varchar);
"""
CUSTOMER_REVENUE = """\
create table mart.customer_revenue as
select c.customer_id, c.region, sum(o.amount) as net_revenue, sum(o.amount) as gross_revenue
from raw.customers c
join raw.orders o on o.customer_id = c.customer_id
where o.status <> 'returned'
group by c.customer_id, c.region
"""
ORDER_PAYMENTS = """\
create table mart.order_payments as
select o.order_id, sum(p.amount) as paid
from raw.orders o
join raw.payments p on p.order_id = o.order_id
group by o.order_id
"""
DEFINITIONS = """\
version: 1
terms:
  - name: Net revenue
    kind: measure
    synonyms: [revenue]
    definition: Order value from orders that were not returned.
    owner: {name: Ana Ruiz, role: Finance}
    status: approved
    approved: 2026-10-01
    grain: one row per customer
    columns: [customer_revenue.net_revenue]
    filters: ["orders.status <> 'returned'"]
  - name: Gross revenue
    kind: measure
    definition: Order value before returns; replaced by net revenue.
    status: retired
    columns: [customer_revenue.gross_revenue]
    related: [Net revenue]
  - name: Paid amount
    kind: measure
    definition: What customers paid against an order.
    status: draft
    columns: [order_payments.paid]
  - name: Order amount
    kind: measure
    synonyms: [order value, order total]
    definition: What the customer was charged for an order.
    owner: {name: Ana Ruiz, role: Finance}
    status: approved
    approved: 2026-10-01
    columns: [orders.amount]
"""


def make_shop(root: Path, *, definitions: str | None = DEFINITIONS, dialect: str | None = None) -> Graph:
    con = duckdb.connect(str(root / 'shop.duckdb'))
    con.execute(WAREHOUSE)
    con.close()
    (root / 'mart').mkdir()
    (root / 'mart' / 'customer_revenue.sql').write_text(CUSTOMER_REVENUE)
    (root / 'mart' / 'order_payments.sql').write_text(ORDER_PAYMENTS)
    if definitions:
        (root / 'definitions.yml').write_text(definitions)
    return Project(root).build(run_dbt=False, dialect=dialect).graph


BRIDGE_WAREHOUSE = """
create schema raw;
create table raw.customers (customer_id int primary key, name varchar, country varchar, signup_date date);
create table raw.segments (segment_id int primary key, segment_name varchar);
create table raw.customer_segments (customer_id int, segment_id int, primary key (customer_id, segment_id));
create table raw.customer_notes (customer_id int, note varchar);
create table raw.orders (
    order_id int primary key, customer_id int, order_date date, status varchar, channel varchar,
    amount decimal(12, 2), legacy_amount decimal(12, 2)
);
create table raw.order_items (item_id int primary key, order_id int, product_id int, quantity int, price decimal(12, 2));
create table raw.products (product_id int primary key, category varchar, list_price decimal(12, 2));
"""
BRIDGE_MODELS = {
    'order_revenue': """\
create table mart.order_revenue as
select o.order_id, o.customer_id, o.order_date, sum(i.quantity * i.price) as item_revenue
from raw.orders o
join raw.order_items i on i.order_id = o.order_id
where o.status <> 'cancelled'
group by o.order_id, o.customer_id, o.order_date
""",
    'customer_value': """\
create table mart.customer_value as
select c.customer_id, c.country, sum(o.amount) as completed_revenue, count(*) as completed_orders
from raw.customers c
join raw.orders o on o.customer_id = c.customer_id
where o.status = 'completed' and o.order_date >= date '2020-01-01'
group by c.customer_id, c.country
""",
    'segment_revenue': """\
create table mart.segment_revenue as
select cs.segment_id, sum(o.amount) as segment_revenue
from raw.customer_segments cs
join raw.orders o on o.customer_id = cs.customer_id
where o.status = 'completed'
group by cs.segment_id
""",
}
BRIDGE_DEFINITIONS = """\
version: 1
terms:
  - name: Completed revenue
    kind: measure
    synonyms: [booked revenue]
    definition: Order value of completed orders since 2020.
    owner: {name: Ana Ruiz, role: Finance}
    status: approved
    approved: 2026-09-01
    columns: [customer_value.completed_revenue]
    filters: ["orders.status = 'completed'", "orders.order_date >= '2020-01-01'"]
  - name: Item revenue
    kind: measure
    definition: Quantity times price of every item of an order that was not cancelled.
    owner: {name: Ana Ruiz, role: Finance}
    status: approved
    approved: 2026-09-01
    columns: [order_revenue.item_revenue]
    filters: ["orders.status <> 'cancelled'"]
  - name: Order amount
    kind: measure
    definition: What the customer was charged for an order.
    status: agreed
    columns: [orders.amount]
  - name: Legacy amount
    definition: The order amount of the old billing system.
    status: retired
    columns: [orders.legacy_amount]
    related: [Order amount]
"""


def make_bridge(root: Path, *, dialect: str | None = None) -> Graph:
    """A shop whose customers belong to segments through a bridge table with a composite key."""
    con = duckdb.connect(str(root / 'bridge.duckdb'))
    con.execute(BRIDGE_WAREHOUSE)
    con.close()
    (root / 'mart').mkdir()
    for name, sql in BRIDGE_MODELS.items():
        (root / 'mart' / f'{name}.sql').write_text(sql)
    (root / 'definitions.yml').write_text(BRIDGE_DEFINITIONS)
    return Project(root).build(run_dbt=False, dialect=dialect).graph


@pytest.fixture(scope='module')
def shop(tmp_path_factory: pytest.TempPathFactory) -> Checker:
    return Checker(make_shop(tmp_path_factory.mktemp('shop')))


@pytest.fixture(scope='module')
def bare(tmp_path_factory: pytest.TempPathFactory) -> Checker:
    """The same shop without definitions.yml: only what Konstruct derives."""
    return Checker(make_shop(tmp_path_factory.mktemp('bare'), definitions=None))


@pytest.fixture(scope='module')
def bridge(tmp_path_factory: pytest.TempPathFactory) -> Checker:
    return Checker(make_bridge(tmp_path_factory.mktemp('bridge')))


@pytest.fixture(scope='module')
def dialects(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Checker]:
    """The shop read as Snowflake and as BigQuery SQL."""
    return {
        dialect: Checker(make_shop(tmp_path_factory.mktemp(f'shop_{dialect}'), dialect=dialect))
        for dialect in ('snowflake', 'bigquery')
    }


def only(result: SqlCheck, rule: str) -> Finding:
    found = [f for f in result.findings if f.rule == rule]
    assert len(found) == 1, str(result)
    return found[0]


def rules(result: SqlCheck) -> list[str]:
    return sorted(f.rule for f in result.findings)


def loud(result: SqlCheck) -> list[str]:
    return sorted(f.rule for f in result.findings if f.severity != 'info')


def flat(sql: str) -> str:
    return ' '.join(sql.split())


# Data to run a query and its correction on, so a correction can be seen to change its measure and
# nothing else. Customers 11 and 12 have only returned orders, and order 9 only cancelled items.


def shop_rows(con: duckdb.DuckDBPyConnection) -> None:
    statuses = ['completed', 'returned', 'shipped']
    for c in range(1, 13):
        con.execute(
            'insert into raw.customers values (?, ?, ?)',
            [c, f'customer {c}', ['North', 'South', 'East'][c % 3]],
        )
    payment = 1
    for o in range(1, 61):
        customer = 11 + o % 2 if o > 50 else o % 10 + 1
        status = 'returned' if o > 50 else statuses[o % 3]
        date = f'2026-{o % 12 + 1:02d}-{o % 27 + 1:02d}'
        con.execute('insert into raw.orders values (?, ?, ?, ?, ?)', [o, customer, o * 1.5, status, date])
        for p in range(o % 3):
            con.execute(
                'insert into raw.payments values (?, ?, ?, ?)',
                [payment, o, o / 2, ['card', 'coupon', 'cash'][p]],
            )
            payment += 1


def bridge_rows(con: duckdb.DuckDBPyConnection) -> None:
    for c in range(1, 9):
        con.execute(
            'insert into raw.customers values (?, ?, ?, ?)', [c, f'c{c}', ['UK', 'ES'][c % 2], '2019-06-01']
        )
        for segment in range(1, c % 3 + 2):
            con.execute('insert into raw.customer_segments values (?, ?)', [c, segment])
    for segment in range(1, 4):
        con.execute('insert into raw.segments values (?, ?)', [segment, f'segment {segment}'])
    for product in range(1, 5):
        con.execute(
            'insert into raw.products values (?, ?, ?)',
            [product, ['toys', 'books'][product % 2], product * 3.0],
        )
    item = 1
    for o in range(1, 41):
        status = ['completed', 'cancelled', 'pending'][o % 3]
        date = f'{[2019, 2020, 2026][o % 3]}-{o % 12 + 1:02d}-01'
        con.execute(
            'insert into raw.orders values (?, ?, ?, ?, ?, ?, ?)',
            [o, o % 8 + 1, date, status, ['web', 'store'][o % 2], o * 2.0, o * 2.0],
        )
        for i in range(o % 3 + 1):
            con.execute(
                'insert into raw.order_items values (?, ?, ?, ?, ?)', [item, o, i + 1, i + 1, o * 0.5]
            )
            item += 1


def database(
    root: Path, warehouse: str, models: list[str], rows: Callable[[duckdb.DuckDBPyConnection], None]
) -> Path:
    path = root / 'data.duckdb'
    con = duckdb.connect(str(path))
    con.execute(warehouse)
    rows(con)
    con.execute('create schema mart')
    for model in models:
        con.execute(model)
    con.close()
    return path


@pytest.fixture(scope='module')
def shop_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return database(
        tmp_path_factory.mktemp('shop_db'), WAREHOUSE, [CUSTOMER_REVENUE, ORDER_PAYMENTS], shop_rows
    )


@pytest.fixture(scope='module')
def bridge_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return database(
        tmp_path_factory.mktemp('bridge_db'), BRIDGE_WAREHOUSE, list(BRIDGE_MODELS.values()), bridge_rows
    )


def runs_alike(db: Path, sql: str, result: SqlCheck, dialect: str | None = None) -> None:
    """The corrected SQL runs on DuckDB, keeps the query's columns, and changes only the columns of
    its findings: no other column's values, and no row of the result, come or go."""
    import sqlglot

    assert result.corrected is not None, str(result)
    if dialect:
        sql = sqlglot.transpile(sql, read=dialect, write='duckdb')[0]
        corrected = ';\n'.join(sqlglot.transpile(result.corrected, read=dialect, write='duckdb'))
    else:
        corrected = result.corrected
    changed = {f.column for f in result.findings if f.rewrite and f.column}
    con = duckdb.connect(str(db), read_only=True)
    try:
        before = con.execute(sql)
        names = [d[0].lower() for d in before.description]
        rows = before.fetchall()
        after = con.execute(corrected)
        assert [d[0].lower() for d in after.description] == names
        fixed = after.fetchall()
    finally:
        con.close()
    kept = [i for i, name in enumerate(names) if name not in changed]
    assert sorted(str([r[i] for i in kept]) for r in rows) == sorted(str([r[i] for i in kept]) for r in fixed)


# Required filters


NET_REVENUE = """
select c.region, sum(o.amount) as net_revenue
from raw.orders o join raw.customers c on c.customer_id = o.customer_id
group by c.region
"""


def test_a_measure_without_its_required_filter_is_an_error_with_the_filter_added(
    shop: Checker, shop_db: Path
) -> None:
    result = shop.check(NET_REVENUE)
    finding = only(result, 'missing-filter')
    assert (finding.severity, finding.column, finding.term) == ('error', 'net_revenue', 'Net revenue')
    assert "orders.status <> 'returned'" in finding.message
    # Grouped by region: a WHERE clause would drop a region whose orders were all returned.
    assert finding.rewrite == "o.status <> 'returned' applied to SUM(o.amount) alone"
    assert not result.passed and result.failed()
    assert result.corrected is not None
    assert "SUM(o.amount) FILTER(WHERE o.status <> 'returned') AS net_revenue" in flat(result.corrected)
    # The corrected SQL computes the measure as its column does, which is only a note.
    assert rules(shop.check(result.corrected)) == ['recomputes-measure']
    runs_alike(shop_db, NET_REVENUE, result)


@pytest.mark.parametrize(
    'condition',
    [
        "o.status <> 'returned'",
        "'returned' != o.status",
        "not o.status = 'returned'",
        "not (o.status in ('returned'))",
        "o.status not in ('returned')",
    ],
)
def test_a_required_filter_counts_however_it_is_written(shop: Checker, condition: str) -> None:
    sql = NET_REVENUE.replace('group by', f'where {condition}\ngroup by')
    assert rules(shop.check(sql)) == ['recomputes-measure']


@pytest.mark.parametrize(
    'condition',
    [
        "o.order_date >= '2020-01-01'",
        "o.order_date >= date '2020-01-01'",
        "'2020-01-01' <= o.order_date",
        "o.order_date >= cast('2020-01-01' as date)",
        "o.order_date >= '2020-01-01'::date",
        "o.order_date >= timestamp '2020-01-01 00:00:00'",
        "o.order_date between '2020-01-01' and '2026-12-31'",
        "o.order_date > '2019-12-31'",
    ],
)
def test_a_date_filter_counts_however_it_is_written(bridge: Checker, condition: str) -> None:
    sql = (
        'select sum(o.amount) as completed_revenue from raw.orders o '
        f"where o.status in ('completed') and {condition}"
    )
    assert loud(bridge.check(sql)) == []


def test_a_different_filter_on_the_same_column_is_a_warning_without_a_rewrite(shop: Checker) -> None:
    result = shop.check(NET_REVENUE.replace('group by', "where o.status = 'completed'\ngroup by"))
    finding = only(result, 'missing-filter')
    assert finding.severity == 'warning' and finding.rewrite is None
    assert "it filters the same column with orders.status = 'completed'" in finding.message
    assert result.passed and result.failed(strict=True) and result.corrected is None


def test_reading_the_approved_column_needs_no_filter_it_already_applies(shop: Checker) -> None:
    result = shop.check('select region, sum(net_revenue) as revenue from mart.customer_revenue group by 1')
    assert result.findings == [] and result.terms == ['Net revenue']
    assert result.summary() == 'No findings in 1 statement.'


def test_a_filter_on_a_relation_the_query_does_not_read_is_advice_only(shop: Checker) -> None:
    result = shop.check('select sum(paid) as net_revenue from mart.order_payments')
    finding = only(result, 'missing-filter')
    assert finding.rewrite is None and result.corrected is None and finding.severity == 'warning'
    assert finding.fix == "Add orders.status <> 'returned' to the query."


def test_the_filter_lands_in_the_subquery_that_reads_the_measure(shop: Checker, shop_db: Path) -> None:
    sql = 'with o as (select customer_id, amount from raw.orders) select sum(amount) as net_revenue from o'
    result = shop.check(sql)
    assert only(result, 'missing-filter').rewrite == "orders.status <> 'returned' added to the WHERE clause"
    assert result.corrected is not None
    assert rules(shop.check(result.corrected)) == ['recomputes-measure']
    runs_alike(shop_db, sql, result)
    # Grouped, the filter cannot go into the CTE, which leaves out the column it needs.
    grouped = (
        'with o as (select customer_id, amount from raw.orders) '
        'select customer_id, sum(amount) as net_revenue from o group by 1'
    )
    assert only(shop.check(grouped), 'missing-filter').rewrite is None


def test_a_filter_goes_into_the_scope_whose_aggregate_produces_the_measure(shop: Checker) -> None:
    sql = (
        'with all_o as (select count(*) as n from raw.orders), '
        'rev as (select sum(amount) as net_revenue from raw.orders) '
        'select all_o.n, rev.net_revenue from all_o cross join rev'
    )
    result = shop.check(sql)
    assert only(result, 'missing-filter').rewrite == "orders.status <> 'returned' added to the WHERE clause"
    assert result.corrected is not None
    before, after = result.corrected.split('rev AS')
    assert "WHERE\n    orders.status <> 'returned'" in after and 'WHERE' not in before


def test_a_filter_that_would_change_another_column_is_written_into_the_measure(shop: Checker) -> None:
    sql = 'select sum(o.amount) as net_revenue, count(*) as orders_placed from raw.orders o'
    result = shop.check(sql)
    finding = only(result, 'missing-filter')
    assert finding.rewrite == "o.status <> 'returned' applied to SUM(o.amount) alone"
    assert result.corrected is not None
    flat = ' '.join(result.corrected.split())
    assert flat == (
        "SELECT SUM(o.amount) FILTER(WHERE o.status <> 'returned') AS net_revenue, "
        'COUNT(*) AS orders_placed FROM raw.orders AS o'
    )
    assert loud(shop.check(result.corrected)) == []


def test_a_left_join_filter_that_would_change_another_count_is_written_into_the_measure(
    shop: Checker,
) -> None:
    sql = (
        'select c.region, sum(o.amount) as net_revenue, count(o.order_id) as orders_placed '
        'from raw.customers c left join raw.orders o on o.customer_id = c.customer_id group by 1'
    )
    result = shop.check(sql)
    assert only(result, 'missing-filter').rewrite == "o.status <> 'returned' applied to SUM(o.amount) alone"
    assert result.corrected is not None
    assert 'ON o.customer_id = c.customer_id\n' in result.corrected
    assert 'COUNT(o.order_id) AS orders_placed' in result.corrected


def test_a_filter_on_a_left_join_with_the_measure_alone_goes_into_its_on_clause(
    shop: Checker, shop_db: Path
) -> None:
    sql = (
        'select sum(o.amount) as net_revenue from raw.customers c '
        'left join raw.orders o on o.customer_id = c.customer_id'
    )
    result = shop.check(sql)
    assert (
        only(result, 'missing-filter').rewrite
        == "o.status <> 'returned' added to the ON clause of the join to o"
    )
    runs_alike(shop_db, sql, result)


def test_a_filter_on_rows_another_query_reads_too_is_advice_only(shop: Checker) -> None:
    sql = (
        'with o as (select customer_id, amount from raw.orders), '
        'a as (select sum(amount) as net_revenue from o), b as (select count(*) as n from o) '
        'select a.net_revenue, b.n from a cross join b'
    )
    result = shop.check(sql)
    finding = only(result, 'missing-filter')
    assert finding.rewrite is None and result.corrected is None


def test_a_filter_is_not_added_to_the_where_clause_of_a_right_or_full_join(
    shop: Checker, shop_db: Path
) -> None:
    sql = (
        'select sum(o.amount) as net_revenue, count(*) as n from raw.orders o '
        'full join raw.customers c on c.customer_id = o.customer_id'
    )
    result = shop.check(sql)
    assert only(result, 'missing-filter').rewrite == "o.status <> 'returned' applied to SUM(o.amount) alone"
    runs_alike(shop_db, sql, result)


@pytest.mark.parametrize(
    'tie',
    [
        "where o.order_id in (select order_id from raw.orders where status <> 'returned')",
        "where exists (select 1 from raw.orders k where k.order_id = o.order_id and k.status <> 'returned')",
        "join (select order_id from raw.orders where status <> 'returned') k on k.order_id = o.order_id",
        "join raw.orders k on k.order_id = o.order_id where k.status <> 'returned'",
    ],
)
def test_a_filter_on_the_same_rows_through_their_key_counts(shop: Checker, tie: str) -> None:
    sql = f'select sum(o.amount) as net_revenue from raw.orders o {tie}'
    assert loud(shop.check(sql)) == []


def test_a_filter_through_a_cte_joined_on_the_key_counts(shop: Checker) -> None:
    sql = (
        "with kept as (select order_id from raw.orders where status <> 'returned') "
        'select sum(o.amount) as net_revenue from raw.orders o join kept using (order_id)'
    )
    assert loud(shop.check(sql)) == []


def test_a_filter_on_other_rows_of_the_same_relation_does_not_count(shop: Checker) -> None:
    exists = (
        'select sum(o.amount) as net_revenue from raw.orders o '
        "where exists (select 1 from raw.orders o2 where o2.status <> 'returned')"
    )
    assert only(shop.check(exists), 'missing-filter').severity == 'error'
    # Tied to the measure's rows in a way the check does not follow: a note, not an error.
    for tie in (
        "where o.order_id not in (select order_id from raw.orders where status = 'returned')",
        "join raw.orders o2 on o2.customer_id = o.customer_id where o2.status <> 'returned'",
    ):
        sql = f'select sum(o.amount) as net_revenue from raw.orders o {tie}'
        assert only(shop.check(sql), 'missing-filter').severity == 'info'
    # The same rows, filtered to the opposite.
    contradiction = (
        'select sum(a.amount) as net_revenue from raw.orders a join raw.orders b on b.order_id = a.order_id '
        "where b.status <> 'returned' and a.status = 'returned'"
    )
    assert only(shop.check(contradiction), 'missing-filter').severity == 'warning'


def test_a_filter_on_a_joined_relation_is_applied(bridge: Checker, bridge_db: Path) -> None:
    sql = (
        'select o.order_date, sum(i.quantity * i.price) as item_revenue from raw.orders o '
        'join raw.order_items i on i.order_id = o.order_id group by 1'
    )
    result = bridge.check(sql)
    assert only(result, 'missing-filter').rewrite == (
        "o.status <> 'cancelled' applied to SUM(i.quantity * i.price) alone"
    )
    assert result.corrected is not None and loud(bridge.check(result.corrected)) == []
    runs_alike(bridge_db, sql, result)


CORRECTIONS = [
    ('select sum(amount) as net_revenue from raw.orders', None),
    ('select customer_id, sum(amount) as net_revenue from raw.orders group by customer_id', None),
    (
        'select c.customer_id, sum(o.amount) as net_revenue from raw.customers c '
        'join raw.orders o on o.customer_id = c.customer_id group by 1',
        None,
    ),
    (
        'select c.customer_id, c.name, sum(o.amount) as net_revenue from raw.customers c '
        'left join raw.orders o on o.customer_id = c.customer_id group by 1, 2',
        None,
    ),
    (
        'with base as (select o.order_id, o.customer_id, o.amount, o.status from raw.orders o) '
        'select customer_id, sum(amount) as net_revenue, count(*) as n from base group by 1',
        None,
    ),
    (
        'select (select count(*) from raw.orders) as orders, (select sum(amount) from raw.orders) as net_revenue',
        None,
    ),
    (
        'select c.region, sum(o.amount) as net_revenue, count(*) as orders_placed from raw.orders o '
        'join raw.customers c on c.customer_id = o.customer_id group by 1',
        'snowflake',
    ),
    (
        'select c.region, sum(o.amount) as net_revenue, count(*) as orders_placed from raw.orders o '
        'join raw.customers c on c.customer_id = o.customer_id group by 1',
        'bigquery',
    ),
    ('select customer_id, sum(amount) as net_revenue from raw.orders group by customer_id', 'snowflake'),
    ('select sum(amount) as net_revenue from raw.orders', 'bigquery'),
    (
        'select region, sum(gross_revenue) as gross_revenue from mart.customer_revenue group by 1 '
        'order by gross_revenue desc',
        None,
    ),
    (
        'select customer_id, gross_revenue from mart.customer_revenue where gross_revenue > 100 '
        'order by gross_revenue',
        None,
    ),
    ('select region, sum(gross_revenue) as total from mart.customer_revenue group by 1', 'snowflake'),
    (
        'with all_o as (select count(*) as n from raw.orders), '
        'rev as (select sum(amount) as net_revenue from raw.orders) '
        'select all_o.n, rev.net_revenue from all_o cross join rev',
        None,
    ),
    ('select sum(o.amount) as net_revenue, count(*) as orders_placed from raw.orders o', None),
    (
        'select c.region, sum(o.amount) as net_revenue, count(o.order_id) as orders_placed '
        'from raw.customers c left join raw.orders o on o.customer_id = c.customer_id group by 1',
        None,
    ),
    ('select region, sum(gross_revenue) as total from mart.customer_revenue group by 1', None),
    ('select r.region, r.gross_revenue from mart.customer_revenue r', None),
]


@pytest.mark.parametrize(('sql', 'dialect'), CORRECTIONS)
def test_every_correction_runs_and_changes_only_what_it_fixes(
    shop: Checker, dialects: dict[str, Checker], shop_db: Path, sql: str, dialect: str | None
) -> None:
    checker = dialects[dialect] if dialect else shop
    result = checker.check(sql)
    runs_alike(shop_db, sql, result, dialect)
    assert loud(checker.check(result.corrected or '')) == []


@pytest.mark.parametrize(
    'sql',
    [
        'select region, sum(o.amount) as net_revenue, rank() over (order by sum(o.amount) desc) as rnk '
        'from raw.orders o join raw.customers c on c.customer_id = o.customer_id group by region',
        'select customer_id, sum(amount) as net_revenue from raw.orders group by 1 '
        'qualify rank() over (order by net_revenue desc) <= 3',
        'select customer_id, sum(amount) as net_revenue from raw.orders group by 1 order by 2 desc limit 3',
        'with t as (select customer_id, sum(amount) as net_revenue from raw.orders group by 1) '
        'select * from t order by net_revenue desc limit 3',
    ],
)
def test_no_correction_where_the_measure_chooses_or_ranks_the_rows(shop: Checker, sql: str) -> None:
    result = shop.check(sql)
    assert only(result, 'missing-filter').rewrite is None and result.corrected is None


@pytest.mark.parametrize(
    'sql',
    [
        # The difference would read the replacement twice.
        'select r.customer_id, r.gross_revenue - r.net_revenue as diff from mart.customer_revenue r',
        'select sum(r.gross_revenue) as total, max(r.net_revenue) as top from mart.customer_revenue r',
    ],
)
def test_a_retired_column_is_not_swapped_for_one_the_select_already_reads(shop: Checker, sql: str) -> None:
    result = shop.check(sql)
    assert only(result, 'retired-column').rewrite is None and result.corrected is None


def test_a_retired_column_passed_on_by_select_star_is_a_note(shop: Checker) -> None:
    finding = only(shop.check('select * from mart.customer_revenue'), 'retired-column')
    assert finding.severity == 'info' and finding.rewrite is None


# Other dialects


@pytest.mark.parametrize(
    ('dialect', 'sql'),
    [
        ('snowflake', "select sum(amount) as net_revenue from raw.orders where status <> 'returned'"),
        (
            'snowflake',
            "select customer_id, sum(iff(status <> 'returned', amount, 0)) as net_revenue from raw.orders group by 1",
        ),
        (
            'snowflake',
            'select region, sum(net_revenue) as net_revenue from mart.customer_revenue group by all',
        ),
        (
            'snowflake',
            "select to_char(order_date, 'YYYY-MM') as month, sum(amount) as net_revenue from raw.orders "
            "where status != 'returned' group by 1",
        ),
        (
            'snowflake',
            'select sum(amount) as net_revenue from raw.orders '
            "where status <> 'returned' and order_date >= '2026-01-01'::date",
        ),
        (
            'bigquery',
            "select customer_id, sum(if(status <> 'returned', amount, 0)) as net_revenue from raw.orders "
            'group by customer_id',
        ),
        (
            'bigquery',
            "select format_date('%Y-%m', order_date) as month, sum(amount) as net_revenue from raw.orders "
            "where status != 'returned' group by month",
        ),
        (
            'bigquery',
            'select region, sum(net_revenue) as net_revenue from `mart.customer_revenue` group by region',
        ),
    ],
)
def test_correct_sql_in_snowflake_and_bigquery_passes(
    dialects: dict[str, Checker], dialect: str, sql: str
) -> None:
    assert loud(dialects[dialect].check(sql)) == []


@pytest.mark.parametrize('dialect', ['snowflake', 'bigquery'])
def test_a_missing_filter_in_snowflake_and_bigquery_is_found_and_added_once(
    dialects: dict[str, Checker], dialect: str
) -> None:
    result = dialects[dialect].check(
        "select sum(amount) as net_revenue from raw.orders where order_date >= '2026-01-01'"
    )
    assert [f.rule for f in result.findings if f.severity == 'error'] == ['missing-filter']
    assert result.corrected is not None and result.corrected.count('returned') == 1


@pytest.mark.parametrize(
    ('dialect', 'condition'),
    [
        ('bigquery', "o.status not in unnest(['returned'])"),
        (None, "o.status not like 'returned'"),
        (None, "o.status not in (values ('returned'))"),
        (None, "not o.status = any(['returned'])"),
    ],
)
def test_a_required_filter_written_as_a_list_or_pattern_counts(
    shop: Checker, dialects: dict[str, Checker], dialect: str | None, condition: str
) -> None:
    checker = dialects[dialect] if dialect else shop
    assert (
        loud(checker.check(f'select sum(o.amount) as net_revenue from raw.orders o where {condition}')) == []
    )


def test_an_extra_exclusion_is_an_error_only_on_a_column_the_definition_filters(shop: Checker) -> None:
    sql = "select sum(o.amount) as net_revenue from raw.orders o where o.status <> 'returned' and {}"
    other = only(shop.check(sql.format('o.customer_id <> 7')), 'formula-differs')
    assert other.severity == 'warning'
    same = only(shop.check(sql.format("o.status <> 'cancelled'")), 'formula-differs')
    assert same.severity == 'error'
    # A filter in place of the required one is the missing filter's finding, not another formula.
    instead = shop.check("select sum(amount) as net_revenue from raw.orders where status not like 'return%'")
    assert rules(instead) == ['missing-filter']


def test_a_window_aggregate_is_a_measure(shop: Checker) -> None:
    window = 'select customer_id, sum(amount) over (partition by customer_id) as net_revenue from raw.orders'
    finding = only(shop.check(window), 'missing-filter')
    assert finding.severity == 'error' and finding.rewrite is None
    fanned = (
        'select o.order_id, sum(o.amount) over (partition by o.customer_id) as total from raw.orders o '
        'join raw.payments p on p.order_id = o.order_id'
    )
    assert only(shop.check(fanned), 'fan-out').column == 'total'
    # Partitioned by the key of the side that repeats, each partition holds one order row.
    by_payment = fanned.replace('partition by o.customer_id', 'partition by p.payment_id')
    assert 'fan-out' not in rules(shop.check(by_payment))


@pytest.mark.parametrize(
    ('dialect', 'sql'),
    [
        (
            None,
            'select o.customer_id, sum(o.amount) as total from raw.orders o cross join unnest([1, 2]) as t(x) group by 1',
        ),
        (
            'bigquery',
            'select o.customer_id, sum(o.amount) as total from raw.orders o cross join unnest([1, 2]) as x group by 1',
        ),
        (
            'snowflake',
            'select o.customer_id, sum(o.amount) as total from raw.orders o, lateral flatten(input => array_construct(1, 2)) f group by 1',
        ),
    ],
)
def test_an_unnested_array_repeats_the_rows_it_is_joined_to(
    shop: Checker, dialects: dict[str, Checker], dialect: str | None, sql: str
) -> None:
    checker = dialects[dialect] if dialect else shop
    assert only(checker.check(sql), 'fan-out').severity == 'warning'


@pytest.mark.parametrize('name', ['orders_placed', 'order_count', 'orders'])
def test_a_count_named_for_a_repeated_source_is_a_fan_out(shop: Checker, name: str) -> None:
    sql = (
        f'select c.region, count(*) as {name} from raw.customers c join raw.orders o on o.customer_id = c.customer_id '
        'join raw.payments p on p.order_id = o.order_id group by 1'
    )
    assert only(shop.check(sql), 'fan-out').message.startswith('COUNT(*) counts each orders row')
    # Named for the rows it does count, it is right.
    assert 'fan-out' not in rules(shop.check(sql.replace(f'as {name}', 'as payment_count')))


def test_an_unknown_column_named_through_an_alias_is_found(shop: Checker) -> None:
    result = shop.check('select sum(o.amount_total) as net_revenue from raw.orders o')
    assert only(result, 'unknown-column').message == 'orders has no column amount_total; did you mean amount?'


REFERENCES = [
    # A query, and one written by hand that answers it correctly: the correction must agree with it.
    (
        'select sum(amount) as net_revenue from raw.orders',
        "select sum(amount) as net_revenue from raw.orders where status <> 'returned'",
        None,
    ),
    (
        'select customer_id, sum(amount) as net_revenue from raw.orders group by customer_id',
        "select customer_id, sum(case when status <> 'returned' then amount end) as net_revenue "
        'from raw.orders group by customer_id',
        None,
    ),
    (
        'select customer_id, sum(amount) as net_revenue from raw.orders group by 1 having count(*) >= 3',
        "select customer_id, sum(case when status <> 'returned' then amount end) as net_revenue "
        'from raw.orders group by 1 having count(*) >= 3',
        None,
    ),
    (
        'select c.customer_id, c.name, sum(o.amount) as net_revenue from raw.customers c '
        'left join raw.orders o on o.customer_id = c.customer_id group by 1, 2',
        "select c.customer_id, c.name, sum(case when o.status <> 'returned' then o.amount end) as net_revenue "
        'from raw.customers c left join raw.orders o on o.customer_id = c.customer_id group by 1, 2',
        None,
    ),
    (
        'select sum(o.amount) as net_revenue, count(*) as orders_placed from raw.orders o',
        "select sum(case when o.status <> 'returned' then o.amount end) as net_revenue, count(*) as orders_placed "
        'from raw.orders o',
        None,
    ),
    (
        'with all_o as (select count(*) as n from raw.orders), '
        'rev as (select sum(amount) as net_revenue from raw.orders) '
        'select all_o.n, rev.net_revenue from all_o cross join rev',
        "select (select count(*) from raw.orders) as n, (select sum(amount) from raw.orders where status <> 'returned') "
        'as net_revenue',
        None,
    ),
    (
        'select region, sum(gross_revenue) as total from mart.customer_revenue group by 1',
        'select region, sum(net_revenue) as total from mart.customer_revenue group by 1',
        None,
    ),
    (
        'select o.order_id, c.name from raw.orders o join raw.customers c on o.order_id = c.customer_id',
        'select o.order_id, c.name from raw.orders o join raw.customers c on o.customer_id = c.customer_id',
        None,
    ),
    (
        'select customer_id, sum(amount) as net_revenue from raw.orders group by customer_id',
        "select customer_id, sum(case when status <> 'returned' then amount end) as net_revenue "
        'from raw.orders group by customer_id',
        'snowflake',
    ),
    (
        'select c.region, sum(o.amount) as net_revenue, count(*) as orders_placed from raw.orders o '
        'join raw.customers c on c.customer_id = o.customer_id group by 1',
        "select c.region, sum(case when o.status <> 'returned' then o.amount end) as net_revenue, "
        'count(*) as orders_placed from raw.orders o join raw.customers c on c.customer_id = o.customer_id group by 1',
        'bigquery',
    ),
]


@pytest.mark.parametrize(('sql', 'reference', 'dialect'), REFERENCES)
def test_every_correction_gives_the_right_answer(
    shop: Checker, dialects: dict[str, Checker], shop_db: Path, sql: str, reference: str, dialect: str | None
) -> None:
    import sqlglot

    result = (dialects[dialect] if dialect else shop).check(sql)
    assert result.corrected is not None, str(result)
    corrected = (
        sqlglot.transpile(result.corrected, read=dialect, write='duckdb')[0] if dialect else result.corrected
    )
    con = duckdb.connect(str(shop_db), read_only=True)
    try:
        assert sorted(map(str, con.execute(corrected).fetchall())) == sorted(
            map(str, con.execute(reference).fetchall())
        )
    finally:
        con.close()


@pytest.mark.parametrize(
    'sql',
    [
        'select customer_id, sum(amount) as net_revenue from raw.orders group by 1 having sum(amount) > 140',
        'with t as (select customer_id, sum(amount) as net_revenue from raw.orders group by 1) '
        'select * from t where coalesce(net_revenue, 0) < 120',
        'with t as (select customer_id, sum(amount) as net_revenue from raw.orders group by 1) '
        'select c.name, t.net_revenue from raw.customers c join t on t.customer_id = c.customer_id '
        'and t.net_revenue > 100',
    ],
)
def test_no_correction_where_a_filter_reads_the_measure(shop: Checker, sql: str) -> None:
    result = shop.check(sql)
    assert only(result, 'missing-filter').rewrite is None and result.corrected is None


def test_an_asof_join_repeats_the_rows_of_the_side_it_joins(shop: Checker, shop_db: Path) -> None:
    join = (
        'from raw.payments p asof join raw.orders o on o.order_id = p.order_id and o.order_id <= p.order_id'
    )
    assert only(shop.check(f'select sum(o.amount) as total {join}'), 'fan-out').severity == 'warning'
    assert loud(shop.check(f'select sum(p.amount) as total {join}')) == []
    con = duckdb.connect(str(shop_db), read_only=True)
    try:
        con.execute(f'select sum(o.amount) as total {join}').fetchall()
    finally:
        con.close()


def test_a_statement_the_analysis_cannot_read_is_reported_as_not_checked(shop: Checker) -> None:
    sql = 'select p.customer_id, p.returned from (pivot raw.orders on status using sum(amount) group by customer_id) p'
    finding = only(shop.check(sql), 'unchecked')
    assert finding.severity == 'info' and 'not checked' in finding.message


@pytest.mark.parametrize(
    'condition',
    [
        "o.status not in (select 'returned')",
        "o.status <> (select 'returned')",
        "o.status <> all(select 'returned')",
        "o.status not in (values ('returned'))",
    ],
)
def test_a_filter_through_a_query_of_constants_counts(shop: Checker, condition: str) -> None:
    assert loud(shop.check(f'select sum(o.amount) as net_revenue from raw.orders o where {condition}')) == []


def test_a_filter_through_a_list_of_values_counts(shop: Checker) -> None:
    for values in (
        "excluded(status) as (values ('returned'))",
        "excluded as (select * from (values ('returned')) t(status))",
    ):
        sql = (
            f'with {values} select sum(o.amount) as net_revenue from raw.orders o '
            'where o.status not in (select status from excluded)'
        )
        assert rules(shop.check(sql)) == ['recomputes-measure']


def test_a_filter_through_a_query_of_another_relation_is_a_note(shop: Checker) -> None:
    sql = 'select sum(o.amount) as net_revenue from raw.orders o where o.status not in (select method from raw.payments)'
    assert only(shop.check(sql), 'missing-filter').severity == 'info'


def test_an_unchecked_statement_does_not_pass(shop: Checker) -> None:
    sql = 'select p.customer_id, p.returned from (pivot raw.orders on status using sum(amount) group by customer_id) p'
    result = shop.check(sql)
    assert not result.checked and not result.passed and result.count('error') == 0
    assert result.to_dict()['checked'] is False
    assert shop.check('select 1').checked


def test_grouping_by_a_value_of_the_element_is_not_a_fan_out(dialects: dict[str, Checker]) -> None:
    sql = (
        'select f.value::string as v, count(*) as n from raw.orders o, '
        'lateral flatten(input => array_construct(1, 2)) f group by 1'
    )
    assert loud(dialects['snowflake'].check(sql)) == []


def test_a_typed_date_on_a_text_column_is_not_a_plain_string(shop: Checker) -> None:
    def key(text: str) -> str:
        tree = shop.parse(text)
        assert tree is not None
        made = shop.condition(tree, shop.resolved)
        assert made is not None
        return made.key

    assert key("shop.raw.orders.status = DATE '2020-01-01'") != key("shop.raw.orders.status = '2020-01-01'")
    assert key("shop.raw.orders.order_date = DATE '2020-01-01'") == key(
        "shop.raw.orders.order_date = '2020-01-01'"
    )
    assert key("shop.raw.orders.status = '2020-01-01 00:00:00'") != key(
        "shop.raw.orders.status = '2020-01-01'"
    )


@pytest.mark.parametrize(
    ('dialect', 'sql'),
    [
        (None, 'select t.x, count(*) as n from raw.orders o cross join unnest([1, 2]) as t(x) group by 1'),
        ('bigquery', 'select x, count(*) as n from raw.orders o cross join unnest([1, 2]) as x group by x'),
        (
            'snowflake',
            'select f.value, count(*) as n from raw.orders o, lateral flatten(input => array_construct(1, 2)) f group by 1',
        ),
        (
            None,
            'select tag, count(*) as n from (select o.amount, unnest([1, 2]) as tag from raw.orders o) t group by 1',
        ),
    ],
)
def test_counting_by_the_unnested_element_is_not_a_fan_out(
    shop: Checker, dialects: dict[str, Checker], dialect: str | None, sql: str
) -> None:
    assert loud((dialects[dialect] if dialect else shop).check(sql)) == []


@pytest.mark.parametrize(
    ('dialect', 'sql'),
    [
        (
            'snowflake',
            'select o.customer_id, sum(o.amount) as total from raw.orders o, table(flatten(input => array_construct(1, 2))) f group by 1',
        ),
        (
            'snowflake',
            'select o.customer_id, sum(o.amount) as total from raw.orders o join table(flatten(input => array_construct(1, 2))) f on true group by 1',
        ),
        (
            None,
            'select sum(amount) as total from (select o.amount, unnest([1, 2]) as tag from raw.orders o) t',
        ),
    ],
)
def test_a_flattened_or_listed_array_repeats_rows(
    shop: Checker, dialects: dict[str, Checker], dialect: str | None, sql: str
) -> None:
    assert only((dialects[dialect] if dialect else shop).check(sql), 'fan-out').severity == 'warning'


def test_an_unknown_column_of_a_cte_is_found(shop: Checker) -> None:
    sql = 'with t as (select customer_id, amount from raw.orders) select sum(t.amount_total) as total from t'
    assert (
        only(shop.check(sql), 'unknown-column').message
        == 't has no column amount_total; did you mean amount?'
    )


@pytest.mark.parametrize(
    'condition',
    ['o.customer_id <> 7', 'c.customer_id <> 7', "o.order_date <> '2026-01-01'"],
)
def test_every_extra_exclusion_is_a_warning(shop: Checker, condition: str) -> None:
    sql = (
        'select sum(o.amount) as net_revenue from raw.orders o join raw.customers c on c.customer_id = o.customer_id '
        f"where o.status <> 'returned' and {condition}"
    )
    assert only(shop.check(sql), 'formula-differs').severity == 'warning'


@pytest.mark.parametrize(
    'condition',
    [
        "o.status <> 'returned' and o.amount <> 0",
        "o.status not like 'returned' escape '!'",
        "o.status <> all(['returned'])",
        "o.status <> 'returned' and o.order_date >= make_date(2026, 1, 1)",
    ],
)
def test_filters_that_change_nothing_or_are_written_another_way_pass(shop: Checker, condition: str) -> None:
    assert loud(shop.check(f'select sum(o.amount) as net_revenue from raw.orders o where {condition}')) == []


def test_a_filter_of_the_graph_counts_wherever_it_sits(shop: Checker) -> None:
    orders = 'table.shop.raw.orders'
    for label, limited in (
        ('WHERE', frozenset()),
        ('WHERE BEFORE WINDOW', frozenset()),
        ('WHERE BEFORE WINDOW, LIMIT', frozenset()),
        ('JOIN ON', frozenset()),
        # On the nullable side of an outer join, a filter restricts that side's rows only.
        ('LEFT JOIN WHERE', frozenset({orders})),
        ('LEFT JOIN ON', frozenset({orders})),
    ):
        [condition] = shop.conjuncts(f"{label}: shop.raw.orders.status <> 'returned'")
        assert (condition.label, condition.limited) == ('WHERE', limited), label
    for label in ('DISTINCT', 'LIMIT', 'GROUP BY', 'RIGHT JOIN WHERE'):
        assert shop.conjuncts(f'{label}: shop.raw.orders.status') == []
    # The equality of a join is the join, not a filter.
    assert shop.conjuncts('JOIN ON: shop.raw.orders.customer_id = shop.raw.customers.customer_id') == []


REGION_MODELS = {
    'region_counts': (
        'create table mart.region_counts as with returned as (select customer_id, count(*) as n from raw.orders '
        "where status = 'returned' group by 1) select c.region, count(distinct c.customer_id) as customers, "
        'sum(r.n) as returns from raw.customers c left join returned r on r.customer_id = c.customer_id '
        'group by c.region'
    ),
    'region_returns': (
        'create table mart.region_returns as with returned as (select customer_id, amount from raw.orders '
        "where status = 'returned') select c.region, sum(r.amount) as returned_amount from raw.customers c "
        'left join returned r on r.customer_id = c.customer_id group by c.region'
    ),
}


def test_a_model_s_filter_on_the_side_of_an_outer_join_holds_for_that_side_only(
    tmp_path: Path, shop_db: Path
) -> None:
    make_shop(tmp_path, definitions=None)
    for name, sql in REGION_MODELS.items():
        (tmp_path / 'mart' / f'{name}.sql').write_text(sql)
    checker = Checker(Project(tmp_path).build(run_dbt=False).graph)
    # The customers of a region are all of them, whatever their returns.
    counted = 'select c.region, count(distinct c.customer_id) as customers from raw.customers c group by 1'
    assert loud(checker.check(counted)) == []
    # The returned amount reads the joined orders, which the model filters.
    summed = (
        'select c.region, sum(r.amount) as returned_amount from raw.customers c '
        'left join raw.orders r on r.customer_id = c.customer_id group by 1'
    )
    result = checker.check(summed)
    assert only(result, 'missing-filter').severity == 'warning' and result.corrected is not None
    reference = (
        "select c.region, sum(case when r.status = 'returned' then r.amount end) as returned_amount "
        'from raw.customers c left join raw.orders r on r.customer_id = c.customer_id group by 1'
    )
    con = duckdb.connect(str(shop_db), read_only=True)
    try:
        fixed = con.execute(result.corrected).fetchall()
        assert sorted(map(str, fixed)) == sorted(map(str, con.execute(reference).fetchall()))
    finally:
        con.close()


# Names: which terms an output column stands for


@pytest.mark.parametrize(
    'sql',
    [
        'select count(o.order_id) as orders from raw.orders o',
        'select count(*) as total_orders from raw.orders',
        'select order_id as "order" from raw.orders',
        'select amount as net_revenue from raw.orders',
        'select sum(amount) as sales from raw.orders',
        'select sum(amount) as income from raw.payments',
        'select sum(amount) as amount from raw.payments',
    ],
)
def test_only_an_aggregate_named_exactly_for_a_term_is_compared_with_it(shop: Checker, sql: str) -> None:
    assert loud(shop.check(sql)) == []


def test_a_synonym_names_its_term(shop: Checker) -> None:
    finding = only(shop.check('select sum(amount) as revenue from raw.orders'), 'missing-filter')
    assert finding.term == 'Net revenue'


def test_a_match_by_name_alone_is_never_an_error(shop: Checker) -> None:
    sql = NET_REVENUE.replace('sum(o.amount)', 'sum(o.amount * 1.2)').replace(
        'group by', "where o.status <> 'returned'\ngroup by"
    )
    finding = only(shop.check(sql), 'formula-differs')
    assert finding.severity == 'warning' and finding.term == 'Net revenue'
    assert finding.detail == 'SUM(orders.amount * 1.2)'
    assert 'customer_revenue.net_revenue computes it as SUM(orders.amount)' in finding.message
    assert 'Read customer_revenue.net_revenue (approved' in finding.fix
    missing = only(
        shop.check('select sum(o.amount * 1.2) as net_revenue from raw.orders o'), 'missing-filter'
    )
    assert missing.severity == 'warning'


# Formulas


@pytest.mark.parametrize(
    'value',
    [
        'round(sum(amount), 2)',
        'cast(sum(amount) as double)',
        'coalesce(sum(amount), 0)',
        'sum(amount) * 1.0',
        'sum(cast(amount as decimal(12, 2)))',
    ],
)
def test_a_value_preserving_wrapper_is_the_same_formula(shop: Checker, value: str) -> None:
    sql = f"select {value} as net_revenue from raw.orders where status <> 'returned'"
    assert rules(shop.check(sql)) == ['recomputes-measure']


@pytest.mark.parametrize(
    'value',
    [
        "sum(case when status <> 'returned' then amount else 0 end)",
        "sum(case when status <> 'returned' then amount end)",
        "sum(amount) filter (where status <> 'returned')",
        "sum(if(status <> 'returned', amount, 0))",
    ],
)
def test_a_condition_inside_the_aggregate_is_its_filter(shop: Checker, value: str) -> None:
    assert rules(shop.check(f'select {value} as net_revenue from raw.orders')) == ['recomputes-measure']
    # A WHERE clause that repeats it changes nothing.
    sql = f"select {value} as net_revenue from raw.orders where status <> 'returned'"
    assert rules(shop.check(sql)) == ['recomputes-measure']


def test_each_branch_of_a_union_is_a_measure_of_its_own(shop: Checker) -> None:
    both = (
        "select 'north' as part, sum(o.amount) as net_revenue from raw.orders o join raw.customers c "
        "on c.customer_id = o.customer_id where c.region = 'North' and o.status <> 'returned' "
        "union all select 'south', sum(o.amount) from raw.orders o join raw.customers c "
        "on c.customer_id = o.customer_id where c.region = 'South' and o.status <> 'returned'"
    )
    assert loud(shop.check(both)) == []
    one = both.rsplit(" and o.status <> 'returned'", 1)[0]
    finding = only(shop.check(one), 'missing-filter')
    assert finding.column == 'net_revenue' and finding.severity == 'error'


def test_each_union_branch_without_the_filter_is_reported(shop: Checker) -> None:
    sql = (
        'select sum(amount) as net_revenue from raw.orders union all '
        "select sum(amount) from raw.orders where order_date >= '2026-06-01'"
    )
    assert [f.rule for f in shop.check(sql).findings] == ['missing-filter', 'missing-filter']


def test_a_scalar_subquery_is_a_measure_of_its_own(shop: Checker) -> None:
    sql = 'select (select sum(amount) from raw.orders) as net_revenue'
    finding = only(shop.check(sql), 'missing-filter')
    assert 'SELECT' not in finding.message and finding.column == 'net_revenue'
    filtered = "select (select sum(amount) from raw.orders where status <> 'returned') as net_revenue"
    assert loud(shop.check(filtered)) == []


def test_a_value_computed_from_a_query_is_not_compared(shop: Checker) -> None:
    sql = (
        'select sum(amount) / (select count(*) from raw.customers) as net_revenue from raw.orders '
        "where status <> 'returned'"
    )
    assert shop.check(sql).findings == []


def test_an_extra_filter_that_leaves_out_rows_is_another_formula(shop: Checker) -> None:
    sql = (
        'select o.order_id, sum(p.amount) as paid_amount from raw.orders o '
        "join raw.payments p on p.order_id = o.order_id where p.method <> 'coupon' group by 1"
    )
    finding = only(shop.check(sql), 'formula-differs')
    assert finding.detail == "with payments.method <> 'coupon'" and finding.severity == 'warning'
    assert 'but with payments.method' in finding.message


@pytest.mark.parametrize(
    'condition',
    [
        "p.method = 'card'",
        "o.order_date >= '2026-01-01'",
    ],
)
def test_a_filter_that_chooses_the_rows_of_a_question_is_not_another_formula(
    shop: Checker, condition: str
) -> None:
    sql = (
        'select o.order_id, sum(p.amount) as paid_amount from raw.orders o '
        f'join raw.payments p on p.order_id = o.order_id where {condition} group by 1'
    )
    assert shop.check(sql).findings == []


def test_a_measure_from_other_sources_names_them(shop: Checker) -> None:
    finding = only(shop.check('select sum(amount) as net_revenue from raw.payments'), 'formula-differs')
    assert finding.evidence == [
        'reads payments.amount, which customer_revenue.net_revenue does not',
        'leaves out orders.amount',
    ]


def test_an_average_against_a_sum_is_an_aggregation_difference(shop: Checker) -> None:
    sql = NET_REVENUE.replace('sum(o.amount)', 'avg(o.amount)').replace(
        'group by', "where o.status <> 'returned'\ngroup by"
    )
    finding = only(shop.check(sql), 'formula-differs')
    assert finding.evidence == ['aggregates with AVG against SUM']


def test_a_draft_measure_recomputed_the_same_way_is_a_note(shop: Checker) -> None:
    sql = (
        'select o.order_id, sum(p.amount) as paid_amount from raw.orders o '
        'join raw.payments p on p.order_id = o.order_id group by 1'
    )
    finding = only(shop.check(sql), 'recomputes-measure')
    assert finding.severity == 'info' and finding.term == 'Paid amount'
    assert finding.fix.startswith("Read order_payments.paid (draft 'Paid amount'")


def test_re_aggregating_a_sum_is_the_same_formula(shop: Checker) -> None:
    sql = (
        'with t as (select o.order_id, sum(p.amount) as paid from raw.orders o '
        'join raw.payments p on p.order_id = o.order_id group by 1) '
        'select sum(paid) as paid_amount from t'
    )
    assert rules(shop.check(sql)) == ['recomputes-measure']


# Retired columns


def test_a_retired_column_is_swapped_for_its_replacement_in_the_same_relation(shop: Checker) -> None:
    result = shop.check('select region, sum(gross_revenue) as total from mart.customer_revenue group by 1')
    finding = only(result, 'retired-column')
    assert finding.severity == 'warning' and finding.term == 'Gross revenue'
    assert finding.rewrite == 'gross_revenue read from net_revenue'
    assert finding.fix.startswith('Read customer_revenue.net_revenue (approved')
    assert result.corrected is not None and 'SUM(net_revenue) AS total' in result.corrected


def test_a_retired_column_in_the_result_keeps_its_name(shop: Checker) -> None:
    result = shop.check('select r.region, r.gross_revenue from mart.customer_revenue r')
    assert result.corrected is not None and 'r.net_revenue AS gross_revenue' in result.corrected


@pytest.mark.parametrize(
    'sql',
    [
        # The column passes out of the CTE under its name, which the query around it reads.
        'with r as (select region, gross_revenue from mart.customer_revenue) '
        'select region, sum(gross_revenue) as total from r group by 1',
        # Its replacement is already in the result.
        'select r.gross_revenue, r.net_revenue from mart.customer_revenue r',
    ],
)
def test_a_retired_column_is_not_swapped_where_that_would_break_the_query(shop: Checker, sql: str) -> None:
    result = shop.check(sql)
    assert only(result, 'retired-column').rewrite is None and result.corrected is None


# Joins


def test_a_sum_over_a_join_that_repeats_its_rows_is_a_fan_out(shop: Checker) -> None:
    sql = (
        'select c.region, sum(o.amount) as total from raw.orders o '
        'join raw.payments p on p.order_id = o.order_id '
        'join raw.customers c on c.customer_id = o.customer_id group by 1'
    )
    finding = only(shop.check(sql), 'fan-out')
    assert finding.severity == 'warning' and finding.column == 'total'
    assert finding.message == 'SUM(o.amount) adds orders.amount once per matching payments row'
    assert finding.evidence == [
        'payments.order_id refers to order (payments.order_id = orders.order_id), so many payments rows '
        'can match one orders row'
    ]
    assert finding.fix.startswith('Aggregate payments to one row per order_id in a subquery before the join')


@pytest.mark.parametrize(
    'sql',
    [
        # The many side's own column: each payment appears once.
        'select sum(p.amount) from raw.orders o join raw.payments p on p.order_id = o.order_id',
        # Pre-aggregated to the join key first.
        'with p as (select order_id, sum(amount) as paid from raw.payments group by 1) '
        'select sum(o.amount), sum(p.paid) from raw.orders o join p on p.order_id = o.order_id',
        # Measures that do not change with repeated rows.
        'select max(o.amount), count(distinct o.order_id), sum(distinct o.amount) '
        'from raw.orders o join raw.payments p on p.order_id = o.order_id',
        # Many-to-one joins keep the grain.
        'select c.region, sum(o.amount) from raw.orders o join raw.customers c on c.customer_id = o.customer_id '
        'group by 1',
        # A window over the many side's own column.
        'select sum(p.amount) over (partition by o.order_id) from raw.orders o '
        'join raw.payments p on p.order_id = o.order_id',
        # Grouped by the key of the side that repeats: one payment, so one order, per group.
        'select p.payment_id, sum(o.amount) from raw.payments p join raw.orders o on o.order_id = p.order_id '
        'group by p.payment_id',
        # Made distinct on the key of the side that repeats.
        'with x as (select distinct o.order_id, o.amount from raw.orders o '
        "join raw.payments p on p.order_id = o.order_id where p.method = 'card') select sum(amount) from x",
    ],
)
def test_joins_that_keep_the_grain_are_not_fan_outs(shop: Checker, sql: str) -> None:
    assert 'fan-out' not in rules(shop.check(sql))


def test_distinct_without_the_key_keeps_the_fan_out(shop: Checker) -> None:
    sql = (
        'with x as (select distinct o.customer_id, o.amount, p.payment_id from raw.orders o '
        'join raw.payments p on p.order_id = o.order_id) select sum(amount) from x'
    )
    assert 'fan-out' in rules(shop.check(sql))


def test_a_fan_out_inside_a_subquery_is_found_where_it_is_aggregated(shop: Checker) -> None:
    sql = (
        'with j as (select o.amount, p.payment_id from raw.orders o join raw.payments p on p.order_id = o.order_id) '
        'select sum(amount) from j'
    )
    finding = only(shop.check(sql), 'fan-out')
    assert finding.column is None and finding.message.startswith('SUM(j.amount) adds orders.amount')


def test_a_fan_out_through_a_bridge_table(bridge: Checker) -> None:
    sql = (
        'select c.country, sum(o.amount) as revenue from raw.orders o '
        'join raw.customer_segments cs on cs.customer_id = o.customer_id '
        'join raw.customers c on c.customer_id = o.customer_id group by 1'
    )
    finding = only(bridge.check(sql), 'fan-out')
    assert finding.severity == 'warning'
    assert finding.evidence == [
        'customer_segments has one row per customer_id, segment_id, not per customer_id, so many of its rows '
        'can match one orders row'
    ]
    # The side joined on its unique key repeats once per segment.
    unique_side = (
        'select sum(v.completed_revenue) as completed_revenue from mart.customer_value v '
        'join raw.customer_segments cs on cs.customer_id = v.customer_id'
    )
    assert only(bridge.check(unique_side), 'fan-out').severity == 'warning'
    # Grouped by the rest of the bridge's key, each customer counts once per segment.
    grouped = (
        'select cs.segment_id, sum(v.completed_revenue) as completed_revenue from mart.customer_value v '
        'join raw.customer_segments cs on cs.customer_id = v.customer_id group by cs.segment_id'
    )
    assert 'fan-out' not in rules(bridge.check(grouped))


def test_a_join_on_a_key_to_a_relation_with_no_known_key_is_a_note(bridge: Checker) -> None:
    sql = (
        'select sum(v.completed_revenue) as completed_revenue from mart.customer_value v '
        'join raw.customer_notes n on n.customer_id = v.customer_id'
    )
    finding = only(bridge.check(sql), 'fan-out')
    assert finding.severity == 'info'
    assert finding.evidence == [
        'customer_value is joined on its key, and nothing says customer_notes has one row per customer_id'
    ]


def test_a_coarser_measure_repeated_on_finer_rows_is_a_mixed_grain(shop: Checker) -> None:
    sql = (
        'select o.order_id, r.net_revenue from raw.orders o '
        'join mart.customer_revenue r on r.customer_id = o.customer_id'
    )
    finding = only(shop.check(sql), 'mixed-grain')
    assert finding.column == 'net_revenue'
    assert finding.message.startswith('customer_revenue.net_revenue has one value per customer_revenue row')
    # A dimension repeated on finer rows is fine.
    assert 'mixed-grain' not in rules(
        shop.check(
            'select o.order_id, c.region from raw.orders o join raw.customers c on c.customer_id = o.customer_id'
        )
    )


def test_an_amount_the_project_adds_up_repeated_on_finer_rows_is_a_mixed_grain(
    bare: Checker, bridge: Checker
) -> None:
    # orders.amount is bound to no term, but customer_revenue adds it up.
    sql = 'select p.payment_id, o.amount from raw.payments p join raw.orders o on o.order_id = p.order_id'
    assert only(bare.check(sql), 'mixed-grain').column == 'amount'
    # A price is not added up anywhere, so repeating it is not a mixed grain.
    price = 'select i.item_id, p.list_price from raw.order_items i join raw.products p on p.product_id = i.product_id'
    assert 'mixed-grain' not in rules(bridge.check(price))


def test_a_join_on_the_wrong_key_is_put_back_on_the_relationship(shop: Checker, shop_db: Path) -> None:
    result = shop.check(
        'select o.order_id, c.name from raw.orders o join raw.customers c on o.order_id = c.customer_id'
    )
    finding = only(result, 'join-off-relationship')
    assert finding.rewrite == 'join on o.customer_id = c.customer_id'
    assert 'order refers to customer through orders.customer_id = customers.customer_id' in finding.message
    assert result.corrected is not None and 'ON o.customer_id = c.customer_id' in result.corrected
    assert shop.check(result.corrected).findings == []
    con = duckdb.connect(str(shop_db), read_only=True)
    try:
        assert con.execute(result.corrected).fetchall()
    finally:
        con.close()


def test_a_join_of_one_key_to_another_entity_s_identifier_is_a_warning(shop: Checker) -> None:
    result = shop.check(
        'select c.name from raw.payments p join raw.customers c on p.payment_id = c.customer_id'
    )
    finding = only(result, 'unknown-join')
    assert finding.severity == 'warning'
    assert 'payments.payment_id, which identifies a payment' in finding.message


def test_a_join_nothing_vouches_for_is_a_note(shop: Checker) -> None:
    result = shop.check('select o.order_id from raw.orders o join raw.payments p on o.status = p.method')
    finding = only(result, 'unknown-join')
    assert finding.severity == 'info' and result.passed and not result.failed(strict=True)


@pytest.mark.parametrize(
    'sql',
    [
        # A CTE grouped by its join column has one row per value of it.
        'with p as (select method, sum(amount) as paid from raw.payments group by method) '
        'select o.status, sum(p.paid) from p join raw.orders o on o.status = p.method group by 1',
        # A relation built by one query grouped by a key has one row per key.
        'select r.region, r.net_revenue, p.payment_id from mart.customer_revenue r '
        'join raw.payments p on p.amount = r.customer_id',
    ],
)
def test_a_join_on_a_grouped_key_is_not_an_unknown_join(shop: Checker, sql: str) -> None:
    assert 'unknown-join' not in rules(shop.check(sql))


GROUPED_THEN_JOINED = {
    'region_customers': (
        'create table mart.region_customers as with r as (select c.region, sum(o.amount) as region_revenue '
        'from raw.orders o join raw.customers c on c.customer_id = o.customer_id group by c.region) '
        'select r.region, r.region_revenue, c.customer_id, c.name from r join raw.customers c on c.region = r.region'
    ),
    'order_lines': (
        'create table mart.order_lines as select o.order_id, o.customer_id, p.payment_id, p.amount as paid '
        'from (select order_id, customer_id from raw.orders group by order_id, customer_id) o '
        'join raw.payments p on p.order_id = o.order_id'
    ),
}


def test_a_model_that_groups_and_joins_back_to_detail_has_no_grouped_key(tmp_path: Path) -> None:
    make_shop(tmp_path)
    for name, sql in GROUPED_THEN_JOINED.items():
        (tmp_path / 'mart' / f'{name}.sql').write_text(sql)
    checker = Checker(Project(tmp_path).build(run_dbt=False).graph)
    for name in GROUPED_THEN_JOINED:
        relation = checker.graph.node_for_relation(f'shop.mart.{name}')
        assert relation is not None and checker.relation_keys(relation) == [], name
    grouped = checker.graph.node_for_relation('shop.mart.customer_revenue')
    assert grouped is not None and checker.relation_keys(grouped) == [frozenset({'customer_id'})]
    fanned = checker.check(
        'select o.status, sum(o.amount) as order_amount from raw.orders o '
        'join mart.order_lines l on l.order_id = o.order_id group by 1'
    )
    assert 'fan-out' in rules(fanned)


def test_a_join_through_a_subquery_is_checked_against_the_columns_it_passes_through(shop: Checker) -> None:
    sql = (
        'with c as (select customer_id as id, name from raw.customers) '
        'select o.order_id, c.name from raw.orders o join c on o.order_id = c.id'
    )
    assert 'join-off-relationship' in rules(shop.check(sql))


# Names and statements


def test_unknown_relations_and_columns_suggest_close_names(shop: Checker) -> None:
    result = shop.check('select amount_total from raw.orders; select * from raw.orderz')
    assert only(result, 'unknown-column').message == 'orders has no column amount_total; did you mean amount?'
    assert (
        'raw.orderz is not a relation of the project; did you mean orders'
        in only(result, 'unknown-relation').message
    )
    assert result.statements == 2


def test_sql_that_does_not_parse_is_an_error(shop: Checker) -> None:
    result = shop.check('select sum(amount from raw.orders')
    assert only(result, 'unparsed').severity == 'error' and result.statements == 0


def test_statements_that_are_not_queries_are_skipped_and_create_as_select_is_checked(shop: Checker) -> None:
    result = shop.check('create schema tmp; create table tmp.t as ' + NET_REVENUE)
    assert result.statements == 1 and rules(result) == ['missing-filter']
    assert result.corrected is not None and result.corrected.startswith('CREATE SCHEMA tmp;')


def test_the_result_renders_as_text_markdown_and_json(shop: Checker) -> None:
    result = shop.check(NET_REVENUE)
    text = str(result)
    assert text.startswith('1 error, 0 warnings, 0 notes in 1 statement.')
    assert '\nerror    missing-filter  net_revenue computes ' in text and '\nCorrected SQL\n  SELECT' in text
    assert '\n         fix       Apply the filter: ' in text
    markdown = result.markdown()
    assert '| error | `missing-filter` |' in markdown and '```sql' in markdown
    data = json.loads(json.dumps(result.to_dict()))
    assert data['passed'] is False and data['findings'][0]['rule'] == 'missing-filter'
    assert set(RULES) >= {f['rule'] for f in data['findings']}


def test_every_finding_has_the_same_keys_in_json(shop: Checker) -> None:
    keys = {'rule', 'severity', 'message', 'fix', 'column', 'term', 'detail', 'rewrite', 'evidence'}
    assert set(Finding('fan-out', 'warning', 'm', 'f').to_dict()) == keys
    sql = NET_REVENUE + '; select o.order_id from raw.orders o join raw.payments p on o.status = p.method'
    findings = shop.check(sql).to_dict()['findings']
    assert len(findings) == 2 and all(set(f) == keys for f in findings)


def test_check_sql_builds_its_own_checker(shop: Checker) -> None:
    assert rules(check_sql(shop.graph, NET_REVENUE)) == ['missing-filter']


def test_the_module_uses_only_public_names_of_other_modules() -> None:
    import konstruct.analysis.checksql as module

    tree = ast.parse(Path(module.__file__).read_text())
    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or '').startswith('konstruct')
        for alias in node.names
    ]
    assert imported and [name for name in imported if name.startswith('_')] == []


# Without definitions.yml


def test_without_definitions_a_filter_the_existing_column_applies_is_still_required(bare: Checker) -> None:
    result = bare.check(NET_REVENUE)
    finding = only(result, 'missing-filter')
    assert finding.severity == 'warning' and finding.term is None
    assert finding.message == (
        "net_revenue computes 'net_revenue' as customer_revenue.net_revenue does, but without its filter "
        "orders.status <> 'returned'"
    )
    assert finding.evidence == ['WHERE in customer_revenue (mart/customer_revenue.sql)']
    assert finding.rewrite == "o.status <> 'returned' applied to SUM(o.amount) alone"


def test_without_definitions_another_formula_for_a_named_idea_is_a_note(bare: Checker) -> None:
    sql = NET_REVENUE.replace('sum(o.amount)', 'sum(o.amount) * 2').replace(
        'group by', "where o.status <> 'returned'\ngroup by"
    )
    assert only(bare.check(sql), 'formula-differs').severity == 'info'


def test_without_definitions_joins_use_the_derived_keys(bare: Checker) -> None:
    sql = (
        'select sum(o.amount) from raw.orders o join raw.payments p on p.order_id = o.order_id; '
        'select o.order_id from raw.orders o join raw.customers c on o.order_id = c.customer_id'
    )
    assert rules(bare.check(sql)) == ['fan-out', 'join-off-relationship']


STRIPE = """
create schema stripe;
create table stripe.charge (
    id varchar, amount bigint, amount_refunded bigint, captured boolean, paid boolean,
    currency varchar, livemode boolean, customer_id varchar
);
create table stripe.customer (id varchar, email varchar, delinquent boolean, livemode boolean);
create table stripe.invoice (id varchar, customer_id varchar, amount_due bigint);
"""


def test_an_ontology_pack_gives_keys_and_terms_to_a_project_without_definitions(tmp_path: Path) -> None:
    con = duckdb.connect(str(tmp_path / 'warehouse.duckdb'))
    con.execute(STRIPE)
    con.close()
    graph = Project(tmp_path).build(run_dbt=False).graph
    result = check_sql(
        graph,
        'select u.email, sum(c.amount) as charged from stripe.customer u '
        'join stripe.charge c on c.customer_id = u.id '
        'join stripe.invoice i on i.customer_id = u.id group by 1',
    )
    finding = only(result, 'fan-out')
    assert finding.evidence == [
        'invoice.customer_id refers to stripe_customer (invoice.customer_id = customer.id), as the stripe '
        'pack gives it, so many invoice rows can match one charge row'
    ]
    # The query reads the customer's email, which the pack marks personal.
    assert result.terms == ['Stripe charge amount', 'Stripe customer contact details']


# The example project


def test_the_example_project(jaffle_built_graph: Path) -> None:
    checker = Checker(Project(jaffle_built_graph).graph)
    result = checker.check(
        'select o.customer_id, sum(p.amount * 100) as lifetime_value from stg_payments p '
        'join stg_orders o on o.order_id = p.order_id group by 1'
    )
    finding = only(result, 'formula-differs')
    assert finding.term == 'Customer lifetime value' and finding.severity == 'warning'
    assert 'SUM(raw_payments.amount / 100)' in finding.message
    completed = checker.check('select count(*) as completed_orders from orders')
    assert (
        only(completed, 'missing-filter').rewrite == "orders.status = 'completed' added to the WHERE clause"
    )
    fanned = checker.check(
        'select c.customer_id, sum(c.customer_lifetime_value) as ltv from customers c '
        'join orders o on o.customer_id = c.customer_id group by 1'
    )
    assert only(fanned, 'fan-out').evidence[0].startswith('orders.customer_id refers to customer')
    assert checker.check('select customer_id, customer_lifetime_value from customers').findings == []
    # A FILTER inside the aggregate is the CASE the project's column applies.
    by_card = (
        "select sum(amount) filter (where payment_method = 'credit_card') as credit_card_amount "
        'from stg_payments'
    )
    assert rules(checker.check(by_card)) == ['recomputes-measure']


# Query history, from rows recorded in each warehouse's shape


FIXTURES = Path(__file__).parent / 'fixtures' / 'history'


def recorded(name: str) -> list[Any]:
    rows: list[Any] = json.loads((FIXTURES / f'{name}.json').read_text())['rows']
    return rows


def _stub(monkeypatch: pytest.MonkeyPatch, module_name: str, rows: list[Any]) -> list[str]:
    log: list[str] = []

    class Cursor:
        def execute(self, sql: str) -> None:
            log.append(sql)

        def fetchall(self) -> list[tuple[Any, ...]]:
            return [tuple(r) for r in rows]

    connection = types.SimpleNamespace(cursor=Cursor, close=lambda: None)
    module = types.ModuleType(module_name)
    module.connect = lambda **kwargs: connection  # type: ignore[attr-defined]
    package = types.ModuleType(module_name.split('.')[0])
    setattr(package, module_name.split('.')[1], module)
    monkeypatch.setitem(sys.modules, module_name.split('.')[0], package)
    monkeypatch.setitem(sys.modules, module_name, module)
    return log


def test_history_from_snowflake_counts_shadow_metrics_by_queries_runs_and_people(
    shop: Checker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from konstruct.inputs.warehouse import SnowflakeConnector

    _stub(monkeypatch, 'snowflake.connector', recorded('snowflake'))
    history = fetch_history(SnowflakeConnector({'account': 'x'}), UsageSettings())
    result = check_history(shop.graph, history)
    assert (result.queries, result.runs, result.unparsed, result.redacted) == (5, 959, 0, 0)
    missing, extra = result.shadow
    assert (missing.term, missing.status, missing.rule) == ('Net revenue', 'approved', 'missing-filter')
    assert missing.detail == "without orders.status <> 'returned'"
    assert (missing.queries, missing.runs, missing.users, missing.people) == (2, 17, 2, ['ANA', 'BEN'])
    assert missing.example.startswith('select c.region, sum(o.amount) as net_revenue')
    assert (extra.rule, extra.detail) == ('formula-differs', 'with orders.customer_id <> 1042')
    adopted = next(a for a in result.adoption if a.term == 'Net revenue')
    assert (adopted.queries, adopted.runs, adopted.users) == (1, 40, 1)
    text = str(result)
    assert text.startswith('5 queries checked (959 runs); 2 shadow metrics, 2 of an approved measure.')
    assert "2 queries by 2 people compute 'Net revenue' (approved) without" in text
    assert "1 query by 1 person computes 'Net revenue' (approved) with orders.customer_id <> 1042" in text
    assert '\n    people  ANA, BEN\n' in text and '\nRead through their own columns (2)\n' in text
    data = json.loads(json.dumps(result.to_dict()))
    assert data['shadow'][0]['queries'] == 2 and adopted.to_dict() in data['adoption']


def test_history_from_databricks_counts_redacted_texts_apart(
    shop: Checker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from konstruct.inputs.warehouse import DatabricksConnector

    _stub(monkeypatch, 'databricks.sql', recorded('databricks'))
    history = fetch_history(DatabricksConnector({'server_hostname': 'x'}), UsageSettings())
    result = check_history(shop.graph, history)
    assert (result.queries, result.redacted) == (2, 15)
    assert [s.users for s in result.shadow] == [1]
    assert 'databricks_pii_access' in str(result) and result.to_dict()['redacted'] == 15


def test_history_from_bigquery(shop: Checker) -> None:
    from konstruct.inputs.warehouse import BigQueryConnector

    rows = recorded('bigquery')

    class Job:
        def result(self) -> list[dict[str, Any]]:
            return rows

    connector = BigQueryConnector('acme', location='EU')
    connector.client = lambda: types.SimpleNamespace(query=lambda sql: Job())  # type: ignore[method-assign]
    result = check_history(shop.graph, fetch_history(connector, UsageSettings()))
    assert result.queries == 2
    [shadow] = result.shadow
    assert (shadow.rule, shadow.users) == ('missing-filter', 1)


def test_history_can_pseudonymise_people_and_limit_the_queries(shop: Checker) -> None:
    sql = (
        'select sum(o.amount) as net_revenue from raw.orders o '
        "where o.status <> 'returned' and o.customer_id <> 1042"
    )
    history = QueryHistory([HistoryQuery(sql, 'ann@example.com', 3), HistoryQuery('select 1', 'bob', 1)])
    result = check_history(shop.graph, history, pseudonymise=True, limit=1)
    assert result.queries == 1
    [shadow] = result.shadow
    assert (shadow.users, shadow.people) == (1, [])
    assert '1042' not in shadow.example and 'returned' not in shadow.example and '<> ?' in shadow.example
    assert 'ann' not in json.dumps(result.to_dict())


def test_the_project_checks_sql_and_the_history_of_its_warehouses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from konstruct import QueryError

    make_shop(tmp_path)
    project = Project(tmp_path)
    assert project.check_sql(NET_REVENUE).to_dict() == check_sql(project.graph, NET_REVENUE).to_dict()
    with pytest.raises(QueryError, match='no Snowflake, BigQuery or Databricks warehouse is configured'):
        project.check_history()
    (tmp_path / 'konstruct.toml').write_text(
        '[[warehouse]]\ntype = "snowflake"\naccount = "x"\n\n[usage]\ndays = 14\n'
    )
    log = _stub(monkeypatch, 'snowflake.connector', recorded('snowflake'))
    # The project's own connection is used only once it is trusted.
    with pytest.raises(QueryError, match=r'cannot read query history: .* is not trusted'):
        project.check_history()
    monkeypatch.setenv('KONSTRUCT_TRUST_CONFIG', '1')
    result = project.check_history(limit=3, pseudonymise=True)
    assert 'dateadd(day, -14,' in log[-1] and 'limit 3' in log[-1]
    assert result.queries == 3 and [s.people for s in result.shadow] == [[]]
    assert project.check_history(days=7).shadow[0].people == ['ANA', 'BEN']
    assert 'dateadd(day, -7,' in log[-1]
    with pytest.raises(QueryError, match='at least 1 query'):
        project.check_history(limit=0)
    # A window of no days, or one in the future, is refused, not read as the default.
    for days in (0, -3):
        with pytest.raises(QueryError, match='at least 1 day'):
            project.check_history(days=days)


def test_the_project_reports_a_warehouse_that_refuses_its_history_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from konstruct import QueryError

    make_shop(tmp_path)
    project = Project(tmp_path)
    (tmp_path / 'konstruct.toml').write_text('[[warehouse]]\ntype = "snowflake"\naccount = "x"\n')
    monkeypatch.setenv('KONSTRUCT_TRUST_CONFIG', '1')

    class Error(Exception):
        """The driver's DB-API base error."""

    def connect(failure: BaseException) -> Callable[..., Any]:
        def refused(**options: Any) -> Any:
            raise failure

        return refused

    module = types.ModuleType('snowflake.connector')
    module.Error = Error  # type: ignore[attr-defined]
    package = types.ModuleType('snowflake')
    package.connector = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, 'snowflake', package)
    monkeypatch.setitem(sys.modules, 'snowflake.connector', module)
    module.connect = connect(Error('250001: could not connect'))  # type: ignore[attr-defined]
    with pytest.raises(
        QueryError, match='cannot read query history: Snowflake refused the query history: 250001'
    ):
        project.check_history()
    # Any other error is a bug, and is not passed off as the warehouse's answer.
    module.connect = connect(KeyError('account'))  # type: ignore[attr-defined]
    with pytest.raises(KeyError):
        project.check_history()
    monkeypatch.delitem(sys.modules, 'snowflake.connector')
    monkeypatch.setitem(sys.modules, 'snowflake', None)
    with pytest.raises(QueryError, match='cannot read query history: install the snowflake extra'):
        project.check_history()


def test_a_databricks_connection_that_never_reaches_the_warehouse_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib3.exceptions import MaxRetryError

    from konstruct import QueryError

    make_shop(tmp_path)
    project = Project(tmp_path)
    (tmp_path / 'konstruct.toml').write_text('[[warehouse]]\ntype = "databricks"\nserver_hostname = "x"\n')
    monkeypatch.setenv('KONSTRUCT_TRUST_CONFIG', '1')
    module = types.ModuleType('databricks.sql')
    package = types.ModuleType('databricks')
    package.sql = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, 'databricks', package)
    monkeypatch.setitem(sys.modules, 'databricks.sql', module)
    # The driver has no DB-API Error here, so only the transport's own errors are reported.
    for failure in (
        MaxRetryError(None, '/sql/1.0'),  # type: ignore[arg-type]
        ConnectionRefusedError(111, 'refused'),
    ):

        def refused(failure: BaseException = failure, **options: Any) -> Any:
            raise failure

        module.connect = refused  # type: ignore[attr-defined]
        with pytest.raises(
            QueryError, match='cannot read query history: Databricks refused the query history'
        ):
            project.check_history()


def test_bigquery_credentials_that_cannot_be_read_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from konstruct import QueryError
    from konstruct.interfaces.cli import main

    pytest.importorskip('google.cloud.bigquery')
    make_shop(tmp_path)
    (tmp_path / 'konstruct.toml').write_text('[[warehouse]]\ntype = "bigquery"\nproject = "acme"\n')
    monkeypatch.setenv('KONSTRUCT_TRUST_CONFIG', '1')
    monkeypatch.setenv('GOOGLE_APPLICATION_CREDENTIALS', '/nonexistent/creds.json')
    with pytest.raises(QueryError, match='cannot read query history: BigQuery refused the query history'):
        Project(tmp_path).check_history()
    assert main(['--no-color', '-q', '-C', str(tmp_path), 'check-sql', '--history']) == 1
    assert 'BigQuery refused the query history' in capsys.readouterr().err


def test_pseudonyms_cannot_be_matched_across_reports() -> None:
    import konstruct.analysis.checksql as module

    first, second = (pseudonym('ann@example.com', key) for key in (b'a' * 32, b'b' * 32))
    assert first != second and first == pseudonym('ann@example.com', b'a' * 32)
    assert len(first.removeprefix('user-')) >= 12
    # Without a key, each check makes its own.
    keys: list[bytes] = []
    real = module.pseudonym

    def spy(user: str, key: bytes) -> str:
        keys.append(key)
        return real(user, key)

    history = QueryHistory([HistoryQuery('select 1', 'ann', 1)])
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, 'pseudonym', spy)
        check_history(Graph(), history, pseudonymise=True)
        check_history(Graph(), history, pseudonymise=True)
    assert len(keys) == 2 and keys[0] != keys[1]


def test_counts_read_as_words() -> None:
    assert Adoption('Net revenue', 1, 1, 1).to_dict() == {
        'term': 'Net revenue',
        'queries': 1,
        'runs': 1,
        'users': 1,
    }
    one = HistoryCheck(queries=1, runs=1).summary()
    assert one == '1 query checked (1 run); 0 shadow metrics, 0 of an approved measure.'
    assert HistoryCheck(queries=2, runs=3).summary().startswith('2 queries checked (3 runs)')


# The grain of a join, against a brute-force join of random tables


@settings(deadline=None)  # the brute-force join of the first example can take far longer than the rest
@given(st.data())
def test_a_source_said_to_keep_its_grain_never_has_a_row_repeated_within_a_group(data: st.DataObject) -> None:
    count = data.draw(st.integers(2, 4))
    aliases = [f't{i}' for i in range(count)]
    columns = st.sampled_from(['k', 'a', 'b'])
    equalities = data.draw(
        st.lists(
            # Two distinct sources, drawn directly rather than filtered, which Hypothesis may reject.
            st.tuples(st.permutations(aliases), columns, columns).map(
                lambda e: (e[0][0], e[1], e[0][1], e[2])
            ),
            max_size=5,
        )
    )
    grouped = data.draw(st.lists(st.tuples(st.sampled_from(aliases), columns), max_size=3))
    fixed: dict[str, set[str]] = {}
    for alias, column in grouped:
        fixed.setdefault(alias, set()).add(column)
    single = {a for a in aliases if data.draw(st.booleans())}
    rows = {
        a: [
            {'k': i, 'a': data.draw(st.integers(0, 2)), 'b': data.draw(st.integers(0, 2))}
            for i in range(data.draw(st.integers(0, 1 if a in single else 3)))
        ]
        for a in aliases
    }
    # Every k is unique; a source known to hold one row has the empty set as a unique set.
    unique = {a: [frozenset({'k'}), *([frozenset()] if a in single else [])] for a in aliases}
    reach = reachable(aliases, equalities, unique, fixed)
    position = {a: i for i, a in enumerate(aliases)}
    joined = [
        combination
        for combination in itertools.product(*(rows[a] for a in aliases))
        if all(combination[position[x]][xc] == combination[position[y]][yc] for x, xc, y, yc in equalities)
    ]
    for alias in aliases:
        if reach[alias] == set(aliases):
            groups: dict[tuple[Any, ...], list[int]] = {}
            for combination in joined:
                group = tuple(combination[position[a]][c] for a, c in grouped)
                groups.setdefault(group, []).append(combination[position[alias]]['k'])
            for keys in groups.values():
                assert len(keys) == len(set(keys))


def test_a_chain_of_many_to_one_joins_keeps_the_grain_of_its_first_source() -> None:
    unique = {
        'p': [frozenset({'payment_id'})],
        'o': [frozenset({'order_id'})],
        'c': [frozenset({'customer_id'})],
    }
    reach = reachable(
        ['p', 'o', 'c'],
        [('p', 'order_id', 'o', 'order_id'), ('o', 'customer_id', 'c', 'customer_id')],
        unique,
    )
    assert reach == {'p': {'p', 'o', 'c'}, 'o': {'o', 'c'}, 'c': {'c'}}
    grouped = reachable(['p', 'o'], [('p', 'order_id', 'o', 'order_id')], unique, {'p': {'payment_id'}})
    assert grouped['o'] == {'o', 'p'}


# Scale


@pytest.mark.scale
def test_checking_a_query_on_a_large_project_is_fast(tmp_path: Path) -> None:
    from tests.test_scale import make_project

    make_project(tmp_path, models=300)
    graph = Project(tmp_path).build(run_dbt=False).graph
    started = time.perf_counter()
    checker = Checker(graph)
    ready = time.perf_counter() - started
    sql = """
        select a.c00, sum(a.c01) as c01, sum(b.c02 * 2) as c04
        from wh.analytics.m299 a join wh.analytics.m150 b on a.c00 = b.c00
        where a.c03 > 0
        group by 1
    """
    started = time.perf_counter()
    for _ in range(20):
        checker.check(sql)
    per_query = (time.perf_counter() - started) / 20
    assert ready < 20 and per_query < 2, (ready, per_query)


def test_a_key_a_definition_lists_without_a_test_does_not_hide_a_fan_out(tmp_path: Path) -> None:
    """A term of kind entity binds payments.order_id as well as orders.order_id. That makes it a key
    of order, but no test or constraint makes it unique: many payments still match one order, so a
    sum of the order's amount over the join is still a fan-out."""
    definitions = (
        'version: 1\nterms:\n  - name: Order\n    kind: entity\n'
        '    columns: [orders.order_id, payments.order_id]\n'
    )
    checker = Checker(make_shop(tmp_path, definitions=definitions))
    order = next(e for e in checker.ontology.entities if e.name == 'order')
    assert any(i.endswith('payments#order_id') for i in order.key_ids)
    sql = 'select sum(o.amount) as total from raw.orders o join raw.payments p on p.order_id = o.order_id'
    assert only(checker.check(sql), 'fan-out').column == 'total'


@pytest.fixture(scope='module')
def stripe(tmp_path_factory: pytest.TempPathFactory) -> Graph:
    root = tmp_path_factory.mktemp('stripe')
    con = duckdb.connect(str(root / 'warehouse.duckdb'))
    con.execute(STRIPE)
    con.close()
    return Project(root).build(run_dbt=False).graph


def test_a_finding_says_when_it_rests_on_a_key_a_pack_presumes(stripe: Graph) -> None:
    """Stripe's ids are unique in Stripe, which the pack presumes and nothing in the project tests:
    a finding that rests on one says so."""
    sql = 'select u.email, sum(i.amount_due) from stripe.invoice i join stripe.customer u on u.email = i.id group by 1'
    assert only(check_sql(stripe, sql), 'fan-out').evidence == [
        'customer has one row per id (id presumed unique by the stripe pack), not per email, so many of its '
        'rows can match one invoice row'
    ]
    keyed = 'select sum(c.amount) from stripe.charge c join (select email from stripe.customer) x on x.email = c.id'
    assert only(check_sql(stripe, keyed), 'fan-out').evidence == [
        'charge is joined on its key (id presumed unique by the stripe pack), and nothing says the subquery '
        'x has one row per email'
    ]


def test_a_join_a_presumed_key_makes_safe_is_noted(stripe: Graph) -> None:
    result = check_sql(
        stripe, 'select sum(c.amount) from stripe.charge c join stripe.customer u on u.id = c.customer_id'
    )
    assert rules(result) == ['presumed-key']
    [note] = result.findings
    assert note.severity == 'info'
    assert note.message == (
        'the join u.id = c.customer_id is taken not to repeat rows because the stripe ontology pack '
        'presumes customer.id unique; nothing has tested it'
    )


def test_a_relationship_only_a_pack_gives_is_labelled_so(stripe: Graph) -> None:
    sql = (
        'select sum(i.amount_due) from stripe.invoice i join stripe.charge c on c.customer_id = i.customer_id'
    )
    assert only(check_sql(stripe, sql), 'fan-out').evidence == [
        'charge.customer_id refers to stripe_customer (charge.customer_id = customer.id), as the stripe pack '
        'gives it, so many charge rows can match one invoice row'
    ]
