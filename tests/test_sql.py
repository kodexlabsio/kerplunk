from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from typing import Any

import pytest

from konstruct.sql import ColumnSource, StatementLineage, analyse, dialect_for, strip_templating

SCHEMA = {
    'db.main.orders': {
        'id': 'int',
        'customer_id': 'int',
        'amount': 'int',
        'status': 'text',
        'ordered_at': 'date',
    },
    'db.main.customers': {'id': 'int', 'first_name': 'text', 'last_name': 'text'},
    'db.main.payments': {'id': 'int', 'order_id': 'int', 'amount': 'int', 'method': 'text'},
}

ORDERS = 'db.main.orders'
CUSTOMERS = 'db.main.customers'
PAYMENTS = 'db.main.payments'


def one(sql: str, **kwargs: object) -> StatementLineage:
    kwargs.setdefault('schema', SCHEMA)
    kwargs.setdefault('dialect', 'duckdb')
    kwargs.setdefault('default_catalog', 'db')
    kwargs.setdefault('default_db', 'main')
    results = analyse(sql, **kwargs)  # type: ignore[arg-type]
    assert len(results) == 1
    return results[0]


def src(relation: str, column: str, transform: str = 'direct') -> ColumnSource:
    return ColumnSource(relation, column, transform)  # type: ignore[arg-type]


def test_plain_select_with_alias() -> None:
    result = one('select id as order_id, amount from orders')
    assert result.kind == 'select'
    assert result.reads == [ORDERS]
    assert result.columns == {'order_id': [src(ORDERS, 'id')], 'amount': [src(ORDERS, 'amount')]}
    assert result.errors == []


def test_expression_is_marked_transformed() -> None:
    result = one('select amount / 100 as amount_dollars, upper(status) as status from orders')
    assert result.columns == {
        'amount_dollars': [src(ORDERS, 'amount', 'transformed')],
        'status': [src(ORDERS, 'status', 'transformed')],
    }


def test_ctes_chain_back_to_physical_columns() -> None:
    sql = """
    with source as (select * from orders),
    renamed as (select id as order_id, amount * 2 as doubled from source),
    final as (select order_id, doubled as total from renamed)
    select * from final
    """
    result = one(sql)
    assert result.reads == [ORDERS]
    assert result.columns == {
        'order_id': [src(ORDERS, 'id')],
        'total': [src(ORDERS, 'amount', 'transformed')],
    }


def test_joins_resolve_each_column_to_its_own_table() -> None:
    sql = """
    select o.id as order_id, c.first_name || ' ' || c.last_name as customer_name, p.amount
    from orders as o
    left join customers c on o.customer_id = c.id
    join payments p on p.order_id = o.id
    """
    result = one(sql)
    assert sorted(result.reads) == sorted([ORDERS, CUSTOMERS, PAYMENTS])
    assert result.columns['order_id'] == [src(ORDERS, 'id')]
    assert sorted(result.columns['customer_name'], key=lambda s: s.column) == [
        src(CUSTOMERS, 'first_name', 'transformed'),
        src(CUSTOMERS, 'last_name', 'transformed'),
    ]
    assert result.columns['amount'] == [src(PAYMENTS, 'amount')]


def test_join_filter_and_group_columns_are_references() -> None:
    sql = """
    with o as (select * from orders)
    select o.customer_id, sum(p.amount) as total
    from o join payments p on p.order_id = o.id
    where o.status = 'completed'
    group by o.customer_id
    having count(*) > 1
    """
    result = one(sql)
    assert set(result.references) == {
        (PAYMENTS, 'order_id'),
        (ORDERS, 'id'),
        (ORDERS, 'status'),
        (ORDERS, 'customer_id'),
    }


def test_unused_columns_of_an_import_cte_are_not_references() -> None:
    result = one('with o as (select * from orders) select o.id from o')
    assert result.references == []


def test_select_star_expands_with_a_schema() -> None:
    result = one('select * from customers')
    assert list(result.columns) == ['id', 'first_name', 'last_name']
    assert result.columns['first_name'] == [src(CUSTOMERS, 'first_name')]


def test_qualified_star_in_a_join() -> None:
    result = one('select c.*, o.amount from customers c join orders o on o.customer_id = c.id')
    assert list(result.columns) == ['id', 'first_name', 'last_name', 'amount']
    assert result.columns['amount'] == [src(ORDERS, 'amount')]


def test_star_over_an_unknown_relation_uses_the_columns_the_query_asks_for() -> None:
    sql = """
    with source as (select * from raw.unknown_orders),
    renamed as (
        select id as order_id, order_total as order_total_cents, (order_total / 100)::numeric(16, 2) as order_total
        from source
    )
    select * from renamed
    """
    result = one(sql, schema={})
    relation = 'db.raw.unknown_orders'
    assert result.columns == {
        'order_id': [src(relation, 'id')],
        'order_total_cents': [src(relation, 'order_total')],
        'order_total': [src(relation, 'order_total', 'transformed')],
    }
    assert any('unknown columns: db.raw.unknown_orders' in e for e in result.errors)


def test_select_star_without_a_schema_is_reported() -> None:
    result = one('select * from mystery', schema={})
    assert result.reads == ['db.main.mystery']
    assert result.columns == {}
    assert any('unknown columns' in e for e in result.errors)


def test_union_all_takes_columns_from_every_branch() -> None:
    sql = """
    select id, amount, 'order' as kind from orders
    union all
    select id, amount, 'payment' from payments
    """
    result = one(sql)
    assert result.columns['id'] == [src(ORDERS, 'id'), src(PAYMENTS, 'id')]
    assert result.columns['amount'] == [src(ORDERS, 'amount'), src(PAYMENTS, 'amount')]
    assert result.columns['kind'] == []


def test_window_function_columns_are_transformed_sources() -> None:
    sql = """
    select id,
           row_number() over (partition by customer_id order by ordered_at) as order_seq,
           sum(amount) over (partition by customer_id) as customer_total
    from orders
    """
    result = one(sql)
    assert result.columns['id'] == [src(ORDERS, 'id')]
    assert set(result.columns['order_seq']) == {
        src(ORDERS, 'customer_id', 'transformed'),
        src(ORDERS, 'ordered_at', 'transformed'),
    }
    assert set(result.columns['customer_total']) == {
        src(ORDERS, 'amount', 'transformed'),
        src(ORDERS, 'customer_id', 'transformed'),
    }


def test_qualify_clause_columns_are_references() -> None:
    sql = 'select id from orders qualify row_number() over (partition by customer_id order by id) = 1'
    assert (ORDERS, 'customer_id') in one(sql).references


def test_subquery_in_from() -> None:
    sql = 'select t.total from (select customer_id, sum(amount) as total from orders group by 1) t'
    result = one(sql)
    assert result.columns == {'total': [src(ORDERS, 'amount', 'transformed')]}
    assert (ORDERS, 'customer_id') in result.references


def test_aggregate_and_case() -> None:
    sql = """
    select customer_id,
           sum(case when method = 'card' then amount else 0 end) as card_amount
    from payments group by customer_id
    """
    result = one(sql, schema={PAYMENTS: {**SCHEMA[PAYMENTS], 'customer_id': 'int'}})
    assert set(result.columns['card_amount']) == {
        src(PAYMENTS, 'method', 'transformed'),
        src(PAYMENTS, 'amount', 'transformed'),
    }


def test_target_names_the_relation_a_select_writes() -> None:
    result = one('select id from orders', target='db.main.stg_orders')
    assert result.target == 'db.main.stg_orders'


def test_create_table_as_select() -> None:
    result = one('create table analytics.big_orders as select id, amount from orders where amount > 10')
    assert result.kind == 'create_table'
    assert result.target == 'db.analytics.big_orders'
    assert result.reads == [ORDERS]
    assert result.columns['amount'] == [src(ORDERS, 'amount')]
    assert result.references == [(ORDERS, 'amount')]


def test_create_view() -> None:
    result = one('create or replace view v_orders as select id from orders')
    assert result.kind == 'create_view'
    assert result.target == 'db.main.v_orders'


def test_insert_with_a_column_list_maps_by_position() -> None:
    result = one('insert into archive (order_key, value) select id, amount from orders')
    assert result.target == 'db.main.archive'
    assert result.columns == {'order_key': [src(ORDERS, 'id')], 'value': [src(ORDERS, 'amount')]}


def test_merge_and_delete_are_relation_level() -> None:
    merge = one(
        "merge into customers t using payments s on t.id = s.id when matched then update set first_name = 'x'"
    )
    assert merge.target == CUSTOMERS
    assert merge.reads == [PAYMENTS]
    delete = one('delete from orders where id in (select order_id from payments)')
    assert delete.target == ORDERS
    assert delete.reads == [PAYMENTS]


def test_later_statements_see_tables_created_earlier() -> None:
    sql = """
    create table tmp_totals as select customer_id, sum(amount) as total from orders group by 1;
    create table report as select * from tmp_totals;
    """
    results = analyse(sql, dialect='duckdb', schema=SCHEMA, default_catalog='db', default_db='main')
    assert [r.target for r in results] == ['db.main.tmp_totals', 'db.main.report']
    assert results[1].columns == {
        'customer_id': [src('db.main.tmp_totals', 'customer_id')],
        'total': [src('db.main.tmp_totals', 'total')],
    }


def test_unqualified_tables_without_defaults_use_bare_names() -> None:
    results = analyse('select a from t', dialect='postgres')
    assert results[0].reads == ['t']
    assert results[0].columns == {'a': [src('t', 'a')]}


def test_fully_qualified_names_win_over_defaults() -> None:
    result = one('select "ID" from "Other"."Sales"."Orders"')
    assert result.reads == ['other.sales.orders']


def test_snowflake_dialect() -> None:
    results = analyse(
        "select o.id, iff(o.amount > 0, 'y', 'n') as positive from raw.shop.orders o",
        dialect='snowflake',
    )
    assert results[0].reads == ['raw.shop.orders']
    assert results[0].columns['positive'] == [src('raw.shop.orders', 'amount', 'transformed')]


def test_unparseable_sql_is_reported_not_raised() -> None:
    results = analyse('select from where (', dialect='duckdb', target='db.main.x')
    assert results[0].kind == 'unparsed'
    assert results[0].target == 'db.main.x'
    assert results[0].errors


def test_ddl_without_a_query_is_ignored() -> None:
    assert analyse('drop table if exists orders; set search_path = public', dialect='postgres') == []
    sql = "create schema if not exists reporting; create index ix on orders (id); create function f() returns int as 'select 1' language sql"
    assert analyse(sql, dialect='postgres') == []


def test_strip_templating() -> None:
    sql = "{% if x %}select 1{% endif %} from t where ds = '{{ ds }}' and d = {{ ds }}{# note #}"
    assert strip_templating(sql) == "select 1 from t where ds = '__konstruct__' and d = '__konstruct__'"
    sql = 'select a from {{ params.schema }}.orders join "{{ var }}".x on true where d = {{ ds }}'
    stripped = strip_templating(sql)
    assert (
        stripped
        == 'select a from __konstruct__.orders join "__konstruct__".x on true where d = \'__konstruct__\''
    )
    assert analyse(stripped, dialect='duckdb')[0].reads == ['__konstruct__.orders', '__konstruct__.x']


def test_round_trip_to_dict() -> None:
    result = one("select id as order_id from orders where status = 'x'")
    assert StatementLineage.from_dict(result.to_dict()) == result


@pytest.mark.parametrize(
    ('adapter', 'dialect'),
    [
        ('duckdb', 'duckdb'),
        ('postgres', 'postgres'),
        ('sqlserver', 'tsql'),
        ('Snowflake', 'snowflake'),
        (None, None),
    ],
)
def test_dialect_for(adapter: str | None, dialect: str | None) -> None:
    assert dialect_for(adapter) == dialect


STRUCTS = {'lake.sales.orders': {'id': 'bigint', 'customer': 'UNKNOWN'}}


@pytest.mark.parametrize(
    ('sql', 'dialect', 'expected'),
    [
        ('select o.customer.country as c from lake.sales.orders o', 'trino', ('customer.country', 'direct')),
        ('select customer.country as c from lake.sales.orders', 'trino', ('customer.country', 'direct')),
        (
            "select o.customer['country'] as c from lake.sales.orders o",
            'duckdb',
            ('customer.country', 'direct'),
        ),
        (
            'select upper(o.customer.address.city) as c from lake.sales.orders o',
            'trino',
            ('customer.address.city', 'transformed'),
        ),
        (
            'with x as (select customer from lake.sales.orders) select x.customer.country as c from x',
            'trino',
            ('customer.country', 'direct'),
        ),
        (
            'with y as (select customer.address as a from lake.sales.orders) select y.a.city as c from y',
            'trino',
            ('customer.address.city', 'direct'),
        ),
        ('select o.customer as c from lake.sales.orders o', 'trino', ('customer', 'direct')),
    ],
)
def test_struct_field_lineage(sql: str, dialect: str, expected: tuple[str, str]) -> None:
    [result] = analyse(sql, dialect=dialect, schema=STRUCTS)
    assert result.columns['c'] == [src('lake.sales.orders', *expected)]


def test_a_column_read_whole_and_by_field_gives_both() -> None:
    sql = 'select coalesce(o.customer.country, cast(o.customer as varchar)) as c from lake.sales.orders o'
    [result] = analyse(sql, dialect='trino', schema=STRUCTS)
    assert set(result.columns['c']) == {src('lake.sales.orders', 'customer', 'transformed')}


def test_struct_fields_in_filters_are_references() -> None:
    sql = (
        'with x as (select id, customer from lake.sales.orders) '
        "select id from x where x.customer.address.city = 'Leeds' and x.customer['country'] = 'UK'"
    )
    [result] = analyse(sql, dialect='duckdb', schema=STRUCTS)
    assert set(result.references) == {
        ('lake.sales.orders', 'customer.address.city'),
        ('lake.sales.orders', 'customer.country'),
    }


LISTS = {'db.s.orders': {'id': 'int', 'items': 'UNKNOWN', 'attrs': 'UNKNOWN'}}


@pytest.mark.parametrize(
    ('sql', 'dialect', 'expected'),
    [
        ('select o.items[1].sku as c from db.s.orders o', 'duckdb', ('items.sku', 'direct')),
        ('select o.items[1] as c from db.s.orders o', 'duckdb', ('items', 'direct')),
        ('select upper(o.items[1].sku) as c from db.s.orders o', 'duckdb', ('items.sku', 'transformed')),
        ('select i.sku as c from `db.s.orders` o, unnest(o.items) as i', 'bigquery', ('items.sku', 'direct')),
        (
            'select i.qty * 2 as c from `db.s.orders` o, unnest(o.items) as i',
            'bigquery',
            ('items.qty', 'transformed'),
        ),
        ('select unnest(items) as c from db.s.orders', 'duckdb', ('items', 'direct')),
        (
            'select t.item.sku as c from db.s.orders o cross join unnest(o.items) as t(item)',
            'duckdb',
            ('items.sku', 'direct'),
        ),
        ("select o.attrs['colour'] as c from db.s.orders o", 'duckdb', ('attrs.colour', 'direct')),
    ],
)
def test_list_and_map_lineage(sql: str, dialect: str, expected: tuple[str, str]) -> None:
    [result] = analyse(sql, dialect=dialect, schema=LISTS)
    assert result.columns['c'] == [src('db.s.orders', *expected)]


def test_list_elements_in_filters_are_references() -> None:
    [result] = analyse(
        "select id from db.s.orders o where o.items[1].sku = 'x'", dialect='duckdb', schema=LISTS
    )
    assert result.references == [('db.s.orders', 'items.sku')]


def test_source_order_does_not_depend_on_the_hash_seed() -> None:
    # sqlglot gathers a column's inputs in a set, so their order follows Python's string hashing,
    # which changes from one process to the next unless PYTHONHASHSEED is fixed.
    script = (
        'from konstruct.sql import analyse\n'
        "sql = \"select sum(case when payment_method = 'credit_card' then amount else 0 end) as cc, "
        'coalesce(refund, fee, amount, tax) as net from main.payments"\n'
        "[result] = analyse(sql, dialect='duckdb', schema={'main.payments': "
        "{'payment_method': 'TEXT', 'amount': 'INT', 'refund': 'INT', 'fee': 'INT', 'tax': 'INT'}})\n"
        'print([(name, [s.column for s in sources]) for name, sources in result.columns.items()])\n'
    )
    outputs = set()
    for seed in range(8):
        env = {**os.environ, 'PYTHONHASHSEED': str(seed)}
        done = subprocess.run(
            [sys.executable, '-c', script], env=env, capture_output=True, text=True, check=True
        )
        outputs.add(done.stdout)
    assert len(outputs) == 1, outputs


def test_canonical_expansion_leaves_the_statement_as_it_was() -> None:
    """Canonical forms swap columns into the analysed statement and back; the statement must be
    unchanged afterwards, including when an expansion fails part-way."""
    import sqlglot
    from sqlglot.lineage import lineage
    from sqlglot.optimizer.qualify import qualify

    from konstruct import sql as module

    for text in (
        'with c as (select x + 1 as a, y + 2 as b from t) select a * b as m from c',
        'select case when x > 0 then x else y end + coalesce(x, 1) as m from t',
        'select * from (select x * 2 as m from t) as s',
    ):
        tree = qualify(sqlglot.parse_one(text), validate_qualify_columns=False)
        build: Any = lineage
        node = build('m', tree, schema=None, copy=False, trim_selects=False)
        before = tree.sql()
        canonical = module._Canonical(None, None, None)
        assert canonical.node(node)
        assert tree.sql() == before
        failing = module._Canonical(None, None, None)
        real = failing.node
        calls: list[int] = []

        def flaky(child: Any, real: Any = real, calls: list[int] = calls) -> str:
            calls.append(1)
            if len(calls) == 2:
                raise RecursionError('deep')
            return str(real(child))

        failing.node = flaky  # type: ignore[assignment]
        with contextlib.suppress(RecursionError):
            failing._expand(node)
        assert tree.sql() == before
