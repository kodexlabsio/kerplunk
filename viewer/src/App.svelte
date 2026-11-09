<script lang="ts">
    import { onMount, untrack } from 'svelte';
    import Help from './components/Help.svelte';
    import Lineage from './components/Lineage.svelte';
    import Nav from './components/Nav.svelte';
    import Page from './components/Page.svelte';
    import Palette from './components/Palette.svelte';
    import TopBar from './components/TopBar.svelte';
    import { i18n, t } from './lib/i18n.svelte';
    import type { Viewer } from './lib/viewer.svelte';

    // The viewer is made once and never replaced.
    const props: { viewer: Viewer } = $props();
    const viewer = untrack(() => props.viewer);

    const prefersLight = window.matchMedia?.('(prefers-color-scheme: light)').matches;
    // svelte-ignore state_referenced_locally
    viewer.setTheme(viewer.storedTheme() ?? (prefersLight ? 'light' : 'dark'), false);
    // svelte-ignore state_referenced_locally
    viewer.readRoute(location.hash);

    $effect(() => {
        document.documentElement.setAttribute('data-theme', viewer.theme);
    });
    $effect(() => {
        document.documentElement.lang = i18n.locale;
        document.documentElement.dir = i18n.dir;
    });

    let main: HTMLElement | undefined = $state();
    let split: HTMLElement | undefined = $state();

    // The page title says where you are, for history, bookmarks and screen readers.
    $effect(() => {
        const project = viewer.projectName() || t('app.graph');
        const r = viewer.route;
        const subject =
            r.page === 'node' && r.id ? viewer.model.label(r.id) : r.page === 'entity' ? r.id : null;
        const page = subject ?? t(`page.${r.page}`);
        document.title =
            r.page === 'overview' ? t('app.title', { project }) : t('app.titleOf', { page, project });
    });

    // Moving to another page moves focus to its heading, as a screen reader expects of a new page.
    let first = true;
    $effect(() => {
        void viewer.route.page;
        void viewer.route.id;
        if (first) {
            first = false;
            return;
        }
        requestAnimationFrame(() => {
            const page = document.getElementById('page');
            if (page) page.scrollTop = 0;
            const heading = document.querySelector<HTMLElement>('#page h1');
            if (heading && !document.activeElement?.closest('#graph-box, #nav, #palette'))
                heading.focus({ preventScroll: true });
        });
    });

    onMount(() => {
        if (viewer.carriedScroll) {
            const page = document.getElementById('page');
            if (page) page.scrollTop = viewer.carriedScroll;
        } else if (viewer.lineageShown) viewer.request('fit');
    });

    function typing(event: KeyboardEvent): boolean {
        const el = event.target as HTMLElement | null;
        return (
            !!el &&
            (el.tagName === 'INPUT' ||
                el.tagName === 'TEXTAREA' ||
                el.tagName === 'SELECT' ||
                el.isContentEditable)
        );
    }

    function onkeydown(event: KeyboardEvent): void {
        const key = event.key;
        if ((event.ctrlKey || event.metaKey) && (key === 'k' || key === 'K')) {
            event.preventDefault();
            viewer.paletteOpen = !viewer.paletteOpen;
            return;
        }
        if (typing(event) || event.ctrlKey || event.metaKey || event.altKey) return;
        if (viewer.paletteOpen || viewer.helpOpen) return;
        if (key === '/') {
            event.preventDefault();
            viewer.paletteOpen = true;
        } else if (key === '?') viewer.helpOpen = true;
        else if (key === 'l' || key === 'L') {
            if (event.shiftKey || viewer.route.page === 'lineage') viewer.toggleFullScreen();
            else viewer.toggleDock();
        } else if (key === 'Escape' && viewer.route.page === 'lineage') viewer.toggleFullScreen();
    }

    // The splitter between the page and the docked graph.
    let dragging = false;
    function startResize(event: PointerEvent): void {
        dragging = true;
        split?.setPointerCapture(event.pointerId);
    }
    function resize(event: PointerEvent): void {
        if (!dragging || !main) return;
        const box = main.getBoundingClientRect();
        viewer.resizeDock(((box.bottom - event.clientY) / box.height) * 100);
    }
    function endResize(): void {
        if (!dragging) return;
        dragging = false;
        viewer.request('fit');
    }
    function resizeKey(event: KeyboardEvent): void {
        const step = event.shiftKey ? 10 : 4;
        if (event.key === 'ArrowUp') viewer.resizeDock(viewer.dockSize + step);
        else if (event.key === 'ArrowDown') viewer.resizeDock(viewer.dockSize - step);
        else if (event.key === 'Home') viewer.resizeDock(80);
        else if (event.key === 'End') viewer.resizeDock(20);
        else return;
        event.preventDefault();
    }

    function skip(): void {
        const heading = document.querySelector<HTMLElement>('#page h1') ?? main;
        heading?.focus();
    }
</script>

<svelte:window {onkeydown} onhashchange={() => viewer.readRoute(location.hash)} />

<div
    id="app"
    class:full={viewer.route.page === 'lineage'}
    class:docked={viewer.lineageShown && viewer.route.page !== 'lineage'}
>
    <button type="button" class="skip" onclick={skip}>{t('app.skip')}</button>
    <TopBar {viewer} />
    <Nav {viewer} />
    <main id="main" bind:this={main} tabindex="-1" style="--dock: {viewer.dockSize}%">
        {#if viewer.route.page !== 'lineage'}
            <div id="page">
                <Page {viewer} />
            </div>
        {/if}
        {#if viewer.lineageShown}
            {#if viewer.route.page !== 'lineage'}
                <!-- A focusable separator is the ARIA window splitter, which the arrow keys resize. -->
                <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
                <div
                    id="splitter"
                    role="separator"
                    tabindex="0"
                    aria-orientation="horizontal"
                    aria-label={t('dock.resize')}
                    aria-valuemin={20}
                    aria-valuemax={80}
                    aria-valuenow={Math.round(viewer.dockSize)}
                    aria-controls="dock"
                    bind:this={split}
                    onpointerdown={startResize}
                    onpointermove={resize}
                    onpointerup={endResize}
                    onpointercancel={endResize}
                    onkeydown={resizeKey}
                ></div>
            {/if}
            <section id="dock" aria-label={t('lineage.region')}>
                <Lineage {viewer} />
            </section>
        {/if}
    </main>
    {#if viewer.paletteOpen}<Palette {viewer} />{/if}
    {#if viewer.helpOpen}<Help {viewer} />{/if}
</div>
