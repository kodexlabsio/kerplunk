<script lang="ts">
    import { untrack } from 'svelte';
    import { i18n, t } from '../../lib/i18n.svelte';
    import { transformName } from '../../lib/labels';
    import { attr, display, type LineageItem } from '../../lib/model';
    import { DASH } from '../../lib/transform';
    import type { GraphNode } from '../../lib/types';
    import type { Viewer } from '../../lib/viewer.svelte';
    import Command from '../Command.svelte';
    import NodeLink from '../NodeLink.svelte';

    // Pages are keyed by node, so a body is made for one node and never sees it change.
    const props: { viewer: Viewer; n: GraphNode } = $props();
    const viewer = untrack(() => props.viewer);
    const n = untrack(() => props.n);
    // svelte-ignore state_referenced_locally
    const model = viewer.model;
    // svelte-ignore state_referenced_locally
    const id = n.id;
    // svelte-ignore state_referenced_locally
    const info = viewer.formulas[id];
    const own = attr<string>(n, 'expression');
    const up = model.columnLineage(id, true);
    const down = model.columnLineage(id, false);
    const refs = model.referencedBy(id);
    const terms = model.termsOf(id);
    const tests = model
        .ins(id, 'tests')
        .map(e => model.node(e.source))
        .filter((x): x is GraphNode => !!x);
    // svelte-ignore state_referenced_locally
    const conditions = viewer.conditions[n.parent ?? ''] ?? [];
    const label = model.label(id);
    const command = /^[\w.]+$/.test(label) ? label : JSON.stringify(label);

    function formulaOf(columnId: string): string {
        const f = viewer.formulas[columnId];
        return f?.e ?? attr<string>(model.node(columnId), 'expression') ?? '';
    }
</script>

{#snippet steps(items: LineageItem[], upstream: boolean, title: string, empty: string)}
    <section aria-labelledby="h-{upstream ? 'up' : 'down'}">
        <h2 id="h-{upstream ? 'up' : 'down'}">
            {title} <span class="count">{i18n.number(items.length)}</span>
        </h2>
        {#if items.length}
            <div class="table-wrap">
                <table class="data lineage-steps" id={upstream ? 'comes-from' : 'feeds'}>
                    <thead>
                        <tr>
                            <th scope="col">{t('table.step')}</th>
                            <th scope="col">{t('table.column')}</th>
                            <th scope="col">{t('table.transform')}</th>
                            <th scope="col">{t('table.formula')}</th>
                            <th scope="col">{t('table.verified')}</th>
                        </tr>
                    </thead>
                    <tbody>
                        {#each items as item (item.id)}
                            <tr>
                                <td class="mono">{upstream ? '←' : '→'} {i18n.number(item.depth)}</td>
                                <th scope="row" style="padding-inline-start: {8 + (item.depth - 1) * 14}px"
                                    ><NodeLink {viewer} id={item.id} /></th
                                >
                                <td>
                                    <span class="transform t-{item.transform}"
                                        ><svg viewBox="0 0 28 10" aria-hidden="true"
                                            ><path
                                                d="M1,5 L27,5"
                                                stroke-dasharray={DASH[item.transform]}
                                            /></svg
                                        >{transformName(item.transform)}</span
                                    >
                                </td>
                                <td class="formula-cell"><code dir="ltr">{formulaOf(item.id)}</code></td>
                                <td
                                    >{#if item.verified === 'confirmed'}<span class="ok"
                                            >{t('verify.confirmed')}</span
                                        >{:else if item.verified}<span class="note">{item.verified}</span
                                        >{/if}</td
                                >
                            </tr>
                        {/each}
                    </tbody>
                </table>
            </div>
        {:else}<p class="note">{empty}</p>{/if}
    </section>
{/snippet}

<section aria-labelledby="h-formula">
    <h2 id="h-formula">{t('column.formula')}</h2>
    {#if info?.e || own}
        <pre class="formula" dir="ltr"><span class="eq">= </span>{info?.e ?? own}</pre>
        {#if info?.c && info.c !== info.e}
            <h3>{t('column.inSources')}</h3>
            <pre class="formula" dir="ltr"><span class="eq">= </span>{info.c}</pre>
            {#if info.s?.length}
                <p class="note">
                    {t('column.from', { sources: info.s.join(', ') })}{#if info.p}{' '}{t(
                            'column.partial',
                        )}{/if}
                </p>
            {/if}
        {/if}
    {:else}
        <p class="note">
            {model.ins(id, 'derived_from').length
                ? t('column.readDirectly')
                : model.outs(id, 'derived_from').length
                  ? t('column.noFormula')
                  : t('column.fromSource')}
        </p>
    {/if}
    {#if terms.length}
        <h3>{t('column.definedBy')}</h3>
        <ul class="plain-list">
            {#each terms as term (term.id)}
                <li>
                    <NodeLink {viewer} id={term.id} /><span class="grow"></span><span
                        class="badge status-{viewer.status(term)}"
                        >{t(`status.${viewer.status(term)}` as never)}</span
                    >
                </li>
            {/each}
        </ul>
    {/if}
</section>

{@render steps(up, true, t('column.comesFrom'), t('column.nothingUpstream'))}
{@render steps(down, false, t('column.feeds'), t('column.nothingDownstream'))}

{#if refs.length || conditions.length}
    <section aria-labelledby="h-refs">
        <h2 id="h-refs">{t('column.shaping')}</h2>
        {#if refs.length}
            <h3>{t('column.referencedBy')}</h3>
            <ul class="plain-list">
                {#each refs as r (r)}<li><NodeLink {viewer} id={r} /></li>{/each}
            </ul>
        {/if}
        {#if conditions.length}
            <h3>{t('node.conditions')}</h3>
            <div class="conditions">
                {#each conditions as c, i (i)}<code dir="ltr">{c}</code>{/each}
            </div>
        {/if}
    </section>
{/if}

{#if tests.length}
    <section aria-labelledby="h-ctests">
        <h2 id="h-ctests">{t('node.tests')} <span class="count">{i18n.number(tests.length)}</span></h2>
        <ul class="plain-list">
            {#each tests as test (test.id)}<li>
                    <NodeLink {viewer} id={test.id} /><span class="grow"></span><span class="note"
                        >{display(attr(test, 'test'))}</span
                    >
                </li>{/each}
        </ul>
    </section>
{/if}

<section aria-labelledby="h-ask">
    <h2 id="h-ask">{t('node.ask')}</h2>
    <div class="commands">
        <Command text="konstruct explain {command}" />
        <Command text="konstruct impact {command}" />
        <Command text="konstruct history {command}" />
    </div>
</section>
