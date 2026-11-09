// Node selection in dbt's syntax, for the lineage graph: "+orders", "orders+", "2+orders+1",
// "@orders", "tag:nightly", "path:models/marts", "package:shop", "kind:model", unions with
// spaces and intersections with commas. Konstruct adds column selection ("orders.amount"),
// "term:", "entity:", "dag:", "source:", "exposure:" and "materialized:".

import { attr, type GraphModel } from './model';

export const METHODS = [
    'tag',
    'path',
    'package',
    'kind',
    'resource_type',
    'source',
    'exposure',
    'term',
    'entity',
    'column',
    'dag',
    'materialized',
    'config.materialized',
    'fqn',
    'name',
] as const;

export interface Atom {
    method: string | null;
    value: string;
    /** Ancestors to include: 0 for none, Infinity for all. */
    up: number;
    down: number;
    /** dbt's "@": descendants and all of their ancestors. */
    at: boolean;
    text: string;
}

/** A union of intersections. */
export interface Selector {
    terms: Atom[][];
    errors: string[];
}

export interface Selection {
    relations: Set<string>;
    /** Columns selected through column atoms, or null when only relations were selected. */
    columns: Set<string> | null;
    /** The nodes the atoms named, before graph operators: what to highlight. */
    seeds: Set<string>;
    /** Atoms that matched nothing. */
    unmatched: string[];
}

function split(text: string, separator: RegExp): string[] {
    const out: string[] = [];
    let current = '';
    let quote = '';
    for (const c of text) {
        if (quote) {
            if (c === quote) quote = '';
            else current += c;
        } else if (c === '"' || c === "'") quote = c;
        else if (separator.test(c)) {
            if (current) out.push(current);
            current = '';
        } else current += c;
    }
    if (current) out.push(current);
    return out;
}

function depth(text: string | undefined): number {
    if (!text) return 0;
    const digits = text.replace('+', '');
    return digits ? Number(digits) : Infinity;
}

export function parseAtom(text: string): Atom | string {
    const match = /^(@)?(\d*\+)?(.*?)(\+\d*)?$/s.exec(text);
    if (!match || !match[3]) return `"${text}" names nothing`;
    const at = !!match[1];
    if (at && match[2]) return `"${text}" cannot combine @ with +`;
    let method: string | null = null;
    let value = match[3];
    const colon = value.indexOf(':');
    if (colon > 0 && /^[a-z_.]+$/.test(value.slice(0, colon))) {
        method = value.slice(0, colon);
        value = value.slice(colon + 1);
        if (!(METHODS as readonly string[]).includes(method)) return `unknown method "${method}:"`;
        if (!value) return `"${method}:" needs a value`;
    }
    return { method, value, up: depth(match[2]), down: depth(match[4]), at, text };
}

export function parseSelector(text: string): Selector {
    const terms: Atom[][] = [];
    const errors: string[] = [];
    // A quoted value keeps its spaces and commas: term:"Order amount".
    for (const term of splitTerms(text)) {
        const atoms: Atom[] = [];
        for (const part of term) {
            const atom = parseAtom(part);
            if (typeof atom === 'string') errors.push(atom);
            else atoms.push(atom);
        }
        if (atoms.length) terms.push(atoms);
    }
    return { terms, errors };
}

function splitTerms(text: string): string[][] {
    // Split on spaces outside quotes, then each term on commas outside quotes; quotes are kept
    // until the atom is read, so they are stripped here.
    const terms: string[][] = [];
    let quote = '';
    let term = '';
    const flush = (): void => {
        if (term) terms.push(split(term, /,/));
        term = '';
    };
    for (const c of text) {
        if (quote) {
            if (c === quote) quote = '';
            term += c;
        } else if (c === '"' || c === "'") {
            quote = c;
            term += c;
        } else if (/\s/.test(c)) flush();
        else term += c;
    }
    flush();
    return terms;
}

function glob(pattern: string): (text: string) => boolean {
    const lower = pattern.toLowerCase();
    if (!lower.includes('*') && !lower.includes('?')) return text => text.toLowerCase() === lower;
    const re = new RegExp(
        `^${lower
            .replace(/[.+^${}()|[\]\\]/g, '\\$&')
            .replace(/\*/g, '.*')
            .replace(/\?/g, '.')}$`,
    );
    return text => re.test(text.toLowerCase());
}

/** The relations (and tasks and exposures) or columns an atom names, before graph operators. */
export function match(model: GraphModel, atom: Atom): { relations: Set<string>; columns: Set<string> } {
    const relations = new Set<string>();
    const columns = new Set<string>();
    const test = glob(atom.value);
    const lower = atom.value.toLowerCase();
    const add = (id: string): void => {
        const n = model.node(id);
        if (!n) return;
        if (n.kind === 'column') {
            if (model.viewOf(id)) columns.add(id);
        } else if (model.inView.has(id)) relations.add(id);
    };
    const views = model.viewNodes;
    switch (atom.method) {
        case 'tag':
            for (const n of views) if (model.tagsOf(n).some(test)) add(n.id);
            break;
        case 'path': {
            const prefix = lower.replace(/\/+$/, '');
            for (const n of views) {
                const path = (n.path ?? '').toLowerCase();
                if (path && (test(path) || path === prefix || path.startsWith(`${prefix}/`))) add(n.id);
            }
            break;
        }
        case 'package':
            for (const n of views) if (test(model.packageOf(n) ?? '')) add(n.id);
            break;
        case 'kind':
        case 'resource_type':
            for (const n of views) if (test(n.kind)) add(n.id);
            break;
        case 'materialized':
        case 'config.materialized':
            for (const n of views) if (test(String(attr(n, 'materialized') ?? ''))) add(n.id);
            break;
        case 'source':
            for (const n of model.of('source')) {
                const group = String(attr(n, 'source_name') ?? '');
                if (test(group) || test(`${group}.${n.name}`) || test(n.name)) add(n.id);
            }
            break;
        case 'exposure':
            for (const n of model.of('exposure')) if (test(n.name)) add(n.id);
            break;
        case 'dag':
            for (const n of model.of('task')) if (test(String(attr(n, 'dag') ?? ''))) add(n.id);
            break;
        case 'term':
            for (const t of model.of('term'))
                if (test(t.name) || test(t.id) || test(t.id.replace(/^term\./, '')))
                    for (const id of model.boundTo(t.id)) add(id);
            break;
        case 'entity':
            for (const n of views) if ((attr<string[]>(n, 'entities') ?? []).some(test)) add(n.id);
            break;
        case 'fqn':
            for (const n of views) {
                const fqn = (attr<string[]>(n, 'fqn') ?? []).join('.');
                if (fqn && (test(fqn) || fqn.toLowerCase().startsWith(`${lower}.`))) add(n.id);
            }
            break;
        case 'column':
            matchColumns(model, atom.value, test, add);
            break;
        default: {
            // A bare name: an id, then a relation's label, name or warehouse name, then a column.
            if (model.node(atom.value)) {
                add(atom.value);
                break;
            }
            // As dbt does, a bare name is a model's name; a source is named source.table, and a
            // warehouse name or a column is tried only when nothing else matches.
            for (const n of views) if (test(model.label(n.id))) add(n.id);
            if (!relations.size) for (const n of views) if (test(n.name)) add(n.id);
            if (!relations.size) for (const n of views) if (n.relation && test(n.relation)) add(n.id);
            if (!relations.size) matchColumns(model, atom.value, test, add);
        }
    }
    return { relations, columns };
}

function matchColumns(
    model: GraphModel,
    value: string,
    test: (s: string) => boolean,
    add: (id: string) => void,
): void {
    if (value.includes('#')) {
        if (model.node(value)) add(value);
        return;
    }
    if (!value.includes('.')) {
        for (const c of model.of('column')) if (test(c.name)) add(c.id);
        return;
    }
    for (const c of model.of('column')) {
        if (!c.parent || !test(model.label(c.id))) continue;
        add(c.id);
    }
    if (value.split('.').length > 2) {
        // A warehouse name and a column: shop.main.orders.amount.
        const at = value.lastIndexOf('.');
        const relation = value.slice(0, at).toLowerCase();
        const name = value.slice(at + 1).toLowerCase();
        for (const n of model.viewNodes) {
            if (n.relation?.toLowerCase() !== relation) continue;
            for (const c of model.columnsOf(n.id)) if (c.name.toLowerCase() === name) add(c.id);
        }
    }
}

function expandColumns(model: GraphModel, seeds: Set<string>, atom: Atom): Set<string> {
    const found = new Set(seeds);
    for (const id of seeds) {
        if (atom.up || atom.at)
            for (const item of model.columnLineage(id, true, atom.at ? Infinity : atom.up))
                found.add(item.id);
        if (atom.down || atom.at)
            for (const item of model.columnLineage(id, false, atom.at ? Infinity : atom.down))
                found.add(item.id);
    }
    if (atom.at)
        for (const id of [...found]) for (const item of model.columnLineage(id, true)) found.add(item.id);
    return found;
}

function expandRelations(model: GraphModel, seeds: Set<string>, atom: Atom): Set<string> {
    const found = new Set(seeds);
    const starts = [...seeds];
    if (atom.at) {
        const below = model.walk(starts, model.downAdj);
        for (const id of model.walk([...below.keys()], model.upAdj).keys()) found.add(id);
        return found;
    }
    if (atom.up) for (const id of model.walk(starts, model.upAdj, atom.up).keys()) found.add(id);
    if (atom.down) for (const id of model.walk(starts, model.downAdj, atom.down).keys()) found.add(id);
    return found;
}

function evaluateAtom(
    model: GraphModel,
    atom: Atom,
): { relations: Set<string>; columns: Set<string> | null; seeds: Set<string> } {
    const matched = match(model, atom);
    const seeds = new Set([...matched.relations, ...matched.columns]);
    const relations = expandRelations(model, matched.relations, atom);
    if (!matched.columns.size) return { relations, columns: null, seeds };
    const columns = expandColumns(model, matched.columns, atom);
    for (const id of columns) {
        const v = model.viewOf(id);
        if (v) relations.add(v);
        // The relations that join, filter or group on a column of the path are part of it.
        for (const r of model.referencedBy(id)) relations.add(r);
    }
    return { relations, columns, seeds };
}

export function evaluate(model: GraphModel, selector: Selector, exclude?: Selector): Selection {
    const relations = new Set<string>();
    let columns: Set<string> | null = null;
    const seeds = new Set<string>();
    const unmatched: string[] = [];
    for (const term of selector.terms) {
        let termRelations: Set<string> | null = null;
        let termColumns: Set<string> | null = null;
        for (const atom of term) {
            const result = evaluateAtom(model, atom);
            if (!result.seeds.size) unmatched.push(atom.text);
            for (const s of result.seeds) seeds.add(s);
            termRelations = termRelations ? intersect(termRelations, result.relations) : result.relations;
            if (result.columns)
                termColumns = termColumns ? intersect(termColumns, result.columns) : result.columns;
        }
        for (const id of termRelations ?? []) relations.add(id);
        if (termColumns) {
            columns ??= new Set();
            for (const id of termColumns) if (termRelations?.has(model.viewOf(id) ?? '')) columns.add(id);
        }
    }
    if (exclude?.terms.length) {
        const out = evaluate(model, exclude);
        for (const id of out.relations) relations.delete(id);
        if (columns)
            for (const id of [...columns]) if (!relations.has(model.viewOf(id) ?? '')) columns.delete(id);
    }
    return { relations, columns, seeds, unmatched };
}

function intersect<T>(a: Set<T>, b: Set<T>): Set<T> {
    const out = new Set<T>();
    for (const x of a) if (b.has(x)) out.add(x);
    return out;
}

/** The shortest text that names a node on its own in a selector: its label if unique, or its id. */
export function reference(model: GraphModel, id: string): string {
    const n = model.node(id);
    if (!n) return id;
    const text = model.label(id);
    const quoted = /^[\w.#-]+$/.test(text) ? text : JSON.stringify(text);
    if (n.kind === 'column') {
        const atom = parseAtom(text);
        if (typeof atom !== 'string') {
            const found = match(model, atom);
            if (found.columns.size === 1 && !found.relations.size) return quoted;
        }
        return id;
    }
    if (n.kind === 'term') return `term:${JSON.stringify(n.name)}`;
    const atom = parseAtom(text);
    if (typeof atom !== 'string' && atom.method === null) {
        const found = match(model, atom);
        if (found.relations.size === 1) return quoted;
    }
    return id;
}

/** The default lineage selector for a node's page: its whole lineage both ways. */
export function lineageOf(model: GraphModel, id: string, direction: 'up' | 'down' | 'both' = 'both'): string {
    const n = model.node(id);
    if (!n) return '';
    const ref = reference(model, id);
    if (n.kind === 'term') return ref;
    if (n.kind === 'dag') return `+dag:${n.name}+`;
    if (n.kind === 'test') {
        const targets = model.testTargets(id).map(t => reference(model, model.viewOf(t) ?? t));
        return targets.map(t => `+${t}+`).join(' ');
    }
    return `${direction === 'down' ? '' : '+'}${ref}${direction === 'up' ? '' : '+'}`;
}
