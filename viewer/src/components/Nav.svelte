<script lang="ts">
    import { untrack } from 'svelte';
    import { i18n, t } from '../lib/i18n.svelte';
    import type { MessageKey } from '../locales/en-GB';
    import { buildTree, flatten, pathTo, type TreeRow, type TreeTab } from '../lib/tree';
    import { scrollFor, windowOf } from '../lib/virtual';
    import type { Viewer } from '../lib/viewer.svelte';
    import Icon from './Icon.svelte';

    // The viewer is made once and never replaced.
    const props: { viewer: Viewer } = $props();
    const viewer = untrack(() => props.viewer);
    // svelte-ignore state_referenced_locally
    const model = viewer.model;

    const ROW_HEIGHT = 32;

    const conflicts = viewer.conflicts.conflicts?.length ?? 0;
    const warnings = viewer.data.warnings?.length ?? 0;
    const tests = model.of('test').length;
    const terms = model.of('term').length;
    const usage = !!viewer.data.meta?.usage;
    const ontology = (viewer.ontology.entities?.length ?? 0) + (viewer.ontology.measures?.length ?? 0) > 0;

    const pages: { page: string; key: MessageKey; icon: string; count?: number; show: boolean }[] = [
        { page: 'overview', key: 'page.overview', icon: 'overview', show: true },
        { page: 'lineage', key: 'page.lineage', icon: 'lineage', show: true },
        { page: 'map', key: 'page.map', icon: 'map', show: true },
        { page: 'browse', key: 'page.browse', icon: 'list', show: true },
        { page: 'glossary', key: 'page.glossary', icon: 'term', count: terms, show: terms > 0 },
        { page: 'ontology', key: 'page.ontology', icon: 'entity', show: ontology },
        { page: 'conflicts', key: 'page.conflicts', icon: 'conflicts', count: conflicts, show: true },
        { page: 'tests', key: 'page.tests', icon: 'test', count: tests, show: tests > 0 },
        { page: 'usage', key: 'page.usage', icon: 'usage', show: usage },
        { page: 'warnings', key: 'page.warnings', icon: 'warnings', count: warnings, show: true },
    ];

    const tabKeys: Record<TreeTab, MessageKey> = {
        project: 'nav.project',
        database: 'nav.database',
        pipelines: 'nav.pipelines',
        business: 'nav.business',
    };
    let tab = $state<TreeTab>('project');
    let filter = $state('');
    const expanded = $state(new Set<string>());
    let expandedVersion = $state(0);

    const trees = $derived.by(() => {
        void i18n.locale;
        const labels = {
            sources: t('nav.sources'),
            exposures: t('nav.exposures'),
            project: t('nav.thisProject'),
            noSchema: t('nav.noSchema'),
            terms: t('nav.terms'),
            entities: t('nav.entities'),
            tests: t('page.tests'),
        };
        const all = {} as Record<TreeTab, ReturnType<typeof buildTree>>;
        for (const key of Object.keys(tabKeys) as TreeTab[])
            all[key] = buildTree(model, key, labels, i18n.compare, viewer.ontology);
        return all;
    });
    const tabs = $derived((Object.keys(tabKeys) as TreeTab[]).filter(k => trees[k].length > 0));

    const rows: TreeRow[] = $derived.by(() => {
        void expandedVersion;
        return flatten(trees[tab] ?? [], expanded, filter);
    });

    // Open the branches that lead to the current page, so the tree shows where you are.
    const current = $derived(
        viewer.route.page === 'node'
            ? (viewer.route.id ?? '')
            : viewer.route.page === 'entity'
              ? `entity:${viewer.route.id}`
              : '',
    );
    $effect(() => {
        const key = current;
        if (!key) return;
        untrack(() => {
            for (const name of tabs) {
                const path = pathTo(trees[name], key);
                if (!path) continue;
                if (name !== tab && !pathTo(trees[tab] ?? [], key)) tab = name;
                for (const k of path) expanded.add(k);
                expandedVersion++;
                requestAnimationFrame(() => reveal(rows.findIndex(r => r.item.key === key)));
                break;
            }
        });
    });

    let scroller: HTMLElement | undefined = $state();
    let scrollTop = $state(0);
    let height = $state(400);
    const win = $derived(windowOf(rows.length, ROW_HEIGHT, scrollTop, height));
    let active = $state(0);

    function reveal(index: number): void {
        if (index < 0 || !scroller) return;
        active = index;
        const top = scrollFor(index, ROW_HEIGHT, scroller.scrollTop, scroller.clientHeight);
        if (top !== null) scroller.scrollTop = top;
    }

    function toggle(key: string, open?: boolean): void {
        const now = expanded.has(key);
        if (open === undefined ? now : !open) expanded.delete(key);
        else expanded.add(key);
        expandedVersion++;
    }

    function focusRow(index: number): void {
        reveal(index);
        requestAnimationFrame(() => scroller?.querySelector<HTMLElement>(`[data-row="${index}"]`)?.focus());
    }

    function activate(row: TreeRow): void {
        if (row.item.href) {
            location.hash = row.item.href.slice(1);
            viewer.navOpen = false;
        } else if (row.item.children) toggle(row.item.key);
    }

    function onkeydown(event: KeyboardEvent, index: number): void {
        const row = rows[index];
        if (!row) return;
        const key = event.key;
        // Arrow keys follow the reading direction in right-to-left scripts.
        const rtl = i18n.dir === 'rtl';
        const right = rtl ? 'ArrowLeft' : 'ArrowRight';
        const left = rtl ? 'ArrowRight' : 'ArrowLeft';
        if (key === 'ArrowDown') focusRow(Math.min(rows.length - 1, index + 1));
        else if (key === 'ArrowUp') focusRow(Math.max(0, index - 1));
        else if (key === 'Home') focusRow(0);
        else if (key === 'End') focusRow(rows.length - 1);
        else if (key === right) {
            if (row.item.children && !row.expanded) toggle(row.item.key, true);
            else if (row.item.children) focusRow(index + 1);
        } else if (key === left) {
            if (row.item.children && row.expanded) toggle(row.item.key, false);
            else if (row.parent) focusRow(rows.findIndex(r => r.item.key === row.parent));
        } else if (key === 'Enter' || key === ' ') activate(row);
        else return;
        event.preventDefault();
    }

    function isCurrent(page: string): boolean {
        return viewer.route.page === page;
    }
</script>

<nav id="nav" class:open={viewer.navOpen} aria-label={t('nav.label')}>
    <ul class="pages">
        {#each pages.filter(p => p.show) as p (p.page)}
            <li>
                <a
                    href={p.page === 'overview' ? '#/' : `#/${p.page}`}
                    aria-current={isCurrent(p.page) ? 'page' : undefined}
                    onclick={() => (viewer.navOpen = false)}
                >
                    <Icon kind={p.icon} />
                    <span class="grow">{t(p.key)}</span>
                    {#if p.count}<span class="count">{i18n.number(p.count)}</span>{/if}
                </a>
            </li>
        {/each}
    </ul>
    {#if tabs.length}
        <div class="tabs" role="tablist" aria-label={t('nav.trees')}>
            {#each tabs as name (name)}
                <button
                    type="button"
                    role="tab"
                    id="tab-{name}"
                    aria-selected={tab === name}
                    aria-controls="tree"
                    tabindex={tab === name ? 0 : -1}
                    onclick={() => {
                        tab = name;
                        active = 0;
                    }}
                    onkeydown={event => {
                        const i = tabs.indexOf(name);
                        const next = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
                        if (!next) return;
                        event.preventDefault();
                        const target =
                            tabs[(i + (i18n.dir === 'rtl' ? -next : next) + tabs.length) % tabs.length]!;
                        tab = target;
                        document.getElementById(`tab-${target}`)?.focus();
                    }}>{t(tabKeys[name])}</button
                >
            {/each}
        </div>
        <div class="tree-filter">
            <label class="sr-only" for="tree-filter">{t('nav.filter')}</label>
            <Icon kind="filter" />
            <input
                id="tree-filter"
                type="search"
                placeholder={t('nav.filter')}
                autocomplete="off"
                spellcheck="false"
                bind:value={filter}
            />
        </div>
        <div
            id="tree"
            class="tree"
            role="tree"
            aria-labelledby="tab-{tab}"
            bind:this={scroller}
            bind:clientHeight={height}
            onscroll={() => (scrollTop = scroller?.scrollTop ?? 0)}
        >
            <div style="height: {rows.length * ROW_HEIGHT}px; position: relative">
                {#each rows.slice(win.start, win.end) as row, i (row.item.key)}
                    {@const index = win.start + i}
                    <div
                        class="treeitem"
                        role="treeitem"
                        data-row={index}
                        data-key={row.item.key}
                        tabindex={index === Math.min(active, rows.length - 1) ? 0 : -1}
                        aria-level={row.depth + 1}
                        aria-setsize={row.siblings}
                        aria-posinset={row.index + 1}
                        aria-expanded={row.item.children ? row.expanded : undefined}
                        aria-selected={row.item.key === current}
                        aria-current={row.item.key === current ? 'page' : undefined}
                        style="top: {index * ROW_HEIGHT}px; padding-inline-start: {10 + row.depth * 16}px"
                        onclick={() => {
                            active = index;
                            if (row.item.children && (!row.item.href || row.expanded === false))
                                toggle(row.item.key);
                            if (row.item.href) activate(row);
                        }}
                        onkeydown={event => onkeydown(event, index)}
                        onfocus={() => (active = index)}
                    >
                        <span class="twisty" class:open={row.expanded} aria-hidden="true">
                            {#if row.item.children}<Icon kind="chevron" size={14} />{/if}
                        </span>
                        <Icon kind={row.item.icon} size={16} />
                        <span class="label">{row.item.label}</span>
                        {#if row.item.children}<span class="count"
                                >{i18n.number(row.item.children.length)}</span
                            >{/if}
                    </div>
                {/each}
            </div>
            {#if !rows.length}<p class="note empty-tree">{t('nav.nothing')}</p>{/if}
        </div>
    {/if}
    <p class="built">
        {#if viewer.data.meta?.generated_at}
            <span title={i18n.date(String(viewer.data.meta.generated_at), 'long')}
                >{t('app.built', { when: i18n.relative(String(viewer.data.meta.generated_at)) })}</span
            >
        {/if}
        {#if viewer.data.meta?.konstruct_version}<span
                >Konstruct {String(viewer.data.meta.konstruct_version)}</span
            >{/if}
    </p>
</nav>
{#if viewer.navOpen}<button
        type="button"
        class="scrim"
        aria-label={t('nav.close')}
        onclick={() => (viewer.navOpen = false)}
    ></button>{/if}
