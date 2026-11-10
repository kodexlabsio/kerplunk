"""Search by meaning: synonyms, abbreviations, formulas and lineage find what names alone miss."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import duckdb
import pytest

from konstruct import Project
from konstruct.analysis import query, semantic
from konstruct.core.graph import Graph, Node
from konstruct.engine.build import build
from konstruct.engine.config import load_config


@pytest.fixture(scope='module')
def shop(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A warehouse whose raw revenue column is renamed twice on its way to a mart."""
    root = tmp_path_factory.mktemp('shop')
    con = duckdb.connect(str(root / 'shop.duckdb'))
    con.execute('create schema raw')
    con.execute(
        'create table raw.orders (id integer, customer_id integer, revenue double, refund double, '
        'qty integer, unit_price double, created_at timestamp, cancelled_at timestamp)'
    )
    con.execute('create table raw.customers (id integer, email varchar, churned_at timestamp)')
    con.execute('create table raw.lines (id integer, list_price double, units integer)')
    con.close()
    (root / 'sql').mkdir()
    (root / 'sql' / 'staging.sql').write_text(
        'create table main.stg_orders as select id, customer_id, revenue as rev, '
        'refund as refund_amt_gbp, qty, unit_price, cancelled_at from raw.orders'
    )
    (root / 'sql' / 'marts.sql').write_text(
        'create table main.fct_sales as select id, rev - refund_amt_gbp as net_sales_amt, '
        'unit_price * qty as line_total from main.stg_orders'
    )
    (root / 'sql' / 'lines.sql').write_text(
        'create table main.fct_lines as select id, list_price * units as gross_value from raw.lines'
    )
    build(load_config(root))
    return root


@pytest.fixture
def graph(shop: Path) -> Graph:
    return query.load_graph(shop / 'konstruct-out')


def labels(result: query.SearchResult | semantic.SimilarResult) -> list[str]:
    return [r.label for r in result.results]


def test_a_renamed_revenue_column_is_found_by_meaning(graph: Graph) -> None:
    keyword = labels(query.search(graph, 'revenue'))
    assert 'fct_sales.net_sales_amt' not in keyword
    found = query.search(graph, 'revenue', limit=5, mode='semantic')
    # The exact name first, then its renamed copy, then the mart column that reads it.
    assert labels(found)[0] == 'orders.revenue'
    assert 'stg_orders.rev' in labels(found)
    net = next(r for r in found.results if r.label == 'fct_sales.net_sales_amt')
    assert net.matched in ('lineage', 'formula')
    assert 'sales' in str(semantic.search(graph, 'sales', limit=3))


def test_synonyms_and_abbreviations(graph: Graph) -> None:
    assert (
        labels(semantic.search(graph, 'refund amount in pounds', limit=3))[0] == 'stg_orders.refund_amt_gbp'
    )
    assert 'stg_orders.qty' in labels(semantic.search(graph, 'quantity', limit=5))
    assert not query.search(graph, 'cancellations').results
    assert labels(semantic.search(graph, 'cancellations', limit=3))[0] == 'stg_orders.cancelled_at'
    assert 'customers.churned_at' in labels(semantic.search(graph, 'customer churn', limit=5))


def test_formulas_are_found_by_their_shape(graph: Graph) -> None:
    # A question that reads as SQL asks for the computation, not its inputs: the column that
    # multiplies price by quantity first, then the same product under other names.
    found = semantic.search(graph, 'price * quantity', limit=5)
    assert labels(found)[0] == 'fct_sales.line_total'
    assert 'fct_lines.gross_value' in labels(found)[:3]
    assert {r.matched for r in found.results if r.label.startswith('fct_')} == {'formula'}
    like = semantic.similar(graph, 'fct_sales.line_total', limit=5, kinds=['column'])
    assert 'fct_lines.gross_value' in labels(like)
    assert like.node.label == 'fct_sales.line_total'
    assert 'fct_sales.line_total' not in labels(like)
    assert str(like).startswith('Like fct_sales.line_total (')


def test_exact_names_still_win(graph: Graph) -> None:
    assert labels(semantic.search(graph, 'rev', limit=3))[0] == 'stg_orders.rev'
    assert labels(semantic.search(graph, 'fct_sales', limit=3))[0] == 'fct_sales'
    first = semantic.search(graph, 'line_total', limit=3).results[0]
    assert first.label == 'fct_sales.line_total' and first.score is not None and first.score > 1


def test_results_say_what_they_rest_on(graph: Graph) -> None:
    found = query.search(graph, 'revenue', limit=5, mode='semantic')
    data = found.to_dict()
    assert json.loads(json.dumps(data)) == data
    assert all('score' in r for r in data['results'])
    text = str(found)
    [net] = [line for line in text.splitlines() if line.startswith('fct_sales.net_sales_amt')]
    assert 'by lineage' in net or 'by formula' in net
    assert str(semantic.search(graph, 'zzqx')) == "Nothing matches 'zzqx'."
    assert semantic.search(graph, 'revenue', limit=0).results == []
    assert str(semantic.SimilarResult(found.results[0], [])) == 'Nothing is like orders.revenue.'


def test_kinds_filter_both_searches(graph: Graph) -> None:
    found = semantic.search(graph, 'orders', kinds=['table'])
    assert found.results and all(r.kind == 'table' for r in found.results)
    assert all(r.kind == 'column' for r in semantic.search(graph, 'revenue', kinds=['column']).results)


def test_the_first_search_saves_the_index_and_later_ones_reuse_it(shop: Path, tmp_path: Path) -> None:
    root = tmp_path / 'shop'
    shutil.copytree(shop, root, ignore=shutil.ignore_patterns('konstruct-out'))
    out = root / 'konstruct-out'
    result = build(load_config(root))
    saved = out / semantic.VECTOR_FILE
    # The build itself makes no index.
    assert not saved.exists() and saved not in result.outputs
    graph = query.load_graph(out)
    count = len(graph.nodes)
    assert semantic.vector_index(graph, out).update == (0, count, 0, False)
    assert saved.stat().st_size > 0
    first = saved.read_bytes()
    # A later search, in another process or after reloading, reads the file whole: every node
    # kept, and its blocks read rather than computed.
    again = query.load_graph(out)
    assert semantic.vector_index(again, out).update == (count, 0, 0, True)
    assert semantic.vector_index(again, out).core.rows_read
    assert saved.read_bytes() == first
    # Without the file, or with a damaged one, the index is built anew.
    assert semantic.VectorIndex(graph, b'damaged').update == (0, count, 0, False)
    # After a rebuild with a renamed column, the next search reinserts it with what reads it,
    # keeps the rest, and saves the result.
    staging = root / 'sql' / 'staging.sql'
    staging.write_text(staging.read_text().replace('revenue as rev', 'revenue as gross_rev'))
    build(load_config(root), update=True)
    updated = query.load_graph(out)
    kept, added, removed, reused = semantic.vector_index(updated, out).update
    assert not semantic.vector_index(updated, out).core.rows_read
    # (A column's vector carries its neighbours' words, so in a graph this small most change.)
    assert reused and kept > 0 and added > 0 and removed > 0
    assert saved.read_bytes() != first
    found = semantic.search(updated, 'gross revenue', limit=3, out_dir=out)
    assert 'stg_orders.gross_rev' in labels(found)[:2]
    fresh = query.load_graph(out)
    assert semantic.vector_index(fresh, out).update[1:] == (0, 0, True)
    assert semantic.vector_index(fresh, out).core.rows_read
    assert labels(found) == labels(semantic.search(fresh, 'gross revenue', limit=3))


def test_an_output_directory_that_cannot_be_written_still_answers(graph: Graph, tmp_path: Path) -> None:
    missing = tmp_path / 'not' / 'there'
    found = semantic.search(graph, 'revenue', limit=3, out_dir=missing)
    assert labels(found)[0] == 'orders.revenue'
    assert not missing.exists()


def test_ids_and_names_with_lone_surrogates_stay_distinct() -> None:
    # File names that are not UTF-8 decode, with surrogateescape, to lone surrogates; U+D7FF is
    # the core's escape character.
    names = [b'caf\xe9'.decode('utf-8', 'surrogateescape'), b'caf\xe8'.decode('utf-8', 'surrogateescape')]
    names += ['caf' + chr(0xD7FF), 'r\udc80evenue']
    graph = Graph()
    nodes = [
        graph.add_node(
            Node(
                f'table.{n}',
                'table',
                n,
                relation=f'db.main.{n}',
                description=f'about {n}',
                attrs={'synonyms': [n]},
            )
        )
        for n in names
    ]
    column = graph.add_column(nodes[0], 'amount', attrs={'expression': f'"{names[1]}".x * 2'})
    graph.add_edge(column.id, nodes[1].id, 'derived_from')
    found = semantic.search(graph, 'caf', limit=10)
    assert {r.id for r in found.results} >= {n.id for n in nodes[:3]}
    assert len({r.id for r in found.results}) == len(found.results)
    for node in nodes:
        assert semantic.similar(graph, node.id, limit=3).node.id == node.id
    assert semantic.search(graph, chr(0xDC80), limit=3).query == chr(0xDC80)
    assert semantic.search(graph, 'amount', kinds=['column']).results[0].id == column.id


def test_a_graph_with_no_file_builds_its_index_in_memory() -> None:
    graph = Graph()
    term = graph.add_node(
        Node(
            id='term.net_revenue',
            kind='term',
            name='Net revenue',
            description='Sales after refunds, in pounds',
            attrs={'synonyms': ['net sales'], 'unit': 'currency', 'currency': 'GBP'},
        )
    )
    relation = graph.add_node(Node(id='table.w.main.f', kind='table', name='f', relation='w.main.f'))
    column = graph.add_column(relation, 'x1', attrs={'expression': 'w.main.g.a - w.main.g.b'})
    graph.add_edge(term.id, column.id, 'defines')
    # The term by its synonym's concept, then the column it defines.
    assert labels(semantic.search(graph, 'takings', limit=5)) == ['Net revenue', 'f.x1']
    # The column says nothing itself; the term that defines it does.
    found = semantic.search(graph, 'revenue in pounds', limit=5)
    assert labels(found)[:2] == ['Net revenue', 'f.x1']
    assert found.results[1].matched == 'lineage'
    assert semantic.vector_index(graph) is semantic.vector_index(graph)
    graph.add_column(relation, 'x2')
    assert len(semantic.vector_index(graph).nodes) == 4


@pytest.mark.dbt
def test_the_example_project(jaffle_built_graph: Path) -> None:
    project = Project(jaffle_built_graph)
    graph = project.graph
    ltv = labels(project.search('lifetime value', limit=5, mode='semantic'))
    assert 'customer_ltv.lifetime_value' in ltv[:3]
    assert (project.out_dir / semantic.VECTOR_FILE).is_file()
    payments = labels(semantic.search(graph, 'payment amount', limit=10))
    assert {'stg_payments.amount', 'raw_payments.amount'} <= set(payments)
    like = labels(semantic.similar(graph, 'stg_payments.amount', limit=5))
    assert 'orders.amount' in like


@pytest.fixture(scope='module')
def example(jaffle_built_graph: Path) -> Graph:
    return Project(jaffle_built_graph).graph


@pytest.mark.dbt
@pytest.mark.parametrize('question', ['revenue', 'sales', 'turnover'])
def test_revenue_in_other_words_finds_the_revenue_measures(example: Graph, question: str) -> None:
    # jaffle_shop names nothing revenue: its revenue is the order amount and lifetime value.
    found = labels(semantic.search(example, question, limit=10))
    assert {'Order amount', 'Customer lifetime value'} <= set(found[:3])
    columns = labels(semantic.search(example, question, limit=10, kinds=['column']))
    assert {'orders.amount', 'customers.customer_lifetime_value'} <= set(columns)


@pytest.mark.dbt
def test_each_result_means_most_of_the_question(example: Graph) -> None:
    # The order amount is the sales, and its order's status says which were returned; the
    # definition of a completed order says not returned. Lifetime value means sales but nothing
    # about refunds, so it is not an answer.
    found = labels(semantic.search(example, 'net sales after refunds', limit=10))
    assert set(found[:2]) == {'Order amount', 'Completed order'}
    assert 'Customer lifetime value' not in found
    assert semantic.search(example, 'net sales').results == []


@pytest.mark.dbt
@pytest.mark.parametrize('question', ['churn', 'employee turnover', 'staff churn rate'])
def test_a_question_about_what_the_project_does_not_hold_finds_nothing(example: Graph, question: str) -> None:
    # jaffle_shop records no churn and no employees. Turnover alone is revenue, but a result
    # must mean every word that changes what is asked, not only the words the project has.
    assert semantic.search(example, question).results == []


@pytest.mark.dbt
def test_a_surname_is_a_last_name(example: Graph) -> None:
    found = semantic.search(example, 'surname', limit=5, kinds=['column']).results
    assert found[0].label == 'customers.last_name'
    assert all(r.label.endswith('last_name') for r in found[:3])


@pytest.mark.dbt
def test_matches_rest_on_shared_meaning_not_hashing(example: Graph) -> None:
    assert not any('last_name' in r for r in labels(semantic.search(example, 'turnover', limit=20)))
    refunds = semantic.search(example, 'refunds', limit=20).results
    status = next(r for r in refunds if r.label == 'orders.status')
    # Its description lists the returned status; it computes nothing.
    assert status.matched == 'words'
    for noise in ['xyzzy plugh', 'quarterly weather forecast', 'employee salary', 'zzqx']:
        assert semantic.search(example, noise).results == [], noise


@pytest.mark.dbt
def test_a_copied_column_is_never_matched_by_formula(example: Graph) -> None:
    copies = [
        n
        for n in example.nodes.values()
        if n.kind == 'column'
        and (inputs := example.out_edges(n.id, 'derived_from'))
        and all(e.attrs.get('transform') == 'direct' for e in inputs)
    ]
    assert len(copies) > 10
    ids = {n.id for n in copies}
    for question in ['order status', 'refunds', 'amount', 'customer id', 'amount / 100', 'sum(amount)']:
        for result in semantic.search(example, question, limit=50).results:
            if result.id in ids:
                assert result.matched != 'formula', (question, result.label)
    for node in copies[:5]:
        for result in semantic.similar(example, node.id, limit=10).results:
            if result.id in ids:
                assert result.matched != 'formula', (node.id, result.label)
