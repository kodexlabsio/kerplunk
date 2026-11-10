"""`verify --why`: the source rows behind one value, each with its share, and the rows left out."""

from __future__ import annotations

import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from konstruct import Project
from konstruct.analysis.query import QueryError, resolve_one
from konstruct.analysis.verify import DbApiSource, DuckDBEngine, SampledEngine
from konstruct.analysis.why import Why, _Composer, parse_where, why

CUSTOMERS = [(1, 'north'), (2, 'south'), (3, 'north'), (4, 'east')]
ORDERS: list[tuple[int, int, int | None, str]] = [
    (1, 1, 10, 'ok'),
    (2, 1, 25, 'ok'),
    (3, 1, 7, 'void'),
    (4, 1, None, 'ok'),
    (5, 2, 40, 'ok'),
    (6, 2, 5, 'void'),
    (7, 3, 12, 'ok'),
    (8, 9, 30, 'ok'),  # customer 9 does not exist
    (9, 9, 1, 'void'),
]

MODELS = {
    'ltv.sql': """
create table main.ltv as
select
    c.id as customer_id,
    sum(o.amount) as total,
    count(*) as orders,
    count(o.amount) as priced,
    max(o.amount) as biggest,
    min(o.amount) as smallest,
    avg(o.amount) as mean,
    median(o.amount) as middle,
    count(distinct o.status) as statuses,
    coalesce(sum(o.amount), 0) + 1 as total_plus_one,
    coalesce(sum(o.amount), 0) as total_or_zero,
    sum(o.amount) filter (where o.amount > 10) as large,
    count(*) filter (where o.amount > 10) as large_orders,
    sum(cast(o.amount as decimal(10, 2))) as exact
from raw.customers c
join raw.orders o on o.customer_id = c.id
where o.status <> 'void'
group by c.id
""",
    'stg.sql': """
create table main.stg_orders as
select id as order_id, customer_id, amount * 2 as doubled
from raw.orders
where status <> 'void'
""",
    'mart.sql': """
create table main.mart as
with t as (select customer_id, doubled from main.stg_orders)
select customer_id, sum(doubled) as total from t group by customer_id
""",
    'spend.sql': """
create table main.spend as
select c.id as customer_id, c.region, sum(o.amount) as total
from raw.customers c
left join raw.orders o on o.customer_id = c.id and o.status = 'ok'
group by c.id, c.region
""",
    'regions.sql': """
create table main.regions as
select c.region, sum(l.total) as total, count(*) as customers
from main.ltv l
join raw.customers c on c.id = l.customer_id
group by c.region
""",
    'numbered.sql': """
create table main.numbered as
select id as order_id, row_number() over (partition by customer_id order by id) as place
from raw.orders
""",
    'latest.sql': """
create table main.latest as
select id as order_id, customer_id, amount from raw.orders
qualify row_number() over (partition by customer_id order by id desc) = 1
""",
    'ranked.sql': """
create table main.ranked as
with g as (select customer_id, sum(amount) as total from raw.orders group by customer_id),
r as (select customer_id, total, rank() over (order by total) as place from g)
select place, sum(total) as total from r group by place
""",
    'righted.sql': """
create table main.righted as
select c.id as customer_id, sum(o.amount) as total
from raw.orders o right join raw.customers c on o.customer_id = c.id
group by c.id
""",
    'big.sql': """
create table main.big as
select customer_id, sum(amount) as total from raw.orders where status = 'ok' group by customer_id having sum(amount) > 20
""",
    'bigsum.sql': """
create table main.bigsum as
select customer_id, sum(total) as total from main.big group by customer_id
""",
    'recursive.sql': """
create table main.recursive as
with recursive r(n) as (select 1 union all select n + 1 from r where n < 3) select n from r
""",
    'statuses.sql': """
create table main.statuses as select distinct customer_id, status from raw.orders
""",
    'status_counts.sql': """
create table main.status_counts as select customer_id, count(*) as statuses from main.statuses group by customer_id
""",
    'paired.sql': """
create table main.paired as
select c.region, t.total from raw.customers c cross join (select sum(amount) as total from raw.orders) t
""",
    'both.sql': """
create table main.both_ids as
select id from raw.customers union all select id from raw.orders
""",
    'comma.sql': """
create table main.comma as
select c.id as customer_id, sum(o.amount) as total
from raw.customers c, raw.orders o
where o.customer_id = c.id and o.status <> 'void'
group by c.id
""",
    'on_true.sql': """
create table main.on_true as
select c.id as customer_id, sum(o.amount) as total
from raw.customers c join raw.orders o on true
where o.customer_id = c.id and o.status <> 'void'
group by c.id
""",
    'above.sql': """
create table main.above as
select customer_id, sum(amount) as total from raw.orders
where amount > (select avg(amount) from raw.orders)
group by customer_id
""",
    'firsts.sql': """
create table main.firsts as
select customer_id, sum(amount) as total from (select * from raw.orders order by id limit 5) o
group by customer_id
""",
    'crossed.sql': """
create table main.crossed as
select o.customer_id, sum(o.amount * r.rate) as total
from raw.orders o cross join raw.rates r
group by o.customer_id
""",
    'windowed.sql': """
create table main.windowed as
select customer_id, id as order_id, sum(amount) over (partition by customer_id) as cust_total,
    sum(amount) over (partition by customer_id, id) as order_total
from raw.orders
where status <> 'void'
""",
    'don.sql': """
create table main.don as
select distinct on (customer_id) customer_id, id as order_id, amount
from raw.orders
where status <> 'void'
order by customer_id, amount desc nulls last
""",
    'don_up.sql': """
create table main.don_up as select customer_id, amount * 2 as doubled from main.don
""",
    'using_j.sql': """
create table main.using_j as
with c as (select id as customer_id, region from raw.customers)
select customer_id, sum(amount) as total from raw.orders join c using (customer_id) group by customer_id
""",
    'rolled.sql': """
create table main.rolled as
select customer_id, sum(amount) as total from raw.orders group by rollup (customer_id)
""",
    'semi.sql': """
create table main.semi as
select o.customer_id, sum(o.amount) as total
from raw.orders o semi join raw.customers c on c.id = o.customer_id
group by o.customer_id
""",
}


def load(path: Path, orders: list[tuple[int, int, int | None, str]]) -> None:
    con = duckdb.connect(str(path))
    con.execute('create schema if not exists raw')
    con.execute('create or replace table raw.customers (id integer, region varchar)')
    con.executemany('insert into raw.customers values (?, ?)', CUSTOMERS)
    con.execute(
        'create or replace table raw.orders (id integer, customer_id integer, amount integer, status varchar)'
    )
    if orders:
        con.executemany('insert into raw.orders values (?, ?, ?, ?)', orders)
    con.execute('create or replace table raw.rates (rate integer)')  # empty: a cross join of it has no rows
    con.close()


def make_project(root: Path, orders: list[tuple[int, int, int | None, str]] = ORDERS) -> Project:
    load(root / 'shop.duckdb', orders)
    for name, sql in MODELS.items():
        (root / name).write_text(sql)
    project = Project(root)
    project.build()
    return project


@pytest.fixture(scope='module')
def shop(tmp_path_factory: pytest.TempPathFactory) -> Project:
    return make_project(tmp_path_factory.mktemp('why'))


def run(project: Project, ref: str, **where: Any) -> Why:
    return why(
        project.graph, project.root, project.root / 'shop.duckdb', ref, {k: str(v) for k, v in where.items()}
    )


def ids(result: Why, relation: str = 'orders') -> list[int]:
    return sorted(int(r.values['id']) for c in result.contributions for r in c.rows if r.relation == relation)


def excluded(result: Why, relation: str = 'orders') -> dict[int, str]:
    return {int(e.row.values['id']): e.reason for e in result.excluded if e.row.relation == relation}


def test_sum_lists_each_addend_and_the_rows_a_filter_left_out(shop: Project) -> None:
    result = run(shop, 'ltv.total', customer_id=1)
    assert result.value == 35 and result.matches == 1
    [aggregate] = result.aggregates
    assert (aggregate.function, aggregate.decomposable, aggregate.total) == ('SUM', True, 35)
    shares = {int(c.rows[0].values['id']): c.values[0] for c in result.contributions}
    assert shares == {1: 10, 2: 25, 4: None}
    assert result.consistent is True
    assert ids(result, 'customers') == [1, 1, 1]
    assert excluded(result) == {3: "ltv: WHERE o.status <> 'void'"}
    assert result.step == 'ltv'


def test_count_gives_one_each_and_none_for_a_null(shop: Project) -> None:
    rows = run(shop, 'ltv.orders', customer_id=1)
    assert rows.value == 3 and [c.values for c in rows.contributions] == [[1], [1], [1]]
    assert rows.consistent is True
    priced = run(shop, 'ltv.priced', customer_id=1)
    assert priced.value == 2 and sorted(c.values[0] for c in priced.contributions) == [0, 1, 1]
    assert priced.aggregates[0].count == 2 and priced.consistent is True


def test_max_and_min_mark_the_row_that_won(shop: Project) -> None:
    biggest = run(shop, 'ltv.biggest', customer_id=1)
    assert biggest.value == 25 and biggest.consistent is True
    [winner] = [c for c in biggest.contributions if c.chosen]
    assert winner.rows[0].values['id'] == 2 and biggest.contributions[0] is winner
    smallest = run(shop, 'ltv.smallest', customer_id=1)
    assert [c.rows[0].values['id'] for c in smallest.contributions if c.chosen] == [1]
    assert 'MAX(o.amount) takes the largest of 3 rows: 25, marked *.' in str(biggest)
    assert biggest.to_dict()['contributions'][0]['chosen'] is True


def test_avg_lists_each_addend_and_the_count(shop: Project) -> None:
    result = run(shop, 'ltv.mean', customer_id=1)
    assert result.value == 17.5 and result.consistent is True
    [aggregate] = result.aggregates
    assert aggregate.count == 2 and aggregate.total == 17.5
    assert '35 / 2 = 17.5' in str(result)


@pytest.mark.parametrize(('column', 'function'), [('middle', 'MEDIAN'), ('statuses', 'COUNT')])
def test_median_and_distinct_are_not_decomposable(shop: Project, column: str, function: str) -> None:
    result = run(shop, f'ltv.{column}', customer_id=1)
    [aggregate] = result.aggregates
    assert aggregate.function == function and not aggregate.decomposable and not result.decomposable
    assert result.consistent is None and aggregate.total is None
    assert result.contributors == 3
    assert 'is not decomposable' in str(result)


def test_a_join_that_found_no_match_is_the_reason(shop: Project) -> None:
    result = run(shop, 'ltv.total', customer_id=9)
    assert result.matches == 0 and result.value is None and result.contributors == 0
    assert excluded(result) == {
        8: 'ltv: JOIN o ON o.customer_id = c.id found no match',
        9: 'ltv: JOIN o ON o.customer_id = c.id found no match',
    }
    assert 'No row of ltv has customer_id = 9' in str(result)


def test_a_left_join_condition_is_the_reason(shop: Project) -> None:
    result = run(shop, 'spend.total', customer_id=2)
    assert result.value == 40 and ids(result) == [5]
    assert excluded(result) == {
        6: "spend: LEFT JOIN o ON o.customer_id = c.id AND o.status = 'ok' found no match"
    }
    empty = run(shop, 'spend.total', customer_id=4)
    assert empty.matches == 1 and empty.value is None and empty.contributors == 1
    assert [r.relation for c in empty.contributions for r in c.rows] == ['customers']


def test_a_two_model_chain_reaches_the_source_rows(shop: Project) -> None:
    result = run(shop, 'mart.total', customer_id=1)
    assert result.value == 70 and result.consistent is True
    assert {int(c.rows[0].values['id']): c.values[0] for c in result.contributions} == {1: 20, 2: 50, 4: None}
    assert excluded(result) == {3: "stg_orders: WHERE orders.status <> 'void'"}
    assert result.composed == 'SUM(orders.amount * 2)'


def test_an_aggregate_of_an_aggregate_lists_the_inner_rows(shop: Project) -> None:
    result = run(shop, 'regions.total', region='north')
    assert result.value == 47 and result.consistent is True
    by_customer = sorted(
        (c.values[0], sorted(r.values['id'] for r in c.rows if r.relation == 'orders'))
        for c in result.contributions
    )
    assert by_customer == [(12, [7]), (35, [1, 2, 4])]
    count = run(shop, 'regions.customers', region='north')
    assert count.value == 2 and count.contributors == 2 and count.consistent is True


def test_a_value_computed_row_by_row(shop: Project) -> None:
    result = run(shop, 'stg_orders.doubled', order_id=2)
    assert result.value == 50 and result.aggregates == [] and result.consistent is True
    [row] = result.contributions
    assert row.values == [50] and row.rows[0].values == {
        'id': 2,
        'customer_id': 1,
        'amount': 25,
        'status': 'ok',
    }
    assert row.rows[0].shown == ['id', 'amount']
    assert 'The row gives the value, 50.' in str(result)
    gone = run(shop, 'stg_orders.doubled', order_id=3)
    assert gone.matches == 0 and gone.contributors == 0
    assert excluded(gone) == {3: "stg_orders: WHERE orders.status <> 'void'"}


def test_an_expression_over_the_aggregate_is_not_checked(shop: Project) -> None:
    result = run(shop, 'ltv.total_plus_one', customer_id=1)
    assert result.value == 36 and result.aggregates[0].total == 35 and result.consistent is None
    assert run(shop, 'ltv.total_or_zero', customer_id=1).consistent is True


def test_filtered_and_decimal_aggregates(shop: Project) -> None:
    large = run(shop, 'ltv.large', customer_id=1)
    assert large.value == 25 and large.consistent is True
    assert large.aggregates[0].sql == 'SUM(o.amount) FILTER(WHERE o.amount > 10)'
    assert sorted(c.values[0] for c in large.contributions if c.values[0] is not None) == [25]
    counted = run(shop, 'ltv.large_orders', customer_id=1)
    assert counted.value == 1 and sorted(c.values[0] for c in counted.contributions) == [0, 0, 1]
    exact = run(shop, 'ltv.exact', customer_id=1)
    assert exact.value == Decimal('35.00') and exact.consistent is True
    assert exact.to_dict()['value'] == 35


def test_a_distinct_step_is_grouped(shop: Project) -> None:
    result = run(shop, 'status_counts.statuses', customer_id=1)
    assert result.value == 2 and result.contributors == 2 and result.consistent is True
    rows = sorted(sorted(r.values['id'] for r in c.rows) for c in result.contributions)
    assert rows == [[1, 2, 4], [3]]


def test_a_refused_exclusion_query_is_a_note(shop: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    from konstruct.analysis.verify import DuckDBEngine

    real = DuckDBEngine.run

    def refuse(self: DuckDBEngine, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        if 'FULL JOIN' in sql:
            raise RuntimeError('FULL JOIN is only supported with merge-joinable join conditions')
        return real(self, sql)

    monkeypatch.setattr(DuckDBEngine, 'run', refuse)
    result = run(shop, 'ltv.total', customer_id=1)
    assert result.value == 35 and result.consistent is True and result.exclusions == 0
    assert result.notes == [
        'Rows left out are not traced: the warehouse refused the query '
        '(FULL JOIN is only supported with merge-joinable join conditions).'
    ]


def test_a_window_function_is_not_decomposable(shop: Project) -> None:
    result = run(shop, 'numbered.place', order_id=2)
    assert result.value == 2 and result.contributors == 1 and result.consistent is True
    assert result.contributions[0].values == [2] and result.contributions[0].rows[0].values['id'] == 2
    assert any('window function' in note for note in result.notes)


def test_a_deduplication_is_the_reason(shop: Project) -> None:
    result = run(shop, 'latest.amount', customer_id=1)
    assert result.matches == 1 and result.value is None and ids(result) == [4]
    reason = 'latest: QUALIFY ROW_NUMBER() OVER (PARTITION BY orders.customer_id ORDER BY orders.id DESC) = 1'
    assert excluded(result) == {1: reason, 2: reason, 3: reason}


def test_a_right_join_keeps_its_contributions_but_not_its_exclusions(shop: Project) -> None:
    result = run(shop, 'righted.total', customer_id=1)
    assert result.value == 42 and result.consistent is True and ids(result) == [1, 2, 3, 4]
    assert result.notes == ['Rows left out are not traced: the computing step uses a RIGHT JOIN in righted.']


def test_a_having_condition_is_the_reason(shop: Project) -> None:
    kept = run(shop, 'bigsum.total', customer_id=1)
    assert kept.value == 35 and kept.consistent is True and set(excluded(kept)) == {3}
    result = run(shop, 'bigsum.total', customer_id=3)
    assert result.matches == 0 and result.contributors == 0
    assert excluded(result) == {7: 'big: HAVING SUM(orders.amount) > 20'}
    direct = run(shop, 'big.total', customer_id=3)
    assert direct.matches == 0 and ids(direct) == [7]
    assert 'A filter may remove the row: big: HAVING SUM(orders.amount) > 20.' in direct.notes


def test_the_cap_is_reported(shop: Project) -> None:
    result = why(
        shop.graph, shop.root, shop.root / 'shop.duckdb', 'ltv.orders', {'customer_id': '1'}, limit=2
    )
    assert result.contributors == 3 and len(result.contributions) == 2 and result.capped
    assert 'showing 2 of 3; raise the limit to see more' in str(result)
    assert result.to_dict()['capped'] is True


def test_to_dict_and_text(shop: Project) -> None:
    result = run(shop, 'ltv.total', customer_id=1)
    data = result.to_dict()
    assert data['value'] == 35 and data['contributors'] == 3 and data['exclusions'] == 1
    assert data['aggregates'] == [
        {
            'sql': 'SUM(o.amount)',
            'function': 'SUM',
            'decomposable': True,
            'total': 35,
            'count': 2,
            'addends': None,
        }
    ]
    assert data['excluded'][0]['values'] == {'id': 3, 'customer_id': 1, 'amount': 7, 'status': 'void'}
    assert data['explanation'][0] == 'SUM(o.amount) adds up 3 rows: their addends total 35.'
    text = str(result)
    assert text.startswith('ltv.total where customer_id = 1: 35\n  = SUM(orders.amount)')
    assert "orders id=3 amount=7  ltv: WHERE o.status <> 'void'" in text


def test_where_options_are_parsed() -> None:
    assert parse_where(['customer_id=42', ' month = 2024-01 ']) == {'customer_id': '42', 'month': '2024-01'}
    for bad in (['customer_id'], ['=4'], []):
        with pytest.raises(QueryError):
            parse_where(bad)


@pytest.mark.parametrize(
    ('ref', 'where', 'message'),
    [
        ('ltv', {'customer_id': '1'}, 'is a table'),
        ('ltv.total', {}, 'give the row with --where'),
        ('ltv.total', {'nope': '1'}, 'nope is not a column of the model'),
        ('spend.total', {'region': 'north'}, '2 rows of spend match region = north'),
        ('ltv.total', {'orders': '3'}, 'orders is computed by an aggregate'),
        ('orders.amount', {'id': '1'}, 'is not built by SQL that verify can rerun'),
        ('both_ids.id', {'id': '1'}, 'the SQL uses a UNION'),
        ('ranked.total', {'place': '1'}, 'uses a window function over grouped rows in ranked'),
        ('recursive.n', {'n': '1'}, 'uses a recursive CTE'),
        ('paired.total', {'region': 'north'}, 'cannot follow region into t in paired'),
    ],
)
def test_what_cannot_be_explained_is_refused(
    shop: Project, ref: str, where: dict[str, str], message: str
) -> None:
    with pytest.raises(QueryError, match=message):
        why(shop.graph, shop.root, shop.root / 'shop.duckdb', ref, where)


def test_a_sample_says_it_is_one(shop: Project) -> None:
    source = DbApiSource(duckdb.connect(str(shop.root / 'shop.duckdb'), read_only=True), 'databricks')
    engine = SampledEngine(source, 'databricks (sampled)', shop.graph)
    result = why(shop.graph, shop.root, engine, 'ltv.total', {'customer_id': '1'}, max_rows=4)
    assert result.warehouse == 'databricks (sampled)'
    assert result.value == 35 and result.consistent is True
    assert any('only the first 4 were read' in note for note in result.notes)


# A join written as a WHERE equality, and the rows each copy reads


@pytest.mark.parametrize('model', ['comma', 'on_true'])
def test_a_join_through_a_where_equality_relates_only_its_own_rows(shop: Project, model: str) -> None:
    """``FROM a, b WHERE a.k = b.k`` is a join: the rows it leaves out are those of the key alone."""
    result = run(shop, f'{model}.total', customer_id=1)
    assert result.value == 35 and ids(result) == [1, 2, 4]
    assert excluded(result) == {3: f"{model}: WHERE o.status <> 'void'"}
    assert excluded(result, 'customers') == {}
    missing = run(shop, f'{model}.total', customer_id=9)
    assert missing.matches == 0
    assert excluded(missing) == {
        8: f'{model}: WHERE o.customer_id = c.id found no match',
        9: f'{model}: WHERE o.customer_id = c.id found no match',
    }


class Spy(DuckDBEngine):
    """A DuckDB engine that records each copy it is asked for."""

    def __init__(self, warehouse: Path) -> None:
        super().__init__(warehouse)
        self.copies: list[tuple[str, str | None]] = []

    def copy(
        self, key: str, index: int, max_rows: int, where: str | None = None
    ) -> tuple[str, list[tuple[str, str]]]:
        self.copies.append((key, where))
        return super().copy(key, index, max_rows, where)


def spied(project: Project, ref: str, **where: Any) -> tuple[Why, dict[str, str | None]]:
    spy = Spy(project.root / 'shop.duckdb')
    result = why(project.graph, project.root, spy, ref, {k: str(v) for k, v in where.items()})
    # The first copy of each source only reads its columns; the second holds the rows.
    return result, {key: condition for key, condition in spy.copies if condition != 'FALSE'}


def test_the_key_is_pushed_into_each_copy(shop: Project) -> None:
    result, copies = spied(shop, 'ltv.total', customer_id=1)
    assert result.value == 35 and excluded(result) == {3: "ltv: WHERE o.status <> 'void'"}
    assert copies == {'shop.raw.customers': '"id" = \'1\'', 'shop.raw.orders': '"customer_id" = \'1\''}
    # Through a model in between, and through a grouped step keyed by its group.
    _, chain = spied(shop, 'mart.total', customer_id=1)
    assert chain == {'shop.raw.orders': '"customer_id" = \'1\''}
    missing, unmatched = spied(shop, 'comma.total', customer_id=9)
    assert set(excluded(missing)) == {8, 9} and unmatched['shop.raw.orders'] == '"customer_id" = \'9\''


def test_a_source_read_beyond_the_key_is_copied_whole(shop: Project) -> None:
    """An average over every order decides which orders count, so the orders are not cut to the key."""
    result, copies = spied(shop, 'above.total', customer_id=1)
    con = duckdb.connect(str(shop.root / 'shop.duckdb'), read_only=True)
    try:
        [(expected,)] = con.execute(
            'select sum(amount) from raw.orders where customer_id = 1 '
            'and amount > (select avg(amount) from raw.orders)'
        ).fetchall()
    finally:
        con.close()
    assert result.value == expected == 25 and copies == {'shop.raw.orders': None}
    numbered, whole = spied(shop, 'numbered.place', order_id=2)
    assert numbered.value == 2 and whole == {'shop.raw.orders': None}
    assert any('orders was read whole' in n and 'a window function in numbered' in n for n in numbered.notes)
    firsts, cut = spied(shop, 'firsts.total', customer_id=1)
    assert cut == {'shop.raw.orders': None}
    assert any('a LIMIT in firsts' in n for n in firsts.notes)


def test_rows_a_limit_or_a_join_with_no_condition_drops_are_noted(shop: Project) -> None:
    firsts = run(shop, 'firsts.total', customer_id=2)
    assert firsts.value == 40 and ids(firsts) == [5]
    assert any('firsts keeps only some rows (LIMIT)' in note for note in firsts.notes)
    crossed = run(shop, 'crossed.total', customer_id=1)
    assert crossed.matches == 0 and crossed.exclusions == 0
    assert any('crossed: CROSS JOIN r has no condition' in note for note in crossed.notes)


def test_every_key_of_the_output_is_always_there(shop: Project) -> None:
    contributions = run(shop, 'ltv.total', customer_id=1).to_dict()['contributions']
    assert {c['chosen'] for c in contributions} == {False}
    assert run(shop, 'ltv.total', customer_id=1).to_dict()['aggregates'][0]['addends'] is None
    assert run(shop, 'ltv.mean', customer_id=1).to_dict()['aggregates'][0]['addends'] == 35


def test_a_model_that_is_not_one_query_that_reads_is_refused(shop: Project, tmp_path: Path) -> None:
    """Nothing of a model runs unless its SQL is a single query that only reads."""
    from konstruct.analysis.verify import Candidate

    node = shop.graph.parent_of(resolve_one(shop.graph, 'ltv.total'))
    assert node is not None and node.relation
    written = tmp_path / 'written.csv'
    candidate = Candidate(node, f"select 1 as total; copy (select 1) to '{written}'", None, None)
    with pytest.raises(QueryError, match='ltv is not a query that only reads: the model is 2 statements'):
        _Composer(shop.graph, {node.relation: candidate}, 'duckdb', 'duckdb').query(candidate)
    assert not written.exists()


def test_each_model_is_composed_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A diamond of models reads each model twice; each is still rewritten once, not once per path."""
    load(tmp_path / 'shop.duckdb', ORDERS)
    (tmp_path / 'd0.sql').write_text('create table main.d0 as select id, customer_id, amount from raw.orders')
    depth = 6
    for level in range(1, depth + 1):
        (tmp_path / f'd{level}.sql').write_text(
            f'create table main.d{level} as select a.id, a.customer_id, a.amount + b.amount as amount '
            f'from main.d{level - 1} a join main.d{level - 1} b on a.id = b.id'
        )
    (tmp_path / 'top.sql').write_text(
        f'create table main.top as select customer_id, sum(amount) as total from main.d{depth} group by customer_id'
    )
    project = Project(tmp_path)
    project.build()
    calls: list[str] = []
    from konstruct.analysis.verify import rewrite as original

    def counted(sql: str, *args: Any, **kwargs: Any) -> tuple[str, set[str]]:
        calls.append(sql)
        return original(sql, *args, **kwargs)

    monkeypatch.setattr('konstruct.analysis.why.rewrite', counted)
    result = why(project.graph, project.root, project.root / 'shop.duckdb', 'top.total', {'customer_id': '1'})
    assert result.value == 42 * 2**depth and result.consistent is True  # orders 1, 3 and 4 of customer 1
    # Once for each model, and once more for d0, the one that reads a source.
    assert len(calls) == depth + 3


def _without_pushdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Copy every source whole, the oracle push-down must agree with."""
    monkeypatch.setattr('konstruct.analysis.why._pushdown', lambda tree, where: ({}, {}))


def _answer(result: Why) -> dict[str, Any]:
    """What a result says about the rows, leaving out row ids (positions in a copy, which a filtered
    copy numbers differently) and the two notes about how the copies were made."""
    data = result.to_dict()
    data.pop('seconds')
    data['explanation'] = [
        line
        for line in data['explanation']
        if 'was read whole' not in line and not re.search(r'has more than \d+ rows', line)
    ]
    for item in [*data['contributions'], *data['excluded']]:
        for row in item.get('rows', [item]):
            row.pop('row_id')
    data['contributions'].sort(key=repr)
    data['excluded'].sort(key=repr)
    return data


def test_a_window_partitioned_by_part_of_the_key_reads_every_row_of_that_part(
    shop: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window sums each customer's orders: the copy keeps every order of customer 1, not only
    order 1, which the finer key names."""
    result, copies = spied(shop, 'windowed.cust_total', customer_id=1, order_id=1)
    assert result.value == 35 and copies == {'shop.raw.orders': '"customer_id" = \'1\''}
    _without_pushdown(monkeypatch)
    assert _answer(result) == _answer(run(shop, 'windowed.cust_total', customer_id=1, order_id=1))


def test_distinct_on_keeps_the_first_row_of_each_key(shop: Project) -> None:
    """DISTINCT ON keeps one row per ON key, the first by ORDER BY: that row is the contributor, and
    the others of the key are left out by the DISTINCT ON, not grouped with it."""
    result, copies = spied(shop, 'don.amount', customer_id=1)
    assert result.value == 25 and ids(result) == [2] and result.consistent is True
    assert excluded(result) == {
        1: 'don: DISTINCT ON (customer_id) kept another row',
        3: "don: WHERE orders.status <> 'void'",
        4: 'don: DISTINCT ON (customer_id) kept another row',
    }
    assert copies == {'shop.raw.orders': '"customer_id" = \'1\''}
    above = run(shop, 'don_up.doubled', customer_id=1)
    assert above.value == 50 and ids(above) == [2]


def test_a_using_key_copies_its_source_whole_and_says_why(shop: Project) -> None:
    result, copies = spied(shop, 'using_j.total', customer_id=1)
    assert result.value == 42 and copies['shop.raw.orders'] is None
    assert any(
        'orders was read whole' in note and 'customer_id is not a plain column in using_j' in note
        for note in result.notes
    )


@pytest.mark.parametrize(
    ('ref', 'message'),
    [('rolled.total', 'ROLLUP, CUBE or GROUPING SETS in rolled'), ('semi.total', 'a SEMI JOIN in semi')],
)
def test_grouping_sets_and_semi_joins_are_refused(shop: Project, ref: str, message: str) -> None:
    with pytest.raises(QueryError, match=message):
        run(shop, ref, customer_id=1)


WHY_CASES = [
    ('ltv.total', {'customer_id': 1}),
    ('ltv.total', {'customer_id': 9}),
    ('ltv.orders', {'customer_id': 1}),
    ('ltv.biggest', {'customer_id': 1}),
    ('ltv.mean', {'customer_id': 1}),
    ('ltv.middle', {'customer_id': 1}),
    ('ltv.large', {'customer_id': 1}),
    ('spend.total', {'customer_id': 2}),
    ('spend.total', {'customer_id': 4}),
    ('mart.total', {'customer_id': 1}),
    ('regions.total', {'region': 'north'}),
    ('stg_orders.doubled', {'order_id': 2}),
    ('numbered.place', {'order_id': 2}),
    ('latest.amount', {'customer_id': 1}),
    ('big.total', {'customer_id': 3}),
    ('bigsum.total', {'customer_id': 1}),
    ('status_counts.statuses', {'customer_id': 1}),
    ('righted.total', {'customer_id': 1}),
    ('comma.total', {'customer_id': 1}),
    ('on_true.total', {'customer_id': 9}),
    ('above.total', {'customer_id': 1}),
    ('firsts.total', {'customer_id': 2}),
    ('windowed.cust_total', {'customer_id': 1, 'order_id': 4}),
    ('windowed.order_total', {'customer_id': 2, 'order_id': 5}),
    ('don.amount', {'customer_id': 1}),
    ('don.amount', {'customer_id': 2}),
    ('don_up.doubled', {'customer_id': 1}),
    ('using_j.total', {'customer_id': 1}),
]


@pytest.mark.parametrize(('ref', 'where'), WHY_CASES)
def test_pushing_the_key_down_changes_no_answer(
    shop: Project, monkeypatch: pytest.MonkeyPatch, ref: str, where: dict[str, Any]
) -> None:
    pushed = run(shop, ref, **where)
    _without_pushdown(monkeypatch)
    assert _answer(pushed) == _answer(run(shop, ref, **where))


# Property: on any data, the shares of SUM and COUNT add up to the value, and the contributing and
# excluded rows are exactly those a brute-force reading of the SQL gives.

orders_data = st.lists(
    st.tuples(st.integers(1, 6), st.one_of(st.none(), st.integers(-50, 50)), st.sampled_from(['ok', 'void'])),
    max_size=25,
).map(lambda rows: [(i + 1, c, a, s) for i, (c, a, s) in enumerate(rows)])


@settings(
    max_examples=int(os.environ.get('KONSTRUCT_WHY_EXAMPLES', '15')),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(orders_data)
def test_shares_add_up_to_the_value_on_any_data(
    shop: Project, orders: list[tuple[int, int, int | None, str]]
) -> None:
    load(shop.root / 'shop.duckdb', orders)
    try:
        for customer, _ in CUSTOMERS:
            kept = [o for o in orders if o[1] == customer and o[3] != 'void']
            void = {o[0] for o in orders if o[1] == customer and o[3] == 'void'}
            total = run(shop, 'ltv.total', customer_id=customer)
            count = run(shop, 'ltv.orders', customer_id=customer)
            joined = [run(shop, f'{model}.total', customer_id=customer) for model in ('comma', 'on_true')]
            for result in (total, count, *joined):
                assert ids(result) == sorted(o[0] for o in kept)
                assert set(excluded(result)) == void
                # The customer's own row is left out exactly when no order of theirs is kept.
                assert set(excluded(result, 'customers')) == (set() if kept else {customer})
            if not kept:
                assert total.matches == 0 and count.matches == 0
                continue
            amounts = [o[2] for o in kept if o[2] is not None]
            assert total.value == (sum(amounts) if amounts else None)
            assert total.aggregates[0].total == total.value
            assert total.consistent is (True if amounts else None)
            assert count.value == len(kept) == sum(c.values[0] for c in count.contributions)
            assert count.consistent is True
    finally:
        load(shop.root / 'shop.duckdb', ORDERS)


# The example project


def test_every_customer_lifetime_value_in_the_example(jaffle_built_graph: Path) -> None:
    project = Project(jaffle_built_graph)
    warehouse = jaffle_built_graph / 'jaffle_shop.duckdb'
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        values = dict(
            con.execute('select customer_id, customer_lifetime_value from main.customers').fetchall()
        )
        counts = dict(con.execute('select customer_id, number_of_orders from main.customers').fetchall())
    finally:
        con.close()
    for customer, value in values.items():
        result = why(
            project.graph,
            project.root,
            warehouse,
            'customers.customer_lifetime_value',
            {'customer_id': str(customer)},
        )
        assert result.value == value
        if value is None:
            assert result.contributors == 0
            continue
        assert result.consistent is True and result.composed == 'SUM(raw_payments.amount / 100)'
        assert sum(Decimal(str(c.values[0])) for c in result.contributions) == Decimal(str(value))
        assert {r.relation for c in result.contributions for r in c.rows} == {'raw_payments', 'raw_orders'}
    for customer in (1, 3, 55):
        result = why(
            project.graph,
            project.root,
            warehouse,
            'customers.number_of_orders',
            {'customer_id': str(customer)},
        )
        assert result.value == counts[customer]
        assert result.contributors == (counts[customer] or 0)


def test_customer_ltv_in_the_example(jaffle_built_graph: Path) -> None:
    project = Project(jaffle_built_graph)
    warehouse = jaffle_built_graph / 'jaffle_shop.duckdb'
    result = why(project.graph, project.root, warehouse, 'customer_ltv.lifetime_value', {'customer_id': '3'})
    assert result.value == 65 and result.step == 'customers' and result.consistent is True
    assert sorted(
        r.values['id'] for c in result.contributions for r in c.rows if r.relation == 'raw_payments'
    ) == [
        2,
        27,
        79,
    ]
    missing = why(project.graph, project.root, warehouse, 'customer_ltv.lifetime_value', {'customer_id': '4'})
    assert missing.matches == 0
    assert any('customer_ltv: WHERE c.customer_lifetime_value > 0' in note for note in missing.notes)


@pytest.mark.postgres
def test_why_on_postgres_leaves_nothing_behind(tmp_path: Path) -> None:
    """The same answers from Postgres, in a transaction that is rolled back."""
    import uuid

    import psycopg

    from .test_warehouse import require

    dsn = require('KONSTRUCT_TEST_PG_DSN')
    with psycopg.connect(dsn) as pg:
        database = str(pg.execute('select current_database()').fetchone()[0])  # type: ignore[index]
    schema = f'kp_why_{uuid.uuid4().hex[:8]}'
    local = duckdb.connect(str(tmp_path / f'{database}.duckdb'))
    local.execute(f'create schema {schema}')
    local.execute(f'create table {schema}.customers (id integer, region varchar)')
    local.execute(
        f'create table {schema}.orders (id integer, customer_id integer, amount integer, status varchar)'
    )
    local.close()
    (tmp_path / 'ltv.sql').write_text(MODELS['ltv.sql'].replace('raw.', f'{schema}.'))
    (tmp_path / 'spend.sql').write_text(MODELS['spend.sql'].replace('raw.', f'{schema}.'))
    project = Project(tmp_path)
    project.build()
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(f'create schema {schema}')
        pg.execute(f'create table {schema}.customers (id integer, region varchar)')
        pg.execute(
            f'create table {schema}.orders (id integer, customer_id integer, amount integer, status varchar)'
        )
        with pg.cursor() as cursor:
            cursor.executemany(f'insert into {schema}.customers values (%s, %s)', CUSTOMERS)
            cursor.executemany(f'insert into {schema}.orders values (%s, %s, %s, %s)', ORDERS)
    try:
        result = why(project.graph, project.root, dsn, 'ltv.total', {'customer_id': '1'})
        assert result.warehouse == f'postgres database {database}'
        assert result.value == 35 and result.consistent is True
        assert {int(c.rows[0].values['id']): c.values[0] for c in result.contributions} == {
            1: 10,
            2: 25,
            4: None,
        }
        assert excluded(result) == {3: "ltv: WHERE o.status <> 'void'"}
        missing = why(project.graph, project.root, dsn, 'ltv.total', {'customer_id': '9'})
        assert set(excluded(missing)) == {8, 9}
        spend = why(project.graph, project.root, dsn, 'spend.total', {'customer_id': '2'})
        assert spend.value == 40 and ids(spend) == [5] and set(excluded(spend)) == {6}
        with psycopg.connect(dsn) as pg:
            leftovers = pg.execute("select count(*) from pg_class where relname like 'kp\\_%'").fetchone()
        assert leftovers == (0,)
    finally:
        with psycopg.connect(dsn, autocommit=True) as pg:
            pg.execute(f'drop schema if exists {schema} cascade')
