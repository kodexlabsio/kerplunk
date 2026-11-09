import { describe, expect, it } from 'vitest';
import { GraphModel } from '../src/lib/model';
import { buildIndex, fuzzy, parseQuery, search } from '../src/lib/search';
import { graph } from './fixture';

const model = new GraphModel(graph);
const index = buildIndex(model, { entities: [{ name: 'order', key: 'order_id' }] });
const ids = (query: string, kinds?: string[]): string[] =>
    search(index, query, kinds ? { kinds } : {}).hits.map(h => h.entry.id);

describe('fuzzy matching', () => {
    it('prefers substrings at word starts, then letters in order', () => {
        expect(fuzzy('stg_orders.amount', 'amount')!.score).toBeGreaterThan(
            fuzzy('stg_orders.amount', 'moun')!.score,
        );
        expect(fuzzy('stg_orders.amount', 'soa')!.marks).toEqual([0, 4, 11]);
        expect(fuzzy('orders', 'xyz')).toBeNull();
        expect(fuzzy('orders', 'orders')!.score).toBeGreaterThan(fuzzy('orders_x', 'orders')!.score);
    });
});

describe('search', () => {
    it('ranks an exact synonym first', () => {
        expect(ids('sales')[0]).toBe('term.revenue');
    });

    it('matches word starts in labels and needs every word', () => {
        expect(ids('orders amount')).toEqual(['source.raw.orders#amount', 'model.stg_orders#amount']);
        expect(search(index, 'orders zzz').total).toBe(0);
        expect(search(index, '  ').hits).toEqual([]);
    });

    it('matches abbreviations fuzzily and highlights the letters', () => {
        const hit = search(index, 'stgord').hits[0]!;
        expect(hit.entry.id).toBe('model.stg_orders');
        expect(hit.marks).toEqual([0, 1, 2, 4, 5, 6]);
    });

    it('filters by kind, tag and package, in the query or beside it', () => {
        expect(ids('orders kind:model')).toEqual(['model.orders', 'model.stg_orders']);
        expect(ids('orders', ['source'])).toEqual(['source.raw.orders']);
        expect(ids('tag:finance')).toContain('model.orders');
        expect(ids('tag:finance')).not.toContain('model.stg_orders');
        expect(ids('revenue package:shop')[0]).toBe('model.orders#revenue');
        expect(ids('revenue package:other')).toEqual([]);
        expect(parseQuery('Orders kind:model,seed pkg:shop').filters).toEqual({
            kinds: ['model', 'seed'],
            package: 'shop',
        });
    });

    it('finds entities from the ontology', () => {
        expect(ids('order', ['entity'])).toEqual(['entity:order']);
    });
});
