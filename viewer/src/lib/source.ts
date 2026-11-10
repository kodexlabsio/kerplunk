// Where the graph comes from. The page works two ways.
//
// Static (graph.html): Konstruct fills the two JSON placeholders, konstruct-data (the graph) and
// konstruct-explain (formulas, SQL, ontology, conflicts), when it writes the file. Nothing is
// fetched.
//
// Served: a server sends the built viewer.html unchanged except for one element in <head>,
//
//     <meta name="konstruct-serve" content="BASE">
//
// where BASE is the path the server's files are under, with no trailing slash ("" for the
// root). It must be on the page's own origin: the page's Content-Security-Policy allows
// connections only there. The page then
//
// - fetches GET BASE/graph.json: Graph.to_dict(), the same JSON as konstruct-out/graph.json;
// - fetches GET BASE/explain.json: the same object Konstruct puts in konstruct-explain. It is
//   optional: a missing or failing response shows the pages without formulas, SQL, the ontology
//   and conflicts;
// - opens GET BASE/events as server-sent events (text/event-stream). Any message, whatever its
//   data, means the graph changed: the page fetches both files again (once for a burst of
//   messages within 150 ms) and redraws in place, keeping the page, the selection, the zoom and
//   pan, the scroll position and the open columns. Unnamed events only: the page listens with
//   onmessage.
//
// Everything about loading the graph is in this module, so that a server needs nothing else.

import type { Explanations, GraphData } from './types';

export interface Loaded {
    graph: GraphData;
    explanations: Explanations;
}

function embedded<T>(id: string): T | null {
    const text = document.getElementById(id)?.textContent?.trim() ?? '';
    if (!text || text.startsWith('__KONSTRUCT_')) return null;
    return JSON.parse(text) as T;
}

/** The base address of a serving Konstruct, or null for a static page. */
export function servedFrom(): string | null {
    const meta = document.querySelector<HTMLMetaElement>('meta[name="konstruct-serve"]');
    if (!meta) return null;
    return (meta.content || '').replace(/\/+$/, '');
}

async function fetchJson<T>(url: string): Promise<T | null> {
    const response = await fetch(url, { cache: 'no-store', headers: { Accept: 'application/json' } });
    return response.ok ? ((await response.json()) as T) : null;
}

/** The graph and what the pages show beside it. */
export async function loadGraph(): Promise<Loaded> {
    const base = servedFrom();
    if (base !== null) {
        const [graph, explanations] = await Promise.all([
            fetchJson<GraphData>(`${base}/graph.json`),
            fetchJson<Explanations>(`${base}/explain.json`).catch(() => null),
        ]);
        return { graph: graph ?? {}, explanations: explanations ?? {} };
    }
    const graph = embedded<GraphData>('konstruct-data');
    if (graph) return { graph, explanations: embedded<Explanations>('konstruct-explain') ?? {} };
    // Only `pnpm dev` reads files; the production build never makes a request for a static page.
    if (import.meta.env.DEV) {
        const [devGraph, devExplain] = await Promise.all([
            fetchJson<GraphData>('/dev/graph.json'),
            fetchJson<Explanations>('/dev/explain.json').catch(() => null),
        ]);
        return { graph: devGraph ?? {}, explanations: devExplain ?? {} };
    }
    return { graph: {}, explanations: {} };
}

/**
 * Call ``onChange`` with the graph again whenever a serving Konstruct says it changed. Returns a
 * function that stops listening; on a static page it does nothing.
 */
export function watchGraph(onChange: (loaded: Loaded) => void): () => void {
    const base = servedFrom();
    if (base === null || typeof EventSource === 'undefined') return () => undefined;
    const events = new EventSource(`${base}/events`);
    let pending = false;
    events.onmessage = () => {
        // A burst of events (a build writing several files) loads the graph once.
        if (pending) return;
        pending = true;
        setTimeout(() => {
            pending = false;
            loadGraph().then(onChange, () => undefined);
        }, 150);
    };
    return () => events.close();
}
