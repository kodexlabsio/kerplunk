"""graph.html in a real browser (Chromium through Playwright).

Skipped when Playwright's Chromium is not installed (`uv run playwright install chromium`);
CI installs it, and KONSTRUCT_REQUIRE_SERVICES turns the skip into a failure there.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from konstruct.core.graph import Graph, Node, column_id
from konstruct.outputs.html import write_html

pytestmark = pytest.mark.browser


@pytest.fixture(scope='module')
def browser() -> Iterator[Any]:
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        pytest.skip('playwright is not installed')
    with sync_playwright() as playwright:
        try:
            # KONSTRUCT_BROWSER_CHANNEL=chrome uses an installed Google Chrome instead of Playwright's.
            instance = playwright.chromium.launch(channel=os.environ.get('KONSTRUCT_BROWSER_CHANNEL') or None)
        except Error as error:
            if os.environ.get('KONSTRUCT_REQUIRE_SERVICES'):
                raise
            pytest.skip(f'Chromium is not available: {str(error).splitlines()[0]}')
        yield instance
        instance.close()


@pytest.fixture
def open_page(browser: Any) -> Iterator[Any]:
    pages = []

    def opener(path: Path, fragment: str = '', **options: Any) -> Any:
        options.setdefault('viewport', {'width': 1400, 'height': 900})
        context = browser.new_context(**options)
        page = context.new_page()
        page.problems = []
        page.on('pageerror', lambda error: page.problems.append(str(error)))
        page.on('console', lambda m: page.problems.append(m.text) if m.type in ('error', 'warning') else None)
        # The page is offline: anything it requests beyond the file itself is a problem.
        page.on(
            'request',
            lambda r: (
                page.problems.append(f'request {r.url}') if not r.url.startswith(('file:', 'data:')) else None
            ),
        )
        page.goto(path.as_uri() + fragment)
        page.wait_for_selector('#page h1, #lineage')
        pages.append(page)
        return page

    yield opener
    for page in pages:
        assert page.problems == [], page.problems
        page.context.close()


def wait_for_ids(page: Any, ids: list[str]) -> None:
    """Wait until the graph draws exactly these nodes: a route change renders after the hash changes."""
    page.wait_for_function(
        'ids => JSON.stringify([...document.querySelectorAll("#nodes [data-id]")]'
        '.map(e => e.getAttribute("data-id")).sort()) === JSON.stringify(ids)',
        arg=sorted(ids),
    )


def graph_ids(page: Any) -> list[str]:
    ids: list[str] = page.eval_on_selector_all(
        '#nodes [data-id]', "els => els.map(e => e.getAttribute('data-id'))"
    )
    return ids


@pytest.mark.dbt
def test_overview_and_model_page(jaffle_built_graph: Path, open_page: Any) -> None:
    page = open_page(jaffle_built_graph / 'konstruct-out' / 'graph.html')
    assert page.inner_text('#page h1') == 'jaffle_shop'
    assert page.title() == 'jaffle_shop · Konstruct'
    stats = page.inner_text('#stats')
    assert '44\ncolumns' in stats and '5\nmodels' in stats
    assert 'Column lineage' in page.inner_text('#page')

    # The project tree opens the model's page, with its lineage docked beneath it.
    page.click('#tree [data-key="pkg:jaffle_shop/models"]')
    page.click('#tree [data-key="model.jaffle_shop.orders"]')
    page.wait_for_selector('#page h1:text-is("orders")')
    assert page.evaluate('location.hash') == '#/node/model.jaffle_shop.orders'
    text = page.inner_text('#page')
    assert 'jaffle_shop.main.orders' in text and 'models/orders.sql' in text
    names = page.eval_on_selector_all('#col-table tbody th a', 'els => els.map(e => e.textContent)')
    assert names[:3] == ['order_id', 'customer_id', 'order_date']
    assert 'not_null_orders_amount' in text
    assert 'GROUP BY: stg_payments.order_id' in text
    assert 'konstruct impact orders' in text
    # The SQL as written and as dbt compiled it.
    assert "ref('stg_payments')" in page.inner_text('#sec-code pre')
    page.click('#sec-code [role="tab"]:text("Compiled")')
    compiled = page.inner_text('#sec-code pre')
    assert 'jaffle_shop' in compiled and 'ref(' not in compiled
    assert page.input_value('#select') == '+orders+'
    ids = graph_ids(page)
    assert {
        'model.jaffle_shop.orders',
        'model.jaffle_shop.stg_payments',
        'task.jaffle_shop_daily.dbt_run_marts',
    } <= set(ids)
    assert 'model.jaffle_shop.stg_customers' not in ids
    assert not any(i.startswith(('test.', 'dag.')) or '#' in i for i in ids)

    page.click('#col-table a.col-link:text-is("amount")')
    page.wait_for_selector('#page h1:text-is("amount")')
    column = page.inner_text('#page')
    assert '= SUM(stg_payments.amount)' in column
    assert '= SUM(raw_payments.amount / 100)' in column
    assert 'stg_payments.amount' in page.inner_text('#comes-from')
    assert 'raw_payments.amount' in page.inner_text('#comes-from')
    assert 'Nothing downstream reads this column.' in column
    assert 'konstruct explain orders.amount' in column
    assert page.evaluate('location.hash') == '#/node/model.jaffle_shop.orders%23amount'
    # The column's path is drawn on the docked graph.
    assert page.locator(".row.selected[data-col='model.jaffle_shop.orders#amount']").count() == 1
    assert page.locator(".row.on-path[data-col='seed.jaffle_shop.raw_payments#amount']").count() == 1
    assert page.locator('.edge.column.hl').count() == 2
    # Back returns to the model.
    page.go_back()
    page.wait_for_selector('#page h1:text-is("orders")')


@pytest.mark.dbt
def test_links_selectors_and_full_screen(jaffle_built_graph: Path, open_page: Any) -> None:
    graph = jaffle_built_graph / 'konstruct-out' / 'graph.html'
    # Links that earlier versions wrote still open their node.
    fresh = open_page(graph, '#term.order_amount')
    assert fresh.inner_text('#page h1') == 'Order amount'
    old = open_page(graph, '#model.jaffle_shop.stg_payments%23amount')
    assert old.inner_text('#page h1') == 'amount'
    assert 'raw_payments.amount / 100' in old.inner_text('#page')

    page = open_page(graph, '#/lineage?select=stg_orders+')
    page.wait_for_selector('#nodes [data-id]')
    assert sorted(graph_ids(page)) == [
        'exposure.jaffle_shop.customer_dashboard',
        'model.jaffle_shop.customers',
        'model.jaffle_shop.orders',
        'model.jaffle_shop.stg_orders',
        'table.jaffle_shop.reporting.customer_ltv',
        'task.jaffle_shop_daily.build_customer_ltv',
    ]
    page.fill('#select', 'path:models/staging')
    page.fill('#exclude', 'stg_customers')
    page.press('#select', 'Enter')
    wait_for_ids(page, ['model.jaffle_shop.stg_orders', 'model.jaffle_shop.stg_payments'])
    assert 'exclude=stg_customers' in page.evaluate('location.hash')
    page.fill('#select', '+orders.amount nowhere')
    page.fill('#exclude', '')
    page.press('#select', 'Enter')
    page.wait_for_selector(".row[data-col='seed.jaffle_shop.raw_payments#amount']")
    assert 'Nothing matches nowhere' in page.inner_text('#selector-status')
    page.go_back()
    wait_for_ids(page, ['model.jaffle_shop.stg_orders', 'model.jaffle_shop.stg_payments'])

    # A node page goes full screen with its selection, and comes back.
    node = open_page(graph, '#/node/model.jaffle_shop.customers')
    node.click('#fullscreen')
    node.wait_for_function("location.hash.startsWith('#/lineage?select=+customers+')")
    node.wait_for_selector('#page', state='detached')
    node.click("[data-direction='upstream']")
    node.wait_for_function("document.getElementById('select').value === '+customers'")
    node.keyboard.press('Shift+L')
    node.wait_for_selector('#page h1:text-is("customers")')


@pytest.mark.dbt
def test_keyboard_graph_and_text_alternative(jaffle_built_graph: Path, open_page: Any) -> None:
    page = open_page(jaffle_built_graph / 'konstruct-out' / 'graph.html', '#/lineage?select=+orders')
    page.wait_for_selector('#nodes [data-id]')
    page.focus('#graph-box')
    page.keyboard.press('Home')
    first = page.inner_text('#graph-announce')
    assert 'upstream' in first and 'downstream' in first
    # Right follows the lineage downstream; the list beside the graph names the neighbours.
    for _ in range(4):
        page.keyboard.press('ArrowRight')
    assert page.locator('.node.cursor').count() == 1
    assert page.get_attribute('.node.cursor', 'data-id') == 'model.jaffle_shop.orders'
    inspector = page.text_content('#inspector') or ''
    assert 'Upstream in view' in inspector and 'stg_payments' in inspector
    page.keyboard.press('ArrowLeft')
    assert page.get_attribute('.node.cursor', 'data-id') != 'model.jaffle_shop.orders'
    page.keyboard.press('Enter')
    page.wait_for_selector('#page h1')
    assert page.evaluate('location.hash').startswith('#/node/')


@pytest.mark.dbt
def test_search_palette(jaffle_built_graph: Path, open_page: Any) -> None:
    page = open_page(jaffle_built_graph / 'konstruct-out' / 'graph.html')
    page.keyboard.press('Control+k')
    assert page.evaluate('document.activeElement.id') == 'palette-input'
    page.keyboard.type('lifetime')
    labels = page.eval_on_selector_all('#results .label', 'els => els.map(e => e.textContent)')
    # The business term comes first, then the columns.
    assert labels[:3] == [
        'Customer lifetime value',
        'customer_ltv.lifetime_value',
        'customers.customer_lifetime_value',
    ]
    assert page.inner_text('#match-count') == f'{len(labels)} matches'
    page.fill('#palette-input', 'ltv')
    assert page.inner_text('#results li:first-child .label') == 'Customer lifetime value'
    # Fuzzy: the letters in order.
    page.fill('#palette-input', 'stgpay')
    assert page.inner_text('#results li:first-child .label') == 'stg_payments'
    page.fill('#palette-input', 'amount kind:column')
    kinds = page.eval_on_selector_all('#results .kind', 'els => els.map(e => e.textContent)')
    assert kinds and set(kinds) == {'Column'}
    page.press('#palette-input', 'ArrowDown')
    page.press('#palette-input', 'Enter')
    assert page.locator('#palette').count() == 0
    hash_ = page.evaluate('location.hash')
    assert hash_.startswith('#/node/') and hash_.endswith('%23amount')
    page.keyboard.press('/')
    page.keyboard.type('zzzz')
    assert page.inner_text('#match-count') == 'No matches'
    page.keyboard.press('Escape')
    assert page.locator('#palette').count() == 0


@pytest.mark.dbt
def test_columns_theme_language_and_export(jaffle_built_graph: Path, open_page: Any) -> None:
    page = open_page(jaffle_built_graph / 'konstruct-out' / 'graph.html', '#/lineage', accept_downloads=True)
    page.wait_for_selector('#nodes [data-id]')
    page.click('#columns')
    assert page.locator('.row[data-col]').count() == 44
    assert page.locator('.edge.column').count() == 36
    page.click('#columns')
    page.click("[data-toggle='model.jaffle_shop.customers']")
    assert page.locator('.row[data-col]').count() == 8
    with page.expect_download() as download:
        page.click('#export')
        page.click('#export-svg')
    svg = Path(download.value.path()).read_text(encoding='utf-8')
    assert svg.startswith('<svg') and 'customers' in svg and '@font-face' in svg
    with page.expect_download() as download:
        page.click('#export')
        page.click('#export-png')
    assert download.value.suggested_filename.endswith('-lineage.png')
    assert Path(download.value.path()).read_bytes()[:4] == b'\x89PNG'

    theme = page.evaluate('document.documentElement.dataset.theme')
    page.click('#theme')
    assert page.evaluate('document.documentElement.dataset.theme') != theme
    page.select_option('#language', 'de')
    assert page.evaluate('document.documentElement.lang') == 'de'
    assert 'Übersicht' in page.inner_text('#nav')
    # The choice is remembered.
    page.reload()
    page.wait_for_selector('#lineage')
    assert page.evaluate('document.documentElement.lang') == 'de'


@pytest.mark.dbt
def test_right_to_left_and_locale_parameter(jaffle_built_graph: Path, open_page: Any) -> None:
    graph = jaffle_built_graph / 'konstruct-out' / 'graph.html'
    page = open_page(graph, '', locale='ar-EG')
    assert page.evaluate('document.documentElement.dir') == 'rtl'
    assert page.evaluate('document.documentElement.lang') == 'ar'
    page.goto(graph.as_uri() + '?lang=pt-PT#/glossary')
    page.wait_for_selector('#page h1:text-is("Glossário")')
    assert page.evaluate('document.documentElement.dir') == 'ltr'


def test_hostile_text_is_shown_as_text(tmp_path: Path, open_page: Any) -> None:
    graph = Graph()
    hostile = "</script><script>window.pwned = 1</script><img src=x onerror='window.pwned=2'>"
    node = graph.add_node(Node('model.p.evil', 'model', 'evil<b>', description=hostile, relation='db.s.evil'))
    graph.add_column(node, 'c', description=hostile)
    graph.warn(hostile)
    path = write_html(graph, tmp_path / 'graph.html')
    page = open_page(path, '#/node/model.p.evil')
    assert hostile in page.inner_text('#page')
    assert page.inner_text('#page h1') == 'evil<b>'
    page.goto(path.as_uri() + '#/warnings')
    page.wait_for_selector('#warnings')
    assert hostile in page.inner_text('#warnings')
    assert page.evaluate('window.pwned') is None
    assert page.locator('#page img').count() == 0


def test_a_large_graph_stays_responsive(tmp_path: Path, open_page: Any) -> None:
    graph = Graph()
    for i in range(1200):
        node = graph.add_node(Node(f'model.p.m{i}', 'model', f'm{i}', relation=f'db.s.m{i}'))
        for j in range(8):
            graph.add_column(node, f'c{j}')
        for parent in (i // 2, i // 3):
            if parent != i:
                graph.add_edge(f'model.p.m{parent}', node.id, 'feeds')
                graph.add_edge(
                    column_id(node.id, 'c0'), column_id(f'model.p.m{parent}', 'c0'), 'derived_from'
                )
    path = write_html(graph, tmp_path / 'graph.html')
    started = time.perf_counter()
    page = open_page(path, '#/lineage')
    page.wait_for_selector('#nodes [data-id]')
    assert time.perf_counter() - started < 10
    # Every node is laid out, but only those near the window are drawn.
    assert '1,200 nodes of 1,200' in page.inner_text('#selector-status')
    assert 0 < page.locator('#nodes [data-id]').count() <= 1200
    clicked = time.perf_counter()
    page.goto(path.as_uri() + '#/node/model.p.m1199')
    page.wait_for_selector('#page h1:text-is("m1199")')
    assert page.input_value('#select') == '+m1199+'
    # Its lineage is the model and its 30 ancestors.
    page.wait_for_selector('#selector-status:has-text("31 nodes of 1,200")')
    page.click('#col-table a.col-link:text-is("c0")')
    page.wait_for_selector('#comes-from')
    assert time.perf_counter() - clicked < 5
    # The browse table is virtualised: a few rows are in the page, not 1,200.
    page.goto(path.as_uri() + '#/browse')
    page.wait_for_selector('.browse-table')
    assert page.locator('.browse-table tbody tr:not(.spacer)').count() < 120
    assert page.get_attribute('.browse-table', 'aria-rowcount') == '1201'


def test_an_empty_graph(tmp_path: Path, browser: Any) -> None:
    page = browser.new_page()
    errors: list[str] = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    path = write_html(Graph(), tmp_path / 'graph.html')
    page.goto(path.as_uri())
    page.wait_for_selector('#page h1')
    page.goto(path.as_uri() + '#/lineage')
    assert page.locator('#empty').inner_text().strip()
    assert errors == []
    page.close()
