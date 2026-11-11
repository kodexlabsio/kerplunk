import { describe, expect, it } from 'vitest';
import type { OntologyData, WorldMetric } from '../src/lib/types';
import {
    buildWorld,
    cardinalityEnds,
    formulaOf,
    layout,
    layoutHash,
    metricKeys,
    metricsOf,
    radiusFor,
    sourcesOf,
    stepFrom,
    trustOf,
} from '../src/lib/world';

const ontology: OntologyData = {
    entities: [
        { name: 'order', key: 'order_id', confidence: 'verified', check: 'confirmed' },
        { name: 'customer', key: 'customer_id', confidence: 'declared' },
        { name: 'payment', key: 'payment_id', confidence: 'inferred', check: 'failed' },
    ],
    relationships: [
        {
            source: 'order',
            target: 'customer',
            via: 'a = b',
            cardinality: 'many-to-one',
            confidence: 'verified',
        },
        { source: 'payment', target: 'order', via: 'c = d', confidence: 'inferred' },
        { source: 'payment', target: 'nowhere', via: 'e = f' },
    ],
};

const metrics: WorldMetric[] = [
    { name: 'order count', source: 'entity count', confidence: 'inferred', proposed: true, entity: 'order' },
    {
        name: 'Order amount',
        source: 'definitions',
        confidence: 'declared',
        status: 'approved',
        entity: 'order',
    },
    { name: 'revenue', source: 'looker', confidence: 'declared' },
];

describe('trust', () => {
    it('is the confidence, unless a check failed', () => {
        expect(trustOf('verified', 'confirmed')).toBe('verified');
        expect(trustOf('declared')).toBe('declared');
        expect(trustOf('verified', 'failed')).toBe('failed');
        expect(trustOf(undefined)).toBe('unrated');
        expect(trustOf('certain')).toBe('unrated');
    });

    it('labels both ends of a relationship by its cardinality', () => {
        expect(cardinalityEnds('many-to-one')).toEqual(['N', '1']);
        expect(cardinalityEnds('one-to-one')).toEqual(['1', '1']);
        expect(cardinalityEnds('one-to-many')).toEqual(['1', 'N']);
        expect(cardinalityEnds(undefined)).toBeNull();
    });
});

describe('the world', () => {
    const world = buildWorld(ontology, metrics);

    it('has a node for each entity and an edge for each relationship between two of them', () => {
        expect(world.nodes.map(n => n.name)).toEqual(['customer', 'order', 'payment']);
        expect(world.nodes.map(n => n.trust)).toEqual(['declared', 'verified', 'failed']);
        expect(world.edges).toHaveLength(2);
        expect(world.edges[0]!.ends).toEqual(['N', '1']);
        expect(world.edges[1]!.ends).toBeNull();
    });

    it('sizes each node by its metrics, defined ones first', () => {
        const order = world.nodes.find(n => n.name === 'order')!;
        expect(order.metrics.map(m => m.name)).toEqual(['Order amount', 'order count']);
        expect(order.r).toBe(radiusFor(2));
        expect(order.r).toBeGreaterThan(world.nodes.find(n => n.name === 'customer')!.r);
        expect(world.loose.map(m => m.name)).toEqual(['revenue']);
    });

    it('lists the trust levels it has, strongest first', () => {
        expect(world.trusts).toEqual(['verified', 'declared', 'inferred', 'failed']);
    });

    it('places every node apart, the same way every time', () => {
        const again = buildWorld(ontology, metrics);
        expect(layoutHash(again.nodes)).toBe(layoutHash(world.nodes));
        for (const a of world.nodes)
            for (const b of world.nodes)
                if (a !== b) expect(Math.hypot(a.x - b.x, a.y - b.y)).toBeGreaterThan(a.r + b.r);
        for (const n of world.nodes) {
            expect(n.x).toBeGreaterThanOrEqual(world.bounds.x);
            expect(n.x).toBeLessThanOrEqual(world.bounds.x + world.bounds.w);
        }
    });

    it('lays out two thousand entities quickly, without overlaps', () => {
        const n = 2000;
        const spans = Array.from({ length: n }, () => 50);
        const links: [number, number][] = [];
        for (let i = 10; i < n; i++) links.push([i, Math.floor(i / 2)], [i, Math.floor(i / 3)]);
        const started = performance.now();
        const placed = layout(spans, links);
        const ms = performance.now() - started;
        expect(ms).toBeLessThan(3000);
        let close = 0;
        for (let i = 0; i < n; i += 7)
            for (let j = 0; j < n; j++)
                if (i !== j && Math.hypot(placed.x[i]! - placed.x[j]!, placed.y[i]! - placed.y[j]!) < 60)
                    close++;
        expect(close).toBe(0);
    });

    it('moves between nodes with the arrow keys', () => {
        const points = [
            { x: 0, y: 0 },
            { x: 100, y: 5 },
            { x: 0, y: 100 },
            { x: 300, y: 0 },
        ];
        expect(stepFrom(points, 0, 1, 0)).toBe(1);
        expect(stepFrom(points, 1, 1, 0)).toBe(3);
        expect(stepFrom(points, 0, 0, 1)).toBe(2);
        expect(stepFrom(points, 0, -1, 0)).toBe(0);
    });
});

describe('keys', () => {
    it('are the entity and name, and tell apart metrics that share them', () => {
        const views: WorldMetric[] = [
            { name: 'count', source: 'looker', path: 'views/orders.view.lkml' },
            { name: 'count', source: 'looker', path: 'views/refunds.view.lkml' },
            {
                name: 'lifetime',
                source: 'aggregate',
                entity: 'customer',
                columns: ['model.s.customers#lifetime'],
            },
            {
                name: 'lifetime',
                source: 'aggregate',
                entity: 'customer',
                columns: ['model.s.summary#lifetime'],
            },
            { name: 'twin', source: 'looker' },
            { name: 'twin', source: 'looker' },
            { name: 'Revenue', source: 'definitions', entity: 'order' },
        ];
        const keys = metricKeys(views);
        expect([...keys.values()]).toEqual([
            'metric:/count@views/orders.view.lkml',
            'metric:/count@views/refunds.view.lkml',
            'metric:customer/lifetime@model.s.customers#lifetime',
            'metric:customer/lifetime@model.s.summary#lifetime',
            'metric:/twin@looker',
            'metric:/twin@looker~2',
            'metric:order/Revenue',
        ]);
        expect([...metricKeys(views).values()]).toEqual([...keys.values()]);
    });
});

describe('keys that look alike', () => {
    it('stay apart however a name is written', () => {
        const odd: WorldMetric[] = [
            { name: 'count', source: 'looker', path: 'a' },
            { name: 'count', source: 'looker', path: 'a' },
            { name: 'count@a~2', source: 'looker' },
            { name: 'count@a', source: 'looker' },
        ];
        const keys = [...metricKeys(odd).values()];
        expect(new Set(keys).size).toBe(4);
        // A name of its own needs no suffix, whatever characters it holds.
        expect(keys[3]).toBe('metric:/count%40a');
        expect(keys[2]).toBe('metric:/count%40a%7E2');
    });

    it('name the view that defines a measure before the file it is in', () => {
        const views: WorldMetric[] = [
            { name: 'count', source: 'looker', path: 'all.lkml', exposure: 'exposure.looker.orders' },
            { name: 'count', source: 'looker', path: 'all.lkml', exposure: 'exposure.looker.refunds' },
        ];
        expect([...metricKeys(views).values()]).toEqual([
            'metric:/count@exposure.looker.orders',
            'metric:/count@exposure.looker.refunds',
        ]);
    });
});

describe('the key above the map', () => {
    it('lists the trust only of what the map draws', () => {
        const one: OntologyData = { entities: [{ name: 'order', key: 'order_id', confidence: 'declared' }] };
        const world = buildWorld(one, [
            { name: 'x', source: 'aggregate', confidence: 'inferred', entity: 'order' },
        ]);
        expect(world.trusts).toEqual(['declared']);
    });
});

describe('entities in no relationship', () => {
    it('are one block beneath the groups, counted', () => {
        const spans = [40, 40, 40, 40, 40];
        const placed = layout(spans, [[0, 1]]);
        expect(placed.lonerCount).toBe(3);
        const block = placed.loners!;
        for (const i of [2, 3, 4]) {
            expect(placed.y[i]!).toBeGreaterThan(block.y);
            expect(placed.y[i]!).toBeLessThan(block.y + block.h);
        }
        for (const i of [0, 1]) expect(placed.y[i]! + spans[i]!).toBeLessThan(block.y);
        expect(
            layout(spans, [
                [0, 1],
                [2, 3],
                [3, 4],
            ]).loners,
        ).toBeNull();
    });
});

describe('metrics', () => {
    it('are the ontology measures when the page has no world model', () => {
        const found = metricsOf(
            {},
            {
                measures: [
                    {
                        name: 'Order amount',
                        status: 'approved',
                        entity: 'order',
                        columns: [
                            { column: 'orders.amount', id: 'model.s.orders#amount', formula: 'SUM(x)' },
                        ],
                    },
                ],
            },
        );
        expect(found).toEqual([
            expect.objectContaining({
                name: 'Order amount',
                proposed: false,
                formula: 'SUM(x)',
                columns: ['model.s.orders#amount'],
            }),
        ]);
        expect(metricsOf({ metrics }, {})).toBe(metrics);
    });

    it('take their formula and sources from the columns that hold them', () => {
        const formulas = {
            'model.s.orders#amount': { e: 'SUM(p.amount)', c: 'SUM(raw.amount)', s: ['raw.amount'] },
        };
        const m: WorldMetric = { name: 'x', source: 'aggregate', columns: ['model.s.orders#amount'] };
        expect(formulaOf(m, formulas)).toBe('SUM(raw.amount)');
        expect(formulaOf({ ...m, formula: 'COUNT(*)' }, formulas)).toBe('COUNT(*)');
        expect(sourcesOf(m, formulas)).toEqual(['raw.amount']);
        expect(formulaOf({ name: 'y', source: 'aggregate' }, formulas)).toBeUndefined();
    });
});
