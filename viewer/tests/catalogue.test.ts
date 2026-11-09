// Every catalogue has every message of the source catalogue, in valid ICU syntax, with the same
// arguments, so a missing or broken translation fails here (and pnpm check fails on a missing key).

import { describe, expect, it } from 'vitest';
import { argumentsOf, format, parse } from '../src/lib/icu';
import enGB from '../src/locales/en-GB';
import { CATALOGUES } from '../src/locales/index';

const keys = Object.keys(enGB).sort();

describe('message catalogues', () => {
    it('carry the locales the viewer offers', () => {
        expect(Object.keys(CATALOGUES).sort()).toEqual(['en-GB']);
    });

    for (const [locale, messages] of Object.entries(CATALOGUES)) {
        describe(locale, () => {
            it('has every key and no others', () => {
                expect(Object.keys(messages).sort()).toEqual(keys);
            });

            it('parses, keeps the arguments and formats every message', () => {
                for (const key of keys) {
                    const text = (messages as Record<string, string>)[key]!;
                    const source = (enGB as Record<string, string>)[key]!;
                    expect(text.trim(), `${locale} ${key}`).not.toBe('');
                    const parts = parse(text);
                    const wanted = [...argumentsOf(parse(source))].sort();
                    const used = [...argumentsOf(parts)].sort();
                    // A translation may leave out an argument the language does not need (a plural
                    // count in Japanese), but never invent one.
                    for (const arg of used) expect(wanted, `${locale} ${key} uses {${arg}}`).toContain(arg);
                    const values = Object.fromEntries(wanted.map(a => [a, 2]));
                    expect(() => format(parts, values, locale)).not.toThrow();
                }
            });

            it('has no em or en dashes', () => {
                for (const text of Object.values(messages)) expect(text).not.toMatch(/[–—]/);
            });
        });
    }
});
