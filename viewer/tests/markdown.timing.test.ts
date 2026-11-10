// Markdown is read in linear time, whatever the description. Structural cases are checked by the
// size of what the reader makes, which a machine's speed cannot change; the others by how their
// time grows when the input is eight times longer, which a linear reader keeps near eight and a
// quadratic one takes to 64; the bound allows 24, and 500 ms of slack for a busy machine. This file runs alone in one fork (vite.config.ts), so other tests do
// not share its processor.

import { describe, expect, it } from 'vitest';
import { parseMarkdown, plainSummary } from '../src/lib/markdown';

const N = 32 * 1024;

function read(source: string): unknown {
    return [parseMarkdown(source), plainSummary(source)];
}

function once(source: string): number {
    const started = performance.now();
    read(source);
    return performance.now() - started;
}

function best(source: string, runs = 5): number {
    let fastest = Infinity;
    for (let run = 0; run < runs; run++) fastest = Math.min(fastest, once(source));
    return fastest;
}

/** Inputs of about `size` characters that a parser could take quadratic time over. */
const timed: [string, (size: number) => string][] = [
    ['[', size => '['.repeat(size)],
    ['_a', size => '_a '.repeat(size / 3)],
    ['*a', size => '*a '.repeat(size / 3)],
    ['__a', size => '__a '.repeat(size / 4)],
    ['backticks', size => '`'.repeat(size)],
    [
        'backtick runs',
        size => Array.from({ length: Math.sqrt(2 * size) }, (_, k) => '`'.repeat(k + 1)).join('a'),
    ],
    ['[a](', size => '[a]('.repeat(size / 4)],
    ['[](x)', size => '[]('.concat('[](x)'.repeat(size / 5))],
    ['spaces in an address', size => '[a](' + ' '.repeat(size) + 'x'],
    ['unclosed autolinks', size => '<https://'.repeat(size / 9)],
    ['a fence line', size => '```' + ' '.repeat(size) + 'x!'],
    ['a heading', size => '# a' + '#'.repeat(size) + 'b'],
    ['a table rule', size => '| a |\n|-' + ' '.repeat(size) + 'x'],
    ['a paragraph with line breaks', size => 'word word word  \nword\n'.repeat(size / 22)],
];

/** Inputs whose output a parser could make far larger than they are. */
const structural: [string, (size: number) => string][] = [
    ['a wide header over many rows', size => '|'.repeat(size / 4) + '\n-\n' + '|\n'.repeat(size / 4)],
    ['a 64-column header over many rows', size => '|'.repeat(65) + '\n-\n' + '|\n'.repeat(size / 2)],
    ['deep quotes', size => '>'.repeat(size)],
    ['short paragraphs', size => 'One short paragraph, with `code`.\n\n'.repeat(size / 36)],
    ['a long paragraph', size => 'word word word word  \nword word\n'.repeat(size / 31)],
];

describe('Markdown in linear time', () => {
    for (const [name, make] of timed) {
        it(`reads ${name}`, { timeout: 30_000 }, () => {
            const bound = 24 * best(make(N)) + 500;
            // A quadratic reader is far past the bound on its first run; only a slow first run
            // is repeated, to tell a busy machine from a slow reader.
            const source = make(8 * N);
            const first = once(source);
            const large = first <= bound ? first : Math.min(first, best(source));
            expect(large).toBeLessThanOrEqual(bound);
        });
    }
    for (const [name, make] of structural) {
        it(`makes no more than it reads of ${name}`, { timeout: 30_000 }, () => {
            const source = make(8 * N);
            expect(JSON.stringify(parseMarkdown(source)).length).toBeLessThanOrEqual(
                4 * source.length + 1000,
            );
            expect(plainSummary(source).length).toBeLessThanOrEqual(source.length);
        });
    }
});
