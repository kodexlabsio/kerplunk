<script lang="ts">
    import { plainSummary } from '../../lib/markdown';
    import { untrack } from 'svelte';
    import { i18n, t } from '../../lib/i18n.svelte';
    import { kindName } from '../../lib/labels';
    import { attr, display, VIEW_KINDS } from '../../lib/model';
    import type { MessageKey } from '../../locales/en-GB';
    import { windowOf } from '../../lib/virtual';
    import type { Viewer } from '../../lib/viewer.svelte';
    import Icon from '../Icon.svelte';

    // The viewer is made once and never replaced.
    const props: { viewer: Viewer } = $props();
    const viewer = untrack(() => props.viewer);
    // svelte-ignore state_referenced_locally
    const model = viewer.model;
    const ROW = 42;

    interface Row {
        id: string;
        name: string;
        kind: string;
        pkg: string;
        materialized: string;
        columns: number;
        tests: number;
        up: number;
        down: number;
        description: string;
        tags: string[];
    }

    const all: Row[] = model.viewNodes.map(n => ({
        id: n.id,
        name: model.label(n.id),
        kind: n.kind,
        pkg: model.packageOf(n) ?? '',
        materialized: display(attr(n, 'materialized')),
        columns: model.columnsOf(n.id).length,
        tests: model.testsOf(n.id).length,
        up: model.upAdj.get(n.id)?.length ?? 0,
        down: model.downAdj.get(n.id)?.length ?? 0,
        description: plainSummary(n.description ?? ''),
        tags: model.tagsOf(n),
    }));
    const kinds = VIEW_KINDS.filter(k => all.some(r => r.kind === k));
    const packages = [...new Set(all.map(r => r.pkg).filter(Boolean))].sort(i18n.compare);
    const tags = [...new Set(all.flatMap(r => r.tags))].sort(i18n.compare);

    const r = $derived(viewer.route);
    let query = $state('');
    $effect(() => {
        query = viewer.route.q ?? '';
    });

    type Key = 'name' | 'kind' | 'pkg' | 'materialized' | 'columns' | 'tests' | 'up' | 'down';
    let sortKey = $state<Key>('name');
    let ascending = $state(true);

    const rows = $derived.by(() => {
        const q = query.trim().toLowerCase();
        const list = all.filter(
            row =>
                (!r.kind || row.kind === r.kind) &&
                (!r.package || row.pkg === r.package) &&
                (!r.tag || row.tags.includes(r.tag)) &&
                (!q || row.name.toLowerCase().includes(q) || row.description.toLowerCase().includes(q)),
        );
        const key = sortKey;
        const sign = ascending ? 1 : -1;
        return list.sort((a, b) => {
            const x = a[key];
            const y = b[key];
            const c =
                typeof x === 'number' && typeof y === 'number' ? x - y : i18n.compare(String(x), String(y));
            return sign * c || i18n.compare(a.name, b.name);
        });
    });

    function filter(key: 'kind' | 'package' | 'tag', value: string): void {
        viewer.go({ ...viewer.route, [key]: value || undefined }, true);
    }

    function sort(key: Key): void {
        if (sortKey === key) ascending = !ascending;
        else {
            sortKey = key;
            ascending = key === 'name' || key === 'kind' || key === 'pkg' || key === 'materialized';
        }
    }

    let scroller: HTMLElement | undefined = $state();
    let scrollTop = $state(0);
    let height = $state(600);
    const win = $derived(windowOf(rows.length, ROW, scrollTop, height, 12));

    const headers: [Key, MessageKey, boolean][] = [
        ['name', 'table.name', false],
        ['kind', 'table.kind', false],
        ['pkg', 'table.package', false],
        ['materialized', 'table.materialized', false],
        ['columns', 'table.columns', true],
        ['tests', 'table.tests', true],
        ['up', 'table.upstream', true],
        ['down', 'table.downstream', true],
    ];
</script>

<article class="page browse">
    <header class="page-head">
        <h1 tabindex="-1">{r.kind ? kindName(r.kind) : t('page.browse')}</h1>
        <p class="lead">{t('browse.lead')}</p>
    </header>
    <div class="filters" role="group" aria-label={t('browse.filters')}>
        <label>
            <span>{t('table.kind')}</span>
            <select
                id="filter-kind"
                value={r.kind ?? ''}
                onchange={e => filter('kind', (e.target as HTMLSelectElement).value)}
            >
                <option value="">{t('browse.anyKind')}</option>
                {#each kinds as k (k)}<option value={k}>{kindName(k)}</option>{/each}
            </select>
        </label>
        {#if packages.length > 1}
            <label>
                <span>{t('table.package')}</span>
                <select
                    id="filter-package"
                    value={r.package ?? ''}
                    onchange={e => filter('package', (e.target as HTMLSelectElement).value)}
                >
                    <option value="">{t('browse.anyPackage')}</option>
                    {#each packages as p (p)}<option value={p}>{p}</option>{/each}
                </select>
            </label>
        {/if}
        {#if tags.length}
            <label>
                <span>{t('browse.tag')}</span>
                <select
                    id="filter-tag"
                    value={r.tag ?? ''}
                    onchange={e => filter('tag', (e.target as HTMLSelectElement).value)}
                >
                    <option value="">{t('browse.anyTag')}</option>
                    {#each tags as tag (tag)}<option value={tag}>{tag}</option>{/each}
                </select>
            </label>
        {/if}
        <label class="grow">
            <span>{t('browse.search')}</span>
            <input
                id="browse-q"
                type="search"
                bind:value={query}
                placeholder={t('browse.searchPlaceholder')}
            />
        </label>
    </div>
    <p class="note" role="status">
        {t('browse.count', { count: rows.length, total: all.length })}
        {#if r.kind || r.tag || r.package}<a
                href={viewer.href({
                    page: 'lineage',
                    select: [
                        r.kind ? `kind:${r.kind}` : '',
                        r.tag ? `tag:${r.tag}` : '',
                        r.package ? `package:${r.package}` : '',
                    ]
                        .filter(Boolean)
                        .join(','),
                })}>{t('browse.showLineage')}</a
            >{/if}
    </p>
    <div
        class="table-wrap virtual"
        bind:this={scroller}
        bind:clientHeight={height}
        onscroll={() => (scrollTop = scroller?.scrollTop ?? 0)}
        role="region"
        aria-label={t('page.browse')}
        tabindex="-1"
    >
        <table class="data browse-table" aria-rowcount={rows.length + 1}>
            <thead>
                <tr aria-rowindex={1}>
                    {#each headers as [key, label, numeric] (key)}
                        <th
                            scope="col"
                            class:num={numeric}
                            aria-sort={sortKey === key ? (ascending ? 'ascending' : 'descending') : 'none'}
                        >
                            <button type="button" class="sort" onclick={() => sort(key)}
                                >{t(label)}<span aria-hidden="true"
                                    >{sortKey === key ? (ascending ? ' ↑' : ' ↓') : ''}</span
                                ></button
                            >
                        </th>
                    {/each}
                    <th scope="col">{t('table.description')}</th>
                </tr>
            </thead>
            <tbody>
                {#if win.before}<tr class="spacer" aria-hidden="true" style="height: {win.before}px"
                        ><td colspan="9"></td></tr
                    >{/if}
                {#each rows.slice(win.start, win.end) as row, i (row.id)}
                    <tr aria-rowindex={win.start + i + 2} style="height: {ROW}px">
                        <th scope="row"
                            ><a class="node-link" href={viewer.href({ page: 'node', id: row.id })}
                                ><Icon kind={row.kind} size={16} /><span>{row.name}</span></a
                            ></th
                        >
                        <td>{kindName(row.kind)}</td>
                        <td>{row.pkg}</td>
                        <td>{row.materialized}</td>
                        <td class="num">{i18n.number(row.columns)}</td>
                        <td class="num">{i18n.number(row.tests)}</td>
                        <td class="num">{i18n.number(row.up)}</td>
                        <td class="num">{i18n.number(row.down)}</td>
                        <td class="desc clip" title={row.description}>{row.description}</td>
                    </tr>
                {/each}
                {#if win.after}<tr class="spacer" aria-hidden="true" style="height: {win.after}px"
                        ><td colspan="9"></td></tr
                    >{/if}
            </tbody>
        </table>
    </div>
</article>
