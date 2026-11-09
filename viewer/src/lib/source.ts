// Where the graph comes from. The page works two ways:
//
// - Static (graph.html): Konstruct fills the two JSON placeholders when it writes the file, and
//   nothing is fetched.
// - Served (`konstruct serve`): the server marks the page with <meta name="konstruct-serve"
//   content="BASE">, where BASE is the address the files are under ("" or "/" for the page's
//   own origin). The page then fetches BASE/graph.json (and BASE/explain.json, which may be
//   missing), and listens to server-sent events on BASE/events, loading the graph again when one
//   arrives.
//
// Everything about loading the graph is in this module, so that the server can change only this.

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
