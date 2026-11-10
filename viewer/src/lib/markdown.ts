// A small Markdown reader for descriptions, as dbt's documentation renders them: paragraphs,
// headings, lists, tables, code, quotes, emphasis and links. It returns tokens, which a component
// renders as elements and text, so a description can never inject markup. Links are kept only
// when they go to http(s) or mailto addresses.

export type Inline =
    | { t: 'text'; v: string }
    | { t: 'code'; v: string }
    | { t: 'em' | 'strong'; c: Inline[] }
    | { t: 'link'; href: string; c: Inline[] }
    | { t: 'br' };

export type Block =
    | { t: 'p'; c: Inline[] }
    | { t: 'h'; level: number; c: Inline[] }
    | { t: 'list'; ordered: boolean; start: number; items: Inline[][] }
    | { t: 'code'; v: string }
    | { t: 'quote'; c: Block[] }
    | { t: 'table'; align: ('left' | 'right' | 'center' | null)[]; head: Inline[][]; rows: Inline[][][] }
    | { t: 'hr' };

const SAFE_LINK = /^(https?:\/\/|mailto:)/i;

export function safeHref(href: string): string | null {
    const trimmed = href.trim();
    return SAFE_LINK.test(trimmed) ? trimmed : null;
}

/** Quotes nested deeper than this are read as text, so a description cannot exhaust the stack. */
export const MAX_QUOTE_DEPTH = 8;
// Emphasis and links nest only a few levels in practice; deeper is read as text.
const MAX_INLINE_DEPTH = 8;
/** A description longer than this is cut, with a note, as a long SQL file is. */
export const DESCRIPTION_LIMIT = 100_000;

/** The part of a description the page renders, and whether it was cut at DESCRIPTION_LIMIT. */
export function cutDescription(source: string): { text: string; cut: boolean } {
    return source.length > DESCRIPTION_LIMIT
        ? { text: source.slice(0, DESCRIPTION_LIMIT), cut: true }
        : { text: source, cut: false };
}

function pushText(out: Inline[], text: string): void {
    if (!text) return;
    const last = out[out.length - 1];
    if (last?.t === 'text') last.v += text;
    else out.push({ t: 'text', v: text });
}

const SPECIAL = /[\\`\n<[*_]/g;
const ESCAPABLE = /[\\`*_[\]()<>#+\-.!|]/;
// An address stops at the next "<", so a run of unclosed ones is not rescanned from each.
const AUTOLINK = /<((?:https?:\/\/|mailto:)[^\s<>]+)>/iy;
// The lookahead and back reference make the leading spaces atomic: without them, the spaces
// before and after an empty address are shared out in every possible way before failing.
const DESTINATION = /\((?=(\s*))\1<?((?:[^()\s<>]|\([^()\s]*\))*)>?\s*(?:"[^"]*"\s*)?\)/y;
const SPACE = /\s/;
const WORD = /\w/;

/**
 * Where each opener closes, found once for the whole text, so that reading it is linear: an
 * opener that finds no closer does not rescan the rest of the text, as a regular expression would.
 */
class Closers {
    private readonly text: string;
    private readonly lists = new Map<string, { at: number[]; next: number }>();
    private brackets: number[] | null = null;
    private labelEnds: Int32Array | null = null;
    private bracket = 0;
    private runs: { at: number[]; length: number[]; same: Int32Array } | null = null;
    private run = 0;

    constructor(text: string) {
        this.text = text;
    }

    /** The first closer of `kind` at or after `from`: `c` closes emphasis, `cc` strong emphasis. */
    emphasis(kind: string, from: number): number {
        let list = this.lists.get(kind);
        if (!list) {
            const text = this.text;
            const c = kind[0]!;
            const at: number[] = [];
            for (let j = text.indexOf(c, 1); j >= 0; j = text.indexOf(c, j + 1)) {
                if (SPACE.test(text[j - 1]!)) continue;
                if (kind.length === 2 ? text[j + 1] === c : text[j + 1] !== c) at.push(j);
            }
            list = { at, next: 0 };
            this.lists.set(kind, list);
        }
        // Openers are met left to right, so each list is walked once.
        while (list.next < list.at.length && list.at[list.next]! < from) list.next++;
        return list.next < list.at.length ? list.at[list.next]! : -1;
    }

    /**
     * Where the label of a link opened by the "[" at `open` ends: its "]", with one level of
     * nested brackets allowed inside, or -1.
     */
    labelEnd(open: number): number {
        if (!this.brackets) {
            const text = this.text;
            const brackets: number[] = [];
            for (let j = 0; j < text.length; j++) if (text[j] === '[' || text[j] === ']') brackets.push(j);
            // From the bracket at k, scanning right: a "]" ends the label, and a "[" opens a
            // nested pair that ends at the next "]", after which the scan goes on. Computed from
            // the right, each bracket's answer reuses one already known.
            const ends = new Int32Array(brackets.length + 1).fill(-1);
            let close = -1;
            for (let k = brackets.length - 1; k >= 0; k--) {
                if (text[brackets[k]!] === ']') {
                    close = k;
                    ends[k] = brackets[k]!;
                } else ends[k] = close < 0 ? -1 : ends[close + 1]!;
            }
            this.brackets = brackets;
            this.labelEnds = ends;
        }
        const brackets = this.brackets;
        while (this.bracket < brackets.length && brackets[this.bracket]! < open) this.bracket++;
        return this.labelEnds![this.bracket + 1]!;
    }

    /** The run of backticks at `start`: its length, and where the next run of that length starts. */
    ticks(start: number): { length: number; close: number } {
        if (!this.runs) {
            const text = this.text;
            const at: number[] = [];
            const length: number[] = [];
            for (let j = text.indexOf('`'); j >= 0;) {
                let e = j;
                while (text[e] === '`') e++;
                at.push(j);
                length.push(e - j);
                j = text.indexOf('`', e);
            }
            const same = new Int32Array(at.length).fill(-1);
            const last = new Map<number, number>();
            for (let r = at.length - 1; r >= 0; r--) {
                same[r] = last.get(length[r]!) ?? -1;
                last.set(length[r]!, r);
            }
            this.runs = { at, length, same };
        }
        const runs = this.runs;
        while (this.run < runs.at.length && runs.at[this.run]! < start) this.run++;
        const r = this.run;
        if (runs.at[r] !== start) {
            // The rest of a run whose first backtick was escaped: text.
            let end = start;
            while (this.text[end] === '`') end++;
            return { length: end - start, close: -1 };
        }
        const close = runs.same[r]!;
        return { length: runs.length[r]!, close: close < 0 ? -1 : runs.at[close]! };
    }
}

/** Inline Markdown: `code`, **strong**, *em*, [text](url), <url> and two-space line breaks. */
export function inline(text: string): Inline[] {
    return readInline(text, 0, { link: false });
}

/** `seen.link` is set when a link is read, so a label holding one is not a link itself. */
function readInline(text: string, depth: number, seen: { link: boolean }): Inline[] {
    const out: Inline[] = [];
    if (depth > MAX_INLINE_DEPTH) {
        pushText(out, text.replace(/\n/g, ' '));
        return out;
    }
    const closers = new Closers(text);
    let i = 0;
    while (i < text.length) {
        // Set on every pass: the calls for nested emphasis and labels move it.
        SPECIAL.lastIndex = i;
        const found = SPECIAL.exec(text);
        const at = found ? found.index : text.length;
        pushText(out, text.slice(i, at));
        i = at;
        if (!found) break;
        const c = text[i]!;
        if (c === '\\' && i + 1 < text.length && ESCAPABLE.test(text[i + 1]!)) {
            pushText(out, text[i + 1]!);
            i += 2;
            continue;
        }
        if (c === '`') {
            // A code span closes at the next run of backticks of the same length; a run that
            // none closes is text.
            const { length, close } = closers.ticks(i);
            if (close > 0) {
                out.push({ t: 'code', v: text.slice(i + length, close).trim() });
                i = close + length;
            } else {
                pushText(out, text.slice(i, i + length));
                i += length;
            }
            continue;
        }
        if (c === '\n') {
            if (i >= 2 && text[i - 1] === ' ' && text[i - 2] === ' ') {
                const last = out[out.length - 1];
                if (last?.t === 'text') {
                    let end = last.v.length;
                    while (end > 0 && last.v[end - 1] === ' ') end--;
                    if (end) last.v = last.v.slice(0, end);
                    else out.pop();
                }
                out.push({ t: 'br' });
            } else pushText(out, ' ');
            i++;
            continue;
        }
        if (c === '<') {
            AUTOLINK.lastIndex = i;
            const m = AUTOLINK.exec(text);
            if (m) {
                seen.link = true;
                out.push({ t: 'link', href: m[1]!, c: [{ t: 'text', v: m[1]!.replace(/^mailto:/i, '') }] });
                i += m[0].length;
                continue;
            }
        }
        if (c === '[') {
            const end = closers.labelEnd(i);
            if (end > 0 && text[end + 1] === '(') {
                DESTINATION.lastIndex = end + 1;
                const m = DESTINATION.exec(text);
                const inner = { link: false };
                const label = m ? readInline(text.slice(i + 1, end), depth + 1, inner) : [];
                // As in CommonMark, links do not nest: the inner one is the link, and the outer
                // brackets are text.
                if (m && !inner.link) {
                    seen.link = true;
                    const href = safeHref(m[2]!);
                    if (href) out.push({ t: 'link', href, c: label });
                    else
                        for (const piece of label) {
                            if (piece.t === 'text') pushText(out, piece.v);
                            else out.push(piece);
                        }
                    i = end + 1 + m[0].length;
                    continue;
                }
            }
        }
        if (c === '*' || c === '_') {
            const wordInside = c === '_' && i > 0 && WORD.test(text[i - 1]!);
            if (!wordInside) {
                // Strong: "cc", text that starts and ends with a non-space, then the first "cc".
                if (text[i + 1] === c && i + 2 < text.length && !SPACE.test(text[i + 2]!)) {
                    const close = closers.emphasis(c + c, i + 3);
                    if (close > 0) {
                        out.push({ t: 'strong', c: readInline(text.slice(i + 2, close), depth + 1, seen) });
                        i = close + 2;
                        continue;
                    }
                }
                // Emphasis: "c", the same, then the first single "c"; "_" also not inside a word.
                if (i + 1 < text.length && !SPACE.test(text[i + 1]!)) {
                    const close = closers.emphasis(c, i + 2);
                    const after = close > 0 ? text[close + 1] : undefined;
                    if (close > 0 && !(c === '_' && after && WORD.test(after))) {
                        out.push({ t: 'em', c: readInline(text.slice(i + 1, close), depth + 1, seen) });
                        i = close + 1;
                        continue;
                    }
                }
            }
        }
        pushText(out, c);
        i++;
    }
    return out;
}

const LIST = /^(\s*)([-*+]|(\d{1,9})[.)])\s+(.*)$/;
// Tested on the trimmed line, so the spaces on either side of the info cannot be shared out.
const FENCE = /^(`{3,}|~{3,})\s*([\w+-]*)$/;
/** A header wider than this is read as a paragraph: no one reads a table that wide. */
const MAX_TABLE_COLUMNS = 64;
// Tested on the trimmed line, so no two runs of spaces meet and the test is linear.
const TABLE_RULE = /^\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?$/;
const HEADING = /^\s{0,3}(#{1,6})\s/;
const QUOTE = /^\s{0,3}>/;
const LINE_END = /\r\n?|\n/g;

function cells(line: string): string[] {
    let text = line.trim();
    if (text.startsWith('|')) text = text.slice(1);
    if (text.endsWith('|') && !text.endsWith('\\|')) text = text.slice(0, -1);
    return text.split(/(?<!\\)\|/).map(cell => cell.trim().replace(/\\\|/g, '|'));
}

/** A heading's text: without the hashes, and without a closing run of them after a space. */
function headingText(line: string, open: string): string {
    let text = line.slice(open.length).trimEnd();
    let end = text.length;
    while (end > 0 && text[end - 1] === '#') end--;
    if (end === 0 || /\s/.test(text[end - 1]!)) text = text.slice(0, end);
    return text.trim();
}

/** The lines of a source, split only as far as the parser reads. */
class Lines {
    private readonly items: string[];
    private readonly source: string;
    private next: number;

    constructor(source: string | string[]) {
        if (typeof source === 'string') {
            this.items = [];
            this.source = source;
            this.next = 0;
        } else {
            this.items = source;
            this.source = '';
            this.next = -1;
        }
    }

    has(i: number): boolean {
        while (i >= this.items.length && this.next >= 0) {
            LINE_END.lastIndex = this.next;
            const end = LINE_END.exec(this.source);
            const line = this.source.slice(this.next, end ? end.index : undefined);
            this.items.push(line.includes('\t') ? line.replace(/\t/g, '    ') : line);
            this.next = end ? LINE_END.lastIndex : -1;
        }
        return i < this.items.length;
    }

    at(i: number): string {
        return this.has(i) ? this.items[i]! : '';
    }
}

/** Block Markdown, line by line. */
export function parseMarkdown(source: string): Block[] {
    return blocksOf(new Lines(source), 0, false);
}

/** The blocks of `lines`; with `first`, only up to the end of the first block a summary reads. */
function blocksOf(lines: Lines, depth: number, first: boolean): Block[] {
    const blocks: Block[] = [];
    const quotes = depth < MAX_QUOTE_DEPTH;
    let i = 0;
    while (lines.has(i)) {
        if (first && blocks.length && SUMMARISED.has(blocks[blocks.length - 1]!.t)) break;
        const line = lines.at(i);
        if (!line.trim()) {
            i++;
            continue;
        }
        const fence = FENCE.exec(line.trim());
        if (fence) {
            const body: string[] = [];
            i++;
            while (lines.has(i) && !lines.at(i).trim().startsWith(fence[1]!)) body.push(lines.at(i++));
            i++;
            blocks.push({ t: 'code', v: body.join('\n') });
            continue;
        }
        if (/^ {4}/.test(line)) {
            const body: string[] = [];
            while (lines.has(i) && (/^ {4}/.test(lines.at(i)) || !lines.at(i).trim()))
                body.push(lines.at(i++).slice(4));
            blocks.push({ t: 'code', v: body.join('\n').replace(/\n+$/, '') });
            continue;
        }
        const heading = HEADING.exec(line);
        if (heading) {
            blocks.push({ t: 'h', level: heading[1]!.length, c: inline(headingText(line, heading[0])) });
            i++;
            continue;
        }
        if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) {
            blocks.push({ t: 'hr' });
            i++;
            continue;
        }
        if (
            line.includes('|') &&
            lines.has(i + 1) &&
            lines.at(i + 1).includes('-') &&
            TABLE_RULE.test(lines.at(i + 1).trim()) &&
            cells(line).length <= MAX_TABLE_COLUMNS
        ) {
            const head = cells(line);
            const align = cells(lines.at(i + 1)).map(rule =>
                rule.startsWith(':') && rule.endsWith(':')
                    ? 'center'
                    : rule.endsWith(':')
                      ? 'right'
                      : rule.startsWith(':')
                        ? 'left'
                        : null,
            );
            i += 2;
            const rows: Inline[][][] = [];
            while (lines.has(i) && lines.at(i).includes('|') && lines.at(i).trim()) {
                // A short row is not padded to the header's width, so a table's tokens are never
                // more than its text.
                const row = cells(lines.at(i++)).slice(0, head.length);
                rows.push(row.map(cell => inline(cell)));
            }
            blocks.push({ t: 'table', align, head: head.map(cell => inline(cell)), rows });
            continue;
        }
        if (quotes && QUOTE.test(line)) {
            const body: string[] = [];
            while (lines.has(i) && lines.at(i).trim() && QUOTE.test(lines.at(i)))
                body.push(lines.at(i++).replace(/^\s{0,3}> ?/, ''));
            blocks.push({ t: 'quote', c: blocksOf(new Lines(body), depth + 1, first) });
            continue;
        }
        const item = LIST.exec(line);
        if (item) {
            const ordered = item[3] !== undefined;
            const start = ordered ? Number(item[3]) : 1;
            const items: string[] = [];
            while (lines.has(i)) {
                const current = lines.at(i);
                const m = LIST.exec(current);
                if (m && (m[3] !== undefined) === ordered) {
                    items.push(m[4]!);
                    i++;
                } else if (current.trim() && /^\s+/.test(current) && items.length) {
                    // A continuation, or a nested item: kept with its item as text.
                    items[items.length - 1] += `\n${current.trim()}`;
                    i++;
                } else break;
            }
            blocks.push({ t: 'list', ordered, start, items: items.map(text => inline(text)) });
            continue;
        }
        // A paragraph runs to a blank line or the start of another block; past the deepest quote,
        // a ">" is text like any other.
        const body: string[] = [line];
        i++;
        while (
            lines.has(i) &&
            lines.at(i).trim() &&
            !FENCE.test(lines.at(i).trim()) &&
            !HEADING.test(lines.at(i)) &&
            !(quotes && QUOTE.test(lines.at(i))) &&
            !LIST.test(lines.at(i))
        )
            body.push(lines.at(i++));
        blocks.push({ t: 'p', c: inline(body.join('\n')) });
    }
    return blocks;
}

const SUMMARISED = new Set<Block['t']>(['p', 'h', 'list', 'quote']);

/**
 * The first paragraph, heading, list or quote as plain text, for one-line summaries such as table
 * cells. Only the source up to the end of that block is read.
 */
export function plainSummary(source: string): string {
    const text = (blocks: Block[]): string => {
        for (const block of blocks) {
            if (block.t === 'p' || block.t === 'h') return flatten(block.c);
            if (block.t === 'list') return block.items.map(flatten).join('; ');
            if (block.t === 'quote') return text(block.c);
        }
        return '';
    };
    return text(blocksOf(new Lines(cutDescription(source).text), 0, true));
}

function flatten(items: Inline[]): string {
    return items
        .map(i => (i.t === 'text' || i.t === 'code' ? i.v : i.t === 'br' ? ' ' : flatten(i.c)))
        .join('');
}
