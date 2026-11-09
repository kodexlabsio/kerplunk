// A small ICU MessageFormat subset: {name}, {n, number}, {n, number, percent}, {d, date, short},
// {n, plural, =0 {...} one {# item} other {# items}}, {n, selectordinal, ...} and
// {x, select, a {...} other {...}}. Quoting follows ICU: '' is an apostrophe, and '{...}' is literal.
// Formatting uses the browser's Intl, so nothing is bundled for plural rules or number formats.

export type Part =
    | string
    | { arg: string; type: 'value' }
    | { arg: string; type: 'number'; style?: string }
    | { arg: string; type: 'date'; style?: string }
    | {
          arg: string;
          type: 'plural' | 'selectordinal' | 'select';
          offset: number;
          options: Record<string, Part[]>;
      }
    | { type: 'hash' };

export class MessageError extends Error {}

const SPACE = /\s/;

class Parser {
    i = 0;
    constructor(readonly text: string) {}

    fail(message: string): never {
        throw new MessageError(`${message} at ${this.i} in "${this.text}"`);
    }

    skip(): void {
        while (this.i < this.text.length && SPACE.test(this.text[this.i]!)) this.i++;
    }

    word(): string {
        this.skip();
        const start = this.i;
        while (this.i < this.text.length && /[^\s{},#]/.test(this.text[this.i]!)) this.i++;
        if (start === this.i) this.fail('expected a name');
        return this.text.slice(start, this.i);
    }

    expect(char: string): void {
        this.skip();
        if (this.text[this.i] !== char) this.fail(`expected "${char}"`);
        this.i++;
    }

    /** Parts until the end, or until an unmatched "}" when nested. */
    message(nested: boolean, plural: boolean): Part[] {
        const parts: Part[] = [];
        let literal = '';
        const flush = (): void => {
            if (literal) parts.push(literal);
            literal = '';
        };
        while (this.i < this.text.length) {
            const c = this.text[this.i]!;
            if (c === "'") {
                const next = this.text[this.i + 1];
                if (next === "'") {
                    literal += "'";
                    this.i += 2;
                } else if (next === '{' || next === '}' || (plural && next === '#')) {
                    const end = this.text.indexOf("'", this.i + 1);
                    if (end < 0) this.fail('unclosed quote');
                    literal += this.text.slice(this.i + 1, end);
                    this.i = end + 1;
                } else {
                    literal += c;
                    this.i++;
                }
            } else if (c === '{') {
                flush();
                this.i++;
                parts.push(this.argument());
            } else if (c === '}') {
                if (!nested) this.fail('unmatched "}"');
                flush();
                return parts;
            } else if (c === '#' && plural) {
                flush();
                parts.push({ type: 'hash' });
                this.i++;
            } else {
                literal += c;
                this.i++;
            }
        }
        if (nested) this.fail('unclosed "{"');
        flush();
        return parts;
    }

    argument(): Part {
        const arg = this.word();
        this.skip();
        if (this.text[this.i] === '}') {
            this.i++;
            return { arg, type: 'value' };
        }
        this.expect(',');
        const type = this.word();
        this.skip();
        if (type === 'number' || type === 'date') {
            let style: string | undefined;
            if (this.text[this.i] === ',') {
                this.i++;
                style = this.word();
            }
            this.expect('}');
            return { arg, type, style };
        }
        if (type !== 'plural' && type !== 'select' && type !== 'selectordinal')
            this.fail(`unknown type ${type}`);
        this.expect(',');
        let offset = 0;
        const options: Record<string, Part[]> = {};
        for (;;) {
            this.skip();
            if (this.text[this.i] === '}') {
                this.i++;
                break;
            }
            const key = this.word();
            if (key.startsWith('offset:')) {
                offset = Number(key.slice(7));
                continue;
            }
            this.expect('{');
            options[key] = this.message(true, type !== 'select');
            this.i++;
        }
        if (!options.other) this.fail(`${type} needs an "other" option`);
        return { arg, type, offset, options };
    }
}

export function parse(text: string): Part[] {
    return new Parser(text).message(false, false);
}

export type Values = Record<string, string | number | Date | undefined>;

const numberFormats = new Map<string, Intl.NumberFormat>();
const pluralRules = new Map<string, Intl.PluralRules>();
const dateFormats = new Map<string, Intl.DateTimeFormat>();

function cached<T>(map: Map<string, T>, key: string, make: () => T): T {
    let found = map.get(key);
    if (!found) {
        found = make();
        map.set(key, found);
    }
    return found;
}

export function formatNumber(locale: string, value: number, style?: string): string {
    return cached(
        numberFormats,
        `${locale}|${style ?? ''}`,
        () =>
            new Intl.NumberFormat(
                locale,
                style === 'percent'
                    ? { style: 'percent', maximumFractionDigits: 0 }
                    : { maximumFractionDigits: 2 },
            ),
    ).format(value);
}

export function formatDate(locale: string, value: Date, style?: string): string {
    const dateStyle = style === 'short' || style === 'long' || style === 'full' ? style : 'medium';
    return cached(
        dateFormats,
        `${locale}|${dateStyle}`,
        () =>
            new Intl.DateTimeFormat(locale, { dateStyle, timeStyle: style === 'long' ? 'short' : undefined }),
    ).format(value);
}

export function format(parts: Part[], values: Values, locale: string, hash?: number): string {
    let out = '';
    for (const part of parts) {
        if (typeof part === 'string') {
            out += part;
            continue;
        }
        if (part.type === 'hash') {
            out += hash === undefined ? '#' : formatNumber(locale, hash);
            continue;
        }
        const value = values[part.arg];
        if (part.type === 'value')
            out += value instanceof Date ? formatDate(locale, value) : String(value ?? '');
        else if (part.type === 'number') out += formatNumber(locale, Number(value ?? 0), part.style);
        else if (part.type === 'date')
            out += formatDate(locale, value instanceof Date ? value : new Date(String(value)), part.style);
        else if (part.type === 'select') {
            const branch = part.options[String(value)] ?? part.options.other!;
            out += format(branch, values, locale, hash);
        } else {
            const n = Number(value ?? 0) - part.offset;
            const exact = part.options[`=${Number(value ?? 0)}`];
            const rules = cached(
                pluralRules,
                `${locale}|${part.type}`,
                () => new Intl.PluralRules(locale, { type: part.type === 'plural' ? 'cardinal' : 'ordinal' }),
            );
            const branch = exact ?? part.options[rules.select(n)] ?? part.options.other!;
            out += format(branch, values, locale, n);
        }
    }
    return out;
}

/** The argument names a message uses, for checking that translations keep them. */
export function argumentsOf(parts: Part[], found = new Set<string>()): Set<string> {
    for (const part of parts) {
        if (typeof part === 'string' || part.type === 'hash') continue;
        found.add(part.arg);
        if ('options' in part) for (const branch of Object.values(part.options)) argumentsOf(branch, found);
    }
    return found;
}
