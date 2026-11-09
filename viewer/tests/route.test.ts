import { describe, expect, it } from 'vitest';
import { formatRoute, parseRoute, type Route } from '../src/lib/route';

describe('routes', () => {
    it('round-trips every page', () => {
        const routes: Route[] = [
            { page: 'overview' },
            { page: 'node', id: 'model.shop.orders#amount' },
            { page: 'node', id: 'term.order_amount', select: '+orders+' },
            {
                page: 'lineage',
                select: '2+orders tag:nightly,kind:model',
                exclude: 'stg_orders',
                focus: 'model.shop.orders',
            },
            { page: 'browse', kind: 'model', tag: 'nightly', package: 'shop', q: 'order value' },
            { page: 'entity', id: 'customer order' },
            { page: 'glossary' },
            { page: 'conflicts' },
        ];
        for (const route of routes) expect(parseRoute(formatRoute(route))).toEqual(route);
    });

    it('writes readable links', () => {
        expect(formatRoute({ page: 'overview' })).toBe('#/');
        expect(formatRoute({ page: 'node', id: 'model.shop.orders#amount' })).toBe(
            '#/node/model.shop.orders%23amount',
        );
        expect(formatRoute({ page: 'lineage', select: '+orders+ tag:x' })).toBe(
            '#/lineage?select=+orders+%20tag:x',
        );
    });

    it('opens the links earlier versions wrote, and falls back to the overview', () => {
        expect(parseRoute('#model.jaffle_shop.orders%23amount')).toEqual({
            page: 'node',
            id: 'model.jaffle_shop.orders#amount',
        });
        expect(parseRoute('#term.order_amount')).toEqual({ page: 'node', id: 'term.order_amount' });
        expect(parseRoute('')).toEqual({ page: 'overview' });
        expect(parseRoute('#/nowhere')).toEqual({ page: 'overview' });
        expect(parseRoute('#/node')).toEqual({ page: 'overview' });
        expect(parseRoute('#/browse?kind=model&unknown=1&tag=')).toEqual({ page: 'browse', kind: 'model' });
        expect(parseRoute('#/node/%E0%A4%A')).toEqual({ page: 'node', id: '%E0%A4%A' });
    });
});
