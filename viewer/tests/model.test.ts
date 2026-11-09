import { describe, expect, it } from 'vitest';
import { computeFocus, NO_FOCUS } from '../src/lib/focus';
import { bounds, computeLayers, computeOrder, curve, place, W, wrapHeight } from '../src/lib/layout';
import { byName, GraphModel } from '../src/lib/model';
import type { GraphNode } from '../src/lib/types';
import { graph } from './fixture';

const model = new GraphModel(graph);

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
