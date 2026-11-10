<script lang="ts">
    import { untrack } from 'svelte';
    import { i18n, t } from '../../lib/i18n.svelte';
    import type { ConflictSide } from '../../lib/types';
    import type { Viewer } from '../../lib/viewer.svelte';
    import NodeLink from '../NodeLink.svelte';

    // The viewer is made once and never replaced.
    const props: { viewer: Viewer } = $props();
    const viewer = untrack(() => props.viewer);
    // svelte-ignore state_referenced_locally
    const data = viewer.conflicts;
    const conflicts = data.conflicts ?? [];

    function reach(side: ConflictSide): string {
        const r = side.reach ?? {};
        return t('conflicts.reach', {
            exposures: r.exposures ?? 0,
            tasks: r.tasks ?? 0,
            columns: r.columns ?? 0,
        });
    }
</script>

{#snippet sideOf(side: ConflictSide, name: string)}
    <div class="side">
        <h3><span class="badge">{name}</span> <NodeLink {viewer} id={side.column} /></h3>
        <pre class="formula" dir="ltr"><span class="eq">= </span>{side.composed ??
                side.expression ??
                t('conflicts.noFormula')}</pre>
        <p class="note">{side.path ?? ''} &#x00B7; {reach(side)}</p>
        {#if side.filters?.length}
            <p class="note">{t('conflicts.filters')}</p>
            <div class="conditions">
                {#each side.filters as f, i (i)}<code dir="ltr">{f.relation}: {f.condition}</code>{/each}
            </div>
        {/if}
        {#if side.carried_into?.length}<p class="note">
                {t('conflicts.carried', {
                    columns: side.carried_into.slice(0, 8).join(', '),
                    count: side.carried_into.length,
                })}
            </p>{/if}
    </div>
{/snippet}

<article class="page conflicts">
    <header class="page-head">
        <h1 tabindex="-1">{t('page.conflicts')}</h1>
        <p class="lead">
            {#if data.candidates === undefined}
                {t('conflicts.notRun')}
            {:else if conflicts.length}
                {t('conflicts.lead', { count: conflicts.length, candidates: data.candidates })}
            {:else}
                {t('conflicts.none', { candidates: data.candidates })}
            {/if}
        </p>
    </header>
    {#each conflicts as c, i (i)}
        <section class="card conflict" aria-labelledby="conflict-{i}">
            <h2 id="conflict-{i}">
                {c.idea} <span class="count">{t('conflicts.weight', { score: c.score })}</span>
            </h2>
            <p class="note">{t('conflicts.why', { reason: c.reason })}</p>
            <div class="grid two">
                {@render sideOf(c.a, 'A')}
                {@render sideOf(c.b, 'B')}
            </div>
            <h3>{t('conflicts.differences')}</h3>
            <ul class="bullets">
                {#each c.differences as d, j (j)}
                    <li>
                        <b>{d.kind}</b>{#if d.side}{' '}<span class="badge">{d.side.toUpperCase()}</span
                            >{/if}: <code dir="ltr">{d.detail}</code>{#if d.path}<span class="note">
                                ({d.path})</span
                            >{/if}
                    </li>
                {/each}
            </ul>
        </section>
    {/each}
    {#if conflicts.length}<p class="note">
            {t('conflicts.more', { count: i18n.number(conflicts.length) })}
        </p>{/if}
</article>
