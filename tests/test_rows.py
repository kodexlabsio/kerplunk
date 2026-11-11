"""Whether a formula composed down to source columns still counts the rows its column does."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

from konstruct import Project
from konstruct.core.graph import Graph, Node
from konstruct.interfaces.cli import main
from konstruct.semantics.rows import Rows

MODELS = """\
create table main.crossed as select o.id as order_id, o.amount_cents, r.name from raw.orders o
cross join raw.regions r;
create table main.fanned as select o.id as order_id, o.amount_cents, p.method from raw.orders o
left join raw.payments p on p.order_id = o.id;
create table main.looked_up as select o.id as order_id, o.amount_cents, c.name from raw.orders o
left join raw.customers c on o.customer_id = c.customer_id;
create table main.matched as select o.id as order_id, o.amount_cents, c.name from raw.orders o
join raw.customers c on o.customer_id = c.customer_id;
create table main.kept as select id as order_id, amount_cents from raw.orders;
create table main.paid as select c.customer_id, p.amount_cents from raw.customers c
left join raw.payments p on p.customer_id = c.customer_id;
create table main.through as with cp as (
    select o.customer_id, p.amount_cents from raw.payments p left join raw.orders o on p.order_id = o.id
) select c.customer_id, cp.amount_cents from raw.customers c left join cp on cp.customer_id = c.customer_id;
create table main.selfed as select o.id, o.amount_cents from raw.orders o join raw.orders p on p.parent_id = o.id;
create table main.deduplicated as select amount_cents from raw.orders union select amount_cents from raw.refunds;
create table main.intersected as select amount_cents from raw.orders intersect select amount_cents from raw.refunds;
create table main.unnested as select p.order_id, p.amount_cents, t.x from raw.payments p
cross join unnest([1, 2, 3]) as t(x);
create table main.series as select p.order_id, p.amount_cents from raw.payments p, generate_series(1, 3) g;
create table main.lateraled as select p.order_id, p.amount_cents, l.y from raw.payments p,
lateral (select p.order_id + 1 as y) l;
create table main.both_sides as select o.id, o.amount_cents as order_cents, p.amount_cents,
    o.amount_cents + p.amount_cents as combined from raw.orders o
left join raw.payments p on p.order_id = o.id;
create table main.excepted as select amount_cents from raw.orders except select amount_cents from raw.refunds;
create table main.paid_again as select c.customer_id, p.amount_cents, (select count(*) from raw.payments x) as n
from raw.customers c left join raw.payments p on p.customer_id = c.customer_id;
"""


@pytest.fixture
def graph(tmp_path: Path) -> Graph:
    (tmp_path / 'models.sql').write_text(MODELS)
    assert main(['build', str(tmp_path), '--no-warehouse', '--no-dbt', '--dialect', 'duckdb', '-q']) == 0
    g = Project(tmp_path).graph
    _test(g, 'unique', 'table.raw.customers#customer_id')
    return g


def _test(graph: Graph, kind: str, column: str, target: str | None = None) -> None:
    test = graph.add_node(Node(f'test.shop.{kind}_{column}', 'test', kind, attrs={'test': kind}))
    graph.add_edge(test.id, column, 'tests')
    if target is not None:
        graph.add_edge(test.id, target, 'tests', role='relationship target')


def _changed(graph: Graph, relation: str) -> str | None:
    return Rows(graph).changed(graph.nodes[f'table.main.{relation}#amount_cents'])


def test_a_relation_that_reads_one_table_keeps_its_rows(graph: Graph) -> None:
    assert _changed(graph, 'kept') is None


def test_a_cross_join_changes_the_rows(graph: Graph) -> None:
    # SUM(raw_orders.amount_cents) counts each order once; the model counts it once per region.
    assert _changed(graph, 'crossed') == 'regions read in main.crossed with no join condition on a unique key'


def test_a_join_that_can_match_several_rows_changes_them(graph: Graph) -> None:
    assert _changed(graph, 'fanned') == (
        'a join in main.fanned to payments.order_id, which is not known to be unique'
    )


def test_a_left_join_to_a_unique_key_keeps_the_rows(graph: Graph) -> None:
    assert _changed(graph, 'looked_up') is None


def test_an_inner_join_keeps_the_rows_only_when_tests_vouch_for_the_foreign_key(graph: Graph) -> None:
    assert _changed(graph, 'matched') == (
        'an inner join in main.matched on orders.customer_id, which no not_null and relationships tests '
        'vouch for'
    )
    _test(graph, 'not_null', 'table.raw.orders#customer_id')
    assert _changed(graph, 'matched') is not None  # not_null alone: an order may name no customer
    _test(graph, 'relationships', 'table.raw.orders#customer_id', 'table.raw.customers#customer_id')
    assert _changed(graph, 'matched') is None


def test_the_ossie_export_leaves_out_what_a_join_would_miscount(tmp_path: Path) -> None:
    (tmp_path / 'models.sql').write_text(
        MODELS
        + 'create table main.crossed_total as select sum(amount_cents) as total from main.crossed;\n'
        + 'create table main.fanned_total as select sum(amount_cents) as total from main.fanned;\n'
        + 'create table main.looked_up_total as select sum(amount_cents) as total from main.looked_up;\n'
    )
    (tmp_path / 'definitions.yml').write_text(
        textwrap.dedent(
            """\
            version: 1
            terms:
              - {name: Crossed total, kind: measure, definition: x, columns: [main.crossed_total.total]}
              - {name: Fanned total, kind: measure, definition: y, columns: [main.fanned_total.total]}
              - {name: Looked up total, kind: measure, definition: z, columns: [main.looked_up_total.total]}
            """
        )
    )
    assert main(['build', str(tmp_path), '--no-warehouse', '--no-dbt', '--dialect', 'duckdb', '-q']) == 0
    project = Project(tmp_path)
    # customers.customer_id is not known to be unique here, so even the left join is left out.
    with pytest.raises(ValueError, match='nothing to export: ') as error:
        project.export('ossie')
    assert "measure 'Crossed total': its composed formula would not count the rows it does" in str(
        error.value
    )
    assert 'a join in main.fanned to payments.order_id' in str(error.value)


def test_rows_on_the_nullable_side_of_an_outer_join_reach_it_only_where_they_match(graph: Graph) -> None:
    # Payments with no customer are not in main.paid, but SUM(raw_payments.amount_cents) counts them.
    assert _changed(graph, 'paid') == (
        'an outer join in main.paid that keeps payments.customer_id only where it matches, which no '
        'not_null and relationships tests vouch for'
    )
    _test(graph, 'not_null', 'table.raw.payments#customer_id')
    _test(graph, 'relationships', 'table.raw.payments#customer_id', 'table.raw.customers#customer_id')
    assert _changed(graph, 'paid') is None


def test_rows_joined_through_the_nullable_side_must_match_all_the_way(graph: Graph) -> None:
    _test(graph, 'unique', 'table.raw.orders#id')
    # A payment reaches main.through only through its order's customer.
    assert 'keeps payments.order_id only where it matches' in str(_changed(graph, 'through'))
    _test(graph, 'not_null', 'table.raw.payments#order_id')
    _test(graph, 'relationships', 'table.raw.payments#order_id', 'table.raw.orders#id')
    assert 'keeps orders.customer_id only where it matches' in str(_changed(graph, 'through'))
    _test(graph, 'not_null', 'table.raw.orders#customer_id')
    _test(graph, 'relationships', 'table.raw.orders#customer_id', 'table.raw.customers#customer_id')
    assert _changed(graph, 'through') is None


def test_a_reading_on_the_nullable_side_is_checked_as_a_relation_on_it(graph: Graph) -> None:
    """main.paid_again reads payments twice: the reading it passes on is on the nullable side of
    the left join, which the relation records by its reading, and the other is not."""
    assert 'an outer join in main.paid_again that keeps payments.customer_id only where it matches' in str(
        _changed(graph, 'paid_again')
    )


def test_a_self_join_is_checked(graph: Graph) -> None:
    assert _changed(graph, 'selfed') == 'a self join in main.selfed on orders.parent_id = orders.id'


def test_set_operations_that_remove_rows_change_them(graph: Graph) -> None:
    rows = Rows(graph)
    for relation, label in (('deduplicated', 'UNION'), ('intersected', 'INTERSECT'), ('excepted', 'EXCEPT')):
        assert (
            rows.changed(graph.nodes[f'table.main.{relation}#amount_cents']) == f'{label} in main.{relation}'
        )


def test_a_union_all_is_no_metric(tmp_path: Path) -> None:
    (tmp_path / 'models.sql').write_text(
        'create table main.all_orders as select amount from raw.web_orders union all select amount from raw.shop_orders;\n'
        'create table main.total as select sum(amount) as total from main.all_orders;\n'
    )
    (tmp_path / 'definitions.yml').write_text(
        'version: 1\nterms:\n  - {name: Total, kind: measure, definition: x, columns: [main.total.total]}\n'
    )
    assert main(['build', str(tmp_path), '--no-warehouse', '--no-dbt', '--dialect', 'duckdb', '-q']) == 0
    # SUM(UNION(web_orders.amount, shop_orders.amount)) is no SQL.
    with pytest.raises(ValueError, match=re.escape('it combines the rows of several queries (UNION)')):
        Project(tmp_path).export('ossie')


def test_unnest_table_functions_and_lateral_change_the_rows(graph: Graph) -> None:
    # Each payment appears three times: SUM(raw_payments.amount_cents) is a third of the model's.
    assert _changed(graph, 'unnested') == 'ROWS FROM in main.unnested'
    assert _changed(graph, 'series') == 'ROWS FROM in main.series'
    assert _changed(graph, 'lateraled') == 'ROWS FROM in main.lateraled'


def test_a_measure_that_reads_both_sides_of_a_join_is_checked(graph: Graph) -> None:
    # Each order appears once per payment, so orders.amount_cents is counted once per payment.
    column = graph.nodes['table.main.both_sides#order_cents']
    assert Rows(graph).changed(column) == (
        'a join in main.both_sides to payments.order_id, which is not known to be unique'
    )
    # A column that reads both sides is checked both ways: each order repeats once per payment,
    # and a payment with no order is dropped.
    combined = graph.nodes['table.main.both_sides#combined']
    assert str(Rows(graph).changed(combined)).endswith('which is not known to be unique')
    _test(graph, 'unique', 'table.raw.orders#id')
    _test(graph, 'unique', 'table.raw.payments#order_id')
    assert 'keeps payments.order_id only where it matches' in str(Rows(graph).changed(combined))
    _test(graph, 'not_null', 'table.raw.payments#order_id')
    _test(graph, 'relationships', 'table.raw.payments#order_id', 'table.raw.orders#id')
    assert Rows(graph).changed(combined) is None


def test_a_snowflake_table_function_changes_the_rows(tmp_path: Path) -> None:
    (tmp_path / 'models.sql').write_text(
        'create table main.flattened as select o.id, o.amount as v from raw.orders o,\n'
        'table(flatten(input => o.tags)) f;\n'
        'create table main.generated as select o.id, o.amount as v from raw.orders o,\n'
        'table(generator(rowcount => 3)) g;\n'
    )
    assert main(['build', str(tmp_path), '--no-warehouse', '--no-dbt', '--dialect', 'snowflake', '-q']) == 0
    g = Project(tmp_path).graph
    rows = Rows(g)
    for relation in ('flattened', 'generated'):
        assert rows.changed(g.nodes[f'table.main.{relation}#v']) == f'ROWS FROM in main.{relation}'
