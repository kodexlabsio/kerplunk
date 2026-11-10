import { describe, expect, it } from 'vitest';
import {
    cutDescription,
    DESCRIPTION_LIMIT,
    inline,
    MAX_QUOTE_DEPTH,
    parseMarkdown,
    plainSummary,
    safeHref,
    type Block,
} from '../src/lib/markdown';

describe('Markdown descriptions', () => {
    it('reads paragraphs, headings, lists, code, quotes and rules', () => {
        const blocks = parseMarkdown(
            '## Orders\n\nOne row per order,\nwith its total.\n\n- placed\n- shipped\n  late\n\n3. three\n4. four\n\n```sql\nselect 1\n```\n\n> a note\n\n---',
        );
        expect(blocks.map(b => b.t)).toEqual(['h', 'p', 'list', 'list', 'code', 'quote', 'hr']);
        expect(blocks[1]).toEqual({ t: 'p', c: [{ t: 'text', v: 'One row per order, with its total.' }] });
        expect(blocks[2]).toMatchObject({
            ordered: false,
            items: [[{ v: 'placed' }], [{ v: 'shipped late' }]],
        });
        expect(blocks[3]).toMatchObject({ ordered: true, start: 3 });
        expect(blocks[4]).toEqual({ t: 'code', v: 'select 1' });
    });

    it('reads tables with alignment', () => {
        const [table] = parseMarkdown(
            '| status | meaning |\n|:--|--:|\n| placed | not shipped |\n| `returned` | back \\| refunded |',
        );
        expect(table).toMatchObject({ t: 'table', align: ['left', 'right'] });
        if (table?.t !== 'table') throw new Error('not a table');
        expect(table.rows).toHaveLength(2);
        expect(table.rows[1]![0]).toEqual([{ t: 'code', v: 'returned' }]);
        expect(table.rows[1]![1]).toEqual([{ t: 'text', v: 'back | refunded' }]);
        // Short rows are not padded, long ones are cut to the header.
        const [ragged] = parseMarkdown('| a | b | c |\n|---|---|---|\n| 1 |\n| 1 | 2 | 3 | 4 |');
        if (ragged?.t !== 'table') throw new Error('not a table');
        expect(ragged.rows.map(row => row.length)).toEqual([1, 3]);
        // A header wider than 64 columns is a paragraph.
        expect(parseMarkdown('|'.repeat(66) + '\n-\n|')[0]?.t).toBe('p');
        expect(parseMarkdown('  ```sql  \nselect 1\n  ```')).toEqual([{ t: 'code', v: 'select 1' }]);
    });

    it('reads emphasis, code and links, but only web and mail links', () => {
        expect(
            inline('**total** of *paid* `amount`, see [docs](https://example.com/x) or <mailto:a@b.c>'),
        ).toEqual([
            { t: 'strong', c: [{ t: 'text', v: 'total' }] },
            { t: 'text', v: ' of ' },
            { t: 'em', c: [{ t: 'text', v: 'paid' }] },
            { t: 'text', v: ' ' },
            { t: 'code', v: 'amount' },
            { t: 'text', v: ', see ' },
            { t: 'link', href: 'https://example.com/x', c: [{ t: 'text', v: 'docs' }] },
            { t: 'text', v: ' or ' },
            { t: 'link', href: 'mailto:a@b.c', c: [{ t: 'text', v: 'a@b.c' }] },
        ]);
        expect(inline('[click](javascript:alert(1))')).toEqual([{ t: 'text', v: 'click' }]);
        expect(inline('[x](data:text/html,hi)')).toEqual([{ t: 'text', v: 'x' }]);
        expect(safeHref(' HTTPS://example.com ')).toBe('HTTPS://example.com');
        expect(safeHref('/relative')).toBeNull();
    });

    it('leaves snake_case and markup as text', () => {
        expect(inline('customer_lifetime_value and <b>bold</b>')).toEqual([
            { t: 'text', v: 'customer_lifetime_value and <b>bold</b>' },
        ]);
    });

    it('summarises the first paragraph as plain text', () => {
        expect(plainSummary('## Title\n\nMore')).toBe('Title');
        expect(plainSummary('**Paid** orders, [docs](https://x.y)\n\nSecond')).toBe('Paid orders, docs');
        expect(plainSummary('')).toBe('');
    });

    it('reads quotes no deeper than eight, and the rest as text', () => {
        let blocks: Block[] = parseMarkdown('>'.repeat(10000));
        let depth = 0;
        while (blocks[0]?.t === 'quote') {
            blocks = blocks[0].c;
            depth++;
        }
        expect(depth).toBe(MAX_QUOTE_DEPTH);
        expect(MAX_QUOTE_DEPTH).toBe(8);
        expect(blocks).toEqual([{ t: 'p', c: [{ t: 'text', v: '>'.repeat(10000 - 8) }] }]);
        expect(parseMarkdown('> > a\n> > b')).toEqual([
            { t: 'quote', c: [{ t: 'quote', c: [{ t: 'p', c: [{ t: 'text', v: 'a b' }] }] }] },
        ]);
    });

    it('keeps two-space line breaks and code spans of matching length', () => {
        expect(inline('one  \ntwo\nthree')).toEqual([
            { t: 'text', v: 'one' },
            { t: 'br' },
            { t: 'text', v: 'two three' },
        ]);
        expect(inline('``a ` b`` and `c`')).toEqual([
            { t: 'code', v: 'a ` b' },
            { t: 'text', v: ' and ' },
            { t: 'code', v: 'c' },
        ]);
        expect(inline('```a``')).toEqual([{ t: 'text', v: '```a``' }]);
        expect(inline('__strong__ and _em_, *a* **b**')).toEqual([
            { t: 'strong', c: [{ t: 'text', v: 'strong' }] },
            { t: 'text', v: ' and ' },
            { t: 'em', c: [{ t: 'text', v: 'em' }] },
            { t: 'text', v: ', ' },
            { t: 'em', c: [{ t: 'text', v: 'a' }] },
            { t: 'text', v: ' ' },
            { t: 'strong', c: [{ t: 'text', v: 'b' }] },
        ]);
        // As in CommonMark, links do not nest: the inner one wins and the outer brackets are text.
        expect(inline('[a [b] c](https://x.y) [[n](https://n.m)](https://o.p)')).toEqual([
            { t: 'link', href: 'https://x.y', c: [{ t: 'text', v: 'a [b] c' }] },
            { t: 'text', v: ' [' },
            { t: 'link', href: 'https://n.m', c: [{ t: 'text', v: 'n' }] },
            { t: 'text', v: '](https://o.p)' },
        ]);
        expect(inline('[*<https://n.m>*](https://o.p)')).toEqual([
            { t: 'text', v: '[' },
            { t: 'em', c: [{ t: 'link', href: 'https://n.m', c: [{ t: 'text', v: 'https://n.m' }] }] },
            { t: 'text', v: '](https://o.p)' },
        ]);
        // Titles, spaces and angle brackets around the address.
        const docs = { t: 'link', href: 'https://x.y/a_(b)', c: [{ t: 'text', v: 'docs' }] };
        expect(inline('[docs](https://x.y/a_(b))')).toEqual([docs]);
        expect(inline('[docs](  https://x.y/a_(b)  "The docs" )')).toEqual([docs]);
        expect(inline('[docs](<https://x.y/a_(b)>)')).toEqual([docs]);
        expect(inline('[docs](https://x.y "unclosed)')).toEqual([
            { t: 'text', v: '[docs](https://x.y "unclosed)' },
        ]);
        expect(inline('<https://x.y> <https://<b>')).toEqual([
            { t: 'link', href: 'https://x.y', c: [{ t: 'text', v: 'https://x.y' }] },
            { t: 'text', v: ' <https://<b>' },
        ]);
    });

    it('summarises without reading past the first paragraph', () => {
        expect(plainSummary('```\ncode\n```\n\n> quoted *first*\n\nlater')).toBe('quoted first');
        expect(plainSummary('| a |\n|---|\n| b |\n\n- one\n- two')).toBe('one; two');
        expect(plainSummary('first\n\n' + '>'.repeat(10000))).toBe('first');
    });

    it('cuts a long description and says so', () => {
        expect(cutDescription('short')).toEqual({ text: 'short', cut: false });
        const long = 'a'.repeat(DESCRIPTION_LIMIT + 10);
        expect(cutDescription(long)).toEqual({ text: 'a'.repeat(DESCRIPTION_LIMIT), cut: true });
    });
});
