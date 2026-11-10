import { describe, expect, it } from 'vitest';
import { computeFocus, NO_FOCUS } from '../src/lib/focus';
import { bounds, computeLayers, computeOrder, curve, place, W, wrapHeight } from '../src/lib/layout';
import { byName, GraphModel } from '../src/lib/model';
import type { GraphData, GraphNode } from '../src/lib/types';
import { graph } from './fixture';

const model = new GraphModel(graph);

/** Models m0..m(n-1) in package p, with feeds edges between them. */
function models(n: number, edges: [number, number][], packageOf = (_: number) => 'p'): GraphModel {
    const nodes: GraphNode[] = Array.from({ length: n }, (_, i) => ({
        id: `model.m${i}`,
        kind: 'model',
        name: `m${i}`,
        attrs: { package: packageOf(i) },
    }));
    const data: GraphData = {
        nodes,
        edges: edges.map(([s, t]) => ({ source: `model.m${s}`, target: `model.m${t}`, kind: 'feeds' })),
    };
    return new GraphModel(data);
}

/** Crossings between edges of adjacent layers, by checking every pair. */
function crossings(m: GraphModel, order: string[][]): number {
    const layer = new Map<string, number>();
    const rank = new Map<string, number>();
    order.forEach((row, l) => row.forEach((id, r) => (layer.set(id, l), rank.set(id, r))));
    // Each edge between adjacent layers as its upper layer and the ranks of its two ends.
    const ends = m.layoutEdges
        .map(e => (layer.get(e.source)! < layer.get(e.target)! ? [e.source, e.target] : [e.target, e.source]))
        .filter(([a, b]) => layer.get(b!)! - layer.get(a!)! === 1)
        .map(([a, b]) => [layer.get(a!)!, rank.get(a!)!, rank.get(b!)!] as const);
    let count = 0;
    for (let i = 0; i < ends.length; i++)
        for (let j = i + 1; j < ends.length; j++) {
            const [l, a0, a1] = ends[i]!;
            const [k, b0, b1] = ends[j]!;
            if (l === k && (a0 - b0) * (a1 - b1) < 0) count++;
        }
    return count;
}

function random(seed: number): () => number {
    let s = seed;
    return () => (s = (Math.imul(s, 1103515245) + 12345) >>> 0) / 2 ** 32;
}

/** A seeded random DAG: each model after the first reads one to three of the 60 before it. */
function randomDag(n: number, seed: number): GraphModel {
    const next = random(seed);
    const edges: [number, number][] = [];
    for (let i = 1; i < n; i++) {
        const parents = 1 + Math.floor(next() * 3);
        for (let k = 0; k < parents; k++) edges.push([i - 1 - Math.floor(next() * Math.min(i, 60)), i]);
    }
    return models(n, edges, i => `p${i % 5}`);
}

describe('the graph model', () => {
    it('draws relations and tasks, not columns or terms', () => {
        expect(model.viewNodes.map(n => n.id)).toEqual([
            'source.raw.orders',
            'model.stg_orders',
            'model.orders',
            'task.nightly.run',
        ]);
    });

    it('labels sources, tasks and columns the way the CLI does', () => {
        expect(model.label('source.raw.orders')).toBe('raw.orders');
        expect(model.label('task.nightly.run')).toBe('nightly.run');
        expect(model.label('model.orders#revenue')).toBe('orders.revenue');
    });

    it('folds a filter column into its relation for relation edges', () => {
        expect(model.layoutEdges).toContainEqual({
            source: 'source.raw.orders',
            target: 'model.stg_orders',
            kind: 'feeds',
        });
        expect(model.layoutEdges.filter(e => e.source === 'source.raw.orders')).toHaveLength(1);
    });

    it('walks column lineage both ways with depth, transform and verification', () => {
        expect(model.columnLineage('model.orders#revenue', true)).toEqual([
            {
                id: 'model.stg_orders#amount',
                depth: 1,
                via: 'model.orders#revenue',
                transform: 'aggregate',
                verified: 'confirmed',
            },
            {
                id: 'source.raw.orders#amount',
                depth: 2,
                via: 'model.stg_orders#amount',
                transform: 'direct',
                verified: undefined,
            },
        ]);
        expect(model.columnLineage('source.raw.orders#amount', false).map(i => i.id)).toEqual([
            'model.stg_orders#amount',
            'model.orders#revenue',
        ]);
    });

    it('knows which terms define a column', () => {
        expect(model.boundTo('term.revenue')).toEqual(['model.orders#revenue']);
        expect(model.termsOf('model.orders#revenue').map(t => t.name)).toEqual(['Revenue']);
    });
});

describe('layout', () => {
    it('layers by longest path and breaks cycles', () => {
        const layers = computeLayers(
            ['a', 'b', 'c'],
            [
                { source: 'a', target: 'b', kind: 'feeds' },
                { source: 'b', target: 'c', kind: 'feeds' },
                { source: 'c', target: 'a', kind: 'feeds' },
            ],
        );
        expect([layers.get('a'), layers.get('b'), layers.get('c')]).toEqual([0, 1, 2]);
    });

    it('puts a task just before what it runs', () => {
        const order = computeOrder(model, new Set(model.viewNodes.map(n => n.id)));
        const layerOf = (id: string): number => order.findIndex(row => row.includes(id));
        expect(layerOf('source.raw.orders')).toBeLessThan(layerOf('model.stg_orders'));
        expect(layerOf('model.stg_orders')).toBeLessThan(layerOf('model.orders'));
        expect(layerOf('task.nightly.run')).toBe(layerOf('model.orders') - 1);
    });

    it('keeps the order with the fewest crossings, not the last sweep’s', () => {
        // m2 leads to m3 and m4..m6 lead nowhere: an upward sweep last put m4 before m2, under m1.
        const m = models(7, [
            [0, 2],
            [2, 3],
            [1, 4],
            [1, 5],
            [1, 6],
        ]);
        const order = computeOrder(m, m.inView);
        expect(order[1]).toEqual(['model.m2', 'model.m4', 'model.m5', 'model.m6']);
        expect(crossings(m, order)).toBe(0);
    });

    it('starts each layer by package, so a package’s nodes stay together', () => {
        const m = models(6, [], i => (i % 2 ? 'b' : 'a'));
        expect(computeOrder(m, m.inView)).toEqual([
            ['model.m0', 'model.m2', 'model.m4', 'model.m1', 'model.m3', 'model.m5'],
        ]);
    });

    it('orders random graphs the same way every time, with no more crossings than at the start', () => {
        const next = random(11);
        for (let graphs = 0; graphs < 40; graphs++) {
            const n = 5 + Math.floor(next() * 40);
            const edges: [number, number][] = [];
            for (let i = 0; i < n; i++)
                for (let j = i + 1; j < n; j++) if (next() < 3 / n) edges.push([i, j]);
            const packages = (i: number): string => `p${(i * 7) % 3}`;
            const m = models(n, edges, packages);
            const order = computeOrder(m, m.inView);
            expect(computeOrder(m, m.inView)).toEqual(order);
            // Each row holds its layer's nodes; the start is each layer by package, then name.
            const start = order.map(row =>
                row.slice().sort((a, b) => {
                    const [x, y] = [Number(a.slice(7)), Number(b.slice(7))];
                    return packages(x).localeCompare(packages(y)) || `m${x}`.localeCompare(`m${y}`);
                }),
            );
            expect(order.flat().sort()).toEqual(m.viewNodes.map(v => v.id).sort());
            expect(crossings(m, order)).toBeLessThanOrEqual(crossings(m, start));
        }
    });

    it('orders seeded random DAGs with these many crossings', () => {
        // Pinned, so that a change to the order shows what it does to them.
        const small = randomDag(500, 1);
        expect(crossings(small, computeOrder(small, small.inView))).toBe(546);
        const large = randomDag(2000, 2);
        expect(crossings(large, computeOrder(large, large.inView))).toBe(2635);
    });

    it('stacks cards by height and measures bounds', () => {
        const coords = place([['a', 'b']], id => (id === 'a' ? 46 : 100));
        expect(coords.get('a')).toEqual({ x: 0, y: -84, h: 46 });
        expect(coords.get('b')).toEqual({ x: 0, y: -16, h: 100 });
        expect(bounds(coords)).toEqual({ x: 0, y: -84, w: W, h: 168 });
        expect(bounds(new Map())).toBeNull();
    });

    it('wraps a layer taller than the limit into columns', () => {
        const coords = place([['a', 'b', 'c'], ['d']], () => 100, 230);
        expect(coords.get('a')!.x).toBe(0);
        expect(coords.get('b')!.x).toBe(0);
        expect(coords.get('c')!.x).toBeGreaterThan(W);
        expect(coords.get('d')!.x).toBeGreaterThan(coords.get('c')!.x + W);
        expect(wrapHeight([['a']], () => 46)).toBeGreaterThanOrEqual(900);
    });

    it('draws backward edges beneath', () => {
        expect(curve({ x: 0, y: 0 }, { x: 200, y: 0 })).toBe('M0,0 C100,0 100,0 200,0');
        expect(curve({ x: 200, y: 0 }, { x: 0, y: 0 })).toContain('C260,40');
    });
});

describe('focus', () => {
    it("lights a relation's whole lineage", () => {
        // The task runs orders, which is downstream of stg_orders, so the task is not in its lineage.
        expect([...(computeFocus(model, 'model.stg_orders').relations ?? [])].sort()).toEqual([
            'model.orders',
            'model.stg_orders',
            'source.raw.orders',
        ]);
        expect(computeFocus(model, 'model.orders').relations?.has('task.nightly.run')).toBe(true);
        expect(computeFocus(model, null)).toEqual(NO_FOCUS);
    });

    it("lights a column's path and the filters on it", () => {
        const focus = computeFocus(model, 'model.orders#revenue');
        expect([...(focus.columns ?? [])].sort()).toEqual([
            'model.orders#revenue',
            'model.stg_orders#amount',
            'source.raw.orders#amount',
        ]);
        expect(focus.references).toEqual([]);
        expect(computeFocus(model, 'source.raw.orders#status').references).toEqual([
            ['source.raw.orders#status', 'model.stg_orders'],
        ]);
    });

    it("lights a term's columns", () => {
        const focus = computeFocus(model, 'term.revenue');
        expect([...(focus.columns ?? [])]).toEqual(['model.orders#revenue']);
        expect([...(focus.relations ?? [])]).toEqual(['model.orders']);
    });
});

describe('groupings', () => {
    it('reads packages from attributes or ids, and tests by target', () => {
        expect(model.packageOf(model.node('model.orders')!)).toBe('shop');
        expect(model.packageOf(model.node('model.orders#revenue')!)).toBe('shop');
        expect(model.tagsOf(model.node('model.orders')!)).toEqual(['finance', 'nightly']);
        expect(model.testsOf('model.orders').map(([t, target]) => [t.id, target])).toEqual([
            ['test.shop.not_null_orders_revenue', 'model.orders#revenue'],
        ]);
        expect(model.schemaOf(model.node('model.orders')!)).toEqual({
            database: 'db',
            schema: 'main',
            name: 'orders',
        });
        expect([...model.ancestors('model.orders')].sort()).toEqual([
            'model.stg_orders',
            'source.raw.orders',
            'task.nightly.run',
        ]);
        expect(model.referencedBy('source.raw.orders#status')).toEqual(['model.stg_orders']);
    });
});

describe('ordering', () => {
    // The browser's default locale must not change how a graph is laid out.
    function underLocale<T>(locale: string, run: () => T): T {
        const original = String.prototype.localeCompare;
        String.prototype.localeCompare = function (this: string, that: string) {
            return original.call(this, that, locale);
        };
        try {
            return run();
        } finally {
            String.prototype.localeCompare = original;
        }
    }

    it('sorts names the same way in every locale', () => {
        const nodes = ['z', 'ä', 'a', 'B', 'a'].map(
            (name, i) => ({ id: `model.p.${name}${i}`, kind: 'model', name }) as GraphNode,
        );
        const order = (locale: string): string[] =>
            underLocale(locale, () => [...nodes].sort(byName).map(n => n.id));
        expect(order('sv')).toEqual(order('en'));
        expect(order('en')).toEqual(['model.p.B3', 'model.p.a2', 'model.p.a4', 'model.p.z0', 'model.p.ä1']);
    });
});
