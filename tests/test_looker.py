"""Looker semantic models (LookML) as exposures of the tables and columns they read."""

from __future__ import annotations

import textwrap
from pathlib import Path

from konstruct import Project
from konstruct.looker import parse, resolve
from konstruct.status import inspect

VIEW = textwrap.dedent(
    """\
    view: orders {
      sql_table_name: main.orders ;;
      label: "Orders"
      dimension: id { primary_key: yes sql: ${TABLE}.id ;; }
      dimension: amount { type: number sql: ${TABLE}.amount_cents / 100.0 ;; }
      dimension: status {}
      dimension_group: placed { type: time timeframes: [date, raw] sql: ${TABLE}."placed_at" ;; }
      dimension: is_recent { type: yesno sql: ${placed_date} > current_date - 7 ;; }
      dimension: doubled { type: number sql: ${orders.amount} * 2 ;; }
      dimension_group: wait { type: duration intervals: [day] sql_start: ${TABLE}.placed_at ;; sql_end: ${TABLE}.shipped_at ;; }
      dimension: spot { type: location sql_latitude: ${TABLE}.lat ;; sql_longitude: ${TABLE}.lon ;; }
      measure: revenue { type: sum sql: ${amount} ;; }
      measure: completed { type: count filters: [status: "complete"] }
      measure: loop_a { type: number sql: ${loop_b} ;; }
      measure: loop_b { type: number sql: ${loop_a} ;; }
      dimension: ghost { sql: ${TABLE}.nowhere ;; }
    }

    view: big_orders {
      derived_table: { sql: select id from main.orders where amount_cents > 1000 ;; }
      dimension: id {}
    }

    view: dynamic {
      sql_table_name: {% parameter schema %}.orders ;;
    }

    view: customers {
      dimension: name {}
    }
    """
)

REFINED = textwrap.dedent(
    """\
    view: +orders {
      dimension: channel { sql: ${TABLE}.channel ;; }
    }

    view: base_order {
      extension: required
      sql_table_name: main.orders ;;
      dimension: id { sql: ${TABLE}.id ;; }
    }

    view: order_lines {
      extends: [base_order]
      dimension: sku { sql: ${TABLE}.sku ;; }
    }
    """
)


def views(*texts: str) -> dict[str, object]:
    found, problems = resolve([parse(text, f'f{i}.lkml') for i, text in enumerate(texts)], 'duckdb')
    return {'views': {v.name: v for v in found}, 'problems': problems}


def test_views_their_tables_and_the_columns_each_field_reads() -> None:
    result = views(VIEW)
    by_name = result['views']
    assert isinstance(by_name, dict)
    orders = by_name['orders']
    assert (orders.table, orders.label) == ('main.orders', 'Orders')
    fields = {f.name: f.columns for f in orders.fields}
    assert fields['amount'] == ['amount_cents']
    assert fields['revenue'] == ['amount_cents']
    assert fields['status'] == ['status']
    assert fields['placed'] == ['placed_at']
    assert fields['is_recent'] == ['placed_at']
    assert fields['doubled'] == ['amount_cents']
    assert fields['wait'] == ['placed_at', 'shipped_at']
    assert fields['spot'] == ['lat', 'lon']
    assert fields['completed'] == ['status']
    assert fields['loop_a'] == []
    assert by_name['big_orders'].derived_reads == ['main.orders'] and by_name['big_orders'].table is None
    assert by_name['dynamic'].table is None
    assert by_name['customers'].table == 'customers'
    assert result['problems'] == [
        "f0.lkml: view 'dynamic': sql_table_name is built at run time, so it is skipped"
    ]
    assert parse('view: {', 'x.lkml').problems[0].startswith('x.lkml: not valid LookML')


def test_refinements_extends_and_abstract_views() -> None:
    by_name = views(VIEW, REFINED)['views']
    assert isinstance(by_name, dict)
    assert 'base_order' not in by_name and '+orders' not in by_name
    assert {f.name: f.columns for f in by_name['orders'].fields}['channel'] == ['channel']
    lines = by_name['order_lines']
    assert lines.table == 'main.orders'
    assert {f.name: f.columns for f in lines.fields} == {'id': ['id'], 'sku': ['sku']}


def test_merges_keep_extends_and_the_childs_own_table() -> None:
    by_name = views(
        'view: base { sql_table_name: main.base ;; dimension: id {} }\n'
        'view: base2 { extension: required dimension: code {} }\n'
        'view: c { extends: [base] }\n'
        'view: +c { extends: [base2] }\n'
        'view: d { derived_table: { sql: select 1 as x from main.src ;; } }\n'
        'view: e { extends: [d] sql_table_name: main.e ;; }\n'
        'view: t { dimension_group: created { type: time sql: ${TABLE}.created_at ;; } '
        'measure: recent { type: count filters: [created_date: "7 days"] } }\n'
    )['views']
    assert isinstance(by_name, dict)
    assert by_name['c'].table == 'main.base'
    assert sorted(f.name for f in by_name['c'].fields) == ['code', 'id']
    assert (by_name['e'].table, by_name['e'].derived_reads) == ('main.e', [])
    assert {f.name: f.columns for f in by_name['t'].fields}['recent'] == ['created_at']


def _project(root: Path) -> Project:
    (root / 'models.sql').write_text(
        'create table main.orders as select id, amount_cents, status, placed_at, shipped_at, lat, lon, notes '
        'from raw.orders;\n'
    )
    (root / 'views').mkdir()
    (root / 'views' / 'orders.view.lkml').write_text(VIEW)
    return Project(root)


def test_a_view_is_an_exposure_of_the_columns_it_reads(tmp_path: Path) -> None:
    project = _project(tmp_path)
    graph = project.build(dialect='duckdb', warehouses=False, run_dbt=False).graph
    exposure = graph.nodes['looker.view.orders']
    assert (exposure.kind, exposure.path, exposure.attrs['type']) == (
        'exposure',
        'views/orders.view.lkml',
        'looker view',
    )
    impact = project.impact('raw.orders.amount_cents')
    assert {e.label for e in impact.exposures} == {'orders', 'big_orders'}
    fields = next(
        e.attrs['fields']
        for e in graph.in_edges('looker.view.orders', 'feeds')
        if e.source.endswith('#amount_cents')
    )
    assert fields == ['amount', 'doubled', 'revenue']
    # A column no field reads, upstream or not, reaches no Looker view built on its table.
    assert 'orders' not in {e.label for e in project.impact('raw.orders.notes').exposures}
    assert 'orders' not in {e.label for e in project.impact('main.orders.notes').exposures}
    assert any(
        "view 'orders' reads main.orders.nowhere (ghost), which is not a known column" in w
        for w in graph.warnings
    )
    assert {'input': 'looker', 'path': 'views/orders.view.lkml', 'detail': '4 views'} in graph.meta['sources']
    assert inspect(tmp_path).looker_files == ['views/orders.view.lkml']


def test_a_view_over_a_table_the_build_does_not_know_gives_it_columns(tmp_path: Path) -> None:
    (tmp_path / 'v.view.lkml').write_text(
        'view: events { sql_table_name: prod.events ;; dimension: kind {} }\n'
    )
    project = Project(tmp_path)
    graph = project.build(warehouses=False, run_dbt=False).graph
    assert [e.label for e in project.impact('prod.events.kind').exposures] == ['events']
    assert not [w for w in graph.warnings if 'not a known column' in w]


def test_a_table_read_elsewhere_still_grows_the_columns_a_view_names(tmp_path: Path) -> None:
    (tmp_path / 'a.sql').write_text('create table main.a as select id from prod.events')
    (tmp_path / 'v.view.lkml').write_text(
        'view: events { sql_table_name: prod.events ;; dimension: kind {} }\n'
    )
    project = Project(tmp_path)
    graph = project.build(warehouses=False, run_dbt=False).graph
    assert [e.label for e in project.impact('prod.events.kind').exposures] == ['events']
    assert not [w for w in graph.warnings if 'not a known column' in w]


def test_looker_paths_in_konstruct_toml(tmp_path: Path) -> None:
    (tmp_path / 'lookml').mkdir()
    (tmp_path / 'lookml' / 'a.view.lkml').write_text('view: a { sql_table_name: main.a ;; }\n')
    (tmp_path / 'other.view.lkml').write_text('view: b { sql_table_name: main.b ;; }\n')
    (tmp_path / 'konstruct.toml').write_text('[looker]\npaths = ["lookml"]\n')
    graph = Project(tmp_path).build(warehouses=False, run_dbt=False).graph
    assert 'looker.view.a' in graph.nodes and 'looker.view.b' not in graph.nodes
