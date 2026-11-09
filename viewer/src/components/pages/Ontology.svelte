<script lang="ts">
    import { untrack } from 'svelte';
    import { i18n, t } from '../../lib/i18n.svelte';
    import type { Viewer } from '../../lib/viewer.svelte';
    import NodeLink from '../NodeLink.svelte';

    // The viewer is made once and never replaced.
    const props: { viewer: Viewer } = $props();
    const viewer = untrack(() => props.viewer);
    // svelte-ignore state_referenced_locally
    const o = viewer.ontology;
    // svelte-ignore state_referenced_locally
    const model = viewer.model;
    const entities = (o.entities ?? []).slice().sort((a, b) => i18n.compare(a.name, b.name));
    const measures = o.measures ?? [];
    const relationships = o.relationships ?? [];
    const rules = o.rules ?? [];
    const termId = (name: string): string | undefined => model.of('term').find(x => x.name === name)?.id;
</script>

<article class="page ontology">
    <header class="page-head">
        <h1 tabindex="-1">{t('page.ontology')}</h1>
        <p class="lead">
            {t('ontology.lead', {
                entities: entities.length,
                measures: measures.length,
                relationships: relationships.length,
            })}
        </p>
    </header>
    <section aria-labelledby="h-entities">
        <h2 id="h-entities">
            {t('ontology.entities')} <span class="count">{i18n.number(entities.length)}</span>
        </h2>
        {#if entities.length}
            <div class="table-wrap">
                <table class="data">
                    <thead
                        ><tr
                            ><th scope="col">{t('kind.entity')}</th><th scope="col">{t('ontology.key')}</th
                            ><th scope="col">{t('ontology.relations')}</th><th scope="col"
                                >{t('kind.term')}</th
                            ></tr
                        ></thead
                    >
                    <tbody>
                        {#each entities as e (e.name)}
                            <tr>
                                <th scope="row"
                                    ><a href={viewer.href({ page: 'entity', id: e.name })}>{e.name}</a></th
                                >
                                <td class="mono">{e.key}</td>
                                <td
                                    >{#each e.key_ids ?? [] as k (k)}{@const p =
                                            model.node(k)?.parent}{#if p}<NodeLink
                                                {viewer}
                                                id={p}
                                            />{' '}{/if}{/each}</td
                                >
                                <td
                                    >{#if e.term && termId(e.term)}<NodeLink
                                            {viewer}
                                            id={termId(e.term)!}
                                        />{/if}</td
                                >
                            </tr>
                        {/each}
                    </tbody>
                </table>
            </div>
        {:else}<p class="note">{t('ontology.noEntities')}</p>{/if}
    </section>
    <section aria-labelledby="h-measures">
        <h2 id="h-measures">
            {t('ontology.measures')} <span class="count">{i18n.number(measures.length)}</span>
        </h2>
        {#if measures.length}
            <div class="table-wrap">
                <table class="data">
                    <thead
                        ><tr
                            ><th scope="col">{t('ontology.measure')}</th><th scope="col"
                                >{t('ontology.status')}</th
                            ><th scope="col">{t('kind.entity')}</th><th scope="col">{t('term.columns')}</th
                            ><th scope="col">{t('table.formula')}</th></tr
                        ></thead
                    >
                    <tbody>
                        {#each measures as m (m.name)}
                            <tr>
                                <th scope="row"
                                    >{#if termId(m.name)}<NodeLink
                                            {viewer}
                                            id={termId(m.name)!}
                                        />{:else}{m.name}{/if}</th
                                >
                                <td
                                    ><span class="badge status-{m.status ?? 'draft'}"
                                        >{t(`status.${m.status ?? 'draft'}` as never)}</span
                                    ></td
                                >
                                <td
                                    >{#if m.entity}<a href={viewer.href({ page: 'entity', id: m.entity })}
                                            >{m.entity}</a
                                        >{/if}</td
                                >
                                <td
                                    >{#each m.columns ?? [] as c (c.column)}{#if c.id}<NodeLink
                                                {viewer}
                                                id={c.id}
                                            />{:else}{c.column}{/if}{' '}{/each}</td
                                >
                                <td class="formula-cell"
                                    ><code dir="ltr">{m.expression ?? m.columns?.[0]?.formula ?? ''}</code
                                    ></td
                                >
                            </tr>
                        {/each}
                    </tbody>
                </table>
            </div>
        {:else}<p class="note">{t('ontology.noMeasures')}</p>{/if}
    </section>
    {#if relationships.length}
        <section aria-labelledby="h-rels">
            <h2 id="h-rels">
                {t('ontology.relationships')} <span class="count">{i18n.number(relationships.length)}</span>
            </h2>
            <div class="table-wrap">
                <table class="data">
                    <thead
                        ><tr
                            ><th scope="col">{t('ontology.from')}</th><th scope="col">{t('ontology.to')}</th
                            ><th scope="col">{t('ontology.cardinality')}</th><th scope="col"
                                >{t('ontology.via')}</th
                            ><th scope="col">{t('ontology.tested')}</th></tr
                        ></thead
                    >
                    <tbody>
                        {#each relationships as rel, i (i)}
                            <tr>
                                <th scope="row"
                                    ><a href={viewer.href({ page: 'entity', id: rel.source })}>{rel.source}</a
                                    ></th
                                >
                                <td
                                    ><a href={viewer.href({ page: 'entity', id: rel.target })}>{rel.target}</a
                                    ></td
                                >
                                <td>{rel.cardinality ?? t('ontology.unknown')}</td>
                                <td class="mono">{rel.via}</td>
                                <td>{rel.tested ? t('ontology.yes') : t('ontology.no')}</td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            </div>
        </section>
    {/if}
    {#if rules.length}
        <section aria-labelledby="h-rules">
            <h2 id="h-rules">{t('ontology.rules')}</h2>
            <ul class="plain-list rules">
                {#each rules as rule (rule.name)}
                    <li>
                        <span
                            class="badge"
                            class:ok={!rule.violations?.length}
                            class:warn={!!rule.violations?.length}
                            >{rule.violations?.length
                                ? t('ontology.broken', { count: rule.violations.length })
                                : t('ontology.holds')}</span
                        >
                        <span class="grow"
                            ><b class="mono">{rule.name}</b>
                            {rule.description ?? ''}
                            {#if rule.violations?.length}<ul>
                                    {#each rule.violations as v, i (i)}<li>{v}</li>{/each}
                                </ul>{/if}</span
                        >
                    </li>
                {/each}
            </ul>
        </section>
    {/if}
</article>
