<script lang="ts">
    import { untrack } from 'svelte';
    import { i18n, t } from '../../lib/i18n.svelte';
    import { attr, strings } from '../../lib/model';
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
    const measure = viewer.ontology.measures?.find(m => m.name === n.name);
    // svelte-ignore state_referenced_locally
    const concept = viewer.ontology.concepts?.find(c => c.name === n.name);
    // svelte-ignore state_referenced_locally
    const conflicts = (viewer.conflicts.conflicts ?? []).filter(c => c.idea === n.name);
    const bound = model.boundTo(n.id);
    const facts = (
        [
            [t('term.synonyms'), strings(attr(n, 'synonyms')).join(', ')],
            [t('term.grain'), attr<string>(n, 'grain')],
            [
                t('term.unit'),
                [attr<string>(n, 'unit'), attr<string>(n, 'currency')].filter(Boolean).join(' '),
            ],
            [t('term.approved'), attr<string>(n, 'approved')],
            [t('term.entity'), measure?.entity],
            [t('term.exceptions'), attr<string>(n, 'exceptions')],
            [
                t('detail.path'),
                n.path ? `${n.path}${attr(n, 'line') ? `:${String(attr(n, 'line'))}` : ''}` : '',
            ],
        ] as [string, string | undefined][]
    ).filter(([, v]) => v);
    const filters = strings(attr(n, 'filters'));
    const related = [...model.outs(n.id, 'relates_to'), ...model.ins(n.id, 'relates_to')]
        .filter(e => !attr(e, 'role'))
        .map(e => (e.source === n.id ? e.target : e.source))
        .filter(id => model.byId.has(id));
    const termId = (name: string): string | undefined => model.of('term').find(x => x.name === name)?.id;
    const quoted = /^[\w.]+$/.test(n.name) ? n.name : JSON.stringify(n.name);
</script>

{#if facts.length}
    <dl class="details">
        {#each facts as [k, v] (k)}<dt>{k}</dt>
            <dd>{v}</dd>{/each}
    </dl>
{/if}

{#if filters.length}
    <section aria-labelledby="h-filters">
        <h2 id="h-filters">{t('term.filters')}</h2>
        <div class="conditions">
            {#each filters as f, i (i)}<code dir="ltr">{f}</code>{/each}
        </div>
    </section>
{/if}

<section aria-labelledby="h-bound">
    <h2 id="h-bound">{t('term.columns')} <span class="count">{i18n.number(bound.length)}</span></h2>
    {#if measure?.expression}<pre class="formula" dir="ltr"><span class="eq"
                >= </span>{measure.expression}</pre>{/if}
    {#if bound.length}
        <div class="table-wrap">
            <table class="data">
                <thead
                    ><tr
                        ><th scope="col">{t('table.column')}</th><th scope="col">{t('table.formula')}</th><th
                            scope="col">{t('table.verified')}</th
                        ></tr
                    ></thead
                >
                <tbody>
                    {#each bound as id (id)}
                        {@const info = viewer.formulas[id]}
                        {@const m = measure?.columns?.find(c => c.id === id)}
                        <tr>
                            <th scope="row"><NodeLink {viewer} {id} /></th>
                            <td class="formula-cell"
                                ><code dir="ltr">{info?.c || info?.e || m?.formula || ''}</code></td
                            >
                            <td
                                >{#if m?.edges}{t('node.verified', {
                                        done: m.verified ?? 0,
                                        total: m.edges,
                                    })}{/if}</td
                            >
                        </tr>
                    {/each}
                </tbody>
            </table>
        </div>
    {:else}<p class="note">{t('term.unbound')}</p>{/if}
</section>

{#if conflicts.length}
    <section aria-labelledby="h-tconf">
        <h2 id="h-tconf">{t('page.conflicts')} <span class="count">{i18n.number(conflicts.length)}</span></h2>
        <ul class="plain-list">
            {#each conflicts as c, i (i)}
                <li>
                    <NodeLink {viewer} id={c.a.column} /><span class="sr-only">{t('conflicts.versus')}</span
                    ><span aria-hidden="true">&#x2194;</span><NodeLink {viewer} id={c.b.column} /><span
                        class="grow"
                    ></span><span class="note">{c.differences.map(d => d.kind).join(', ')}</span>
                </li>
            {/each}
        </ul>
        <p><a href="#/conflicts">{t('overview.allConflicts')}</a></p>
    </section>
{/if}

{#if related.length || concept?.broader?.length || concept?.narrower?.length}
    <section aria-labelledby="h-related">
        <h2 id="h-related">{t('term.related')}</h2>
        {#if concept?.broader?.length}
            <h3>{t('term.broader')}</h3>
            <ul class="plain-list">
                {#each concept.broader as b (b)}<li>
                        {#if termId(b)}<NodeLink {viewer} id={termId(b)!} />{:else}{b}{/if}
                    </li>{/each}
            </ul>
        {/if}
        {#if concept?.narrower?.length}
            <h3>{t('term.narrower')}</h3>
            <ul class="plain-list">
                {#each concept.narrower as b (b)}<li>
                        {#if termId(b)}<NodeLink {viewer} id={termId(b)!} />{:else}{b}{/if}
                    </li>{/each}
            </ul>
        {/if}
        {#if related.length}
            <h3>{t('term.relatedTerms')}</h3>
            <ul class="plain-list">
                {#each related as r (r)}<li><NodeLink {viewer} id={r} /></li>{/each}
            </ul>
        {/if}
    </section>
{/if}

<section aria-labelledby="h-task">
    <h2 id="h-task">{t('node.ask')}</h2>
    <div class="commands"><Command text="konstruct define {quoted}" /></div>
</section>
