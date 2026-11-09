<script lang="ts">
    import { i18n, t } from '../../lib/i18n.svelte';
    import { kindName } from '../../lib/labels';
    import { attr, display, strings } from '../../lib/model';
    import type { Owner, Usage } from '../../lib/types';
    import type { Viewer } from '../../lib/viewer.svelte';
    import Icon from '../Icon.svelte';
    import NodeLink from '../NodeLink.svelte';
    import ColumnBody from './ColumnBody.svelte';
    import OtherBody from './OtherBody.svelte';
    import RelationBody from './RelationBody.svelte';
    import TermBody from './TermBody.svelte';

    let { viewer, id }: { viewer: Viewer; id: string } = $props();
    // svelte-ignore state_referenced_locally
    const model = viewer.model;
    // svelte-ignore state_referenced_locally
    const n = model.node(id)!;
    const parent = n.parent ? model.node(n.parent) : undefined;
    const relational = ['source', 'seed', 'snapshot', 'table', 'model'].includes(n.kind);

    function owner(): string {
        const o = attr<Owner | string>(n, 'owner');
        if (!o) return '';
        if (typeof o === 'string') return o;
        const extra = o.role ?? (o as unknown as Record<string, unknown>).email;
        return extra ? `${o.name}, ${String(extra)}` : o.name;
    }

    const verified = (() => {
        const edges = (relational ? model.columnsOf(id) : n.kind === 'column' ? [n] : []).flatMap(c =>
            model.outs(c.id, 'derived_from'),
        );
        if (!edges.some(e => attr(e, 'verified'))) return null;
        return { done: edges.filter(e => attr(e, 'verified') === 'confirmed').length, total: edges.length };
    })();

    const usage = attr<Usage>(n, 'usage');
    const tags = model.tagsOf(n);
    const entities = strings(attr(n, 'entities'));
    const pkg = model.packageOf(n);

    let copied = $state(false);
    function copyLink(): void {
        const url = location.href;
        if (navigator.clipboard && window.isSecureContext)
            void navigator.clipboard.writeText(url).then(() => {
                copied = true;
                setTimeout(() => (copied = false), 1400);
            });
    }

    const hasLineage = $derived(viewer.hasLineage);
</script>

<article class="page node-page k-{n.kind}">
    <header class="page-head">
        <p class="eyebrow">
            <Icon kind={n.kind} size={16} />
            {#if n.kind === 'column' && parent}
                {t('node.columnOf', { kind: kindName(parent.kind) })}
                <NodeLink {viewer} id={parent.id} icon={false} />
            {:else}
                {kindName(n.kind)}
            {/if}
        </p>
        <h1 tabindex="-1">{n.kind === 'column' ? n.name : n.name}</h1>
        {#if n.relation}<p class="relation mono">{n.relation}</p>{/if}
        {#if n.kind === 'column' && n.data_type}<p class="relation mono">{n.data_type.toLowerCase()}</p>{/if}
        <ul class="badges" aria-label={t('node.facts')}>
            {#if attr(n, 'materialized')}<li class="badge">{display(attr(n, 'materialized'))}</li>{/if}
            {#if pkg && n.kind !== 'column' && n.kind !== 'term'}<li>
                    <a class="badge" href={viewer.href({ page: 'browse', package: pkg })}
                        ><Icon kind="package" size={14} />{pkg}</a
                    >
                </li>{/if}
            {#each tags as tag (tag)}<li>
                    <a class="badge tag" href={viewer.href({ page: 'browse', tag })}>#{tag}</a>
                </li>{/each}
            {#each entities as e (e)}<li>
                    <a class="badge" href={viewer.href({ page: 'entity', id: e })}
                        ><Icon kind="entity" size={14} />{e}</a
                    >
                </li>{/each}
            {#if n.kind === 'term'}<li class="badge status-{viewer.status(n)}">
                    {t(`status.${viewer.status(n)}` as never)}
                </li>{/if}
            {#if owner()}<li class="badge"><Icon kind="entity" size={14} />{owner()}</li>{/if}
            {#if verified}
                <li class="badge" class:ok={verified.done === verified.total}>
                    <Icon kind="check" size={14} />{t('node.verified', {
                        done: verified.done,
                        total: verified.total,
                    })}
                </li>
            {/if}
            {#if usage}<li class="badge">
                    <Icon kind="usage" size={14} />{t('node.usage', {
                        queries: usage.queries,
                        users: usage.users,
                        days: usage.days,
                    })}
                </li>{/if}
            {#if attr(n, 'in_warehouse') === false}<li class="badge warn">{t('node.notInWarehouse')}</li>{/if}
            {#each n.origins ?? [] as origin (origin)}<li class="badge quiet">{origin}</li>{/each}
        </ul>
        {#if n.description}<p class="description">{n.description}</p>{:else if n.kind !== 'test'}<p
                class="description none"
            >
                {t('node.noDescription')}
            </p>{/if}
        <div class="actions">
            {#if hasLineage}
                <button
                    type="button"
                    class="button"
                    id="toggle-dock"
                    aria-pressed={viewer.dockOpen}
                    aria-controls={viewer.dockOpen ? 'dock' : undefined}
                    aria-keyshortcuts="L"
                    onclick={() => viewer.toggleDock()}
                    ><Icon kind="dock" />{viewer.dockOpen ? t('dock.hide') : t('dock.show')}</button
                >
                <a
                    class="button"
                    href={viewer.href({
                        page: 'lineage',
                        focus: id,
                        select: viewer.selectorText || undefined,
                    })}><Icon kind="expand" />{t('lineage.fullScreen')}</a
                >
            {/if}
            {#if typeof navigator !== 'undefined' && navigator.clipboard && window.isSecureContext}
                <button type="button" class="button" onclick={copyLink}
                    ><Icon kind="link" /><span aria-live="polite"
                        >{copied ? t('code.copied') : t('node.copyLink')}</span
                    ></button
                >
            {/if}
        </div>
    </header>

    {#if relational}
        <RelationBody {viewer} {n} />
    {:else if n.kind === 'column'}
        <ColumnBody {viewer} {n} />
    {:else if n.kind === 'term'}
        <TermBody {viewer} {n} />
    {:else}
        <OtherBody {viewer} {n} />
    {/if}
    <p class="note id-line">{t('node.id')} <code>{n.id}</code></p>
</article>
