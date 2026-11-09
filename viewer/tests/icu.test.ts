import { describe, expect, it } from 'vitest';
import { argumentsOf, format, MessageError, parse } from '../src/lib/icu';

const f = (text: string, values: Record<string, string | number> = {}, locale = 'en-GB'): string =>
    format(parse(text), values, locale);

describe('ICU messages', () => {
    it('interpolates and formats numbers in the locale', () => {
        expect(f('Hello {name}', { name: 'Ada' })).toBe('Hello Ada');
        expect(f('{n, number} rows', { n: 12345.5 })).toBe('12,345.5 rows');
        expect(f('{n, number} Zeilen', { n: 12345.5 }, 'de')).toBe('12.345,5 Zeilen');
        expect(f('{n, number, percent}', { n: 0.25 })).toBe('25%');
    });

    it('chooses plural forms with the locale rules, exact matches first', () => {
        const message = '{count, plural, =0 {none} one {# model} other {# models}}';
        expect(f(message, { count: 0 })).toBe('none');
        expect(f(message, { count: 1 })).toBe('1 model');
        expect(f(message, { count: 2500 })).toBe('2,500 models');
        const polish = '{count, plural, one {# model} few {# modele} many {# modeli} other {# modelu}}';
        expect(f(polish, { count: 3 }, 'pl')).toBe('3 modele');
        expect(f(polish, { count: 5 }, 'pl')).toBe('5 modeli');
        const arabic =
            '{count, plural, zero {لا شيء} one {واحد} two {اثنان} few {# قليل} many {# كثير} other {# آخر}}';
        expect(f(arabic, { count: 2 }, 'ar')).toBe('اثنان');
    });

    it('selects, nests and quotes', () => {
        expect(f('{s, select, approved {ok} other {not yet}}', { s: 'approved' })).toBe('ok');
        expect(f('{s, select, approved {ok} other {not yet}}', { s: 'x' })).toBe('not yet');
        expect(f("It''s '{literal}'")).toBe("It's {literal}");
        expect(f('{a, plural, one {{b} and # more} other {{b} and # others}}', { a: 3, b: 'x' })).toBe(
            'x and 3 others',
        );
    });

    it('rejects broken messages', () => {
        expect(() => parse('{unclosed')).toThrow(MessageError);
        expect(() => parse('{n, plural, one {x}}')).toThrow('needs an "other"');
        expect(() => parse('{n, wat}')).toThrow('unknown type');
        expect(() => parse('stray }')).toThrow(MessageError);
    });

    it('lists the arguments a message uses', () => {
        expect([...argumentsOf(parse('{a} {b, plural, other {{c}}}'))].sort()).toEqual(['a', 'b', 'c']);
    });
});
