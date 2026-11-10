"""check-sql over a corpus of analyst queries: correct SQL must pass untouched, buggy SQL must not.

An agent told that its correct SQL is wrong will break it, so the correct queries are the larger
half: CTEs, windows, pre-aggregation, semi-joins, DISTINCT, UNION, CASE and FILTER aggregation,
wrappers, scalar subqueries and date filters written in different ways. Each must give no error
and no warning (a note is allowed). Each buggy query must give a warning or an error of its rule.

The queries run over the shop of ``test_checksql.py``, a shop whose customers belong to segments
through a bridge table and the jaffle_shop example, each read as DuckDB, Snowflake and BigQuery
SQL, and over a generated project of 2,000 models.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import duckdb
import pytest

from konstruct import Project
from konstruct.analysis.checksql import Checker, SqlCheck
from tests.test_checksql import (
    BRIDGE_MODELS,
    BRIDGE_WAREHOUSE,
    CUSTOMER_REVENUE,
    ORDER_PAYMENTS,
    WAREHOUSE,
    bridge_rows,
    database,
    make_bridge,
    make_shop,
    shop_rows,
)

CORRECT: dict[str, list[str]] = {
    'shop': [
        'select region, sum(net_revenue) as net_revenue from mart.customer_revenue group by region',
        'select customer_id, net_revenue from mart.customer_revenue order by net_revenue desc limit 10',
        'select c.region, sum(o.amount) as net_revenue from raw.orders o '
        "join raw.customers c on c.customer_id = o.customer_id where o.status <> 'returned' group by c.region",
        "select sum(o.amount) as revenue from raw.orders o where o.status != 'returned'",
        "select sum(case when status <> 'returned' then amount else 0 end) as net_revenue from raw.orders",
        "select sum(amount) filter (where status <> 'returned') as net_revenue from raw.orders",
        "select round(sum(amount), 2) as net_revenue from raw.orders where status <> 'returned'",
        "select cast(sum(amount) as double) as net_revenue from raw.orders where status <> 'returned'",
        "select coalesce(sum(amount), 0) as net_revenue from raw.orders where status <> 'returned'",
        "select sum(amount) as net_revenue from raw.orders where not status = 'returned'",
        'select count(*) as total_orders from raw.orders',
        'select count(o.order_id) as orders from raw.orders o',
        'select order_id as order_number, amount from raw.orders',
        "select sum(amount) as sales from raw.orders where status <> 'returned'",
        'select sum(amount) as amount from raw.payments',
        'select customer_id, order_date, amount, sum(amount) over '
        "(partition by customer_id order by order_date) as running_amount from raw.orders where status <> 'returned'",
        'with paid as (select order_id, sum(amount) as paid from raw.payments group by order_id) '
        'select o.order_id, o.amount, coalesce(p.paid, 0) as paid from raw.orders o '
        'left join paid p on p.order_id = o.order_id',
        'select count(*) as customers_with_returns from raw.customers c where exists '
        "(select 1 from raw.orders o where o.customer_id = c.customer_id and o.status = 'returned')",
        'select c.region, sum(o.amount) as net_revenue from raw.customers c '
        "join raw.orders o on o.customer_id = c.customer_id and o.status <> 'returned' group by 1",
        'select count(distinct o.customer_id) as buyers from raw.orders o '
        "join raw.payments p on p.order_id = o.order_id where p.method = 'card'",
        'with card_orders as (select distinct o.order_id, o.amount from raw.orders o '
        'join raw.payments p on p.order_id = o.order_id '
        "where p.method = 'card' and o.status <> 'returned') "
        'select sum(amount) as net_revenue from card_orders',
        "select 'returned' as kind, sum(amount) as amount from raw.orders where status = 'returned' "
        "union all select 'kept', sum(amount) from raw.orders where status <> 'returned'",
        'select region, sum(net_revenue) / (select sum(net_revenue) from mart.customer_revenue) as share '
        'from mart.customer_revenue group by region',
        'select r.region, r.net_revenue, c.name from mart.customer_revenue r '
        'join raw.customers c on c.customer_id = r.customer_id',
        "select date_trunc('month', order_date) as month, sum(amount) as net_revenue from raw.orders "
        "where status <> 'returned' and order_date >= date '2026-01-01' group by 1",
        'select o.order_id, sum(p.amount) as paid_amount from raw.orders o '
        'join raw.payments p on p.order_id = o.order_id group by o.order_id',
        'select p.payment_id, sum(o.amount) as order_amount from raw.payments p '
        'join raw.orders o on o.order_id = p.order_id group by p.payment_id',
        'select customer_id, count(*) as order_count, '
        "sum(amount) filter (where status <> 'returned') as net_revenue from raw.orders group by customer_id",
        'select customer_id, sum(amount) as net_revenue from raw.orders '
        "where status <> 'returned' group by 1 having sum(amount) > 100",
        "select region, sum(net_revenue) as revenue from mart.customer_revenue where region <> 'EMEA' group by 1",
        "select count(*) from raw.orders where status in ('returned')",
        'select * from (select region, sum(net_revenue) as net_revenue from mart.customer_revenue group by 1) t '
        'where net_revenue > 1000',
        'select avg(net_revenue) as average_net_revenue from mart.customer_revenue',
        "with o as (select * from raw.orders where status <> 'returned') "
        'select sum(amount) as net_revenue from o',
    ],
    'bridge': [
        'select country, sum(completed_revenue) as completed_revenue from mart.customer_value group by country',
        'select c.country, sum(o.amount) as completed_revenue from raw.orders o '
        'join raw.customers c on c.customer_id = o.customer_id '
        "where o.status = 'completed' and o.order_date >= '2020-01-01' group by 1",
        'select c.country, sum(o.amount) as completed_revenue from raw.orders o '
        'join raw.customers c on c.customer_id = o.customer_id '
        "where o.status = 'completed' and o.order_date >= date '2020-01-01' group by 1",
        'select sum(o.amount) as completed_revenue from raw.orders o '
        "where '2020-01-01' <= o.order_date and 'completed' = o.status",
        'select sum(amount) as completed_revenue from raw.orders '
        "where status = 'completed' and order_date >= cast('2020-01-01' as date)",
        'select sum(amount) as completed_revenue from raw.orders '
        "where status = 'completed' and order_date between '2020-01-01' and '2026-12-31'",
        'select sum(amount) as completed_revenue from raw.orders '
        "where status = 'completed' and order_date > '2019-12-31'",
        'select sum(amount) as completed_revenue from raw.orders '
        "where status in ('completed') and order_date >= '2020-01-01'::date",
        'select sum(amount) filter '
        "(where status = 'completed' and order_date >= '2020-01-01') as completed_revenue from raw.orders",
        "select sum(case when status = 'completed' and order_date >= date '2020-01-01' then amount else 0 end) "
        'as completed_revenue from raw.orders',
        'select s.segment_name, sum(o.amount) as revenue from raw.segments s '
        'join raw.customer_segments cs on cs.segment_id = s.segment_id '
        'join raw.orders o on o.customer_id = cs.customer_id '
        "where o.status = 'completed' group by s.segment_id, s.segment_name",
        'select cs.segment_id, count(distinct cs.customer_id) as customers from raw.customer_segments cs group by 1',
        'select segment_id, sum(segment_revenue) as segment_revenue from mart.segment_revenue group by 1',
        "with cust as (select customer_id, sum(amount) as amount from raw.orders where status = 'completed' "
        'group by 1) select cs.segment_id, sum(cust.amount) as revenue from raw.customer_segments cs '
        'join cust on cust.customer_id = cs.customer_id group by cs.segment_id',
        'select o.order_date, sum(i.quantity * i.price) as item_revenue from raw.orders o '
        "join raw.order_items i on i.order_id = o.order_id where o.status <> 'cancelled' group by 1",
        'select p.category, sum(i.quantity * i.price) as sales from raw.order_items i '
        'join raw.products p on p.product_id = i.product_id group by 1',
        'select i.order_id, sum(i.quantity) as units, max(p.list_price) as top_price from raw.order_items i '
        'join raw.products p on p.product_id = i.product_id group by 1',
        'select o.channel, count(*) as orders, avg(o.amount) as average_order from raw.orders o '
        "where o.status = 'completed' group by 1",
        'select customer_id, order_id, amount, '
        'row_number() over (partition by customer_id order by order_date desc) as rn from raw.orders',
        'select customer_id, order_id, amount from raw.orders '
        'qualify row_number() over (partition by customer_id order by order_date desc) = 1',
        'select country, sum(completed_revenue) as completed_revenue, '
        'sum(completed_revenue) * 1.0 / (select sum(completed_revenue) from mart.customer_value) as share '
        'from mart.customer_value group by 1',
        "select 'web' as channel, sum(amount) as completed_revenue from raw.orders "
        "where channel = 'web' and status = 'completed' and order_date >= '2020-01-01' "
        "union all select 'store', sum(amount) from raw.orders "
        "where channel = 'store' and status = 'completed' and order_date >= '2020-01-01'",
        'select count(*) as active_customers from raw.customers c '
        "where c.customer_id in (select customer_id from raw.orders where status = 'completed')",
        'select c.customer_id, c.name, v.completed_revenue from raw.customers c '
        'left join mart.customer_value v on v.customer_id = c.customer_id',
        'select o.order_id, o.amount, r.item_revenue from raw.orders o '
        'join mart.order_revenue r on r.order_id = o.order_id',
        "select date_trunc('year', order_date) as year, "
        "count(*) filter (where status = 'completed') as completed_orders from raw.orders group by 1",
        'select customer_id, sum(amount) as order_amount from raw.orders group by 1',
    ],
    'jaffle': [
        'select customer_id, first_name, customer_lifetime_value from customers '
        'order by customer_lifetime_value desc limit 10',
        'select sum(customer_lifetime_value) as total_lifetime_value from customers',
        'select o.customer_id, sum(p.amount) as lifetime_value from stg_payments p '
        'join stg_orders o on o.order_id = p.order_id group by o.customer_id',
        'select status, count(*) as order_count, sum(amount) as order_amount from orders group by status',
        "select count(*) as completed_orders from orders where status = 'completed'",
        "select count(*) filter (where status = 'completed') as completed_orders from orders",
        "select sum(case when status = 'completed' then 1 else 0 end) as completed_orders from orders",
        "select count(case when status = 'completed' then order_id end) as completed_orders from orders",
        "select date_trunc('month', order_date) as month, sum(amount) as order_amount from orders group by 1",
        "select sum(amount) filter (where payment_method = 'credit_card') as credit_card_amount from stg_payments",
        "select order_id, sum(case when payment_method = 'coupon' then amount else 0 end) as coupon_amount "
        'from stg_payments group by 1',
        'select payment_method, sum(amount) as amount from stg_payments group by 1',
        'select c.customer_id, c.first_name, count(o.order_id) as number_of_orders from stg_customers c '
        'left join stg_orders o on o.customer_id = c.customer_id group by 1, 2',
        'with first_orders as (select customer_id, min(order_date) as first_order from stg_orders group by 1) '
        'select first_order, count(*) as new_customers from first_orders group by 1',
        'select o.order_id, o.amount, c.first_name from orders o join customers c on c.customer_id = o.customer_id '
        "where o.status not in ('returned', 'return_pending')",
        'select customer_id, order_date, amount, sum(amount) over (partition by customer_id order by order_date '
        'rows between unbounded preceding and current row) as cumulative_amount from orders',
        'select customer_id, customer_lifetime_value, '
        'customer_lifetime_value / (select sum(customer_lifetime_value) from customers) as share_of_value '
        'from customers',
        'select count(*) as customers_with_orders from customers c '
        'where exists (select 1 from orders o where o.customer_id = c.customer_id)',
        "select distinct customer_id from orders where status = 'completed'",
        "select 'credit_card' as payment_method, sum(credit_card_amount) as amount from orders "
        "union all select 'coupon', sum(coupon_amount) from orders",
        "select round(avg(amount), 2) as average_order_amount from orders where status = 'completed'",
        'select o.customer_id, sum(o.amount) as customer_lifetime_value from orders o group by 1',
        'select first_order, most_recent_order, number_of_orders from customers where number_of_orders > 1',
        'select c.customer_id, c.customer_lifetime_value, count(o.order_id) as orders_placed from customers c '
        'left join orders o on o.customer_id = c.customer_id group by c.customer_id, c.customer_lifetime_value',
    ],
    'big': [
        'select c00, sum(c01) as total from wh.analytics.m1999 group by 1',
        'select a.c00, a.c03, b.c04 from wh.analytics.m1999 a join wh.analytics.m1000 b on a.c00 = b.c00',
        'with x as (select c00, sum(c02) as s from wh.analytics.m1500 group by 1) '
        'select count(*) as n, avg(s) as avg_s from x',
        'select c00, count(*) as n from wh.analytics.m500 where c01 > 0 group by 1 having count(*) > 1',
        'select c00, c01, row_number() over (partition by c00 order by c01) as rn from wh.analytics.m042',
        'select c00 from wh.analytics.m1234 union select c00 from wh.analytics.m999',
        'select c00, (select max(c01) from wh.analytics.m010) as top from wh.analytics.m011',
        'select sum(c01) filter (where c02 > 0) as c01 from wh.analytics.m1999',
        'select distinct c00, c05 from wh.analytics.m1777',
        'select c00, sum(case when c03 > 0 then c04 else 0 end) as positive_c04 from wh.analytics.m1888 group by 1',
    ],
}
"""Correct analyst queries for each project: no error and no warning."""

BUGGY: dict[str, list[tuple[str, str]]] = {
    'shop': [
        (
            'missing-filter',
            'select c.region, sum(o.amount) as net_revenue from raw.orders o '
            'join raw.customers c on c.customer_id = o.customer_id group by 1',
        ),
        ('missing-filter', 'with o as (select * from raw.orders) select sum(amount) as revenue from o'),
        (
            'missing-filter',
            "select sum(amount) as net_revenue from raw.orders where status = 'completed'",
        ),
        (
            'fan-out',
            'select c.region, sum(o.amount) as total from raw.orders o '
            'join raw.payments p on p.order_id = o.order_id '
            'join raw.customers c on c.customer_id = o.customer_id group by 1',
        ),
        (
            'mixed-grain',
            'select o.order_id, r.net_revenue from raw.orders o '
            'join mart.customer_revenue r on r.customer_id = o.customer_id',
        ),
        (
            'join-off-relationship',
            'select o.order_id, c.name from raw.orders o join raw.customers c on o.order_id = c.customer_id',
        ),
        (
            'retired-column',
            'select region, sum(gross_revenue) as total from mart.customer_revenue group by 1',
        ),
        (
            'formula-differs',
            'select o.order_id, sum(p.amount) as paid_amount from raw.orders o '
            "join raw.payments p on p.order_id = o.order_id where p.method <> 'coupon' group by 1",
        ),
    ],
    'bridge': [
        (
            'missing-filter',
            'select c.country, sum(o.amount) as completed_revenue from raw.orders o '
            "join raw.customers c on c.customer_id = o.customer_id where o.status = 'completed' group by 1",
        ),
        (
            'missing-filter',
            'select o.order_date, sum(i.quantity * i.price) as item_revenue from raw.orders o '
            'join raw.order_items i on i.order_id = o.order_id group by 1',
        ),
        (
            'fan-out',
            'select c.country, sum(o.amount) as revenue from raw.orders o '
            'join raw.customer_segments cs on cs.customer_id = o.customer_id '
            "join raw.customers c on c.customer_id = o.customer_id where o.status = 'completed' group by c.country",
        ),
        (
            'fan-out',
            'select sum(v.completed_revenue) as completed_revenue from mart.customer_value v '
            'join raw.customer_segments cs on cs.customer_id = v.customer_id',
        ),
        (
            'fan-out',
            'select o.channel, sum(o.amount) as revenue from raw.orders o '
            'join raw.order_items i on i.order_id = o.order_id group by 1',
        ),
        (
            'mixed-grain',
            'select i.item_id, i.quantity, o.amount from raw.order_items i join raw.orders o on o.order_id = i.order_id',
        ),
        (
            'join-off-relationship',
            'select o.order_id, c.name from raw.orders o join raw.customers c on c.customer_id = o.order_id',
        ),
        ('retired-column', 'select customer_id, sum(legacy_amount) as amount from raw.orders group by 1'),
        (
            'formula-differs',
            'select c.country, sum(o.amount) as completed_revenue from raw.orders o '
            'join raw.customers c on c.customer_id = o.customer_id '
            "where o.status = 'completed' and o.order_date >= '2020-01-01' and o.channel <> 'wholesale' group by 1",
        ),
    ],
    'jaffle': [
        ('missing-filter', 'select count(*) as completed_orders from orders'),
        (
            'fan-out',
            'select c.customer_id, sum(c.customer_lifetime_value) as ltv from customers c '
            'join orders o on o.customer_id = c.customer_id group by 1',
        ),
        (
            'fan-out',
            'select o.status, sum(o.amount) as amount from orders o '
            'join stg_payments p on p.order_id = o.order_id group by 1',
        ),
        (
            'mixed-grain',
            'select o.order_id, o.order_date, c.customer_lifetime_value from orders o '
            'join customers c on c.customer_id = o.customer_id',
        ),
        (
            'join-off-relationship',
            'select o.order_id, c.first_name from orders o join customers c on c.customer_id = o.order_id',
        ),
        (
            'formula-differs',
            'select o.customer_id, sum(p.amount * 100) as lifetime_value from stg_payments p '
            'join stg_orders o on o.order_id = p.order_id group by 1',
        ),
        (
            'formula-differs',
            'select o.customer_id, sum(p.amount) as customer_lifetime_value from stg_payments p '
            "join stg_orders o on o.order_id = p.order_id where p.payment_method <> 'coupon' group by 1",
        ),
    ],
}
"""Buggy queries for each project, with the rule that must catch each."""


# Queries a second review found wrong, and the same projects read as Snowflake and BigQuery SQL.
CORRECT['shop'] += [
    'select * from mart.customer_revenue',
    'select sum(o.amount) as net_revenue from raw.orders o '
    "where o.order_id in (select order_id from raw.orders where status <> 'returned')",
    'select sum(o.amount) as net_revenue from raw.orders o '
    "where o.order_id not in (select order_id from raw.orders where status = 'returned')",
    'select sum(o.amount) as net_revenue from raw.orders o '
    "join (select order_id from raw.orders where status <> 'returned') k on k.order_id = o.order_id",
    "with kept as (select order_id from raw.orders where status <> 'returned') "
    'select sum(o.amount) as net_revenue from raw.orders o join kept using (order_id)',
    'select sum(a.amount) as net_revenue from raw.orders a join raw.orders b on b.order_id = a.order_id '
    "where b.status <> 'returned'",
    'select sum(o.amount) as net_revenue from raw.orders o where exists '
    "(select 1 from raw.orders k where k.order_id = o.order_id and k.status <> 'returned')",
    'select c.customer_id, t.last_amount from raw.customers c, lateral (select o.amount as last_amount '
    'from raw.orders o where o.customer_id = c.customer_id order by o.order_date desc limit 1) t',
    'select c.region, c.customer_id, t.net_revenue from raw.customers c join lateral (select sum(o.amount) '
    "as net_revenue from raw.orders o where o.customer_id = c.customer_id and o.status <> 'returned') t on true",
    "select sum(amount) as net_revenue from raw.orders where status <> 'returned' and amount is not null",
]
CORRECT['bridge'] += [
    # A unit price is a rate, not an amount a total adds up, so repeating it on each item is no mixed grain.
    'select i.item_id, i.price, p.list_price from raw.order_items i join raw.products p on p.product_id = i.product_id',
    'select c.country, count(distinct c.customer_id) as customers from raw.customers c '
    'join raw.customer_segments cs on cs.customer_id = c.customer_id group by 1',
]
CORRECT['shop@snowflake'] = [
    "select customer_id, sum(iff(status <> 'returned', amount, 0)) as net_revenue from raw.orders group by 1",
    "select count_if(status = 'returned') as returned_orders from raw.orders",
    'select customer_id, order_id from raw.orders '
    'qualify row_number() over (partition by customer_id order by order_date desc) = 1',
    'select region, sum(net_revenue) as net_revenue from mart.customer_revenue group by all',
    'select c.region, sum(o.amount) as net_revenue from raw.orders o join raw.customers c '
    "on c.customer_id = o.customer_id where o.status <> 'returned' group by c.region order by net_revenue desc nulls last",
    "select to_char(order_date, 'YYYY-MM') as month, sum(amount) as net_revenue from raw.orders "
    "where status != 'returned' group by 1",
    "select sum(amount) as net_revenue from raw.orders where status <> 'returned' and order_date >= '2026-01-01'::date",
    'select div0(sum(net_revenue), count(*)) as avg_net_revenue from mart.customer_revenue',
    'select o.customer_id, sum(o.amount) as net_revenue from raw.orders o '
    "where o.status <> 'returned' and o.order_date >= dateadd(month, -3, current_date()) group by 1",
    'select r.region, r.net_revenue from mart.customer_revenue r '
    'qualify r.net_revenue = max(r.net_revenue) over (partition by r.region)',
]
CORRECT['shop@bigquery'] = [
    "select countif(status = 'returned') as returned_orders from raw.orders",
    "select customer_id, sum(if(status <> 'returned', amount, 0)) as net_revenue from raw.orders group by customer_id",
    "select format_date('%Y-%m', order_date) as month, sum(amount) as net_revenue from raw.orders "
    "where status != 'returned' group by month",
    'select safe_divide(sum(net_revenue), count(*)) as avg_net_revenue from mart.customer_revenue',
    'select region, sum(net_revenue) as net_revenue from `mart.customer_revenue` group by region',
    'select o.customer_id, sum(o.amount) as net_revenue from raw.orders o '
    "where o.status <> 'returned' and o.order_date >= date_sub(current_date(), interval 3 month) group by 1",
]
CORRECT['bridge@snowflake'] = [
    "select c.country, sum(iff(o.status = 'completed' and o.order_date >= '2020-01-01', o.amount, 0)) "
    'as completed_revenue from raw.orders o join raw.customers c on c.customer_id = o.customer_id group by 1',
]
CORRECT['bridge@bigquery'] = [
    'select c.country, sum(o.amount) as completed_revenue from raw.orders o '
    'join raw.customers c on c.customer_id = o.customer_id '
    "where o.status = 'completed' and o.order_date >= date '2020-01-01' group by 1",
]
CORRECT['jaffle@snowflake'] = [
    "select count_if(status = 'completed') as completed_orders from orders",
    "select count(*) as completed_orders from orders where status = 'completed'",
    'select status, count(*) as order_count from orders group by 1',
]
CORRECT['jaffle@bigquery'] = [
    "select countif(status = 'completed') as completed_orders from orders",
    'select customer_id, customer_lifetime_value from customers order by 2 desc limit 5',
]
BUGGY['shop'] += [
    (
        'fan-out',
        'select c.region, sum(o.amount) as total from raw.customers c join raw.orders o '
        'on o.customer_id = c.customer_id, lateral (select * from raw.payments p where p.order_id = o.order_id) p '
        'group by 1',
    ),
    (
        'formula-differs',
        "select sum(amount) as net_revenue from raw.orders where status <> 'returned' and amount > 10",
    ),
    (
        'fan-out',
        'select c.region, count(*) as orders from raw.customers c join raw.orders o on o.customer_id = c.customer_id '
        'join raw.payments p on p.order_id = o.order_id group by 1',
    ),
    # Grouped by order as well, each order's amount still counts once per payment.
    (
        'fan-out',
        'select o.customer_id, sum(o.amount) as net_revenue, max(p.amount) as biggest_payment from raw.orders o '
        "join raw.payments p on p.order_id = o.order_id where o.status <> 'returned' group by o.customer_id, o.order_id",
    ),
    (
        'missing-filter',
        'select sum(o.amount) as net_revenue from raw.orders o where exists (select 1 from raw.orders o2 '
        "where o2.status <> 'returned')",
    ),
    (
        'missing-filter',
        'select sum(a.amount) as net_revenue from raw.orders a join raw.orders b on b.order_id = a.order_id '
        "where b.status <> 'returned' and a.status = 'returned'",
    ),
    (
        'missing-filter',
        'select sum(amount) as net_revenue from raw.orders union all '
        "select sum(amount) from raw.orders where order_date >= '2026-06-01'",
    ),
]
CORRECT['shop'] += [
    "select sum(o.amount) as net_revenue from raw.orders o where o.status not like 'returned'",
    "select sum(o.amount) as net_revenue from raw.orders o where o.status not in (values ('returned'))",
    "select sum(o.amount) as net_revenue from raw.orders o where not o.status = any(['returned'])",
    "select sum(o.amount) as net_revenue from raw.orders o where o.status <> 'returned' and o.customer_id in (1, 2, 3)",
    # Anti-joins keep the rows with no match, which repeat nothing.
    'select count(*) as customers from raw.customers c left join raw.orders o on o.customer_id = c.customer_id '
    'where o.order_id is null',
    'select sum(o.amount) as unpaid from raw.orders o left join raw.payments p on p.order_id = o.order_id '
    'where p.order_id is null',
    # An ASOF join matches each payment to one order, so the payments it adds up do not repeat.
    'select sum(p.amount) as paid from raw.payments p '
    'asof join raw.orders o on o.order_id = p.order_id and o.order_id <= p.order_id',
    'select kind, sum(value) as total from raw.orders unpivot (value for kind in (amount, customer_id)) group by kind',
    'select order_id, kind, value from raw.orders unpivot (value for kind in (amount, customer_id)) where value > 0',
    'select * from (select customer_id, status, amount from raw.orders) '
    "pivot (sum(amount) for status in ('returned', 'completed')) where returned > 0",
    'select customer_id, sum(amount) over (partition by customer_id) as net_revenue from raw.orders '
    "where status <> 'returned'",
    'select o.order_id, sum(p.amount) over (partition by o.order_id) as paid from raw.orders o '
    'join raw.payments p on p.order_id = o.order_id',
    'select c.region, count(*) as payment_count from raw.customers c join raw.orders o '
    'on o.customer_id = c.customer_id join raw.payments p on p.order_id = o.order_id group by 1',
    'select c.region, count(*) as customer_orders from raw.customers c '
    'join raw.orders o on o.customer_id = c.customer_id group by 1',
]
CORRECT['shop@bigquery'] += [
    "select sum(o.amount) as net_revenue from raw.orders o where o.status not in unnest(['returned'])",
    "select o.customer_id, count(*) as returns from raw.orders o where o.status in unnest(['returned']) group by 1",
]
BUGGY['shop'] += [
    (
        'missing-filter',
        'select customer_id, sum(amount) over (partition by customer_id) as net_revenue from raw.orders',
    ),
    (
        'fan-out',
        'select o.order_id, sum(o.amount) over (partition by o.customer_id) as total from raw.orders o '
        'join raw.payments p on p.order_id = o.order_id',
    ),
    (
        'fan-out',
        'select o.customer_id, sum(o.amount) as total from raw.orders o cross join unnest([1, 2]) as t(x) group by 1',
    ),
    (
        'fan-out',
        'select c.region, count(*) as order_count from raw.customers c join raw.orders o '
        'on o.customer_id = c.customer_id join raw.payments p on p.order_id = o.order_id group by 1',
    ),
    ('unknown-column', 'select sum(o.amount_total) as net_revenue from raw.orders o'),
    (
        'formula-differs',
        "select sum(o.amount) as net_revenue from raw.orders o where o.status <> 'returned' and o.status <> 'cancelled'",
    ),
]
BUGGY['bridge'] += [
    (
        'fan-out',
        'select c.country, count(*) as customers from raw.customers c '
        'join raw.customer_segments cs on cs.customer_id = c.customer_id group by 1',
    ),
]
BUGGY['shop@snowflake'] = [
    ('missing-filter', "select sum(iff(status = 'completed', amount, 0)) as net_revenue from raw.orders"),
    ('missing-filter', 'select sum(amount) as net_revenue from raw.orders'),
    (
        'fan-out',
        'select c.region, sum(o.amount) as total from raw.customers c join raw.orders o '
        'on o.customer_id = c.customer_id join raw.payments p on p.order_id = o.order_id group by 1',
    ),
    ('retired-column', 'select region, sum(gross_revenue) as total from mart.customer_revenue group by 1'),
]
BUGGY['shop@bigquery'] = [
    ('missing-filter', 'select customer_id, sum(amount) as net_revenue from raw.orders group by customer_id'),
    (
        'fan-out',
        'select c.region, sum(o.amount) as total from raw.customers c join raw.orders o '
        'on o.customer_id = c.customer_id join raw.payments p on p.order_id = o.order_id group by 1',
    ),
    ('retired-column', 'select region, sum(gross_revenue) as total from mart.customer_revenue group by 1'),
]
# Shapes from a fourth review: filters through queries of constants, typed values, unnested
# arrays, ASOF joins, CTE columns and exclusions written on any column.
CORRECT['shop'] += [
    "select sum(o.amount) as net_revenue from raw.orders o where o.status not in (select 'returned')",
    "select sum(o.amount) as net_revenue from raw.orders o where o.status <> (select 'returned')",
    "select sum(o.amount) as net_revenue from raw.orders o where o.status <> all(select 'returned')",
    "select sum(o.amount) as net_revenue from raw.orders o where o.status <> all(['returned'])",
    "select sum(o.amount) as net_revenue from raw.orders o where o.status not like 'returned' escape '!'",
    "select sum(o.amount) as net_revenue from raw.orders o where o.status <> 'returned' and o.amount <> 0",
    "select sum(o.amount) as net_revenue from raw.orders o where o.status <> 'returned' "
    'and o.order_date >= make_date(2026, 1, 1)',
    'select t.x, count(*) as n from raw.orders o cross join unnest([1, 2]) as t(x) group by 1',
    'select tag, count(*) as n from (select o.amount, unnest([1, 2]) as tag from raw.orders o) t group by 1',
    'select customer_id, sum(amount) as net_revenue from raw.orders '
    "where status <> 'returned' group by 1 having sum(amount) > 140",
    "with t as (select customer_id, sum(amount) as net_revenue from raw.orders where status <> 'returned' "
    'group by 1) select * from t where coalesce(net_revenue, 0) < 120',
]
CORRECT['shop@bigquery'] += [
    'select x, count(*) as n from raw.orders o cross join unnest([1, 2]) as x group by x',
    "select sum(o.amount) as net_revenue from raw.orders o where o.status <> 'returned' "
    'and o.order_date >= date(2026, 1, 1)',
]
CORRECT['shop@snowflake'] += [
    'select f.value, count(*) as n from raw.orders o, lateral flatten(input => array_construct(1, 2)) f group by 1',
    "select sum(o.amount) as net_revenue from raw.orders o where o.status <> 'returned' "
    'and o.order_date >= date_from_parts(2026, 1, 1)',
]
BUGGY['shop'] += [
    (
        'fan-out',
        'select sum(o.amount) as total from raw.payments p '
        'asof join raw.orders o on o.order_id = p.order_id and o.order_id <= p.order_id',
    ),
    (
        'missing-filter',
        'select customer_id, sum(amount) as net_revenue from raw.orders group by 1 having sum(amount) > 140',
    ),
    (
        'missing-filter',
        "select sum(o.amount) as net_revenue from raw.orders o where o.status <> (select 'cancelled')",
    ),
    (
        'unknown-column',
        'with t as (select customer_id, amount from raw.orders) select sum(t.amount_total) as total from t',
    ),
    (
        'fan-out',
        'select sum(amount) as total from (select o.amount, unnest([1, 2]) as tag from raw.orders o) t',
    ),
    (
        'formula-differs',
        'select sum(o.amount) as net_revenue from raw.orders o join raw.customers c on c.customer_id = o.customer_id '
        "where o.status <> 'returned' and c.customer_id <> 7",
    ),
    (
        'formula-differs',
        "select sum(o.amount) as net_revenue from raw.orders o where o.status <> 'returned' "
        "and o.order_date <> '2026-01-01'",
    ),
]
BUGGY['shop@snowflake'] += [
    (
        'fan-out',
        'select o.customer_id, sum(o.amount) as total from raw.orders o, '
        'table(flatten(input => array_construct(1, 2))) f group by 1',
    ),
    (
        'fan-out',
        'select o.customer_id, sum(o.amount) as total from raw.orders o '
        'join table(flatten(input => array_construct(1, 2))) f on true group by 1',
    ),
]
# Shapes from a fifth review.
CORRECT['shop'] += [
    "with excluded(status) as (values ('returned')) select sum(o.amount) as net_revenue from raw.orders o "
    'where o.status not in (select status from excluded)',
    "select sum(o.amount) as net_revenue from raw.orders o where o.status not in ('returned', 'returned')",
]
CORRECT['shop@snowflake'] += [
    'select f.value::string as v, count(*) as n from raw.orders o, '
    'lateral flatten(input => array_construct(1, 2)) f group by 1',
]
BUGGY['shop'] += [
    (
        'missing-filter',
        "with excluded as (select * from (values ('cancelled')) t(status)) select sum(o.amount) as net_revenue "
        'from raw.orders o where o.status not in (select status from excluded)',
    ),
]
BUGGY['jaffle@snowflake'] = [('missing-filter', 'select count(*) as completed_orders from orders')]
BUGGY['jaffle@bigquery'] = [
    (
        'missing-filter',
        'select customer_id, count(*) as completed_orders, sum(amount) as order_amount from orders group by 1',
    ),
]


def loud(result: SqlCheck) -> list[str]:
    return [f'{f.severity} {f.rule}: {f.message}' for f in result.findings if f.severity != 'info']


def caught(result: SqlCheck, rule: str) -> bool:
    return any(f.rule == rule and f.severity != 'info' for f in result.findings)


class Checkers:
    """A checker for each project, as its key names it (``shop``, ``shop@snowflake``), built once."""

    def __init__(self, request: pytest.FixtureRequest, factory: pytest.TempPathFactory) -> None:
        self.request, self.factory = request, factory
        self.built: dict[str, Checker] = {}

    def __call__(self, key: str) -> Checker:
        if key not in self.built:
            project, _, dialect = key.partition('@')
            root = self.factory.mktemp(key.replace('@', '_'))
            if project == 'shop':
                graph = make_shop(root, dialect=dialect or None)
            elif project == 'bridge':
                graph = make_bridge(root, dialect=dialect or None)
            else:
                example = Path(self.request.getfixturevalue('jaffle_built_graph'))
                if not dialect:
                    graph = Project(example).graph
                else:
                    copy = root / 'jaffle_shop'
                    shutil.copytree(example, copy, ignore=shutil.ignore_patterns('konstruct-out'))
                    graph = Project(copy).build(run_dbt=False, dialect=dialect).graph
            self.built[key] = Checker(graph)
        return self.built[key]


@pytest.fixture(scope='module')
def checkers(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Checkers:
    return Checkers(request, tmp_path_factory)


def test_the_corpus_is_large_enough() -> None:
    assert sum(len(queries) for queries in CORRECT.values()) >= 80
    kinds = {rule for cases in BUGGY.values() for rule, _ in cases}
    assert kinds >= {'missing-filter', 'fan-out', 'mixed-grain', 'join-off-relationship', 'retired-column'}


@pytest.mark.parametrize(
    ('project', 'sql'), [(key, sql) for key, queries in CORRECT.items() if key != 'big' for sql in queries]
)
def test_correct_queries_pass(checkers: Checkers, project: str, sql: str) -> None:
    assert loud(checkers(project).check(sql)) == []


@pytest.mark.parametrize(
    ('project', 'rule', 'sql'), [(key, rule, sql) for key, cases in BUGGY.items() for rule, sql in cases]
)
def test_buggy_queries_are_caught(checkers: Checkers, project: str, rule: str, sql: str) -> None:
    result = checkers(project).check(sql)
    assert caught(result, rule), str(result)


@pytest.mark.scale
def test_correct_queries_on_a_project_of_2000_models_pass(tmp_path_factory: pytest.TempPathFactory) -> None:
    from tests.test_scale import make_project

    root = tmp_path_factory.mktemp('corpus_big')
    make_project(root, models=2000)
    graph = Project(root).build(run_dbt=False).graph
    started = time.perf_counter()
    checker = Checker(graph)
    ready = time.perf_counter() - started
    started = time.perf_counter()
    failures = {sql: loud(checker.check(sql)) for sql in CORRECT['big']}
    per_query = (time.perf_counter() - started) / len(CORRECT['big'])
    assert {sql: found for sql, found in failures.items() if found} == {}
    # Measured at about 0.5 s to build the checker and 50 ms a query.
    assert ready < 10 and per_query < 1, (ready, per_query)


@pytest.fixture(scope='module')
def databases(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return {
        'shop': database(
            tmp_path_factory.mktemp('corpus_shop_db'),
            WAREHOUSE,
            [CUSTOMER_REVENUE, ORDER_PAYMENTS],
            shop_rows,
        ),
        'bridge': database(
            tmp_path_factory.mktemp('corpus_bridge_db'),
            BRIDGE_WAREHOUSE,
            list(BRIDGE_MODELS.values()),
            bridge_rows,
        ),
    }


@pytest.mark.parametrize(
    ('project', 'sql'),
    [
        (key, sql)
        for key in ('shop', 'bridge')
        for sql in [*CORRECT[key], *(sql for _, sql in BUGGY[key])]
        if 'amount_total' not in sql  # the queries of unknown columns cannot run
    ],
)
def test_every_duckdb_query_of_the_corpus_runs(databases: dict[str, Path], project: str, sql: str) -> None:
    con = duckdb.connect(str(databases[project]), read_only=True)
    try:
        con.execute(sql).fetchall()
    finally:
        con.close()
