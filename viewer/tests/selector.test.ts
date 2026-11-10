import { describe, expect, it } from 'vitest';
import { GraphModel } from '../src/lib/model';
import { evaluate, lineageOf, parseAtom, parseSelector, reference } from '../src/lib/selector';
import { graph } from './fixture';

const model = new GraphModel(graph);

function select(text: string, exclude = ''): string[] {
    return [...evaluate(model, parseSelector(text), parseSelector(exclude)).relations].sort();
}

describe('selector parsing', () => {
    it('reads graph operators with and without depths', () => {
        expect(parseAtom('+orders')).toMatchObject({ method: null, value: 'orders', up: Infinity, down: 0 });
        expect(parseAtom('orders+')).toMatchObject({ up: 0, down: Infinity });
        expect(parseAtom('2+orders+1')).toMatchObject({ up: 2, down: 1, at: false });
        expect(parseAtom('@orders')).toMatchObject({ at: true, up: 0, down: 0 });
        expect(parseAtom('tag:nightly')).toMatchObject({ method: 'tag', value: 'nightly' });
        expect(parseAtom('+config.materialized:view')).toMatchObject({
            method: 'config.materialized',
            up: Infinity,
        });
    });

    it('reports unknown methods, empty values and @ with +', () => {
        expect(parseAtom('colour:red')).toEqual({
            code: 'unknownMethod',
            text: 'colour:red',
            method: 'colour',
        });
        expect(parseAtom('tag:')).toEqual({ code: 'noValue', text: 'tag:', method: 'tag' });
        expect(parseAtom('@+orders')).toEqual({ code: 'atWithPlus', text: '@+orders' });
        expect(parseAtom('+@orders')).toEqual({ code: 'atWithPlus', text: '+@orders' });
        expect(parseAtom('2+@orders')).toEqual({ code: 'atWithPlus', text: '2+@orders' });
        expect(parseAtom('+')).toEqual({ code: 'empty', text: '+' });
        expect(parseSelector('orders colour:red ,').errors.map(e => e.code)).toEqual(['unknownMethod']);
    });

    it('splits unions on spaces and intersections on commas, keeping quoted values whole', () => {
        const selector = parseSelector('tag:nightly,kind:model  term:"Order amount" +orders');
        expect(selector.terms.map(t => t.map(a => a.text))).toEqual([
            ['tag:nightly', 'kind:model'],
            ['term:Order amount'],
            ['+orders'],
        ]);
    });
});

describe('selector evaluation', () => {
    it('follows dbt graph operators', () => {
        expect(select('orders')).toEqual(['model.orders']);
        expect(select('+orders')).toEqual([
            'model.orders',
            'model.stg_orders',
            'source.raw.orders',
            'task.nightly.run',
        ]);
        expect(select('1+orders')).toEqual(['model.orders', 'model.stg_orders', 'task.nightly.run']);
        expect(select('stg_orders+')).toEqual(['model.orders', 'model.stg_orders']);
        expect(select('@stg_orders')).toEqual([
            'model.orders',
            'model.stg_orders',
            'source.raw.orders',
            'task.nightly.run',
        ]);
    });

    it('selects by tag, path, package, kind, materialisation, source and DAG', () => {
        expect(select('tag:nightly')).toEqual(['model.orders', 'model.stg_orders']);
        expect(select('tag:fin*')).toEqual(['model.orders']);
        expect(select('path:models/staging')).toEqual(['model.stg_orders']);
        expect(select('package:shop')).toEqual(['model.orders', 'model.stg_orders']);
        expect(select('kind:source')).toEqual(['source.raw.orders']);
        expect(select('materialized:view')).toEqual(['model.stg_orders']);
        expect(select('source:raw')).toEqual(['source.raw.orders']);
        expect(select('dag:nightly')).toEqual(['task.nightly.run']);
        expect(select('entity:order')).toEqual(['model.orders']);
    });

    it('intersects with commas, unites with spaces and excludes', () => {
        expect(select('tag:nightly,materialized:table')).toEqual(['model.orders']);
        expect(select('kind:source orders')).toEqual(['model.orders', 'source.raw.orders']);
        expect(select('+orders', 'kind:task stg_orders')).toEqual(['model.orders', 'source.raw.orders']);
    });

    it('selects a column and its lineage, with the relations that filter on it', () => {
        const result = evaluate(model, parseSelector('+orders.revenue'));
        expect([...(result.columns ?? [])].sort()).toEqual([
            'model.orders#revenue',
            'model.stg_orders#amount',
            'source.raw.orders#amount',
        ]);
        expect([...result.relations].sort()).toEqual([
            'model.orders',
            'model.stg_orders',
            'source.raw.orders',
        ]);
        expect([...result.seeds]).toEqual(['model.orders#revenue']);
        const filtered = evaluate(model, parseSelector('raw.orders.status+'));
        expect([...filtered.relations].sort()).toEqual(['model.stg_orders', 'source.raw.orders']);
    });

    it("selects a term's columns and reports what matched nothing", () => {
        const result = evaluate(model, parseSelector('term:Revenue nowhere'));
        expect([...(result.columns ?? [])]).toEqual(['model.orders#revenue']);
        expect(result.unmatched).toEqual(['nowhere']);
    });

    it('names a node the shortest way that selects it alone', () => {
        expect(reference(model, 'model.orders')).toBe('orders');
        expect(reference(model, 'source.raw.orders')).toBe('raw.orders');
        expect(reference(model, 'model.orders#revenue')).toBe('orders.revenue');
        expect(reference(model, 'term.revenue')).toBe('term:"Revenue"');
        expect(lineageOf(model, 'model.orders')).toBe('+orders+');
        expect(lineageOf(model, 'model.orders', 'up')).toBe('+orders');
        expect(lineageOf(model, 'dag.nightly')).toBe('+dag:nightly+');
        expect(lineageOf(model, 'test.shop.not_null_orders_revenue')).toBe('+orders+');
        expect(select(lineageOf(model, 'source.raw.orders'))).toEqual([
            'model.orders',
            'model.stg_orders',
            'source.raw.orders',
        ]);
    });
});
