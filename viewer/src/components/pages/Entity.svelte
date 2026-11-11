<script lang="ts">
    import Markdown from '../Markdown.svelte';
    import { i18n, t } from '../../lib/i18n.svelte';
    import type { Viewer } from '../../lib/viewer.svelte';
    import Icon from '../Icon.svelte';
    import { trustOf } from '../../lib/world';
    import NodeLink from '../NodeLink.svelte';
    import Trust from '../Trust.svelte';
    import NotFound from './NotFound.svelte';

    let { viewer, name }: { viewer: Viewer; name: string } = $props();
    // svelte-ignore state_referenced_locally
    const e = viewer.ontology.entities?.find(x => x.name === name);
    // svelte-ignore state_referenced_locally
    const model = viewer.model;
    // svelte-ignore state_referenced_locally
    const rels = (viewer.ontology.relationships ?? []).filter(r => r.source === name || r.target === name);
    // svelte-ignore state_referenced_locally
    const measures = (viewer.ontology.measures ?? []).filter(m => m.entity === name);
    const termId = e?.term ? model.of('term').find(x => x.name === e.term)?.id : undefined;
</script>

{#if e}
    <article class="page entity">
        <header class="page-head">
            <p class="eyebrow"><Icon kind="entity" size={16} />{t('kind.entity')}</p>
            <h1 tabindex="-1">{e.name}</h1>
            <p class="relation mono">{t('ontology.keyIs', { key: e.key })}</p>
            <p class="badges"><Trust trust={trustOf(e.confidence, e.check)} /></p>
            {#if e.description}<div class="description"><Markdown source={e.description} /></div>{/if}
            <div class="actions">
                <button
                    type="button"
                    class="button"
                    aria-pressed={viewer.dockOpen}
                    aria-controls={viewer.dockOpen ? 'dock' : undefined}
                    onclick={() => viewer.toggleDock()}
                    ><Icon kind="dock" />{viewer.dockOpen ? t('dock.hide') : t('dock.show')}</button
                >
            </div>
        </header>
        <section aria-labelledby="h-keys">
            <h2 id="h-keys">
                {t('ontology.relations')} <span class="count">{i18n.number(e.key_ids?.length ?? 0)}</span>
            </h2>
            <ul class="plain-list">
                {#each e.key_ids ?? [] as k (k)}<li>
                        <NodeLink {viewer} id={model.node(k)?.parent ?? k} /><span class="grow"
                        ></span><NodeLink {viewer} id={k} label={model.node(k)?.name} />
                    </li>{/each}
            </ul>
            {#if termId}<p>{t('ontology.namedBy')} <NodeLink {viewer} id={termId} /></p>{/if}
        </section>
        {#if rels.length}
            <section aria-labelledby="h-erels">
                <h2 id="h-erels">{t('ontology.relationships')}</h2>
                <ul class="plain-list">
                    {#each rels as r, i (i)}
                        <li>
                            <a href={viewer.href({ page: 'entity', id: r.source })}>{r.source}</a>
                            <span class="note">{r.cardinality ?? t('ontology.unknown')}</span>
                            <a href={viewer.href({ page: 'entity', id: r.target })}>{r.target}</a>
                            <span class="grow mono note">{r.via}</span>
                        </li>
                    {/each}
                </ul>
            </section>
        {/if}
        {#if measures.length}
            <section aria-labelledby="h-emeas">
                <h2 id="h-emeas">{t('ontology.measures')}</h2>
                <ul class="plain-list">
                    {#each measures as m (m.name)}
                        {@const id = model.of('term').find(x => x.name === m.name)?.id}
                        <li>
                            {#if id}<NodeLink {viewer} {id} />{:else}{m.name}{/if}<span class="grow"
                            ></span><span class="note">{m.grain ?? ''}</span>
                        </li>
                    {/each}
                </ul>
            </section>
        {/if}
        {#if e.evidence?.length}
            <section aria-labelledby="h-evidence">
                <h2 id="h-evidence">{t('ontology.evidence')}</h2>
                <ul class="bullets">
                    {#each e.evidence as ev, i (i)}<li>{ev}</li>{/each}
                </ul>
            </section>
        {/if}
    </article>
{:else}
    <NotFound {viewer} what={name} />
{/if}
