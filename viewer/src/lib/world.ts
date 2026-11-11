// The world page: the business's entities as nodes, its relationships as edges, and its metrics,
// each with how far to trust it. Pure functions, so the layout and the trust rules have unit tests.

import type { Box } from '../gpu/place';
import type {
    Formula,
    OntologyData,
    OntologyEntity,
    OntologyRelationship,
    WorldData,
    WorldMetric,
} from './types';

/** How far to trust a fact: its confidence, unless konstruct verify found it false. A page
 * written before Konstruct recorded confidence has none, so its facts are 'unrated'. */
export type Trust = 'verified' | 'declared' | 'inferred' | 'failed' | 'unrated';

/** Strongest first, then a failed check and no rating: the legend's order. */
export const TRUSTS: readonly Trust[] = ['verified', 'declared', 'inferred', 'failed', 'unrated'];

export type { WorldMetric };

export function trustOf(confidence?: string, check?: string): Trust {
    if (check === 'failed') return 'failed';
    if (confidence === 'verified' || confidence === 'declared' || confidence === 'inferred')
        return confidence;
    return 'unrated';
}

/** The world model's metrics, or for a page without them the ontology's measures, which are
 * every metric a definition gives and no proposal. */
export function metricsOf(world: WorldData, ontology: OntologyData): WorldMetric[] {
    if (world.metrics) return world.metrics;
    return (ontology.measures ?? []).map(m => {
        const formulas = new Set((m.columns ?? []).map(c => c.formula).filter(f => f));
        return {
            name: m.name,
            source: 'definitions',
            confidence: m.confidence,
            proposed: false,
            definition: m.definition,
            status: m.status,
            owner: m.owner,
            approved: m.approved,
            entity: m.entity,
            grain: m.grain,
            unit: m.unit,
            currency: m.currency,
            filters: m.filters,
            formula: m.expression ?? (formulas.size === 1 ? [...formulas][0] : undefined),
            evidence: m.evidence,
            path: m.path,
            columns: (m.columns ?? []).map(c => c.id).filter((id): id is string => !!id),
        };
    });
}

/** A key for each metric, for its place in the address and in lists: its entity and name, which
 * are not always unique (two views can each define a count, two relations of one grain can each
 * hold a sum of the same name), so a name that repeats adds what tells the metrics apart: the
 * first column that holds it, else the view or semantic model that defines it, else its file.
 * These depend on the metric alone, not on its place in the page, so an address keeps opening the
 * same metric as others are added. In the parts written from names, `%`, `@`, `~` and `/` are
 * escaped, so `@` and `~` only ever separate; metrics still alike in everything recorded are
 * numbered, and no key is given twice. */
export function metricKeys(metrics: readonly WorldMetric[]): Map<WorldMetric, string> {
    const escape = (text: string, special: RegExp): string =>
        text.replace(special, c => `%${c.charCodeAt(0).toString(16).toUpperCase()}`);
    const name = (text: string): string => escape(text, /[%@~/]/g);
    const base = (m: WorldMetric): string => `metric:${name(m.entity ?? '')}/${name(m.name)}`;
    const counts = new Map<string, number>();
    for (const m of metrics) counts.set(base(m), (counts.get(base(m)) ?? 0) + 1);
    const used = new Set<string>();
    const keys = new Map<WorldMetric, string>();
    for (const m of metrics) {
        let key = base(m);
        if (counts.get(key)! > 1)
            key += `@${escape(m.columns?.[0] ?? m.exposure ?? m.path ?? m.source, /[%@~]/g)}`;
        if (used.has(key)) {
            let n = 2;
            while (used.has(`${key}~${n}`)) n++;
            key = `${key}~${n}`;
        }
        used.add(key);
        keys.set(m, key);
    }
    return keys;
}

/** A metric's formula: as its definition writes it, or composed down to source from the first
 * column that holds it. */
export function formulaOf(metric: WorldMetric, formulas: Record<string, Formula>): string | undefined {
    if (metric.formula) return metric.formula;
    for (const id of metric.columns ?? []) {
        const f = formulas[id];
        if (f?.c ?? f?.e) return f.c ?? f.e;
    }
    return undefined;
}

/** The source columns a metric's columns reach, by label. */
export function sourcesOf(metric: WorldMetric, formulas: Record<string, Formula>): string[] {
    const found = new Set<string>();
    for (const id of metric.columns ?? []) for (const s of formulas[id]?.s ?? []) found.add(s);
    return [...found].sort();
}

/** The labels at each end of an edge from the entity that refers to the one referred to, as an
 * entity-relationship diagram writes them: N at the many end, 1 at the one end. */
export function cardinalityEnds(cardinality?: string): [string, string] | null {
    if (cardinality === 'many-to-one') return ['N', '1'];
    if (cardinality === 'one-to-many') return ['1', 'N'];
    if (cardinality === 'one-to-one') return ['1', '1'];
    if (cardinality === 'many-to-many') return ['N', 'N'];
    return null;
}

export interface WorldNode {
    name: string;
    entity: OntologyEntity;
    trust: Trust;
    metrics: WorldMetric[];
    /** The circle's radius, by how many metrics the entity has. */
    r: number;
    x: number;
    y: number;
}

export interface WorldEdge {
    source: number;
    target: number;
    rel: OntologyRelationship;
    trust: Trust;
    ends: [string, string] | null;
}

export interface World {
    nodes: WorldNode[];
    edges: WorldEdge[];
    metrics: WorldMetric[];
    /** Metrics whose entity is not on the map: no grain, or a grain the ontology has no entity for. */
    loose: WorldMetric[];
    bounds: Box;
    /** The block of entities in no relationship, and how many there are. */
    loners: Box | null;
    lonerCount: number;
    /** The trust levels of the entities and relationships drawn, in the legend's order. */
    trusts: Trust[];
    layoutMs: number;
}

const MIN_RADIUS = 20;

export function radiusFor(metrics: number): number {
    return Math.min(64, MIN_RADIUS + 9 * Math.sqrt(metrics));
}

/** Defined metrics before proposals, then by name. */
function order(a: WorldMetric, b: WorldMetric, compare: (x: string, y: string) => number): number {
    return Number(!!a.proposed) - Number(!!b.proposed) || compare(a.name, b.name);
}

export function buildWorld(
    ontology: OntologyData,
    metrics: WorldMetric[],
    compare: (x: string, y: string) => number = (x, y) => (x < y ? -1 : x > y ? 1 : 0),
): World {
    const started = performance.now();
    const entities = (ontology.entities ?? []).slice().sort((a, b) => compare(a.name, b.name));
    const index = new Map(entities.map((e, i) => [e.name, i]));
    const held: WorldMetric[][] = entities.map(() => []);
    const loose: WorldMetric[] = [];
    for (const m of metrics) {
        const i = m.entity === undefined ? undefined : index.get(m.entity);
        if (i === undefined) loose.push(m);
        else held[i]!.push(m);
    }
    for (const list of held) list.sort((a, b) => order(a, b, compare));
    loose.sort((a, b) => order(a, b, compare));
    const nodes: WorldNode[] = entities.map((e, i) => ({
        name: e.name,
        entity: e,
        trust: trustOf(e.confidence, e.check),
        metrics: held[i]!,
        r: radiusFor(held[i]!.length),
        x: 0,
        y: 0,
    }));
    const edges: WorldEdge[] = [];
    for (const rel of ontology.relationships ?? []) {
        const source = index.get(rel.source);
        const target = index.get(rel.target);
        if (source === undefined || target === undefined || source === target) continue;
        edges.push({
            source,
            target,
            rel,
            trust: trustOf(rel.confidence, rel.check),
            ends: cardinalityEnds(rel.cardinality),
        });
    }
    const spans = nodes.map(n => spanOf(n));
    const placed = layout(
        spans,
        edges.map(e => [e.source, e.target]),
    );
    nodes.forEach((n, i) => {
        n.x = placed.x[i]!;
        n.y = placed.y[i]!;
    });
    // The key above the map explains the map: the trust of what it draws, not of the metrics listed
    // beside it, which carry their trust in words.
    const present = new Set<Trust>([...nodes.map(n => n.trust), ...edges.map(e => e.trust)]);
    return {
        nodes,
        edges,
        metrics,
        loose,
        bounds: placed.bounds,
        loners: placed.loners,
        lonerCount: placed.lonerCount,
        trusts: TRUSTS.filter(t => present.has(t)),
        layoutMs: performance.now() - started,
    };
}

/** The room a node needs: its circle, and its name written beneath it. */
function spanOf(n: WorldNode): number {
    return Math.max(n.r + 30, Math.min(110, n.name.length * 4.6 + 14));
}

// The layout: each connected group of entities by forces (Fruchterman and Reingold), from a
// deterministic start, so the same ontology always gives the same map; then the groups packed in
// rows, largest first. Repulsion is between every two nodes in a small group and, in a large
// one, only between nodes in neighbouring cells of a grid, so a step costs about as much as
// there are nodes.

const GAP = 56;
const EXACT = 160;
const GOLDEN = Math.PI * (3 - Math.sqrt(5));

export interface Placed {
    x: Float64Array;
    y: Float64Array;
    bounds: Box;
    /** The block of entities in no relationship, beneath the rest, or null when there are none. */
    loners: Box | null;
    lonerCount: number;
}

export function layout(spans: number[], links: [number, number][]): Placed {
    const n = spans.length;
    const x = new Float64Array(n);
    const y = new Float64Array(n);
    const adjacency: number[][] = spans.map(() => []);
    for (const [a, b] of links) {
        adjacency[a]!.push(b);
        adjacency[b]!.push(a);
    }
    const groups = components(adjacency);
    const alone = groups.filter(g => g.length === 1).map(g => g[0]!);
    const boxes: { members: number[]; w: number; h: number }[] = [];
    for (const members of groups) {
        if (members.length === 1) continue;
        place(members, spans, adjacency, x, y);
        let x0 = Infinity;
        let y0 = Infinity;
        let x1 = -Infinity;
        let y1 = -Infinity;
        for (const i of members) {
            const s = spans[i]!;
            x0 = Math.min(x0, x[i]! - s);
            y0 = Math.min(y0, y[i]! - s);
            x1 = Math.max(x1, x[i]! + s);
            y1 = Math.max(y1, y[i]! + s);
        }
        for (const i of members) {
            x[i] = x[i]! - x0;
            y[i] = y[i]! - y0;
        }
        boxes.push({ members, w: x1 - x0, h: y1 - y0 });
    }
    // Shelves as wide as the largest group or a square of all of them, whichever is wider.
    const cell = alone.length ? 2 * Math.max(...alone.map(i => spans[i]!)) + 8 : 0;
    const area = boxes.reduce((sum, b) => sum + (b.w + GAP) * (b.h + GAP), 0) + alone.length * cell * cell;
    const width = Math.max(boxes[0]?.w ?? 0, Math.sqrt(area) * 1.4);
    let cx = 0;
    let cy = 0;
    let shelf = 0;
    let right = 0;
    for (const b of boxes) {
        if (cx > 0 && cx + b.w > width) {
            cx = 0;
            cy += shelf + GAP;
            shelf = 0;
        }
        for (const i of b.members) {
            x[i] = x[i]! + cx;
            y[i] = y[i]! + cy;
        }
        cx += b.w + GAP;
        right = Math.max(right, cx - GAP);
        shelf = Math.max(shelf, b.h);
    }
    let bottom = cy + shelf;
    // Entities in no relationship are one block beneath the rest, in name order, under a heading of
    // their own, rather than scattered among the groups; the gap above them grows with the map, so
    // the heading stays clear of the groups above at the zoom that fits a large map.
    let loners: Box | null = null;
    if (alone.length) {
        const columns = Math.max(1, Math.floor(Math.max(width, right) / cell));
        const rows = Math.ceil(alone.length / columns);
        // About a twelfth of the map's height, so the gap reads the same at the zoom that fits it.
        const gap = Math.max(4 * GAP, (bottom + rows * cell) / 11);
        const top = boxes.length ? bottom + gap : 0;
        alone.sort((a, b) => a - b);
        alone.forEach((i, at) => {
            x[i] = (at % columns) * cell + cell / 2;
            y[i] = top + Math.floor(at / columns) * cell + cell / 2;
        });
        loners = { x: 0, y: top, w: Math.min(alone.length, columns) * cell, h: rows * cell };
        right = Math.max(right, loners.w);
        bottom = top + loners.h;
    }
    return {
        x,
        y,
        bounds: { x: 0, y: 0, w: Math.max(right, 1), h: Math.max(bottom, 1) },
        loners,
        lonerCount: alone.length,
    };
}

/** Connected groups, largest first; each in breadth-first order from its best connected node. */
function components(adjacency: number[][]): number[][] {
    const n = adjacency.length;
    const seen = new Uint8Array(n);
    const byDegree = [...adjacency.keys()].sort(
        (a, b) => adjacency[b]!.length - adjacency[a]!.length || a - b,
    );
    const groups: number[][] = [];
    for (const start of byDegree) {
        if (seen[start]) continue;
        seen[start] = 1;
        const members = [start];
        for (let k = 0; k < members.length; k++) {
            const next = adjacency[members[k]!]!.slice().sort((a, b) => a - b);
            for (const j of next) {
                if (seen[j]) continue;
                seen[j] = 1;
                members.push(j);
            }
        }
        groups.push(members);
    }
    return groups.sort((a, b) => b.length - a.length || a[0]! - b[0]!);
}

/** Members bucketed by grid cell (a counting sort), so a pass over neighbouring cells costs about
 * as much as there are members. */
interface Grid {
    cols: number;
    rows: number;
    cell: number;
    x0: number;
    y0: number;
    /** Where each cell's members start in ``items``; one more entry than cells. */
    start: Int32Array;
    /** Positions in ``members``, cell by cell. */
    items: Int32Array;
}

function gridOf(members: number[], x: Float64Array, y: Float64Array, wanted: number): Grid {
    const m = members.length;
    let x0 = Infinity;
    let y0 = Infinity;
    let x1 = -Infinity;
    let y1 = -Infinity;
    for (const i of members) {
        x0 = Math.min(x0, x[i]!);
        y0 = Math.min(y0, y[i]!);
        x1 = Math.max(x1, x[i]!);
        y1 = Math.max(y1, y[i]!);
    }
    // A larger cell than asked only finds more pairs; it keeps a spread-out group's grid small.
    const cell = Math.max(wanted, Math.sqrt(((x1 - x0 + 1) * (y1 - y0 + 1)) / (8 * m + 64)));
    const cols = Math.floor((x1 - x0) / cell) + 1;
    const rows = Math.floor((y1 - y0) / cell) + 1;
    const start = new Int32Array(cols * rows + 1);
    const at = new Int32Array(m);
    for (let a = 0; a < m; a++) {
        const i = members[a]!;
        const c = Math.floor((x[i]! - x0) / cell) * rows + Math.floor((y[i]! - y0) / cell);
        at[a] = c;
        start[c + 1]!++;
    }
    for (let c = 0; c < cols * rows; c++) start[c + 1]! += start[c]!;
    const fill = start.slice(0, cols * rows);
    const items = new Int32Array(m);
    for (let a = 0; a < m; a++) items[fill[at[a]!]!++] = a;
    return { cols, rows, cell, x0, y0, start, items };
}

/** Calls ``visit`` once for each two members in the same or neighbouring cells. */
function nearPairs(grid: Grid, visit: (a: number, b: number) => void): void {
    const { cols, rows, start, items } = grid;
    for (let cx = 0; cx < cols; cx++)
        for (let cy = 0; cy < rows; cy++) {
            const c = cx * rows + cy;
            for (let p = start[c]!; p < start[c + 1]!; p++) {
                const a = items[p]!;
                for (let q = p + 1; q < start[c + 1]!; q++) visit(a, items[q]!);
                // Half the neighbours, so each two cells are visited once.
                for (const [ox, oy] of HALF) {
                    const nx = cx + ox;
                    const ny = cy + oy;
                    if (nx < 0 || ny < 0 || nx >= cols || ny >= rows) continue;
                    const n = nx * rows + ny;
                    for (let q = start[n]!; q < start[n + 1]!; q++) visit(a, items[q]!);
                }
            }
        }
}

const HALF: readonly [number, number][] = [
    [1, -1],
    [1, 0],
    [1, 1],
    [0, 1],
];

function place(
    members: number[],
    spans: number[],
    adjacency: number[][],
    x: Float64Array,
    y: Float64Array,
): void {
    const m = members.length;
    const mean = members.reduce((sum, i) => sum + spans[i]!, 0) / m;
    const k = mean * 1.6 + GAP / 2;
    // A sunflower spiral in breadth-first order puts neighbours near each other from the start.
    members.forEach((i, at) => {
        const radius = k * 0.6 * Math.sqrt(at);
        x[i] = Math.cos(at * GOLDEN) * radius;
        y[i] = Math.sin(at * GOLDEN) * radius;
    });
    if (m < 2) return;
    const local = new Map(members.map((i, at) => [i, at]));
    const dx = new Float64Array(m);
    const dy = new Float64Array(m);
    const steps = m <= EXACT ? 260 : 90;
    // A pull to the middle that grows with distance would crush a large group; it weakens with size.
    const pull = 0.02 / Math.max(1, Math.sqrt(m) / 4);
    // A large group repels only between neighbouring cells; the far field it misses is made up
    // by a stronger push between those it does.
    const strength = m <= EXACT ? 1 : 4;
    let temperature = k * Math.min(Math.sqrt(m), 12) * 0.25;
    const cooling = temperature / (steps + 1);
    const repel = (a: number, b: number): void => {
        const i = members[a]!;
        const j = members[b]!;
        let ex = x[i]! - x[j]!;
        let ey = y[i]! - y[j]!;
        let d2 = ex * ex + ey * ey;
        if (d2 < 1e-6) {
            ex = ((a - b) % 7) + 0.5;
            ey = ((a + b) % 5) - 2.5;
            d2 = ex * ex + ey * ey;
        }
        const f = (strength * k * k) / d2;
        dx[a] = dx[a]! + ex * f;
        dy[a] = dy[a]! + ey * f;
        dx[b] = dx[b]! - ex * f;
        dy[b] = dy[b]! - ey * f;
    };
    for (let step = 0; step < steps; step++) {
        dx.fill(0);
        dy.fill(0);
        if (m <= EXACT) {
            for (let a = 0; a < m; a++) for (let b = a + 1; b < m; b++) repel(a, b);
        } else nearPairs(gridOf(members, x, y, k * 2), repel);
        for (let a = 0; a < m; a++) {
            const i = members[a]!;
            for (const j of adjacency[i]!) {
                const b = local.get(j)!;
                if (b <= a) continue;
                const ex = x[i]! - x[j]!;
                const ey = y[i]! - y[j]!;
                const d = Math.sqrt(ex * ex + ey * ey) || 1;
                // Eades's spring, which grows with the logarithm of its stretch: in a group no
                // drawing can keep short, a pull that grows with distance squeezes it into a ball.
                const f = d > k ? (k * Math.log(d / k)) / d : 0;
                dx[a] = dx[a]! - ex * f;
                dy[a] = dy[a]! - ey * f;
                dx[b] = dx[b]! + ex * f;
                dy[b] = dy[b]! + ey * f;
            }
            dx[a] = dx[a]! - x[i]! * pull;
            dy[a] = dy[a]! - y[i]! * pull;
        }
        for (let a = 0; a < m; a++) {
            const i = members[a]!;
            const d = Math.sqrt(dx[a]! * dx[a]! + dy[a]! * dy[a]!) || 1;
            const move = Math.min(d, temperature);
            x[i] = x[i]! + (dx[a]! / d) * move;
            y[i] = y[i]! + (dy[a]! / d) * move;
        }
        temperature = Math.max(k * 0.02, temperature - cooling);
    }
    separate(members, spans, x, y, 400);
    turn(members, x, y);
}

/** Turns a group about its centre so its longer axis runs across, as a screen is wider than tall. */
function turn(members: number[], x: Float64Array, y: Float64Array): void {
    const m = members.length;
    if (m < 2) return;
    let cx = 0;
    let cy = 0;
    for (const i of members) {
        cx += x[i]!;
        cy += y[i]!;
    }
    cx /= m;
    cy /= m;
    let xx = 0;
    let xy = 0;
    let yy = 0;
    for (const i of members) {
        const dx = x[i]! - cx;
        const dy = y[i]! - cy;
        xx += dx * dx;
        xy += dx * dy;
        yy += dy * dy;
    }
    // The angle of the principal axis, turned back to the horizontal.
    const angle = -0.5 * Math.atan2(2 * xy, xx - yy);
    const cos = Math.cos(angle);
    const sin = Math.sin(angle);
    for (const i of members) {
        const dx = x[i]! - cx;
        const dy = y[i]! - cy;
        x[i] = dx * cos - dy * sin;
        y[i] = dx * sin + dy * cos;
    }
}

/** Pushes apart any two nodes whose circles and names would overlap. */
function separate(
    members: number[],
    spans: number[],
    x: Float64Array,
    y: Float64Array,
    passes: number,
): void {
    const widest = Math.max(...members.map(i => spans[i]!));
    for (let pass = 0; pass < passes; pass++) {
        let moved = false;
        nearPairs(gridOf(members, x, y, widest * 2 + 8), (a, b) => {
            const i = members[a]!;
            const j = members[b]!;
            const need = spans[i]! + spans[j]! + 8;
            let ex = x[j]! - x[i]!;
            let ey = y[j]! - y[i]!;
            let d = Math.sqrt(ex * ex + ey * ey);
            if (d >= need) return;
            if (d < 1e-6) {
                ex = 1;
                ey = 0;
                d = 1;
            }
            const push = (need - d) / 2 + 0.5;
            x[i] = x[i]! - (ex / d) * push;
            y[i] = y[i]! - (ey / d) * push;
            x[j] = x[j]! + (ex / d) * push;
            y[j] = y[j]! + (ey / d) * push;
            moved = true;
        });
        if (!moved) return;
    }
}

/** The node an arrow key moves to from ``from``: the nearest in that direction, favouring those
 * straight ahead; ``from`` itself when there is none. */
export function stepFrom(
    nodes: readonly { x: number; y: number }[],
    from: number,
    ux: number,
    uy: number,
): number {
    const here = nodes[from];
    if (!here) return from;
    let best = from;
    let score = Infinity;
    nodes.forEach((n, i) => {
        if (i === from) return;
        const ex = n.x - here.x;
        const ey = n.y - here.y;
        const d = Math.sqrt(ex * ex + ey * ey);
        const cos = (ex * ux + ey * uy) / (d || 1);
        if (cos <= 0.3) return;
        const s = d * (1 + 2 * (1 - cos));
        if (s < score) {
            score = s;
            best = i;
        }
    });
    return best;
}

/** A layout fingerprint: the same ontology must give the same map. */
export function layoutHash(nodes: readonly { x: number; y: number }[]): string {
    let h = 0x811c9dc5;
    for (const n of nodes)
        for (const v of [Math.round(n.x), Math.round(n.y)]) {
            h ^= v & 0xffff;
            h = Math.imul(h, 0x01000193) >>> 0;
        }
    return h.toString(16);
}
