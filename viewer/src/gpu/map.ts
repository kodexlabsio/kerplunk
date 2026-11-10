// Everything the map needs before its first frame, timed: the order (the lineage page's layout of
// every node), the scene, card positions, the glyph atlas and the instance buffers. The layout is
// kept per graph, so returning to the map draws at once and a new language does not lay the map
// out again. The only text that depends on the language is the "+N" row of a card with more
// columns than it lists, so the text is kept per set of those strings: languages that write
// numbers alike share one atlas and glyph buffer.

import { computeOrder } from '../lib/layout';
import type { GraphModel } from '../lib/model';
import { labelsOf, packEdges, packGlyphs, packNodes } from './pack';
import { PickGrid } from './pick';
import { boundsOf, layoutHash, MAX_ROWS, placeCards, rowsOf, type Box } from './place';
import type { Content } from './renderer';
import { buildScene, type Scene } from './scene';
import { buildAtlas, charactersOf, FACES } from './text';

export interface Timings {
    /** Milliseconds for each step, and for the layout as a whole (order, scene and placement). */
    order: number;
    scene: number;
    place: number;
    atlas: number;
    pack: number;
    layout: number;
}

interface Layout {
    scene: Scene;
    rects: Float32Array;
    bounds: Box | null;
    grid: PickGrid;
    hash: string;
    nodes: ArrayBuffer;
    edges: Uint32Array;
    timings: Pick<Timings, 'order' | 'scene' | 'place' | 'layout'>;
}

export interface Prepared {
    scene: Scene;
    rects: Float32Array;
    bounds: Box | null;
    grid: PickGrid;
    hash: string;
    content: Content;
    timings: Timings;
    /** Characters the glyph atlas had no room for; labels show without them. */
    dropped: number;
}

interface Cached {
    layout: Layout;
    /** Column counts past MAX_ROWS, each once: the numbers the "+N" rows show. */
    overflows: number[];
    byText: Map<string, Promise<Prepared>>;
}

const cache = new WeakMap<GraphModel, Cached>();

function clock(): () => number {
    let mark = performance.now();
    return () => {
        const now = performance.now();
        const ms = now - mark;
        mark = now;
        return ms;
    };
}

function layOut(model: GraphModel): Layout {
    const lap = clock();
    const order = computeOrder(model, model.inView);
    const orderMs = lap();
    const scene = buildScene(model, order);
    const sceneMs = lap();
    const rects = placeCards(scene, scene.order);
    const grid = new PickGrid(rects);
    const placeMs = lap();
    return {
        scene,
        rects,
        bounds: boundsOf(rects),
        grid,
        hash: layoutHash(rects),
        nodes: packNodes(scene, rects),
        edges: packEdges(scene),
        timings: { order: orderMs, scene: sceneMs, place: placeMs, layout: orderMs + sceneMs + placeMs },
    };
}

/** The map of ``model``; ``more`` writes the row that sums up the columns a card leaves out. */
export function prepare(model: GraphModel, more: (count: number) => string): Promise<Prepared> {
    let cached = cache.get(model);
    if (!cached) {
        const layout = layOut(model);
        const counts = new Set<number>();
        for (const columns of layout.scene.columns)
            if (columns.length > MAX_ROWS) counts.add(columns.length - MAX_ROWS);
        const overflows = [...counts].sort((x, y) => x - y);
        cache.set(model, (cached = { layout, overflows, byText: new Map() }));
    }
    const { layout, overflows, byText } = cached;
    const key = overflows.map(more).join('\u0000');
    let found = byText.get(key);
    if (!found) {
        found = withText(layout, more);
        found.catch(() => byText.delete(key));
        byText.set(key, found);
    }
    return found;
}

async function withText(layout: Layout, more: (count: number) => string): Promise<Prepared> {
    const lap = clock();
    await Promise.all(FACES.map(face => document.fonts?.load(face).catch(() => undefined)));
    const labels = labelsOf(layout.scene, more);
    const atlas = buildAtlas(charactersOf(labels.flat()));
    const atlasMs = lap();
    const content: Content = {
        nodes: layout.nodes,
        states: new Uint32Array(layout.scene.ids.length),
        edges: layout.edges,
        glyphs: packGlyphs(labels, atlas.glyphs),
        atlas,
    };
    const packMs = lap();
    return {
        scene: layout.scene,
        rects: layout.rects,
        bounds: layout.bounds,
        grid: layout.grid,
        hash: layout.hash,
        content,
        dropped: atlas.dropped,
        timings: { ...layout.timings, atlas: atlasMs, pack: packMs },
    };
}

/** Rows a card draws, for picking a row. */
export function rowCount(prepared: Prepared, node: number): number {
    return rowsOf(prepared.scene.columns[node]!.length);
}
