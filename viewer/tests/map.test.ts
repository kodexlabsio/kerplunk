// The map's pure parts: the scene, placement, buffer packing, the camera, picking, colours, the
// distance field, text setting, keyboard moves, and the renderer's checks against a device's limits.

import { afterEach, describe, expect, it, vi } from 'vitest';
import { between, fit, focusOn, inView, pan, toScreen, toWorld, zoomAt } from '../src/gpu/camera';
import { kindSlot, parseColour } from '../src/gpu/colour';
import { DOWNSTREAM, FOCUS, HIDDEN, statesFor, step, summarise, UPSTREAM } from '../src/gpu/navigate';
import {
    EDGE_WORDS,
    GLYPH_WORDS,
    glyphsAt,
    labelsOf,
    NAME_MIN_PX,
    NAME_SIZE,
    NODE_WORDS,
    packEdges,
    packGlyphs,
    packNodes,
    ROW_MIN_PX,
    ROW_SIZE,
    TEXT_WIDTH,
} from '../src/gpu/pack';
import { PickGrid } from '../src/gpu/pick';
import { boundsOf, cardHeight, layoutHash, MAX_ROWS, placeCards, rowsOf } from '../src/gpu/place';
import { checked, DeviceError, limitsOf, MapRenderer, TooLarge, type Content } from '../src/gpu/renderer';
import { buildScene, type Scene } from '../src/gpu/scene';
import { charactersOf, distanceField, glyphKey, setLine, type Glyph } from '../src/gpu/text';
import { minifyWgsl } from '../src/gpu/wgsl';
import { computeOrder, HEAD, W } from '../src/lib/layout';
import { byName, GraphModel } from '../src/lib/model';
import { fitText, FONT_NAME, FONT_ROW } from '../src/lib/text';
import type { GraphData, GraphNode } from '../src/lib/types';
import { graph } from './fixture';

/** A layered project like tests/test_scale.py's: model i reads models i / 2 and i / 3. */
function project(models: number, columns = 3): GraphData {
    const nodes: GraphNode[] = [];
    const edges: GraphData['edges'] = [];
    for (let i = 0; i < models; i++) {
        const id = `model.p.m${i}`;
        nodes.push({
            id,
            kind: 'model',
            name: `m${String(i).padStart(4, '0')}`,
            attrs: { package: i % 2 ? 'a' : 'b' },
        });
        for (let c = 0; c < columns; c++) {
            nodes.push({ id: `${id}#c${c}`, kind: 'column', name: `c${c}`, parent: id });
            edges.push({ source: id, target: `${id}#c${c}`, kind: 'contains' });
        }
        if (i >= 3)
            for (const p of new Set([Math.floor(i / 2), Math.floor(i / 3)]))
                edges.push({ source: `model.p.m${p}`, target: id, kind: 'feeds' });
    }
    return { nodes, edges };
}

/** The scene of every node, in the lineage page's layout. */
function sceneOf(model: GraphModel): Scene {
    return buildScene(model, computeOrder(model, model.inView));
}

function random(seed: number): () => number {
    let s = seed;
    return () => (s = (Math.imul(s, 1103515245) + 12345) >>> 0) / 2 ** 32;
}

describe('scene', () => {
    const model = new GraphModel(graph);
    const scene = sceneOf(model);

    it('holds every relation and task in name order, with the edges between them', () => {
        expect(scene.ids).toEqual(
            model.viewNodes
                .slice()
                .sort(byName)
                .map(n => n.id),
        );
        expect(scene.edgeSource.length).toBe(model.layoutEdges.length);
        model.layoutEdges.forEach((e, k) => {
            expect(scene.ids[scene.edgeSource[k]!]).toBe(e.source);
            expect(scene.ids[scene.edgeTarget[k]!]).toBe(e.target);
            expect(scene.edgeRuns[k]).toBe(e.kind === 'runs' ? 1 : 0);
        });
    });

    it('counts each card’s edges both ways', () => {
        scene.ids.forEach((id, i) =>
            expect(scene.degree[i]).toBe(
                model.layoutEdges.filter(e => e.source === id).length +
                    model.layoutEdges.filter(e => e.target === id).length,
            ),
        );
    });

    it('takes its layers and their order from the lineage page’s layout', () => {
        const big = new GraphModel(project(60));
        const order = computeOrder(big, big.inView);
        const s = buildScene(big, order);
        expect(s.order.map(row => row.map(i => s.ids[i]))).toEqual(order);
        expect(s.order.flat().sort((a, b) => a - b)).toEqual(s.ids.map((_, i) => i));
        s.order.forEach((row, l) =>
            row.forEach((i, r) => {
                expect(s.layer[i]).toBe(l);
                expect(s.slot[i]).toBe(r);
            }),
        );
        for (let k = 0; k < s.edgeSource.length; k++)
            expect(s.layer[s.edgeSource[k]!]!).toBeLessThan(s.layer[s.edgeTarget[k]!]!);
    });
});

describe('placement and packing', () => {
    const scene = sceneOf(new GraphModel(project(40, 50)));
    const order = scene.order;
    const rects = placeCards(scene, order);

    it('sizes cards by their rows, with long lists cut', () => {
        expect(rowsOf(3)).toBe(3);
        expect(rowsOf(MAX_ROWS + 5)).toBe(MAX_ROWS + 1);
        expect(cardHeight(0)).toBe(HEAD);
        expect(rects[3]).toBe(cardHeight(50));
    });

    it('places cards without overlaps, in their layers from left to right', () => {
        const n = scene.ids.length;
        for (let a = 0; a < n; a++)
            for (let b = a + 1; b < n; b++) {
                const apart =
                    rects[a * 4]! + W <= rects[b * 4]! ||
                    rects[b * 4]! + W <= rects[a * 4]! ||
                    rects[a * 4 + 1]! + rects[a * 4 + 3]! <= rects[b * 4 + 1]! ||
                    rects[b * 4 + 1]! + rects[b * 4 + 3]! <= rects[a * 4 + 1]!;
                expect(apart).toBe(true);
                if (scene.layer[a]! < scene.layer[b]!) expect(rects[a * 4]!).toBeLessThan(rects[b * 4]!);
            }
    });

    it('fingerprints a layout', () => {
        expect(layoutHash(rects)).toBe(layoutHash(placeCards(scene, order)));
        const moved = rects.slice();
        moved[0]! += 1;
        expect(layoutHash(moved)).not.toBe(layoutHash(rects));
        expect(boundsOf(new Float32Array(0))).toBeNull();
    });

    it('packs cards, edges and glyphs in their strides', () => {
        const nodes = packNodes(scene, rects);
        expect(nodes.byteLength).toBe(scene.ids.length * NODE_WORDS * 4);
        const f = new Float32Array(nodes);
        const u = new Uint32Array(nodes);
        expect([...f.subarray(NODE_WORDS, NODE_WORDS + 4)]).toEqual([...rects.subarray(4, 8)]);
        expect(u[4]).toBe(kindSlot('model'));
        expect(u[5]).toBe(MAX_ROWS + 1);
        expect(u[6]).toBe(50);
        const edges = packEdges(scene);
        expect(edges.length).toBe(scene.edgeSource.length * EDGE_WORDS);
        expect([edges[0], edges[1]]).toEqual([scene.edgeSource[0], scene.edgeTarget[0]]);

        const labels = labelsOf(scene, n => `+${n}`);
        expect(labels[0]!.length).toBe(MAX_ROWS + 2);
        expect(labels[0]!.at(-1)).toEqual([1, '+10']);
        const glyphs = new Map<string, Glyph>();
        for (const [face, char] of charactersOf(labels.flat()))
            glyphs.set(glyphKey(face, char), { advance: 0.5, index: char === ' ' ? -1 : glyphs.size });
        const { data: packed, names } = packGlyphs(labels, glyphs);
        const count = labels.flat().reduce((sum, [, text]) => sum + [...text].length, 0);
        expect(packed.length).toBe(count * GLYPH_WORDS);
        expect(names).toBe(labels.reduce((sum, list) => sum + [...list[0]![1]].length, 0));
        const pf = new Float32Array(packed.buffer);
        expect(pf[0]).toBe(18);
        expect(packed[2]! >>> 24).toBe(0);
        expect(packed[3]).toBe(0);
        // The second glyph of the name sits one advance (half an em at 15 units) further on.
        expect(pf[GLYPH_WORDS]).toBeCloseTo(18 + 7.5);
        // Every name comes before every column row.
        for (let g = 0; g < count; g++) expect(packed[g * GLYPH_WORDS + 2]! >>> 24).toBe(g < names ? 0 : 1);
    });

    it('draws only the glyphs large enough to read', () => {
        const glyphs = { total: 100, names: 10 };
        expect(glyphsAt(1, glyphs, true)).toBe(100);
        expect(glyphsAt(ROW_MIN_PX / ROW_SIZE - 0.01, glyphs, true)).toBe(10);
        expect(glyphsAt(NAME_MIN_PX / NAME_SIZE - 0.01, glyphs, true)).toBe(0);
        expect(glyphsAt(1, glyphs, false)).toBe(0);
    });
});

describe('camera', () => {
    const size = { w: 800, h: 600 };

    it('maps between screen and world', () => {
        const c = { x: 100, y: 50, k: 2 };
        expect(toScreen(c, size, 100, 50)).toEqual([400, 300]);
        const [x, y] = toWorld(c, size, 123, 456);
        expect(toScreen(c, size, x, y)).toEqual([123, 456]);
    });

    it('zooms about the cursor and pans by screen pixels', () => {
        const c = { x: 10, y: 20, k: 1 };
        const before = toWorld(c, size, 600, 100);
        const z = zoomAt(c, size, 600, 100, 1.7);
        expect(z.k).toBeCloseTo(1.7);
        const after = toWorld(z, size, 600, 100);
        expect(after[0]).toBeCloseTo(before[0]);
        expect(after[1]).toBeCloseTo(before[1]);
        expect(zoomAt(c, size, 0, 0, 1e9).k).toBe(4);
        expect(pan(c, 10, -20)).toEqual({ x: 0, y: 40, k: 1 });
    });

    it('fits a box with a margin and brings a card into view', () => {
        const box = { x: 0, y: 0, w: 2000, h: 1000 };
        const f = fit(box, size, 32);
        expect(f).toEqual({ x: 1000, y: 500, k: (800 - 64) / 2000 });
        expect(inView(f, size, box)).toBe(true);
        expect(fit(null, size)).toEqual({ x: 0, y: 0, k: 1 });
        const card = { x: 1500, y: 800, w: 268, h: 46 };
        const near = focusOn(f, size, card);
        expect(inView(near, size, card)).toBe(true);
        expect(near.k).toBeGreaterThan(f.k);
        expect(focusOn({ ...f, k: 3 }, size, card).k).toBe(3);
    });

    it('flies from one camera to another', () => {
        const a = { x: 0, y: 0, k: 0.1 };
        const b = { x: 100, y: 200, k: 1 };
        expect(between(a, b, 0)).toEqual(a);
        expect(between(a, b, 1)).toEqual(b);
        const mid = between(a, b, 0.5);
        expect(mid.k).toBeCloseTo(Math.sqrt(0.1));
        expect(mid.x).toBeCloseTo(50);
    });
});

describe('picking', () => {
    it('finds the same card as a search of every card', () => {
        const scene = sceneOf(new GraphModel(project(300, 4)));
        const rects = placeCards(scene, scene.order);
        const grid = new PickGrid(rects);
        const bounds = boundsOf(rects)!;
        const next = random(3);
        for (let q = 0; q < 2000; q++) {
            const x = bounds.x - 50 + next() * (bounds.w + 100);
            const y = bounds.y - 50 + next() * (bounds.h + 100);
            let brute = -1;
            for (let i = 0; i < scene.ids.length; i++)
                if (
                    x >= rects[i * 4]! &&
                    y >= rects[i * 4 + 1]! &&
                    x <= rects[i * 4]! + rects[i * 4 + 2]! &&
                    y <= rects[i * 4 + 1]! + rects[i * 4 + 3]!
                )
                    brute = i;
            expect(grid.at(x, y)).toBe(brute);
        }
    });

    it('tells the head from the rows', () => {
        const rects = new Float32Array([0, 0, W, cardHeight(3)]);
        const grid = new PickGrid(rects);
        expect(grid.hit(10, 10, () => 3)).toEqual({ node: 0, row: -1 });
        expect(grid.hit(10, HEAD + 6 + 28 * 1.5, () => 3)).toEqual({ node: 0, row: 1 });
        expect(grid.hit(-1, 10, () => 3)).toBeNull();
        expect(new PickGrid(new Float32Array(0)).at(0, 0)).toBe(-1);
    });
});

describe('colours', () => {
    it('reads the forms a browser gives', () => {
        expect(parseColour('#7dc3ff')).toEqual([0x7d / 255, 0xc3 / 255, 1, 1]);
        expect(parseColour('#fff')).toEqual([1, 1, 1, 1]);
        expect(parseColour('rgba(255, 255, 255, 0.42)')).toEqual([1, 1, 1, 0.42]);
        expect(parseColour('rgb(31 42 46 / 50%)')).toEqual([31 / 255, 42 / 255, 46 / 255, 0.5]);
        expect(parseColour('color(srgb 0.5 0.25 1 / 0.8)')).toEqual([0.5, 0.25, 1, 0.8]);
        expect(() => parseColour('oklch(0.7 0.1 200)')).toThrow();
        expect(() => parseColour('rgb(a, b, c)')).toThrow();
    });

    it('colours kinds as the lineage page does', () => {
        expect(kindSlot('model')).toBe(kindSlot('snapshot'));
        expect(kindSlot('source')).toBe(kindSlot('seed'));
        expect(kindSlot('dag')).toBe(6);
    });
});

describe('text', () => {
    it('makes a signed distance field that crosses one half on the outline', () => {
        const w = 40;
        const h = 40;
        const alpha = new Uint8Array(w * h);
        for (let y = 10; y < 30; y++) for (let x = 10; x < 30; x++) alpha[y * w + x] = 255;
        const field = distanceField(alpha, w, h, 6);
        expect(field[20 * w + 20]).toBe(255);
        expect(field[0]).toBe(0);
        // Inside rises and outside falls with the distance to the edge.
        expect(field[20 * w + 10]!).toBeGreaterThan(127);
        expect(field[20 * w + 9]!).toBeLessThan(128);
        expect(field[20 * w + 12]!).toBeGreaterThan(field[20 * w + 11]!);
        expect(field[20 * w + 7]!).toBeLessThan(field[20 * w + 8]!);
    });

    it('sets a line’s pen positions, and lists each face’s characters once', () => {
        const glyphs = new Map<string, Glyph>();
        for (const c of ['a', 'b', ' '])
            glyphs.set(glyphKey(0, c), {
                advance: c === ' ' ? 0.25 : 0.5,
                index: c === ' ' ? -1 : glyphs.size,
            });
        const line = setLine('ab ab', 0, glyphs);
        expect(line.glyphs.map(g => g[1])).toEqual([0, 0.5, 1.25, 1.75]);
        expect(line.width).toBe(2.25);
        // A character the atlas left out advances by nothing.
        expect(setLine('azb', 0, glyphs).glyphs.map(g => g[1])).toEqual([0, 0.5]);
        expect(
            charactersOf([
                [0, 'aba'],
                [1, 'a'],
            ]),
        ).toEqual([
            [0, 'a'],
            [0, 'b'],
            [1, 'a'],
        ]);
    });

    it('cuts labels to the card as the lineage page cuts names', () => {
        const long = 'x'.repeat(200);
        const model = new GraphModel({
            nodes: [
                { id: 'model.p.long', kind: 'model', name: long },
                { id: 'model.p.long#c', kind: 'column', name: long, parent: 'model.p.long' },
            ],
            edges: [{ source: 'model.p.long', target: 'model.p.long#c', kind: 'contains' }],
        });
        const [labels] = labelsOf(sceneOf(model), n => `+${n}`);
        expect(labels).toEqual([
            [0, fitText(long, FONT_NAME, TEXT_WIDTH)],
            [1, fitText(long, FONT_ROW, TEXT_WIDTH)],
        ]);
        expect(labels![0]![1].endsWith('…')).toBe(true);
        expect(labels![0]![1].length).toBeLessThan(long.length);
    });

    it('minifies shaders without joining tokens', () => {
        expect(minifyWgsl('// note\nfn f(a: u32) -> u32 { /* x */ return a - -1; }\n')).toBe(
            'fn f(a:u32)-> u32{return a - -1;}',
        );
        expect(minifyWgsl('var x: array<vec2<u32>> ;')).toBe('var x:array<vec2<u32>>;');
    });
});

describe('focus, keys and summary', () => {
    const model = new GraphModel(project(40));
    const scene = sceneOf(model);
    const order = scene.order;
    const rects = placeCards(scene, order);
    const prepared = { scene, rects } as unknown as Parameters<typeof statesFor>[1];
    const at = (name: string): number => scene.ids.indexOf(`model.p.m${name}`);

    it('lights the focused card’s lineage from the model and hides filtered kinds', () => {
        const { states, lineage } = statesFor(model, prepared, at('8'), new Set());
        expect(states[at('8')]).toBe(FOCUS);
        expect(lineage).toEqual({
            up: model.ancestors('model.p.m8').size,
            down: model.descendants('model.p.m8').size,
        });
        expect(states[at('4')]).toBe(UPSTREAM);
        expect(states[at('16')]).toBe(DOWNSTREAM);
        expect(states[at('5')]).toBe(0);
        const hidden = statesFor(model, prepared, -1, new Set(['model']));
        expect([...hidden.states].every(s => s === HIDDEN)).toBe(true);
    });

    it('moves along the lineage and within a layer', () => {
        const all = (): boolean => true;
        const right = step(model, prepared, at('8'), 'ArrowRight', all)!;
        expect(model.upAdj.get(scene.ids[right]!)).toContain('model.p.m8');
        const left = step(model, prepared, at('8'), 'ArrowLeft', all)!;
        expect(model.downAdj.get(scene.ids[left]!)).toContain('model.p.m8');
        const row = order[scene.layer[at('8')]!]!;
        const down = step(model, prepared, row[0]!, 'ArrowDown', all);
        expect(down).toBe(row[1]);
        expect(step(model, prepared, row[0]!, 'ArrowUp', all)).toBeNull();
        expect(step(model, prepared, at('0'), 'ArrowLeft', all)).toBeNull();
        expect(step(model, prepared, row[0]!, 'ArrowDown', i => i !== row[1])).toBe(row[2] ?? null);
    });

    it('sums up packages, orphans and untested cards', () => {
        const s = summarise(model, prepared, () => true);
        expect(s.total).toBe(40);
        expect(s.packages).toEqual([
            { name: 'a', count: 20 },
            { name: 'b', count: 20 },
        ]);
        // m0 feeds nothing (each model reads models i / 2 and i / 3, from m3 on); nothing has tests.
        expect(s.orphans).toBe(1);
        expect(s.untested).toBe(40);
        expect(summarise(model, prepared, i => i < 5).total).toBe(5);
    });

    it('counts models without tests as the tests page does, and not sources', () => {
        const m = new GraphModel(graph);
        const sc = sceneOf(m);
        expect(sc.nodes.some(n => n.kind === 'source' && m.testsOf(n.id).length === 0)).toBe(true);
        const s = summarise(m, { scene: sc } as unknown as Parameters<typeof summarise>[1], () => true);
        expect(s.untested).toBe(m.of('model').filter(n => !m.testsOf(n.id).length).length);
    });
});

/** A device that records what the renderer asks of it, with the given limits and scope errors. */
function fakeDevice(limits: Partial<Record<string, number>> = {}, errors: (string | null)[] = []) {
    const scopes: string[] = [];
    const buffers: { size: number; destroyed: boolean }[] = [];
    const device = {
        limits: {
            maxBufferSize: 2 ** 28,
            maxStorageBufferBindingSize: 2 ** 27,
            maxTextureDimension2D: 8192,
            ...limits,
        },
        pushErrorScope: (filter: string) => scopes.push(filter),
        popErrorScope: () => {
            scopes.pop();
            const message = errors.shift() ?? null;
            return Promise.resolve(message === null ? null : { message });
        },
        createShaderModule: () => ({}),
        createBindGroupLayout: () => ({}),
        createPipelineLayout: () => ({}),
        createRenderPipelineAsync: () => Promise.resolve({}),
        createSampler: () => ({}),
        createBindGroup: () => ({}),
        createTexture: () => ({ destroy: () => undefined, createView: () => ({}) }),
        createBuffer: ({ size }: { size: number }) => {
            const buffer = {
                size,
                destroyed: false,
                getMappedRange: () => new ArrayBuffer(size),
                unmap: () => undefined,
                destroy: () => (buffer.destroyed = true),
            };
            buffers.push(buffer);
            return buffer;
        },
        queue: { writeTexture: () => undefined, writeBuffer: () => undefined },
    };
    return { device: device as unknown as GPUDevice, scopes, buffers };
}

function content(nodes = 4): Content {
    return {
        nodes: new ArrayBuffer(nodes * NODE_WORDS * 4),
        states: new Uint32Array(nodes),
        edges: new Uint32Array(0),
        glyphs: { data: new Uint32Array(0), names: 0 },
        atlas: {
            width: 4,
            height: 4,
            pixels: new Uint8Array(16),
            table: new Float32Array(0),
            glyphs: new Map(),
            dropped: 0,
        },
    };
}

describe('the renderer against a device’s limits', () => {
    afterEach(() => {
        vi.unstubAllGlobals();
    });
    const stub = (): void => {
        vi.stubGlobal('navigator', { gpu: { getPreferredCanvasFormat: () => 'bgra8unorm' } });
    };

    it('asks for the adapter’s largest buffers', () => {
        const adapter = { limits: { maxBufferSize: 2 ** 32, maxStorageBufferBindingSize: 2 ** 31 } };
        expect(limitsOf(adapter as unknown as GPUAdapter)).toEqual({
            maxBufferSize: 2 ** 32,
            maxStorageBufferBindingSize: 2 ** 31,
        });
    });

    it('refuses a buffer larger than the device binds, before creating any', async () => {
        stub();
        const { device, buffers } = fakeDevice({ maxStorageBufferBindingSize: 64 });
        const renderer = await MapRenderer.create(device);
        const before = buffers.length;
        const error = await renderer.upload(content(4)).catch((e: unknown) => e);
        expect(error).toBeInstanceOf(TooLarge);
        expect(error).toMatchObject({ what: 'node buffer', needed: 128, limit: 64 });
        expect(buffers.length).toBe(before);
        await expect(renderer.upload(content(1))).resolves.toBeUndefined();
    });

    it('reports what the device’s error scopes caught, and frees the buffers', async () => {
        stub();
        const { device, scopes, buffers } = fakeDevice({}, [null, null, 'Buffer size exceeds the limit.']);
        const renderer = await MapRenderer.create(device);
        const before = buffers.length;
        await expect(renderer.upload(content())).rejects.toThrow(
            new DeviceError('Buffer size exceeds the limit.'),
        );
        expect(scopes).toEqual([]);
        expect(buffers.slice(before).every(b => b.destroyed)).toBe(true);
        expect(renderer.gpuBytes).toBe(0);
    });

    it('reports an error creating the pipelines', async () => {
        stub();
        const { device } = fakeDevice({}, ['Out of memory.']);
        await expect(MapRenderer.create(device)).rejects.toThrow(DeviceError);
    });

    it('passes on an error the work throws, with the scopes popped', async () => {
        const { device, scopes } = fakeDevice({}, ['ignored']);
        await expect(
            checked(device, () => {
                throw new Error('boom');
            }),
        ).rejects.toThrow('boom');
        expect(scopes).toEqual([]);
        await expect(checked(device, () => 7)).resolves.toBe(7);
    });
});
