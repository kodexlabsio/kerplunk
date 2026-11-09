<script lang="ts">
    import { untrack } from 'svelte';
    import { i18n, t } from '../../lib/i18n.svelte';
    import { attr } from '../../lib/model';
    import type { Usage } from '../../lib/types';
    import type { Viewer } from '../../lib/viewer.svelte';
    import NodeLink from '../NodeLink.svelte';

    // The viewer is made once and never replaced.
    const props: { viewer: Viewer } = $props();
    const viewer = untrack(() => props.viewer);
    // svelte-ignore state_referenced_locally
    const model = viewer.model;
    const used = model
        .of('column')
        .filter(c => attr(c, 'usage'))
        .sort((a, b) => (attr<Usage>(b, 'usage')?.queries ?? 0) - (attr<Usage>(a, 'usage')?.queries ?? 0));
    // Columns of models nobody queried in the window: candidates for removal.
    const unused = model
        .of('model')
        .flatMap(m => model.columnsOf(m.id))
        .filter(c => !attr(c, 'usage') && !model.ins(c.id, 'derived_from').length);
    // svelte-ignore state_referenced_locally
    const meta = (viewer.data.meta?.usage ?? {}) as Record<string, unknown>;
    let limit = $state(100);
</script>

<article class="page usage">
    <header class="page-head">
        <h1 tabindex="-1">{t('page.usage')}</h1>
        <p class="lead">
            {used.length
                ? t('usage.lead', { count: used.length, days: Number(meta.days ?? 0) })
                : t('usage.none')}
        </p>
    </header>
    {#if used.length}
        <section aria-labelledby="h-most">
            <h2 id="h-most">{t('usage.most')}</h2>
            <div class="table-wrap">
                <table class="data">
                    <thead
                        ><tr
                            ><th scope="col">{t('table.column')}</th><th scope="col" class="num"
                                >{t('usage.queries')}</th
                            ><th scope="col" class="num">{t('usage.users')}</th><th scope="col" class="num"
                                >{t('usage.score')}</th
                            ></tr
                        ></thead
                    >
                    <tbody>
                        {#each used.slice(0, limit) as c (c.id)}
                            {@const u = attr<Usage>(c, 'usage')!}
                            <tr
                                ><th scope="row"><NodeLink {viewer} id={c.id} /></th><td class="num"
                                    >{i18n.number(u.queries)}</td
                                ><td class="num">{i18n.number(u.users)}</td><td class="num"
                                    >{i18n.number(u.score ?? 0)}</td
                                ></tr
                            >
                        {/each}
                    </tbody>
                </table>
            </div>
            {#if used.length > limit}<button
                    type="button"
                    class="button"
                    onclick={() => (limit = used.length)}>{t('node.showAll', { count: used.length })}</button
                >{/if}
        </section>
        <section aria-labelledby="h-unused">
            <h2 id="h-unused">{t('usage.unused')} <span class="count">{i18n.number(unused.length)}</span></h2>
            <p class="note">{t('usage.unusedLead')}</p>
            <ul class="plain-list">
                {#each unused.slice(0, 200) as c (c.id)}<li><NodeLink {viewer} id={c.id} /></li>{/each}
            </ul>
        </section>
    {/if}
</article>
