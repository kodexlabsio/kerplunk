<script lang="ts">
    import { i18n, t } from '../lib/i18n.svelte';
    import { kindName, transformName } from '../lib/labels';
    import type { Viewer } from '../lib/viewer.svelte';
    import Icon from './Icon.svelte';

    // The graph as text: the node under the keyboard cursor (or the highlighted one) with what is
    // upstream and downstream of it in this view, and every node in the view by layer.
    let { viewer, id, onmove }: { viewer: Viewer; id: string | null; onmove: (id: string) => void } =
        $props();
    // svelte-ignore state_referenced_locally
    const model = viewer.model;

    const n = $derived(id ? model.node(id) : undefined);
    const ups = $derived(id ? (model.upAdj.get(id) ?? []).filter(x => viewer.target.has(x)) : []);
    const downs = $derived(id ? (model.downAdj.get(id) ?? []).filter(x => viewer.target.has(x)) : []);
    const column = $derived(
        viewer.highlight && model.node(viewer.highlight)?.kind === 'column' ? viewer.highlight : null,
    );
    const steps = $derived(
        column ? { up: model.columnLineage(column, true), down: model.columnLineage(column, false) } : null,
    );
    let limit = $state(200);
</script>

{#snippet item(other: string)}
    <li>
        <a href={viewer.href({ page: 'node', id: other })}
            ><Icon kind={model.node(other)?.kind ?? ''} size={16} />{model.label(other)}</a
        >
        <button type="button" class="link small" onclick={() => onmove(other)}
            >{t('inspector.moveHere')}</button
        >
    </li>
{/snippet}

<aside id="inspector" class="inspector" aria-label={t('inspector.label')}>
    {#if n && id}
        <p class="eyebrow"><Icon kind={n.kind} size={16} />{kindName(n.kind)}</p>
        <h2>{model.label(id)}</h2>
        {#if n.description}<p class="note clamp">{n.description}</p>{/if}
        <p><a class="button small" href={viewer.href({ page: 'node', id })}>{t('inspector.open')}</a></p>
        <h3>{t('inspector.upstream')} <span class="count">{i18n.number(ups.length)}</span></h3>
        {#if ups.length}<ul class="plain-list">
                {#each ups as other (other)}{@render item(other)}{/each}
            </ul>{:else}<p class="note">{t('lineage.noUpstream')}</p>{/if}
        <h3>{t('inspector.downstream')} <span class="count">{i18n.number(downs.length)}</span></h3>
        {#if downs.length}<ul class="plain-list">
                {#each downs as other (other)}{@render item(other)}{/each}
            </ul>{:else}<p class="note">{t('lineage.noDownstream')}</p>{/if}
    {:else}
        <h2>{t('inspector.title')}</h2>
        <p class="note">{t('inspector.nothing')}</p>
    {/if}
    {#if steps && column}
        <h3>{t('inspector.columnPath', { column: model.label(column) })}</h3>
        <ol class="plain-list steps">
            {#each steps.up.slice().reverse() as s (s.id)}<li>
                    <a href={viewer.href({ page: 'node', id: s.id })}>{model.label(s.id)}</a><span
                        class="note">{transformName(s.transform)}</span
                    >
                </li>{/each}
            <li aria-current="true"><b>{model.label(column)}</b></li>
            {#each steps.down as s (s.id)}<li>
                    <a href={viewer.href({ page: 'node', id: s.id })}>{model.label(s.id)}</a><span
                        class="note">{transformName(s.transform)}</span
                    >
                </li>{/each}
        </ol>
    {/if}
    <details class="all-nodes">
        <summary>{t('inspector.all', { count: viewer.target.size })}</summary>
        <ol class="layers">
            {#each viewer.order as layer, i (i)}
                {#if limit > 0}
                    <li>
                        <span class="note">{t('inspector.layer', { n: i + 1 })}</span>
                        <ul class="plain-list">
                            {#each layer.slice(0, limit) as other (other)}{@render item(other)}{/each}
                        </ul>
                    </li>
                {/if}
            {/each}
        </ol>
        {#if viewer.target.size > limit}<button type="button" class="link" onclick={() => (limit = Infinity)}
                >{t('node.showAll', { count: viewer.target.size })}</button
            >{/if}
    </details>
</aside>
