"""Measure the map page (WebGPU) on a generated project, by default 2,000 models of 25 columns.

The project is the one tests/test_scale.py builds. The script builds it, opens graph.html#/map in
Chromium with WebGPU (by default on SwiftShader, Chromium's software Vulkan, so the numbers are
CPU numbers), and records:

* the layout: the lineage page's order of every node, building the scene and placing the cards,
  as the page measures them;
* the time to the first frame, from opening the map page and from loading the file;
* the frame time, from encoding a frame to the GPU finishing it, with the whole project in view
  and at reading zoom on one model;
* memory: the GPU buffers and atlas the map holds, and the JavaScript heap before and after;
* picking: the pointer-move handler, which picks with the map's CPU grid, against the least a GPU
  ID buffer costs per pick, one 1x1 copy and read back.

Usage:
    uv run .kodex/scripts/map_benchmark.py
    uv run .kodex/scripts/map_benchmark.py --models 500 --frames 30 --json map.json
    uv run .kodex/scripts/map_benchmark.py --gpu        # the machine's GPU instead of SwiftShader
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from konstruct import Project, __version__  # noqa: E402
from tests.test_map import WEBGPU  # noqa: E402
from tests.test_scale import make_project  # noqa: E402

READBACK = """async () => {
    const adapter = await navigator.gpu.requestAdapter();
    const device = await adapter.requestDevice();
    const texture = device.createTexture({size: [1, 1], format: 'r32uint',
        usage: GPUTextureUsage.RENDER_ATTACHMENT | GPUTextureUsage.COPY_SRC});
    const times = [];
    for (let i = 0; i < 50; i++) {
        const buffer = device.createBuffer({size: 256, usage: GPUBufferUsage.MAP_READ | GPUBufferUsage.COPY_DST});
        const started = performance.now();
        const encoder = device.createCommandEncoder();
        encoder.copyTextureToBuffer({texture}, {buffer, bytesPerRow: 256}, [1, 1]);
        device.queue.submit([encoder.finish()]);
        await buffer.mapAsync(GPUMapMode.READ);
        times.push(performance.now() - started);
        buffer.destroy();
    }
    device.destroy();
    times.sort((a, b) => a - b);
    return times[times.length >> 1];
}"""


# Pointer moves over the whole canvas: each one picks the card under it with the CPU grid.
HOVER = """() => {
    const canvas = document.querySelector('#map');
    const box = canvas.getBoundingClientRect();
    const moves = 2000;
    const started = performance.now();
    for (let i = 0; i < moves; i++) {
        const x = box.left + ((i * 37) % 997) / 997 * box.width;
        const y = box.top + ((i * 61) % 991) / 991 * box.height;
        canvas.dispatchEvent(new PointerEvent('pointermove', {clientX: x, clientY: y, pointerId: 1}));
    }
    return (performance.now() - started) / moves;
}"""


def heap(cdp: Any) -> float:
    metrics = cdp.send('Performance.getMetrics')['metrics']
    used: float = next(m['value'] for m in metrics if m['name'] == 'JSHeapUsedSize')
    return used / 2**20


def frames(page: Any) -> int:
    return int(page.get_attribute('#map-stage', 'data-frames'))


def next_frame(page: Any, after: int) -> float:
    page.wait_for_function(
        'after => Number(document.querySelector("#map-stage").dataset.frames) > after',
        arg=after,
        timeout=120_000,
    )
    return float(page.get_attribute('#map-stage', 'data-frame-ms'))


def timed_frames(page: Any, count: int) -> list[float]:
    times = []
    for i in range(count):
        n = frames(page)
        page.mouse.wheel(0, 1 if i % 2 else -1)
        times.append(next_frame(page, n))
    return times


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))]


def measure(graph: Path, models: int, count: int, gpu: bool) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    args = ['--enable-unsafe-webgpu'] if gpu else WEBGPU
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel=os.environ.get('KONSTRUCT_BROWSER_CHANNEL') or None, args=args
        )
        page = browser.new_page(viewport={'width': 1600, 'height': 1000}, reduced_motion='reduce')
        cdp = page.context.new_cdp_session(page)
        cdp.send('Performance.enable')
        page.goto(graph.as_uri())
        page.wait_for_selector('#page h1')
        before = heap(cdp)
        page.goto(graph.as_uri() + '#/map')
        page.wait_for_function(
            '() => Number(document.querySelector("#map-stage")?.dataset.frames) > 0', timeout=600_000
        )
        data = page.eval_on_selector('#map-stage', 'e => ({...e.dataset})')
        after = heap(cdp)
        box = page.locator('#map').bounding_box()
        assert box is not None
        centre = (box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
        page.mouse.move(*centre)
        page.wait_for_timeout(300)
        # The whole project in view: the fitted camera, nudged by the smallest wheel steps.
        whole = timed_frames(page, count)
        # Reading zoom: a model in the middle of the project, its column names readable.
        n = frames(page)
        page.fill('#map-find', f'm{models // 2:03}')
        page.press('#map-find', 'Enter')
        next_frame(page, n)
        page.mouse.move(*centre)
        reading = timed_frames(page, count)
        hover = page.evaluate(HOVER)
        readback = page.evaluate(READBACK)
        adapter = page.evaluate(
            '() => navigator.gpu.requestAdapter().then(a => a.info ? `${a.info.vendor} ${a.info.architecture}` : "")'
        )
        browser.close()
    return {
        'adapter': adapter,
        'layout_ms': int(data['layoutMs']),
        'order_ms': int(data['orderMs']),
        'atlas_ms': int(data['atlasMs']),
        'first_frame_from_map_ms': int(data['firstFrameMs']),
        'first_frame_from_load_ms': int(data['firstFrameAt']),
        'frame_ms_whole_median': round(statistics.median(whole), 2),
        'frame_ms_whole_p95': round(percentile(whole, 0.95), 2),
        'frame_ms_reading_median': round(statistics.median(reading), 2),
        'frame_ms_reading_p95': round(percentile(reading, 0.95), 2),
        'gpu_mib': round(int(data['gpuBytes']) / 2**20, 1),
        'heap_mib_before': round(before, 1),
        'heap_mib_after': round(after, 1),
        'hover_handler_us_mean': round(hover * 1000, 1),
        'gpu_readback_ms_median': round(readback, 3),
        'layout_hash': data['layoutHash'],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--models', type=int, default=2000)
    parser.add_argument('--columns', type=int, default=25)
    parser.add_argument('--frames', type=int, default=40, help='frames to time in each view')
    parser.add_argument('--gpu', action='store_true', help="use the machine's GPU, not SwiftShader")
    parser.add_argument('--json', type=Path, help='also write the results here')
    options = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='konstruct-map-') as directory:
        root = Path(directory)
        make_project(root, models=options.models, columns_per_model=options.columns)
        started = time.perf_counter()
        Project(root).build(run_dbt=False, warehouses=False)
        built = time.perf_counter() - started
        graph = root / 'konstruct-out' / 'graph.html'
        result: dict[str, Any] = {
            'konstruct': __version__,
            'machine': f'{platform.system()} {platform.machine()}, {os.cpu_count()} CPUs',
            'models': options.models,
            'columns': options.models * options.columns,
            'build_s': round(built, 1),
            'graph_html_mib': round(graph.stat().st_size / 2**20, 1),
            **measure(graph, options.models, options.frames, options.gpu),
        }
    width = max(len(k) for k in result)
    for key, value in result.items():
        print(f'{key:<{width}}  {value}')
    if options.json:
        options.json.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
