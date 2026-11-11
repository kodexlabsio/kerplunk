"""Data laws: proposed only where the SQL keeps a total, confirmed per day, broken where data or a
change breaks them, with the model to blame."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import duckdb
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from konstruct import Project
from konstruct.analysis.laws import (
    FORMAT,
    Law,
    Laws,
    _Analyser,
    _Flow,
    _Path,
    _Tests,
    confirm,
    load,
    propose,
    recorded,
    violations,
)
from konstruct.analysis.query import QueryError
from konstruct.analysis.verify import Candidate, DbApiSource, SampledEngine
from konstruct.core.formats import FormatError
from konstruct.core.graph import Graph, Node, column_id
from konstruct.interfaces.cli import main

# A shop with a dbt manifest written by hand: raw tables as sources, models as views, so new raw
# data reaches every model at once.

RAW = {
    'payments': (
        "select i as id, 1 + (i * 7) % 20 as order_id, timestamp '2026-03-01' + to_hours(i * 7) as paid_at, "
        "['card', 'bank', 'voucher'][1 + i % 3] as method, (i * 37) % 500 + 100 as amount from range(1, 81) t(i)"
    ),
    'orders': "select i as id, 1 + i % 6 as customer_id, ['placed', 'shipped'][1 + i % 2] as status from range(1, 21) t(i)",
    'items': "select o.i as order_id, s.sku from range(1, 21) o(i), (values ('a'), ('b')) s(sku)",
    'ledger': (
        "select i as id, 1 + i % 4 as account_id, date '2026-03-01' + (i % 15)::int as booked_on, "
        '(i * 13) % 90 - 30 as amount from range(1, 121) t(i)'
    ),
}


def rel(name: str) -> str:
    return f'"shop"."main"."{name}"'


def raw(name: str) -> str:
    return f'"shop"."raw"."{name}"'


MODELS = {
    'stg_payments': (
        f'select id as payment_id, order_id, paid_at, method, amount / 100.0 as amount from {raw("payments")}'
    ),
    'stg_orders': f'select id as order_id, customer_id, status from {raw("orders")}',
    'payments_enriched': (
        f'with p as (select * from {rel("stg_payments")}), o as (select * from {rel("stg_orders")}) '
        'select p.payment_id, p.paid_at, p.method, p.amount, o.customer_id, o.status '
        'from p left join o on o.order_id = p.order_id'
    ),
    'daily_revenue': (
        'select cast(paid_at as date) as day, sum(amount) as revenue, count(*) as payments, '
        "sum(case when method = 'card' then amount else 0 end) as card_revenue "
        f'from {rel("payments_enriched")} group by 1'
    ),
}
TESTS: list[tuple[str, ...]] = [
    ('unique', 'stg_orders', 'order_id'),
    ('unique', 'stg_payments', 'payment_id'),
]


def write_shop(
    root: Path, models: dict[str, str], tests: list[tuple[str, ...]], raw_sql: dict[str, str] | None = None
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / 'dbt_project.yml').write_text('name: shop\nprofile: shop\n')
    con = duckdb.connect(str(root / 'shop.duckdb'))
    try:
        con.execute('create schema if not exists raw')
        for name, sql in (raw_sql or RAW).items():
            con.execute(f'create or replace table raw.{name} as {sql}')
        for name, sql in models.items():
            con.execute(f'create or replace view main.{name} as {sql}')
    finally:
        con.close()
    nodes: dict[str, Any] = {}
    declared: dict[str, set[str]] = {}
    for kind, model, column, *target in tests:
        declared.setdefault(model, set()).add(column)
        if kind == 'relationships':
            declared.setdefault(target[0], set()).add(target[1])
    for name, sql in models.items():
        depends = [f'source.shop.raw.{t}' for t in RAW if raw(t) in sql]
        depends += [f'model.shop.{m}' for m in models if rel(m) in sql]
        nodes[f'model.shop.{name}'] = {
            'unique_id': f'model.shop.{name}',
            'resource_type': 'model',
            'name': name,
            'alias': name,
            'package_name': 'shop',
            'database': 'shop',
            'schema': 'main',
            'relation_name': rel(name),
            'original_file_path': f'models/{name}.sql',
            'compiled_code': sql,
            'raw_code': sql,
            'language': 'sql',
            'depends_on': {'nodes': depends, 'macros': []},
            'config': {'materialized': 'view'},
            'columns': {c: {'name': c} for c in sorted(declared.get(name, ()))},
            'checksum': {'checksum': str(hash(sql))},
        }
    for kind, model, column, *target in tests:
        attached = f'source.shop.raw.{model}' if model in RAW else f'model.shop.{model}'
        kwargs: dict[str, Any] = {'column_name': column}
        depends = [attached]
        if kind == 'relationships':
            kwargs.update({'to': f"ref('{target[0]}')", 'field': target[1]})
            depends = [f'model.shop.{target[0]}', attached]
        nodes[f'test.shop.{kind}_{model}_{column}'] = {
            'unique_id': f'test.shop.{kind}_{model}_{column}',
            'resource_type': 'test',
            'name': f'{kind}_{model}_{column}',
            'package_name': 'shop',
            'original_file_path': 'models/schema.yml',
            'test_metadata': {'name': kind, 'kwargs': kwargs},
            'column_name': column,
            'attached_node': attached,
            'depends_on': {'nodes': depends, 'macros': []},
            'config': {'severity': 'ERROR'},
        }
    sources = {
        f'source.shop.raw.{t}': {
            'unique_id': f'source.shop.raw.{t}',
            'resource_type': 'source',
            'name': t,
            'identifier': t,
            'source_name': 'raw',
            'package_name': 'shop',
            'database': 'shop',
            'schema': 'raw',
            'relation_name': raw(t),
            'original_file_path': 'models/sources.yml',
        }
        for t in RAW
    }
    (root / 'target').mkdir(exist_ok=True)
    (root / 'target' / 'manifest.json').write_text(
        json.dumps(
            {'metadata': {'adapter_type': 'duckdb'}, 'nodes': nodes, 'sources': sources, 'exposures': {}}
        )
    )


def shop(
    root: Path,
    models: dict[str, str] = MODELS,
    tests: list[tuple[str, ...]] = TESTS,
    configs: dict[str, dict[str, Any]] | None = None,
) -> Project:
    """The shop built from its manifest, with ``configs`` merged into the config of each node named."""
    write_shop(root, models, tests)
    if configs:
        path = root / 'target' / 'manifest.json'
        manifest = json.loads(path.read_text())
        for node_id, config in configs.items():
            manifest['nodes'][node_id]['config'].update(config)
        path.write_text(json.dumps(manifest))
    project = Project(root)
    project.build(run_dbt=False)
    return project


def check(project: Project, laws: Laws | None = None) -> Laws:
    return confirm(project.graph, project.root, project.root / 'shop.duckdb', laws)


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return shop(tmp_path / 'shop')


def by_id(laws: Laws) -> dict[str, Law]:
    return {law.id: law for law in laws.laws}


# Proposing, on a whole project


def test_laws_follow_the_lineage_through_every_model_that_keeps_the_total(project: Project) -> None:
    laws = by_id(propose(project.graph, project.root))
    revenue = laws['conservation:model.shop.daily_revenue#revenue']
    [term] = revenue.terms
    assert term.source == 'source.shop.raw.payments' and term.source_column == 'amount'
    assert term.factor == pytest.approx(0.01)
    assert (revenue.partition, term.partition) == ('day', 'CAST(paid_at AS DATE)')
    assert [s.model for s in revenue.steps] == [
        'model.shop.daily_revenue',
        'model.shop.payments_enriched',
        'model.shop.stg_payments',
    ]
    assert revenue.relies_on == ['test.shop.unique_stg_orders_order_id']
    assert revenue.statement == (
        'SUM(daily_revenue.revenue) = SUM(raw.payments.amount) / 100, per day of daily_revenue.day'
    )
    card = laws['conservation:model.shop.daily_revenue#card_revenue']
    assert card.terms[0].filter == "method = 'card'" and card.terms[0].source == 'source.shop.raw.payments'
    # A COUNT(*) column adds up to the rows it counts, back to the source.
    payments = laws['count:model.shop.daily_revenue#payments']
    assert payments.terms[0].source == 'source.shop.raw.payments' and payments.terms[0].source_column is None
    assert payments.statement.startswith('SUM(daily_revenue.payments) = COUNT(*) of raw.payments')
    # A model that keeps one row per input row has as many rows as its source.
    rows = laws['count:model.shop.payments_enriched']
    assert rows.terms[0].source == 'source.shop.raw.payments' and rows.column is None
    assert 'count:model.shop.daily_revenue' not in laws  # it groups
    # Keys are not measures.
    assert not any(law.column in ('payment_id', 'order_id', 'customer_id') for law in laws.values())
    assert laws['additivity:model.shop.daily_revenue#revenue'].expected == 'additive'


def test_every_law_holds_on_data_that_keeps_the_tests(project: Project) -> None:
    laws = check(project)
    assert laws.warehouse and laws.totals()['broken'] == 0
    found = by_id(laws)
    revenue = found['conservation:model.shop.daily_revenue#revenue']
    assert revenue.status == 'holds' and revenue.held == revenue.checked > 1
    assert found['count:model.shop.daily_revenue#payments'].always_held
    assert found['conservation:model.shop.daily_revenue#card_revenue'].always_held
    assert found['additivity:model.shop.daily_revenue#revenue'].classification == 'additive'
    assert 'held on every partition' in laws.summary()
    assert 'holds' in str(laws) and 'days)' in str(laws)


def test_a_planted_fan_out_join_breaks_the_laws_and_names_the_model(project: Project) -> None:
    # stg_orders.order_id is tested unique, but the data has order 7 twice: every payment of
    # order 7 is joined twice in payments_enriched.
    con = duckdb.connect(str(project.root / 'shop.duckdb'))
    con.execute("insert into raw.orders values (7, 2, 'placed')")
    con.close()
    laws = by_id(check(project))
    for law_id in (
        'count:model.shop.payments_enriched',
        'conservation:model.shop.payments_enriched#amount',
        'conservation:model.shop.daily_revenue#revenue',
        'count:model.shop.daily_revenue#payments',
    ):
        law = laws[law_id]
        assert law.status == 'broken', law_id
        assert law.broken_at == 'model.shop.payments_enriched', law_id
        assert 0 < law.held < law.checked
        assert law.failures and law.failures[0].actual > law.failures[0].expected
    # Upstream of the join the laws still hold.
    assert laws['conservation:model.shop.stg_payments#amount'].always_held
    assert laws['count:model.shop.stg_orders'].always_held  # one row per raw row, duplicates included
    text = str(Laws(laws=list(laws.values()), warehouse='w'))
    assert 'parts at payments_enriched' in text


def test_a_join_no_uniqueness_test_covers_proposes_no_law(project: Project) -> None:
    models = {
        **MODELS,
        'payments_enriched': MODELS['payments_enriched'].replace(
            'from p left join o on o.order_id = p.order_id',
            f'from p left join o on o.order_id = p.order_id left join {raw("items")} i on i.order_id = p.order_id',
        ),
    }
    changed = shop(project.root, models)
    laws = propose(changed.graph, changed.root)
    ids = {law.id for law in laws.laws}
    assert 'conservation:model.shop.payments_enriched#amount' not in ids
    assert 'conservation:model.shop.daily_revenue#revenue' in ids  # back to payments_enriched only
    [blocked] = [b for b in laws.blocked if (b.model, b.column) == ('model.shop.payments_enriched', 'amount')]
    assert 'items' in blocked.reason and 'uniqueness test' in blocked.reason


def test_the_gate_names_the_model_that_breaks_a_law_that_always_held(project: Project) -> None:
    base = check(project)
    assert violations(project.graph, base, propose(project.graph, project.root)) == []

    models = {
        **MODELS,
        'payments_enriched': MODELS['payments_enriched'].replace(
            'from p left join o on o.order_id = p.order_id',
            f'from p left join o on o.order_id = p.order_id left join {raw("items")} i on i.order_id = p.order_id',
        ),
    }
    changed = shop(project.root, models)
    head = propose(changed.graph, changed.root, models=base.models())
    found = violations(changed.graph, base, head)
    by_law = {v.law: v for v in found}
    revenue = by_law['conservation:model.shop.daily_revenue#revenue']
    assert revenue.model == 'model.shop.payments_enriched' and revenue.label == 'payments_enriched'
    assert revenue.path == 'models/payments_enriched.sql'
    assert 'items' in revenue.message and 'uniqueness test' in revenue.message
    assert 'count:model.shop.payments_enriched' in by_law
    assert 'conservation:model.shop.stg_payments#amount' not in by_law
    assert str(revenue).startswith('SUM(daily_revenue.revenue)')
    assert revenue.to_dict()['model'] == 'model.shop.payments_enriched'


def test_the_gate_reports_a_removed_test_a_new_factor_and_a_new_aggregate(project: Project) -> None:
    base = check(project)
    # Without the uniqueness test the join is no longer safe.
    untested = shop(project.root, MODELS, [('unique', 'stg_payments', 'payment_id')])
    found = {v.law: v for v in violations(untested.graph, base, propose(untested.graph, untested.root))}
    assert 'uniqueness test' in found['conservation:model.shop.daily_revenue#revenue'].message
    # A new unit conversion changes the factor.
    models = {**MODELS, 'stg_payments': MODELS['stg_payments'].replace('/ 100.0', '/ 1000.0')}
    scaled = shop(project.root, models)
    found = {v.law: v for v in violations(scaled.graph, base, propose(scaled.graph, scaled.root))}
    message = found['conservation:model.shop.stg_payments#amount'].message
    assert (
        message
        == 'stg_payments: amount now sums payments.amount / 1000 instead of sums payments.amount / 100'
    )
    # A SUM that becomes an AVG is no longer additive.
    models = {
        **MODELS,
        'daily_revenue': MODELS['daily_revenue'].replace('sum(amount) as revenue', 'avg(amount) as revenue'),
    }
    averaged = shop(project.root, models)
    found = {v.law: v for v in violations(averaged.graph, base, propose(averaged.graph, averaged.root))}
    assert (
        found['additivity:model.shop.daily_revenue#revenue'].message
        == 'daily_revenue: revenue now looks non-additive'
    )
    assert 'AVG' in found['conservation:model.shop.daily_revenue#revenue'].message
    # A set-returning function repeats each payment.
    models = {
        **MODELS,
        'stg_payments': MODELS['stg_payments'].replace(
            ', amount / 100.0', ", unnest(['a', 'b']) as tag, amount / 100.0"
        ),
    }
    exploded = shop(project.root, models)
    found = {v.law: v for v in violations(exploded.graph, base, propose(exploded.graph, exploded.root))}
    assert 'set-returning' in found['conservation:model.shop.stg_payments#amount'].message
    assert 'count:model.shop.stg_payments' in found
    # A removed model breaks every law through it.
    gone = {k: v for k, v in MODELS.items() if k != 'daily_revenue'}
    removed = shop(project.root, gone)
    found = {v.law: v for v in violations(removed.graph, base, propose(removed.graph, removed.root))}
    assert (
        found['conservation:model.shop.daily_revenue#revenue'].message
        == 'daily_revenue: the model is removed'
    )


def test_an_inner_join_keeps_rows_only_where_relationships_and_not_null_tests_say_so(tmp_path: Path) -> None:
    inner = {
        **MODELS,
        'payments_enriched': MODELS['payments_enriched'].replace('from p left join o', 'from p join o'),
    }
    project = shop(tmp_path / 'inner', inner)
    laws = propose(project.graph, project.root)
    assert 'count:model.shop.payments_enriched' not in {law.id for law in laws.laws}
    [blocked] = [b for b in laws.blocked if (b.model, b.column) == ('model.shop.payments_enriched', None)]
    assert 'drops rows of stg_payments' in blocked.reason and 'relationships' in blocked.reason
    tests = [
        *TESTS,
        ('relationships', 'stg_payments', 'order_id', 'stg_orders', 'order_id'),
        ('not_null', 'stg_payments', 'order_id'),
    ]
    project = shop(tmp_path / 'inner', inner, tests)
    laws = check(project)
    law = by_id(laws)['count:model.shop.payments_enriched']
    assert law.always_held
    assert law.relies_on == [
        'test.shop.not_null_stg_payments_order_id',
        'test.shop.relationships_stg_payments_order_id',
        'test.shop.unique_stg_orders_order_id',
    ]


def test_count_law_broken_by_lost_rows(tmp_path: Path) -> None:
    """A law confirmed per day fails on the days the data breaks a relationships test."""
    inner = {
        **MODELS,
        'payments_enriched': MODELS['payments_enriched'].replace('from p left join o', 'from p join o'),
    }
    tests = [
        *TESTS,
        ('relationships', 'stg_payments', 'order_id', 'stg_orders', 'order_id'),
        ('not_null', 'stg_payments', 'order_id'),
    ]
    project = shop(tmp_path / 'lost', inner, tests)
    con = duckdb.connect(str(project.root / 'shop.duckdb'))
    con.execute('delete from raw.orders where id = 3')
    con.close()
    law = by_id(check(project))['count:model.shop.payments_enriched']
    assert law.status == 'broken' and law.broken_at == 'model.shop.payments_enriched'
    assert all(f.actual < f.expected for f in law.failures)


def test_an_incremental_model_has_no_step(tmp_path: Path) -> None:
    project = shop(tmp_path / 'inc', configs={'model.shop.stg_payments': {'materialized': 'incremental'}})
    laws = propose(project.graph, project.root)
    ids = {law.id for law in laws.laws}
    assert 'conservation:model.shop.stg_payments#amount' not in ids
    assert 'count:model.shop.stg_payments' not in ids
    reasons = {(b.model, b.column): b.reason for b in laws.blocked}
    for column in ('amount', None):
        assert reasons[('model.shop.stg_payments', column)] == (
            'an incremental model keeps rows its input no longer has'
        )
    # Downstream laws stop at it.
    revenue = by_id(laws)['conservation:model.shop.daily_revenue#revenue']
    assert revenue.terms[0].source == 'model.shop.stg_payments'


def test_a_test_with_a_where_proves_nothing_about_the_whole_relation(tmp_path: Path) -> None:
    project = shop(
        tmp_path / 'where', configs={'test.shop.unique_stg_orders_order_id': {'where': "status = 'placed'"}}
    )
    assert project.graph.nodes['test.shop.unique_stg_orders_order_id'].attrs['where'] == "status = 'placed'"
    laws = propose(project.graph, project.root)
    assert 'count:model.shop.payments_enriched' not in {law.id for law in laws.laws}
    [blocked] = [b for b in laws.blocked if (b.model, b.column) == ('model.shop.payments_enriched', None)]
    assert 'stg_orders' in blocked.reason and 'only uniqueness test has a where' in blocked.reason


def test_a_unique_combination_of_columns_is_a_composite_key(tmp_path: Path) -> None:
    root = tmp_path / 'combination'
    write_shop(root, MODELS, [])
    path = root / 'target' / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['nodes']['test.shop.unique_combination_stg_orders'] = {
        'unique_id': 'test.shop.unique_combination_stg_orders',
        'resource_type': 'test',
        'name': 'unique_combination_stg_orders',
        'package_name': 'shop',
        'original_file_path': 'models/schema.yml',
        'test_metadata': {
            'name': 'unique_combination_of_columns',
            'namespace': 'dbt_utils',
            'kwargs': {'combination_of_columns': ['order_id', 'customer_id']},
        },
        'attached_node': 'model.shop.stg_orders',
        'depends_on': {'nodes': ['model.shop.stg_orders'], 'macros': []},
        'config': {'severity': 'ERROR'},
    }
    path.write_text(json.dumps(manifest))
    project = Project(root)
    project.build(run_dbt=False)
    test = project.graph.nodes['test.shop.unique_combination_stg_orders']
    assert test.attrs['columns'] == ['order_id', 'customer_id']
    tests = _Tests(project.graph)
    assert tests.key('model.shop.stg_orders', {'order_id', 'customer_id', 'status'}) == test.id
    assert tests.key('model.shop.stg_orders', {'order_id'}) is None


def test_a_full_join_keeps_sums_and_refuses_row_counts(tmp_path: Path) -> None:
    """Orders with no payment reach the model as rows of their own: they leave the sum of amount
    alone and add to its row count."""
    models = {
        **MODELS,
        'payments_enriched': MODELS['payments_enriched'].replace('from p left join o', 'from p full join o'),
    }
    project = shop(tmp_path / 'full', models)
    con = duckdb.connect(str(project.root / 'shop.duckdb'))
    con.execute("insert into raw.orders values (99, 1, 'placed')")
    con.close()
    laws = by_id(check(project))
    assert 'count:model.shop.payments_enriched' not in laws
    assert laws['conservation:model.shop.daily_revenue#revenue'].always_held
    assert laws['conservation:model.shop.payments_enriched#amount'].always_held
    # Counting the model's rows downstream stops at the model.
    payments = laws['count:model.shop.daily_revenue#payments']
    assert [t.source for t in payments.terms] == ['model.shop.payments_enriched'] and payments.always_held
    blocked = {(b.model, b.column): b.reason for b in propose(project.graph, project.root).blocked}
    assert blocked[('model.shop.payments_enriched', None)] == (
        'a FULL or RIGHT join, which keeps the unmatched rows of stg_orders'
    )


UNION_MODELS = {
    **MODELS,
    'movements': (
        f'select id, paid_at, amount from {raw("payments")} '
        f'union all select id, cast(booked_on as timestamp), -amount * 2 from {raw("ledger")} where account_id = 1'
    ),
    'daily_movements': (
        f'select cast(paid_at as date) as day, sum(amount) as net, count(*) as moves from {rel("movements")} group by 1'
    ),
}


def test_a_union_all_adds_up_its_branches(tmp_path: Path) -> None:
    project = shop(tmp_path / 'union', UNION_MODELS)
    laws = by_id(check(project))
    movements = laws['conservation:model.shop.movements#amount']
    assert [(t.source, t.factor, t.filter) for t in movements.terms] == [
        ('source.shop.raw.payments', 1.0, None),
        ('source.shop.raw.ledger', -2.0, 'account_id = 1'),
    ]
    assert movements.statement == (
        'SUM(movements.amount) = SUM(raw.payments.amount) + (SUM(raw.ledger.amount) * -2 where account_id = 1), '
        'per day of movements.paid_at'
    )
    assert movements.always_held and movements.checked > 10
    # A law downstream of the union follows each branch back to its source.
    net = laws['conservation:model.shop.daily_movements#net']
    assert [t.source for t in net.terms] == ['source.shop.raw.payments', 'source.shop.raw.ledger']
    assert [[s.model for s in t.steps] for t in net.terms] == [
        ['model.shop.daily_movements', 'model.shop.movements'],
        ['model.shop.daily_movements', 'model.shop.movements'],
    ]
    assert net.always_held and laws['count:model.shop.daily_movements#moves'].always_held
    rows = laws['count:model.shop.movements']
    assert [t.source for t in rows.terms] == ['source.shop.raw.payments', 'source.shop.raw.ledger']
    assert rows.always_held
    again = Laws.from_dict(json.loads(json.dumps(Laws(laws=list(laws.values())).to_dict())))
    assert again.law(net.id) is not None and again.law(net.id).to_dict() == net.to_dict()  # type: ignore[union-attr]


def test_a_union_all_law_is_broken_where_its_branches_part(tmp_path: Path) -> None:
    models = {**UNION_MODELS, 'movements': UNION_MODELS['movements'].replace('-amount * 2', '-amount')}
    project = shop(tmp_path / 'union', models)
    proposed = propose(project.graph, project.root)
    # The model sums a third of what the law says: the law, read before the change, no longer holds.
    data = proposed.to_dict()
    for item in data['laws']:
        if item['id'] == 'conservation:model.shop.daily_movements#net':
            item['terms'][1]['factor'] = -3.0
            for step in item['terms'][1]['steps']:
                if step['model'] == 'model.shop.movements':
                    step['factor'] = -3.0
    net = by_id(check(project, Laws.from_dict(data)))['conservation:model.shop.daily_movements#net']
    assert net.status == 'broken' and net.broken_at == 'model.shop.movements'


def test_the_gate_names_a_union_that_gains_a_branch(tmp_path: Path) -> None:
    project = shop(tmp_path / 'union', UNION_MODELS)
    base = check(project)
    assert violations(project.graph, base, propose(project.graph, project.root)) == []
    models = {
        **UNION_MODELS,
        'movements': UNION_MODELS['movements']
        + f' union all select id, paid_at, amount from {raw("payments")}',
    }
    changed = shop(tmp_path / 'union', models)
    found = {v.law: v for v in violations(changed.graph, base, propose(changed.graph, changed.root))}
    net = found['conservation:model.shop.daily_movements#net']
    assert net.model == 'model.shop.movements'
    assert net.message == 'movements: amount now adds up 3 branches instead of 2'
    assert 'count:model.shop.movements' in found


# Additivity, by probes on the sample


LEDGER_MODELS = {
    **MODELS,
    'balances': (
        f'with daily as (select account_id, booked_on, sum(amount) as net from {raw("ledger")} group by 1, 2) '
        'select account_id, booked_on, sum(net) over (partition by account_id order by booked_on) as balance '
        'from daily'
    ),
    'average_payment': f'select order_id, avg(amount) as average_amount from {rel("stg_payments")} group by 1',
    'sku_revenue': (
        f'select i.sku, sum(p.amount) as sku_amount from {rel("stg_payments")} p '
        f'join {raw("items")} i on i.order_id = p.order_id group by 1'
    ),
}


def test_a_balance_is_semi_additive_an_average_non_additive_and_a_fan_out_breaks_additivity(
    tmp_path: Path,
) -> None:
    project = shop(tmp_path / 'ledger', LEDGER_MODELS)
    laws = by_id(check(project))
    balance = laws['additivity:model.shop.balances#balance']
    assert balance.expected == 'semi-additive' and balance.classification == 'semi-additive'
    assert balance.status == 'holds' and balance.terms[0].source == 'source.shop.raw.ledger'
    assert 'across' in str(balance.evidence) and 'days' in str(balance.evidence)
    assert (
        balance.statement
        == 'balances.balance is semi-additive: it may be summed across entities, not across days'
    )
    average = laws['additivity:model.shop.average_payment#average_amount']
    assert average.expected == average.classification == 'non-additive' and average.always_held
    assert 'by 1, not 2' in str(average.evidence)
    # The SQL says SUM, but each payment reaches every sku of its order: summing the column
    # across skus counts a payment more than once.
    sku = laws['additivity:model.shop.sku_revenue#sku_amount']
    assert sku.expected == 'additive' and sku.classification == 'non-additive'
    assert sku.status == 'broken' and sku.held == 0 and sku.checked == 1
    assert 'changed 2 rows' in str(sku.evidence)
    assert 'broken' in str(Laws(laws=[sku], warehouse='w'))
    # A window, an average and an untested join propose no conservation law.
    for column in ('balance', 'average_amount', 'sku_amount'):
        assert not any(law.column == column and law.kind == 'conservation' for law in laws.values())


AGGREGATED_MODELS = {
    **MODELS,
    'order_days': (
        'with d as (select order_id, cast(paid_at as date) as day, sum(amount) as amount '
        f'from {rel("stg_payments")} group by 1, 2) '
        'select order_id, day, amount, avg(amount) over (partition by order_id) as average_day from d'
    ),
    'order_summary': (
        'with d as (select order_id, cast(paid_at as date) as day, sum(amount) as amount '
        f'from {rel("stg_payments")} group by 1, 2) '
        'select order_id, avg(amount) as average_day, max(amount) as best_day from d group by 1'
    ),
}


def test_an_average_or_maximum_of_sums_is_non_additive_and_a_windowed_average_is_not_broken(
    tmp_path: Path,
) -> None:
    """Every row twice doubles an average of sums, and one row twice changes one of its rows, as for
    a sum; the totals on two halves of the input (split by day) do not add up to the whole."""
    project = shop(tmp_path / 'aggregated', AGGREGATED_MODELS)
    laws = by_id(check(project))
    for column in ('average_day', 'best_day'):
        law = laws[f'additivity:model.shop.order_summary#{column}']
        assert law.expected == law.classification == 'non-additive', law.evidence
        assert law.always_held and 'halves' in str(law.evidence)
    assert laws['additivity:model.shop.order_days#amount'].always_held
    assert 'add up to the whole' in str(laws['additivity:model.shop.order_days#amount'].evidence)
    # A windowed average changes every day of an order, as a balance does: the probes cannot show it
    # is less than the SQL says, so the law is not checked rather than broken.
    windowed = laws['additivity:model.shop.order_days#average_day']
    assert windowed.expected == 'non-additive' and windowed.status == 'unchecked'
    assert windowed.classification is None and 'semi-additive' in str(windowed.note)
    assert windowed.statement.endswith('is non-additive: its values may not be summed')


def test_a_sampled_warehouse_classifies_additivity_but_cannot_confirm_totals(project: Project) -> None:
    connection = duckdb.connect(str(project.root / 'shop.duckdb'), read_only=True)
    engine = SampledEngine(DbApiSource(connection, 'duckdb'), 'duckdb (sampled)', project.graph)
    laws = by_id(confirm(project.graph, project.root, engine))
    revenue = laws['conservation:model.shop.daily_revenue#revenue']
    assert revenue.status == 'unchecked' and 'sampled' in str(revenue.note)
    assert laws['additivity:model.shop.daily_revenue#revenue'].classification == 'additive'


def test_laws_on_relations_the_warehouse_does_not_hold_are_unchecked(
    project: Project, tmp_path: Path
) -> None:
    other = tmp_path / 'other.duckdb'
    duckdb.connect(str(other)).close()
    laws = confirm(project.graph, project.root, other)
    assert {law.status for law in laws.laws} == {'unchecked'}
    assert all(law.note for law in laws.laws)


def test_a_law_with_several_filters_is_checked(tmp_path: Path) -> None:
    models = {
        **MODELS,
        'card_revenue': (
            'select cast(paid_at as date) as day, sum(amount) as large_card_revenue '
            f"from {rel('stg_payments')} where method = 'card' and (amount > 2 or amount < 1) group by 1"
        ),
    }
    project = shop(tmp_path / 'filters', models)
    law = by_id(check(project))['conservation:model.shop.card_revenue#large_card_revenue']
    assert ' AND ' in str(law.terms[0].filter) and ' OR ' in str(law.terms[0].filter)
    assert law.always_held, (law.note, law.failures)


def test_a_law_with_no_rows_to_compare_is_unchecked(project: Project) -> None:
    con = duckdb.connect(str(project.root / 'shop.duckdb'))
    con.execute('delete from raw.payments')
    con.close()
    revenue = by_id(check(project))['conservation:model.shop.daily_revenue#revenue']
    assert revenue.status == 'unchecked' and revenue.note == 'no rows to compare'
    assert revenue.checked == 0 and not revenue.always_held


def test_the_tolerance_is_relative_to_the_size_of_the_values(tmp_path: Path) -> None:
    """Large amounts that cancel on each day leave a small net, which floating point cannot keep to
    a millionth of itself: the tolerance is a millionth of the day's sum of magnitudes."""
    ledger = (
        "select i as id, 1 as account_id, date '2026-03-01' + (i % 5)::int as booked_on, "
        'case when i % 2 = 0 then 123456789012.3 + 0.01 * i else -123456789012.3 end::double as amount '
        'from range(1, 401) t(i)'
    )
    models = {'scaled': f'select id, booked_on, amount * 0.1 as amount from {raw("ledger")}'}
    write_shop(tmp_path, models, [], {**RAW, 'ledger': ledger})
    project = Project(tmp_path)
    project.build(run_dbt=False)
    law = by_id(check(project))['conservation:model.shop.scaled#amount']
    assert law.always_held and law.checked == 5, law.failures
    # A real difference is still far outside it.
    strict = by_id(confirm(project.graph, tmp_path, tmp_path / 'shop.duckdb', tolerance=1e-18))
    assert strict['conservation:model.shop.scaled#amount'].status == 'broken'


def test_a_hand_edited_law_cannot_add_a_statement_or_a_query(project: Project) -> None:
    data = propose(project.graph, project.root).to_dict()
    edits = {
        'conservation:model.shop.daily_revenue#revenue': ('filter', '1 = 1; drop table raw.payments'),
        'conservation:model.shop.daily_revenue#card_revenue': (
            'filter',
            'amount > (select max(amount) from raw.orders)',
        ),
        'count:model.shop.daily_revenue#payments': (
            'partition',
            'paid_at) as d from raw.payments; select (1',
        ),
        'count:model.shop.payments_enriched': ('filter', 'pg_advisory_lock(1) is null'),
    }
    for item in data['laws']:
        if item['id'] in edits:
            what, sql = edits[item['id']]
            item['terms'][0][what] = sql
    laws = by_id(check(project, Laws.from_dict(data)))
    for law_id in edits:
        assert laws[law_id].status == 'unchecked', law_id
    assert 'not one expression' in str(laws['conservation:model.shop.daily_revenue#revenue'].note)
    assert 'reads more than' in str(laws['conservation:model.shop.daily_revenue#card_revenue'].note)
    assert 'not a known pure function' in str(laws['count:model.shop.payments_enriched'].note)
    assert laws['conservation:model.shop.stg_payments#amount'].always_held
    con = duckdb.connect(str(project.root / 'shop.duckdb'), read_only=True)
    assert con.execute('select count(*) from raw.payments').fetchone() == (80,)
    con.close()


def test_an_integer_total_is_compared_exactly(tmp_path: Path) -> None:
    """A duplicated 29 cent payment on a day of 30 million cents: a tolerance of a millionth of the
    day would hide it, an exact comparison does not."""
    payments = (
        "select i as id, i as order_id, timestamp '2026-03-01' + to_hours(i % 20) as paid_at, 'card' as method, "
        'case when i = 7 then 29 else 1000000 end as amount from range(1, 31) t(i)'
    )
    orders = "select i as id, 1 as customer_id, 'placed' as status from range(1, 31) t(i)"
    models = {
        'joined': (
            f'select p.id, p.paid_at, p.amount from {raw("payments")} p '
            f'left join {raw("orders")} o on o.id = p.order_id'
        )
    }
    write_shop(
        tmp_path, models, [('unique', 'orders', 'id')], {**RAW, 'payments': payments, 'orders': orders}
    )
    project = Project(tmp_path)
    project.build(run_dbt=False)
    assert by_id(check(project))['conservation:model.shop.joined#amount'].always_held
    con = duckdb.connect(str(tmp_path / 'shop.duckdb'))
    con.execute("insert into raw.orders values (7, 1, 'placed')")
    con.close()
    law = by_id(check(project))['conservation:model.shop.joined#amount']
    assert law.status == 'broken' and law.failures[0].actual - law.failures[0].expected == 29


def test_one_column_that_will_not_sum_leaves_the_rest_of_the_relation_checked(project: Project) -> None:
    data = propose(project.graph, project.root).to_dict()
    for item in data['laws']:
        if item['id'] == 'conservation:model.shop.daily_revenue#revenue':
            item['terms'][0]['source_column'] = 'method'
    laws = by_id(check(project, Laws.from_dict(data)))
    assert laws['conservation:model.shop.daily_revenue#revenue'].status == 'unchecked'
    # The count reads the same rows of raw.payments, in the query that failed as a whole.
    assert laws['count:model.shop.daily_revenue#payments'].always_held


def test_a_union_all_whose_branches_sum_and_count_adds_each_its_own_way(tmp_path: Path) -> None:
    counted = f'select order_id, count(*) as amount from {rel("stg_payments")} group by 1'
    branches = (f'select amount from {rel("stg_payments")}', f'select amount from {rel("counted")}')
    for first, second in (branches, branches[::-1]):
        models = {**MODELS, 'counted': counted, 'mixed': f'{first} union all {second}'}
        project = shop(tmp_path / 'mixed', models)
        law = by_id(check(project))['conservation:model.shop.mixed#amount']
        assert sorted((t.op, t.source) for t in law.terms) == [
            ('count', 'source.shop.raw.payments'),
            ('sum', 'source.shop.raw.payments'),
        ]
        assert law.always_held, (law.statement, law.note, law.failures)
        assert 'COUNT(*) of raw.payments' in law.statement


SELECTING_MODELS = {
    **MODELS,
    'latest': (
        f'with d as (select * from {rel("stg_payments")} '
        'qualify row_number() over (partition by order_id order by paid_at desc) = 1) '
        'select order_id, sum(amount) as last_amount from d group by 1'
    ),
    'distinct_amounts': (
        f'with d as (select distinct order_id, amount from {rel("stg_payments")}) '
        'select order_id, sum(amount) as distinct_amount from d group by 1'
    ),
    'recent': (
        f'select order_id, sum(amount) as recent_amount from {rel("stg_payments")} '
        f'where paid_at > (select max(paid_at) - interval 5 day from {rel("stg_payments")}) group by 1'
    ),
    'joined_distinct': (
        f'with d as (select distinct order_id from {rel("stg_payments")}) '
        f'select p.order_id, sum(p.amount) as joined_amount from {rel("stg_payments")} p '
        'join d on d.order_id = p.order_id group by 1'
    ),
    'running': (
        f'select id, account_id, booked_on, sum(amount) over (partition by account_id order by booked_on, id) '
        f'as running_balance from {raw("ledger")}'
    ),
    'order_stats': (
        f'select order_id, cast(paid_at as date) as day, avg(amount) as average_amount, max(amount) as max_amount, '
        f'count(distinct method) as methods from {rel("stg_payments")} group by 1, 2'
    ),
    'summed_stats': (
        'select day, sum(average_amount) as summed_average, sum(max_amount) as summed_max, '
        f'sum(methods) as summed_methods from {rel("order_stats")} group by 1'
    ),
}


def test_the_probes_do_not_judge_a_model_that_keeps_rows_by_comparing_them(tmp_path: Path) -> None:
    project = shop(tmp_path / 'selecting', SELECTING_MODELS)
    laws = by_id(check(project))
    for law_id, clause in (
        ('additivity:model.shop.latest#last_amount', 'QUALIFY'),
        ('additivity:model.shop.distinct_amounts#distinct_amount', 'SELECT DISTINCT'),
        ('additivity:model.shop.recent#recent_amount', 'subquery'),
        ('additivity:model.shop.joined_distinct#joined_amount', 'SELECT DISTINCT'),
    ):
        law = laws[law_id]
        assert law.status == 'unchecked' and clause in str(law.note), (law_id, law.status, law.note)
    # A running balance kept row by row returns each row twice when its input is: twice the rows,
    # each with twice the balance.
    running = laws['additivity:model.shop.running#running_balance']
    assert running.expected == running.classification == 'semi-additive', running.evidence
    assert running.always_held


def test_a_sum_of_an_upstream_average_maximum_or_distinct_count_is_not_additive(tmp_path: Path) -> None:
    project = shop(tmp_path / 'selecting', SELECTING_MODELS)
    laws = by_id(check(project))
    for column in ('summed_average', 'summed_max', 'summed_methods'):
        law = laws[f'additivity:model.shop.summed_stats#{column}']
        assert law.expected == 'non-additive', column
        assert law.status != 'holds' or law.classification == 'non-additive', (column, law.evidence)
        assert law.statement.endswith('is non-additive: its values may not be summed')


# laws.json


def test_laws_json_round_trips_and_refuses_a_newer_format(project: Project, tmp_path: Path) -> None:
    laws = check(project)
    path = tmp_path / 'out' / 'laws.json'
    laws.write(path)
    data = json.loads(path.read_text())
    assert next(iter(data)) == 'format_version' and data['format_version'] == data['format'] == FORMAT
    assert data['totals']['laws'] == len(laws.laws)
    again = load(path)
    assert again.to_dict()['laws'] == laws.to_dict()['laws']
    assert again.to_dict()['blocked'] == laws.to_dict()['blocked']
    assert again.warehouse == laws.warehouse and again.dialect == 'duckdb'
    assert violations(project.graph, again, propose(project.graph, project.root)) == []
    # A file written by Konstruct 0.13 carries the version as `format` alone.
    path.write_text(json.dumps({k: v for k, v in data.items() if k != 'format_version'}))
    assert load(path).to_dict()['laws'] == laws.to_dict()['laws']
    data['format_version'] = FORMAT + 1
    path.write_text(json.dumps(data))
    with pytest.raises(FormatError, match=r'written by a newer Konstruct .*upgrade Konstruct'):
        load(path)
    with pytest.raises(ValueError, match='no format number'):
        Laws.from_dict({'laws': []})


def test_proposed_laws_say_they_are_unchecked(project: Project) -> None:
    laws = propose(project.graph, project.root)
    assert laws.summary().endswith('none checked yet.')
    assert all(law.status == 'proposed' for law in laws.laws)
    assert Laws().summary() == 'No law follows from the lineage.'
    assert laws.law('count:model.shop.stg_orders') is not None and laws.law('nothing') is None


# The command line, the Python API, the MCP server and the gate


def _git(root: Path, *args: str) -> None:
    identity = ['-c', 'user.name=t', '-c', 'user.email=t@example.com', '-c', 'commit.gpgsign=false']
    subprocess.run(['git', '-C', str(root), *identity, *args], check=True, capture_output=True)


def _run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str]:
    capsys.readouterr()
    try:
        code = main(['--no-color', '-q', *args])
    except SystemExit as stop:
        code = int(stop.code or 0)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_verify_laws_writes_laws_json(project: Project, capsys: pytest.CaptureFixture[str]) -> None:
    root = str(project.root)
    code, out = _run(capsys, '-C', root, 'verify', '--laws', '--json')
    document = json.loads(out)
    assert code == 0 and document['format_version'] == FORMAT and document['warehouse'] == 'shop.duckdb'
    assert document['totals']['broken'] == 0 and document['totals']['holds'] == len(document['laws'])
    written = json.loads((project.out_dir / 'laws.json').read_text())
    assert written['laws'] == document['laws'] and written['format_version'] == FORMAT
    code, out = _run(capsys, '-C', root, 'verify', '--laws')
    assert code == 0 and out.startswith('Data laws checked against shop.duckdb (tolerance 1e-09)\n')
    assert '  holds     SUM(daily_revenue.revenue) = SUM(raw.payments.amount) / 100' in out
    assert 'Wrote laws.json to ' in out
    # Proposing reads no warehouse and writes nothing: the checked laws stay as they were.
    code, out = _run(capsys, '-C', root, 'verify', '--laws', '--propose', '--warehouse', 'nowhere.duckdb')
    assert code == 0 and 'none checked yet.' in out and '  proposed  ' in out
    assert 'Wrote laws.json' not in out
    assert load(project.out_dir / 'laws.json').warehouse == 'shop.duckdb'
    # A planted fan-out breaks laws; --strict turns that into exit status 1.
    con = duckdb.connect(str(project.root / 'shop.duckdb'))
    con.execute("insert into raw.orders values (7, 2, 'placed')")
    con.close()
    code, out = _run(capsys, '-C', root, 'verify', '--laws', '--strict', '--tolerance', '0.001')
    assert code == 1 and '  broken    ' in out and 'parts at payments_enriched' in out
    assert '(tolerance 0.001)' in out
    assert _run(capsys, '-C', root, 'verify', '--laws')[0] == 0
    code, out = _run(capsys, '-C', root, 'verify', '--laws', '-m', 'daily_revenue')
    assert code == 2 and '--model does not apply to --laws' in out
    for args, said in (
        (['--propose'], '--propose is for the data laws'),
        (['--tolerance', '0.1'], '--tolerance is for the data laws'),
        (['--mutate', '--tolerance', '0.1'], '--tolerance is for the data laws'),
        (['--laws', '--mutate'], '--laws and --mutate are separate modes of verify'),
        (['--laws', '--why', 'daily_revenue.revenue'], '--laws and --why are separate modes of verify'),
    ):
        code, out = _run(capsys, '-C', root, 'verify', *args)
        assert code == 2 and said in out, args


def test_project_laws_and_the_measures_with_no_law(tmp_path: Path) -> None:
    models = {
        **MODELS,
        'payments_enriched': MODELS['payments_enriched'].replace(
            'from p left join o on o.order_id = p.order_id',
            f'from p left join o on o.order_id = p.order_id left join {raw("items")} i on i.order_id = p.order_id',
        ),
    }
    project = shop(tmp_path / 'shop', models)
    laws = project.laws(write=False)
    assert laws.warehouse is None and not (project.out_dir / 'laws.json').exists()
    blocked = {(b['model'], b['column']): b['reason'] for b in laws.to_dict()['blocked']}
    assert 'uniqueness test' in blocked[('model.shop.payments_enriched', 'amount')]
    text = str(laws)
    assert '\nMeasures with no law (' in text and '  payments_enriched.amount  ' in text
    checked = project.laws(confirm=True, tolerance=0.5, max_rows=100)
    assert checked.tolerance == 0.5 and checked.warehouse == 'shop.duckdb'
    assert load(project.out_dir / 'laws.json').to_dict()['laws'] == checked.to_dict()['laws']
    # Proposing again writes nothing, so the checked laws the gate reads stay as they were.
    project.laws()
    assert load(project.out_dir / 'laws.json').to_dict()['laws'] == checked.to_dict()['laws']


FANNED = {
    **MODELS,
    'payments_enriched': MODELS['payments_enriched'].replace(
        'from p left join o on o.order_id = p.order_id',
        f'from p left join o on o.order_id = p.order_id left join {raw("items")} i on i.order_id = p.order_id',
    ),
}


def _repository(root: Path, models: dict[str, str] = MODELS) -> Project:
    """The shop with each model's SQL in its file, as in a dbt project, laws.json committed (the
    rest of konstruct-out ignored) and the laws confirmed on the data."""
    project = shop(root, models)
    _write_models(root, models)
    (root / '.gitignore').write_text('konstruct-out/*\n!konstruct-out/laws.json\n')
    project.laws(confirm=True)
    _git(root, 'init', '-q', '-b', 'main')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'Shop')
    return project


def _write_models(root: Path, models: dict[str, str]) -> None:
    (root / 'models').mkdir(exist_ok=True)
    for name, sql in models.items():
        (root / 'models' / f'{name}.sql').write_text(sql)


def _law_findings(project: Project) -> list[Any]:
    return [f for f in project.gate(base='HEAD', update=False).findings if f.rule == 'data-law-broken']


def test_the_gate_fails_on_a_law_the_change_breaks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _repository(tmp_path / 'shop')
    root = project.root
    assert _law_findings(project) == []
    # The model's file and the manifest dbt compiles from it both change.
    changed = shop(root, FANNED)
    _write_models(root, FANNED)
    found = _law_findings(changed)
    assert found and {f.path for f in found} == {'models/payments_enriched.sql'}
    assert any(
        f.message.startswith('the change breaks a law that held on all the data: SUM(daily_revenue.revenue)')
        and 'uniqueness test' in f.message
        for f in found
    )
    assert all('konstruct verify --laws' in f.fix for f in found)
    code, out = _run(capsys, '-C', str(root), 'gate', '--base', 'HEAD', '--no-update')
    assert code == 1 and 'data-law-broken: the change breaks a law' in out


def test_one_gate_reports_a_broken_law_and_a_conflicting_definition(tmp_path: Path) -> None:
    """The gate finds the conflicts of the columns a change alters itself, and judges the laws
    recorded at the base in the same run."""
    project = _repository(tmp_path / 'shop')
    root = project.root
    models = {
        **FANNED,
        'card_daily': (
            "select cast(paid_at as date) as day, sum(amount) as revenue from {} where method = 'card' "
            'group by 1'
        ).format(rel('stg_payments')),
    }
    changed = shop(root, models)
    _write_models(root, models)
    result = changed.gate(base='HEAD', update=False)
    rules = {f.rule for f in result.findings}
    assert {'data-law-broken', 'conflicting-definition'} <= rules, [str(f) for f in result.findings]
    assert result.laws_from == 'konstruct-out/laws.json at HEAD'


def test_the_gate_reads_an_edit_dbt_has_not_compiled(tmp_path: Path) -> None:
    """An agent's gate runs no dbt, so the manifest still holds the SQL from before the edit: the
    laws are read from the model's file as it is now."""
    project = _repository(tmp_path / 'shop')
    _write_models(project.root, {'payments_enriched': FANNED['payments_enriched']})
    found = _law_findings(project)
    assert found and {f.path for f in found} == {'models/payments_enriched.sql'}
    assert any('uniqueness test' in f.message for f in found)


def test_a_law_broken_on_the_base_fails_only_the_change_that_breaks_it(tmp_path: Path) -> None:
    project = _repository(tmp_path / 'shop')
    root = project.root
    # main breaks the law and moves on without recording the laws again.
    shop(root, FANNED)
    _write_models(root, FANNED)
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'Join the items')
    (root / 'README.md').write_text('A change to the documentation alone.\n')
    later = Project(root)
    assert _law_findings(later) == []
    # A change to another model of the same law is judged on its own step.
    scaled = {**FANNED, 'stg_payments': FANNED['stg_payments'].replace('/ 100.0', '/ 1000.0')}
    shop(root, scaled)
    _write_models(root, scaled)
    found = _law_findings(Project(root))
    assert found and {f.path for f in found} == {'models/stg_payments.sql'}
    assert all('/ 1000' in f.message for f in found)


def test_the_gate_reads_the_laws_committed_at_the_base(tmp_path: Path) -> None:
    """In CI the output directory is built afresh, so the laws come from the base commit; laws
    recorded after the change do not hide what it breaks."""
    project = _repository(tmp_path / 'shop')
    root = project.root
    _write_models(root, {'payments_enriched': FANNED['payments_enriched']})
    shop(root, FANNED).laws(confirm=True)
    head = load(project.out_dir / 'laws.json')
    assert head.law('conservation:model.shop.payments_enriched#amount') is None
    assert _law_findings(Project(root))
    (project.out_dir / 'laws.json').unlink()
    assert _law_findings(Project(root))
    based, where = recorded(root, project.out_dir, 'HEAD')
    assert based is not None and based.law('conservation:model.shop.payments_enriched#amount') is not None
    assert where == 'konstruct-out/laws.json at HEAD'
    assert Project(root).gate(base='HEAD', update=False).laws_from == where
    # In git, only the base's laws.json counts: a copy in the output directory does not stand in
    # for it, and the gate says that no law was checked.
    unrecorded = shop(tmp_path / 'unrecorded')
    (unrecorded.root / '.gitignore').write_text('konstruct-out/\n')
    _git(unrecorded.root, 'init', '-q', '-b', 'main')
    _git(unrecorded.root, 'add', '-A')
    _git(unrecorded.root, 'commit', '-qm', 'Shop')
    unrecorded.laws(confirm=True)
    assert recorded(unrecorded.root, unrecorded.out_dir, 'HEAD') == (
        None,
        'no konstruct-out/laws.json at HEAD, so no data law was checked',
    )
    result = unrecorded.gate(base='HEAD', update=False)
    assert result.to_dict()['laws_from'] == 'no konstruct-out/laws.json at HEAD, so no data law was checked'
    assert str(result).endswith(
        '\n\nData laws: no konstruct-out/laws.json at HEAD, so no data law was checked.'
    )
    assert result.markdown().endswith(
        'Data laws: no konstruct-out/laws.json at HEAD, so no data law was checked.\n'
    )
    # With no git base to read at, the output directory's laws.json is read.
    loose = shop(tmp_path / 'loose')
    assert recorded(loose.root, loose.out_dir) == (None, 'no data laws recorded, so none was checked')
    loose.laws(confirm=True)
    laws, where = recorded(loose.root, loose.out_dir)
    assert laws is not None and where == "the output directory's laws.json, with no git base to read one at"
    (loose.out_dir / 'laws.json').write_text('{"laws": []}')
    with pytest.raises(QueryError, match=r'cannot read .*laws\.json.*no format number'):
        recorded(loose.root, loose.out_dir)


def test_an_aggregate_that_is_no_longer_one_breaks_its_additivity(tmp_path: Path) -> None:
    project = _repository(tmp_path / 'shop')
    flat = {
        **MODELS,
        'daily_revenue': (
            'select cast(paid_at as date) as day, amount as revenue, 1 as payments, '
            "case when method = 'card' then amount else 0 end as card_revenue "
            f'from {rel("payments_enriched")}'
        ),
    }
    changed = shop(project.root, flat)
    _write_models(project.root, flat)
    found = {f.message for f in _law_findings(changed)}
    assert any('daily_revenue: revenue is no longer an aggregate' in m for m in found), found


# The gate without dbt, on a dbt project: the manifest is from before the change


@pytest.fixture(scope='module')
def lawful_jaffle(jaffle_built_graph: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """jaffle_shop with stg_payments converting cents through a macro, and models that join
    payments on a key only a test covers: a model's unique test, a source's unique test and a
    package's unique_combination_of_columns test. Built by dbt, its laws confirmed and laws.json
    committed."""
    from konstruct.engine.build import build
    from konstruct.engine.config import load_config

    from .conftest import dbt_executable, run_dbt

    dbt = dbt_executable()
    assert dbt is not None
    root = tmp_path_factory.mktemp('lawful') / 'jaffle_shop'
    shutil.copytree(jaffle_built_graph, root)
    (root / 'macros').mkdir(exist_ok=True)
    (root / 'macros' / 'cents.sql').write_text('{% macro cents(c) %}({{ c }} / 100){% endmacro %}\n')
    staging = root / 'models' / 'staging' / 'stg_payments.sql'
    staging.write_text(
        staging.read_text().replace('amount / 100 as amount', "{{ cents('amount') }} as amount")
    )
    (root / 'models' / 'pay_enriched.sql').write_text(
        'select p.payment_id, p.amount, o.order_date\n'
        "from {{ ref('stg_payments') }} p\nleft join {{ ref('stg_orders') }} o on o.order_id = p.order_id\n"
    )
    package = root.parent / 'pkgs' / 'dbt_utils'
    (package / 'macros').mkdir(parents=True)
    (package / 'dbt_project.yml').write_text('name: dbt_utils\nversion: "1.0.0"\nconfig-version: 2\n')
    (package / 'macros' / 'combination.sql').write_text(
        '{% test unique_combination_of_columns(model, combination_of_columns) %}\n'
        "select {{ combination_of_columns | join(', ') }} from {{ model }} "
        "group by {{ combination_of_columns | join(', ') }} having count(*) > 1\n{% endtest %}\n"
    )
    (root / 'packages.yml').write_text('packages:\n  - local: ../pkgs/dbt_utils\n')
    (root / 'models' / 'keys.yml').write_text(
        'version: 2\nsources:\n  - name: ext\n    schema: raw\n    tables:\n      - name: order_keys\n'
        '        columns:\n          - name: order_id\n            tests:\n              - unique\n'
        'models:\n  - name: orders_keyed\n    tests:\n      - dbt_utils.unique_combination_of_columns:\n'
        "          arguments:\n            combination_of_columns: ['order_id']\n"
    )
    (root / 'models' / 'orders_keyed.sql').write_text(
        "select order_id, customer_id from {{ ref('stg_orders') }}\n"
    )
    for name, joined in (('pay_src', "source('ext', 'order_keys')"), ('pay_combo', "ref('orders_keyed')")):
        (root / 'models' / f'{name}.sql').write_text(
            f"select p.payment_id, p.amount from {{{{ ref('stg_payments') }}}} p\n"
            f'left join {{{{ {joined} }}}} k on k.order_id = p.order_id\n'
        )
    import duckdb

    con = duckdb.connect(str(root / 'jaffle_shop.duckdb'))
    con.execute('create schema if not exists raw')
    con.execute('create or replace table raw.order_keys as select id as order_id from main.raw_orders')
    con.close()
    run_dbt(root, 'deps')
    run_dbt(
        root,
        'build',
        '-s',
        'stg_payments',
        'pay_enriched',
        'orders_keyed',
        'pay_src',
        'pay_combo',
        'source:ext',
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('PATH', f'{Path(dbt).parent}{os.pathsep}{os.environ.get("PATH", "")}')
        patch.setenv('DBT_SEND_ANONYMOUS_USAGE_STATS', 'false')
        build(load_config(root, trusted=True))
        Project(root).laws(confirm=True)
    (root / '.gitignore').write_text(
        'konstruct-out/*\n!konstruct-out/laws.json\ntarget/\nlogs/\n*.duckdb*\n.user.yml\ndbt_packages/\n'
    )
    _git(root, 'init', '-q', '-b', 'main')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', 'Jaffle shop with its laws')
    return root


def _no_dbt_gate(lawful_jaffle: Path, tmp_path: Path, change: Any) -> list[Any]:
    """The gate an agent runs, from files alone, on a copy of ``lawful_jaffle`` after ``change``."""
    root = tmp_path / 'jaffle_shop'
    shutil.copytree(lawful_jaffle, root)
    change(root)
    result = Project(root).gate(base='HEAD', run_dbt=False, warehouses=False)
    assert result.laws_from == 'konstruct-out/laws.json at HEAD'
    return [f for f in result.findings if f.rule == 'data-law-broken']


@pytest.mark.dbt
def test_steps_record_the_macros_yaml_and_project_files_they_follow_from(lawful_jaffle: Path) -> None:
    laws, _ = recorded(lawful_jaffle, lawful_jaffle / 'konstruct-out', 'HEAD')
    assert laws is not None
    law = laws.law('conservation:model.jaffle_shop.pay_enriched#amount')
    assert law is not None and law.always_held
    files = {f for step in law.steps for f in step.files}
    assert {
        'models/pay_enriched.sql',
        'models/staging/stg_payments.sql',
        'macros/cents.sql',
        'dbt_project.yml',
        'models/staging/schema.yml',
    } <= files
    customers = laws.law('count:table.jaffle_shop.reporting.customer_ltv')
    assert customers is not None and {f for s in customers.steps for f in s.files} == {
        'dags/sql/customer_ltv.sql'
    }


@pytest.mark.dbt
def test_a_macro_edit_dbt_has_not_compiled_breaks_a_law(lawful_jaffle: Path, tmp_path: Path) -> None:
    def tenth(root: Path) -> None:
        (root / 'macros' / 'cents.sql').write_text('{% macro cents(c) %}({{ c }} / 1000){% endmacro %}\n')

    found = _no_dbt_gate(lawful_jaffle, tmp_path, tenth)
    assert found and all('/ 1000 instead of' in f.message for f in found)
    assert {f.path for f in found} == {'models/staging/stg_payments.sql'}


@pytest.mark.dbt
def test_the_mcp_gate_judges_its_copy_against_the_projects_laws(lawful_jaffle: Path, tmp_path: Path) -> None:
    """The MCP gate updates a copy of the graph in its own directory; the laws are still the
    project's, committed at the base."""
    from konstruct.interfaces.mcp_server import gate_copy

    root = tmp_path / 'jaffle_shop'
    shutil.copytree(lawful_jaffle, root)
    (root / 'macros' / 'cents.sql').write_text('{% macro cents(c) %}({{ c }} / 1000){% endmacro %}\n')
    result = gate_copy(Project(root), 'HEAD')
    assert result.laws_from == 'konstruct-out/laws.json at HEAD'
    assert [f for f in result.findings if f.rule == 'data-law-broken']


@pytest.mark.dbt
def test_a_removed_uniqueness_test_dbt_has_not_compiled_breaks_a_law(
    lawful_jaffle: Path, tmp_path: Path
) -> None:
    def untest(root: Path) -> None:
        schema = root / 'models' / 'staging' / 'schema.yml'
        text = schema.read_text()
        kept = text.replace(
            '      - name: order_id\n        tests:\n          - unique\n          - not_null',
            '      - name: order_id\n        tests:\n          - not_null',
            1,
        )
        assert kept != text
        schema.write_text(kept)

    found = _no_dbt_gate(lawful_jaffle, tmp_path, untest)
    assert found and all('no uniqueness test covers' in f.message for f in found)
    assert {f.path for f in found} == {'models/pay_enriched.sql'}


@pytest.mark.dbt
@pytest.mark.parametrize(
    ('model', 'removal'),
    [
        (
            'pay_src',
            (
                '          - name: order_id\n            tests:\n              - unique\n',
                '          - name: order_id\n',
            ),
        ),
        (
            'pay_combo',
            (
                '    tests:\n      - dbt_utils.unique_combination_of_columns:\n          arguments:\n'
                "            combination_of_columns: ['order_id']\n",
                '',
            ),
        ),
    ],
    ids=['a source test', 'a package test'],
)
def test_a_removed_source_or_package_test_dbt_has_not_compiled_breaks_a_law(
    lawful_jaffle: Path, tmp_path: Path, model: str, removal: tuple[str, str]
) -> None:
    laws, _ = recorded(lawful_jaffle, lawful_jaffle / 'konstruct-out', 'HEAD')
    law = laws.law(f'conservation:model.jaffle_shop.{model}#amount') if laws else None
    assert law is not None and law.always_held and law.relies_on

    def untest(root: Path) -> None:
        keys = root / 'models' / 'keys.yml'
        text = keys.read_text()
        assert removal[0] in text
        keys.write_text(text.replace(*removal))

    found = _no_dbt_gate(lawful_jaffle, tmp_path, untest)
    assert {f.path for f in found} == {f'models/{model}.sql'}
    assert all('no uniqueness test covers' in f.message for f in found)


@pytest.mark.dbt
def test_a_deleted_model_dbt_has_not_compiled_breaks_its_laws(lawful_jaffle: Path, tmp_path: Path) -> None:
    def delete(root: Path) -> None:
        _git(root, 'rm', '-q', 'models/pay_enriched.sql')

    found = _no_dbt_gate(lawful_jaffle, tmp_path, delete)
    assert found and all(f.message.endswith('the model is removed') for f in found)

    def move(root: Path) -> None:
        (root / 'models' / 'enriched').mkdir()
        _git(root, 'mv', 'models/pay_enriched.sql', 'models/enriched/pay_enriched.sql')

    # A model moved to another folder is the same model: dbt names it by its file.
    assert _no_dbt_gate(lawful_jaffle, tmp_path / 'moved', move) == []


@pytest.mark.dbt
def test_a_sql_built_table_broken_on_the_base_fails_only_the_change_that_breaks_it(
    lawful_jaffle: Path, tmp_path: Path
) -> None:
    def unfilter(root: Path) -> None:
        built = root / 'dags' / 'sql' / 'customer_ltv.sql'
        built.write_text(built.read_text().replace('where c.customer_lifetime_value > 0', ''))

    found = _no_dbt_gate(lawful_jaffle, tmp_path, unfilter)
    assert found and {f.path for f in found} == {'dags/sql/customer_ltv.sql'}

    def merged_then_documented(root: Path) -> None:
        unfilter(root)
        _git(root, 'commit', '-qam', 'Drop the filter')
        (root / 'models' / 'overview.md').write_text('Documentation only.\n')

    assert _no_dbt_gate(lawful_jaffle, tmp_path / 'later', merged_then_documented) == []


def test_the_laws_tool(project: Project) -> None:
    import anyio
    from mcp import Client

    from konstruct.interfaces.mcp_server import UNTRUSTED_CONTENT, build_server

    async def main() -> tuple[dict[str, Any], dict[str, Any]]:
        async with Client(build_server(project.root)) as client:
            proposed = await client.call_tool('laws', {})
            confirmed = await client.call_tool(
                'laws',
                {
                    'confirm': True,
                    'warehouse': str(project.root / 'shop.duckdb'),
                    'tolerance': 0.5,
                    'max_rows': 50,
                },
            )
            assert isinstance(proposed.structured_content, dict)
            assert isinstance(confirmed.structured_content, dict)
            return proposed.structured_content, confirmed.structured_content

    proposed, confirmed = anyio.run(main)
    assert proposed['untrusted_content'] == UNTRUSTED_CONTENT and proposed['warehouse'] is None
    assert {law['status'] for law in proposed['laws']} == {'proposed'}
    assert confirmed['totals']['holds'] == len(confirmed['laws']) and confirmed['warehouse'] == 'shop.duckdb'
    assert confirmed['tolerance'] == 0.5
    # The tool writes nothing.
    assert not (project.out_dir / 'laws.json').exists()


# The example projects: no law that does not hold


def test_no_false_law_on_jaffle_shop(jaffle_built_graph: Path) -> None:
    project = Project(jaffle_built_graph)
    laws = confirm(project.graph, project.root, project.root / 'jaffle_shop.duckdb')
    found = by_id(laws)
    assert laws.totals()['broken'] == 0, str(laws)
    assert laws.totals()['holds'] >= 14
    payments = found['conservation:model.jaffle_shop.stg_payments#amount']
    assert payments.terms[0].factor == pytest.approx(0.01) and payments.always_held
    orders = found['count:model.jaffle_shop.orders']
    assert orders.terms[0].source == 'seed.jaffle_shop.raw_orders' and orders.partition is not None
    assert orders.held == orders.checked > 30
    # orders.amount reads payments through a left join from orders, which drops a payment with no
    # order; the relationships test on stg_payments.order_id says there is none, so the amount is
    # conserved, and it holds.
    assert found['conservation:model.jaffle_shop.orders#amount'].always_held
    assert found['additivity:model.jaffle_shop.orders#amount'].always_held


@pytest.mark.postgres
def test_no_false_law_on_the_postgres_example(tmp_path: Path) -> None:
    """jaffle_shop built on Postgres: its laws hold in a rolled-back transaction, and the division
    of integer cents, which Postgres rounds, proposes no conservation law."""
    import uuid
    from urllib.parse import urlparse

    import psycopg

    from .conftest import EXAMPLE, run_dbt
    from .test_warehouse import require

    dsn = require('KONSTRUCT_TEST_PG_DSN')
    url = urlparse(dsn)
    schema = f'kl_{uuid.uuid4().hex[:8]}'
    root = tmp_path / 'shop'
    shutil.copytree(
        EXAMPLE, root, ignore=shutil.ignore_patterns('target', 'logs', 'konstruct-out', '*.duckdb', 'dags')
    )
    (root / 'profiles.yml').write_text(
        'jaffle_shop:\n  target: dev\n  outputs:\n    dev:\n      type: postgres\n'
        f'      host: {url.hostname}\n      port: {url.port or 5432}\n      user: {url.username}\n'
        f'      password: {url.password}\n      dbname: {url.path.lstrip("/")}\n      schema: {schema}\n      threads: 4\n'
    )
    try:
        run_dbt(root, 'build')
        run_dbt(root, 'docs', 'generate')
        project = Project(root)
        project.build(run_dbt=False)
        laws = confirm(project.graph, project.root, dsn)
        assert laws.warehouse and laws.warehouse.startswith('postgres database')
        assert laws.totals()['broken'] == 0, str(laws)
        found = by_id(laws)
        assert found['count:model.jaffle_shop.orders'].always_held
        assert found['count:model.jaffle_shop.stg_payments'].always_held
        assert 'conservation:model.jaffle_shop.stg_payments#amount' not in found
        [blocked] = [
            b for b in laws.blocked if (b.model, b.column) == ('model.jaffle_shop.stg_payments', 'amount')
        ]
        assert 'integer division' in blocked.reason
        with psycopg.connect(dsn) as con:
            leftovers = con.execute("select count(*) from pg_class where relname like 'kp_%'").fetchone()
        assert leftovers == (0,)
    finally:
        with psycopg.connect(dsn, autocommit=True) as con:
            con.execute(f'drop schema if exists {schema} cascade')


@pytest.mark.postgres
def test_totals_on_postgres_are_read_in_a_read_only_transaction() -> None:
    from konstruct.analysis.verify import PostgresEngine

    from .test_warehouse import require

    engine = PostgresEngine(require('KONSTRUCT_TEST_PG_DSN'))
    try:
        assert engine.read('select 1 as one') == (['one'], [(1,)])
        with pytest.raises(Exception, match='read-only'):
            engine.read('create table kp_laws_written (a int)')
        # The transaction is read-write again for the copies verify makes.
        engine.con.execute('create temporary table kp_laws_copy as select 1 as a')
        assert engine.run('select a from kp_laws_copy') == (['a'], [(1,)])
    finally:
        engine.close()


# Soundness against a brute-force oracle: data that keeps the tests keeps every law, and a
# duplicated order breaks exactly the laws that cross the join, at the join.


ORACLE_MODELS = {
    **MODELS,
    'payments_full': (
        'select p.payment_id, p.paid_at, p.amount, o.status '
        f'from {rel("stg_payments")} p full join {rel("stg_orders")} o on o.order_id = p.order_id'
    ),
    'daily_full': (
        'select cast(p.paid_at as date) as day, count(*) as full_rows, count(p.payment_id) as payments, '
        f'sum(p.amount) as revenue from {rel("stg_payments")} p '
        f'full join {rel("stg_orders")} o on o.order_id = p.order_id group by 1'
    ),
}


@pytest.fixture(scope='module')
def oracle_shop(tmp_path_factory: pytest.TempPathFactory) -> tuple[Project, Laws]:
    project = shop(tmp_path_factory.mktemp('oracle') / 'shop', ORACLE_MODELS)
    return project, propose(project.graph, project.root)


payments_data = st.lists(
    st.tuples(st.integers(1, 6), st.integers(0, 4), st.integers(0, 900), st.sampled_from(['card', 'bank'])),
    min_size=1,
    max_size=25,
)


def _load_raw(project: Project, payments: list[tuple[int, int, int, str]], duplicate: int | None) -> None:
    values = ', '.join(
        f"({i + 1}, {order}, timestamp '2026-03-01' + to_hours({day * 24 + i % 7}), '{method}', {amount})"
        for i, (order, day, amount, method) in enumerate(payments)
    )
    orders = "select i as id, i % 3 as customer_id, 'placed' as status from range(1, 7) t(i)"
    if duplicate is not None:
        orders += f" union all select {duplicate}, 0, 'placed'"
    con = duckdb.connect(str(project.root / 'shop.duckdb'))
    try:
        con.execute(
            'create or replace table raw.payments (id int, order_id int, paid_at timestamp, method varchar, amount int)'
        )
        con.execute(f'insert into raw.payments values {values}')
        con.execute(f'create or replace table raw.orders as {orders}')
    finally:
        con.close()


@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(payments=payments_data, duplicate=st.one_of(st.none(), st.integers(1, 6)))
def test_laws_agree_with_a_brute_force_oracle(
    oracle_shop: tuple[Project, Laws], payments: list[tuple[int, int, int, str]], duplicate: int | None
) -> None:
    project, proposed = oracle_shop
    _load_raw(project, payments, duplicate)
    laws = by_id(check(project, proposed))
    crossed = [p for p in payments if p[0] == duplicate]
    expect_rows_broken = bool(crossed)
    expect_sum_broken = any(amount for _, _, amount, _ in crossed)
    # Orders with no payment are rows of payments_full and daily_full: their row counts follow from
    # no input, while sums and counts of a payment column do.
    assert (
        'count:model.shop.payments_full' not in laws and 'count:model.shop.daily_full#full_rows' not in laws
    )
    for law_id, broken, at in (
        ('count:model.shop.payments_enriched', expect_rows_broken, 'payments_enriched'),
        ('count:model.shop.daily_revenue#payments', expect_rows_broken, 'payments_enriched'),
        ('conservation:model.shop.payments_enriched#amount', expect_sum_broken, 'payments_enriched'),
        ('conservation:model.shop.daily_revenue#revenue', expect_sum_broken, 'payments_enriched'),
        ('count:model.shop.daily_full#payments', expect_rows_broken, 'daily_full'),
        ('conservation:model.shop.payments_full#amount', expect_sum_broken, 'payments_full'),
        ('conservation:model.shop.daily_full#revenue', expect_sum_broken, 'daily_full'),
    ):
        law = laws[law_id]
        assert (law.status == 'broken') is broken, (law_id, law.status, law.failures)
        if broken:
            assert law.broken_at == f'model.shop.{at}'
            days = {p[1] for p in crossed if law.kind == 'count' or p[2]}
            assert len(law.failures) == len(days)
    for law_id in ('count:model.shop.stg_payments', 'conservation:model.shop.stg_payments#amount'):
        assert laws[law_id].always_held


# The SQL reading, one statement at a time


def _graph(tests: Sequence[tuple[str, str, str]] = (), dialect: str = 'duckdb') -> Graph:
    graph = Graph()
    graph.meta['dialect'] = dialect
    tables = {
        't': {'k': 'INTEGER', 'amount': 'INTEGER', 'm': 'VARCHAR', 'd': 'DATE', 'f': 'INTEGER'},
        'u': {'k': 'INTEGER', 'z': 'INTEGER', 'w': 'INTEGER'},
        # A table the graph knows only one column of.
        'v': {'k': 'INTEGER'},
    }
    for name, columns in tables.items():
        node = graph.add_node(
            Node(id=f'table.db.main.{name}', kind='table', name=name, relation=f'db.main.{name}')
        )
        for column, dtype in columns.items():
            graph.add_column(node, column, data_type=dtype)
    for kind, table, column in tests:
        if kind == 'primary_key':
            graph.nodes[f'table.db.main.{table}'].attrs['primary_key'] = column.split(',')
            continue
        attrs: dict[str, Any] = {'test': kind}
        target = column_id(f'table.db.main.{table}', column)
        if kind == 'unique_combination_of_columns':
            attrs['columns'], target = column.split(','), f'table.db.main.{table}'
        test = graph.add_node(Node(id=f'test.{kind}.{table}.{column}', kind='test', name=kind, attrs=attrs))
        graph.add_edge(test.id, target, 'tests')
    if any(kind == 'relationships' for kind, _, _ in tests):
        for test in [
            n for n in graph.nodes.values() if n.kind == 'test' and n.attrs['test'] == 'relationships'
        ]:
            graph.add_edge(test.id, column_id('table.db.main.u', 'k'), 'tests', role='relationship target')
    return graph


def _branches(
    sql: str, tests: Sequence[tuple[str, str, str]] = (), dialect: str = 'duckdb'
) -> tuple[dict[str, list[_Flow] | str], list[_Path] | str]:
    graph = _graph(tests, dialect)
    model = graph.add_node(Node(id='model.x.m', kind='model', name='m', relation='db.main.m'))
    return _Analyser(graph).model(Candidate(model, sql, 'db', 'main'))


def _read(
    sql: str, tests: Sequence[tuple[str, str, str]] = (), dialect: str = 'duckdb'
) -> tuple[dict[str, _Flow | str], _Path | str]:
    """The flows and rows of a query that is not a UNION ALL: one of each."""
    flows, rows = _branches(sql, tests, dialect)
    single = {name: flow if isinstance(flow, str) else flow[0] for name, flow in flows.items()}
    assert all(isinstance(flow, str) or len(flow) == 1 for flow in flows.values())
    assert isinstance(rows, str) or len(rows) == 1
    return single, rows if isinstance(rows, str) else rows[0]


UNIQUE_U = [('unique', 'u', 'k')]
REFERENTIAL = [*UNIQUE_U, ('relationships', 't', 'k'), ('not_null', 't', 'k')]


@pytest.mark.parametrize(
    ('sql', 'tests', 'op', 'factor', 'filters'),
    [
        ('select amount from t', [], 'sum', 1.0, []),
        ('select amount * 2 as a from t', [], 'sum', 2.0, []),
        ('select -amount as a from t', [], 'sum', -1.0, []),
        ('select coalesce(amount, 0) as a from t', [], 'sum', 1.0, []),
        ('select cast(amount as double) as a from t', [], 'sum', 1.0, []),
        ('select amount / 100 as a from t', [], 'sum', 0.01, []),
        ('select k, sum(amount) as a from t group by k', [], 'sum', 1.0, []),
        ('select sum(amount) as a from t', [], 'sum', 1.0, []),
        ('select amount as a from t where m = 1', [], 'sum', 1.0, ['m = 1']),
        ("select sum(case when m = 'x' then amount else 0 end) as a from t", [], 'sum', 1.0, ["m = 'x'"]),
        ("select sum(if(m = 'x', amount, null)) as a from t", [], 'sum', 1.0, ["m = 'x'"]),
        ('select count(*) as a from t', [], 'count', 1.0, []),
        ('select count(t.amount) as a from t full join u on u.k = t.k', UNIQUE_U, 'count', 1.0, []),
        ('select amount as a, (select max(z) from u) as top from t', [], 'sum', 1.0, []),
        ('select k, count(amount) as a from t group by k', [], 'count', 1.0, []),
        (
            'with g as (select k, sum(amount) as s from t group by k) select sum(s) as a from g',
            [],
            'sum',
            1.0,
            [],
        ),
        (
            'with g as (select k, count(*) as c from t group by k) select sum(c) as a from g',
            [],
            'count',
            1.0,
            [],
        ),
        (
            'with g as (select amount, m from t where k > 0) select sum(amount) as a from g where m = 1',
            [],
            'sum',
            1.0,
            ['k > 0', 'm = 1'],
        ),
        ('select t.amount as a from t left join u on u.k = t.k', UNIQUE_U, 'sum', 1.0, []),
        ('select t.amount as a from t left join u on u.k = t.k and u.z = 1', UNIQUE_U, 'sum', 1.0, []),
        ('select t.amount as a from u right join t on t.k = u.k', UNIQUE_U, 'sum', 1.0, []),
        ('select t.amount as a from t full join u on u.k = t.k', UNIQUE_U, 'sum', 1.0, []),
        ('select t.amount as a from t join u on u.k = t.k', REFERENTIAL, 'sum', 1.0, []),
        (
            'select t.amount as a from t left join (select k, count(*) as n from u group by k) g on g.k = t.k',
            [],
            'sum',
            1.0,
            [],
        ),
        (
            'select t.amount as a from t left join (select distinct k from u) g on g.k = t.k',
            [],
            'sum',
            1.0,
            [],
        ),
        (
            'select t.amount as a from t left join (select k from u where z > 0) g on g.k = t.k',
            UNIQUE_U,
            'sum',
            1.0,
            [],
        ),
        # Deduplicated on the key the join reads.
        (
            'select t.amount as a from t left join '
            '(select k, z from u qualify row_number() over (partition by k order by z) = 1) g on g.k = t.k',
            [],
            'sum',
            1.0,
            [],
        ),
        (
            'select t.amount as a from t left join (select k, row_number() over (partition by k order by z) '
            'as rn from u qualify rn = 1) g on g.k = t.k',
            [],
            'sum',
            1.0,
            [],
        ),
        (
            'select t.amount as a from t left join (select distinct on (k) k, z from u order by k, z) g on g.k = t.k',
            [],
            'sum',
            1.0,
            [],
        ),
        # Composite keys.
        (
            'select t.amount as a from t left join u on u.k = t.k and u.z = t.f',
            [('unique_combination_of_columns', 'u', 'k,z')],
            'sum',
            1.0,
            [],
        ),
        (
            'select t.amount as a from t left join u on u.k = t.k and u.z = t.f',
            [('primary_key', 'u', 'k,z')],
            'sum',
            1.0,
            [],
        ),
    ],
)
def test_a_step_is_read_from_sql_that_keeps_the_total(
    sql: str, tests: list[tuple[str, str, str]], op: str, factor: float, filters: list[str]
) -> None:
    flows, _ = _read(sql, tests)
    flow = flows['a'] if 'a' in flows else flows['amount']
    assert isinstance(flow, _Flow), flow
    assert (flow.op, flow.path.base) == (op, 'db.main.t')
    assert flow.factor == pytest.approx(factor)
    assert [f.sql(dialect='duckdb') for f in flow.path.filters] == filters


@pytest.mark.parametrize(
    ('sql', 'tests', 'reason'),
    [
        ('select coalesce(amount, 1) as a from t', [], 'COALESCE'),
        ('select cast(amount as integer) as a from t', [], 'can round'),
        ('select cast(amount as decimal(10, 2)) as a from t', [], 'can round'),
        ('select amount * f as a from t', [], 'product of two columns'),
        ('select amount / f as a from t', [], 'division by a column'),
        ('select amount + f as a from t', [], 'computed with ADD'),
        ('select round(amount) as a from t', [], 'computed with ROUND'),
        ('select sum(distinct amount) as a from t', [], 'DISTINCT'),
        ('select count(distinct amount) as a from t', [], 'DISTINCT'),
        ('select count(amount + 1) as a from t', [], 'COUNT of a computed value'),
        ('select sum(amount * f) as a from t', [], 'product of two columns'),
        ('select sum(abs(amount)) as a from t', [], 'SUM of a computed value'),
        ('select avg(amount) as a from t', [], 'AVG'),
        ('select max(amount) as a from t', [], 'MAX'),
        (
            'select case when sum(amount) > 0 then sum(amount) else 0 end as a from t',
            [],
            'condition on an aggregate',
        ),
        ("select case when m = 'x' then amount when m = 'y' then f else 0 end as a from t", [], 'CASE'),
        ("select if(m = 'x', amount, f) as a from t", [], 'IF'),
        ('select amount as a, k from t group by amount, k', [], 'grouping column'),
        ('select sum(amount) over (order by d) as a from t', [], 'window'),
        ('select distinct amount as a from t', [], 'DISTINCT'),
        ('select amount as a from t limit 5', [], 'LIMIT'),
        ('select k, sum(amount) as a from t group by k having count(*) > 1', [], 'HAVING'),
        ('select amount as a from t qualify row_number() over (partition by k) = 1', [], 'QUALIFY'),
        ('select k, sum(amount) as a from t group by rollup (k)', [], 'ROLLUP'),
        ('select amount as a from t where k in (select k from u)', [], 'subquery'),
        ('select amount as a from t union select z from u', [], 'set operation'),
        ('select amount as a from t except select z from u', [], 'set operation'),
        ('select amount as a from t union all by name select z as a from u', [], 'set operation'),
        ('select amount as a from t union all select z from u limit 3', [], 'LIMIT'),
        ("select amount as a from t; copy (select 42) to 'out.csv'", [], 'the model is 2 statements'),
        ('select cast(amount as decimal) as a from t', [], 'can round'),
        # Set-returning functions in the SELECT repeat each row.
        ('select k, unnest([1, 2]) as v, amount as a from t', [], 'set-returning'),
        ('select k, generate_series(1, 3) as v, amount as a from t', [], 'set-returning'),
        ('select k, explode([1, 2]) as v, amount as a from t', [], 'set-returning'),
        ('select k, json_array_elements(m) as v, amount as a from t', [], 'set-returning'),
        ('select k, jsonb_array_elements(m) as v, amount as a from t', [], 'set-returning'),
        ('select k, jsonb_each(m) as v, amount as a from t', [], 'set-returning'),
        ("select k, string_to_table(m, ',') as v, amount as a from t", [], 'set-returning'),
        (
            'select k, sum(amount) as a from (select k, unnest([1, 2]) as v, amount from t) group by k',
            [],
            'set-returning',
        ),
        # A set-returning function in a joined subquery repeats its keys, grouped or not.
        (
            'select t.amount as a from t left join (select k, unnest([1, 2]) as v from u) g on g.k = t.k',
            UNIQUE_U,
            'no uniqueness test',
        ),
        (
            'select t.amount as a from t left join (select k, unnest([1, 2]) as v from u group by k) g on g.k = t.k',
            [],
            'no uniqueness test',
        ),
        (
            'select t.amount as a from t left join (select max(k) as k, unnest([1, 2]) as v from u) g on g.k = t.k',
            [],
            'no uniqueness test',
        ),
        # A scalar subquery aggregates rows of its own, not the query's.
        (
            'select t.amount as a from t left join (select k, (select max(z) from u) as m from u) g on g.k = t.k',
            [],
            'no uniqueness test',
        ),
        # A joined query that drops rows cannot be the side every row matches.
        (
            'select t.amount as a from t join (select k from u offset 1) g on g.k = t.k',
            REFERENTIAL,
            'drops rows of t',
        ),
        (
            'select t.amount as a from t join (select k from u tablesample 50%) g on g.k = t.k',
            REFERENTIAL,
            'drops rows of t',
        ),
        # An UNPIVOT repeats each row for each column it unpivots and drops NULLs.
        (
            'select t.amount as a from t left join u unpivot (v for c in (z, w)) on u.k = t.k',
            UNIQUE_U,
            'PIVOT',
        ),
        (
            'select t.amount as a from t join u unpivot (v for c in (z, w)) on u.k = t.k',
            REFERENTIAL,
            'PIVOT',
        ),
        ('select t.amount as a from t unpivot (v for c in (k, f))', [], 'PIVOT'),
        ('select val as a from t unpivot (val for col in (amount, f))', [], 'PIVOT'),
        ('select cast(amount as float) as a from t', [], 'can round'),
        ('select cast(amount as real) as a from t', [], 'can round'),
        # A sample keeps some rows.
        ('select amount as a from t using sample 10%', [], 'sample'),
        ('select amount as a from t tablesample 10%', [], 'sample'),
        ('select t.amount as a from t left join u tablesample 10% on u.k = t.k', UNIQUE_U, 'sample'),
        # A filter on the clock or a random number selects other rows on each run.
        ('select amount as a from t where d > current_date', [], 'changes between runs'),
        ("select amount as a from t where d > now() - interval '1 day'", [], 'changes between runs'),
        (
            'select sum(case when random() > 0.5 then amount else 0 end) as a from t',
            [],
            'changes between runs',
        ),
        # A DISTINCT * over a table the graph knows only some columns of keys none of them.
        (
            'select t.amount as a from t left join (select distinct * from v) g on g.k = t.k',
            [],
            'no uniqueness test',
        ),
        (
            'select t.amount as a from t left join (select distinct k, * from v) g on g.k = t.k',
            [],
            'no uniqueness test',
        ),
        ('select count(*) as a from t full join u on u.k = t.k', UNIQUE_U, 'unmatched rows of u'),
        ('select count(*) as a from t right join u on u.k = t.k', REFERENTIAL, 'unmatched rows of u'),
        ('select t.k as b from t full join u on u.k = t.k', UNIQUE_U, 'unmatched rows of u'),
        ('select t.k as b from t right join u on u.k = t.k', REFERENTIAL, 'unmatched rows of u'),
        (
            'select t.k as b from t left join u on u.k = t.k full join (select distinct k from u) v on v.k = u.k',
            UNIQUE_U,
            'unmatched rows of u',
        ),
        (
            'select t.amount as a from t left join '
            '(select k, z from u qualify rank() over (partition by k order by z) = 1) g on g.k = t.k',
            [],
            'no uniqueness test',
        ),
        (
            'select t.amount as a from t left join (select distinct on (z) k, z from u) g on g.k = t.k',
            [],
            'no uniqueness test',
        ),
        (
            'select t.amount as a from t left join u on u.k = t.k',
            [('unique_combination_of_columns', 'u', 'k,z')],
            'no uniqueness test',
        ),
        (
            'select t.amount as a from t left join u on u.k = t.k',
            [('primary_key', 'u', 'k,z')],
            'no uniqueness test',
        ),
        (
            'with g as (select amount from t union all select z from u) select amount as a from g',
            [],
            'set operation',
        ),
        ('select t.amount as a from t join u on u.k = t.k', [], 'no uniqueness test'),
        ('select t.amount as a from t left join u on u.k = t.k', [], 'no uniqueness test'),
        ('select t.amount as a from t join u on u.k = t.k', UNIQUE_U, 'drops rows of t'),
        ('select t.amount as a from u left join t on t.k = u.k', UNIQUE_U, 'drops rows of t'),
        ('select t.amount as a from t join u on u.k = t.k and u.z = 1', REFERENTIAL, 'drops rows'),
        (
            'select t.amount as a from t left join u on u.k = t.k where u.z = 1',
            UNIQUE_U,
            'WHERE condition on u',
        ),
        ('select t.amount as a from t cross join u', [], 'CROSS JOIN'),
        ('select t.amount as a from t semi join u on u.k = t.k', [], 'SEMI JOIN'),
        ('select t.amount as a from t left join u on u.k > t.k', UNIQUE_U, 'other than equal keys'),
        ('select t.amount as a from t, unnest([1, 2]) as x(v)', [], 'UNNEST'),
        ('select t.amount as a from t left join (select k from u) g on g.k = t.k', [], 'no uniqueness test'),
        (
            'with g as (select k, sum(amount) as s from t group by k) select sum(s) as a from g where s > 10',
            [],
            'filter on a computed column',
        ),
        ('select amount as a from (select amount, sum(f) over () as s from t where s > 0)', [], 'aggregate'),
        (
            'with g as (select k, count(*) as c from t group by k) select count(*) as a from g',
            [],
            'already grouped',
        ),
        (
            'with g as (select k, amount + 1 as v from t) select count(v) as a from g',
            [],
            'COUNT of a computed column',
        ),
    ],
)
def test_sql_that_can_repeat_or_drop_rows_has_no_step(
    sql: str, tests: list[tuple[str, str, str]], reason: str
) -> None:
    flows, rows = _read(sql, tests)
    flow = flows.get('a', rows)
    assert isinstance(flow, str) and reason in flow, flow


def test_integer_division_keeps_a_total_only_where_the_dialect_divides_exactly() -> None:
    flows, _ = _read('select amount / 100 as a from t', dialect='postgres')
    assert isinstance(flows['a'], str) and 'integer division' in flows['a']
    flows, _ = _read('select amount / 100.0 as a from t', dialect='postgres')
    assert isinstance(flows['a'], _Flow) and flows['a'].factor == pytest.approx(0.01)


@pytest.mark.parametrize(
    ('dialect', 'sql'),
    [
        ('postgres', 'select k, generate_series(1, 3) as v, amount as a from t'),
        ('postgres', 'select k, unnest(array[1, 2]) as v, amount as a from t'),
        ('postgres', 'select k, json_array_elements(m::json) as v, amount as a from t'),
        ('postgres', 'select k, jsonb_each(m::jsonb) as v, amount as a from t'),
        ('postgres', "select k, regexp_matches(m, 'a', 'g') as v, amount as a from t"),
        ('databricks', 'select k, explode(array(1, 2)) as v, amount as a from t'),
        ('databricks', 'select k, explode_outer(array(1, 2)) as v, amount as a from t'),
        ('databricks', 'select k, posexplode(array(1, 2)) as v, amount as a from t'),
        ('databricks', "select k, inline(array(struct(1, 'a'))) as v, amount as a from t"),
        ('snowflake', 'select amount as a from t sample (10)'),
        ('databricks', 'select k, stack(2, 1, 2) as v, amount as a from t'),
        ('postgres', 'select amount as a from t tablesample bernoulli (10)'),
    ],
)
def test_set_returning_functions_and_samples_have_no_step_in_each_dialect(dialect: str, sql: str) -> None:
    flows, rows = _read(sql, dialect=dialect)
    assert isinstance(flows['a'], str) and isinstance(rows, str), (flows['a'], rows)
    assert 'set-returning' in rows or 'sample' in rows


def test_a_primary_key_is_a_key_only_where_the_warehouse_enforces_it() -> None:
    sql = 'select t.amount as a from t left join u on u.k = t.k and u.z = t.f'
    for dialect in ('snowflake', 'bigquery', 'redshift', 'databricks'):
        flows, _ = _read(sql, [('primary_key', 'u', 'k,z')], dialect=dialect)
        assert isinstance(flows['a'], str) and 'no uniqueness test' in flows['a'], dialect
    # A dbt constraint is enforced only under an enforced contract.
    graph = _graph([('primary_key', 'u', 'k,z')])
    u = graph.nodes['table.db.main.u']
    u.origins = ['dbt']
    assert _Tests(graph).key(u.id, {'k', 'z'}) is None
    u.attrs['primary_key_enforced'] = True
    assert _Tests(graph).key(u.id, {'k', 'z'}) == 'table.db.main.u:primary_key'


def test_a_cast_to_decimal_keeps_a_total_only_where_decimal_has_no_default_scale() -> None:
    flows, _ = _read('select cast(amount as decimal) as a from t', dialect='postgres')
    assert isinstance(flows['a'], _Flow)
    for dialect in ('duckdb', 'databricks', 'snowflake'):
        flows, _ = _read('select cast(amount as decimal) as a from t', dialect=dialect)
        assert isinstance(flows['a'], str) and 'can round' in flows['a'], dialect


def test_a_union_all_reads_each_branch() -> None:
    flows, rows = _branches("select amount as a, m from t union all select -z, 'u' from u where w > 0")
    branches = flows['a']
    assert isinstance(branches, list)
    assert [(f.path.base, f.factor) for f in branches] == [('db.main.t', 1.0), ('db.main.u', -1.0)]
    assert [f.sql() for f in branches[1].path.filters] == ['w > 0']
    assert isinstance(rows, list) and [r.base for r in rows] == ['db.main.t', 'db.main.u']
    flows, _ = _branches('select sum(amount) as a from t union all select count(*) from u')
    assert flows['a'] == 'a UNION ALL that sums in one branch and counts in another'
    flows, rows = _branches('select * from t union all select k from u')
    assert flows == {} and isinstance(rows, str) and 'column by column' in rows


def test_rows_keep_one_row_per_input_row_or_are_grouped() -> None:
    _, rows = _read('select t.k, t.amount from t left join u on u.k = t.k', UNIQUE_U)
    assert isinstance(rows, _Path) and rows.mode == 'map' and rows.base == 'db.main.t'
    assert set(rows.carried or {}) == {'k', 'amount'}
    _, rows = _read('select k, sum(amount) from t group by k')
    assert isinstance(rows, _Path) and rows.mode == 'agg'
    _, rows = _read('select k from t union select k from u')
    assert rows == 'a set operation (UNION, INTERSECT or EXCEPT)'
    flows, rows = _read('select from where')
    assert flows == {} and isinstance(rows, str) and 'could not be read' in rows


# Scale: reading the SQL of a large generated project


@pytest.mark.scale
@pytest.mark.parametrize('tested', [False, True], ids=['untested joins', 'every join tested'])
def test_laws_on_a_large_project(tmp_path: Path, tested: bool) -> None:
    """``KONSTRUCT_SCALE_MODELS`` sets the size (300 by default). With ``tested``, every model has a
    uniqueness test on the key it is joined on, so every model's SQL is read in full rather than
    stopped at an untested join. The CPU bound is the start-up, measured on a project of one model
    (which also warms the parser), and a cost per model with room for slow runners: untested joins
    cost about 0.2 ms a model at 300 and 1000 models on a quiet machine and 1 ms on a hosted
    runner, and the bound is 2 ms."""
    from .test_scale import make_project

    one = tmp_path / 'one'
    make_project(one, models=1, columns_per_model=20)
    small = Project(one)
    small.build(run_dbt=False, warehouses=False)
    startup = time.process_time()
    propose(small.graph, one)
    startup = time.process_time() - startup
    models = int(os.environ.get('KONSTRUCT_SCALE_MODELS', '300'))
    tmp_path = tmp_path / 'big'
    make_project(tmp_path, models=models, columns_per_model=20)
    if tested:
        path = tmp_path / 'target' / 'manifest.json'
        manifest = json.loads(path.read_text())
        for index in range(models):
            name = f'm{index:03}'
            manifest['nodes'][f'test.big.unique_{name}_c00'] = {
                'unique_id': f'test.big.unique_{name}_c00',
                'resource_type': 'test',
                'name': f'unique_{name}_c00',
                'package_name': 'big',
                'test_metadata': {'name': 'unique', 'kwargs': {'column_name': 'c00'}},
                'column_name': 'c00',
                'attached_node': f'model.big.{name}',
                'depends_on': {'nodes': [f'model.big.{name}'], 'macros': []},
            }
        path.write_text(json.dumps(manifest))
    project = Project(tmp_path)
    project.build(run_dbt=False, warehouses=False)
    graph = project.graph
    started, cpu = time.perf_counter(), time.process_time()
    laws = propose(graph, tmp_path)
    wall, cpu = time.perf_counter() - started, time.process_time() - cpu
    assert laws.totals()['laws'] >= 10
    bound = startup + models * (0.015 if tested else 0.002)
    assert cpu < bound, f'propose took {cpu:.2f}s of CPU, more than {bound:.2f}s'
    started = time.perf_counter()
    head = propose(graph, tmp_path, models=laws.models())
    gate = time.perf_counter() - started
    assert violations(graph, laws, head) == []
    print(
        f'\n{models} models ({"every join tested" if tested else "untested joins"}): propose {wall:.2f}s '
        f'({cpu:.2f}s CPU; {startup:.2f}s on one model), {len(laws.laws)} laws, {laws.totals()["blocked"]} measures blocked; '
        f'propose for the gate on {len(laws.models())} models {gate:.2f}s'
    )


@pytest.mark.scale
def test_confirming_laws_on_a_large_warehouse(tmp_path: Path) -> None:
    """A tree of ``KONSTRUCT_SCALE_MODELS`` views (300 by default) over one payments table, each
    mapping its parent row for row with a unit conversion: every model has a conservation and a
    count law, chained back to the source and confirmed per day."""
    models = int(os.environ.get('KONSTRUCT_SCALE_MODELS', '300'))
    tree = {
        f'm{i:04}': (
            f'select payment_id, paid_at, amount * 2 as amount from '
            f'{raw("payments") if i == 0 else rel(f"m{(i - 1) // 2:04}")}'
        )
        for i in range(models)
    }
    payments = (
        "select i as payment_id, timestamp '2026-01-01' + to_hours(i % 2000) as paid_at, "
        '(i * 37) % 1000 as amount from range(1, 20001) t(i)'
    )
    write_shop(tmp_path, tree, [], {**RAW, 'payments': payments})
    project = Project(tmp_path)
    project.build(run_dbt=False)
    started, cpu = time.perf_counter(), time.process_time()
    laws = propose(project.graph, tmp_path)
    proposing, proposing_cpu = time.perf_counter() - started, time.process_time() - cpu
    started, cpu = time.perf_counter(), time.process_time()
    checked = confirm(project.graph, tmp_path, tmp_path / 'shop.duckdb', laws)
    confirming, confirming_cpu = time.perf_counter() - started, time.process_time() - cpu
    totals = checked.totals()
    assert totals['laws'] == totals['holds'] == 2 * models
    assert max(law.checked for law in checked.laws) > 80
    print(
        f'\n{models} models, {totals["laws"]} laws: propose {proposing:.2f}s ({proposing_cpu:.2f}s CPU), '
        f'confirm {confirming:.2f}s ({confirming_cpu:.2f}s CPU)'
    )
