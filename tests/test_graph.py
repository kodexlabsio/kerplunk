from __future__ import annotations

from pathlib import Path

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
