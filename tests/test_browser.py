"""graph.html in a real browser (Chromium through Playwright).

Skipped when Playwright's Chromium is not installed (`uv run playwright install chromium`);
CI installs it, and KONSTRUCT_REQUIRE_SERVICES turns the skip into a failure there.
"""

from __future__ import annotations

import importlib.util
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from konstruct.core.graph import Graph, Node, column_id
from konstruct.outputs.html import write_html

pytestmark = pytest.mark.browser

WCAG = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']


@pytest.fixture(scope='module')
def browser() -> Iterator[Any]:
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        pytest.skip('playwright is not installed')
    with sync_playwright() as playwright:
        try:
            # KONSTRUCT_BROWSER_CHANNEL=chrome uses an installed Google Chrome instead of Playwright's, and
            # KONSTRUCT_BROWSER_EXECUTABLE a Chromium binary Playwright did not download for this version.
            instance = playwright.chromium.launch(
                channel=os.environ.get('KONSTRUCT_BROWSER_CHANNEL') or None,
                executable_path=os.environ.get('KONSTRUCT_BROWSER_EXECUTABLE') or None,
            )
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
                page.problems.append(f'request {r.url}')
                if not r.url.startswith(('file:', 'data:', 'blob:'))
                else None
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


def axe_violations(page: Any) -> list[str]:
    """axe-core's WCAG 2.2 A and AA violations on the page as it stands."""
    # axe-playwright-python carries axe-core's script; it is read from the package, not fetched.
    spec = importlib.util.find_spec('axe_playwright_python')
    if spec is None or spec.origin is None:
        pytest.skip('axe-playwright-python is not installed')
    script = Path(spec.origin).parent / 'axe.min.js'
    page.add_script_tag(path=str(script))
    result = page.evaluate('tags => axe.run(document, {runOnly: {type: "tag", values: tags}})', WCAG)
    return [
        f'{v["id"]}: {", ".join(" ".join(n["target"]) for n in v["nodes"][:3])}' for v in result['violations']
    ]


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
    # Functions, not expressions: the page's policy refuses eval, which expressions need.
    node.wait_for_function("() => location.hash.startsWith('#/lineage?select=+customers+')")
    node.wait_for_selector('#page', state='detached')
    node.click("[data-direction='upstream']")
    node.wait_for_function("() => document.getElementById('select').value === '+customers'")
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


@pytest.mark.dbt
def test_wcag_aa_in_both_themes(jaffle_built_graph: Path, open_page: Any) -> None:
    graph = jaffle_built_graph / 'konstruct-out' / 'graph.html'
    for scheme in ('dark', 'light'):
        for fragment in (
            '#/',
            '#/node/model.jaffle_shop.orders',
            '#/node/model.jaffle_shop.orders%23amount',
            '#/node/term.customer_lifetime_value',
            '#/lineage?select=+customers+&focus=model.jaffle_shop.customers',
            '#/browse',
            '#/ontology',
        ):
            # axe-core is added as an inline script, which the page's own policy would refuse.
            page = open_page(graph, fragment, color_scheme=scheme, bypass_csp=True)
            assert axe_violations(page) == [], (scheme, fragment)
    page = open_page(graph, bypass_csp=True)
    page.keyboard.press('Control+k')
    page.keyboard.type('order')
    assert axe_violations(page) == []
    # At 200% zoom (a 700 by 450 window) the navigation folds away and nothing scrolls sideways.
    zoomed = open_page(
        graph, '#/node/model.jaffle_shop.orders', viewport={'width': 700, 'height': 450}, bypass_csp=True
    )
    assert zoomed.evaluate('document.documentElement.scrollWidth') <= 700
    assert not zoomed.locator('#nav').is_visible() or zoomed.locator('#nav').bounding_box()['x'] < 0
    assert axe_violations(zoomed) == []


def test_a_served_page_loads_the_graph_and_follows_changes(browser: Any) -> None:
    """Served by `konstruct serve`, the page fetches graph.json and reloads it in place when the
    server sends an event, keeping the page and the view."""
    import json
    import queue
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from konstruct.outputs.html import template

    graph = Graph()
    orders = graph.add_node(Node('model.p.orders', 'model', 'orders', relation='db.s.orders'))
    graph.add_column(orders, 'amount')
    state = {'graph': graph.to_dict()}
    events: queue.Queue[str] = queue.Queue()
    page_html = template().replace(
        '<meta charset="utf-8" />', '<meta charset="utf-8" />\n<meta name="konstruct-serve" content="" />', 1
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def send(self, body: bytes, kind: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == '/graph.json':
                self.send(json.dumps(state['graph']).encode(), 'application/json')
            elif self.path == '/events':
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                try:
                    while True:
                        self.wfile.write(f'data: {events.get(timeout=30)}\n\n'.encode())
                        self.wfile.flush()
                except (queue.Empty, OSError):
                    return
            elif self.path == '/':
                self.send(page_html.encode(), 'text/html; charset=utf-8')
            else:
                self.send(b'', 'text/plain', 404)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        problems: list[str] = []
        page.on('pageerror', lambda error: problems.append(str(error)))
        page.on('console', lambda m: problems.append(m.text) if m.type in ('error', 'warning') else None)
        page.goto(f'http://127.0.0.1:{server.server_address[1]}/#/node/model.p.orders')
        page.wait_for_selector('#page h1:text-is("orders")')
        page.wait_for_selector("#nodes [data-id='model.p.orders']")
        more = graph.add_node(Node('model.p.revenue', 'model', 'revenue', relation='db.s.revenue'))
        graph.add_edge(orders.id, more.id, 'feeds')
        state['graph'] = graph.to_dict()
        events.put('changed')
        page.wait_for_selector("#nodes [data-id='model.p.revenue']")
        assert page.inner_text('#page h1') == 'orders'
        assert page.evaluate('location.hash') == '#/node/model.p.orders'
        # explain.json is optional: the server here has none.
        assert [p for p in problems if '404' not in p] == []
        page.close()
    finally:
        server.shutdown()


@pytest.mark.dbt
def test_business_term_page(jaffle_built_graph: Path, open_page: Any) -> None:
    page = open_page(
        jaffle_built_graph / 'konstruct-out' / 'graph.html', '#/node/term.customer_lifetime_value'
    )
    assert page.inner_text('#page h1') == 'Customer lifetime value'
    text = page.inner_text('#page')
    assert 'Priya Shah, Head of Finance' in text
    assert 'one row per customer' in text
    assert 'SUM(raw_payments.amount / 100)' in text
    assert 'Approved' in text and 'LTV' in text
    related = page.text_content('#page') or ''
    assert 'Related terms' in related and 'Order amount' in related
    # The bound column's path is drawn on the docked graph.
    page.wait_for_selector(".row.on-path[data-col='model.jaffle_shop.customers#customer_lifetime_value']")
    page.locator('#page').get_by_role('link', name='Order amount', exact=True).first.click()
    page.wait_for_selector('#page h1:text-is("Order amount")')
    assert page.evaluate('location.hash') == '#/node/term.order_amount'
    # The overview counts the business terms.
    graph = jaffle_built_graph / 'konstruct-out' / 'graph.html'
    page.goto(graph.as_uri() + '#/')
    page.wait_for_selector('#stats')
    assert page.locator('#stats a[href="#/glossary"]').inner_text().split() == ['5', 'business', 'terms']
    # A column shows the terms that define it, each a link to the term's page.
    page.goto(graph.as_uri() + '#/node/model.jaffle_shop.orders%23amount')
    page.wait_for_selector('#page h1:text-is("amount")')
    # Headings are shown in capitals; the text is as written.
    assert 'Defined by' in (page.text_content('#page') or '')
    page.locator('#page').get_by_role('link', name='Order amount', exact=True).first.click()
    page.wait_for_selector('#page h1:text-is("Order amount")')
    assert page.evaluate('location.hash') == '#/node/term.order_amount'


@pytest.mark.dbt
def test_markdown_descriptions(jaffle_built_graph: Path, open_page: Any) -> None:
    graph = jaffle_built_graph / 'konstruct-out' / 'graph.html'
    page = open_page(graph)
    # The dbt project's {% docs __overview__ %}, rendered as Markdown with its links.
    assert 'Data Documentation for Jaffle Shop' in page.inner_text('.about')
    assert page.get_attribute('.about a:text-is("dbt")', 'href') == 'https://www.getdbt.com/'
    # A column description's Markdown table is a table.
    page.goto(graph.as_uri() + '#/node/model.jaffle_shop.orders')
    page.wait_for_selector('#page h1:text-is("orders")')
    status = page.locator('#col-table tr[data-column="model.jaffle_shop.orders#status"] .markdown table')
    assert status.count() == 1 and 'placed' in status.inner_text()


def test_deep_quotes_and_long_descriptions(tmp_path: Path, open_page: Any) -> None:
    graph = Graph()
    graph.add_node(Node('model.p.deep', 'model', 'deep', description='>' * 10_000, relation='db.s.deep'))
    graph.add_node(Node('model.p.long', 'model', 'long', description='word ' * 30_000, relation='db.s.long'))
    path = write_html(graph, tmp_path / 'graph.html')
    # Quotes nest eight deep, and the rest is text, rather than overflowing the stack.
    page = open_page(path, '#/node/model.p.deep')
    assert page.locator('#page .markdown blockquote').count() == 8
    assert '>' * 9_992 in page.inner_text('#page .markdown')
    # A description longer than the viewer shows is cut, with a note.
    page.goto(path.as_uri() + '#/node/model.p.long')
    page.wait_for_selector('#page h1:text-is("long")')
    assert 'The description is long, so only its start is shown.' in page.inner_text('#page')
    assert len(page.inner_text('#page .markdown')) < 101_000


@pytest.mark.dbt
def test_selector_errors_and_unknown_addresses(jaffle_built_graph: Path, open_page: Any) -> None:
    graph = jaffle_built_graph / 'konstruct-out' / 'graph.html'
    page = open_page(graph, '#/lineage')
    # Selector mistakes are reported one by one, in the reader's language.
    page.fill('#select', 'colour:red tag:')
    page.press('#select', 'Enter')
    errors = page.locator('.selector-error')
    errors.first.wait_for()
    assert errors.all_inner_texts() == [
        'Unknown selector method "colour:" in "colour:red"',
        '"tag:" needs a value',
    ]
    page.goto(graph.as_uri() + '#/bogus')
    page.wait_for_selector('#page h1:text-is("Not found")')


@pytest.mark.dbt
def test_narrow_and_zoomed_windows(jaffle_built_graph: Path, open_page: Any) -> None:
    graph = jaffle_built_graph / 'konstruct-out' / 'graph.html'
    # Full screen lineage uses the whole width, even where the navigation folds away.
    for width, height in ((960, 540), (640, 450)):
        page = open_page(graph, '#/lineage?select=+orders+', viewport={'width': width, 'height': height})
        box = page.locator('#select').bounding_box()
        assert box is not None and box['width'] > 100, (width, box)
        graph_box = page.locator('#graph-box').bounding_box()
        assert graph_box is not None and graph_box['width'] > width / 2
    # At 320 pixels (a phone, or 640 at 200% zoom) every top bar control is on screen.
    page = open_page(graph, '#/node/model.jaffle_shop.orders', viewport={'width': 320, 'height': 640})
    for control in page.locator('#topbar button:visible, #topbar a:visible, #topbar select:visible').all():
        box = control.bounding_box()
        assert box is not None and box['x'] >= 0 and box['x'] + box['width'] <= 320, control.evaluate(
            'e => e.outerHTML.slice(0, 80)'
        )
    assert page.evaluate('document.documentElement.scrollWidth') <= 320


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


FIRST_FRAME = '() => Number(document.querySelector("#world-stage")?.dataset.firstFrameMs) > 0'


def world_focus(page: Any) -> str:
    """The aria-label of the map's node that has focus, or '' when focus is elsewhere."""
    label: str = page.evaluate(
        '() => document.activeElement?.closest("#world") ? document.activeElement.getAttribute("aria-label") : ""'
    )
    return label


@pytest.mark.dbt
def test_the_world_map(jaffle_built_graph: Path, open_page: Any) -> None:
    graph = jaffle_built_graph / 'konstruct-out' / 'graph.html'
    page = open_page(graph)
    page.click('#nav a[href="#/world"]')
    page.wait_for_function(FIRST_FRAME)
    assert page.title() == 'World · jaffle_shop · Konstruct'
    assert page.get_attribute('#world-stage', 'data-entities') == '3'
    labels = page.eval_on_selector_all(
        '#world [data-node]', 'es => es.map(e => e.getAttribute("aria-label"))'
    )
    assert sorted(label.split(':')[0] for label in labels) == ['customer', 'order', 'payment']
    order = next(label for label in labels if label.startswith('order:'))
    assert order.startswith('order: Declared, ') and order.endswith(' metrics')
    # The relationships carry their cardinality at each end: many orders to one customer.
    ends = page.eval_on_selector_all('#world .end text', 'es => es.map(e => e.textContent)')
    assert sorted(ends) == ['1', '1', 'N', 'N']

    # The keyboard: one tab stop on the map, the arrow keys between entities, Enter to show one.
    page.focus('#page h1')
    for _ in range(10):
        page.keyboard.press('Tab')
        if world_focus(page):
            break
    first = world_focus(page)
    assert first
    page.keyboard.press('ArrowRight')
    page.wait_for_function('first => document.activeElement.getAttribute("aria-label") !== first', arg=first)
    page.keyboard.press('Enter')
    name = world_focus(page).split(':')[0]
    page.wait_for_selector(f'#world-panel-title:text-is("{name}")')
    assert f'focus=entity:{name}' in page.evaluate('location.hash') and world_focus(page)
    page.keyboard.press('Escape')
    page.wait_for_function('() => location.hash === "#/world"')

    # A proposal is proposed and inferred, and never approved; a definition keeps its status and owner.
    page.goto(graph.as_uri() + '#/world?focus=metric:order/order count')
    page.wait_for_selector('#world-panel-title:text-is("order count")')
    panel = page.inner_text('.world-panel')
    assert 'Proposed' in panel and 'Inferred' in panel and 'Approved' not in panel
    assert 'COUNT(DISTINCT orders.order_id)' in panel
    assert 'The count of each order, by its key.' in panel
    page.click('.world-panel a.back')
    page.wait_for_selector('#world-panel-title:text-is("order")')
    page.click('.world-panel a.row-link:text-is("Order amount")')
    page.wait_for_selector('#world-panel-title:text-is("Order amount")')
    panel = page.inner_text('.world-panel')
    assert 'Approved' in panel and 'Priya Shah' in panel and 'Proposed' not in panel
    assert page.evaluate('document.activeElement.id') == 'world-panel-title'

    for scheme in ('dark', 'light'):
        for fragment in ('#/world', '#/world?focus=entity:order', '#/world?focus=metric:order/order count'):
            checked = open_page(graph, fragment, color_scheme=scheme, bypass_csp=True)
            checked.wait_for_function(FIRST_FRAME)
            assert axe_violations(checked) == [], (scheme, fragment)


def test_the_world_map_marks_a_failed_check_and_reads_an_older_page(tmp_path: Path, open_page: Any) -> None:
    from konstruct.outputs.html import viewer_data

    from .test_world import _shop

    graph = _shop()
    key = 'model.shop.customers#customer_id'
    graph.meta['integrity'] = {
        'keys': {key: {'state': 'failed', 'detail': 'customers.customer_id has 2 nulls'}}
    }
    page = open_page(write_html(graph, tmp_path / 'graph.html'), '#/world')
    page.wait_for_function(FIRST_FRAME)
    customer = page.locator('#world .node.t-failed')
    assert customer.get_attribute('aria-label').startswith('customer: Check failed, ')
    # Not by colour alone: the node carries the warning triangle, and the key names it.
    assert customer.locator('.mark').count() == 1
    assert 'Check failed' in page.inner_text('.world-legend')
    page.click('.world-panel a.row-link:has-text("customer")')
    page.wait_for_selector('#world-panel-title:text-is("customer")')
    assert 'customers.customer_id has 2 nulls' in page.inner_text('.world-panel')

    # A page written before Konstruct recorded trust has no world model and no confidence.
    data = viewer_data(graph, tmp_path)
    del data['world']
    for kind in ('entities', 'measures', 'relationships'):
        for item in data['ontology'][kind]:
            item.pop('confidence', None)
            item.pop('check', None)
    older = open_page(write_html(graph, tmp_path / 'older.html', data=data), '#/world')
    older.wait_for_function(FIRST_FRAME)
    assert older.locator('.world-legend li').all_inner_texts()[0].startswith('Not rated')
    assert older.locator('.world-legend li').count() == 1
    # Not rated is drawn apart from inferred: its own edge style, as its own ring.
    assert older.locator('#world .edge.t-unrated').count() >= 1
    assert older.locator('#world .edge.t-inferred').count() == 0
    assert 'written by an earlier Konstruct' in older.inner_text('.world-panel')
    assert older.locator('#world [data-node]').count() == page.locator('#world [data-node]').count()


def test_a_large_world_draws_names_only_once_they_can_be_read(tmp_path: Path, open_page: Any) -> None:
    graph = Graph()
    for i in range(400):
        node = graph.add_node(Node(f'model.p.m{i}', 'model', f'm{i}', relation=f'db.s.m{i}'))
        node.attrs['primary_key'] = ['c0']
        graph.add_column(node, 'c0')
    page = open_page(write_html(graph, tmp_path / 'graph.html'), '#/world')
    page.wait_for_function(FIRST_FRAME)
    assert page.get_attribute('#world-stage', 'data-entities') == '400'
    assert page.locator('#world .label').count() < 400
    # They are in no relationship, so they are one block under a heading that counts them.
    assert page.locator('#world .loners text').text_content() == '400 entities in no relationship'
    # The panel lists the first hundred and finds the rest by name.
    assert page.locator('.world-panel li').count() <= 200
    page.fill('#world-find', 'm399')
    page.click('.world-panel a.row-link:text-is("m399")')
    page.wait_for_selector('#world-panel-title:text-is("m399")')
    page.wait_for_selector('#world .node.selected .label:text-is("m399")')


def _two_lifetimes() -> Graph:
    """The ontology tests' shop with a second relation of customer grain whose lifetime is also a sum:
    two proposals named 'customer lifetime' for the entity customer."""
    from .test_ontology import _relation
    from .test_world import _shop

    graph = _shop()
    summary = _relation(graph, 'customer_summary', ['customer_id', 'lifetime'])
    summary.attrs['conditions'] = ['GROUP BY: db.main.customer_summary.customer_id']
    lifetime = graph.nodes[f'{summary.id}#lifetime']
    lifetime.attrs['expression'] = 'SUM(db.main.payments.amount)'
    graph.add_edge(lifetime.id, 'model.shop.payments#amount', 'derived_from', transform='transformed')
    return graph


def test_world_metrics_of_one_name_and_entity_each_have_a_place(tmp_path: Path, open_page: Any) -> None:
    page = open_page(write_html(_two_lifetimes(), tmp_path / 'graph.html'), '#/world')
    page.wait_for_function(FIRST_FRAME)
    links = page.locator('.world-panel a.row-link:text-is("customer lifetime")')
    assert links.count() == 2
    hrefs = [links.nth(i).get_attribute('href') for i in range(2)]
    assert len(set(hrefs)) == 2
    shown = []
    for href in hrefs:
        page.goto(page.url.split('#')[0] + href)
        page.wait_for_selector('#world-panel-title:text-is("customer lifetime")')
        shown.append(page.inner_text('.world-panel .links'))
    assert sorted(shown) == ['customer_summary.lifetime', 'customers.lifetime']


def test_world_measures_of_one_name_in_two_views_each_have_a_place(tmp_path: Path, open_page: Any) -> None:
    from .test_world import _shop

    graph = _shop()
    for view in ('orders', 'refunds'):
        graph.add_node(
            Node(
                f'exposure.looker.{view}',
                'exposure',
                view,
                path=f'views/{view}.view.lkml',
                origins=['looker'],
                attrs={'type': 'view', 'measures': {'count': 'COUNT(*)', 'total': 'SUM(amount)'}},
            )
        )
    page = open_page(write_html(graph, tmp_path / 'graph.html'), '#/world')
    page.wait_for_function(FIRST_FRAME)
    # Measures with no entity have no place on the map, so the panel lists them under their own heading.
    assert page.locator('.world-panel h3:has-text("Metrics with no entity")').count() == 1
    for name in ('count', 'total'):
        page.goto(page.url.split('#')[0] + '#/world')
        page.wait_for_selector('#world-find')
        links = page.locator(f'.world-panel a.row-link:text-is("{name}")')
        assert links.count() == 2
        hrefs = {links.nth(i).get_attribute('href') for i in range(2)}
        assert len(hrefs) == 2
        paths = []
        for href in sorted(hrefs):
            page.goto(page.url.split('#')[0] + href)
            page.wait_for_selector(f'#world-panel-title:text-is("{name}")')
            paths.append(page.inner_text('.world-panel .details'))
        assert any('views/orders.view.lkml' in p for p in paths)
        assert any('views/refunds.view.lkml' in p for p in paths)


def _views_in_one_file(*views: str) -> Graph:
    """The ontology tests' shop with Looker views, all in one file, each with a measure count."""
    from .test_world import _shop

    graph = _shop()
    for view in views:
        graph.add_node(
            Node(
                f'exposure.looker.{view}',
                'exposure',
                view,
                path='views/all.view.lkml',
                origins=['looker'],
                attrs={'type': 'view', 'measures': {'count': 'COUNT(*)'}},
            )
        )
    return graph


def test_a_world_metrics_address_stays_with_it_when_a_view_is_added(tmp_path: Path, open_page: Any) -> None:
    page = open_page(write_html(_views_in_one_file('orders', 'refunds'), tmp_path / 'two.html'), '#/world')
    page.wait_for_function(FIRST_FRAME)
    rows = page.locator('.world-panel li:has(a.row-link:text-is("count"))')
    assert rows.count() == 2
    # Each row names the view that defines its count.
    hints = sorted(rows.nth(i).locator('.hint').inner_text() for i in range(2))
    assert hints == ['orders · views/all.view.lkml', 'refunds · views/all.view.lkml']
    refunds = rows.filter(has_text='refunds').locator('a.row-link').get_attribute('href')
    # A view added to the top of the same file leaves the address opening the same view's count.
    later = write_html(_views_in_one_file('accounts', 'orders', 'refunds'), tmp_path / 'three.html')
    page.goto(later.as_uri() + refunds)
    page.wait_for_selector('#world-panel-title:text-is("count")')
    assert 'Looker view refunds' in page.inner_text('.world-panel')


def test_a_world_with_no_entities_says_so(tmp_path: Path, open_page: Any) -> None:
    graph = Graph()
    graph.add_node(
        Node(
            'exposure.looker.orders',
            'exposure',
            'orders',
            path='views/orders.view.lkml',
            origins=['looker'],
            attrs={'type': 'view', 'measures': {'count': 'COUNT(*)'}},
        )
    )
    page = open_page(write_html(graph, tmp_path / 'graph.html'), '#/world')
    page.wait_for_function(FIRST_FRAME)
    assert 'No entities found' in page.inner_text('#world-stage')
    assert page.locator('.world-legend').count() == 0
    assert page.locator('.world-panel a.row-link:text-is("count")').count() == 1
