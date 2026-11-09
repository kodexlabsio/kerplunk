// Search over everything with a page: relations, columns, tasks, DAGs, tests, exposures, business
// terms and entities. Each word of the query must match, as a substring or fuzzily (its letters in
// order); word starts, runs of letters and exact names rank higher. "kind:", "tag:" and "package:"
// words filter instead of matching.

import { attr, strings, type GraphModel } from './model';
import type { OntologyData } from './types';

export const MAX_RESULTS = 80;

export interface Entry {
    id: string;
    /** What the result opens: a node id, or "entity:<name>". */
    kind: string;
    label: string;
    text: string;
    /** Exact names that rank first: the name, the label and a term's synonyms. */
    names: string[];
    extra: string;
    tags: string[];
    package: string;
    /** Boost for the kinds people look for most. */
    weight: number;
}

export interface Hit {
    entry: Entry;
    score: number;
    /** Indices of the matched letters in the label, for highlighting. */
    marks: number[];
}

export interface Filters {
    kinds?: string[];
    tag?: string;
    package?: string;
}

export interface SearchResult {
    hits: Hit[];
    total: number;
    filters: Filters;
}

const WEIGHTS: Record<string, number> = {
    term: 3,
    entity: 2.5,
    model: 2,
    source: 2,
    seed: 1.5,
    snapshot: 1.5,
    table: 1.5,
    exposure: 1.5,
    dag: 1,
    task: 1,
    column: 0,
    test: -1,
};

export function buildIndex(model: GraphModel, ontology?: OntologyData): Entry[] {
    const entries: Entry[] = [];
    for (const n of model.nodes) {
        if (n.kind === 'column' && !n.parent) continue;
        const label = n.kind === 'term' ? n.name : model.label(n.id);
        const synonyms = n.kind === 'term' ? strings(attr(n, 'synonyms')) : [];
        const parent = n.parent ? model.node(n.parent) : undefined;
        entries.push({
            id: n.id,
            kind: n.kind,
            label,
            text: [label, ...synonyms].join(' ').toLowerCase(),
            names: [n.name, label, ...synonyms].map(x => x.toLowerCase()),
            extra: `${n.relation ?? ''} ${n.description ?? ''} ${n.path ?? ''}`.toLowerCase(),
            tags: model.tagsOf(parent ?? n),
            package: model.packageOf(n) ?? '',
            weight: WEIGHTS[n.kind] ?? 0,
        });
    }
    for (const e of ontology?.entities ?? []) {
        entries.push({
            id: `entity:${e.name}`,
            kind: 'entity',
            label: e.name,
            text: e.name.toLowerCase(),
            names: [e.name.toLowerCase(), (e.term ?? '').toLowerCase()],
            extra: `${e.key} ${e.description ?? ''}`.toLowerCase(),
            tags: [],
            package: '',
            weight: WEIGHTS.entity!,
        });
    }
    return entries;
}

function wordStart(text: string, at: number): boolean {
    if (at === 0) return true;
    const before = text[at - 1]!;
    return (
        before === '.' ||
        before === '_' ||
        before === ' ' ||
        before === '-' ||
        before === '/' ||
        before === '#'
    );
}

/** The best match of one word in a text: a score, and where its letters are. */
export function fuzzy(text: string, word: string): { score: number; marks: number[] } | null {
    if (!word) return { score: 0, marks: [] };
    let best = -1;
    let bestAt = -1;
    for (let at = text.indexOf(word); at >= 0; at = text.indexOf(word, at + 1)) {
        const score = (wordStart(text, at) ? 60 : 40) - Math.min(at, 20) * 0.1;
        if (score > best) {
            best = score;
            bestAt = at;
        }
        if (at === 0) break;
    }
    if (bestAt >= 0) {
        const marks = Array.from({ length: word.length }, (_, i) => bestAt + i);
        return { score: best + (text.length === word.length ? 40 : 0), marks };
    }
    // Letters in order: favour word starts and runs, penalise gaps.
    const marks: number[] = [];
    let score = 10;
    let last = -2;
    for (let i = 0, j = 0; i < word.length; i++) {
        const c = word[i]!;
        let found = -1;
        // Prefer the next word start that has the letter, then any occurrence.
        for (let k = j; k < text.length; k++) {
            if (text[k] === c && wordStart(text, k)) {
                found = k;
                break;
            }
        }
        const plain = text.indexOf(c, j);
        if (found < 0 || (plain >= 0 && plain === last + 1)) found = plain;
        if (found < 0) return null;
        if (found === last + 1) score += 4;
        else if (wordStart(text, found)) score += 3;
        else score -= Math.min(4, (found - last) * 0.25);
        marks.push(found);
        last = found;
        j = found + 1;
    }
    return score > 0 ? { score, marks } : null;
}

const FILTER = /^(kind|type|resource_type|tag|package|pkg):(.+)$/i;

export function parseQuery(query: string): { words: string[]; filters: Filters } {
    const filters: Filters = {};
    const words: string[] = [];
    for (const raw of query.trim().toLowerCase().split(/\s+/)) {
        if (!raw) continue;
        const m = FILTER.exec(raw);
        if (!m) words.push(raw);
        else if (m[1] === 'tag') filters.tag = m[2];
        else if (m[1] === 'package' || m[1] === 'pkg') filters.package = m[2];
        else filters.kinds = m[2]!.split(',');
    }
    return { words, filters };
}

function passes(entry: Entry, filters: Filters): boolean {
    if (filters.kinds?.length && !filters.kinds.includes(entry.kind)) return false;
    if (filters.tag && !entry.tags.some(t => t.toLowerCase() === filters.tag!.toLowerCase())) return false;
    if (filters.package && entry.package.toLowerCase() !== filters.package.toLowerCase()) return false;
    return true;
}

export function search(
    index: Entry[],
    query: string,
    extra: Filters = {},
    compare: (a: string, b: string) => number = (a, b) => a.localeCompare(b),
    limit = MAX_RESULTS,
): SearchResult {
    const parsed = parseQuery(query);
    const filters: Filters = { ...extra, ...parsed.filters };
    if (extra.kinds?.length && parsed.filters.kinds?.length)
        filters.kinds = extra.kinds.filter(k => parsed.filters.kinds!.includes(k));
    const words = parsed.words;
    const filtering = !!(filters.kinds?.length || filters.tag || filters.package);
    if (!words.length && !filtering) return { hits: [], total: 0, filters };
    const phrase = words.join(' ');
    const hits: Hit[] = [];
    for (const entry of index) {
        if (filtering && !passes(entry, filters)) continue;
        let score = 0;
        const marks: number[] = [];
        let failed = false;
        for (const w of words) {
            const m = fuzzy(entry.text, w);
            if (m) {
                score += m.score;
                for (const i of m.marks) if (i < entry.label.length) marks.push(i);
            } else if (entry.extra.includes(w)) score += 8;
            else {
                failed = true;
                break;
            }
        }
        if (failed) continue;
        if (phrase && entry.names.includes(phrase)) score += 100;
        else if (phrase && entry.names.some(n => n.endsWith(`.${phrase}`))) score += 50;
        score += entry.weight * (words.length || 1);
        hits.push({ entry, score, marks });
    }
    hits.sort(
        (a, b) =>
            b.score - a.score ||
            a.entry.label.length - b.entry.label.length ||
            compare(a.entry.label, b.entry.label),
    );
    return { hits: hits.slice(0, limit), total: hits.length, filters };
}
