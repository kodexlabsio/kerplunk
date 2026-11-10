"""The map page in a real browser: WebGPU in Chromium, on SwiftShader's software Vulkan.

Skipped when Chromium is not available, like tests/test_browser.py. The page runs under the
viewer's Content-Security-Policy, so a violation shows up as a console error and fails the test.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from konstruct.core.graph import Graph, Node
from konstruct.outputs.html import write_html

from .test_browser import axe_violations

pytestmark = pytest.mark.browser

# WebGPU on SwiftShader: Vulkan through SwiftShader, and ANGLE on it, so the canvas can present.
WEBGPU = [
    '--enable-unsafe-webgpu',
    '--enable-features=Vulkan',
    '--use-vulkan=swiftshader',
    '--use-webgpu-adapter=swiftshader',
    '--use-angle=swiftshader',
]

# Function strings, not expressions: Playwright evaluates an expression with eval, which the CSP blocks.
FRAMES = '() => Number(document.querySelector("#map-stage")?.dataset.frames) > 0'

# Decodes a PNG in the page and counts its colours, and the share of pixels unlike the corner.
PIXELS = """async data => {
    const image = new Image();
    image.src = 'data:image/png;base64,' + data;
    await image.decode();
    const canvas = document.createElement('canvas');
    canvas.width = image.width;
    canvas.height = image.height;
    const context = canvas.getContext('2d');
    context.drawImage(image, 0, 0);
    const pixels = context.getImageData(0, 0, image.width, image.height).data;
    const colours = new Set();
    let different = 0;
    for (let i = 0; i < pixels.length; i += 4) {
        colours.add((pixels[i] << 16) | (pixels[i + 1] << 8) | pixels[i + 2]);
        if (Math.abs(pixels[i] - pixels[0]) + Math.abs(pixels[i + 1] - pixels[1]) + Math.abs(pixels[i + 2] - pixels[2]) > 24)
            different++;
    }
    return {colours: colours.size, share: different / (pixels.length / 4)};
}"""


@pytest.fixture(scope='module')
def gpu_browser() -> Iterator[Any]:
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        pytest.skip('playwright is not installed')
    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch(
                channel=os.environ.get('KONSTRUCT_BROWSER_CHANNEL') or None, args=WEBGPU
            )
        except Error as error:
            if os.environ.get('KONSTRUCT_REQUIRE_SERVICES'):
                raise
            pytest.skip(f'Chromium is not available: {str(error).splitlines()[0]}')
        yield instance
        instance.close()


@pytest.fixture
def open_map(gpu_browser: Any) -> Iterator[Any]:
    pages = []

    def opener(path: Path, fragment: str = '#/map', wait: bool = True, **options: Any) -> Any:
        options.setdefault('viewport', {'width': 1400, 'height': 900})
        options.setdefault('reduced_motion', 'reduce')
        context = gpu_browser.new_context(**options)
        page = context.new_page()
        page.problems = []
        page.on('pageerror', lambda error: page.problems.append(str(error)))
        page.on('console', lambda m: page.problems.append(m.text) if m.type in ('error', 'warning') else None)
        page.on(
            'request',
            lambda r: (
                page.problems.append(f'request {r.url}') if not r.url.startswith(('file:', 'data:')) else None
            ),
        )
        page.goto(path.as_uri() + fragment)
        page.wait_for_selector('#page h1')
        if wait:
            page.wait_for_function(FRAMES, timeout=120_000)
        pages.append(page)
        return page

    yield opener
    for page in pages:
        assert page.problems == [], page.problems
        page.context.close()


def pixels(page: Any) -> dict[str, float]:
    shot = page.locator('#map').screenshot()
    result: dict[str, float] = page.evaluate(PIXELS, base64.b64encode(shot).decode())
    return result


def wait_for_frame(page: Any, after: int) -> None:
    page.wait_for_function(
        'after => Number(document.querySelector("#map-stage").dataset.frames) > after', arg=after
    )


def frames(page: Any) -> int:
    return int(page.get_attribute('#map-stage', 'data-frames'))


@pytest.mark.dbt
def test_map_draws_the_project_and_opens_a_page(jaffle_built_graph: Path, open_map: Any) -> None:
    graph = jaffle_built_graph / 'konstruct-out' / 'graph.html'
    page = open_map(graph)
    assert page.title() == 'Map · jaffle_shop · Konstruct'
    stage = page.locator('#map-stage')
    assert stage.get_attribute('data-state') == 'ready'
    assert int(stage.get_attribute('data-gpu-bytes')) > 0
    # Cards, edges and the ground: many colours, and much of the canvas is not the ground.
    seen = pixels(page)
    assert seen['colours'] > 50 and seen['share'] > 0.02, seen

    # The same graph gives the same layout and the same pixels.
    again = open_map(graph)
    assert again.get_attribute('#map-stage', 'data-layout-hash') == stage.get_attribute('data-layout-hash')
    assert again.locator('#map').screenshot() == page.locator('#map').screenshot()

    # Search flies to a node (at once, with reduced motion) and focuses it; Enter opens its page.
    count = frames(page)
    page.fill('#map-find', 'orders')
    assert page.inner_text('#map-results [aria-selected="true"] .name') == 'orders'
    page.press('#map-find', 'Enter')
    wait_for_frame(page, count)
    assert page.evaluate('() => document.activeElement.id') == 'map'
    assert page.inner_text('#map-announce').startswith('orders, Model.')
    page.keyboard.press('Enter')
    page.wait_for_selector('#page h1:text-is("orders")')
    assert page.evaluate('() => location.hash') == '#/node/model.jaffle_shop.orders'
    # Back returns to the map with the node still focused.
    page.go_back()
    page.wait_for_function(FRAMES)
    assert page.evaluate('() => location.hash') == '#/map?focus=model.jaffle_shop.orders'
    page.wait_for_function('() => document.querySelector("#map-announce").textContent.startsWith("orders")')

    # A click on the focused card, now at the centre of the canvas, opens it or one of its columns.
    box = page.locator('#map').bounding_box()
    page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
    page.wait_for_function('() => location.hash.startsWith("#/node/model.jaffle_shop.orders")')


@pytest.mark.dbt
def test_map_keyboard_filters_and_list(jaffle_built_graph: Path, open_map: Any) -> None:
    page = open_map(jaffle_built_graph / 'konstruct-out' / 'graph.html')
    canvas = page.locator('#map')
    assert canvas.get_attribute('role') == 'application'
    assert canvas.get_attribute('aria-label') == 'Map of the project, 16 nodes'
    assert canvas.get_attribute('aria-describedby') == 'map-help'
    assert 'Enter opens the page' in page.inner_text('#map-help')
    page.focus('#map')
    page.keyboard.press('ArrowRight')
    first = page.inner_text('#map-announce')
    assert 'upstream' in first and 'downstream' in first
    # Up runs out at the top of the layer, and says so.
    for _ in range(20):
        page.keyboard.press('ArrowUp')
    assert page.inner_text('#map-announce') == 'Nothing further in this layer.'
    page.keyboard.press('ArrowDown')
    page.keyboard.press('Escape')
    assert page.inner_text('#map-announce') == ''

    # The map comes before the list in the tab order, and the list is one tab stop: the arrow keys
    # move between its rows, and between showing a node and opening its page.
    active = (
        '() => document.activeElement.closest("[data-row]")?.dataset.row ?? document.activeElement.tagName'
    )
    page.keyboard.press('Tab')
    assert page.evaluate(active) == 'SUMMARY'
    page.keyboard.press('Tab')
    assert page.evaluate('() => document.activeElement.classList.contains("locate")')
    assert page.evaluate(active).isdigit()
    page.keyboard.press('Home')
    page.wait_for_function(f'() => ({active})() === "0"')
    page.keyboard.press('ArrowDown')
    page.wait_for_function(f'() => ({active})() === "1"')
    page.keyboard.press('ArrowRight')
    page.wait_for_function('() => document.activeElement.tagName === "A"')
    assert page.evaluate(active) == '1'
    page.keyboard.press('Tab')
    assert page.evaluate('() => document.activeElement.closest(".map-scroller")') is None
    page.keyboard.press('Shift+Tab')
    assert page.evaluate(active) == '1'
    page.keyboard.press('End')
    page.wait_for_function(f'() => ({active})() === "15"')
    page.keyboard.press('Shift+Tab')
    page.keyboard.press('Shift+Tab')
    assert page.evaluate('() => document.activeElement.id') == 'map'

    # Hiding a kind takes it off the map and out of the list.
    before = page.locator('.map-scroller li').count()
    count = frames(page)
    page.click('.map-kinds button:has-text("Task")')
    wait_for_frame(page, count)
    assert page.get_attribute('.map-kinds button:has-text("Task")', 'aria-pressed') == 'false'
    assert page.locator('.map-scroller li').count() < before
    assert 'dbt_run_marts' not in page.inner_text('.map-scroller')
    shown = page.inner_text('.map-list .map-count')
    assert shown.endswith('of 16 nodes shown') and int(shown.split()[0]) < 16

    # The list locates a node on the map; its link opens the page.
    page.click('.map-scroller li:has-text("customers") button.locate')
    assert page.inner_text('#map-announce').startswith('customers, Model.')
    page.click('.map-scroller li:has-text("customers") a')
    page.wait_for_selector('#page h1:text-is("customers")')


@pytest.mark.dbt
def test_map_is_accessible_in_both_themes(jaffle_built_graph: Path, open_map: Any) -> None:
    graph = jaffle_built_graph / 'konstruct-out' / 'graph.html'
    shots = []
    for scheme in ('dark', 'light'):
        page = open_map(graph, color_scheme=scheme, bypass_csp=True)
        assert axe_violations(page) == [], scheme
        shots.append(page.locator('#map').screenshot())
    # The theme's tokens colour the map.
    assert shots[0] != shots[1]


def test_without_webgpu_the_map_says_so(tmp_path: Path, open_map: Any) -> None:
    graph = Graph()
    graph.add_node(Node('model.p.a', 'model', 'a', relation='db.s.a'))
    path = write_html(graph, tmp_path / 'graph.html')
    page = open_map(path, wait=False)
    page.add_init_script("Object.defineProperty(Navigator.prototype, 'gpu', {get: () => undefined})")
    page.reload()
    page.wait_for_selector('#map-unsupported')
    assert 'needs a browser with WebGPU' in page.inner_text('#map-unsupported')
    assert page.locator('#map').count() == 0
    # The rest of the viewer works as before, and the message leads to the lineage graph.
    page.click('#nav a[href="#/browse"]')
    page.wait_for_selector('#page h1:text-is("Browse")')
    page.go_back()
    page.click('#map-unsupported a:text-is("Open the lineage graph")')
    page.wait_for_function('() => location.hash === "#/lineage"')


# Every device reports a binding limit of 64 bytes, so the map's buffers cannot fit.
SMALL_LIMITS = """
const limits = Object.getOwnPropertyDescriptor(GPUDevice.prototype, 'limits').get;
Object.defineProperty(GPUDevice.prototype, 'limits', {
    get() {
        const real = limits.call(this);
        return new Proxy(real, {
            get: (target, key) => (key === 'maxStorageBufferBindingSize' ? 64 : Reflect.get(target, key)),
        });
    },
});
"""


def test_a_map_too_large_for_the_device_says_so(tmp_path: Path, open_map: Any) -> None:
    graph = Graph()
    for i in range(4):
        graph.add_node(Node(f'model.p.m{i}', 'model', f'm{i}', relation=f'db.s.m{i}'))
    path = write_html(graph, tmp_path / 'graph.html')
    page = open_map(path, wait=False)
    page.add_init_script(SMALL_LIMITS)
    page.reload()
    page.wait_for_selector('#map-stage[data-state="failed"]')
    message = page.inner_text('#map-stage [role="alert"]')
    assert message == (
        'The map could not be drawn: a buffer of 128 bytes is more than this graphics device allows (64 bytes).'
    )
    # The failure is logged as well as shown.
    assert any('128 bytes' in problem for problem in page.problems), page.problems
    page.problems.clear()


def test_the_map_reports_characters_the_atlas_has_no_room_for(tmp_path: Path, open_map: Any) -> None:
    # 6,000 distinct ideographs, ten to a name short enough to show whole, in names and again in
    # column names: more glyphs than the atlas holds.
    characters = [chr(0x4E00 + i) for i in range(6000)]
    graph = Graph()
    for i in range(600):
        text = ''.join(characters[i * 10 : (i + 1) * 10])
        graph.add_node(Node(f'model.p.m{i}', 'model', text, relation=f'db.s.m{i}'))
        graph.add_node(Node(f'model.p.m{i}#c', 'column', text, parent=f'model.p.m{i}'))
        graph.add_edge(f'model.p.m{i}', f'model.p.m{i}#c', 'contains')
    path = write_html(graph, tmp_path / 'graph.html')
    page = open_map(path)
    dropped = int(page.get_attribute('#map-stage', 'data-dropped-glyphs'))
    assert dropped > 0
    assert page.inner_text('#map-dropped') == (
        f'{dropped:,} characters are missing from the labels: the glyph atlas is full.'
    )
