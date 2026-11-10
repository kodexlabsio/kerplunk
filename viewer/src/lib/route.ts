// Hash routes: every page and view of the viewer is a link. #/node/<id>, #/lineage?select=...,
// #/browse?kind=model&tag=nightly, and so on. A bare #<node id>, the form earlier versions wrote,
// still opens that node.

export const PAGES = [
    'overview',
    'node',
    'lineage',
    'browse',
    'glossary',
    'ontology',
    'entity',
    'conflicts',
    'usage',
    'tests',
    'warnings',
    'map',
] as const;

/** A page the address names, or 'notfound' for an address the viewer does not know. */
export type Page = (typeof PAGES)[number] | 'notfound';

export interface Route {
    page: Page;
    /** The node id on a node page, the entity name on an entity page. */
    id?: string;
    /** A selector for the lineage graph, in dbt's syntax. */
    select?: string;
    exclude?: string;
    /** The node or column highlighted on the lineage page. */
    focus?: string;
    /** Browse filters. */
    kind?: string;
    tag?: string;
    package?: string;
    q?: string;
}

const PARAMS = ['select', 'exclude', 'focus', 'kind', 'tag', 'package', 'q'] as const;
const WITH_ID: ReadonlySet<Page> = new Set(['node', 'entity', 'notfound']);

function decode(text: string): string {
    try {
        return decodeURIComponent(text);
    } catch {
        return text;
    }
}

/** Encoded for a hash, keeping the characters selectors and ids use readable. */
function encode(text: string): string {
    return encodeURIComponent(text)
        .replace(/%2B/g, '+')
        .replace(/%3A/gi, ':')
        .replace(/%2C/gi, ',')
        .replace(/%2F/gi, '/');
}

export function parseRoute(hash: string): Route {
    const text = hash.startsWith('#') ? hash.slice(1) : hash;
    if (!text || text === '/') return { page: 'overview' };
    if (!text.startsWith('/')) return { page: 'node', id: decode(text) };
    const at = text.indexOf('?');
    const path = at < 0 ? text.slice(1) : text.slice(1, at);
    const query = at < 0 ? '' : text.slice(at + 1);
    const slash = path.indexOf('/');
    const head = decode(slash < 0 ? path : path.slice(0, slash));
    const rest = slash < 0 ? '' : decode(path.slice(slash + 1));
    const page = (PAGES as readonly string[]).includes(head) ? (head as Page) : null;
    if (!page) return { page: 'notfound', id: path };
    const route: Route = { page };
    if (WITH_ID.has(page)) {
        if (!rest) return { page: 'notfound', id: path };
        route.id = rest;
    }
    for (const pair of query.split('&')) {
        if (!pair) continue;
        const eq = pair.indexOf('=');
        const key = eq < 0 ? pair : pair.slice(0, eq);
        const value = eq < 0 ? '' : decode(pair.slice(eq + 1));
        if ((PARAMS as readonly string[]).includes(key) && value)
            route[key as (typeof PARAMS)[number]] = value;
    }
    return route;
}

export function formatRoute(route: Route): string {
    let path = `#/${route.page === 'overview' ? '' : route.page}`;
    if (WITH_ID.has(route.page) && route.id) path += `/${encode(route.id)}`;
    const query = PARAMS.filter(key => route[key]).map(key => `${key}=${encode(route[key]!)}`);
    return query.length ? `${path}?${query.join('&')}` : path;
}

export function nodeRoute(id: string): Route {
    return { page: 'node', id };
}

export function href(route: Route): string {
    return formatRoute(route);
}

export function sameRoute(a: Route, b: Route): boolean {
    return formatRoute(a) === formatRoute(b);
}
