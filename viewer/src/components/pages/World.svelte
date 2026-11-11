<script lang="ts">
    import { onMount, untrack } from 'svelte';
    import { between, fit, inView, toScreen, zoomAt, type Camera, type Size } from '../../gpu/camera';
    import { i18n, t } from '../../lib/i18n.svelte';
    import type { Viewer } from '../../lib/viewer.svelte';
    import {
        buildWorld,
        layoutHash,
        metricKeys,
        metricsOf,
        stepFrom,
        TRUSTS,
        type WorldMetric,
    } from '../../lib/world';
    import Icon from '../Icon.svelte';
    import TrustMark from '../TrustMark.svelte';
    import WorldPanel from './WorldPanel.svelte';

    // The viewer is made once and never replaced.
    const props: { viewer: Viewer } = $props();
    const viewer = untrack(() => props.viewer);
    const began = performance.now();

    // svelte-ignore state_referenced_locally
    const metrics = metricsOf(viewer.world, viewer.ontology);
    const world = buildWorld(viewer.ontology, metrics, (a, b) => i18n.compare(a, b));
    const nodes = world.nodes;
    const edges = world.edges;
    const empty = nodes.length === 0 && metrics.length === 0;
    const byName = new Map(nodes.map((n, i) => [n.name, i]));
    const neighbours = nodes.map(() => new Set<number>());
    for (const e of edges) {
        neighbours[e.source]!.add(e.target);
        neighbours[e.target]!.add(e.source);
    }
    // Every label when there are few enough to read; otherwise a node's name shows once its circle is
    // large enough on screen, and a relationship's cardinality once the map is near enough.
    const ALL_LABELS = 80;
    const ALL_ENDS = 120;
    const FLIGHT_MS = 360;
    /** The zoom a flight to one entity ends at, unless nearer already: its name can be read. */
    const READ = 0.8;

    const keys = metricKeys(metrics);
    const metricKey = (m: WorldMetric): string => keys.get(m)!;
    const byKey = new Map([...keys].map(([m, key]) => [key, m]));

    const selected = $derived.by(() => {
        const focus = viewer.route.focus ?? '';
        if (focus.startsWith('entity:')) {
            const node = byName.get(focus.slice(7));
            return node === undefined ? null : { node, metric: null };
        }
        const metric = byKey.get(focus);
        if (!metric) return null;
        return { node: metric.entity === undefined ? -1 : (byName.get(metric.entity) ?? -1), metric };
    });
    const selectedNode = $derived(selected?.node ?? -1);

    let stage: HTMLElement | undefined = $state();
    let svg: SVGSVGElement | undefined = $state();
    let camera = $state<Camera>({ x: 0, y: 0, k: 1 });
    let size = $state<Size>({ w: 0, h: 0 });
    let cursor = $state(0);
    let hover = $state(-1);
    let announcement = $state('');
    let firstFrame = $state(0);
    let firstFrameAt = $state(0);
    let flight: { from: Camera; to: Camera; start: number } | null = null;

    // The labels change only when the zoom passes a step, not on every frame of a zoom.
    const zoomStep = $derived(Math.round(Math.log2(camera.k) * 6));
    // Nothing is labelled by zoom until the map is fitted: at the first frame's zoom of one, a large
    // map would lay out every label only for the fit to take them away.
    let fitted = $state(false);
    const labelled = $derived.by(() => {
        const k = 2 ** (zoomStep / 6);
        return nodes.map(
            (n, i) => nodes.length <= ALL_LABELS || i === selectedNode || (fitted && k * n.r >= 15),
        );
    });
    const endsShown = $derived(edges.length <= ALL_ENDS || (fitted && 2 ** (zoomStep / 6) >= 0.7));

    // The layout runs across; a stage taller than wide, as on a phone, turns it to run down.
    let portrait = $state(false);
    const X = (n: { x: number; y: number }): number => (portrait ? n.y : n.x);
    const Y = (n: { x: number; y: number }): number => (portrait ? n.x : n.y);
    const points = $derived(nodes.map(n => ({ x: X(n), y: Y(n) })));
    const bounds = $derived(
        portrait
            ? { x: world.bounds.y, y: world.bounds.x, w: world.bounds.h, h: world.bounds.w }
            : world.bounds,
    );

    // Where each edge meets its circles, and the screen offset of the cardinality written at each
    // end: further out where the edge leaves downwards, through the name written under the circle.
    const geometry = $derived(
        edges.map(e => {
            const a = { ...points[e.source]!, r: nodes[e.source]!.r };
            const b = { ...points[e.target]!, r: nodes[e.target]!.r };
            const d = Math.hypot(b.x - a.x, b.y - a.y) || 1;
            const ux = (b.x - a.x) / d;
            const uy = (b.y - a.y) / d;
            const out = (dy: number): number => (dy > 0.45 ? 46 : 18);
            return {
                x1: a.x + ux * a.r,
                y1: a.y + uy * a.r,
                x2: b.x - ux * b.r,
                y2: b.y - uy * b.r,
                o1: [ux * out(uy), uy * out(uy)] as const,
                o2: [-ux * out(-uy), -uy * out(-uy)] as const,
            };
        }),
    );

    // One path for each trust level: thousands of edges as a handful of elements, which the browser
    // lays out far faster. The edges of a selected entity are drawn again, on top.
    const r1 = (v: number): number => Math.round(v * 10) / 10;
    const edgePaths = $derived(
        TRUSTS.map(trust => ({
            trust,
            d: edges
                .map((e, i) => {
                    const g = geometry[i]!;
                    return e.trust === trust ? `M${r1(g.x1)} ${r1(g.y1)}L${r1(g.x2)} ${r1(g.y2)}` : '';
                })
                .join(''),
        })).filter(p => p.d),
    );
    const nearEdges = $derived(
        selectedNode < 0
            ? []
            : edges.flatMap((e, i) => (e.source === selectedNode || e.target === selectedNode ? [i] : [])),
    );

    // The block of entities in no relationship is labelled with how many it holds.
    const lonersAt = $derived(
        world.loners && world.lonerCount > 1
            ? portrait
                ? { x: world.loners.y, y: world.loners.x }
                : { x: world.loners.x, y: world.loners.y }
            : null,
    );

    const transform = $derived(
        `translate(${size.w / 2 - camera.x * camera.k} ${size.h / 2 - camera.y * camera.k}) scale(${camera.k})`,
    );

    function reducedMotion(): boolean {
        return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
    }

    function fly(to: Camera): void {
        if (reducedMotion()) {
            flight = null;
            camera = to;
            return;
        }
        flight = { from: { ...camera }, to, start: performance.now() };
        requestAnimationFrame(tick);
    }

    function tick(now: number): void {
        if (!flight) return;
        const p = (now - flight.start) / FLIGHT_MS;
        camera = between(flight.from, flight.to, p);
        if (p >= 1) flight = null;
        else requestAnimationFrame(tick);
    }

    /** The first view: the whole map, or near the entity the address selects. */
    function initialView(): void {
        portrait = size.h > size.w * 1.15;
        fitAll(false);
        fitted = true;
        const n = nodes[selectedNode];
        if (n) camera = { x: X(n), y: Y(n), k: Math.max(camera.k, READ) };
    }

    function fitAll(animate = true): void {
        const to = fit(bounds, size, 48, 1.4);
        if (animate) fly(to);
        else camera = to;
    }

    function zoomBy(factor: number): void {
        flight = null;
        camera = zoomAt(camera, size, size.w / 2, size.h / 2, factor);
    }

    /** Brings a node into view, at the zoom the map has, if it is not already. */
    function reveal(i: number): void {
        const n = nodes[i];
        if (!n) return;
        const box = { x: X(n) - n.r - 40, y: Y(n) - n.r - 40, w: 2 * n.r + 80, h: 2 * n.r + 80 };
        if (!inView(camera, size, box)) fly({ x: X(n), y: Y(n), k: Math.max(camera.k, READ) });
    }

    function select(i: number): void {
        const n = nodes[i];
        if (!n) return;
        viewer.go({ page: 'world', focus: `entity:${n.name}` });
    }

    function clear(): void {
        if (viewer.route.focus) viewer.go({ page: 'world' });
    }

    function nodeLabel(i: number): string {
        const n = nodes[i]!;
        return t('world.node', { name: n.name, trust: t(`trust.${n.trust}`), count: n.metrics.length });
    }

    // A selection made elsewhere (the panel, a link, the back button) is brought into view.
    $effect(() => {
        const i = selectedNode;
        if (i < 0) return;
        untrack(() => {
            cursor = i;
            if (size.w) reveal(i);
            announcement = nodeLabel(i);
        });
    });

    function focusNode(i: number): void {
        cursor = i;
        reveal(i);
        requestAnimationFrame(() => svg?.querySelector<SVGGElement>(`[data-node="${i}"]`)?.focus());
    }

    function onkeydown(event: KeyboardEvent, i: number): void {
        const key = event.key;
        const rtl = i18n.dir === 'rtl';
        const arrows: Record<string, [number, number]> = {
            ArrowRight: [rtl ? -1 : 1, 0],
            ArrowLeft: [rtl ? 1 : -1, 0],
            ArrowUp: [0, -1],
            ArrowDown: [0, 1],
        };
        const arrow = arrows[key];
        if (arrow) focusNode(stepFrom(points, i, arrow[0], arrow[1]));
        else if (key === 'Enter' || key === ' ') select(i);
        else if (key === 'Escape') clear();
        else if (key === '+' || key === '=') zoomBy(1.25);
        else if (key === '-') zoomBy(0.8);
        else if (key === 'f' || key === 'F' || key === '0') fitAll();
        else if (key === 'Home') focusNode(0);
        else return;
        event.preventDefault();
    }

    // Dragging moves the map; a press that does not move selects what is under it.
    let press: { x: number; y: number; camera: Camera; moved: boolean; id: number } | null = null;
    function onpointerdown(event: PointerEvent): void {
        if (event.button !== 0) return;
        flight = null;
        press = {
            x: event.clientX,
            y: event.clientY,
            camera: { ...camera },
            moved: false,
            id: event.pointerId,
        };
    }
    function onpointermove(event: PointerEvent): void {
        if (!press || press.id !== event.pointerId) {
            const target = (event.target as Element | null)?.closest('[data-node]');
            hover = target ? Number(target.getAttribute('data-node')) : -1;
            return;
        }
        const dx = event.clientX - press.x;
        const dy = event.clientY - press.y;
        if (!press.moved && Math.hypot(dx, dy) < 4) return;
        if (!press.moved) svg?.setPointerCapture(event.pointerId);
        press.moved = true;
        camera = {
            k: press.camera.k,
            x: press.camera.x - dx / press.camera.k,
            y: press.camera.y - dy / press.camera.k,
        };
    }
    function onpointerup(event: PointerEvent): void {
        if (!press || press.id !== event.pointerId) return;
        const moved = press.moved;
        press = null;
        if (moved) return;
        const target = (event.target as Element | null)?.closest('[data-node]');
        if (target) select(Number(target.getAttribute('data-node')));
        else clear();
    }
    function onwheel(event: WheelEvent): void {
        event.preventDefault();
        flight = null;
        const box = svg!.getBoundingClientRect();
        camera = zoomAt(
            camera,
            size,
            event.clientX - box.left,
            event.clientY - box.top,
            Math.exp(-event.deltaY * 0.0015),
        );
    }

    const hoverAt = $derived.by(() => {
        const n = nodes[hover];
        if (!n || labelled[hover]) return null;
        const [x, y] = toScreen(camera, size, X(n), Y(n) + n.r);
        return { x, y, text: n.name };
    });

    onMount(() => {
        if (!stage || !svg) return;
        size = { w: stage.clientWidth, h: stage.clientHeight };
        if (size.w && size.h) initialView();
        const observer = new ResizeObserver(() => {
            if (!stage) return;
            size = { w: stage.clientWidth, h: stage.clientHeight };
            if (!size.w || !size.h) return;
            if (!fitted || portrait !== size.h > size.w * 1.15) initialView();
        });
        observer.observe(stage);
        svg.addEventListener('wheel', onwheel, { passive: false });
        // The first frame: two animation frames after the map is in the page, the second once the
        // browser has painted the first.
        requestAnimationFrame(() =>
            requestAnimationFrame(() => {
                firstFrame = Math.round(performance.now() - began);
                firstFrameAt = Math.round(performance.now());
            }),
        );
        return () => {
            observer.disconnect();
            svg?.removeEventListener('wheel', onwheel);
            flight = null;
        };
    });
</script>

<article class="world-page">
    <header class="world-head">
        <h1 tabindex="-1">{t('page.world')}</h1>
        {#if !empty}
            <p class="lead">
                {t('world.lead', {
                    entities: nodes.length,
                    metrics: metrics.length,
                    relationships: edges.length,
                })}
            </p>
        {/if}
    </header>

    {#if empty}
        <p class="world-message">{t('world.empty')}</p>
    {:else}
        {#if nodes.length}
            <div class="world-tools">
                <ul class="world-legend" aria-label={t('world.legend')}>
                    {#each world.trusts as trust (trust)}
                        <li title={t(`trust.${trust}Help`)}>
                            <TrustMark {trust} />{t(`trust.${trust}`)}
                            <span class="sr-only">: {t(`trust.${trust}Help`)}</span>
                        </li>
                    {/each}
                </ul>
                <div class="world-zoom" role="group" aria-label={t('lineage.tools')}>
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
        {/if}

        <div class="world-body">
            <div
                class="world-stage"
                id="world-stage"
                bind:this={stage}
                data-state="ready"
                data-entities={nodes.length}
                data-metrics={metrics.length}
                data-layout-ms={Math.round(world.layoutMs)}
                data-layout-hash={layoutHash(nodes)}
                data-first-frame-ms={firstFrame || undefined}
                data-first-frame-at={firstFrameAt || undefined}
            >
                <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
                <svg
                    id="world"
                    bind:this={svg}
                    role="group"
                    aria-label={t('world.label', { count: nodes.length })}
                    aria-describedby="world-help"
                    class:selecting={selectedNode >= 0}
                    style:--inv={1 / camera.k}
                    {onpointerdown}
                    {onpointermove}
                    {onpointerup}
                    onpointercancel={() => (press = null)}
                    onpointerleave={() => (hover = -1)}
                >
                    <g {transform}>
                        <g class="edges" aria-hidden="true">
                            {#each edgePaths as path (path.trust)}
                                <path class="edge t-{path.trust}" d={path.d} />
                            {/each}
                            {#each nearEdges as i (i)}
                                {@const g = geometry[i]!}
                                <line
                                    class="edge near t-{edges[i]!.trust}"
                                    x1={g.x1}
                                    y1={g.y1}
                                    x2={g.x2}
                                    y2={g.y2}
                                />
                            {/each}
                        </g>
                        {#if endsShown}
                            <g class="ends" aria-hidden="true">
                                {#each edges as e, i (i)}
                                    {@const g = geometry[i]!}
                                    {#if e.ends}
                                        <g
                                            class="end"
                                            class:near={selectedNode >= 0 &&
                                                (e.source === selectedNode || e.target === selectedNode)}
                                        >
                                            <g transform="translate({g.x1} {g.y1})"
                                                ><g class="fixed"
                                                    ><g transform="translate({g.o1[0]} {g.o1[1]})"
                                                        ><rect
                                                            x="-10"
                                                            y="-11"
                                                            width="20"
                                                            height="22"
                                                            rx="6"
                                                        /><text>{e.ends[0]}</text></g
                                                    ></g
                                                ></g
                                            >
                                            <g transform="translate({g.x2} {g.y2})"
                                                ><g class="fixed"
                                                    ><g transform="translate({g.o2[0]} {g.o2[1]})"
                                                        ><rect
                                                            x="-10"
                                                            y="-11"
                                                            width="20"
                                                            height="22"
                                                            rx="6"
                                                        /><text>{e.ends[1]}</text></g
                                                    ></g
                                                ></g
                                            >
                                        </g>
                                    {/if}
                                {/each}
                            </g>
                        {/if}
                        {#if lonersAt}
                            <g class="loners" transform="translate({lonersAt.x} {lonersAt.y})"
                                ><g class="fixed"
                                    ><text y="-16">{t('world.loners', { count: world.lonerCount })}</text></g
                                ></g
                            >
                        {/if}
                        <g class="nodes">
                            {#each nodes as n, i (n.name)}
                                <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
                                <g
                                    class="node t-{n.trust}"
                                    class:selected={i === selectedNode}
                                    class:near={selectedNode >= 0 && neighbours[selectedNode]!.has(i)}
                                    transform="translate({X(n)} {Y(n)})"
                                    data-node={i}
                                    role="button"
                                    tabindex={i === cursor ? 0 : -1}
                                    aria-label={nodeLabel(i)}
                                    aria-pressed={i === selectedNode}
                                    onkeydown={event => onkeydown(event, i)}
                                    onfocus={() => (cursor = i)}
                                >
                                    {#if i === selectedNode || i === cursor}
                                        <circle class="halo" r={n.r + 6} />
                                    {/if}
                                    <circle class="disc" r={n.r} />
                                    {#if n.metrics.length && labelled[i]}
                                        <g class="fixed"
                                            ><text class="num">{i18n.number(n.metrics.length)}</text></g
                                        >
                                    {/if}
                                    {#if labelled[i] && (n.trust === 'verified' || n.trust === 'failed')}
                                        <g transform="translate({n.r * 0.72} {-n.r * 0.72})"
                                            ><g class="fixed mark">
                                                {#if n.trust === 'verified'}
                                                    <circle class="fill" r="9" />
                                                    <path class="cut" d="M-4 0.4l2.6 2.6 5-5.4" />
                                                {:else}
                                                    <path class="fill" d="M0 -10.5 10 7.5H-10z" />
                                                    <path class="cut" d="M0 -3.5v4.5M0 4v.1" />
                                                {/if}
                                            </g></g
                                        >
                                    {/if}
                                    {#if labelled[i]}
                                        <g transform="translate(0 {n.r})"
                                            ><g class="fixed"><text class="label" y="22">{n.name}</text></g
                                            ></g
                                        >
                                    {/if}
                                </g>
                            {/each}
                        </g>
                    </g>
                </svg>
                <p id="world-help" class="sr-only">{t('world.help')}</p>
                {#if !nodes.length}
                    <!-- Metrics with no entity are listed beside the map, which has nothing to draw. -->
                    <p class="world-message on-stage">{t('ontology.noEntities')}</p>
                {/if}
                <div class="sr-only" role="status" aria-live="polite">{announcement}</div>
                {#if hoverAt}
                    <p
                        class="hover-label"
                        aria-hidden="true"
                        style:left="{hoverAt.x}px"
                        style:top="{hoverAt.y + 8}px"
                    >
                        {hoverAt.text}
                    </p>
                {/if}
            </div>
            <WorldPanel
                {viewer}
                {world}
                selectedNode={selected ? selectedNode : -1}
                selectedMetric={selected?.metric ?? null}
                {metricKey}
                legacy={!viewer.world.metrics}
            />
        </div>
    {/if}
</article>

<style>
    .world-page {
        display: flex;
        flex-direction: column;
        block-size: 100%;
        min-block-size: 520px;
        padding: 18px 24px 20px;
        gap: 12px;
    }
    .world-head h1 {
        margin: 0;
        font-size: 28px;
        font-weight: 800;
        letter-spacing: -0.03em;
    }
    .world-head .lead {
        margin: 4px 0 0;
        max-inline-size: 90ch;
        color: var(--text-2);
    }
    .world-message {
        margin: 0;
        max-inline-size: 70ch;
        padding: 14px 16px;
        border: 1px solid var(--card-border);
        border-radius: var(--radius-card);
        background: var(--card);
    }
    .world-message.on-stage {
        position: absolute;
        inset-block-start: 16px;
        inset-inline-start: 16px;
        max-inline-size: calc(100% - 32px);
    }
    .world-tools {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 10px;
    }
    .world-legend {
        display: flex;
        flex-wrap: wrap;
        gap: 4px 10px;
        margin: 0;
        padding: 0;
        list-style: none;
    }
    /* A key, not a row of controls: no pill, so it is not mistaken for the Map page's filters. */
    .world-legend li {
        display: inline-flex;
        align-items: center;
        gap: 7px;
        min-block-size: 36px;
        padding-inline-end: 12px;
        color: var(--text-2);
        font-size: 14px;
        cursor: help;
    }
    .world-zoom {
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
    .icon-button:hover {
        border-color: var(--accent);
        color: var(--text);
    }
    .icon-button:focus-visible {
        outline: 2px solid var(--accent);
        outline-offset: 2px;
    }
    /* The stage comes first in the page, and so in the tab order, but the panel is drawn beside it. */
    .world-body {
        display: grid;
        grid-template-areas: 'panel stage';
        grid-template-columns: minmax(260px, 360px) 1fr;
        gap: 12px;
        flex: 1 1 auto;
        min-block-size: 0;
    }
    .world-stage {
        grid-area: stage;
        position: relative;
        min-block-size: 360px;
        overflow: hidden;
        border: 1px solid var(--card-border);
        border-radius: var(--radius-card);
        background: var(--ground);
    }
    #world {
        display: block;
        inline-size: 100%;
        block-size: 100%;
        touch-action: none;
        cursor: grab;
        user-select: none;
    }
    .edge {
        fill: none;
        stroke: var(--trust);
        stroke-width: 1.6;
        stroke-opacity: 0.75;
        vector-effect: non-scaling-stroke;
    }
    .edge.t-inferred {
        stroke-dasharray: 6 5;
    }
    /* Not rated is dotted and thinner, as its ring is thinner: not to be read as inferred. */
    .edge.t-unrated {
        stroke-width: 1.3;
        stroke-dasharray: 1 4;
        stroke-linecap: round;
    }
    .edge.t-failed {
        stroke-width: 2.4;
        stroke-opacity: 1;
    }
    .end rect {
        fill: var(--card);
        stroke: var(--card-border);
    }
    /* Text, counts, badges and cardinalities keep their size on screen while the map zooms. */
    .fixed {
        transform: scale(var(--inv, 1));
    }
    .end text {
        fill: var(--text);
        font: 700 14px var(--font-mono);
        text-anchor: middle;
        dominant-baseline: central;
    }
    .node {
        cursor: pointer;
        outline: none;
    }
    .node .halo {
        fill: none;
        stroke: transparent;
        stroke-width: 3;
        vector-effect: non-scaling-stroke;
    }
    .node.selected .halo,
    .node:focus-visible .halo {
        stroke: var(--accent);
    }
    .node:focus-visible .halo {
        stroke-dasharray: 4 3;
    }
    .node.selected:focus-visible .halo {
        stroke-dasharray: none;
    }
    .node .disc {
        fill: color-mix(in srgb, var(--trust) 16%, var(--card));
        stroke: var(--trust);
        stroke-width: 2.2;
        vector-effect: non-scaling-stroke;
    }
    .node:hover .disc {
        fill: color-mix(in srgb, var(--trust) 26%, var(--card));
    }
    .node.t-inferred .disc {
        stroke-dasharray: 6 4;
    }
    .node.t-unrated .disc {
        stroke-width: 1.4;
    }
    .num {
        fill: var(--text);
        font: 800 14px var(--font-sans);
        text-anchor: middle;
        dominant-baseline: central;
        pointer-events: none;
    }
    .mark .fill {
        fill: var(--trust);
        stroke: var(--ground);
        stroke-width: 2;
    }
    .mark .cut {
        fill: none;
        stroke: var(--ground);
        stroke-width: 2.2;
        stroke-linecap: round;
        stroke-linejoin: round;
    }
    .loners text {
        fill: var(--text-2);
        font: 700 15px var(--font-sans);
        paint-order: stroke;
        stroke: var(--ground);
        stroke-width: 4px;
        stroke-linejoin: round;
    }
    .label {
        fill: var(--text);
        font: 700 15px var(--font-sans);
        text-anchor: middle;
        paint-order: stroke;
        stroke: var(--ground);
        stroke-width: 4px;
        stroke-linejoin: round;
        pointer-events: none;
    }
    /* With an entity selected, the rest of the map steps back. */
    .selecting .node:not(.selected, .near),
    .selecting .edge:not(.near),
    .selecting .end:not(.near) {
        opacity: 0.32;
    }
    .selecting .edge.near {
        stroke-opacity: 1;
        stroke-width: 2.4;
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
        transform: translateX(-50%);
        pointer-events: none;
    }
    @media (prefers-reduced-motion: no-preference) {
        .node .disc,
        .node,
        .edge,
        .end {
            transition:
                opacity 180ms var(--ease),
                fill 180ms var(--ease);
        }
    }
    @media (max-width: 900px) {
        .world-page {
            block-size: auto;
        }
        .world-body {
            display: flex;
            flex-direction: column;
        }
        .world-stage {
            block-size: 62vh;
        }
        .world-zoom {
            margin-inline-start: 0;
        }
    }
</style>
