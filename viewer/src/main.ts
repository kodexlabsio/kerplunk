import { mount, unmount } from 'svelte';
import './app.css';
import App from './App.svelte';
import { i18n } from './lib/i18n.svelte';
import { loadGraph, watchGraph, type Loaded } from './lib/source';
import { Viewer } from './lib/viewer.svelte';

let app: ReturnType<typeof mount> | null = null;
let viewer: Viewer | null = null;

/** Show a graph; when one is already shown, keep the page, the selection and the view. */
function show(loaded: Loaded): void {
    const previous = viewer;
    viewer = new Viewer(loaded.graph, loaded.explanations);
    if (previous) viewer.carryFrom(previous);
    const target = document.getElementById('root') ?? document.body;
    if (app) void unmount(app);
    app = mount(App, { target, props: { viewer } });
}

async function start(): Promise<void> {
    const loaded = await loadGraph();
    i18n.choose(loaded.explanations.locale);
    // Names are fitted to cards with the real fonts, so wait for them.
    await document.fonts?.ready.catch(() => undefined);
    show(loaded);
    watchGraph(show);
}

void start();
