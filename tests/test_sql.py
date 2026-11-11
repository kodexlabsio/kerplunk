from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
from typing import Any

import duckdb
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from konstruct.core.sql import (
    ColumnSource,
    StatementLineage,
    analyse,
    canonical_condition,
    dialect_for,
    strip_templating,
)

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


STAGED = {'db.raw.stg': {'id': 'int', 'amount': 'int'}, 'db.raw.other': {'id': 'int'}}


@pytest.mark.parametrize('schema', [STAGED, {}], ids=['known columns', 'unknown columns'])
@pytest.mark.parametrize(
    'sql',
    [
        'with x as (select * from raw.stg) select sum(amount) as total from x',
        'with x as (select * from raw.stg), y as (select * from x) select sum(y.amount) as total from y',
        'with x as (select * from raw.stg), y as (select * from x) select sum(amount) as total from (select * from y) z',
        'select sum(amount) as total from (select * from (select s.* from raw.stg s) y) z',
        'with x as (select * from raw.stg), o as (select * from raw.other) select sum(amount) as total from x',
        'with x as (select * from raw.stg) select sum(x.amount) as total from x join raw.other o on o.id = x.id',
        'with x as (select *, amount as kept from raw.stg) select sum(kept) as total from x',
    ],
)
def test_a_star_passes_on_the_column_through_ctes_and_subqueries(sql: str, schema: dict[str, Any]) -> None:
    result = one(sql, schema=schema)
    assert result.columns == {'total': [src('db.raw.stg', 'amount', 'transformed')]}
    assert result.expressions == {'total': 'SUM(db.raw.stg.amount)'}


def test_a_star_over_a_join_passes_on_the_column_from_the_relation_that_can_have_it() -> None:
    sql = 'with x as (select * from raw.other o join raw.stg s on o.id = s.id) select sum(amount) as total from x'
    result = one(sql, schema={'db.raw.other': {'id': 'int'}})
    assert result.columns == {'total': [src('db.raw.stg', 'amount', 'transformed')]}
    assert result.expressions == {'total': 'SUM(db.raw.stg.amount)'}


def test_a_star_passes_on_the_column_through_a_union() -> None:
    sql = """
    with x as (select * from raw.stg union all select *, 1 as extra from raw.stg2)
    select sum(amount) as total from x
    """
    result = one(sql, schema={})
    assert result.columns == {
        'total': [src('db.raw.stg', 'amount', 'transformed'), src('db.raw.stg2', 'amount', 'transformed')]
    }
    assert '*' not in result.expressions['total']


def test_a_star_over_a_join_of_unknown_relations_still_names_the_column() -> None:
    result = one(
        'with x as (select * from raw.a join raw.b on a.id = b.id) select sum(amount) as total from x'
    )
    assert '*' not in result.expressions['total']
    assert 'amount' in result.expressions['total']


def test_a_qualify_over_a_star_keeps_the_lineage_of_the_columns_it_passes_on() -> None:
    sql = """
    with latest as (
        select * from raw.events
        qualify row_number() over (partition by event_id order by loaded_at desc) = 1
    )
    select event_id, payload from latest
    """
    result = one(sql, schema={})
    assert result.columns == {
        'event_id': [src('db.raw.events', 'event_id')],
        'payload': [src('db.raw.events', 'payload')],
    }
    assert set(result.references) == {('db.raw.events', 'event_id'), ('db.raw.events', 'loaded_at')}
    assert result.conditions == [
        'QUALIFY: ROW_NUMBER() OVER (PARTITION BY db.raw.events.event_id ORDER BY db.raw.events.loaded_at DESC) = 1'
    ]


@pytest.mark.parametrize(
    ('sql', 'sources', 'conditions'),
    [
        (
            'with x as (select * from orders order by 1 limit 5) select sum(amount) as total from x',
            [ORDERS],
            ['LIMIT: 5', 'ORDER BY: 1'],
        ),
        (
            'with x as (select * from orders order by 2 desc limit 5) select sum(amount) as total from x',
            [ORDERS],
            ['LIMIT: 5', 'ORDER BY: 2 DESC'],
        ),
        (
            'select sum(amount) as total from (select distinct on (1) * from orders order by 1, 2) x',
            [ORDERS],
            ['DISTINCT ON: 1', 'ORDER BY: 1, 2'],
        ),
        (
            'with x as (select * from orders union all select * from payments order by 1 limit 5) '
            'select sum(amount) as total from x',
            [ORDERS, PAYMENTS],
            ['LIMIT: 5', 'ORDER BY: 1'],
        ),
        (
            'with x as (select * from orders union all (select * from payments order by 1 limit 5)) '
            'select sum(amount) as total from x',
            [ORDERS, PAYMENTS],
            ['LIMIT: 5', 'ORDER BY: 1'],
        ),
    ],
)
def test_a_position_over_a_star_of_unknown_columns_stays_the_position(
    sql: str, sources: list[str], conditions: list[str]
) -> None:
    # It names a column no one knows; selecting a column by name before the star must not move it.
    result = one(sql, schema={})
    assert not any(e.startswith('column lineage failed') for e in result.errors)
    assert result.columns == {'total': [src(relation, 'amount', 'transformed') for relation in sources]}
    assert result.references == []
    assert result.conditions == conditions


def test_a_star_over_relations_whose_columns_were_only_guessed_does_not_pick_one() -> None:
    # Other queries were seen to read id and amount of raw.src_a, which may have more columns.
    sql = 'with x as (select * from raw.src_a a join raw.src_b b on a.id = b.a_id) select region from x'
    schema = {'db.raw.src_a': {'id': 'UNKNOWN', 'amount': 'UNKNOWN'}}
    guessed = one(sql, schema=schema, guessed={'db.raw.src_a'})
    assert src('db.raw.src_a', 'region') in guessed.columns['region']
    known = one(sql, schema=schema)
    assert known.columns == {'region': [src('db.raw.src_b', 'region')]}


@pytest.mark.parametrize(
    ('sql', 'columns'),
    [
        ('select o.region from raw.src_a o', {'region'}),
        ("select * from raw.src_a where region = 'x'", {'id', 'amount', 'region'}),
        (
            "with x as (select * from raw.src_a) select * from x where region = 'x'",
            {'id', 'amount', 'region'},
        ),
        ('with x as (select * from raw.src_a), y as (select * from x) select region from y', {'region'}),
    ],
)
def test_a_relation_whose_columns_were_only_guessed_has_the_columns_a_query_names(
    sql: str, columns: set[str]
) -> None:
    result = one(
        sql, schema={'db.raw.src_a': {'id': 'UNKNOWN', 'amount': 'UNKNOWN'}}, guessed={'db.raw.src_a'}
    )
    assert result.columns == {name: [src('db.raw.src_a', name)] for name in columns}
    # What a star selects from such a relation may not be all it has, and the warning says so.
    star = ['SELECT * over relations with unknown columns: db.raw.src_a']
    assert result.errors == (star if '*' in sql else [])


@pytest.mark.parametrize(
    ('replace', 'expression'),
    [
        ('* replace (amount * 100 as amount)', 'SUM(db.main.orders.*)'),
        ('* rename (price as amount)', 'SUM(db.main.orders.*)'),
        ('* exclude (status)', 'SUM(db.main.orders.amount)'),
    ],
)
def test_a_star_that_replaces_or_renames_a_column_keeps_the_relation_in_its_formula(
    replace: str, expression: str
) -> None:
    result = one(f'with x as (select {replace} from orders) select sum(amount) as total from x', schema={})
    assert result.expressions == {'total': expression}


def test_a_star_that_renames_or_replaces_a_column_gives_its_source() -> None:
    renamed = one('with x as (select * rename (amount as amt) from orders) select amt from x', schema={})
    assert renamed.columns == {'amt': [src(ORDERS, 'amount')]}
    replaced = one(
        'with x as (select * replace (amount * 2 as amount) from orders) select amount from x', schema={}
    )
    assert replaced.columns == {'amount': [src(ORDERS, 'amount', 'transformed')]}


def test_the_work_through_a_chain_of_stars_grows_in_proportion_to_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each star looked through every star below it again, for every column it passed on.
    import konstruct.core.sql as sql_module

    looked = 0
    star_sources = sql_module._star_sources

    def counted(scope: Any) -> Any:
        nonlocal looked
        looked += 1
        return star_sources(scope)

    monkeypatch.setattr(sql_module, '_star_sources', counted)

    def work(length: int) -> int:
        nonlocal looked
        ctes = ['c0 as (select * from raw.src)'] + [
            f'c{i} as (select * from c{i - 1} where k{i % 20} > 0)' for i in range(1, length)
        ]
        sql = f'with {", ".join(ctes)} select {", ".join(f"k{j}" for j in range(20))} from c{length - 1}'
        looked = 0
        result = one(sql, schema={})
        assert result.columns['k7'] == [src('db.raw.src', 'k7')]
        return looked

    assert work(32) <= 2.5 * work(16)


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


@pytest.mark.parametrize('upper', [False, True], ids=['as a build lists them', 'as Snowflake spells them'])
def test_snowflake_names_match_the_schema_in_either_case(upper: bool) -> None:
    # Snowflake folds unquoted names to upper case; a build lists them in lower case.
    schema = {k.upper(): {c.upper(): t for c, t in v.items()} for k, v in SCHEMA.items()} if upper else SCHEMA
    star = one('with x as (select * from orders) select * from x', dialect='snowflake', schema=schema)
    assert star.errors == []
    assert star.columns == {name: [src(ORDERS, name)] for name in SCHEMA[ORDERS]}
    sql = """
    select o.id from orders o
    where exists (select 1 from payments where order_id = o.id and method = 'card')
    """
    filtered = one(sql, dialect='snowflake', schema=schema)
    assert filtered.errors == []
    assert filtered.columns == {'id': [src(ORDERS, 'id')]}
    assert set(filtered.references) == {(PAYMENTS, 'order_id'), (PAYMENTS, 'method'), (ORDERS, 'id')}
    assert "WHERE: db.main.payments.order_id = db.main.orders.id AND db.main.payments.method = 'card'" in (
        filtered.conditions
    )


@pytest.mark.parametrize('dialect', ['snowflake', 'postgres', 'duckdb'])
@pytest.mark.parametrize(
    ('sql', 'schema', 'column'),
    [
        ('select o."id" from orders o', {ORDERS: {'id': 'int'}}, 'id'),
        ('select o."CustomerId" from orders o', {ORDERS: {'customerid': 'int'}}, 'customerid'),
        ('select o.customerid from orders o', {ORDERS: {'CustomerId': 'int'}}, 'customerid'),
        ('select "Amount" from orders', {ORDERS: {'Amount': 'int'}}, 'amount'),
    ],
)
def test_quoted_names_match_the_schema_in_any_case(
    sql: str, schema: dict[str, Any], column: str, dialect: str
) -> None:
    result = one(sql, dialect=dialect, schema=schema)
    assert result.errors == []
    assert result.columns == {column: [src(ORDERS, column)]}


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
        'from konstruct.core.sql import analyse\n'
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

    from konstruct.core import sql as module

    for text in (
        'with c as (select x + 1 as a, y + 2 as b from t) select a * b as m from c',
        'select case when x > 0 then x else y end + coalesce(x, 1) as m from t',
        'select * from (select x * 2 as m from t) as s',
    ):
        tree = qualify(sqlglot.parse_one(text), validate_qualify_columns=False)
        build: Any = lineage
        node = build('m', tree, schema=None, copy=False, trim_selects=False)
        before = tree.sql()
        canonical = module.Canonical(None, None, None)
        assert canonical.node(node)
        assert tree.sql() == before
        failing = module.Canonical(None, None, None)
        real = failing.node
        calls: list[int] = []

        def flaky(child: Any, *args: Any, real: Any = real, calls: list[int] = calls) -> str:
            calls.append(1)
            if len(calls) == 2:
                raise RecursionError('deep')
            return str(real(child, *args))

        failing.node = flaky  # type: ignore[assignment]
        with contextlib.suppress(RecursionError):
            failing._expand(node)
        assert tree.sql() == before


# A bind parameter or a Snowflake stage where a relation's name belongs, which the parser accepts
# and sqlglot's qualify cannot normalise. Found by the SQL fuzz target.
PARAMETER_RELATIONS = [
    (None, 'select c from ?'),
    ('duckdb', 'select c from a.?'),
    ('postgres', 'select c from ? as t'),
    ('snowflake', 'select c from @p'),
    ('snowflake', 'select c from @p/x.csv'),
]


@pytest.mark.parametrize(('dialect', 'sql'), PARAMETER_RELATIONS)
def test_a_relation_named_by_a_parameter_has_no_column_lineage(dialect: str | None, sql: str) -> None:
    [result] = analyse(sql, dialect=dialect, target='db.schema.model')
    assert result.columns == {}
    assert result.errors == ['column lineage failed: a relation is named by a parameter or a stage']


CRASHES = [
    ('duckdb', '->'),
    ('duckdb', 'select ->'),
    ('duckdb', 'select a -> from t'),
    ('postgres', '#>'),
    ('bigquery', 'select {:}'),
]


@pytest.mark.parametrize(('dialect', 'sql'), CRASHES)
def test_sql_the_parser_fails_on_is_unparsed(dialect: str, sql: str) -> None:
    results = analyse(sql, dialect=dialect)
    assert [r.kind for r in results] == ['unparsed'] and results[0].errors


@pytest.mark.parametrize(
    'sql',
    [
        'select * from (x {}) s',
        'select * from t, (x {}) s',
        'select * from t join (x {}) s on true',
        # The input the SQL fuzz target found, as it decodes it: the dialect, then the text.
        "i; from '__konstruct__'\n\n\x04,\n\n(xS\x7f\x7fdbz.schema\x7f\x7f\x7f\x7f\x7f\x7f\x7f\x7f\x7f\x7f"
        '{\x7f\x7f\x7f\x7f\x7f\x7f\x7f\x7f\x7f\x00\x00\x00}\x7fo)Y\x01;\x19\x00\x08\x00O;\x00\x00\x00\x00\x00'
        '\x19\x00\x00\x00\x00\x04<\x00',
    ],
)
def test_sql_that_fails_a_check_of_sqlglots_reports_its_lineage_failed(sql: str) -> None:
    # Snowflake parses `(x {})` as a subquery of a struct, which sqlglot's scopes assert is a query.
    results = analyse(sql, dialect='snowflake', target='db.schema.model')
    assert any(e.startswith('column lineage failed: STRUCT(') for r in results for e in r.errors)


@pytest.mark.parametrize(('dialect', 'sql'), CRASHES)
def test_every_reader_of_sql_survives_what_the_parser_fails_on(dialect: str, sql: str) -> None:
    from konstruct.analysis import conflicts
    from konstruct.analysis.explain import Explainer
    from konstruct.core.graph import Graph
    from konstruct.engine.build import _filter_columns
    from konstruct.inputs import looker, powerbi, spark
    from konstruct.outputs import export
    from konstruct.semantics import ontology, ossie

    assert looker._derived_reads(sql, dialect) is None
    assert powerbi.native_reads(sql, dialect) is None
    assert spark.without_session_catalog(f'{sql} spark_catalog', dialect) == f'{sql} spark_catalog'
    assert _filter_columns(sql, dialect) is None
    assert conflicts.top_function(sql, dialect) is None
    assert export._subtype(sql, 'transformed', dialect) == 'TRANSFORMATION'
    assert ossie._tree(sql, dialect) is None
    graph = Graph()
    graph.meta['dialect'] = dialect
    assert Explainer(graph).namer.parse(sql) is None
    assert ontology.join_columns(graph, f'INNER JOIN: {sql}') == []


def test_a_filter_on_the_nullable_side_of_a_left_join_belongs_to_the_join() -> None:
    """A CTE's or subquery's filter on the side of a LEFT JOIN that may find no row decides which
    rows join: when the CTE only selects and filters, it is part of the join's ON, as a condition
    written there is; otherwise it is a LEFT JOIN WHERE. The same filter in the outer WHERE
    removes rows, and stays a WHERE. The nullable relations are named."""
    joined = one(
        'with c as (select id, first_name from db.main.customers where last_name is null) '
        'select o.id from db.main.orders o left join c on c.id = o.customer_id'
    )
    on = one(
        'select o.id from db.main.orders o left join db.main.customers c '
        'on c.id = o.customer_id and c.last_name is null'
    )
    outer = one(
        'select o.id from db.main.orders o left join db.main.customers c on c.id = o.customer_id '
        'where c.last_name is null'
    )
    grouped = one(
        'with c as (select id from db.main.customers where last_name is null group by id) '
        'select o.id from db.main.orders o left join c on c.id = o.customer_id'
    )
    both = 'LEFT JOIN ON: db.main.customers.id = db.main.orders.customer_id AND db.main.customers.last_name IS NULL'
    assert joined.conditions == on.conditions == [both]
    assert outer.conditions == [
        'LEFT JOIN ON: db.main.customers.id = db.main.orders.customer_id',
        'WHERE: db.main.customers.last_name IS NULL',
    ]
    assert grouped.conditions == [
        'GROUP BY: db.main.customers.id',
        'LEFT JOIN ON: db.main.customers.id = db.main.orders.customer_id',
        'LEFT JOIN WHERE: db.main.customers.last_name IS NULL',
    ]
    assert joined.nullable == on.nullable == outer.nullable == {'db.main.customers': 'LEFT'}
    assert StatementLineage.from_dict(joined.to_dict()) == joined


def test_an_inner_join_on_the_nullable_side_stays_an_inner_join() -> None:
    """Joining two relations and left joining the result is not left joining each in turn."""
    nested = one(
        'select o.id from db.main.orders o left join (select c.id from db.main.customers c '
        'join db.main.payments p on p.id = c.id) x on x.id = o.customer_id'
    )
    chained = one(
        'select o.id from db.main.orders o left join db.main.customers c on c.id = o.customer_id '
        'left join db.main.payments p on p.id = c.id'
    )
    assert 'JOIN ON: db.main.payments.id = db.main.customers.id' in nested.conditions
    assert 'LEFT JOIN ON: db.main.payments.id = db.main.customers.id' in chained.conditions
    assert nested.conditions != chained.conditions


def test_the_nullable_sides_of_right_and_full_joins_and_a_relation_read_twice() -> None:
    """A filter under a RIGHT or FULL JOIN is that side's. Under a FULL JOIN it is not the same as
    a condition in the ON, which keeps the unmatched rows of both sides."""
    right = one(
        'select c.id from (select * from db.main.orders where amount > 0) o '
        'right join db.main.customers c on c.id = o.customer_id group by c.id'
    )
    assert right.conditions == [
        'GROUP BY: db.main.customers.id',
        'RIGHT JOIN ON: db.main.customers.id = db.main.orders.customer_id',
        'RIGHT JOIN WHERE: db.main.orders.amount > 0',
    ]
    assert right.nullable == {'db.main.orders': 'RIGHT'}
    full = one(
        'select 1 as x from db.main.orders o full join (select * from db.main.payments where amount > 0) p '
        'on p.order_id = o.id'
    )
    assert full.conditions == [
        'FULL JOIN ON: db.main.payments.order_id = db.main.orders.id',
        'FULL JOIN WHERE: db.main.payments.amount > 0',
    ]
    assert full.nullable == {'db.main.orders': 'FULL', 'db.main.payments': 'FULL'}
    # Read on the preserved side too, a relation's own rows decide the result.
    twice = one(
        'select o.id from db.main.orders o left join db.main.orders p on p.id = o.id '
        'left join (select order_id from db.main.payments where amount > 0 group by order_id) q '
        'on q.order_id = o.id'
    )
    # The joined reading of orders is on the nullable side, though orders is read on the other too.
    [joined] = [k for k in twice.nullable if k.startswith('db.main.orders#')]
    assert twice.nullable == {joined: 'LEFT', 'db.main.payments': 'LEFT'}
    assert joined.split('#')[1] not in twice.expressions['id']
    # A subquery in the joined side's filter, and a union's branches, are on that side too.
    inner = one(
        'select o.id from db.main.orders o left join (select id from db.main.customers '
        'where id in (select order_id from db.main.payments where amount > 0) '
        'union all select id from db.main.customers where first_name is null) c on c.id = o.customer_id'
    )
    assert 'LEFT JOIN WHERE: db.main.payments.amount > 0' in inner.conditions
    # Each branch reads customers: the filter is one reading's.
    filters = [c for c in inner.conditions if c.endswith('first_name IS NULL')]
    assert len(filters) == 1 and filters[0].startswith('LEFT JOIN WHERE: db.main."customers#')
    assert inner.nullable == {'db.main.customers': 'LEFT', 'db.main.payments': 'LEFT'}


def test_a_condition_before_a_window_or_a_limit_says_so() -> None:
    """A filter that a window function, QUALIFY or LIMIT sees the rows of changes what they give,
    so where it is applied is part of it; a filter after them, or across a plain projection, is
    a WHERE as before."""
    numbered = 'select id, amount, row_number() over (order by amount) as rn from db.main.orders'
    inside = one(f'{numbered} where amount > 0')
    nested = one(
        f'select * from ({numbered.replace("db.main.orders", "(select * from db.main.orders where amount > 0) o")}) x'
    )
    outside = one(f'select * from ({numbered}) x where amount > 0')
    assert inside.conditions == nested.conditions == ['WHERE BEFORE WINDOW: db.main.orders.amount > 0']
    assert outside.conditions == ['WHERE: db.main.orders.amount > 0']
    assert inside.read_before == {'db.main.orders': ['WINDOW']}
    limited = one(
        'select * from (select id from db.main.orders where amount > 0 order by id limit 10 offset 5) x '
        'where id > 1'
    )
    assert limited.conditions == [
        'LIMIT: 10',
        'OFFSET: 5',
        'ORDER BY: db.main.orders.id',
        'WHERE BEFORE LIMIT: db.main.orders.amount > 0',
        'WHERE: db.main.orders.id > 1',
    ]
    qualified = one(
        'select id from db.main.orders where amount > 0 qualify row_number() over (order by id) = 1 limit 3'
    )
    assert qualified.conditions == [
        'LIMIT: 3',
        'QUALIFY BEFORE LIMIT: ROW_NUMBER() OVER (ORDER BY db.main.orders.id) = 1',
        'WHERE BEFORE WINDOW, LIMIT: db.main.orders.amount > 0',
    ]


def test_distinct_set_operations_and_group_by_all_are_conditions() -> None:
    """Each decides which rows there are: DISTINCT, UNION (not UNION ALL), INTERSECT and EXCEPT,
    and the keys GROUP BY ALL groups by."""
    assert one('select distinct customer_id, status from db.main.orders').conditions == [
        'DISTINCT: db.main.orders.customer_id, db.main.orders.status'
    ]
    assert one(
        'select distinct on (customer_id) customer_id, amount from db.main.orders order by customer_id, amount desc'
    ).conditions == [
        'DISTINCT ON: db.main.orders.customer_id',
        'ORDER BY: db.main.orders.customer_id, db.main.orders.amount DESC',
    ]
    union = 'select id from db.main.orders union {} select id from db.main.payments'
    assert one(union.format('')).conditions == ['UNION: DISTINCT']
    assert one(union.format('all')).conditions == []
    assert one(union.replace('union {}', 'except')).conditions == ['EXCEPT: DISTINCT']
    assert one(union.replace('union {}', 'intersect all')).conditions == ['INTERSECT: ALL']
    grouped = one('select customer_id, status, sum(amount) as total from db.main.orders group by all')
    assert grouped.conditions == ['GROUP BY: db.main.orders.customer_id, db.main.orders.status']
    assert ('db.main.orders', 'status') in grouped.references


@pytest.mark.parametrize('dialect', ['duckdb', 'snowflake', 'databricks', 'bigquery'])
def test_group_by_all_groups_by_the_select_items_without_an_aggregate(dialect: str) -> None:
    select = """
    with o as (select * from orders)
    select customer_id, upper(status) as status_code, count(*) as order_count, sum(amount) as total
    from o
    """
    kwargs: dict[str, Any] = {'dialect': dialect}
    grouped = one(f'{select} group by all', **kwargs)
    assert grouped.conditions == one(f'{select} group by 1, 2', **kwargs).conditions
    assert set(grouped.references) == {(ORDERS, 'customer_id'), (ORDERS, 'status')}
    if dialect == 'duckdb':
        assert grouped.conditions == ['GROUP BY: UPPER(db.main.orders.status), db.main.orders.customer_id']


def test_group_by_all_leaves_out_window_functions() -> None:
    sql = """
    select customer_id, sum(amount) as total, rank() over (order by sum(amount)) as position
    from orders group by all
    """
    assert one(sql).conditions == ['GROUP BY BEFORE WINDOW: db.main.orders.customer_id']


def test_group_by_all_of_aggregates_alone_and_with_named_keys() -> None:
    assert one('select sum(amount) as total from orders group by all').conditions == []
    named = one('select status, count(*) as n from orders group by all status', dialect='postgres')
    assert named.conditions == ['GROUP BY: db.main.orders.status']


def test_group_by_all_leaves_out_functions_sqlglot_does_not_know() -> None:
    # my_udaf is a user-defined aggregate, and sqlglot does not know DuckDB's product as one.
    sql = 'select customer_id, my_udaf(amount) as spread, product(amount) as factor from orders group by all'
    result = one(sql)
    assert result.conditions == ['GROUP BY: db.main.orders.customer_id']
    assert result.references == [(ORDERS, 'customer_id')]


@pytest.mark.parametrize('schema', [SCHEMA, {}], ids=['known columns', 'unknown columns'])
@pytest.mark.parametrize(
    ('condition', 'outer', 'inner'),
    [
        ('o.id in (select order_id from payments)', 'id', {'order_id'}),
        ('o.id not in (select order_id from payments where amount > 0)', 'id', {'order_id', 'amount'}),
        ('exists (select 1 from payments where order_id = o.id)', 'id', {'order_id'}),
        ('not exists (select 1 from payments p where p.order_id = o.id)', 'id', {'order_id'}),
        ('o.amount > (select avg(amount) from payments)', 'amount', {'amount'}),
    ],
)
def test_a_subquery_filter_beside_a_join(
    condition: str, outer: str, inner: set[str], schema: dict[str, Any]
) -> None:
    sql = f"""
    select o.id, c.first_name
    from orders o join customers c on o.customer_id = c.id
    where {condition}
    """
    result = one(sql, schema=schema)
    assert [e for e in result.errors if 'a correlated subquery' not in e] == []
    assert result.columns == {'id': [src(ORDERS, 'id')], 'first_name': [src(CUSTOMERS, 'first_name')]}
    assert set(result.references) == {
        (ORDERS, 'customer_id'),
        (CUSTOMERS, 'id'),
        (ORDERS, outer),
        *((PAYMENTS, column) for column in inner),
    }


@pytest.mark.parametrize('schema', [SCHEMA, {}], ids=['known', 'unknown'])
def test_a_subquery_that_reuses_an_outer_alias_reads_its_own_relation(schema: dict[str, Any]) -> None:
    result = one(
        'select o.id from orders o join customers c on c.id = o.customer_id '
        'where o.id in (select c.order_id from payments c)',
        schema=schema,
    )
    assert (PAYMENTS, 'order_id') in result.references
    assert (CUSTOMERS, 'order_id') not in result.references
    assert f'WHERE: {ORDERS}.id IN (SELECT {PAYMENTS}.order_id AS order_id FROM {PAYMENTS})' in (
        result.conditions
    )


def test_a_subquery_that_reuses_a_lateral_alias_reads_its_own_relation() -> None:
    result = one(
        'select o.id, l.t from orders o cross join lateral '
        '(select sum(p.amount) as t from payments p where p.order_id = o.id) l '
        'where o.id in (select l.id from customers l)'
    )
    assert set(result.references) == {(ORDERS, 'id'), (PAYMENTS, 'order_id'), (CUSTOMERS, 'id')}
    assert f'WHERE: {ORDERS}.id IN (SELECT {CUSTOMERS}.id AS id FROM {CUSTOMERS})' in result.conditions


TREE = {ORDERS: {'id': 'int', 'parent_id': 'int', 'amount': 'int', 'status': 'text'}}
# Each pair reads one relation twice and differs only in which reading each column is from; the
# third of each is the first with other aliases.
SELF_JOINS = {
    'join': (
        'select o.id from orders o join orders p on p.parent_id = o.id',
        'select o.id from orders o join orders p on o.parent_id = p.id',
        'select a.id from orders a join orders b on b.parent_id = a.id',
    ),
    'exists': (
        'select o.id from orders o where exists (select 1 from orders p where p.parent_id = o.id)',
        'select o.id from orders o where exists (select 1 from orders p where o.parent_id = p.id)',
        'select x.id from orders x where exists (select 1 from orders y where y.parent_id = x.id)',
    ),
    'in': (
        'select o.id from orders o where o.id in (select p.parent_id from orders p where p.amount > o.amount)',
        'select o.id from orders o where o.id in (select p.parent_id from orders p where o.amount > p.amount)',
        'select c.id from orders c where c.id in (select d.parent_id from orders d where d.amount > c.amount)',
    ),
    'cte': (
        'with t as (select o.id from orders o join orders p on p.parent_id = o.id) select id from t',
        'with t as (select o.id from orders o join orders p on o.parent_id = p.id) select id from t',
        'with t as (select k.id from orders k join orders m on m.parent_id = k.id) select id from t',
    ),
    'cte read twice': (
        'with t as (select * from orders) select x.id from t x join t y on y.parent_id = x.id',
        'with t as (select * from orders) select x.id from t x join t y on x.parent_id = y.id',
        'with t as (select * from orders) select a.id from t a join t b on b.parent_id = a.id',
    ),
    'cte read twice in exists': (
        'with t as (select * from orders) select x.id from t x where exists (select 1 from t y where y.parent_id = x.id)',
        'with t as (select * from orders) select x.id from t x where exists (select 1 from t y where x.parent_id = y.id)',
        'with t as (select * from orders) select a.id from t a where exists (select 1 from t b where b.parent_id = a.id)',
    ),
}


@pytest.mark.parametrize('schema', [TREE, {}], ids=['known', 'unknown'])
@pytest.mark.parametrize('case', SELF_JOINS)
def test_each_reading_of_a_relation_read_twice_has_a_name_of_its_own(
    case: str, schema: dict[str, Any]
) -> None:
    """A self-join that follows the parent rather than the child reads the same columns but means
    something else, so its conditions print differently; other aliases print the same."""
    child, parent, renamed = (
        (result.conditions, result.expressions)
        for result in (one(sql, schema=schema) for sql in SELF_JOINS[case])
    )
    assert child != parent
    assert child == renamed


def test_the_readings_of_a_relation_read_twice_are_numbered_in_order() -> None:
    result = one('select o.id from orders o join orders p on p.parent_id = o.id', schema=TREE)
    assert result.conditions == ['JOIN ON: db.main."orders#1".parent_id = db.main."orders#2".id']
    assert result.expressions == {'id': 'db.main."orders#2".id'}
    # The columns the rows depend on are the relation's own, whichever reading they are from.
    assert set(result.references) == {(ORDERS, 'id'), (ORDERS, 'parent_id')}


def test_a_cte_read_twice_names_the_readings_of_what_it_reads() -> None:
    """Each reference to a CTE reads the relations it reads once more, so a self-join in a CTE that
    is itself joined to itself has four readings, told apart by what is said of each."""
    sql = (
        'with t as (select o.id, p.amount from orders o join orders p on p.parent_id = o.id) '
        'select x.id, x.amount - y.amount as gap from t x join t y on y.id > x.id'
    )
    gap = one(sql, schema=TREE).expressions['gap']
    assert len(set(re.findall(r'orders#[\d/]+', gap))) == 2
    assert (
        one(sql.replace('x.amount - y.amount', 'y.amount - x.amount'), schema=TREE).expressions['gap'] != gap
    )
    assert (
        one(sql.replace('x.', 'a.').replace(' x ', ' a ').replace('= x', '= a'), schema=TREE).expressions[
            'gap'
        ]
        == gap
    )


def test_a_relation_whose_name_has_a_hash_is_not_a_reading() -> None:
    """Canonical text doubles a ``#`` in a relation's name, so ``events#2`` is never read back as
    the second reading of ``events``."""
    result = one(
        'select e.id, f.id as other from main."events#2" e join main.events f on f.id = e.id', schema={}
    )
    assert result.expressions == {'id': 'db.main."events##2".id', 'other': 'db.main.events.id'}
    assert result.conditions == ['JOIN ON: db.main.events.id = db.main."events##2".id']


def test_a_cte_read_by_two_others_names_each_reference_and_moving_a_filter_between_them_shows() -> None:
    """jaffle_shop's customers reads stg_orders through one CTE that two others read, each joined to
    customers on its own: the two readings meet through customers, so each is named, and a filter
    moved from one to the other, which changes the rows, changes what is printed. Putting the two
    CTEs the other way round changes nothing."""
    orders = 'orders as (select * from stg_orders)'

    def customers(counted_where: str, paid_where: str, swap: bool = False) -> StatementLineage:
        counted = (
            f'counted as (select customer_id, count(*) as n from orders {counted_where} group by customer_id)'
        )
        paid = (
            'paid as (select orders.customer_id, sum(p.amount) as total from payments p '
            f'left join orders on p.order_id = orders.order_id {paid_where} group by orders.customer_id)'
        )
        ctes = [orders, paid, counted] if swap else [orders, counted, paid]
        final = (
            'select c.id, counted.n, paid.total from customers c left join counted on counted.customer_id = c.id '
            'left join paid on paid.customer_id = c.id'
        )
        return one(f'with {", ".join(ctes)} {final}', schema={})

    def printed(result: StatementLineage) -> tuple[list[str], dict[str, str]]:
        return result.conditions, result.expressions

    first = customers("where status = 'done'", '')
    assert any('stg_orders#' in c for c in first.conditions)
    assert printed(first) == printed(customers("where status = 'done'", '', swap=True))
    assert printed(first) != printed(customers('', "where orders.status = 'done'"))


def test_the_order_of_scalar_subqueries_over_one_relation_does_not_matter() -> None:
    top = '(select max(p.amount) from orders p where p.parent_id = o.id) as top'
    low = '(select min(p.amount) from orders p where p.id = o.parent_id) as low'
    first, second = (
        one(f'select o.id, {x}, {y} from orders o', schema=TREE) for x, y in ((top, low), (low, top))
    )
    assert (first.conditions, first.expressions) == (second.conditions, second.expressions)


def test_a_self_join_in_t_sql_prints_each_reading_once_bracketed() -> None:
    gap = one(
        'select o.amount - p.amount as gap from orders o join orders p on p.id = o.parent_id',
        schema=TREE,
        dialect='tsql',
    ).expressions['gap']
    assert re.fullmatch(r'db\.main\.\[orders#\d\]\.amount - db\.main\.\[orders#\d\]\.amount', gap), gap


def test_a_formula_over_a_relation_read_twice_names_each_reading() -> None:
    sql = 'select o.amount - p.amount as gap from orders o join orders p on p.id = o.parent_id'
    gap = one(sql, schema=TREE).expressions['gap']
    swapped = one(sql.replace('o.amount - p.amount', 'p.amount - o.amount'), schema=TREE).expressions['gap']
    assert gap == 'db.main."orders#1".amount - db.main."orders#2".amount' and swapped != gap
    # A relation read once keeps its plain name.
    assert one('select o.amount from orders o', schema=TREE).expressions['amount'] == 'db.main.orders.amount'


def test_a_correlated_subquery_names_the_outer_column() -> None:
    sql = 'select o.id from orders o where exists (select 1 from payments where order_id = o.id)'
    assert 'WHERE: db.main.payments.order_id = db.main.orders.id' in one(sql, schema={}).conditions


@pytest.mark.parametrize('schema', [SCHEMA, {}], ids=['known columns', 'unknown columns'])
@pytest.mark.parametrize(
    'join',
    [
        'join (select order_id, sum(amount) as total from payments group by order_id) l on l.order_id = o.id',
        'cross join lateral (select sum(p.amount) as total from payments p where p.order_id = o.id) l',
        'left join lateral (select sum(p.amount) as total from payments p where p.order_id = o.id) l on true',
    ],
)
def test_the_columns_a_joined_subquery_selects_are_not_references(join: str, schema: dict[str, Any]) -> None:
    result = one(f'select o.id, l.total from orders o {join}', schema=schema)
    assert set(result.references) == {(PAYMENTS, 'order_id'), (ORDERS, 'id')}


def test_the_columns_of_a_join_using_are_references() -> None:
    result = one('select o.amount from orders o join payments p using (id)')
    assert set(result.references) == {(ORDERS, 'id'), (PAYMENTS, 'id')}


def test_guessing_the_relation_of_a_column_in_a_subquery_is_reported() -> None:
    sql = "select o.id from orders o where exists (select 1 from payments where order_id = o.id and status = 'x')"
    warning = (
        "unqualified columns in a subquery taken to be its own relation's, though a relation of the outer query "
        'could also have them (a correlated subquery): '
    )
    # orders has unknown columns too, so it could have them.
    assert one(sql, schema={}).errors == [f'{warning}payments.order_id, payments.status']
    # Not when the subquery's relation has known columns, or no outer relation could have the name.
    assert one(sql).errors == []
    assert one('select id from orders where id in (select order_id from payments)', schema={}).errors == [
        f'{warning}payments.order_id'
    ]
    known = {ORDERS: SCHEMA[ORDERS]}
    assert one('select id from orders where id in (select order_id from payments)', schema=known).errors == []


@pytest.mark.parametrize(
    ('sql', 'dialect'),
    [
        ('select id from arrs cross join unnest(arr) as t(x)', 'duckdb'),
        ('select id from arrs, unnest(arr) as x', 'bigquery'),
        ('select a.id from arrs a, lateral flatten(input => a.arr) f', 'snowflake'),
    ],
)
def test_the_list_a_join_unnests_is_a_reference(sql: str, dialect: str) -> None:
    # An empty list joins no row: the list decides which rows there are.
    result = one(sql, dialect=dialect, schema={'db.main.arrs': {'id': 'int', 'arr': 'int[]'}})
    assert ('db.main.arrs', 'arr') in result.references


@pytest.mark.parametrize(
    ('sql', 'inner', 'condition'),
    [
        (
            "select o.id from orders o where o.customer_id in (select id from customers where country = 'NL')",
            'customers.id',
            'IN (SELECT db.main.customers.id',
        ),
        (
            'select o.id from orders o where o.amount > (select avg(amount) from payments)',
            'payments.amount',
            'AVG(db.main.payments.amount)',
        ),
        (
            'select id from orders where exists (select 1 from order_lines where order_id = id)',
            'order_lines.id',
            'db.main.order_lines.order_id = db.main.order_lines.id',
        ),
        (
            'with c as (select id, amount from orders) select c.id from c where c.amount > (select avg(amount) from payments)',
            'payments.amount',
            'AVG(db.main.payments.amount)',
        ),
    ],
)
def test_a_bare_name_in_a_subquery_is_its_own_relations_as_sql_reads_it(
    sql: str, inner: str, condition: str
) -> None:
    # SQL looks the name up in the subquery first, and its relation, with unknown columns, may well
    # have it. That a relation of the outer query has the name too is reported.
    result = one(sql, schema={ORDERS: SCHEMA[ORDERS]})
    assert (f'db.main.{inner.split(".")[0]}', inner.split('.')[1]) in result.references
    assert any(condition in c for c in result.conditions)
    assert result.errors == [
        "unqualified columns in a subquery taken to be its own relation's, though a relation of the outer query "
        f'could also have them (a correlated subquery): {inner}'
    ]


@pytest.mark.parametrize(
    'schema', [SCHEMA, {}, {ORDERS: SCHEMA[ORDERS]}], ids=['known', 'unknown', 'outer known']
)
@pytest.mark.parametrize(
    'lateral',
    [
        'cross join lateral (select sum(amount) as total from payments where order_id = o.id) l',
        'left join lateral (select sum(p.amount) as total from payments p where p.order_id = o.id) l on true',
        ', lateral (select sum(amount) as total from payments where order_id = o.id) l',
    ],
    ids=['cross join', 'left join', 'comma'],
)
def test_a_lateral_subquerys_columns_are_traced_through_its_select(
    lateral: str, schema: dict[str, Any]
) -> None:
    """The outer column a lateral subquery is correlated on picks its rows; what it selects is
    its own."""
    result = one(f'select o.id, l.total from orders o {lateral}', schema=schema)
    assert result.columns == {'id': [src(ORDERS, 'id')], 'total': [src(PAYMENTS, 'amount', 'transformed')]}
    assert set(result.references) == {(PAYMENTS, 'order_id'), (ORDERS, 'id')}


@pytest.mark.parametrize('schema', [SCHEMA, {}], ids=['known', 'unknown'])
def test_a_lateral_subquery_can_select_the_outer_columns_it_is_correlated_on(schema: dict[str, Any]) -> None:
    result = one(
        'select o.id, l.share from orders o cross join lateral '
        '(select o.amount / sum(p.amount) as share from payments p where p.order_id = o.id) l',
        schema=schema,
    )
    assert result.columns['share'] == [
        src(ORDERS, 'amount', 'transformed'),
        src(PAYMENTS, 'amount', 'transformed'),
    ]


def test_a_filter_on_a_lateral_subquerys_column_is_the_select_that_makes_it() -> None:
    result = one(
        'select o.id from orders o cross join lateral '
        '(select sum(p.amount) as total from payments p where p.order_id = o.id) l where l.total > 10'
    )
    assert f'WHERE: (SUM({PAYMENTS}.amount)) > 10' in result.conditions
    assert (PAYMENTS, 'amount') in result.references


def test_a_relation_whose_columns_were_guessed_keeps_a_name_it_was_seen_to_have() -> None:
    schema = {ORDERS: SCHEMA[ORDERS], PAYMENTS: {'amount': 'UNKNOWN', 'order_id': 'UNKNOWN'}}
    result = one(
        'select o.id from orders o where o.amount > (select avg(amount) from payments)',
        schema=schema,
        guessed={PAYMENTS},
    )
    assert (PAYMENTS, 'amount') in result.references


def test_unqualified_columns_of_a_relation_with_unknown_columns_are_references() -> None:
    result = one("select id from orders where status = 'completed' group by id", schema={})
    assert result.references == [(ORDERS, 'status'), (ORDERS, 'id')]


def test_what_distinct_on_and_a_limit_pick_rows_by_are_references() -> None:
    picked = one(
        'select distinct on (customer_id) id from orders order by customer_id, ordered_at desc',
        dialect='postgres',
    )
    assert set(picked.references) == {(ORDERS, 'customer_id'), (ORDERS, 'ordered_at')}
    assert one('select id from orders order by amount desc limit 3').references == [(ORDERS, 'amount')]
    # A name an ORDER BY gives a select item is that item.
    named = one('select id, amount * 2 as doubled from orders order by doubled desc limit 3')
    assert named.references == [(ORDERS, 'amount')]
    assert one('select id from orders order by amount').references == []


@pytest.mark.parametrize('schema', [SCHEMA, {}], ids=['known columns', 'unknown columns'])
@pytest.mark.parametrize(
    ('sql', 'references'),
    [
        (
            'with d as (select distinct customer_id, upper(status) as s from orders) select count(*) as n from d',
            {(ORDERS, 'customer_id'), (ORDERS, 'status')},
        ),
        (
            'select id, amount from orders union select id, amount from payments',
            {(ORDERS, 'id'), (ORDERS, 'amount'), (PAYMENTS, 'id'), (PAYMENTS, 'amount')},
        ),
        (
            'select id from orders intersect all select order_id from payments',
            {(ORDERS, 'id'), (PAYMENTS, 'order_id')},
        ),
        (
            'select id from orders except select order_id from payments',
            {(ORDERS, 'id'), (PAYMENTS, 'order_id')},
        ),
        ('select id from orders union all select id from payments', set()),
    ],
)
def test_the_columns_that_distinct_and_a_union_compare_rows_by_are_references(
    sql: str, references: set[tuple[str, str]], schema: dict[str, Any]
) -> None:
    assert set(one(sql, schema=schema).references) == references


@pytest.mark.parametrize('schema', [SCHEMA, {}], ids=['known columns', 'unknown columns'])
def test_a_distinct_star_is_the_same_condition_whatever_is_selected_from_it(schema: dict[str, Any]) -> None:
    def distinct(columns: str) -> tuple[list[str], set[tuple[str, str]]]:
        result = one(f'with x as (select distinct * from orders) select {columns} from x', schema=schema)
        return result.conditions, set(result.references)

    assert distinct('amount') == distinct('amount, status') == distinct('count(*) as n')
    if not schema:
        assert distinct('amount') == (['DISTINCT: db.main.orders.*'], set())


@pytest.mark.parametrize('schema', [SCHEMA, {}], ids=['known columns', 'unknown columns'])
@pytest.mark.parametrize(
    ('sql', 'condition'),
    [
        (
            'select id, amount from orders union all select id, amount from payments order by amount desc limit 5',
            'ORDER BY: UNION(db.main.orders.amount, db.main.payments.amount) DESC',
        ),
        (
            'with u as (select id, amount from orders union all select id, amount from payments order by 2 limit 5) '
            'select id from u',
            'ORDER BY: UNION(db.main.orders.amount, db.main.payments.amount)',
        ),
    ],
)
def test_the_order_by_of_a_set_operation_names_the_items_of_every_branch(
    sql: str, condition: str, schema: dict[str, Any]
) -> None:
    result = one(sql, schema=schema)
    assert set(result.references) == {(ORDERS, 'amount'), (PAYMENTS, 'amount')}
    assert result.conditions == ['LIMIT: 5', condition]


def test_a_distinct_star_through_a_cte_names_the_relation_it_reads() -> None:
    def distinct(relation: str) -> list[str]:
        return one(f'with s as (select * from {relation}) select distinct * from s', schema={}).conditions

    assert distinct('raw.a') == ['DISTINCT: db.raw.a.*']
    assert distinct('raw.b') == ['DISTINCT: db.raw.b.*']


def test_an_expression_a_set_operation_orders_by_names_the_items_of_every_branch() -> None:
    schema = {'db.main.t1': {'a': 'int', 'b': 'int'}, 'db.main.t2': {'c': 'int', 'd': 'int'}}
    result = one('select a, b from t1 union all select c, d from t2 order by a + b limit 5', schema=schema)
    assert set(result.references) == {
        ('db.main.t1', 'a'),
        ('db.main.t1', 'b'),
        ('db.main.t2', 'c'),
        ('db.main.t2', 'd'),
    }
    assert (
        'ORDER BY: UNION(db.main.t1.a, db.main.t2.c) + UNION(db.main.t1.b, db.main.t2.d)' in result.conditions
    )


def test_a_join_is_labelled_with_its_kind_as_written() -> None:
    """The labels of the reviewing-changes guide: a join's kind is part of its condition."""
    semi = one('select o.id from db.main.orders o semi join db.main.payments p on p.order_id = o.id')
    assert semi.conditions == ['SEMI JOIN ON: db.main.payments.order_id = db.main.orders.id']
    inner = one('select o.id from db.main.orders o inner join db.main.payments p on p.order_id = o.id')
    assert inner.conditions == ['INNER JOIN ON: db.main.payments.order_id = db.main.orders.id']


def test_the_relations_a_star_it_could_not_expand_reads() -> None:
    """Every column of a relation with unknown columns reaches the output of a ``SELECT *`` over it,
    beside any other item and through a CTE, except those it leaves out."""
    assert one('select *, total * 2 as t2 from db.other.ltv').star_reads == {'db.other.ltv': []}
    assert one('select * exclude (region) from db.other.ltv').star_reads == {'db.other.ltv': ['region']}
    assert one('with x as (select * from db.other.ltv) select l.* from x l').star_reads == {
        'db.other.ltv': []
    }
    assert one('with x as (select * from db.other.ltv) select total from x').star_reads == {}


def test_rows_from_a_table_function_or_values_are_a_condition() -> None:
    """No relation describes them, so the SQL that makes them is what they are."""
    assert one('select o.id from db.main.orders o cross join range(2) t(d)').conditions == [
        'ROWS FROM: RANGE(0, 2)'
    ]
    made = one(
        "select o.id from db.main.orders o join (values ('paid', 1)) v(status, w) on v.status = o.status"
    )
    assert "ROWS FROM: VALUES ('paid', 1)" in made.conditions


def test_rows_from_unnest_and_lateral_are_a_condition() -> None:
    """An UNNEST or a LATERAL makes rows from each row of another source, so the rows a query keeps
    depend on it as on a table function."""
    assert one('select o.id, t.x from db.main.orders o cross join unnest([1, 2]) as t(x)').conditions == [
        'ROWS FROM: UNNEST([1, 2])'
    ]
    lateral = one('select o.id, l.y from db.main.orders o, lateral (select o.id + 1 as y) as l')
    assert [c for c in lateral.conditions if c.startswith('ROWS FROM: LATERAL')]
    exploded = one(
        'select o.id, e.x from db.main.orders o lateral view explode(array(o.id)) e as x', dialect='spark'
    )
    assert [c for c in exploded.conditions if c.startswith('ROWS FROM: LATERAL VIEW')]


def test_conditions_do_not_depend_on_alias_names() -> None:
    """A Snowflake TABLE(...) source, a table function in FROM and a subquery in a filter name
    their columns by relation and drop their aliases, so renaming an alias changes no condition."""
    spellings = [
        (
            'select o.amount as v from db.main.orders o, table(flatten(input => o.status)) f',
            'select q.amount as v from db.main.orders q, table(flatten(input => q.status)) g',
            'snowflake',
        ),
        (
            'select o.amount from db.main.orders o, table(generator(rowcount => 3)) g',
            'select q.amount from db.main.orders q, table(generator(rowcount => 3)) h',
            'snowflake',
        ),
        (
            'select o.id from db.main.orders o, jsonb_array_elements(o.status) e',
            'select q.id from db.main.orders q, jsonb_array_elements(q.status) x',
            'postgres',
        ),
        (
            'select o.id from db.main.orders o where exists '
            '(select 1 from db.main.payments p where p.order_id = o.id)',
            'select q.id from db.main.orders q where exists '
            '(select 1 from db.main.payments r where r.order_id = q.id)',
            'duckdb',
        ),
        (
            'select o.id from db.main.orders o where o.id in (select p.order_id from db.main.payments p)',
            'select q.id from db.main.orders q where q.id in (select r.order_id from db.main.payments r)',
            'duckdb',
        ),
    ]
    for first, second, dialect in spellings:
        conditions = one(first, dialect=dialect).conditions
        assert conditions and conditions == one(second, dialect=dialect).conditions, (first, conditions)
    assert one(spellings[0][0], dialect='snowflake').conditions == [
        'ROWS FROM: TABLE(FLATTEN(input => db.main.orders.status))'
    ]
    assert one(spellings[3][0]).conditions == [
        'WHERE: EXISTS(SELECT 1 AS "1" FROM db.main.payments WHERE db.main.payments.order_id = db.main.orders.id)',
        'WHERE: db.main.payments.order_id = db.main.orders.id',
    ]


def test_rows_from_unnest_and_lateral_do_not_depend_on_alias_names() -> None:
    """Renaming an alias changes no condition, so it makes no lockfile pin or receipt stale."""
    spellings = [
        (
            'select o.id, t.n from db.main.orders o cross join unnest([o.amount, o.customer_id]) as t(n)',
            'select ord.id, x.n from db.main.orders ord cross join unnest([ord.amount, ord.customer_id]) as x(n)',
        ),
        (
            'select o.id, l.m from db.main.orders o, lateral (select p.method as m from db.main.payments p '
            'where p.order_id = o.id) as l',
            'select ord.id, z.m from db.main.orders ord, lateral (select pay.method as m from db.main.payments pay '
            'where pay.order_id = ord.id) as z',
        ),
    ]
    for first, second in spellings:
        made = [c for c in one(first).conditions if c.startswith('ROWS FROM')]
        assert made and made == [c for c in one(second).conditions if c.startswith('ROWS FROM')]
    exploded = [
        one(
            f'select {a}.id, {b}.x from db.main.orders {a} lateral view explode(array({a}.amount)) {b} as x',
            dialect='spark',
        ).conditions
        for a, b in (('o', 'e'), ('ord', 'items'))
    ]
    assert exploded[0] == exploded[1] == ['ROWS FROM: LATERAL VIEW EXPLODE(ARRAY(db.main.orders.amount))']
    assert one(spellings[0][0]).conditions == [
        'ROWS FROM: UNNEST([db.main.orders.amount, db.main.orders.customer_id])'
    ]


@pytest.mark.parametrize(
    ('a', 'b'),
    [
        (
            "WHERE: o.status <> 'x' AND o.d >= DATE '2020-01-01'",
            "WHERE: o.d >= '2020-01-01' AND 'x' != o.status",
        ),
        ("WHERE: o.status IN ('x')", "WHERE: NOT o.status <> 'x'"),
        (
            "WHERE: o.d BETWEEN '2020-01-01' AND '2020-12-31'",
            "WHERE: o.d <= '2020-12-31' AND '2020-01-01' <= o.d",
        ),
        ("WHERE: NOT o.status = ANY(['x'])", "WHERE: o.status <> 'x'"),
        ("WHERE: o.status NOT IN (VALUES ('x'), ('y'))", "WHERE: NOT o.status IN ('x', 'y')"),
        ("WHERE: o.status NOT LIKE 'x'", "WHERE: o.status <> 'x'"),
        ("WHERE: o.d >= TIMESTAMP '2020-01-01 00:00:00'", "WHERE: o.d >= DATE '2020-01-01'"),
        ("WHERE: o.status IN ('y', 'x', 'x')", "WHERE: o.status IN ('x', 'y')"),
        ("WHERE: o.status NOT IN ('x', 'x')", "WHERE: o.status <> 'x'"),
    ],
)
def test_conditions_that_filter_alike_have_one_key(a: str, b: str) -> None:
    from konstruct.core.sql import condition_key

    assert condition_key(a, 'duckdb') == condition_key(b, 'duckdb')
    assert condition_key("WHERE: o.status = 'x'", 'duckdb') != condition_key(
        "WHERE: o.status <> 'x'", 'duckdb'
    )


@pytest.mark.parametrize(
    ('a', 'b', 'dialect'),
    [
        ("WHERE: o.status NOT IN UNNEST(['x'])", "WHERE: o.status <> 'x'", 'bigquery'),
        ("WHERE: o.status IN UNNEST(['x', 'y'])", "WHERE: o.status IN ('x', 'y')", 'bigquery'),
    ],
)
def test_a_list_written_as_an_array_is_a_list(a: str, b: str, dialect: str) -> None:
    from konstruct.core.sql import condition_key

    assert condition_key(a, dialect) == condition_key(b, dialect)


def test_a_typed_literal_on_a_column_of_another_type_keeps_its_type() -> None:
    import sqlglot

    from konstruct.core.sql import normal_condition

    def normal(text: str, untimed: tuple[str, ...] = ()) -> str:
        return normal_condition(sqlglot.parse_one(text, read='duckdb'), 'duckdb', untimed=untimed).sql()

    assert normal("note = DATE '2020-01-01'", ('note',)) != normal("note = '2020-01-01'", ('note',))
    assert normal("d = DATE '2020-01-01'") == normal("d = '2020-01-01'")
    assert normal('d >= MAKE_DATE(2020, 1, 1)') == normal("d >= '2020-01-01'")


def test_patterns_and_untyped_times_keep_their_meaning() -> None:
    from konstruct.core.sql import condition_key

    # A wildcard is a pattern, not a value.
    assert condition_key("WHERE: o.status NOT LIKE 'x%'", 'duckdb') != condition_key(
        "WHERE: o.status <> 'x%'", 'duckdb'
    )
    # A string that looks like a midnight timestamp is only a day where the column is a date or time.
    assert condition_key("WHERE: o.note = '2020-01-01 00:00:00'", 'duckdb') != condition_key(
        "WHERE: o.note = '2020-01-01'", 'duckdb'
    )


# check-sql: one small case for each finding, against the shop of tests/test_checksql.py (or, for
# a rule about ontology packs, its Stripe warehouse)

CHECK_SQL_CASES = [
    (
        'missing-filter',
        'select c.region, sum(o.amount) as net_revenue from raw.orders o '
        'join raw.customers c on c.customer_id = o.customer_id group by 1',
    ),
    ('formula-differs', "select sum(amount * 2) as net_revenue from raw.orders where status <> 'returned'"),
    ('recomputes-measure', "select sum(amount) as net_revenue from raw.orders where status <> 'returned'"),
    ('retired-column', 'select sum(gross_revenue) as total from mart.customer_revenue'),
    (
        'fan-out',
        'select sum(o.amount) as total from raw.orders o join raw.payments p on p.order_id = o.order_id',
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
    ('unknown-join', 'select o.order_id from raw.orders o join raw.payments p on o.status = p.method'),
    # Against Stripe's tables, which an ontology pack binds: its ids are presumed unique.
    (
        'presumed-key',
        'select sum(c.amount) from stripe.charge c join stripe.customer u on u.id = c.customer_id',
    ),
    ('unknown-relation', 'select * from raw.orderz'),
    ('unknown-column', 'select amount_total from raw.orders'),
    ('unparsed', 'select sum(amount from raw.orders'),
    (
        'unchecked',
        'select p.customer_id, p.returned from (pivot raw.orders on status using sum(amount) group by customer_id) p',
    ),
]


@pytest.fixture(scope='module')
def check_sql_shop(tmp_path_factory: pytest.TempPathFactory) -> Any:
    from konstruct.analysis.checksql import Checker
    from tests.test_checksql import make_shop

    return Checker(make_shop(tmp_path_factory.mktemp('check_sql_shop')))


def test_every_check_sql_rule_has_a_case() -> None:
    from konstruct.analysis.checksql import RULES

    assert sorted({rule for rule, _ in CHECK_SQL_CASES}) == sorted(RULES)


PACK_RULES = frozenset({'presumed-key'})
"""The rules whose case runs against tables an ontology pack binds."""


@pytest.fixture(scope='module')
def check_sql_stripe(tmp_path_factory: pytest.TempPathFactory) -> Any:
    import duckdb

    from konstruct import Project
    from konstruct.analysis.checksql import Checker
    from tests.test_checksql import STRIPE

    root = tmp_path_factory.mktemp('check_sql_stripe')
    con = duckdb.connect(str(root / 'warehouse.duckdb'))
    con.execute(STRIPE)
    con.close()
    return Checker(Project(root).build(run_dbt=False).graph)


@pytest.mark.parametrize(('rule', 'sql'), CHECK_SQL_CASES)
def test_check_sql_finds_each_rule(check_sql_shop: Any, check_sql_stripe: Any, rule: str, sql: str) -> None:
    result = (check_sql_stripe if rule in PACK_RULES else check_sql_shop).check(sql)
    assert [f.rule for f in result.findings] == [rule], str(result)


# Self-joins of two small tables, two or three readings of them, to check the readings' names
# against what DuckDB returns.
PAIRS_TABLE = {
    'db.main.t': {'id': 'int', 'parent_id': 'int', 'amount': 'int', 'kind': 'text'},
    'db.main.u': {'id': 'int', 'region': 'text', 'score': 'int'},
}
PAIRS_ROWS = (
    "create table t as select * from (values (1, null, 10, 'x'), (2, 1, 20, 'y'), (3, 1, 30, 'x'), "
    "(4, 2, 15, 'y'), (5, 3, 30, 'x'), (6, 2, 5, 'y')) v(id, parent_id, amount, kind); "
    "create table u as select * from (values (1, 'n', 10), (2, 's', 25), (3, 'n', 30)) v(id, region, score)"
)
# The conditions joining reading Y to an earlier reading X, by their relations: X and Y the same
# relation or not, tautologies among them, and ONs that do not name the reading they join.
JOINS = {
    ('t', 't'): [
        'TRUE',
        "X.kind = 'x'",
        'Y.parent_id = X.id',
        'X.parent_id = Y.id',
        'X.kind = Y.kind',
        'Y.amount > X.amount',
        'Y.parent_id = X.parent_id',
        'X.kind = X.kind',
        'Y.kind = Y.kind',
    ],
    ('t', 'u'): ['TRUE', 'X.amount > 10', 'Y.id = X.parent_id', 'Y.id = X.id', 'Y.score > X.amount'],
    ('u', 't'): ['Y.parent_id = X.id', 'Y.amount < X.score'],
    ('u', 'u'): ['TRUE', "X.region = 'n'", 'X.region = Y.region', 'Y.id = X.id', 'X.region = X.region'],
}
FILTERS = {'t': ["R.kind = 'x'", 'R.amount > 10'], 'u': ["R.region = 'n'"]}
COLUMNS = {'t': ['id', 'amount', 'parent_id'], 'u': ['id', 'score']}
ALIASES = 'abc'
_ALIAS = re.compile(r'\b([abc])\.')


@st.composite
def self_joins(draw: st.DrawFn) -> dict[str, Any]:
    """Two or three readings of t and u, the first of t, each joined to an earlier one, with a
    filter, and as output two of their columns or a difference, COUNT(*) alone, or a star."""
    relations = ['t', *draw(st.lists(st.sampled_from('tu'), min_size=1, max_size=2))]
    readings = list(zip(ALIASES, relations, strict=False))
    joins = []
    for index, (alias, relation) in enumerate(readings[1:], 1):
        earlier, kind = draw(st.sampled_from(readings[:index]))
        condition = draw(st.sampled_from(JOINS[kind, relation]))
        joins.append(
            (
                draw(st.sampled_from(['', 'left '])),
                condition.replace('X.', f'{earlier}.').replace('Y.', f'{alias}.'),
            )
        )
    filtered, kind = draw(st.sampled_from(readings))
    filter_ = draw(st.sampled_from(['', *FILTERS[kind]])).replace('R.', f'{filtered}.')
    pool = [f'{alias}.{c}' for alias, relation in readings for c in COLUMNS[relation]]
    pool += [
        f'{x}.amount - {y}.amount' for x, rx in readings for y, ry in readings if x != y and rx == ry == 't'
    ]
    output = draw(st.sampled_from(['columns', 'count', 'star']))
    if output == 'count':
        items = ['count(*)']
    elif output == 'star':
        items = [f'{draw(st.sampled_from(readings))[0]}.*']
    else:
        items = draw(st.lists(st.sampled_from(pool), min_size=1, max_size=2, unique=True))
    return {
        'relations': relations,
        'joins': joins,
        'filter': filter_,
        'items': items,
        'form': draw(st.sampled_from(['plain', 'one cte', 'two ctes'])),
        'unknown': draw(st.booleans()),
    }


@st.composite
def changed(draw: st.DrawFn, spec: dict[str, Any]) -> dict[str, Any]:
    """``spec`` with one of its parts reading another reading of the same relation."""
    readings = list(zip(ALIASES, spec['relations'], strict=False))
    pairs = [(x, y) for x, rx in readings for y, ry in readings if x != y and rx == ry]
    if not pairs:
        return spec
    x, y = draw(st.sampled_from(pairs))
    swap = draw(st.booleans())

    def moved(text: str) -> str:
        return _ALIAS.sub(lambda m: (y if m[1] == x else x if swap and m[1] == y else m[1]) + '.', text)

    parts = [('join', i) for i in range(len(spec['joins']))] + [('filter', 0), ('items', 0)]
    part, index = draw(st.sampled_from(parts))
    other = {**spec, 'joins': list(spec['joins']), 'items': list(spec['items'])}
    if part == 'join':
        side, condition = other['joins'][index]
        condition = moved(condition)
        if any(ALIASES.index(m[1]) > index + 1 for m in _ALIAS.finditer(condition)):
            return spec  # a reading joined later is not there yet
        other['joins'][index] = (draw(st.sampled_from([side, 'left ' if not side else ''])), condition)
    elif part == 'filter':
        other['filter'] = moved(spec['filter'])
    else:
        other['items'] = [moved(item) for item in spec['items']]
    return other


def _self_join(spec: dict[str, Any], *, reorder: bool = False) -> str:
    """The SQL of ``spec``; with ``reorder``, with other aliases and its select items and CTEs the
    other way round."""
    names = dict(zip(ALIASES, 'zyx' if reorder else ALIASES, strict=True))

    def aliased(text: str) -> str:
        return _ALIAS.sub(lambda m: names[m[1]] + '.', text)

    items = [
        f'{aliased(item)} as c{i}' if '*' not in item else aliased(item)
        for i, item in enumerate(spec['items'])
    ]
    if reorder:
        items.reverse()
    ctes = {'plain': [], 'one cte': ['r'], 'two ctes': ['ra', 'rb']}[spec['form']]
    sources = []
    t_readings = 0
    for relation in spec['relations']:
        if relation == 't' and ctes:
            sources.append(ctes[t_readings % len(ctes)])
            t_readings += 1
        else:
            sources.append(relation)
    first = f'{sources[0]} {names["a"]}'
    joined = ''.join(
        f' {side}join {source} {names[alias]} on {aliased(condition)}'
        for (side, condition), source, alias in zip(spec['joins'], sources[1:], ALIASES[1:], strict=False)
    )
    where = f' where {aliased(spec["filter"])}' if spec['filter'] else ''
    body = f'select {", ".join(items)} from {first}{joined}{where}'
    defined = [f'{name} as (select * from t)' for name in ctes]
    if reorder:
        defined.reverse()
    return f'with {", ".join(defined)} {body}' if defined else body


def _printed(sql: str, unknown: bool = False) -> tuple[Any, ...]:
    """What the build records of a statement's rows and columns, its conditions in the form diff
    and the lockfile compare them in."""
    result = one(sql, schema={} if unknown else PAIRS_TABLE)
    conditions = sorted(canonical_condition(c, 'duckdb') for c in result.conditions)
    return conditions, result.expressions, result.nullable, result.star_reads


def _rows(sql: str) -> list[tuple[Any, ...]]:
    connection = duckdb.connect()
    connection.execute(PAIRS_ROWS)
    cursor = connection.execute(sql)
    names = [d[0] for d in cursor.description]
    rows = [tuple(sorted(zip(names, row, strict=True), key=lambda p: p[0])) for row in cursor.fetchall()]
    return sorted(rows, key=repr)


@settings(max_examples=200, deadline=None)
@given(self_joins())
def test_a_self_join_prints_the_same_whatever_its_aliases_and_the_order_of_its_items_and_ctes(
    spec: dict[str, Any],
) -> None:
    assert _printed(_self_join(spec), spec['unknown']) == _printed(
        _self_join(spec, reorder=True), spec['unknown']
    )


@settings(max_examples=150, deadline=None)
@given(self_joins(), st.sampled_from(['a', 'b']))
def test_the_branches_of_a_union_print_the_same_either_way_round(spec: dict[str, Any], kept: str) -> None:
    spec = {**spec, 'items': ['a.id', 'a.amount']}
    other = f"select {kept}.id as c0, {kept}.amount as c1 from t {kept} where {kept}.kind = 'y'"
    first = _self_join(spec)
    assert _printed(f'{first} union all {other}') == _printed(f'{other} union all {first}')


@settings(max_examples=400, deadline=None)
@given(st.data())
def test_self_joins_that_return_other_rows_print_differently(data: st.DataObject) -> None:
    """A change to which reading a self-join's parts read, or another self-join of the same
    readings, that changes its rows is a change to what it prints."""
    spec = data.draw(self_joins())
    other = data.draw(st.one_of(changed(spec), self_joins().map(lambda o: {**o, 'form': spec['form']})))
    other = {**other, 'unknown': spec['unknown']}
    first, second = _self_join(spec), _self_join(other)
    if _rows(first) != _rows(second):
        assert _printed(first, spec['unknown']) != _printed(second, spec['unknown']), (first, second)


def test_a_symmetric_self_join_is_not_a_tautology() -> None:
    """``on a.k = b.k`` joins each order to every order of its kind, ``on a.k = a.k`` to every
    order: 19 rows against 56 of PAIRS_ROWS's 6 orders, which the readings' names tell apart. The
    symmetric join prints the same whichever way round it is written."""
    sql = 'select count(*) from t a join t b on {on}'
    symmetric, tautology = (_printed(sql.format(on=on)) for on in ('a.kind = b.kind', 'a.kind = a.kind'))
    assert _rows(sql.format(on='a.kind = b.kind')) != _rows(sql.format(on='a.kind = a.kind'))
    assert symmetric != tautology
    assert symmetric == _printed(sql.format(on='b.kind = a.kind'))


def test_readings_said_the_same_of_are_still_numbered_apart() -> None:
    """Joined to the same order, b and c are said the same of; joined to each other, they are not:
    each reading has a number of its own either way."""
    first = 'select count(*) from t a join t b on b.parent_id = a.id join t c on c.parent_id = a.id'
    second = first.replace('on c.parent_id = a.id', 'on b.parent_id = c.id')
    assert _rows(first) != _rows(second)
    assert _printed(first) != _printed(second)
    assert len(set(re.findall(r't#\d', ' '.join(_printed(first)[0])))) == 3


@pytest.mark.parametrize(
    ('before', 'after'),
    [
        ("o.region = 'n'", "d.region = 'n'"),
        ('o.score', 'd.score'),
    ],
    ids=['a filter', 'a column'],
)
def test_a_relation_read_twice_through_another_has_two_readings(before: str, after: str) -> None:
    """A flight's origin and destination are two readings of the airports, joined through the
    flights: moving a filter, or a column, from one to the other changes the rows."""
    sql = 'select f.id, {x} as x from t f join u o on o.id = f.parent_id join u d on d.id = f.id where f.amount > 0'
    first, second = sql.format(x=before), sql.format(x=after)
    if 'region' in before:
        first = sql.format(x='f.amount') + f' and {before}'
        second = sql.format(x='f.amount') + f' and {after}'
    assert _rows(first) != _rows(second)
    assert _printed(first) != _printed(second)


@pytest.mark.parametrize('cte', [False, True], ids=['plain', 'through a cte'])
def test_which_reading_a_star_passes_on_is_printed(cte: bool) -> None:
    sql = 'select {s}.* from t a join t b on b.parent_id = a.id'
    if cte:
        sql = 'with r as (select * from t) ' + sql.replace(' t ', ' r ')
    one_way, other_way = (_printed(sql.format(s=s), unknown=True) for s in 'ab')
    assert _rows(sql.format(s='a')) != _rows(sql.format(s='b'))
    assert one_way != other_way
    assert any(c.startswith('SELECT: ') for c in one_way[0])


def test_a_distinct_star_through_a_cte_names_the_reference_it_passes_on() -> None:
    sql = 'with r as (select * from t) select distinct {s}.* from r x join r y on y.parent_id = x.id'
    assert _rows(sql.format(s='x')) != _rows(sql.format(s='y'))
    assert _printed(sql.format(s='x'), unknown=True) != _printed(sql.format(s='y'), unknown=True)


def test_without_readings_touches_only_relation_names_and_keeps_their_escapes() -> None:
    from konstruct.core.sql import without_readings

    assert (
        without_readings('db.main."orders#2".id = db.main.t."acct#2"')
        == 'db.main.orders.id = db.main.t."acct#2"'
    )
    assert without_readings('db.main."events##2#1".id') == 'db.main."events##2".id'
    assert (
        without_readings('EXISTS(SELECT 1 FROM db.main."orders#1/2")')
        == 'EXISTS(SELECT 1 FROM db.main.orders)'
    )


@pytest.mark.parametrize(
    ('first', 'second'),
    [
        (
            'select amount from t except select score as amount from u',
            'select score as amount from u except select amount from t',
        ),
        (
            "select amount from t where kind = 'x' except all select amount from t where kind = 'y'",
            "select amount from t where kind = 'y' except all select amount from t where kind = 'x'",
        ),
        (
            'with r as (select a.amount from t a join t b on b.parent_id = a.id) '
            'select amount from r except select amount from t where amount > 10',
            'with r as (select a.amount from t a join t b on b.parent_id = a.id) '
            'select amount from t where amount > 10 except select amount from r',
        ),
    ],
    ids=['two relations', 'except all over one relation', 'a self-join in a cte'],
)
def test_the_branches_of_an_except_are_in_their_order(first: str, second: str) -> None:
    """An EXCEPT takes the rows of its second branch from those of its first: the other way
    round, it returns other rows."""
    assert _rows(first) != _rows(second)
    assert _printed(first) != _printed(second)


@pytest.mark.parametrize('on', ['TRUE', "a.kind = 'x'"])
def test_a_join_whose_on_does_not_name_its_reading_names_it_in_its_label(on: str) -> None:
    """``b`` is joined on an ON that does not name it and ``c`` on one that names both the same way:
    which of them a column is from is which rows it has, as a ``b`` with no parent has no ``c``."""
    sql = f'select a.id, {{x}}.amount as amount from t a left join t b on {on} left join t c on c.parent_id = b.parent_id'
    assert _rows(sql.format(x='b')) != _rows(sql.format(x='c'))
    assert _printed(sql.format(x='b')) != _printed(sql.format(x='c'))


def test_a_comparison_between_readings_written_the_other_way_round_is_the_same() -> None:
    sql = 'select a.id, b.id as other from t a join t b on {on} where a.kind = {k}'
    for k in ("'x'", 'b.kind'):
        one_way, other_way = (
            _printed(sql.format(on=on, k=k)) for on in ('a.amount < b.amount', 'b.amount > a.amount')
        )
        assert one_way == other_way


@settings(max_examples=150, deadline=None)
@given(
    st.sampled_from(['TRUE', "a.kind = 'x'", 'a.amount > 10', 'a.parent_id IS NULL']),
    st.sampled_from([*JOINS['t', 't'], "b.kind = 'y'"]),
    st.sampled_from(['left ', '', 'full ']),
    st.sampled_from(['amount', 'id', 'parent_id']),
    st.booleans(),
)
def test_outer_joins_whose_on_does_not_name_the_reading_they_join_print_their_rows(
    on: str, then: str, side: str, column: str, also_a: bool
) -> None:
    """``b`` is joined on an ON that names only ``a``, ``c`` on one that names ``b``: which of
    the readings a column is from changes the rows, and so what is printed."""
    then = then.replace('X.', 'b.').replace('Y.', 'c.')
    outputs = {x: f'{"a.id, " if also_a else ""}{x}.{column} as v' for x in 'bc'}
    sql = 'select {o} from t a left join t b on ' + on + f' {side}join t c on {then}'
    first, second = (sql.format(o=outputs[x]) for x in 'bc')
    if _rows(first) != _rows(second):
        assert _printed(first) != _printed(second), (first, second)


@pytest.mark.parametrize('operator', ['IS DISTINCT FROM', 'IS NOT DISTINCT FROM'])
def test_a_null_safe_comparison_between_readings_written_the_other_way_round_is_the_same(
    operator: str,
) -> None:
    sql = 'select a.id, b.id as other from t a join t b on {on} and a.amount < b.amount'
    one_way, other_way = (
        _printed(sql.format(on=on))
        for on in (f'a.parent_id {operator} b.parent_id', f'b.parent_id {operator} a.parent_id')
    )
    assert one_way == other_way
