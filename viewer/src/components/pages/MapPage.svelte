<script module lang="ts">
    import type { MapRenderer as Renderer } from '../../gpu/renderer';
    import type { Prepared as Ready } from '../../gpu/map';

    // The renderer and its buffers outlive the page, so coming back to the map draws at once.
    let kept: { renderer: Renderer; prepared: Ready } | null = null;
</script>

<script lang="ts">
    import { onMount, untrack } from 'svelte';
    import { SvelteSet } from 'svelte/reactivity';
    import {
        fit,
        focusOn,
        inView,
        pan,
        toWorld,
        zoomAt,
        between,
        type Camera,
        type Size,
    } from '../../gpu/camera';
    import { groundOf, readTokens } from '../../gpu/colour';
    import { prepare, rowCount, type Prepared } from '../../gpu/map';
    import { statesFor, step, summarise } from '../../gpu/navigate';
    import { MAX_ROWS } from '../../gpu/place';
    import { gpuDevice, MapRenderer, TooLarge, Unsupported } from '../../gpu/renderer';
    import { i18n, t } from '../../lib/i18n.svelte';
    import { kindName } from '../../lib/labels';
    import { VIEW_KINDS } from '../../lib/model';
    import { nodeRoute } from '../../lib/route';
    import { search } from '../../lib/search';
    import { scrollFor, windowOf } from '../../lib/virtual';
    import type { Viewer } from '../../lib/viewer.svelte';
    import Icon from '../Icon.svelte';

    // The viewer is made once and never replaced.
    const props: { viewer: Viewer } = $props();
    const viewer = untrack(() => props.viewer);
    // svelte-ignore state_referenced_locally
    const model = viewer.model;

    const FLIGHT_MS = 420;
    const LIST_ROW = 60;

    type Status = 'loading' | 'ready' | 'unsupported' | 'failed' | 'lost' | 'empty';
    let status = $state<Status>(model.viewNodes.length ? 'loading' : 'empty');
    let error = $state('');
    let prepared = $state.raw<Prepared | null>(null);
    let renderer: MapRenderer | null = null;

    let stage: HTMLElement | undefined = $state();
    let canvas: HTMLCanvasElement | undefined = $state();
    let probe: HTMLElement | undefined = $state();

    // The camera and pointer change every frame and are not page state; only what the page shows is.
    let camera: Camera = { x: 0, y: 0, k: 1 };
    let size: Size = { w: 0, h: 0 };
    let dpr = 1;
    let hover = -1;
    let hoverRow = -1;
    let flight: { from: Camera; to: Camera; start: number } | null = null;
    let fitted = false;

    let focus = $state(-1);
    let announcement = $state('');
    let hoverText = $state('');
    let hoverAt = $state({ x: 0, y: 0 });
    let labels = $state(true);
    const hiddenKinds = new SvelteSet<string>();
    const stats = $state({ frames: 0, frame: 0, firstFrame: 0, firstFrameAt: 0, gpuBytes: 0 });
    let mounted = 0;

    const kinds = VIEW_KINDS.filter(kind => model.viewNodes.some(n => n.kind === kind));
    const shownAt = (i: number): boolean => !hiddenKinds.has(prepared?.scene.nodes[i]?.kind ?? '');

    // Frames, drawn only when something changes.

    let pending = false;
    function redraw(): void {
        if (pending || !renderer) return;
        pending = true;
        requestAnimationFrame(frame);
    }

    function frame(now: number): void {
        pending = false;
        if (!renderer || status !== 'ready') return;
        if (flight) {
            const t = (now - flight.start) / FLIGHT_MS;
            camera = between(flight.from, flight.to, t);
            if (t >= 1) flight = null;
            else redraw();
        }
        void renderer
            .draw({
                x: camera.x,
                y: camera.y,
                k: camera.k,
                dpr,
                w: size.w,
                h: size.h,
                hover,
                hoverRow,
                focused: focus >= 0,
                labels,
            })
            .then(ms => {
                stats.frames++;
                stats.frame = Math.round(ms * 100) / 100;
                if (!stats.firstFrame) {
                    stats.firstFrame = Math.round(performance.now() - mounted);
                    stats.firstFrameAt = Math.round(performance.now());
                }
            });
    }

    function reducedMotion(): boolean {
        return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
    }

    function moveCamera(to: Camera, animate = true): void {
        if (animate && !reducedMotion()) flight = { from: camera, to, start: performance.now() };
        else {
            flight = null;
            camera = to;
        }
        redraw();
    }

    function fitAll(animate = true): void {
        if (prepared) moveCamera(fit(prepared.bounds, size), animate);
    }

    function zoomBy(factor: number): void {
        flight = null;
        camera = zoomAt(camera, size, size.w / 2, size.h / 2, factor);
        redraw();
    }

    function boxOf(i: number): { x: number; y: number; w: number; h: number } {
        const r = prepared!.rects;
        return { x: r[i * 4]!, y: r[i * 4 + 1]!, w: r[i * 4 + 2]!, h: r[i * 4 + 3]! };
    }

    // Focus: the card the keyboard and search are on, with its lineage lit.

    function updateStates(): { up: number; down: number } {
        if (!prepared || !renderer) return { up: 0, down: 0 };
        const { states, lineage } = statesFor(model, prepared, focus, hiddenKinds);
        renderer.setStates(states);
        return lineage;
    }

    function setFocus(i: number, follow: boolean, animate = true): void {
        if (!prepared) return;
        focus = i;
        const lineage = updateStates();
        if (i >= 0) {
            const n = prepared.scene.nodes[i]!;
            announcement = t('map.announce', {
                name: model.label(n.id),
                kind: kindName(n.kind),
                up: lineage.up,
                down: lineage.down,
            });
            const box = boxOf(i);
            if (follow && !inView(camera, size, box)) moveCamera(focusOn(camera, size, box), animate);
            revealInList(i);
        } else announcement = '';
        redraw();
    }

    /** Leaves a note of the focused card in the address, then opens a page: Back returns here. */
    function openPage(id: string, from: number): void {
        viewer.go({ ...viewer.route, focus: prepared!.scene.ids[from] }, true);
        viewer.open(id);
    }

    function openCard(i: number, row = -1): void {
        const columns = prepared!.scene.columns[i]!;
        const column = row >= 0 && row < MAX_ROWS ? columns[row] : undefined;
        openPage(column ? column.id : prepared!.scene.ids[i]!, i);
    }

    // Pointer

    const pointers = new Map<number, { x: number; y: number }>();
    let drag: { x: number; y: number; moved: boolean } | null = null;

    function local(event: PointerEvent | WheelEvent): [number, number] {
        const box = canvas!.getBoundingClientRect();
        return [event.clientX - box.left, event.clientY - box.top];
    }

    function pick(sx: number, sy: number): { node: number; row: number } | null {
        if (!prepared) return null;
        const [wx, wy] = toWorld(camera, size, sx, sy);
        const hit = prepared.grid.hit(wx, wy, i => rowCount(prepared!, i));
        return hit && shownAt(hit.node) ? hit : null;
    }

    function onpointerdown(event: PointerEvent): void {
        if (event.button !== 0) return;
        canvas?.setPointerCapture(event.pointerId);
        const [x, y] = local(event);
        pointers.set(event.pointerId, { x, y });
        drag = { x, y, moved: pointers.size > 1 };
        flight = null;
    }

    function onpointermove(event: PointerEvent): void {
        const [x, y] = local(event);
        const before = pointers.get(event.pointerId);
        if (before && pointers.size === 2) {
            // Two fingers: zoom by the change in their distance, about their midpoint.
            const other = [...pointers.entries()].find(([id]) => id !== event.pointerId)![1];
            const was = Math.hypot(before.x - other.x, before.y - other.y);
            const now = Math.hypot(x - other.x, y - other.y);
            pointers.set(event.pointerId, { x, y });
            if (was > 0) camera = zoomAt(camera, size, (x + other.x) / 2, (y + other.y) / 2, now / was);
            redraw();
            return;
        }
        if (drag && before) {
            pointers.set(event.pointerId, { x, y });
            if (Math.hypot(x - drag.x, y - drag.y) > 4) drag.moved = true;
            if (drag.moved) {
                camera = pan(camera, x - before.x, y - before.y);
                redraw();
            }
            return;
        }
        const hit = pick(x, y);
        const node = hit?.node ?? -1;
        const row = hit?.row ?? -1;
        if (node !== hover || row !== hoverRow) {
            hover = node;
            hoverRow = row;
            if (node >= 0) {
                const n = prepared!.scene.nodes[node]!;
                const column = row >= 0 ? prepared!.scene.columns[node]![row] : undefined;
                hoverText = t('map.hover', {
                    name: column ? model.label(column.id) : model.label(n.id),
                    kind: kindName(column ? 'column' : n.kind),
                });
            } else hoverText = '';
            redraw();
        }
        hoverAt = { x, y };
    }

    function onpointerup(event: PointerEvent): void {
        const was = drag;
        pointers.delete(event.pointerId);
        if (pointers.size) return;
        drag = null;
        if (!was || was.moved || event.type === 'pointercancel') return;
        const [x, y] = local(event);
        const hit = pick(x, y);
        if (hit) openCard(hit.node, hit.row);
    }

    function onpointerleave(): void {
        if (drag) return;
        hover = hoverRow = -1;
        hoverText = '';
        redraw();
    }

    function onwheel(event: WheelEvent): void {
        event.preventDefault();
        flight = null;
        const [x, y] = local(event);
        const unit = event.deltaMode === 1 ? 0.05 : event.deltaMode === 2 ? 1 : 0.0015;
        // Pinch on a trackpad arrives as a wheel with the control key held, in finer steps.
        const factor = Math.exp(-event.deltaY * unit * (event.ctrlKey ? 6 : 1));
        camera = zoomAt(camera, size, x, y, factor);
        redraw();
    }

    // Keyboard

    function nearestToCentre(): number {
        if (!prepared) return -1;
        let best = -1;
        let distance = Infinity;
        prepared.scene.ids.forEach((_, i) => {
            if (!shownAt(i)) return;
            const b = boxOf(i);
            const d = Math.hypot(b.x + b.w / 2 - camera.x, b.y + Math.min(b.h, 46) / 2 - camera.y);
            if (d < distance) {
                distance = d;
                best = i;
            }
        });
        return best;
    }

    function onkeydown(event: KeyboardEvent): void {
        if (event.ctrlKey || event.metaKey || event.altKey || !prepared) return;
        const key = event.key;
        if (key === 'ArrowLeft' || key === 'ArrowRight' || key === 'ArrowUp' || key === 'ArrowDown') {
            if (focus < 0) setFocus(nearestToCentre(), true);
            else {
                const next = step(model, prepared, focus, key, shownAt);
                if (next !== null) setFocus(next, true);
                else if (key === 'ArrowRight') announcement = t('lineage.noDownstream');
                else if (key === 'ArrowLeft') announcement = t('lineage.noUpstream');
                else announcement = t('map.layerEnd');
            }
        } else if (key === 'Enter') {
            if (focus >= 0) openCard(focus);
        } else if (key === 'Escape') {
            if (focus < 0) return;
            setFocus(-1, false);
        } else if (key === 'f' || key === 'F' || key === '0') fitAll();
        else if (key === '+' || key === '=') zoomBy(1.25);
        else if (key === '-') zoomBy(0.8);
        else if (key === 'Home') setFocus(prepared.scene.order.flat().find(shownAt) ?? -1, true);
        else return;
        event.preventDefault();
        event.stopPropagation();
    }

    // Search

    let query = $state('');
    let active = $state(0);
    let searching = $state(false);
    const results = $derived.by(() => {
        if (!prepared || !query.trim()) return [];
        const index = prepared.scene.index;
        return search(viewer.index, query, { kinds: [...VIEW_KINDS] }, i18n.compare, 40)
            .hits.filter(h => index.has(h.entry.id) && shownAt(index.get(h.entry.id)!))
            .slice(0, 8);
    });

    function choose(id: string): void {
        const i = prepared?.scene.index.get(id);
        if (i === undefined) return;
        searching = false;
        setFocus(i, false);
        moveCamera(focusOn(camera, size, boxOf(i)));
        canvas?.focus();
    }

    function searchKey(event: KeyboardEvent): void {
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            if (!results.length) return;
            searching = true;
            active = (active + (event.key === 'ArrowDown' ? 1 : results.length - 1)) % results.length;
        } else if (event.key === 'Enter') {
            const hit = results[active] ?? results[0];
            if (hit) choose(hit.entry.id);
        } else if (event.key === 'Escape') {
            if (!searching) return;
            searching = false;
        } else return;
        event.preventDefault();
        event.stopPropagation();
    }

    // Filters

    function toggleKind(kind: string): void {
        if (hiddenKinds.has(kind)) hiddenKinds.delete(kind);
        else hiddenKinds.add(kind);
        if (focus >= 0 && !shownAt(focus)) setFocus(-1, false);
        else {
            updateStates();
            redraw();
        }
    }

    function toggleLabels(): void {
        labels = !labels;
        redraw();
    }

    // The list of the cards shown, kept with the filters.

    const shownList = $derived.by(() => {
        void hiddenKinds.size;
        if (!prepared) return [];
        return prepared.scene.ids
            .map((_, i) => i)
            .filter(i => !hiddenKinds.has(prepared!.scene.nodes[i]!.kind));
    });
    let scroller: HTMLElement | undefined = $state();
    let scrollTop = $state(0);
    let listHeight = $state(400);
    const win = $derived(windowOf(shownList.length, LIST_ROW, scrollTop, listHeight));

    // The list is one tab stop: the arrow keys move between its rows, and between a row's two
    // controls, showing the node on the map and opening its page.
    let cursor = $state(0);
    let cell = $state<0 | 1>(0);
    const tabRow = $derived.by(() => {
        const at = Math.min(cursor, shownList.length - 1);
        return at >= win.start && at < win.end ? at : win.start;
    });

    function scrollToRow(at: number): void {
        if (!scroller) return;
        const top = scrollFor(at, LIST_ROW, scroller.scrollTop, scroller.clientHeight);
        if (top !== null) scroller.scrollTop = top;
    }

    function revealInList(i: number): void {
        const at = shownList.indexOf(i);
        if (at < 0) return;
        cursor = at;
        scrollToRow(at);
    }

    function focusRow(at: number, which: 0 | 1): void {
        cursor = at;
        cell = which;
        scrollToRow(at);
        // The row may be rendered only after the scroll.
        requestAnimationFrame(() =>
            scroller?.querySelector<HTMLElement>(`[data-row="${at}"] ${which ? 'a' : 'button'}`)?.focus(),
        );
    }

    function listKey(event: KeyboardEvent, at: number): void {
        if (event.ctrlKey || event.metaKey || event.altKey) return;
        const last = shownList.length - 1;
        const page = Math.max(1, Math.floor(listHeight / LIST_ROW) - 1);
        // Left and right follow the reading direction in right-to-left scripts.
        const rtl = i18n.dir === 'rtl';
        const key = event.key;
        if (key === 'ArrowDown') focusRow(Math.min(last, at + 1), cell);
        else if (key === 'ArrowUp') focusRow(Math.max(0, at - 1), cell);
        else if (key === 'PageDown') focusRow(Math.min(last, at + page), cell);
        else if (key === 'PageUp') focusRow(Math.max(0, at - page), cell);
        else if (key === 'Home') focusRow(0, cell);
        else if (key === 'End') focusRow(last, cell);
        else if (key === (rtl ? 'ArrowLeft' : 'ArrowRight')) focusRow(at, 1);
        else if (key === (rtl ? 'ArrowRight' : 'ArrowLeft')) focusRow(at, 0);
        else return;
        event.preventDefault();
    }

    function locate(i: number): void {
        setFocus(i, false);
        moveCamera(focusOn(camera, size, boxOf(i)));
    }

    const summary = $derived.by(() => {
        void hiddenKinds.size;
        return prepared
            ? summarise(model, prepared, i => !hiddenKinds.has(prepared!.scene.nodes[i]!.kind))
            : null;
    });

    // Set-up

    function applyTheme(): void {
        if (!renderer || !probe) return;
        renderer.setTheme(readTokens(probe), groundOf(probe));
        redraw();
    }

    function resize(entry: ResizeObserverEntry): void {
        if (!canvas) return;
        const box = entry.contentRect;
        const device = entry.devicePixelContentBoxSize?.[0];
        const limit = renderer?.device.limits.maxTextureDimension2D ?? 8192;
        // One ratio for both axes, from the exact device pixel width where the browser gives it, so
        // lines stay one pixel wide, and lowered when the canvas would exceed the device's textures.
        const ratio = device && box.width > 0 ? device.inlineSize / box.width : window.devicePixelRatio || 1;
        dpr = Math.min(ratio, limit / Math.max(1, box.width), limit / Math.max(1, box.height));
        canvas.width = Math.max(1, Math.round(box.width * dpr));
        canvas.height = Math.max(1, Math.round(box.height * dpr));
        size = { w: box.width, h: box.height };
        if (!fitted && prepared && size.w > 0) start();
        redraw();
    }

    function start(): void {
        fitted = true;
        camera = fit(prepared!.bounds, size);
        const id = viewer.route.focus;
        const i = id ? prepared!.scene.index.get(id) : undefined;
        // Coming back from a node's page: that node is in focus, in the middle, as it was left.
        if (i === undefined) return;
        camera = focusOn(camera, size, boxOf(i));
        setFocus(i, false);
    }

    /** Lays out and uploads the map; ``refit`` false keeps the camera, as when the language changes. */
    async function setUp(refit = true): Promise<void> {
        if (refit || status !== 'ready') status = model.viewNodes.length ? 'loading' : 'empty';
        if (status === 'empty') return;
        try {
            const device = await gpuDevice();
            const ready = await prepare(model, count => `+${i18n.number(count)}`);
            if (kept && kept.prepared === ready && kept.renderer.device === device) renderer = kept.renderer;
            else {
                // The same device keeps its pipelines: only the buffers are uploaded again.
                const reuse = kept?.renderer.device === device ? kept.renderer : null;
                if (!reuse) kept?.renderer.destroy();
                kept = null;
                renderer = reuse ?? (await MapRenderer.create(device));
                await renderer.upload(ready.content);
                kept = { renderer, prepared: ready };
            }
            void device.lost.then(info => {
                if (info.reason === 'destroyed') return;
                kept = null;
                renderer = null;
                status = 'lost';
            });
            renderer.attach(canvas!);
            prepared = ready;
            stats.gpuBytes = renderer.gpuBytes;
            status = 'ready';
            applyTheme();
            updateStates();
            if (size.w > 0 && (refit || !fitted)) start();
            redraw();
        } catch (e) {
            if (e instanceof Unsupported) status = 'unsupported';
            else {
                status = 'failed';
                error =
                    e instanceof TooLarge
                        ? t('map.tooLarge', { needed: e.needed, limit: e.limit })
                        : e instanceof Error
                          ? e.message
                          : String(e);
                console.error(e);
            }
        }
    }

    // A new language sets the labels again; the layout and the camera stay as they are.
    let shownLocale = i18n.locale;
    $effect(() => {
        const locale = i18n.locale;
        if (locale === shownLocale) return;
        shownLocale = locale;
        if (untrack(() => status) === 'ready') void setUp(false);
    });

    function retry(): void {
        fitted = false;
        void setUp();
    }

    onMount(() => {
        mounted = performance.now();
        const observer = new ResizeObserver(entries => entries.forEach(resize));
        if (stage) observer.observe(stage, { box: 'content-box' });
        const themes = new MutationObserver(applyTheme);
        themes.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
        // Wheel listeners added by Svelte may be passive; this one must be able to stop the page scrolling.
        canvas?.addEventListener('wheel', onwheel, { passive: false });
        void setUp();
        return () => {
            observer.disconnect();
            themes.disconnect();
            canvas?.removeEventListener('wheel', onwheel);
            renderer?.detach();
            renderer = null;
        };
    });
</script>

<article class="map-page">
    <header class="map-head">
        <h1 tabindex="-1">{t('page.map')}</h1>
        <p class="lead">
            {status === 'empty'
                ? t('lineage.empty')
                : t('map.lead', { count: model.viewNodes.length, edges: model.layoutEdges.length })}
        </p>
    </header>

    {#if status === 'unsupported'}
        <div class="map-message" id="map-unsupported" role="status">
            <p>{t('map.unsupported')}</p>
            <a href={viewer.href({ page: 'lineage' })}>{t('lineage.open')}</a>
        </div>
    {:else if status !== 'empty'}
        <div class="map-tools" role="toolbar" aria-label={t('lineage.tools')}>
            <div class="map-search" role="search">
                <label class="sr-only" for="map-find">{t('map.find')}</label>
                <Icon kind="search" size={16} />
                <input
                    id="map-find"
                    type="search"
                    role="combobox"
                    autocomplete="off"
                    placeholder={t('map.findPlaceholder')}
                    aria-expanded={searching && results.length > 0}
                    aria-controls="map-results"
                    aria-autocomplete="list"
                    aria-activedescendant={searching && results[active] ? `map-result-${active}` : undefined}
                    bind:value={query}
                    oninput={() => {
                        searching = true;
                        active = 0;
                    }}
                    onkeydown={searchKey}
                    onblur={() => setTimeout(() => (searching = false), 150)}
                />
                <ul
                    id="map-results"
                    role="listbox"
                    aria-label={t('map.find')}
                    hidden={!searching || !results.length}
                >
                    {#each results as hit, i (hit.entry.id)}
                        <li
                            id="map-result-{i}"
                            role="option"
                            aria-selected={i === active}
                            onpointerdown={event => {
                                event.preventDefault();
                                choose(hit.entry.id);
                            }}
                        >
                            <span class="name">{hit.entry.label}</span>
                            <span class="kind">{kindName(hit.entry.kind)}</span>
                        </li>
                    {/each}
                </ul>
                <span class="sr-only" role="status" aria-live="polite"
                    >{query.trim() && prepared
                        ? results.length
                            ? t('map.matches', { count: results.length })
                            : t('map.noMatch')
                        : ''}</span
                >
            </div>
            <div class="map-kinds" role="group" aria-label={t('lineage.kinds')}>
                {#each kinds as kind (kind)}
                    <button
                        type="button"
                        class="chip k-{kind}"
                        aria-pressed={!hiddenKinds.has(kind)}
                        onclick={() => toggleKind(kind)}
                        ><span class="dot" aria-hidden="true"></span>{kindName(kind)}</button
                    >
                {/each}
            </div>
            <button type="button" class="chip" aria-pressed={labels} onclick={toggleLabels}
                >{t('map.labels')}</button
            >
            <div class="map-zoom">
                <button
                    type="button"
                    class="icon-button"
                    aria-label={t('lineage.zoomOut')}
                    onclick={() => zoomBy(0.8)}><Icon kind="minus" /></button
                >
                <button
                    type="button"
                    class="icon-button"
                    aria-label={t('lineage.zoomIn')}
                    onclick={() => zoomBy(1.25)}><Icon kind="plus" /></button
                >
                <button
                    type="button"
                    class="icon-button"
                    aria-label={t('lineage.fit')}
                    aria-keyshortcuts="F"
                    onclick={() => fitAll()}><Icon kind="fit" /></button
                >
            </div>
        </div>

        <div class="map-body">
            <div
                class="map-stage"
                id="map-stage"
                bind:this={stage}
                data-state={status}
                data-frames={stats.frames}
                data-frame-ms={stats.frame}
                data-first-frame-ms={stats.firstFrame}
                data-first-frame-at={stats.firstFrameAt}
                data-layout-ms={prepared ? Math.round(prepared.timings.layout) : undefined}
                data-order-ms={prepared ? Math.round(prepared.timings.order) : undefined}
                data-atlas-ms={prepared ? Math.round(prepared.timings.atlas) : undefined}
                data-gpu-bytes={stats.gpuBytes || undefined}
                data-dropped-glyphs={prepared?.dropped}
                data-layout-hash={prepared?.hash}
            >
                <!-- An application region: the arrow keys move between nodes, as the description says. -->
                <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions, a11y_no_interactive_element_to_noninteractive_role -->
                <canvas
                    id="map"
                    bind:this={canvas}
                    role="application"
                    aria-roledescription={t('map.roledescription')}
                    aria-label={t('map.label', { count: model.viewNodes.length })}
                    aria-describedby="map-help"
                    tabindex="0"
                    {onkeydown}
                    {onpointerdown}
                    {onpointermove}
                    {onpointerup}
                    onpointercancel={onpointerup}
                    {onpointerleave}
                ></canvas>
                <p id="map-help" class="sr-only">{t('map.help')}</p>
                <span class="probe" bind:this={probe} aria-hidden="true"></span>
                {#if hoverText}
                    <p
                        class="hover-label"
                        aria-hidden="true"
                        style:left="{hoverAt.x + 14}px"
                        style:top="{hoverAt.y + 14}px"
                    >
                        {hoverText}
                    </p>
                {/if}
                {#if status === 'loading'}
                    <p class="map-message overlay" role="status">{t('map.loading')}</p>
                {:else if status === 'failed'}
                    <p class="map-message overlay" role="alert">{t('map.failed', { error })}</p>
                {:else if status === 'lost'}
                    <div class="map-message overlay" role="alert">
                        <p>{t('map.lost')}</p>
                        <button type="button" class="chip" onclick={retry}>{t('map.retry')}</button>
                    </div>
                {/if}
                <div class="sr-only" role="status" aria-live="polite" id="map-announce">{announcement}</div>
                {#if summary}
                    <details class="map-summary" open>
                        <summary>{t('map.summary')}</summary>
                        <h2 class="sr-only">{t('map.summary')}</h2>
                        <p class="sub">{t('map.packages')}</p>
                        <ul class="shares">
                            {#each summary.packages.slice(0, 6) as p (p.name)}
                                <li>
                                    <span class="name">{p.name || t('map.noPackage')}</span>
                                    <span class="share"
                                        >{i18n.number(p.count / Math.max(1, summary.total), 'percent')}</span
                                    >
                                </li>
                            {/each}
                        </ul>
                        <p>{t('map.orphans', { count: summary.orphans })}</p>
                        <p>{t('map.untested', { count: summary.untested })}</p>
                        {#if prepared?.dropped}
                            <p id="map-dropped">{t('map.dropped', { count: prepared.dropped })}</p>
                        {/if}
                    </details>
                {/if}
            </div>
            <section class="map-list" aria-labelledby="map-list-title">
                <h2 id="map-list-title">{t('map.list')}</h2>
                <p class="map-count">
                    {t('map.shown', { shown: shownList.length, total: model.viewNodes.length })}
                </p>
                <p id="map-list-help" class="sr-only">{t('map.listHelp')}</p>
                <div
                    class="map-scroller"
                    bind:this={scroller}
                    bind:clientHeight={listHeight}
                    onscroll={() => (scrollTop = scroller?.scrollTop ?? 0)}
                >
                    <ul style:padding-block-start="{win.before}px" style:padding-block-end="{win.after}px">
                        {#each shownList.slice(win.start, win.end) as i, n (i)}
                            {@const node = prepared!.scene.nodes[i]!}
                            {@const at = win.start + n}
                            <li
                                class:current={i === focus}
                                data-row={at}
                                aria-setsize={shownList.length}
                                aria-posinset={at + 1}
                            >
                                <button
                                    type="button"
                                    class="locate"
                                    tabindex={at === tabRow && cell === 0 ? 0 : -1}
                                    aria-describedby="map-list-help"
                                    onclick={() => locate(i)}
                                    onkeydown={event => listKey(event, at)}
                                    onfocus={() => {
                                        cursor = at;
                                        cell = 0;
                                    }}
                                >
                                    <span class="name">{node.name}</span>
                                    <span class="sr-only"
                                        >{t('map.locate', { name: model.label(node.id) })}</span
                                    >
                                    <span class="context" aria-hidden="true"
                                        >{kindName(node.kind)}{prepared!.scene.packages[i]
                                            ? ` · ${prepared!.scene.packages[i]}`
                                            : ''}</span
                                    >
                                </button>
                                <a
                                    href={viewer.href(nodeRoute(node.id))}
                                    tabindex={at === tabRow && cell === 1 ? 0 : -1}
                                    aria-label="{t('map.open')}: {model.label(node.id)}"
                                    onkeydown={event => listKey(event, at)}
                                    onfocus={() => {
                                        cursor = at;
                                        cell = 1;
                                    }}>{t('map.open')}</a
                                >
                            </li>
                        {/each}
                    </ul>
                </div>
            </section>
        </div>
    {/if}
</article>

<style>
    .map-page {
        display: flex;
        flex-direction: column;
        block-size: 100%;
        min-block-size: 480px;
        padding: 18px 24px 20px;
        gap: 12px;
    }
    .map-head h1 {
        margin: 0;
        font-size: 28px;
        font-weight: 800;
        letter-spacing: -0.03em;
    }
    .map-head .lead {
        margin: 4px 0 0;
        color: var(--text-2);
    }
    .map-tools {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 10px;
    }
    .map-search {
        position: relative;
        display: flex;
        align-items: center;
        gap: 8px;
        flex: 1 1 260px;
        max-inline-size: 420px;
        padding: 0 12px;
        border: 1px solid var(--field-border);
        border-radius: var(--radius-pill);
        background: var(--field);
        color: var(--text-3);
    }
    .map-search:focus-within {
        outline: 2px solid var(--accent);
        outline-offset: 2px;
    }
    .map-search input {
        flex: 1;
        min-inline-size: 0;
        padding: 9px 0;
        border: 0;
        background: transparent;
        color: var(--text);
        font: inherit;
        font-size: 15px;
        outline: none;
    }
    .map-search input::placeholder {
        color: var(--text-3);
    }
    #map-results {
        position: absolute;
        z-index: 5;
        inset-inline: 0;
        inset-block-start: calc(100% + 6px);
        margin: 0;
        padding: 6px;
        list-style: none;
        border: 1px solid var(--card-border);
        border-radius: var(--radius-card);
        background: var(--panel);
        box-shadow: var(--shadow);
    }
    #map-results li {
        display: flex;
        justify-content: space-between;
        gap: 12px;
        padding: 8px 10px;
        border-radius: 8px;
        cursor: pointer;
        color: var(--text);
    }
    #map-results li[aria-selected='true'],
    #map-results li:hover {
        background: var(--accent-soft);
    }
    #map-results .kind {
        color: var(--text-3);
    }
    .map-kinds {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
    }
    .chip {
        display: inline-flex;
        align-items: center;
        gap: 7px;
        min-block-size: 36px;
        padding: 6px 14px;
        border: 1px solid var(--field-border);
        border-radius: var(--radius-pill);
        background: transparent;
        color: var(--text-2);
        font: inherit;
        font-size: 14px;
        cursor: pointer;
    }
    .chip[aria-pressed='true'] {
        border-color: var(--accent);
        background: var(--accent-soft);
        color: var(--text);
    }
    .chip:focus-visible,
    .icon-button:focus-visible,
    .map-list a:focus-visible,
    .map-list button:focus-visible {
        outline: 2px solid var(--accent);
        outline-offset: 2px;
    }
    .dot {
        inline-size: 10px;
        block-size: 10px;
        border-radius: 3px;
        background: var(--edge);
    }
    .k-source .dot,
    .k-seed .dot {
        background: var(--t-renamed);
    }
    .k-model .dot,
    .k-snapshot .dot {
        background: var(--accent);
    }
    .k-table .dot {
        background: var(--t-cast);
    }
    .k-task .dot {
        background: var(--t-aggregate);
    }
    .k-exposure .dot {
        background: var(--t-window);
    }
    .map-zoom {
        display: flex;
        gap: 4px;
        margin-inline-start: auto;
    }
    .icon-button {
        display: inline-grid;
        place-items: center;
        inline-size: 38px;
        block-size: 38px;
        border: 1px solid var(--field-border);
        border-radius: var(--radius-pill);
        background: transparent;
        color: var(--text-2);
        cursor: pointer;
    }
    /* The stage comes first in the page, and so in the tab order, but the list is drawn beside it. */
    .map-body {
        display: grid;
        grid-template-areas: 'list stage';
        grid-template-columns: minmax(220px, 300px) 1fr;
        gap: 12px;
        flex: 1 1 auto;
        min-block-size: 0;
    }
    .map-list {
        grid-area: list;
        display: flex;
        flex-direction: column;
        min-block-size: 0;
        border: 1px solid var(--card-border);
        border-radius: var(--radius-card);
        background: var(--card);
    }
    .map-list h2 {
        margin: 12px 14px 0;
        font-size: 16px;
        font-weight: 800;
    }
    .map-list .map-count {
        margin: 2px 14px 8px;
        color: var(--text-3);
        font-size: 14px;
    }
    .map-scroller {
        flex: 1 1 auto;
        min-block-size: 0;
        overflow: auto;
    }
    .map-scroller ul {
        margin: 0;
        padding-inline: 6px;
        list-style: none;
    }
    .map-scroller li {
        display: flex;
        align-items: center;
        gap: 6px;
        block-size: 60px;
        padding-inline-end: 8px;
        border-radius: 8px;
    }
    .map-scroller li.current {
        background: var(--accent-soft);
    }
    .locate {
        display: flex;
        flex: 1;
        flex-direction: column;
        align-items: flex-start;
        min-inline-size: 0;
        padding: 6px 8px;
        border: 0;
        border-radius: 8px;
        background: transparent;
        color: var(--text);
        font: inherit;
        text-align: start;
        cursor: pointer;
    }
    .locate .name {
        max-inline-size: 100%;
        overflow: hidden;
        font-weight: 700;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .locate .context {
        color: var(--text-3);
        font-size: 14px;
    }
    .map-list a {
        color: var(--accent);
        font-size: 14px;
        font-weight: 700;
    }
    .map-stage {
        grid-area: stage;
        position: relative;
        min-block-size: 320px;
        overflow: hidden;
        border: 1px solid var(--card-border);
        border-radius: var(--radius-card);
        background: var(--ground);
    }
    #map {
        display: block;
        inline-size: 100%;
        block-size: 100%;
        touch-action: none;
        cursor: grab;
    }
    #map:focus-visible {
        outline: 3px solid var(--accent);
        outline-offset: -3px;
    }
    .probe {
        position: absolute;
        inline-size: 0;
        block-size: 0;
        overflow: hidden;
    }
    .hover-label {
        position: absolute;
        margin: 0;
        padding: 6px 10px;
        border: 1px solid var(--card-border);
        border-radius: 8px;
        background: var(--panel);
        color: var(--text);
        font-size: 14px;
        white-space: nowrap;
        pointer-events: none;
    }
    .map-message {
        margin: 0;
        padding: 14px 16px;
        border: 1px solid var(--card-border);
        border-radius: var(--radius-card);
        background: var(--card);
        color: var(--text);
    }
    .map-message.overlay {
        position: absolute;
        inset-block-start: 16px;
        inset-inline-start: 16px;
        max-inline-size: calc(100% - 32px);
    }
    .map-message p {
        margin: 0 0 10px;
    }
    .map-summary {
        position: absolute;
        inset-block-end: 12px;
        inset-inline-end: 12px;
        min-inline-size: 220px;
        max-inline-size: min(320px, calc(100% - 24px));
        padding: 10px 14px;
        border: 1px solid var(--card-border);
        border-radius: var(--radius-card);
        background: var(--panel);
        color: var(--text-2);
        font-size: 14px;
    }
    .map-summary summary {
        color: var(--text);
        font-weight: 800;
        cursor: pointer;
    }
    .map-summary summary:focus-visible {
        outline: 2px solid var(--accent);
        outline-offset: 2px;
    }
    .map-summary p {
        margin: 6px 0 0;
    }
    .map-summary .sub {
        color: var(--text-3);
    }
    .shares {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        margin: 6px 0 0;
        padding: 0;
        list-style: none;
    }
    .shares li {
        display: inline-flex;
        gap: 6px;
        padding: 3px 10px;
        border-radius: var(--radius-pill);
        background: var(--accent-soft);
        color: var(--text);
    }
    .shares .share {
        font-weight: 800;
    }
    @media (max-width: 900px) {
        .map-page {
            block-size: auto;
        }
        .map-body {
            display: flex;
            flex-direction: column;
        }
        .map-stage {
            block-size: 70vh;
        }
        .map-list {
            block-size: 50vh;
        }
        .map-zoom {
            margin-inline-start: 0;
        }
    }
</style>
