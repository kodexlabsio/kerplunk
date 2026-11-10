// Layered layout: layers by longest path over feeds (cycles broken by depth-first search), order
// within layers by barycentre sweeps and then two-sided rounds, then cards stacked by their heights.

import { byName, codePoints, type GraphModel, type LayoutEdge } from './model';
import type { Point } from './types';

export const W = 268;
export const HEAD = 46;
export const ROW = 28;
export const PAD = 6;
export const COL_GAP = 104;
export const ROW_GAP = 22;
export const WRAP_GAP = 36;
/** Rounds of each kind of crossing reduction; few graphs gain anything after about a dozen. */
const ROUNDS = 16;

function push<K, V>(map: Map<K, V[]>, key: K, value: V): void {
    const list = map.get(key);
    if (list) list.push(value);
    else map.set(key, [value]);
}

export function computeLayers(ids: string[], links: LayoutEdge[]): Map<string, number> {
    const succ = new Map(ids.map(id => [id, [] as string[]]));
    const hasInput = new Set<string>();
    for (const e of links) {
        succ.get(e.source)?.push(e.target);
        hasInput.add(e.target);
    }
    // Edges that close cycles are found by DFS from nodes without inputs, and ignored.
    const state = new Map<string, number>();
    const back = new Set<string>();
    for (const root of ids.filter(id => !hasInput.has(id)).concat(ids)) {
        if (state.has(root)) continue;
        state.set(root, 1);
        const stack: [string, number][] = [[root, 0]];
        while (stack.length) {
            const top = stack[stack.length - 1]!;
            const next = succ.get(top[0]) ?? [];
            if (top[1] < next.length) {
                const t = next[top[1]++]!;
                const s = state.get(t);
                if (s === 1) back.add(`${top[0]}\u0000${t}`);
                else if (!s) {
                    state.set(t, 1);
                    stack.push([t, 0]);
                }
            } else {
                state.set(top[0], 2);
                stack.pop();
            }
        }
    }
    const indegree = new Map(ids.map(id => [id, 0]));
    const forward = new Map(ids.map(id => [id, [] as string[]]));
    for (const e of links) {
        if (back.has(`${e.source}\u0000${e.target}`)) continue;
        forward.get(e.source)?.push(e.target);
        indegree.set(e.target, (indegree.get(e.target) ?? 0) + 1);
    }
    const layer = new Map(ids.map(id => [id, 0]));
    const queue = ids.filter(id => indegree.get(id) === 0);
    for (let i = 0; i < queue.length; i++) {
        const id = queue[i]!;
        for (const t of forward.get(id) ?? []) {
            layer.set(t, Math.max(layer.get(t) ?? 0, (layer.get(id) ?? 0) + 1));
            indegree.set(t, (indegree.get(t) ?? 0) - 1);
            if (indegree.get(t) === 0) queue.push(t);
        }
    }
    return layer;
}

/** The visible relations in layers, each layer ordered to reduce crossings. */
export function computeOrder(model: GraphModel, visible: Set<string>): string[][] {
    const ids = model.viewNodes
        .filter(n => visible.has(n.id))
        .sort(byName)
        .map(n => n.id);
    const links = model.layoutEdges.filter(e => visible.has(e.source) && visible.has(e.target));
    const feeds = links.filter(e => e.kind === 'feeds');
    const layer = computeLayers(ids, feeds);
    const at = (id: string): number => layer.get(id) ?? 0;
    // A node with nothing upstream sits just before the earliest node it feeds.
    const hasInput = new Set(feeds.map(e => e.target));
    const feedsTo = new Map<string, number[]>();
    for (const e of feeds) if (!hasInput.has(e.source)) push(feedsTo, e.source, at(e.target));
    for (const [id, targets] of feedsTo) layer.set(id, Math.max(0, Math.min(...targets) - 1));
    // A task linked only by runs edges sits just before the earliest node it runs.
    const linked = new Set<string>();
    for (const e of feeds) {
        linked.add(e.source);
        linked.add(e.target);
    }
    const runsFrom = new Map<string, number[]>();
    for (const e of links)
        if (e.kind === 'runs' && !linked.has(e.source)) push(runsFrom, e.source, at(e.target));
    let shift = 0;
    for (const targets of runsFrom.values()) if (Math.min(...targets) === 0) shift = 1;
    if (shift) for (const id of ids) layer.set(id, at(id) + shift);
    for (const [id, targets] of runsFrom) layer.set(id, Math.min(...targets) + shift - 1);
    return reduceCrossings(model, ids, links, at).map(row => row.map(i => ids[i]!));
}

/**
 * Each layer's order, as indices into ``ids``. Barycentre sweeps, downwards and upwards in turn,
 * then two-sided rounds from the best order they found: the odd layers move to the mean position
 * of their neighbours on both sides, then the even ones. Sweeps settle layered projects well, and
 * the rounds then undo much of what sweeps leave in graphs whose edges span many layers. Each layer
 * starts by package, then name, and the order with the fewest crossings is kept.
 */
function reduceCrossings(
    model: GraphModel,
    ids: string[],
    links: LayoutEdge[],
    at: (id: string) => number,
): number[][] {
    const n = ids.length;
    const index = new Map(ids.map((id, i) => [id, i]));
    const layerOf = new Int32Array(n);
    let depth = 0;
    ids.forEach((id, i) => {
        layerOf[i] = at(id);
        depth = Math.max(depth, layerOf[i]!);
    });
    const packages = ids.map(id => model.packageOf(model.node(id)!) ?? '');
    const seeded = ids.map((_, i) => i);
    seeded.sort((a, b) => codePoints(packages[a]!, packages[b]!) || a - b);
    const start: number[][] = Array.from({ length: depth + 1 }, () => []);
    for (const i of seeded) start[layerOf[i]!]!.push(i);
    // Neighbours in earlier and later layers, each pair once.
    const up: number[][] = ids.map(() => []);
    const down: number[][] = ids.map(() => []);
    const pairs = new Set<number>();
    for (const e of links) {
        let s = index.get(e.source)!;
        let t = index.get(e.target)!;
        if (layerOf[s] === layerOf[t]) continue;
        if (layerOf[s]! > layerOf[t]!) [s, t] = [t, s];
        if (pairs.has(s * n + t)) continue;
        pairs.add(s * n + t);
        down[s]!.push(t);
        up[t]!.push(s);
    }
    const both = ids.map((_, i) => up[i]!.concat(down[i]!));
    // Positions are centred, so layers of different sizes line up on their middles.
    const pos = new Float64Array(n);
    const rank = new Int32Array(n);
    /** Positions from a row's order; true when any card moved. */
    const settle = (row: number[]): boolean => {
        const middle = (row.length - 1) / 2;
        let moved = false;
        row.forEach((i, r) => {
            if (rank[i] !== r) moved = true;
            pos[i] = r - middle;
            rank[i] = r;
        });
        return moved;
    };
    const centre = new Float64Array(n);
    const sortRow = (row: number[], neighbours: number[][]): void => {
        for (const i of row) {
            const list = neighbours[i]!;
            let sum = 0;
            for (const j of list) sum += pos[j]!;
            centre[i] = list.length ? sum / list.length : pos[i]!;
        }
        // A stable sort: ties keep their order, so the result is the same on every run.
        row.sort((a, b) => centre[a]! - centre[b]!);
    };
    let widest = 0;
    for (const row of start) widest = Math.max(widest, row.length);
    const tree = new Int32Array(widest + 1);
    const ends: number[] = [];
    // Crossings between adjacent layers: inversions among each pair's edge ends, by a Fenwick tree.
    const crossings = (rows: number[][]): number => {
        let total = 0;
        for (let l = 0; l + 1 < rows.length; l++) {
            const size = rows[l + 1]!.length;
            ends.length = 0;
            for (const i of rows[l]!)
                for (const j of down[i]!) if (layerOf[j] === l + 1) ends.push(rank[i]! * size + rank[j]!);
            ends.sort((a, b) => a - b);
            tree.fill(0, 0, size + 1);
            let seen = 0;
            for (const end of ends) {
                const r = end % size;
                let atMost = 0;
                for (let x = r + 1; x > 0; x -= x & -x) atMost += tree[x]!;
                total += seen - atMost;
                for (let x = r + 1; x <= size; x += x & -x) tree[x]!++;
                seen++;
            }
        }
        return total;
    };
    const improve = (from: number[][], fewest: number, twoSided: boolean): [number[][], number] => {
        const rows = from.map(row => row.slice());
        rows.forEach(settle);
        let best = from;
        // Rounds stop once the order no longer changes: a two-sided round, or a sweep each way.
        let still = 0;
        for (let round = 0; round < ROUNDS && fewest > 0 && still < (twoSided ? 1 : 2); round++) {
            let moved = false;
            if (twoSided)
                for (const parity of [1, 0]) {
                    for (let l = parity; l < rows.length; l += 2) sortRow(rows[l]!, both);
                    for (let l = parity; l < rows.length; l += 2) if (settle(rows[l]!)) moved = true;
                }
            else {
                const downwards = round % 2 === 0;
                for (let k = 0; k < rows.length; k++) {
                    const row = rows[downwards ? k : rows.length - 1 - k]!;
                    sortRow(row, downwards ? up : down);
                    if (settle(row)) moved = true;
                }
            }
            still = moved ? 0 : still + 1;
            const count = crossings(rows);
            if (count < fewest) {
                fewest = count;
                best = rows.map(row => row.slice());
            }
        }
        return [best, fewest];
    };
    start.forEach(settle);
    const [swept, fewest] = improve(start, crossings(start), false);
    return improve(swept, fewest, true)[0].filter(row => row.length);
}

/**
 * Cards stacked in each layer, centred on zero, given each card's height. A layer taller than
 * ``maxHeight`` wraps into several columns, so a graph with hundreds of models in one layer stays
 * wide rather than becoming one very tall column.
 */
export function place(
    order: string[][],
    heightOf: (id: string) => number,
    maxHeight = Infinity,
): Map<string, Point> {
    const coords = new Map<string, Point>();
    let x = 0;
    for (const row of order) {
        const heights = row.map(heightOf);
        const columns: [string, number][][] = [[]];
        let used = 0;
        row.forEach((id, i) => {
            const h = heights[i]!;
            if (used > 0 && used + h > maxHeight) {
                columns.push([]);
                used = 0;
            }
            columns[columns.length - 1]!.push([id, h]);
            used += h + ROW_GAP;
        });
        for (const column of columns) {
            const total = column.reduce((a, [, h]) => a + h, 0) + ROW_GAP * (column.length - 1);
            let y = -total / 2;
            for (const [id, h] of column) {
                coords.set(id, { x, y, h });
                y += h + ROW_GAP;
            }
            x += W + (columns.length > 1 ? WRAP_GAP : 0);
        }
        x += COL_GAP - (columns.length > 1 ? WRAP_GAP : 0);
    }
    return coords;
}

/** A height that keeps the whole graph about as wide as it is tall, for ``place``. */
export function wrapHeight(order: string[][], heightOf: (id: string) => number): number {
    let area = 0;
    for (const row of order) for (const id of row) area += (heightOf(id) + ROW_GAP) * (W + COL_GAP);
    return Math.max(900, Math.sqrt(area * 0.62));
}

export interface Bounds {
    x: number;
    y: number;
    w: number;
    h: number;
}

export function bounds(at: Map<string, Point>): Bounds | null {
    if (!at.size) return null;
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const c of at.values()) {
        minX = Math.min(minX, c.x);
        minY = Math.min(minY, c.y);
        maxX = Math.max(maxX, c.x + W);
        maxY = Math.max(maxY, c.y + c.h);
    }
    return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
}

/** A cubic edge between two anchors; backwards edges loop beneath. */
export function curve(a: { x: number; y: number }, b: { x: number; y: number }): string {
    if (b.x <= a.x) {
        const drop = 40;
        return `M${a.x},${a.y} C${a.x + 60},${a.y + drop} ${b.x - 60},${b.y + drop} ${b.x},${b.y}`;
    }
    const dx = Math.max(40, (b.x - a.x) / 2);
    return `M${a.x},${a.y} C${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x},${b.y}`;
}
