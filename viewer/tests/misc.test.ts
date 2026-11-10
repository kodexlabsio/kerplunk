import { describe, expect, it } from 'vitest';
import { escapeXml, fileName, pngScale, resolveVars, svgDocument } from '../src/lib/export';
import { GraphModel } from '../src/lib/model';
import { lines, tokenize } from '../src/lib/sql';
import { overview, share } from '../src/lib/stats';
import { classify } from '../src/lib/transform';
import { buildTree, flatten, pathTo } from '../src/lib/tree';
import { scrollFor, windowOf } from '../src/lib/virtual';
import { graph } from './fixture';

const model = new GraphModel(graph);
const labels = {
    sources: 'Sources',
    exposures: 'Exposures',
    project: 'Project',
    noSchema: '(none)',
    terms: 'Terms',
    entities: 'Entities',
    tests: 'Tests',
};
const compare = (a: string, b: string): number => a.localeCompare(b);

describe('transforms', () => {
    it('tells copies, renames, casts, calculations, aggregations and windows apart', () => {
        expect(classify('direct', undefined, 'id', 'id')).toBe('direct');
        expect(classify('direct', undefined, 'order_id', 'id')).toBe('renamed');
        expect(classify('transformed', 'CAST(a.x AS INT)')).toBe('cast');
        expect(classify('transformed', 'a.x::date')).toBe('cast');
        expect(classify('transformed', 'CAST(a.x AS INT) + 1')).toBe('expression');
        expect(classify('transformed', 'a.amount / 100')).toBe('expression');
        expect(classify('transformed', 'SUM(p.amount)')).toBe('aggregate');
        expect(classify('transformed', 'ROW_NUMBER() OVER (PARTITION BY a)')).toBe('window');
        expect(model.transformOf(graph.edges!.find(e => e.source === 'model.orders#revenue')!)).toBe(
            'aggregate',
        );
    });
});

describe('navigation trees', () => {
    it('groups the project by folder, with sources and DAGs apart', () => {
        const project = buildTree(model, 'project', labels, compare);
        expect(project.map(i => i.label)).toEqual(['models', 'Sources']);
        const rows = flatten(project, new Set(['pkg:shop/models', 'pkg:shop/models/marts']));
        expect(rows.map(r => [r.item.label, r.depth])).toEqual([
            ['models', 0],
            ['marts', 1],
            ['orders', 2],
            ['staging', 1],
            ['Sources', 0],
        ]);
        expect(pathTo(project, 'model.stg_orders')).toEqual(['pkg:shop/models', 'pkg:shop/models/staging']);
        expect(buildTree(model, 'pipelines', labels, compare)[0]).toMatchObject({
            label: 'nightly',
            href: '#/node/dag.nightly',
        });
        expect(buildTree(model, 'database', labels, compare).map(i => i.label)).toEqual(['main']);
    });

    it('filters to the branches that lead to a match, opening them', () => {
        const rows = flatten(buildTree(model, 'project', labels, compare), new Set(), 'stg');
        expect(rows.map(r => r.item.label)).toEqual(['models', 'staging', 'stg_orders']);
        expect(rows[2]).toMatchObject({ index: 0, siblings: 1, parent: 'pkg:shop/models/staging' });
    });
});

describe('overview numbers', () => {
    it('counts coverage of lineage, tests and descriptions', () => {
        const o = overview(model, ['dbt: one', 'dbt: two', 'airflow: three']);
        expect(o.counts.model).toBe(2);
        expect(o.lineage).toEqual({ done: 2, total: 3 });
        expect(o.tested).toEqual({ done: 1, total: 2 });
        expect(o.verified).toEqual({ done: 1, total: 2 });
        expect(o.warnings).toEqual([
            ['dbt', 2],
            ['airflow', 1],
        ]);
        expect(o.central[0]).toEqual(['source.raw.orders', 2]);
        expect(share({ done: 1, total: 4 })).toBe(0.25);
        expect(share({ done: 0, total: 0 })).toBe(0);
    });
});

describe('SQL highlighting', () => {
    it('splits keywords, strings, comments, Jinja and functions, keeping all the text', () => {
        const sql = "select sum(amount) -- total\nfrom {{ ref('orders') }} where status = 'paid'";
        const tokens = tokenize(sql);
        expect(tokens.map(t => t.text).join('')).toBe(sql);
        expect(tokens.filter(t => t.kind === 'keyword').map(t => t.text)).toEqual([
            'select',
            'from',
            'where',
        ]);
        expect(tokens.find(t => t.kind === 'function')?.text).toBe('sum');
        expect(tokens.find(t => t.kind === 'jinja')?.text).toBe("{{ ref('orders') }}");
        expect(tokens.find(t => t.kind === 'string')?.text).toBe("'paid'");
        expect(tokens.find(t => t.kind === 'comment')?.text).toBe('-- total');
        expect(lines(sql)).toHaveLength(2);
    });
});

describe('export', () => {
    it('resolves variables, escapes text and wraps the drawing', () => {
        expect(
            resolveVars('a{fill:var(--x)} b{stroke:var(--y, red)}', n => (n === '--x' ? '#fff' : '')),
        ).toBe('a{fill:#fff} b{stroke:red}');
        expect(escapeXml('<a & "b">')).toBe('&lt;a &amp; &quot;b&quot;&gt;');
        const doc = svgDocument('<g/>', '', { x: -10, y: 20, w: 100, h: 50 }, '.x{}', '#000', 'Lineage <1>');
        expect(doc).toContain('width="180" height="130"');
        expect(doc).toContain('translate(50,20)');
        expect(doc).toContain('<title>Lineage &lt;1&gt;</title>');
        expect(fileName('jaffle shop', 'png')).toBe('jaffle-shop-lineage.png');
        expect(pngScale(1000, 1000)).toBe(2);
        expect(pngScale(40000, 1000)).toBeLessThan(1);
        // About 32 megapixels at most.
        const scale = pngScale(6000, 6000);
        expect(6000 * scale * 6000 * scale).toBeLessThan(32_000_001);
    });
});

describe('windowing', () => {
    it('renders the rows near the window and scrolls a row into view', () => {
        expect(windowOf(1000, 30, 3000, 300, 5)).toEqual({ start: 95, end: 115, before: 2850, after: 26550 });
        expect(windowOf(0, 30, 0, 300)).toEqual({ start: 0, end: 0, before: 0, after: 0 });
        expect(scrollFor(50, 30, 0, 300)).toBe(1230);
        expect(scrollFor(5, 30, 0, 300)).toBeNull();
        expect(scrollFor(5, 30, 600, 300)).toBe(150);
    });
});
