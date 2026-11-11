// The viewer's state: the route (what page and view the address names), the lineage graph's
// selection and layout, and the preferences that are not part of a link.

import { SvelteSet } from 'svelte/reactivity';
import { computeFocus, type Focus } from './focus';
import { computeOrder, HEAD, PAD, place, ROW, wrapHeight } from './layout';
import { attr, GraphModel } from './model';
import { formatRoute, parseRoute, type Route } from './route';
import { buildIndex, type Entry } from './search';
import { evaluate, lineageOf, parseSelector, type Selection, type Selector } from './selector';
import type { Transform } from './transform';
import type {
    Code,
    ConflictsData,
    Explanations,
    Formula,
    GraphData,
    GraphNode,
    OntologyData,
    Point,
    WorldData,
} from './types';

export const ANIMATE_LIMIT = 400;
export const EXPAND_ALL_LIMIT = 400;

export type ViewRequest = { kind: 'fit' | 'focus' | 'centre'; id?: string; n: number };
export type Theme = 'dark' | 'light';

export interface Rows {
    rows: GraphNode[];
    more: number;
}

/** Pages that show the lineage of what they are about in a dock beneath them. */
const DOCKED = new Set(['node', 'entity']);

function stored(key: string, value?: string): string | null {
    try {
        if (value === undefined) return window.localStorage.getItem(key);
        window.localStorage.setItem(key, value);
    } catch {
        // Storage can be blocked (private windows, file pages in some browsers): keep defaults.
    }
    return null;
}

export class Viewer {
    readonly model: GraphModel;
    readonly data: GraphData;
    readonly formulas: Record<string, Formula>;
    readonly conditions: Record<string, string[]>;
    readonly sql: Record<string, Code>;
    readonly ontology: OntologyData;
    readonly world: WorldData;
    readonly conflicts: ConflictsData;
    readonly buildLocale: string | undefined;
    private indexCache: Entry[] | null = null;

    route = $state<Route>({ page: 'overview' });
    theme = $state<Theme>('dark');
    paletteOpen = $state(false);
    paletteQuery = $state('');
    helpOpen = $state(false);
    navOpen = $state(false);
    dockOpen = $state(true);
    dockSize = $state(46);
    allColumns = $state(false);
    notice = $state('');
    view = $state<ViewRequest | null>(null);
    readonly expanded = new SvelteSet<string>();
    readonly collapsed = new SvelteSet<string>();
    readonly hiddenKinds = new SvelteSet<string>();
    readonly hiddenTransforms = new SvelteSet<Transform>();
    /** The graph's pan and zoom, kept by the lineage view so a reload can restore it. */
    lastView: { x: number; y: number; k: number } | null = null;
    /** What a reloaded graph starts from: the view and the page's scroll before the reload. */
    carriedView: { x: number; y: number; k: number } | null = null;
    carriedScroll = 0;
    private requests = 0;
    private orderKey = '';
    private orderCache: string[][] = [];

    constructor(data: GraphData, explanations: Explanations) {
        this.data = data;
        this.model = new GraphModel(data);
        this.formulas = explanations.columns ?? {};
        this.conditions = explanations.conditions ?? {};
        this.sql = explanations.sql ?? {};
        this.ontology = explanations.ontology ?? {};
        this.world = explanations.world ?? {};
        this.conflicts = explanations.conflicts ?? {};
        this.buildLocale = explanations.locale;
        this.dockOpen = stored('konstruct-dock') !== 'closed';
        const size = Number(stored('konstruct-dock-size'));
        if (size >= 20 && size <= 80) this.dockSize = size;
    }

    /** The search index, built the first time something searches. */
    get index(): Entry[] {
        this.indexCache ??= buildIndex(this.model, this.ontology);
        return this.indexCache;
    }

    /** Keep what the reader was looking at when a served graph is loaded again. */
    carryFrom(previous: Viewer): void {
        this.theme = previous.theme;
        this.dockOpen = previous.dockOpen;
        this.dockSize = previous.dockSize;
        this.allColumns = previous.allColumns;
        for (const [to, from] of [
            [this.expanded, previous.expanded],
            [this.collapsed, previous.collapsed],
            [this.hiddenKinds, previous.hiddenKinds],
        ] as const)
            for (const item of from) to.add(item);
        for (const item of previous.hiddenTransforms) this.hiddenTransforms.add(item);
        this.carriedView = previous.lastView;
        this.carriedScroll = document.getElementById('page')?.scrollTop ?? 0;
    }

    // Routing

    readRoute(hash: string): void {
        this.route = parseRoute(hash);
    }

    href(route: Route): string {
        return formatRoute(route);
    }

    /** Go to a route: a new history entry, or with ``replace`` a change to the current one. */
    go(route: Route, replace = false): void {
        const hash = formatRoute(route);
        if (replace) {
            history.replaceState(history.state, '', hash);
            this.route = parseRoute(hash);
        } else if (location.hash === hash) this.route = parseRoute(hash);
        else location.hash = hash;
    }

    open(id: string): void {
        if (id.startsWith('entity:')) this.go({ page: 'entity', id: id.slice(7) });
        else this.go({ page: 'node', id });
    }

    // The lineage graph

    /** Whether the graph is on screen: full screen, or docked beneath a page about one thing. */
    readonly lineageShown = $derived(
        this.route.page === 'lineage' || (this.dockOpen && DOCKED.has(this.route.page) && this.hasLineage),
    );

    get hasLineage(): boolean {
        const r = this.route;
        if (r.page === 'entity') return true;
        if (r.page !== 'node' || !r.id) return false;
        const n = this.model.node(r.id);
        return (
            !!n &&
            (this.model.viewOf(r.id) !== null || n.kind === 'term' || n.kind === 'dag' || n.kind === 'test')
        );
    }

    /** The selector the graph shows: the route's, or the lineage of the page's subject. */
    readonly selectorText: string = $derived.by(() => {
        const r = this.route;
        if (r.select !== undefined) return r.select;
        if (r.page === 'node' && r.id) return lineageOf(this.model, r.id);
        if (r.page === 'entity' && r.id) return `entity:${JSON.stringify(r.id)}`;
        return '';
    });

    readonly excludeText: string = $derived(this.route.exclude ?? '');

    /** The node or column the graph highlights, with its impact path. */
    readonly highlight: string | null = $derived(
        this.route.page === 'lineage'
            ? (this.route.focus ?? null)
            : this.route.page === 'node'
              ? (this.route.id ?? null)
              : null,
    );

    readonly selector: Selector = $derived(parseSelector(this.selectorText));
    readonly excluded: Selector = $derived(parseSelector(this.excludeText));

    readonly selection: Selection = $derived.by(() => {
        if (!this.selector.terms.length) {
            const all = new Set(this.model.inView);
            const out = this.excluded.terms.length ? evaluate(this.model, this.excluded) : null;
            if (out) for (const id of out.relations) all.delete(id);
            return { relations: all, columns: null, seeds: new Set<string>(), unmatched: [] };
        }
        return evaluate(this.model, this.selector, this.excluded);
    });

    readonly focus: Focus = $derived.by(() => computeFocus(this.model, this.highlight));

    /** Columns whose rows the cards show: those the selector chose, and the highlighted path. */
    readonly columnFocus: Set<string> | null = $derived.by(() => {
        const a = this.selection.columns;
        const b = this.focus.columns;
        if (!a) return b;
        if (!b) return a;
        return new Set([...a, ...b]);
    });

    readonly visible: Set<string> = $derived.by(() => {
        const visible = new Set<string>();
        const highlightRelation = this.highlight ? this.model.viewOf(this.highlight) : null;
        for (const id of this.selection.relations) {
            const n = this.model.node(id);
            if (!n) continue;
            if (this.hiddenKinds.has(n.kind) && id !== highlightRelation) continue;
            visible.add(id);
        }
        return visible;
    });

    readonly order: string[][] = $derived.by(() => {
        const key = Array.from(this.visible).sort().join('\u0001');
        if (key !== this.orderKey) {
            this.orderCache = computeOrder(this.model, this.visible);
            this.orderKey = key;
        }
        return this.orderCache;
    });

    readonly target: Map<string, Point> = $derived.by(() => {
        const heightOf = (id: string): number => this.heightOf(id);
        return place(this.order, heightOf, wrapHeight(this.order, heightOf));
    });

    /** Which column rows a card shows: all when expanded, the selected path's otherwise. */
    rowsOf(id: string): Rows {
        const all = this.model.columnsOf(id);
        if (!all.length || this.collapsed.has(id)) return { rows: [], more: 0 };
        if (this.expanded.has(id) || this.allColumns) return { rows: all, more: 0 };
        const columns = this.columnFocus;
        if (columns) {
            const rows = all.filter(c => columns.has(c.id));
            return { rows, more: rows.length ? all.length - rows.length : 0 };
        }
        return { rows: [], more: 0 };
    }

    heightOf(id: string): number {
        const { rows, more } = this.rowsOf(id);
        const count = rows.length + (more ? 1 : 0);
        return count ? HEAD + PAD + count * ROW + PAD : HEAD;
    }

    request(kind: ViewRequest['kind'], id?: string): void {
        this.view = { kind, id, n: ++this.requests };
    }

    /** Show a different selection on the graph, keeping the page. */
    setSelector(select: string, exclude = this.excludeText): void {
        this.go({ ...this.route, select: select.trim() || undefined, exclude: exclude.trim() || undefined });
        this.request('fit');
    }

    /** Highlight a node in the graph: on the lineage page in place, elsewhere by opening its page. */
    pick(id: string | null): void {
        if (this.route.page === 'lineage') this.go({ ...this.route, focus: id ?? undefined }, true);
        else if (id)
            this.go({
                page: 'node',
                id,
                select: this.selectorText || undefined,
                exclude: this.route.exclude,
            });
    }

    /** Full screen and back: the lineage page keeps the selection and the highlight. */
    toggleFullScreen(): void {
        const r = this.route;
        if (r.page === 'lineage') {
            if (r.focus) this.go({ page: 'node', id: r.focus, select: r.select, exclude: r.exclude });
            else if (history.length > 1) history.back();
            else this.go({ page: 'overview' });
        } else
            this.go({
                page: 'lineage',
                select: this.selectorText || undefined,
                exclude: r.exclude,
                focus: this.highlight ?? undefined,
            });
    }

    toggleDock(): void {
        this.dockOpen = !this.dockOpen;
        stored('konstruct-dock', this.dockOpen ? 'open' : 'closed');
        if (this.dockOpen) this.request('fit');
    }

    resizeDock(percent: number): void {
        this.dockSize = Math.min(80, Math.max(20, percent));
        stored('konstruct-dock-size', String(Math.round(this.dockSize)));
    }

    toggleAllColumns(): boolean {
        if (!this.allColumns && this.visible.size > EXPAND_ALL_LIMIT) return false;
        this.allColumns = !this.allColumns;
        this.collapsed.clear();
        if (!this.allColumns) this.expanded.clear();
        return true;
    }

    toggleColumns(id: string): void {
        if (this.rowsOf(id).rows.length > 0) {
            this.expanded.delete(id);
            this.collapsed.add(id);
        } else {
            this.collapsed.delete(id);
            this.expanded.add(id);
        }
    }

    toggleKind(kind: string): void {
        if (this.hiddenKinds.has(kind)) this.hiddenKinds.delete(kind);
        else this.hiddenKinds.add(kind);
    }

    toggleTransform(kind: Transform): void {
        if (this.hiddenTransforms.has(kind)) this.hiddenTransforms.delete(kind);
        else this.hiddenTransforms.add(kind);
    }

    setTheme(theme: Theme, remember = true): void {
        this.theme = theme;
        if (remember) stored('konstruct-theme', theme);
    }

    storedTheme(): Theme | null {
        const value = stored('konstruct-theme');
        return value === 'dark' || value === 'light' ? value : null;
    }

    status(n: GraphNode): string {
        return attr<string>(n, 'status') ?? 'draft';
    }

    projectName(): string {
        const root = String(this.data.meta?.root ?? '');
        return (
            root
                .replace(/[\\/]+$/, '')
                .split(/[\\/]/)
                .pop() || ''
        );
    }
}
