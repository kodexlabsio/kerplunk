from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from konstruct.core.graph import Graph, Node, column_id
from konstruct.outputs.html import write_html

HOSTILE = 'Ends here </script><script>alert(1)</script> and\u2028continues & <b>more</b>'
SVG_NAMESPACE = 'http://www.w3.org/2000/svg'


def sample_graph() -> Graph:
    g = Graph()
    g.meta = {'root': '/tmp/project', 'generated_at': '2026-11-06T10:00:00+00:00'}
    raw = g.add_node(Node('source.shop.raw.orders', 'source', 'orders', relation='db.raw.orders'))
    stg = g.add_node(
        Node(
            'model.shop.stg_orders',
            'model',
            'stg_orders',
            relation='db.main.stg_orders',
            description=HOSTILE,
            attrs={'materialized': 'view'},
        )
    )
    final = g.add_node(Node('model.shop.orders', 'model', 'orders', relation='db.main.orders'))
    task = g.add_node(Node('task.nightly.dbt_run', 'task', 'dbt_run', attrs={'operator': 'BashOperator'}))
    dag = g.add_node(Node('dag.nightly', 'dag', 'nightly'))
    test = g.add_node(Node('test.shop.not_null_orders_id', 'test', 'not_null_orders_id'))
    for rel, cols in [
        (raw, ['id', 'amount', 'status']),
        (stg, ['order_id', 'amount']),
        (final, ['order_id', 'amount']),
    ]:
        for col in cols:
            g.add_column(rel, col, data_type='integer')
    g.add_edge(raw.id, stg.id, 'feeds')
    g.add_edge(stg.id, final.id, 'feeds')
    g.add_edge(column_id(stg.id, 'order_id'), column_id(raw.id, 'id'), 'derived_from', transform='direct')
    g.add_edge(
        column_id(stg.id, 'amount'), column_id(raw.id, 'amount'), 'derived_from', transform='transformed'
    )
    g.add_edge(column_id(final.id, 'amount'), column_id(stg.id, 'amount'), 'derived_from')
    g.add_edge(column_id(raw.id, 'status'), stg.id, 'feeds', role='reference')
    g.add_edge(test.id, column_id(final.id, 'order_id'), 'tests')
    g.add_edge(dag.id, task.id, 'runs')
    g.add_edge(task.id, final.id, 'runs')
    g.warn('dbt: model orders: could not parse a statement')
    return g


def embedded_json(page: str) -> str:
    match = re.search(r'<script type="application/json" id="konstruct-data">(.*?)</script>', page, re.S)
    assert match is not None
    return match.group(1)


def inline_script(page: str) -> str:
    match = re.search(r'<script type="module">(.*?)</script>', page, re.S)
    assert match is not None
    return match.group(1)


def test_writes_a_file_with_the_graph_embedded(tmp_path: Path) -> None:
    graph = sample_graph()
    out = write_html(graph, tmp_path / 'out' / 'graph.html')
    assert out == tmp_path / 'out' / 'graph.html'
    assert out.is_file()
    page = out.read_text(encoding='utf-8')
    assert json.loads(embedded_json(page)) == graph.to_dict()


def test_embedded_data_cannot_break_out_of_its_script_tag(tmp_path: Path) -> None:
    page = write_html(sample_graph(), tmp_path / 'graph.html').read_text(encoding='utf-8')
    assert '</script><script>alert' not in page
    assert '<b>more' not in page
    assert '\u2028' not in page
    assert page.count('</script>') == 3
    data = json.loads(embedded_json(page))
    stg = next(n for n in data['nodes'] if n['id'] == 'model.shop.stg_orders')
    assert stg['description'] == HOSTILE


def test_page_references_no_external_resources(tmp_path: Path) -> None:
    """No attribute, stylesheet rule or import points off the page. (The script contains URLs as
    plain strings, such as namespace URIs and links in Svelte's error messages; the browser test
    checks that the page makes no requests at all.)"""
    page = write_html(sample_graph(), tmp_path / 'graph.html').read_text(encoding='utf-8')
    assert re.findall(r"""\b(?:src|href)\s*=\s*["']([^"']*)["']""", page) == ['#search']
    assert '@import' not in page
    urls = re.findall(r'url\(([^)]*)\)', page)
    assert len([u for u in urls if u.startswith('data:font/woff2;base64,')]) == 4
    assert all(u.startswith(('data:', '#')) for u in urls), [
        u[:40] for u in urls if not u.startswith('data:')
    ]
    assert 'fetch(' not in page and 'XMLHttpRequest' not in page


def test_column_formulas_and_conditions_are_embedded(tmp_path: Path) -> None:
    graph = sample_graph()
    stg = graph.nodes['model.shop.stg_orders']
    stg.attrs['conditions'] = ["WHERE: db.raw.orders.status <> 'void'"]
    graph.nodes[column_id(stg.id, 'amount')].attrs['expression'] = 'db.raw.orders.amount / 100'
    graph.nodes[column_id('model.shop.orders', 'amount')].attrs['expression'] = (
        'SUM(db.main.stg_orders.amount)'
    )
    page = write_html(graph, tmp_path / 'graph.html').read_text(encoding='utf-8')
    match = re.search(r'<script type="application/json" id="konstruct-explain">(.*?)</script>', page, re.S)
    assert match is not None
    data = json.loads(match.group(1))
    assert data['conditions'] == {stg.id: ["WHERE: orders.status <> 'void'"]}
    assert data['columns'][column_id('model.shop.orders', 'amount')] == {
        'e': 'SUM(stg_orders.amount)',
        'c': 'SUM(orders.amount / 100)',
        's': ['orders.amount'],
    }


def test_the_viewer_assets_follow_the_design_rules() -> None:
    """DESIGN.md: Manrope and JetBrains Mono, no text below 14px (13.5px eyebrows), no em dashes."""
    from konstruct.outputs.html import template

    page = template()
    assert re.search(r'font-family:\s*"?Manrope"?', page) and re.search(
        r'font-family:\s*"?JetBrains Mono"?', page
    )
    sizes = [float(size) for size in re.findall(r'font-size:\s*(\d+(?:\.\d+)?)px', page)]
    sizes += [
        float(size)
        for size in re.findall(r"(?<![\w.-])(\d+(?:\.\d+)?)px (?:var\(--font|Manrope|'JetBrains)", page)
    ]
    assert sizes and min(sizes) >= 13.5
    assert '\u2014' not in page and '\u2013' not in page


def test_inline_script_is_valid_javascript(tmp_path: Path) -> None:
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is not installed')
    page = write_html(sample_graph(), tmp_path / 'graph.html').read_text(encoding='utf-8')
    script = tmp_path / 'viewer.mjs'
    script.write_text(inline_script(page), encoding='utf-8')
    result = subprocess.run([node, '--check', str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
