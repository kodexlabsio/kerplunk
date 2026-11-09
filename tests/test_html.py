from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from konstruct.core.graph import Graph, Node, column_id
from konstruct.outputs.html import VIEWER_SQL_LIMIT, write_html

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
    # Links within the page are hash routes (#/glossary); nothing points elsewhere.
    links = re.findall(r"""\b(?:src|href)\s*=\s*["']([^"']*)["']""", page)
    assert links and all(link.startswith('#/') for link in links), links
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


def explain_json(page: str) -> dict[str, Any]:
    match = re.search(r'<script type="application/json" id="konstruct-explain">(.*?)</script>', page, re.S)
    assert match is not None
    data: dict[str, Any] = json.loads(match.group(1))
    return data


def test_the_sql_behind_models_and_tasks_is_embedded(tmp_path: Path) -> None:
    root = tmp_path / 'project'
    (root / 'models').mkdir(parents=True)
    (root / 'models' / 'orders.sql').write_text("select * from {{ ref('stg_orders') }}\n")
    (root / 'models' / 'big.sql').write_text('select 1\n' + '-' * (VIEWER_SQL_LIMIT + 10))
    compiled = root / 'target' / 'compiled' / 'shop' / 'models'
    compiled.mkdir(parents=True)
    (compiled / 'orders.sql').write_text('select * from db.main.stg_orders\n')
    (root / 'dags' / 'sql').mkdir(parents=True)
    (root / 'dags' / 'sql' / 'report.sql').write_text('create table report as select 1\n')
    (tmp_path / 'secret.sql').write_text('outside the project')
    graph = Graph()
    graph.meta = {'root': str(root)}
    graph.add_node(
        Node('model.shop.orders', 'model', 'orders', path='models/orders.sql', attrs={'package': 'shop'})
    )
    graph.add_node(Node('model.shop.big', 'model', 'big', path='models/big.sql'))
    graph.add_node(Node('model.shop.away', 'model', 'away', path='../secret.sql'))
    graph.add_node(Node('model.shop.gone', 'model', 'gone', path='models/gone.sql'))
    graph.add_node(Node('task.nightly.report', 'task', 'report', attrs={'sql_path': 'dags/sql/report.sql'}))
    sql = explain_json(write_html(graph, tmp_path / 'graph.html').read_text(encoding='utf-8'))['sql']
    assert sql['model.shop.orders'] == {
        's': "select * from {{ ref('stg_orders') }}\n",
        'c': 'select * from db.main.stg_orders\n',
    }
    assert sql['task.nightly.report'] == {'s': 'create table report as select 1\n'}
    assert len(sql['model.shop.big']['s']) == VIEWER_SQL_LIMIT and sql['model.shop.big']['t'] == 1
    assert 'model.shop.away' not in sql and 'model.shop.gone' not in sql


def test_the_sql_is_read_again_only_when_a_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from konstruct.engine.build import Cache
    from konstruct.outputs import html

    root = tmp_path / 'project'
    (root / 'models').mkdir(parents=True)
    orders = root / 'models' / 'orders.sql'
    orders.write_text('select 1\n')
    (tmp_path / 'secret.sql').write_text('outside the project')
    (root / 'models' / 'link.sql').symlink_to(tmp_path / 'secret.sql')
    graph = Graph()
    graph.meta = {'root': str(root)}
    graph.add_node(
        Node('model.shop.orders', 'model', 'orders', path='models/orders.sql', attrs={'package': 'shop'})
    )
    graph.add_node(Node('model.shop.link', 'model', 'link', path='models/link.sql'))
    first = Cache(tmp_path / 'cache.json', enabled=False)
    assert html.code(graph, first.quiet()) == {'model.shop.orders': {'s': 'select 1\n'}}
    first.save()
    # The next build reads nothing that has not changed.
    cache = Cache(tmp_path / 'cache.json', enabled=True).quiet()
    reads: list[str] = []
    read = html._read

    def counted(root: Path, relative: str) -> str | None:
        reads.append(relative)
        return read(root, relative)

    monkeypatch.setattr(html, '_read', counted)
    assert html.code(graph, cache) == {'model.shop.orders': {'s': 'select 1\n'}}
    assert reads == []
    # A file whose size changes, or a compiled copy that appears, is read again.
    orders.write_text('select 22\n')
    assert html.code(graph, cache)['model.shop.orders'] == {'s': 'select 22\n'}

    # So is a file rewritten in place at the same size with its times put back, as some tools do:
    # its change time still moves.
    def build() -> dict[str, dict[str, Any]]:
        builds = Cache(tmp_path / 'cache.json', enabled=True)
        found = html.code(graph, builds.quiet())
        builds.save()
        return found

    monkeypatch.undo()
    assert build()['model.shop.orders'] == {'s': 'select 22\n'}
    before = orders.stat()
    orders.write_text('select 33\n')
    os.utime(orders, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert build()['model.shop.orders'] == {'s': 'select 33\n'}
    compiled = root / 'target' / 'compiled' / 'shop' / 'models'
    compiled.mkdir(parents=True)
    (compiled / 'orders.sql').write_text('select 22 from compiled\n')
    assert build()['model.shop.orders'] == {'s': 'select 33\n', 'c': 'select 22 from compiled\n'}


def test_a_graph_without_its_project_embeds_no_sql(tmp_path: Path) -> None:
    graph = sample_graph()
    graph.meta['root'] = str(tmp_path / 'missing')
    assert explain_json(write_html(graph, tmp_path / 'graph.html').read_text(encoding='utf-8'))['sql'] == {}


@pytest.mark.dbt
def test_the_ontology_and_conflicts_are_embedded(jaffle_built_graph: Path) -> None:
    data = explain_json((jaffle_built_graph / 'konstruct-out' / 'graph.html').read_text(encoding='utf-8'))
    customer = next(e for e in data['ontology']['entities'] if e['name'] == 'customer')
    assert 'model.jaffle_shop.customers#customer_id' in customer['key_ids']
    measure = next(m for m in data['ontology']['measures'] if m['name'] == 'Order amount')
    assert measure['columns'][0]['id'] == 'model.jaffle_shop.orders#amount'
    assert {r['name'] for r in data['ontology']['rules']} >= {'one-formula-per-measure'}
    assert data['conflicts']['candidates'] > 0 and 'conflicts' in data['conflicts']
    assert 'model.jaffle_shop.orders' in data['sql']


def test_konstruct_locale_sets_the_page_language(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('KONSTRUCT_LOCALE', 'pt-BR')
    page = write_html(sample_graph(), tmp_path / 'graph.html').read_text(encoding='utf-8')
    assert explain_json(page)['locale'] == 'pt-BR'
    monkeypatch.delenv('KONSTRUCT_LOCALE')
    page = write_html(sample_graph(), tmp_path / 'graph.html').read_text(encoding='utf-8')
    assert 'locale' not in explain_json(page)


def test_conflicts_are_reused_while_the_graph_is_unchanged(tmp_path: Path) -> None:
    from konstruct.engine.build import Cache
    from konstruct.outputs.html import conflicts_data

    graph = sample_graph()
    cache = Cache(tmp_path / 'cache.json', enabled=True)
    first = conflicts_data(graph, cache)
    cache.save()
    # The next build: a new build time is not a change to the graph.
    graph.meta['generated_at'] = '2026-11-07T10:00:00+00:00'
    cache = Cache(tmp_path / 'cache.json', enabled=True)
    assert conflicts_data(graph, cache) == first
    assert (cache.hits, cache.misses) == (1, 0)
    graph.add_node(Node('model.shop.more', 'model', 'more'))
    conflicts_data(graph, cache)
    assert cache.misses == 1
