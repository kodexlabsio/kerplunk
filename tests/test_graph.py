from __future__ import annotations

import itertools
from collections.abc import Callable
from pathlib import Path

from konstruct import _core
from konstruct.analysis.query import search_nodes
from konstruct.core.graph import Graph, Node, column_id, relation_key


def small_graph() -> Graph:
    """raw.payments -> stg_payments -> orders -> exposure, with column lineage and a test."""
    g = Graph()
    raw = g.add_node(Node('source.shop.raw.payments', 'source', 'payments', relation='db.raw.payments'))
    stg = g.add_node(
        Node('model.shop.stg_payments', 'model', 'stg_payments', relation='db.main.stg_payments')
    )
    orders = g.add_node(Node('model.shop.orders', 'model', 'orders', relation='db.main.orders'))
    customers = g.add_node(Node('model.shop.customers', 'model', 'customers', relation='db.main.customers'))
    expo = g.add_node(Node('exposure.shop.dashboard', 'exposure', 'dashboard'))
    test = g.add_node(Node('test.shop.not_null_orders_amount', 'test', 'not_null_orders_amount'))
    dag = g.add_node(Node('dag.nightly', 'dag', 'nightly'))
    task = g.add_node(Node('task.nightly.dbt_run', 'task', 'dbt_run'))

    for rel, cols in [
        (raw, ['id', 'order_id', 'amount']),
        (stg, ['payment_id', 'order_id', 'amount']),
        (orders, ['order_id', 'amount']),
        (customers, ['customer_id', 'lifetime_value']),
    ]:
        for col in cols:
            g.add_column(rel, col)

    g.add_edge(raw.id, stg.id, 'feeds')
    g.add_edge(stg.id, orders.id, 'feeds')
    g.add_edge(orders.id, customers.id, 'feeds')
    g.add_edge(orders.id, expo.id, 'feeds')
    g.add_edge(column_id(stg.id, 'payment_id'), column_id(raw.id, 'id'), 'derived_from', transform='direct')
    g.add_edge(
        column_id(stg.id, 'amount'), column_id(raw.id, 'amount'), 'derived_from', transform='transformed'
    )
    g.add_edge(column_id(stg.id, 'order_id'), column_id(raw.id, 'order_id'), 'derived_from')
    g.add_edge(column_id(orders.id, 'amount'), column_id(stg.id, 'amount'), 'derived_from')
    g.add_edge(column_id(orders.id, 'order_id'), column_id(stg.id, 'order_id'), 'derived_from')
    g.add_edge(column_id(customers.id, 'lifetime_value'), column_id(orders.id, 'amount'), 'derived_from')
    g.add_edge(column_id(orders.id, 'order_id'), customers.id, 'feeds', role='reference')
    g.add_edge(test.id, column_id(orders.id, 'amount'), 'tests')
    g.add_edge(dag.id, task.id, 'runs')
    g.add_edge(task.id, orders.id, 'runs')
    return g


def ids(steps: list) -> set[str]:  # type: ignore[type-arg]
    return {s.node.id for s in steps}


def test_relation_key_normalises_quotes_and_case() -> None:
    assert relation_key('"Jaffle"', 'MAIN', '`Orders`') == 'jaffle.main.orders'
    assert relation_key(None, 'main', 'orders') == 'main.orders'


def test_add_node_merges_attributes_and_origins() -> None:
    g = Graph()
    g.add_node(Node('model.a', 'model', 'a', origins=['dbt']))
    merged = g.add_node(
        Node('model.a', 'model', 'a', description='Docs', relation='db.s.a', origins=['duckdb'])
    )
    assert merged.description == 'Docs'
    assert merged.origins == ['dbt', 'duckdb']
    assert g.node_for_relation('DB.S.A') is merged


def test_add_edge_deduplicates_and_ignores_self_loops() -> None:
    g = Graph()
    g.add_node(Node('a', 'table', 'a'))
    g.add_node(Node('b', 'table', 'b'))
    assert g.add_edge('a', 'b', 'feeds') is g.add_edge('a', 'b', 'feeds')
    assert g.add_edge('a', 'a', 'feeds') is None
    assert len(g.edges) == 1


def test_resolve_columns_relations_and_tasks() -> None:
    g = small_graph()
    assert [n.id for n in g.resolve('orders.amount')] == ['model.shop.orders#amount']
    assert [n.id for n in g.resolve('main.orders.amount')] == ['model.shop.orders#amount']
    assert [n.id for n in g.resolve('db.main.orders.amount')] == ['model.shop.orders#amount']
    assert [n.id for n in g.resolve('ORDERS')] == ['model.shop.orders']
    assert [n.id for n in g.resolve('model.shop.orders')] == ['model.shop.orders']
    assert [n.id for n in g.resolve('dbt_run')] == ['task.nightly.dbt_run']
    assert g.resolve('orders.nope') == []
    assert g.resolve('') == []


def test_resolve_prefers_exact_name_over_suffix() -> None:
    g = Graph()
    g.add_node(Node('model.a.orders', 'model', 'orders', relation='db.main.orders'))
    g.add_node(Node('table.db.other.orders', 'table', 'orders', relation='db.other.orders'))
    assert len(g.resolve('orders')) == 2
    assert [n.id for n in g.resolve('other.orders')] == ['table.db.other.orders']


def test_impact_of_a_column_follows_lineage_tests_and_tasks() -> None:
    g = small_graph()
    steps = g.impact(g.resolve('payments.amount')[0])
    assert ids(steps) == {
        'source.shop.raw.payments#amount',
        'model.shop.stg_payments#amount',
        'model.shop.orders#amount',
        'model.shop.customers#lifetime_value',
        'test.shop.not_null_orders_amount',
        # orders is affected, so its exposure and the task that runs it are too
        'exposure.shop.dashboard',
        'task.nightly.dbt_run',
    }


def test_trace_of_a_column_reports_the_tasks_that_build_its_relations() -> None:
    g = small_graph()
    steps = g.trace(g.resolve('orders.amount')[0])
    assert 'task.nightly.dbt_run' in ids(steps)
    assert 'exposure.shop.dashboard' not in ids(steps)


def test_impact_through_a_join_key_stops_at_the_relation() -> None:
    g = small_graph()
    steps = g.impact(g.resolve('orders.order_id')[0])
    assert ids(steps) == {'model.shop.orders#order_id', 'model.shop.customers'}
    reached = next(s for s in steps if s.node.id == 'model.shop.customers')
    assert reached.via is not None and reached.via.attrs['role'] == 'reference'


def test_impact_of_a_relation_includes_its_columns_and_downstream_relations() -> None:
    g = small_graph()
    found = ids(g.impact(g.resolve('stg_payments')[0]))
    assert {'model.shop.orders', 'model.shop.customers', 'exposure.shop.dashboard'} <= found
    assert 'model.shop.customers#lifetime_value' in found
    assert 'task.nightly.dbt_run' in found
    assert 'dag.nightly' not in found
    assert 'source.shop.raw.payments' not in found


def test_trace_of_a_column_goes_to_the_raw_source() -> None:
    g = small_graph()
    steps = g.trace(g.resolve('customers.lifetime_value')[0])
    assert ids(steps) == {
        'model.shop.customers#lifetime_value',
        'model.shop.orders#amount',
        'model.shop.stg_payments#amount',
        'source.shop.raw.payments#amount',
        'task.nightly.dbt_run',
    }
    depths = {s.node.id: s.depth for s in steps}
    assert depths['source.shop.raw.payments#amount'] == 3


def test_trace_of_a_relation_includes_the_task_that_runs_it() -> None:
    g = small_graph()
    found = ids(g.trace(g.resolve('orders')[0]))
    assert 'task.nightly.dbt_run' in found
    assert 'source.shop.raw.payments' in found


def test_max_depth_limits_the_walk() -> None:
    g = small_graph()
    steps = g.trace(g.resolve('customers.lifetime_value')[0], max_depth=1)
    assert ids(steps) - {'task.nightly.dbt_run'} == {
        'model.shop.customers#lifetime_value',
        'model.shop.orders#amount',
    }


def test_path_between_columns_follows_data_flow() -> None:
    g = small_graph()
    path = g.path(g.resolve('payments.amount')[0], g.resolve('customers.lifetime_value')[0])
    assert [s.node.id for s in path] == [
        'source.shop.raw.payments#amount',
        'model.shop.stg_payments#amount',
        'model.shop.orders#amount',
        'model.shop.customers#lifetime_value',
    ]
    reverse = g.path(g.resolve('customers.lifetime_value')[0], g.resolve('payments.amount')[0])
    assert len(reverse) == 4


def test_path_falls_back_to_undirected_and_reports_no_path() -> None:
    g = small_graph()
    path = g.path(g.resolve('dashboard')[0], g.resolve('customers')[0])
    assert path[0].node.id == 'exposure.shop.dashboard'
    assert path[-1].node.id == 'model.shop.customers'
    g.add_node(Node('table.island', 'table', 'island'))
    assert g.path(g.resolve('island')[0], g.resolve('orders')[0]) == []


def test_path_between_equal_routes_does_not_depend_on_edge_order() -> None:
    def diamond(order: list[str]) -> Graph:
        g = Graph()
        for name in ['source', 'left', 'right', 'sink']:
            g.add_node(Node(f'model.shop.{name}', 'model', name))
        for middle in order:
            g.add_edge('model.shop.source', f'model.shop.{middle}', 'feeds')
            g.add_edge(f'model.shop.{middle}', 'model.shop.sink', 'feeds')
        return g

    routes = set()
    for order in (['left', 'right'], ['right', 'left']):
        g = diamond(order)
        path = g.path(g.resolve('source')[0], g.resolve('sink')[0])
        routes.add(tuple(s.node.id for s in path))
    assert routes == {('model.shop.source', 'model.shop.left', 'model.shop.sink')}


def test_round_trip_through_json(tmp_path: Path) -> None:
    g = small_graph()
    g.warn('something odd')
    g.meta['root'] = '/x'
    out = tmp_path / 'graph.json'
    g.write(out)
    loaded = Graph.read(out)
    assert loaded.to_dict() == g.to_dict()
    assert loaded.stats()['column'] == 10
    assert loaded.node_for_relation('db.main.orders') is not None


# The indexes queries run on are made again whenever what they are made from changes.


def test_a_query_sees_a_column_and_its_lineage_added_after_it() -> None:
    g = small_graph()
    amount = g.resolve('orders.amount')[0]
    assert 'model.shop.report#amount' not in ids(g.impact(amount))
    report = g.add_node(Node('model.shop.report', 'model', 'report', relation='db.main.report'))
    total = g.add_column(report, 'amount')
    g.add_edge(total.id, amount.id, 'derived_from')
    assert 'model.shop.report#amount' in ids(g.impact(amount))
    assert 'model.shop.orders#amount' in ids(g.trace(total))
    assert g.resolve('report.amount') == [total]


def test_a_query_sees_an_edge_removed_and_added_again() -> None:
    g = small_graph()
    amount = g.resolve('orders.amount')[0]
    lifetime = 'model.shop.customers#lifetime_value'
    assert lifetime in ids(g.impact(amount))
    [edge] = [e for e in g.in_edges(amount.id, 'derived_from') if e.source == lifetime]
    g.remove_edge(edge)
    assert lifetime not in ids(g.impact(amount))
    g.add_edge(lifetime, amount.id, 'derived_from')
    assert lifetime in ids(g.impact(amount))
    assert [s.node.id for s in g.path(amount, g.nodes[lifetime])] == [amount.id, lifetime]


def test_neighbours_follow_an_edge_added_after_a_query() -> None:
    g = small_graph()
    assert [n for _, n in g.downstream_edges('model.shop.customers')] == []
    g.add_node(Node('exposure.shop.churn', 'exposure', 'churn'))
    g.add_edge('model.shop.customers', 'exposure.shop.churn', 'feeds')
    assert [n for _, n in g.downstream_edges('model.shop.customers')] == ['exposure.shop.churn']


def test_resolve_sees_a_relation_added_after_a_query() -> None:
    g = small_graph()
    assert g.resolve('refunds') == []
    refunds = g.add_node(Node('model.shop.refunds', 'model', 'refunds', relation='db.main.refunds'))
    assert g.resolve('refunds') == [refunds]
    assert g.resolve('main.refunds') == [refunds]


def test_adding_a_column_keeps_the_relation_names_but_not_the_walks() -> None:
    g = small_graph()
    orders = g.resolve('orders')[0]
    g.impact(orders)
    names, walks = g._indexes['relation names'][1], g._indexes['traversal'][1]
    g.add_column(orders, 'status')
    assert g.resolve('orders') == [orders]
    assert g._indexes['relation names'][1] is names
    assert 'model.shop.orders#status' in ids(g.impact(orders))
    assert g._indexes['traversal'][1] is not walks


def test_an_index_is_made_again_only_when_its_basis_changes() -> None:
    g = small_graph()
    made: list[str] = []

    def builder(basis: str) -> Callable[[Graph], int]:
        def build(graph: Graph) -> int:
            made.append(basis)
            return len(made)

        return build

    def query_all() -> list[str]:
        made.clear()
        g.indexed('a', builder('graph'))
        g.indexed('b', builder('nodes'), basis='nodes')
        g.indexed('c', builder('relations'), basis='relations')
        return made

    assert query_all() == ['graph', 'nodes', 'relations']
    assert query_all() == []
    edge = g.add_edge('model.shop.orders', 'model.shop.stg_payments', 'precedes')
    assert query_all() == ['graph']
    assert edge is not None
    g.remove_edge(edge)
    assert query_all() == ['graph']
    g.add_column(g.nodes['model.shop.orders'], 'status')
    assert query_all() == ['graph', 'nodes']
    g.add_node(Node('model.shop.orders', 'model', 'orders', description='One row per order.'))
    assert query_all() == ['graph', 'nodes', 'relations']
    g.changed()
    assert query_all() == ['graph', 'nodes', 'relations']


def test_search_keeps_its_index_when_an_edge_changes_and_sees_new_nodes() -> None:
    g = small_graph()
    assert [n.id for n in search_nodes(g, 'refunds')] == []
    index = g._indexes['search'][1]
    g.add_edge('model.shop.orders', 'exposure.shop.dashboard', 'feeds', role='reference')
    g.add_edge('model.shop.customers', 'exposure.shop.dashboard', 'feeds')
    assert [n.id for n in search_nodes(g, 'dashboard')] == ['exposure.shop.dashboard']
    assert g._indexes['search'][1] is index
    g.add_node(Node('model.shop.refunds', 'model', 'refunds', relation='db.main.refunds'))
    assert [n.id for n in search_nodes(g, 'refunds')] == ['model.shop.refunds']


def test_changed_makes_queries_see_an_edit_in_place() -> None:
    g = small_graph()
    stg = g.resolve('stg_payments.amount')[0]
    assert 'model.shop.customers#lifetime_value' in ids(g.impact(stg))
    [edge] = g.in_edges(stg.id, 'derived_from')
    edge.attrs['role'] = 'reference'
    orders = g.resolve('stg_payments')[0]
    orders.name = 'payments_staged'
    g.changed()
    # A column reached through a reference edge is reported but not walked through.
    assert ids(g.impact(stg)) >= {'model.shop.orders#amount'}
    assert 'model.shop.customers#lifetime_value' not in ids(g.impact(stg))
    assert g.resolve('payments_staged') == [orders]


def test_max_depth_beyond_the_native_range_means_no_limit() -> None:
    g = small_graph()
    amount = g.resolve('raw.payments.amount')[0]
    lifetime = g.resolve('customers.lifetime_value')[0]
    assert g.impact(amount, max_depth=2**70) == g.impact(amount)
    assert g.trace(lifetime, max_depth=2**64) == g.trace(lifetime)
    assert g.impact(amount, max_depth=-(2**70)) == g.impact(amount, max_depth=0)
    assert ids(g.impact(amount, max_depth=0)) == {amount.id}


def test_ids_and_names_with_lone_surrogates_stay_distinct() -> None:
    # File names that are not UTF-8 decode, with surrogateescape, to lone surrogates; U+FFFD is
    # what a lossy conversion would turn them into, and U+D7FF is the core's escape character.
    names = [b'caf\xe9'.decode('utf-8', 'surrogateescape'), b'caf\xe8'.decode('utf-8', 'surrogateescape')]
    names += ['caf' + chr(0xFFFD), 'caf' + chr(0xD7FF), chr(0xD83D) + chr(0xDE00), chr(0x1F600)]
    g = Graph()
    nodes = [g.add_node(Node(f'table.{n}', 'table', n, relation=f'db.main.{n}')) for n in names]
    for upstream, downstream in itertools.pairwise(nodes):
        if upstream is not nodes[2]:
            g.add_edge(upstream.id, downstream.id, 'feeds')
    assert [s.node for s in g.impact(nodes[0])] == nodes[:3]
    assert [s.node for s in g.trace(nodes[5])] == [nodes[5], nodes[4], nodes[3]]
    assert [s.node for s in g.path(nodes[3], nodes[5])] == nodes[3:]
    assert g.path(nodes[0], nodes[4]) == []
    for name, node in zip(names, nodes, strict=True):
        assert g.resolve(name) == [node]
        assert g.resolve(f'main.{name}') == [node]
    assert search_nodes(g, 'caf') == sorted(nodes[:4], key=lambda n: n.id)
    # The task that writes a relation known only as an edge's endpoint is reached through it,
    # and the step names that relation by its own id.
    column = g.add_node(Node(f'table.{names[1]}x#c', 'column', 'c', parent=f'table.{names[1]}x'))
    g.add_node(Node('task.load', 'task', 'load'))
    g.add_edge('task.load', f'table.{names[1]}x', 'runs')
    [step] = [s for s in g.trace(column) if s.node.id == 'task.load']
    assert step.previous == f'table.{names[1]}x'


def test_the_native_index_returns_ids_as_given() -> None:
    ids = ['a' + chr(0xDC80), 'a' + chr(0xD7FF), 'a' + chr(0xD7FF) + chr(0xF0000), 'a' + chr(0xF0001)]
    index = _core.GraphIndex(ids, ['table'] * 4, [None] * 4, [], [], [], [], [])
    assert [index.id(i) for i in range(4)] == ids
    assert [index.neighbours(i, True) for i in ids] == [[], [], [], []]
