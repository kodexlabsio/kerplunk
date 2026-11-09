import { mount } from 'svelte';
import './app.css';
import App from './App.svelte';
import { i18n } from './lib/i18n.svelte';
import type { Explanations, GraphData } from './lib/types';
import { Viewer } from './lib/viewer.svelte';

function embedded<T>(id: string): T | null {
    const text = document.getElementById(id)?.textContent ?? '';
    if (!text.trim() || text.trim().startsWith('__KONSTRUCT_')) return null;
    return JSON.parse(text) as T;
}

/** In `pnpm dev` the placeholders are still in the page: read dev/graph.json instead. */
async function load(): Promise<[GraphData, Explanations]> {
    const graph = embedded<GraphData>('konstruct-data');
    if (graph) return [graph, embedded<Explanations>('konstruct-explain') ?? {}];
    // Only `pnpm dev` reads a file; the production build never makes a request.
    if (import.meta.env.DEV) {
        const response = await fetch('/dev/graph.json');
        const explain = await fetch('/dev/explain.json');
        return [
            response.ok ? ((await response.json()) as GraphData) : {},
            explain.ok ? ((await explain.json()) as Explanations) : {},
        ];
    }
    return [{}, {}];
}

async function start(): Promise<void> {
    const [graph, explanations] = await load();
    i18n.choose(explanations.locale);
    // Names are fitted to cards with the real fonts, so wait for them.
    await document.fonts?.ready.catch(() => undefined);
    const viewer = new Viewer(graph, explanations);
    const target = document.getElementById('root') ?? document.body;
    mount(App, { target, props: { viewer } });
}

void start();
