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

    def opener(path: Path, fragment: str = '') -> Any:
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
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
        page.wait_for_selector('#nodes [data-id]')
        pages.append(page)
        return page

    yield opener
    for page in pages:
        assert page.problems == [], page.problems
        page.close()


@pytest.mark.dbt
def test_example_graph(jaffle_built_graph: Path, open_page: Any) -> None:
    page = open_page(jaffle_built_graph / 'konstruct-out' / 'graph.html')
    ids = page.eval_on_selector_all('#nodes [data-id]', "els => els.map(e => e.getAttribute('data-id'))")
    assert 'model.jaffle_shop.orders' in ids
    assert 'task.jaffle_shop_daily.dbt_run_marts' in ids
    assert 'exposure.jaffle_shop.customer_dashboard' in ids
    assert not any(i.startswith(('test.', 'dag.')) or '#' in i for i in ids)
    assert page.inner_text('#project') == 'jaffle_shop'
    assert '44\ncolumns' in page.inner_text('#stats')
    assert page.title() == 'jaffle_shop \u00b7 Konstruct'

    page.click("[data-id='model.jaffle_shop.orders'] .name")
    panel = page.locator('#panel')
    assert panel.is_visible()
    text = panel.inner_text()
    assert 'jaffle_shop.main.orders' in text and 'models/orders.sql' in text
    names = page.eval_on_selector_all(
        '#panel table.columns td:first-child button', 'els => els.map(e => e.textContent)'
    )
    assert names[:3] == ['order_id', 'customer_id', 'order_date']
    assert 'not_null_orders_amount' in text
    assert 'GROUP BY: stg_payments.order_id' in text
    assert 'konstruct impact orders' in text

    page.locator('#panel table.columns').get_by_role('button', name='amount', exact=True).click()
    lineage = panel.inner_text()
    assert 'orders.amount' in lineage
    assert '= SUM(stg_payments.amount)' in lineage
    assert '= SUM(raw_payments.amount / 100)' in lineage
    assert 'stg_payments.amount' in lineage and 'raw_payments.amount' in lineage
    assert 'Nothing downstream reads this column.' in lineage
    assert 'konstruct explain orders.amount' in lineage
    # The column's rows and lineage are drawn on the canvas.
    assert page.locator(".row.selected[data-col='model.jaffle_shop.orders#amount']").count() == 1
    assert page.locator(".row.on-path[data-col='seed.jaffle_shop.raw_payments#amount']").count() == 1
    assert page.locator('.edge.column.hl').count() == 2
    assert page.evaluate('location.hash') == '#model.jaffle_shop.orders%23amount'
    box = page.locator("[data-id='seed.jaffle_shop.raw_payments']").bounding_box()
    canvas = page.locator('#graph').bounding_box()
    assert box is not None and canvas is not None
    assert canvas['x'] <= box['x'] and box['x'] + box['width'] <= canvas['x'] + canvas['width']

    page.keyboard.press('Escape')
    assert not panel.is_visible()
    assert page.locator('.row').count() == 0


@pytest.mark.dbt
def test_deep_links_direction_and_isolate(jaffle_built_graph: Path, open_page: Any) -> None:
    graph = jaffle_built_graph / 'konstruct-out' / 'graph.html'
    # A link opened fresh, as from a pull request comment or a bookmark.
    fresh = open_page(graph, '#term.order_amount')
    fresh.wait_for_selector('#panel:not([hidden])')
    assert 'Order amount' in fresh.inner_text('#panel h2')
    assert fresh.evaluate('location.hash') == '#term.order_amount'
    page = open_page(graph)
    page.goto(graph.as_uri() + '#model.jaffle_shop.stg_payments%23amount')
    page.wait_for_selector('#panel:not([hidden])')
    assert 'stg_payments.amount' in page.inner_text('#panel h2')
    assert 'raw_payments.amount / 100' in page.inner_text('#panel')
    page.click("[data-direction='upstream']")
    assert 'Feeds' not in (page.locator('#panel').text_content() or '')
    assert page.locator(".row.on-path[data-col='model.jaffle_shop.orders#amount']").count() == 0
    page.click("[data-direction='both']")
    assert page.locator(".row.on-path[data-col='model.jaffle_shop.orders#amount']").count() == 1
    # A selected column's path is isolated by default; the button shows everything again.
    assert page.locator('#isolate').get_attribute('aria-pressed') == 'true'
    isolated = page.locator('#nodes [data-id]').count()
    assert page.locator("[data-id='model.jaffle_shop.stg_customers']").count() == 0
    page.click('#isolate')
    assert page.locator('#nodes [data-id]').count() > isolated
    assert page.locator("[data-id='model.jaffle_shop.stg_customers']").count() == 1
    page.keyboard.press('Escape')
    assert page.locator('#isolate').get_attribute('aria-pressed') == 'false'


@pytest.mark.dbt
def test_columns_theme_and_filters(jaffle_built_graph: Path, open_page: Any) -> None:
    page = open_page(jaffle_built_graph / 'konstruct-out' / 'graph.html')
    page.click('#columns')
    assert page.locator('.row[data-col]').count() == 44
    assert page.locator('.edge.column').count() == 36
    page.click('#columns')
    page.click("[data-toggle='model.jaffle_shop.customers']")
    assert page.locator('.row[data-col]').count() == 8
    theme = page.evaluate('document.documentElement.dataset.theme')
    page.click('#theme')
    assert page.evaluate('document.documentElement.dataset.theme') != theme
    tasks = page.locator("[data-id^='task.']").count()
    assert tasks == 6
    page.click(".chip[data-kind='task']")
    assert page.locator("[data-id^='task.']").count() == 0
    page.keyboard.press('/')
    assert page.evaluate('document.activeElement.id') == 'search'


@pytest.mark.dbt
def test_business_terms(jaffle_built_graph: Path, open_page: Any) -> None:
    graph = jaffle_built_graph / 'konstruct-out' / 'graph.html'
    page = open_page(graph)
    assert '5\nbusiness terms' in page.inner_text('#stats')
    page.fill('#search', 'ltv')
    assert (
        page.eval_on_selector_all('#results .label', 'els => els.map(e => e.textContent)')[0]
        == 'Customer lifetime value'
    )
    page.press('#search', 'Enter')
    panel = page.inner_text('#panel')
    assert 'Customer lifetime value' in page.inner_text('#panel h2')
    assert 'Priya Shah, Head of Finance' in panel and 'one row per customer' in panel
    assert '= SUM(raw_payments.amount / 100)' in panel
    assert (
        page.locator(".row.on-path[data-col='model.jaffle_shop.customers#customer_lifetime_value']").count()
        == 1
    )
    assert page.evaluate('location.hash') == '#term.customer_lifetime_value'
    page.locator('#panel').get_by_role('button', name='Order amount', exact=True).click()
    assert 'Order amount' in page.inner_text('#panel h2')
    # A column shows the terms that define it.
    page.goto(graph.as_uri() + '#model.jaffle_shop.orders%23amount')
    page.wait_for_selector('#panel:not([hidden])')
    assert 'Defined by' in (page.locator('#panel').text_content() or '')
    page.locator('#panel').get_by_role('button', name='Order amount', exact=True).click()
    assert page.evaluate('location.hash') == '#term.order_amount'


@pytest.mark.dbt
def test_search(jaffle_built_graph: Path, open_page: Any) -> None:
    page = open_page(jaffle_built_graph / 'konstruct-out' / 'graph.html')
    page.fill('#search', 'lifetime')
    results = page.eval_on_selector_all('#results .label', 'els => els.map(e => e.textContent)')
    # The business term comes first, then the columns.
    assert results[:3] == [
        'Customer lifetime value',
        'customer_ltv.lifetime_value',
        'customers.customer_lifetime_value',
    ]
    assert page.inner_text('#match-count') == f'{len(results)} matches'
    page.press('#search', 'ArrowDown')
    page.press('#search', 'ArrowUp')
    page.press('#search', 'Enter')
    assert page.locator('#panel').is_visible()
    assert 'lifetime' in page.inner_text('#panel h2').lower()
    page.fill('#search', 'zzzz')
    assert page.inner_text('#match-count') == 'No matches'


def test_hostile_text_is_shown_as_text(tmp_path: Path, open_page: Any) -> None:
    graph = Graph()
    hostile = "</script><script>window.pwned = 1</script><img src=x onerror='window.pwned=2'>"
    node = graph.add_node(Node('model.p.evil', 'model', 'evil<b>', description=hostile, relation='db.s.evil'))
    graph.add_column(node, 'c', description=hostile)
    graph.warn(hostile)
    page = open_page(write_html(graph, tmp_path / 'graph.html'))
    page.click("[data-id='model.p.evil'] .name")
    assert hostile in page.inner_text('#panel')
    page.click('#warnings summary')
    assert hostile in page.inner_text('#warnings')
    assert page.evaluate('window.pwned') is None
    assert page.locator('#panel img').count() == 0


def test_a_large_graph_stays_responsive(tmp_path: Path, open_page: Any) -> None:
    graph = Graph()
    for i in range(400):
        node = graph.add_node(Node(f'model.p.m{i}', 'model', f'm{i}', relation=f'db.s.m{i}'))
        for j in range(8):
            graph.add_column(node, f'c{j}')
        for parent in (i // 2, i // 3):
            if parent != i:
                graph.add_edge(f'model.p.m{parent}', node.id, 'feeds')
                graph.add_edge(
                    column_id(node.id, 'c0'), column_id(f'model.p.m{parent}', 'c0'), 'derived_from'
                )
    started = time.perf_counter()
    page = open_page(write_html(graph, tmp_path / 'graph.html'))
    assert time.perf_counter() - started < 10
    assert page.locator('#nodes [data-id]').count() == 400
    clicked = time.perf_counter()
    page.click('#fit')
    page.locator("[data-id='model.p.m399']").click(force=True)
    # A large graph isolates the lineage of a selection automatically.
    assert page.locator('#isolate').get_attribute('aria-pressed') == 'true'
    assert page.locator('#nodes [data-id]').count() < 400
    page.locator('#panel table.columns').get_by_role('button', name='c0', exact=True).click()
    assert 'Comes from' in (page.locator('#panel').text_content() or '')
    assert time.perf_counter() - clicked < 5


def test_an_empty_graph(tmp_path: Path, browser: Any) -> None:
    page = browser.new_page()
    errors: list[str] = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(write_html(Graph(), tmp_path / 'graph.html').as_uri())
    assert page.locator('#empty').inner_text().strip()
    assert errors == []
    page.close()
