<script module lang="ts">
    /** An id usable in an id attribute and a CSS selector. */
    export function cssId(id: string): string {
        let hash = 0;
        for (let i = 0; i < id.length; i++) hash = (hash * 31 + id.charCodeAt(i)) | 0;
        return `${id.replace(/[^\w-]/g, '_')}-${(hash >>> 0).toString(36)}`;
    }
</script>

<script lang="ts">
    import { onMount, tick, untrack } from 'svelte';
    import {
        collectStyles,
        fileName,
        pngDataUrl,
        pngScale,
        save,
        svgDataUrl,
        svgDocument,
    } from '../lib/export';
    import { i18n, t } from '../lib/i18n.svelte';
    import { ICONS } from '../lib/icons';
    import { kindName, transformName } from '../lib/labels';
    import { bounds, curve, HEAD, PAD, ROW, W } from '../lib/layout';
    import { attr, VIEW_KINDS } from '../lib/model';
    import { reference } from '../lib/selector';
    import { fitText, FONT_MONO, FONT_NAME, FONT_ROW, textWidth } from '../lib/text';
    import { DASH, TRANSFORMS, type Transform } from '../lib/transform';
    import type { Point } from '../lib/types';
    import { ANIMATE_LIMIT, type Viewer } from '../lib/viewer.svelte';
    import Icon from './Icon.svelte';
    import Inspector from './Inspector.svelte';

    // The viewer is made once and never replaced.
    const props: { viewer: Viewer } = $props();
    const viewer = untrack(() => props.viewer);
    // svelte-ignore state_referenced_locally
    const model = viewer.model;

    let svg: SVGSVGElement | undefined = $state();
    let box: HTMLElement | undefined = $state();
    let minimap: HTMLCanvasElement | undefined = $state();
    let size = $state({ w: 1200, h: 700 });

    const full = $derived(viewer.route.page === 'lineage');
    let listOpen = $state(false);
    $effect(() => {
        listOpen = untrack(() => full && window.innerWidth > 1100);
    });

    // Positions on screen: the layout's target, eased towards it when it changes.
    let displayed = $state.raw(new Map<string, Point>());
    let frame = 0;
    const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;

    $effect(() => {
        const target = viewer.target;
        const from = untrack(() => displayed);
        cancelAnimationFrame(frame);
        if (!from.size || target.size > ANIMATE_LIMIT || reducedMotion) {
            displayed = target;
            return;
        }
        const start = performance.now();
        const step = (now: number): void => {
            const k = 1 - Math.pow(1 - Math.min(1, (now - start) / 320), 3);
            const next = new Map<string, Point>();
            for (const [id, c] of target) {
                const a = from.get(id) ?? c;
                next.set(id, { x: a.x + (c.x - a.x) * k, y: a.y + (c.y - a.y) * k, h: c.h });
            }
            displayed = next;
            if (k < 1) frame = requestAnimationFrame(step);
        };
        frame = requestAnimationFrame(step);
    });

    function at(id: string): Point {
        return displayed.get(id) ?? viewer.target.get(id) ?? { x: 0, y: 0, h: HEAD };
    }

    // Pan and zoom

    let view = $state({ x: 0, y: 0, k: 1 });
    const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v));

    function zoomAt(px: number, py: number, factor: number): void {
        const k = clamp(view.k * factor, 0.02, 3);
        view = { x: px - ((px - view.x) * k) / view.k, y: py - ((py - view.y) * k) / view.k, k };
    }

    function fitBounds(subset: Map<string, Point>, maxZoom: number): void {
        const b = bounds(subset);
        if (!b) return;
        const pad = 48;
        const k = clamp(Math.min((size.w - 2 * pad) / b.w, (size.h - 2 * pad) / b.h), 0.02, maxZoom);
        view = { k, x: (size.w - b.w * k) / 2 - b.x * k, y: (size.h - b.h * k) / 2 - b.y * k };
    }

    function centreOn(id: string, keepZoom = false): void {
        const c = viewer.target.get(id);
        if (!c) return;
        const k = keepZoom ? view.k : Math.max(view.k, 0.7);
        view = { k, x: size.w / 2 - (c.x + W / 2) * k, y: size.h / 2 - (c.y + Math.min(c.h, 300) / 2) * k };
    }

    $effect(() => {
        const request = viewer.view;
        if (!request) return;
        requestAnimationFrame(() =>
            untrack(() => {
                const target = viewer.target;
                const lit = viewer.focus.relations;
                if (request.kind === 'focus' && lit) {
                    const subset = new Map<string, Point>();
                    for (const id of lit) {
                        const c = target.get(id);
                        if (c) subset.set(id, c);
                    }
                    fitBounds(subset.size ? subset : target, 1.1);
                } else if (request.kind === 'centre' && request.id) centreOn(request.id);
                else fitBounds(target, 1.2);
            }),
        );
    });

    // A graph loaded again by a serving Konstruct keeps the view it had.
    // svelte-ignore state_referenced_locally
    const carried = viewer.carriedView;
    if (carried) {
        view = carried;
        viewer.carriedView = null;
    }
    $effect(() => {
        viewer.lastView = view;
    });

    // A new selection fits the graph to the window.
    // svelte-ignore state_referenced_locally
    let lastKey = carried ? `${viewer.selectorText}\u0000${viewer.excludeText}` : '';
    $effect(() => {
        const key = `${viewer.selectorText}\u0000${viewer.excludeText}`;
        if (key === lastKey) return;
        lastKey = key;
        untrack(() => viewer.request('fit'));
    });

    // What is drawn: only what is near the window, so thousands of cards stay fast.

    let exporting = $state(false);
    const far = $derived(view.k < 0.32 && !exporting);
    const tiny = $derived(view.k < 0.12 && !exporting);

    const windowBox = $derived.by(() => {
        const margin = 160 / view.k;
        return {
            x0: -view.x / view.k - margin,
            y0: -view.y / view.k - margin,
            x1: (size.w - view.x) / view.k + margin,
            y1: (size.h - view.y) / view.k + margin,
        };
    });

    function onScreen(id: string): boolean {
        if (exporting) return true;
        const c = at(id);
        const b = windowBox;
        return c.x + W >= b.x0 && c.x <= b.x1 && c.y + c.h >= b.y0 && c.y <= b.y1;
    }

    // Card objects stay the same while the layout does, so panning re-renders nothing but the
    // cards that come into view.
    const allCards = $derived(
        Array.from(viewer.target.keys()).map(id => {
            const n = model.node(id)!;
            const { rows, more } = viewer.rowsOf(id);
            return { id, n, all: model.columnsOf(id), rows, more };
        }),
    );
    const cards = $derived(allCards.filter(card => onScreen(card.id)));

    const rowOffsets = $derived.by(() => {
        const index = new Map<string, Map<string, number>>();
        for (const id of viewer.target.keys()) {
            const { rows } = viewer.rowsOf(id);
            index.set(id, new Map(rows.map((c, i) => [c.id, HEAD + PAD + i * ROW + ROW / 2])));
        }
        return index;
    });

    interface DrawnEdge {
        key: string;
        kind: 'feeds' | 'runs' | 'column' | 'reference';
        transform?: Transform;
        from: string;
        to: string;
        fromCol: string | null;
        toCol: string | null;
        title: string;
    }

    const allEdges = $derived.by(() => {
        const drawn: DrawnEdge[] = [];
        const shown = viewer.target;
        for (const e of model.layoutEdges) {
            if (!shown.has(e.source) || !shown.has(e.target)) continue;
            drawn.push({
                key: `${e.kind}|${e.source}|${e.target}`,
                kind: e.kind,
                from: e.source,
                to: e.target,
                fromCol: null,
                toCol: null,
                title: t(e.kind === 'runs' ? 'edge.runsTitle' : 'edge.feedsTitle', {
                    from: model.label(e.source),
                    to: model.label(e.target),
                }),
            });
        }
        for (const [id, rows] of rowOffsets) {
            for (const colId of rows.keys()) {
                for (const e of model.outs(colId, 'derived_from')) {
                    const up = model.viewOf(e.target);
                    if (!up || !rowOffsets.get(up)?.has(e.target)) continue;
                    const transform = model.transformOf(e);
                    const verified = attr<string>(e, 'verified');
                    drawn.push({
                        key: `column|${e.target}|${colId}`,
                        kind: 'column',
                        transform,
                        from: up,
                        to: id,
                        fromCol: e.target,
                        toCol: colId,
                        title: t(verified === 'confirmed' ? 'edge.columnVerified' : 'edge.column', {
                            from: model.label(e.target),
                            to: model.label(colId),
                            transform: transformName(transform),
                        }),
                    });
                }
            }
        }
        for (const [colId, relation] of viewer.focus.references) {
            const from = model.viewOf(colId);
            if (!from || !rowOffsets.has(from) || !rowOffsets.has(relation) || from === relation) continue;
            drawn.push({
                key: `reference|${colId}|${relation}`,
                kind: 'reference',
                transform: 'reference',
                from,
                to: relation,
                fromCol: colId,
                toCol: null,
                title: t('edge.referenceTitle', {
                    relation: model.label(relation),
                    column: model.label(colId),
                }),
            });
        }
        return drawn;
    });

    function edgeClass(e: DrawnEdge): Transform | 'feeds' | 'runs' {
        return e.kind === 'feeds' || e.kind === 'runs' ? e.kind : (e.transform ?? 'direct');
    }

    const legendCounts = $derived.by(() => {
        const counts = new Map<string, number>();
        for (const e of allEdges) counts.set(edgeClass(e), (counts.get(edgeClass(e)) ?? 0) + 1);
        return counts;
    });

    const edges = $derived(
        allEdges.filter(
            e =>
                !viewer.hiddenTransforms.has(edgeClass(e) as Transform) &&
                (exporting || onScreen(e.from) || onScreen(e.to)),
        ),
    );

    function anchor(id: string, col: string | null, side: 'out' | 'in'): { x: number; y: number } {
        const c = at(id);
        const offset = col && !far ? rowOffsets.get(id)?.get(col) : undefined;
        return { x: side === 'out' ? c.x + W : c.x, y: c.y + (offset ?? HEAD / 2) };
    }

    // What is lit: the highlighted node's impact path.

    const lit = $derived(viewer.focus.relations);
    const columnsLit = $derived(viewer.focus.columns);

    function edgeOn(e: DrawnEdge): boolean {
        if (!lit) return false;
        if (e.kind === 'column')
            return !!columnsLit && columnsLit.has(e.fromCol!) && columnsLit.has(e.toCol!);
        if (e.kind === 'reference') return true;
        return !columnsLit && lit.has(e.from) && lit.has(e.to);
    }

    // Keyboard: a cursor moves between cards; it is announced, and the inspector lists its neighbours.

    let cursor = $state<string | null>(null);
    let announcement = $state('');
    const current = $derived(cursor && viewer.target.has(cursor) ? cursor : null);
    const subject = $derived(current ?? (viewer.highlight ? model.viewOf(viewer.highlight) : null));

    function layerIndex(): Map<string, [number, number]> {
        const index = new Map<string, [number, number]>();
        viewer.order.forEach((row, layer) => {
            const sorted = row
                .slice()
                .sort(
                    (a, b) =>
                        (viewer.target.get(a)?.x ?? 0) - (viewer.target.get(b)?.x ?? 0) ||
                        (viewer.target.get(a)?.y ?? 0) - (viewer.target.get(b)?.y ?? 0),
                );
            sorted.forEach((id, i) => index.set(id, [layer, i]));
        });
        return index;
    }

    function nearest(ids: string[], from: string): string | null {
        const c = viewer.target.get(from);
        let best: string | null = null;
        let distance = Infinity;
        for (const id of ids) {
            const p = viewer.target.get(id);
            if (!p || !c) continue;
            const d = Math.abs(p.y + p.h / 2 - (c.y + c.h / 2));
            if (d < distance) {
                distance = d;
                best = id;
            }
        }
        return best;
    }

    function describe(id: string): string {
        const n = model.node(id);
        if (!n) return '';
        const ups = (model.upAdj.get(id) ?? []).filter(x => viewer.target.has(x)).length;
        const downs = (model.downAdj.get(id) ?? []).filter(x => viewer.target.has(x)).length;
        return t('lineage.announce', { name: model.label(id), kind: kindName(n.kind), up: ups, down: downs });
    }

    function moveTo(id: string | null): void {
        if (!id) return;
        cursor = id;
        announcement = describe(id);
        const c = viewer.target.get(id);
        if (c) {
            const sx = c.x * view.k + view.x;
            const sy = c.y * view.k + view.y;
            if (sx < 0 || sy < 0 || sx + W * view.k > size.w || sy + c.h * view.k > size.h)
                centreOn(id, true);
        }
    }

    function start(): string | null {
        if (current) return current;
        const h = viewer.highlight ? model.viewOf(viewer.highlight) : null;
        if (h && viewer.target.has(h)) return h;
        return viewer.order[0]?.[0] ?? null;
    }

    function expand(id: string, direction: 'up' | 'down' | 'both'): void {
        const ref = reference(model, id);
        const atom = direction === 'up' ? `1+${ref}` : direction === 'down' ? `${ref}+1` : `1+${ref}+1`;
        if (!viewer.selectorText.trim()) return;
        viewer.setSelector(`${viewer.selectorText} ${atom}`);
    }

    function hide(id: string): void {
        viewer.setSelector(viewer.selectorText, `${viewer.excludeText} ${reference(model, id)}`.trim());
    }

    function hiddenNeighbours(id: string, adjacency: Map<string, string[]>): number {
        let count = 0;
        for (const other of adjacency.get(id) ?? []) if (!viewer.target.has(other)) count++;
        return count;
    }

    function onkeydown(event: KeyboardEvent): void {
        if (event.ctrlKey || event.metaKey || event.altKey) return;
        const key = event.key;
        const rtl = false;
        const here = start();
        if (key === 'ArrowRight' || key === 'ArrowLeft') {
            if (!here) return;
            const downstream = (key === 'ArrowRight') !== rtl;
            const next =
                (downstream ? model.downAdj : model.upAdj).get(here)?.filter(x => viewer.target.has(x)) ?? [];
            if (next.length) moveTo(nearest(next, here));
            else announcement = downstream ? t('lineage.noDownstream') : t('lineage.noUpstream');
            if (!current) moveTo(here);
        } else if (key === 'ArrowDown' || key === 'ArrowUp') {
            if (!here) return;
            const index = layerIndex();
            const [layer, i] = index.get(here) ?? [0, 0];
            const row = (viewer.order[layer] ?? [])
                .slice()
                .sort((a, b) => (index.get(a)?.[1] ?? 0) - (index.get(b)?.[1] ?? 0));
            const next = row[clamp(i + (key === 'ArrowDown' ? 1 : -1), 0, row.length - 1)];
            moveTo(current ? (next ?? here) : here);
        } else if (key === 'Home') moveTo(viewer.order[0]?.[0] ?? null);
        else if (key === 'Enter') {
            if (here) viewer.open(here);
        } else if (key === ' ') {
            if (here) viewer.pick(here);
        } else if (key === 'e' || key === 'E') {
            if (here) expand(here, 'both');
        } else if (key === 'Delete' || key === 'Backspace') {
            if (here) hide(here);
        } else if (key === 'f' || key === 'F') viewer.request('fit');
        else if (key === 'c' || key === 'C') toggleColumns();
        else if (key === '+' || key === '=') zoomButton(1.25);
        else if (key === '-') zoomButton(0.8);
        else if (key === 'Escape') {
            if (cursor) cursor = null;
            else if (full) viewer.toggleFullScreen();
            else return;
        } else return;
        event.preventDefault();
        event.stopPropagation();
    }

    // Pointer

    let drag: { x: number; y: number; vx: number; vy: number; moved: boolean } | null = null;
    let dragging = $state(false);
    let suppressClick = false;

    function onpointerdown(event: PointerEvent): void {
        if (event.button !== 0) return;
        suppressClick = false;
        drag = { x: event.clientX, y: event.clientY, vx: view.x, vy: view.y, moved: false };
    }

    function onpointermove(event: PointerEvent): void {
        if (!drag) return;
        const dx = event.clientX - drag.x;
        const dy = event.clientY - drag.y;
        if (!drag.moved && Math.abs(dx) + Math.abs(dy) > 4) {
            drag.moved = true;
            svg?.setPointerCapture(event.pointerId);
            dragging = true;
        }
        if (drag.moved) view = { ...view, x: drag.vx + dx, y: drag.vy + dy };
    }

    function endDrag(): void {
        if (drag?.moved) suppressClick = true;
        drag = null;
        dragging = false;
    }

    function onwheel(event: WheelEvent): void {
        event.preventDefault();
        const r = svg?.getBoundingClientRect();
        if (!r) return;
        // Pinch (reported with ctrlKey) and mouse wheels zoom; two-finger trackpad scrolling pans.
        const pinch = event.ctrlKey || event.metaKey;
        const wheel =
            event.deltaMode !== 0 ||
            (event.deltaX === 0 && Number.isInteger(event.deltaY) && Math.abs(event.deltaY) >= 50);
        if (pinch || wheel) {
            const scale = event.deltaMode === 1 ? 0.05 : event.deltaMode === 2 ? 1 : pinch ? 0.01 : 0.0018;
            zoomAt(event.clientX - r.left, event.clientY - r.top, Math.exp(-event.deltaY * scale));
        } else view = { ...view, x: view.x - event.deltaX, y: view.y - event.deltaY };
    }

    function onclick(event: MouseEvent): void {
        if (suppressClick) {
            suppressClick = false;
            return;
        }
        const target = event.target as Element;
        const handle = target.closest('[data-expand]');
        if (handle) {
            expand(handle.getAttribute('data-id')!, handle.getAttribute('data-expand') as 'up' | 'down');
            return;
        }
        const more = target.closest('[data-more]');
        if (more) {
            viewer.expanded.add(more.getAttribute('data-more')!);
            return;
        }
        const toggle = target.closest('[data-toggle]');
        if (toggle) {
            viewer.toggleColumns(toggle.getAttribute('data-toggle')!);
            return;
        }
        const row = target.closest('[data-col]');
        if (row) {
            viewer.pick(row.getAttribute('data-col')!);
            return;
        }
        const card = target.closest('.node');
        if (card) {
            const id = card.getAttribute('data-id')!;
            cursor = id;
            viewer.pick(id);
        } else if (full) viewer.pick(null);
    }

    function ondblclick(event: MouseEvent): void {
        const target = event.target as Element;
        const card = target.closest('.node');
        if (card && !target.closest('[data-col]')) viewer.toggleColumns(card.getAttribute('data-id')!);
    }

    function zoomButton(factor: number): void {
        zoomAt(size.w / 2, size.h / 2, factor);
    }

    function toggleColumns(): void {
        if (!viewer.toggleAllColumns()) announcement = t('lineage.tooMany', { count: viewer.visible.size });
    }

    // Minimap

    let miniTransform: { k: number; ox: number; oy: number } | null = null;
    const miniHidden = $derived.by(() => {
        // Only worth showing when the graph does not fit the window at the current zoom.
        const b = bounds(viewer.target);
        if (!b || viewer.target.size < 6 || size.h < 320) return true;
        return b.w * view.k <= size.w && b.h * view.k <= size.h;
    });

    $effect(() => {
        const positions = displayed;
        const current = view;
        const litNow = lit;
        void viewer.theme;
        void size;
        if (!minimap) return;
        const context = minimap.getContext('2d');
        if (!context) return;
        const styles = getComputedStyle(document.documentElement);
        const b = bounds(positions);
        context.clearRect(0, 0, minimap.width, minimap.height);
        if (!b) return;
        const pad = 12;
        const k = Math.min((minimap.width - 2 * pad) / b.w, (minimap.height - 2 * pad) / b.h);
        const ox = pad + (minimap.width - 2 * pad - b.w * k) / 2 - b.x * k;
        const oy = pad + (minimap.height - 2 * pad - b.h * k) / 2 - b.y * k;
        miniTransform = { k, ox, oy };
        const muted = styles.getPropertyValue('--edge').trim();
        const accent = styles.getPropertyValue('--accent').trim();
        for (const [id, c] of positions) {
            context.fillStyle = litNow?.has(id) ? accent : muted;
            context.fillRect(ox + c.x * k, oy + c.y * k, Math.max(2, W * k), Math.max(2, c.h * k));
        }
        context.strokeStyle = accent;
        context.lineWidth = 2;
        context.strokeRect(
            ox + (-current.x / current.k) * k,
            oy + (-current.y / current.k) * k,
            (size.w / current.k) * k,
            (size.h / current.k) * k,
        );
    });

    let miniDrag = false;
    function miniPan(event: PointerEvent): void {
        if (!miniTransform || !minimap) return;
        const r = minimap.getBoundingClientRect();
        const mx = ((event.clientX - r.left) / r.width) * minimap.width;
        const my = ((event.clientY - r.top) / r.height) * minimap.height;
        const gx = (mx - miniTransform.ox) / miniTransform.k;
        const gy = (my - miniTransform.oy) / miniTransform.k;
        view = { ...view, x: size.w / 2 - gx * view.k, y: size.h / 2 - gy * view.k };
    }

    // Export

    let exportOpen = $state(false);
    $effect(() => {
        if (exportOpen) void tick().then(() => document.getElementById('export-svg')?.focus());
    });

    /** The export menu: arrow keys move between its items, Escape closes it. */
    function menuKey(event: KeyboardEvent): void {
        const items = [document.getElementById('export-svg'), document.getElementById('export-png')];
        const at = items.indexOf(document.activeElement as HTMLElement);
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') items[(at + 1) % 2]?.focus();
        else if (event.key === 'Escape' || event.key === 'Tab') {
            exportOpen = false;
            document.getElementById('export')?.focus();
        } else return;
        event.preventDefault();
        event.stopPropagation();
    }
    async function exportAs(format: 'svg' | 'png'): Promise<void> {
        exportOpen = false;
        const b = bounds(viewer.target);
        if (!b || !svg) return;
        exporting = true;
        await tick();
        try {
            const styles = getComputedStyle(document.documentElement);
            const css = collectStyles(Array.from(document.styleSheets), name =>
                styles.getPropertyValue(name),
            );
            const inner = svg.querySelector('#viewport')?.innerHTML ?? '';
            const defs = svg.querySelector('defs')?.innerHTML ?? '';
            const title = t('lineage.exportTitle', { project: viewer.projectName() || t('app.graph') });
            const doc = svgDocument(
                inner,
                defs,
                b,
                css,
                styles.getPropertyValue('--ground').trim() || '#ffffff',
                title,
            );
            const name = fileName(viewer.projectName(), format);
            if (format === 'svg') save(svgDataUrl(doc), name);
            else {
                const w = b.w + 80;
                const h = b.h + 80;
                save(await pngDataUrl(doc, w, h, pngScale(w, h)), name);
            }
            announcement = t('lineage.exported', { name });
        } catch {
            announcement = t('lineage.exportFailed');
        } finally {
            exporting = false;
        }
    }

    // The selector form

    let selectText = $state('');
    let excludeText = $state('');
    $effect(() => {
        selectText = viewer.selectorText;
        excludeText = viewer.excludeText;
    });

    function apply(event: SubmitEvent): void {
        event.preventDefault();
        viewer.setSelector(selectText, excludeText);
    }

    function direction(d: 'up' | 'down' | 'both'): void {
        const id = subject ?? viewer.highlight;
        if (!id) return;
        const ref = reference(
            model,
            viewer.highlight && model.node(viewer.highlight)?.kind === 'column' ? viewer.highlight : id,
        );
        viewer.setSelector(d === 'up' ? `+${ref}` : d === 'down' ? `${ref}+` : `+${ref}+`);
    }

    const legendItems: (Transform | 'feeds' | 'runs')[] = ['feeds', 'runs', ...TRANSFORMS];
    const kinds = VIEW_KINDS.filter(k => model.of(k).length > 0);

    onMount(() => {
        const observer = new ResizeObserver(() => {
            const r = svg?.getBoundingClientRect();
            if (r && r.width && r.height) size = { w: r.width, h: r.height };
        });
        if (svg) observer.observe(svg);
        const r = svg?.getBoundingClientRect();
        if (r && r.width) size = { w: r.width, h: r.height };
        if (!carried) viewer.request('fit');
        return () => observer.disconnect();
    });

    function typeLabel(dataType: string | undefined): string {
        return dataType ? fitText(dataType.toLowerCase(), FONT_MONO, 92) : '';
    }

    const empty = $derived(
        viewer.target.size
            ? ''
            : viewer.selector.terms.length || viewer.excluded.terms.length
              ? t('lineage.nothingSelected')
              : model.viewNodes.length
                ? t('lineage.nothingShown')
                : t('lineage.empty'),
    );
</script>

<div id="lineage" class="lineage" class:full>
    <form class="lineage-bar" onsubmit={apply} role="search" aria-label={t('lineage.selectorForm')}>
        <label class="selector">
            <span class="label">{t('lineage.select')}</span>
            <input
                id="select"
                type="text"
                bind:value={selectText}
                placeholder={t('lineage.selectPlaceholder')}
                spellcheck="false"
                autocomplete="off"
                dir="ltr"
                aria-describedby="selector-status"
            />
        </label>
        <label class="selector exclude">
            <span class="label">{t('lineage.exclude')}</span>
            <input
                id="exclude"
                type="text"
                bind:value={excludeText}
                placeholder={t('lineage.excludePlaceholder')}
                spellcheck="false"
                autocomplete="off"
                dir="ltr"
            />
        </label>
        <button type="submit" class="button primary" id="apply">{t('lineage.apply')}</button>
        <div class="toolbar" role="toolbar" aria-label={t('lineage.tools')}>
            {#if viewer.highlight || subject}
                <div class="segmented" role="group" aria-label={t('lineage.direction')}>
                    <button type="button" data-direction="upstream" onclick={() => direction('up')}
                        >{t('lineage.upstream')}</button
                    >
                    <button type="button" data-direction="both" onclick={() => direction('both')}
                        >{t('lineage.both')}</button
                    >
                    <button type="button" data-direction="downstream" onclick={() => direction('down')}
                        >{t('lineage.downstream')}</button
                    >
                </div>
            {/if}
            <button
                type="button"
                class="tool"
                id="columns"
                aria-pressed={viewer.allColumns}
                aria-keyshortcuts="C"
                onclick={toggleColumns}
                title={t('lineage.columns')}
                ><Icon kind="columns" /><span class:sr-only={!full}>{t('lineage.columns')}</span></button
            >
            <button
                type="button"
                class="tool icon-only"
                id="zoom-out"
                aria-label={t('lineage.zoomOut')}
                title={t('lineage.zoomOut')}
                onclick={() => zoomButton(0.8)}><Icon kind="minus" /></button
            >
            <button
                type="button"
                class="tool icon-only"
                id="zoom-in"
                aria-label={t('lineage.zoomIn')}
                title={t('lineage.zoomIn')}
                onclick={() => zoomButton(1.25)}><Icon kind="plus" /></button
            >
            <button
                type="button"
                class="tool"
                id="fit"
                aria-keyshortcuts="F"
                onclick={() => viewer.request('fit')}
                title={t('lineage.fit')}
                ><Icon kind="fit" /><span class:sr-only={!full}>{t('lineage.fit')}</span></button
            >
            <div class="menu-wrap">
                <button
                    type="button"
                    class="tool"
                    id="export"
                    aria-haspopup="menu"
                    aria-expanded={exportOpen}
                    aria-controls={exportOpen ? 'export-menu' : undefined}
                    onclick={() => (exportOpen = !exportOpen)}
                    ><Icon kind="download" /><span class:sr-only={!full}>{t('lineage.export')}</span></button
                >
                {#if exportOpen}
                    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
                    <ul
                        class="menu"
                        id="export-menu"
                        role="menu"
                        aria-labelledby="export"
                        onkeydown={menuKey}
                    >
                        <li role="none">
                            <button
                                type="button"
                                role="menuitem"
                                id="export-svg"
                                onclick={() => exportAs('svg')}>{t('lineage.exportSvg')}</button
                            >
                        </li>
                        <li role="none">
                            <button
                                type="button"
                                role="menuitem"
                                id="export-png"
                                onclick={() => exportAs('png')}>{t('lineage.exportPng')}</button
                            >
                        </li>
                    </ul>
                {/if}
            </div>
            <button
                type="button"
                class="tool"
                id="list-toggle"
                aria-pressed={listOpen}
                aria-controls={listOpen ? 'inspector' : undefined}
                onclick={() => (listOpen = !listOpen)}
                title={t('lineage.list')}
                ><Icon kind="list" /><span class:sr-only={!full}>{t('lineage.list')}</span></button
            >
            <button
                type="button"
                class="tool icon-only"
                id="fullscreen"
                aria-label={full ? t('lineage.exitFullScreen') : t('lineage.fullScreen')}
                title={full ? t('lineage.exitFullScreen') : t('lineage.fullScreen')}
                aria-keyshortcuts="Shift+L"
                onclick={() => viewer.toggleFullScreen()}><Icon kind={full ? 'shrink' : 'expand'} /></button
            >
        </div>
    </form>
    <p class="selector-status" id="selector-status" role="status" aria-live="polite">
        <span>{t('lineage.showing', { count: viewer.target.size, total: model.viewNodes.length })}</span>
        {#each viewer.selector.errors.concat(viewer.excluded.errors) as error (error)}<span class="warn"
                >{error}</span
            >{/each}
        {#if viewer.selection.unmatched.length}<span class="warn"
                >{t('lineage.unmatched', { atoms: viewer.selection.unmatched.join(', ') })}</span
            >{/if}
    </p>
    <div class="lineage-body">
        <!-- An application region: the arrow keys move between nodes, as the help text says. -->
        <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
        <div
            id="graph-box"
            class="graph-box"
            bind:this={box}
            role="application"
            aria-roledescription={t('lineage.roledescription')}
            aria-label={t('lineage.label', { count: viewer.target.size })}
            aria-describedby="graph-help"
            tabindex="0"
            {onkeydown}
        >
            <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
            <svg
                id="graph"
                class:dragging
                class:far
                class:tiny
                bind:this={svg}
                {onpointerdown}
                {onpointermove}
                onpointerup={endDrag}
                onpointercancel={endDrag}
                {onwheel}
                {onclick}
                {ondblclick}
                aria-hidden="true"
            >
                <defs>
                    <marker
                        id="arrow"
                        viewBox="0 0 10 10"
                        refX="9"
                        refY="5"
                        markerWidth="6"
                        markerHeight="6"
                        orient="auto"><path d="M0,1 L9,5 L0,9 z" /></marker
                    >
                    <marker
                        id="arrow-hl"
                        viewBox="0 0 10 10"
                        refX="9"
                        refY="5"
                        markerWidth="6"
                        markerHeight="6"
                        orient="auto"><path d="M0,1 L9,5 L0,9 z" /></marker
                    >
                </defs>
                <g id="viewport" transform="translate({view.x},{view.y}) scale({view.k})">
                    <g id="edges">
                        {#each edges as e (e.key)}
                            {@const on = edgeOn(e)}
                            <path
                                class="edge {e.kind} t-{edgeClass(e)}"
                                class:hl={on}
                                class:dim={!!lit && !on}
                                stroke-dasharray={DASH[edgeClass(e)] || undefined}
                                d={curve(anchor(e.from, e.fromCol, 'out'), anchor(e.to, e.toCol, 'in'))}
                                marker-end={e.kind === 'feeds' || e.kind === 'runs'
                                    ? on
                                        ? 'url(#arrow-hl)'
                                        : 'url(#arrow)'
                                    : undefined}><title>{e.title}</title></path
                            >
                        {/each}
                    </g>
                    <g id="nodes">
                        {#each cards as card (card.id)}
                            {@const p = at(card.id)}
                            {@const open = card.rows.length > 0 && !far}
                            {@const seed = viewer.selection.seeds.has(card.id)}
                            <g
                                class="node k-{card.n.kind}"
                                id="card-{cssId(card.id)}"
                                class:selected={card.id === viewer.highlight}
                                class:seed
                                class:on-path={!!lit?.has(card.id)}
                                class:dim={!!lit && !lit.has(card.id)}
                                class:cursor={card.id === current}
                                data-id={card.id}
                                transform="translate({p.x},{p.y})"
                            >
                                <title
                                    >{kindName(card.n.kind)}: {card.n.relation ?? model.label(card.id)}</title
                                >
                                <rect class="card" width={W} height={p.h} rx="12" />
                                <rect class="kind-bar" width="5" height={Math.min(p.h, HEAD)} rx="2" x="0" />
                                {#if tiny}
                                    <!-- Too small to read: the shape alone. -->
                                {:else if far}
                                    <text class="lod" x={W / 2} y={HEAD / 2}
                                        >{fitText(card.n.name, FONT_NAME, W - 24)}</text
                                    >
                                {:else}
                                    <g class="icon" transform="translate(16,{HEAD / 2 - 10}) scale(0.8333)"
                                        ><path d={ICONS[card.n.kind] ?? ''} /></g
                                    >
                                    <text class="name" x="46" y={HEAD / 2}
                                        >{fitText(
                                            card.n.name,
                                            FONT_NAME,
                                            W - 48 - (card.all.length ? 66 : 14),
                                        )}</text
                                    >
                                    {#if card.all.length}
                                        <g class="toggle" data-toggle={card.id}>
                                            <rect x={W - 64} y="8" width="56" height={HEAD - 16} rx="10" />
                                            <title
                                                >{open
                                                    ? t('lineage.hideColumns')
                                                    : t('lineage.showColumns', {
                                                          count: card.all.length,
                                                      })}</title
                                            >
                                            <text class="count" x={W - 32} y={HEAD / 2}
                                                >{card.all.length}</text
                                            >
                                            <path
                                                class="chev"
                                                d={open
                                                    ? `M${W - 26},${HEAD / 2 + 3} l5,-5 l5,5`
                                                    : `M${W - 26},${HEAD / 2 - 2} l5,5 l5,-5`}
                                            />
                                        </g>
                                    {/if}
                                    {#if !far && (card.rows.length || card.more)}
                                        <line class="divider" x1="0" x2={W} y1={HEAD} y2={HEAD} />
                                        {#each card.rows as col, i (col.id)}
                                            {@const type = typeLabel(col.data_type)}
                                            <g
                                                class="row"
                                                class:selected={col.id === viewer.highlight}
                                                class:on-path={!!columnsLit?.has(col.id)}
                                                data-col={col.id}
                                                transform="translate(0,{HEAD + PAD + i * ROW})"
                                            >
                                                <rect x="4" y="1" width={W - 8} height={ROW - 2} rx="8" />
                                                <circle class="dot" cx="16" cy={ROW / 2} r="3" />
                                                <text class="col" x="28" y={ROW / 2}
                                                    >{fitText(
                                                        col.name,
                                                        FONT_ROW,
                                                        W - 44 - (type ? textWidth(type, FONT_MONO) : 0) - 14,
                                                    )}</text
                                                >
                                                {#if type}<text class="type" x={W - 14} y={ROW / 2}
                                                        >{type}</text
                                                    >{/if}
                                            </g>
                                        {/each}
                                        {#if card.more}
                                            <g
                                                class="row more"
                                                data-more={card.id}
                                                transform="translate(0,{HEAD + PAD + card.rows.length * ROW})"
                                            >
                                                <rect x="4" y="1" width={W - 8} height={ROW - 2} rx="8" />
                                                <text class="col" x="28" y={ROW / 2}
                                                    >{t('lineage.moreColumns', { count: card.more })}</text
                                                >
                                            </g>
                                        {/if}
                                    {/if}
                                {/if}
                                {#if !far && (card.id === current || card.id === viewer.highlight) && viewer.selectorText.trim()}
                                    {@const hiddenUp = hiddenNeighbours(card.id, model.upAdj)}
                                    {@const hiddenDown = hiddenNeighbours(card.id, model.downAdj)}
                                    {#if hiddenUp}
                                        <g
                                            class="handle"
                                            data-expand="up"
                                            data-id={card.id}
                                            transform="translate(-14,{HEAD / 2})"
                                        >
                                            <title>{t('lineage.expandUp', { count: hiddenUp })}</title>
                                            <circle r="11" /><text>+{hiddenUp}</text>
                                        </g>
                                    {/if}
                                    {#if hiddenDown}
                                        <g
                                            class="handle"
                                            data-expand="down"
                                            data-id={card.id}
                                            transform="translate({W + 14},{HEAD / 2})"
                                        >
                                            <title>{t('lineage.expandDown', { count: hiddenDown })}</title>
                                            <circle r="11" /><text>+{hiddenDown}</text>
                                        </g>
                                    {/if}
                                {/if}
                            </g>
                        {/each}
                    </g>
                </g>
            </svg>
            {#if empty}<div id="empty">{empty}</div>{/if}
            <p id="graph-help" class="sr-only">{t('lineage.keyboardHelp')}</p>
            <div class="sr-only" role="status" aria-live="polite" id="graph-announce">{announcement}</div>
            <details class="legend" id="legend">
                <summary>{t('lineage.legend')}</summary>
                <div class="legend-body">
                    <p class="legend-title">{t('lineage.edges')}</p>
                    <ul>
                        {#each legendItems as item (item)}
                            {@const count = legendCounts.get(item) ?? 0}
                            <li>
                                <button
                                    type="button"
                                    class="legend-item"
                                    aria-pressed={!viewer.hiddenTransforms.has(item as Transform)}
                                    onclick={() => viewer.toggleTransform(item as Transform)}
                                    disabled={!count && !viewer.hiddenTransforms.has(item as Transform)}
                                >
                                    <svg viewBox="0 0 34 10" aria-hidden="true"
                                        ><path
                                            class="edge sample t-{item}"
                                            d="M1,5 L33,5"
                                            stroke-dasharray={DASH[item] || undefined}
                                        /></svg
                                    >
                                    <span class="grow">{transformName(item)}</span>
                                    <span class="count">{i18n.number(count)}</span>
                                </button>
                            </li>
                        {/each}
                    </ul>
                    <p class="legend-title">{t('lineage.kinds')}</p>
                    <ul class="kinds">
                        {#each kinds as kind (kind)}
                            <li>
                                <button
                                    type="button"
                                    class="chip"
                                    data-kind={kind}
                                    aria-pressed={!viewer.hiddenKinds.has(kind)}
                                    onclick={() => viewer.toggleKind(kind)}
                                    ><Icon {kind} size={16} />{kindName(kind)}</button
                                >
                            </li>
                        {/each}
                    </ul>
                </div>
            </details>
            <canvas
                id="minimap"
                width="400"
                height="250"
                hidden={miniHidden}
                aria-hidden="true"
                bind:this={minimap}
                onpointerdown={event => {
                    miniDrag = true;
                    minimap?.setPointerCapture(event.pointerId);
                    miniPan(event);
                }}
                onpointermove={event => {
                    if (miniDrag) miniPan(event);
                }}
                onpointerup={() => (miniDrag = false)}
            ></canvas>
        </div>
        {#if listOpen}
            <Inspector {viewer} id={subject} onmove={id => moveTo(id)} />
        {/if}
    </div>
</div>
