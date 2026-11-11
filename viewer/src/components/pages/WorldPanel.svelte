<script lang="ts">
    import { tick, untrack } from 'svelte';
    import { i18n, t } from '../../lib/i18n.svelte';
    import type { Viewer } from '../../lib/viewer.svelte';
    import { formulaOf, sourcesOf, trustOf, type World, type WorldMetric } from '../../lib/world';
    import Icon from '../Icon.svelte';
    import Markdown from '../Markdown.svelte';
    import NodeLink from '../NodeLink.svelte';
    import Trust from '../Trust.svelte';
    import TrustMark from '../TrustMark.svelte';

    let {
        viewer,
        world,
        selectedNode,
        selectedMetric,
        metricKey,
        legacy,
    }: {
        viewer: Viewer;
        world: World;
        selectedNode: number;
        selectedMetric: WorldMetric | null;
        metricKey: (m: WorldMetric) => string;
        legacy: boolean;
    } = $props();

    // Long lists show their first rows; the field finds the rest.
    const LIMIT = 100;

    let query = $state('');
    let heading: HTMLElement | undefined = $state();
    let panel: HTMLElement | undefined = $state();

    // svelte-ignore state_referenced_locally
    const ranked = world.nodes
        .map((n, i) => i)
        .sort((a, b) => world.nodes[b]!.metrics.length - world.nodes[a]!.metrics.length || a - b);
    // svelte-ignore state_referenced_locally
    const listed = world.metrics
        .slice()
        .sort((a, b) => Number(!!a.proposed) - Number(!!b.proposed) || i18n.compare(a.name, b.name));

    function matches(text: string, synonyms?: string[]): boolean {
        const q = query.trim().toLowerCase();
        return !q || text.toLowerCase().includes(q) || !!synonyms?.some(s => s.toLowerCase().includes(q));
    }
    const foundNodes = $derived(ranked.filter(i => matches(world.nodes[i]!.name)));
    // Metrics whose entity is on the map, then those with none, which the map cannot show.
    // svelte-ignore state_referenced_locally
    const loose = new Set(world.loose);
    const foundMetrics = $derived(listed.filter(m => !loose.has(m) && matches(m.name, m.synonyms)));
    const foundLoose = $derived(listed.filter(m => loose.has(m) && matches(m.name, m.synonyms)));

    const node = $derived(selectedNode >= 0 ? world.nodes[selectedNode] : undefined);
    const relationships = $derived(
        node ? world.edges.filter(e => world.nodes[e.source] === node || world.nodes[e.target] === node) : [],
    );

    /** A cardinality read from the entity referred to: many-to-one becomes one-to-many. */
    function turned(cardinality?: string): string | undefined {
        if (cardinality === 'many-to-one') return 'one-to-many';
        if (cardinality === 'one-to-many') return 'many-to-one';
        return cardinality;
    }

    // Names that repeat for one entity, such as a count in each of two views: their rows say what
    // tells them apart, the column that holds each or the view and file that define it.
    // svelte-ignore state_referenced_locally
    const repeated = (() => {
        const seen = new Set<string>();
        const twice = new Set<string>();
        for (const m of world.metrics) {
            const name = `${m.entity ?? ''}/${m.name}`;
            if (seen.has(name)) twice.add(name);
            seen.add(name);
        }
        return twice;
    })();
    function hintOf(m: WorldMetric): string {
        const id = m.columns?.[0];
        if (id) return viewer.model.label(id);
        const view = m.exposure ? viewer.model.label(m.exposure) : undefined;
        return [view, m.path].filter(Boolean).join(' · ') || m.source;
    }

    function entityHref(name: string): string {
        return viewer.href({ page: 'world', focus: `entity:${name}` });
    }
    function metricHref(m: WorldMetric): string {
        return viewer.href({ page: 'world', focus: metricKey(m) });
    }

    /** What a proposal rests on, written here rather than carried in the page. */
    function proposalEvidence(m: WorldMetric): string[] {
        const columns = (m.columns ?? []).map(id => viewer.model.label(id)).join(', ');
        const entity = m.entity ?? '';
        if (m.source === 'entity count') return [t('world.fromCount', { entity })];
        const found =
            m.source === 'aggregate'
                ? [t('world.fromAggregate', { aggregation: (m.aggregation ?? '').toUpperCase(), columns })]
                : [t('world.fromColumn', { columns })];
        if (entity) found.push(t('world.grainRead', { entity }));
        if (m.source === 'measure column' && m.additive === undefined) found.push(t('world.notSummed'));
        return found;
    }

    // A new selection moves focus to its heading, unless the keyboard is on the map, as on a new page;
    // arriving on the page leaves focus to the page's own heading.
    let arrived = false;
    $effect(() => {
        void selectedNode;
        void selectedMetric;
        if (!arrived) {
            arrived = true;
            return;
        }
        untrack(() => {
            const active = document.activeElement;
            if (active?.closest('#world')) return;
            if (active && active !== document.body && !panel?.contains(active)) return;
            void tick().then(() => {
                if (panel) panel.scrollTop = 0;
                heading?.focus({ preventScroll: true });
            });
        });
    });
</script>

{#snippet metricRow(m: WorldMetric)}
    <li>
        <a href={metricHref(m)} class="row-link">{m.name}</a>
        <span class="tags">
            {#if m.proposed}
                <span class="badge quiet">{t('world.proposed')}</span>
            {:else if m.status}
                <span class="badge status-{m.status}">{t(`status.${m.status}` as never)}</span>
            {/if}
            <Trust trust={trustOf(m.confidence)} />
        </span>
        {#if repeated.has(`${m.entity ?? ''}/${m.name}`)}
            <span class="hint mono" dir="ltr">{hintOf(m)}</span>
        {/if}
    </li>
{/snippet}

<section class="world-panel" aria-labelledby="world-panel-title" bind:this={panel}>
    {#if selectedMetric}
        {@const m = selectedMetric}
        {@const formula = formulaOf(m, viewer.formulas)}
        {@const sources = sourcesOf(m, viewer.formulas)}
        {@const evidence = m.proposed ? proposalEvidence(m) : (m.evidence ?? [])}
        {#if node}
            <a class="back" href={entityHref(node.name)}
                ><Icon kind="chevron" size={16} />{t('world.back', { name: node.name })}</a
            >
        {:else}
            <a class="back" href={viewer.href({ page: 'world' })}
                ><Icon kind="chevron" size={16} />{t('world.all')}</a
            >
        {/if}
        <h2 id="world-panel-title" tabindex="-1" bind:this={heading}>{m.name}</h2>
        <p class="badges">
            {#if m.proposed}
                <span class="badge quiet">{t('world.proposed')}</span>
            {:else if m.status}
                <span class="badge status-{m.status}">{t(`status.${m.status}` as never)}</span>
            {/if}
            <Trust trust={trustOf(m.confidence)} />
        </p>
        {#if m.proposed}<p class="proposed">{t('world.proposedNote')}</p>{/if}
        {#if m.definition}<div class="definition"><Markdown source={m.definition} /></div>{/if}
        <dl class="details">
            {#if m.entity}
                <dt>{t('kind.entity')}</dt>
                <dd>
                    {#if node}<a href={entityHref(m.entity)}>{m.entity}</a>{:else}{m.entity}{/if}
                </dd>
            {/if}
            {#if m.grain}<dt>{t('world.grain')}</dt>
                <dd>{m.grain}</dd>{/if}
            {#if m.owner}<dt>{t('world.owner')}</dt>
                <dd>{m.owner}</dd>{/if}
            {#if m.approved}<dt>{t('world.approved')}</dt>
                <dd>{m.approved}</dd>{/if}
            {#if !m.proposed}<dt>{t('world.definedIn')}</dt>
                <dd class="mono">{m.path ?? m.source}</dd>{/if}
        </dl>
        {#if formula}
            <h3>{t('table.formula')}</h3>
            <pre class="formula" dir="ltr"><code>{formula}</code></pre>
        {/if}
        {#if m.columns?.length}
            <h3>{t('term.columns')} <span class="count">{i18n.number(m.columns.length)}</span></h3>
            <ul class="links">
                {#each m.columns as id (id)}<li><NodeLink {viewer} {id} /></li>{/each}
            </ul>
        {/if}
        {#if sources.length}
            <h3>{t('world.sources')} <span class="count">{i18n.number(sources.length)}</span></h3>
            <ul class="links mono">
                {#each sources as s (s)}<li dir="ltr">{s}</li>{/each}
            </ul>
        {/if}
        {#if evidence.length}
            <h3>{t('ontology.evidence')}</h3>
            <ul class="bullets">
                {#each evidence as e, i (i)}<li>{e}</li>{/each}
            </ul>
        {/if}
    {:else if node}
        {@const e = node.entity}
        <a class="back" href={viewer.href({ page: 'world' })}
            ><Icon kind="chevron" size={16} />{t('world.all')}</a
        >
        <h2 id="world-panel-title" tabindex="-1" bind:this={heading}>{node.name}</h2>
        <p class="badges"><Trust trust={node.trust} /></p>
        {#if e.description}<div class="definition"><Markdown source={e.description} /></div>{/if}
        <dl class="details">
            <dt>{t('ontology.key')}</dt>
            <dd class="mono">{e.key}</dd>
            {#if e.key_ids?.length}
                <dt>{t('ontology.relations')}</dt>
                <dd>
                    {#each e.key_ids as k (k)}{@const p = viewer.model.node(k)?.parent}{#if p}<NodeLink
                                {viewer}
                                id={p}
                            />{' '}{/if}{/each}
                </dd>
            {/if}
        </dl>
        <h3>{t('world.metrics')} <span class="count">{i18n.number(node.metrics.length)}</span></h3>
        {#if node.metrics.length}
            <ul class="rows">
                {#each node.metrics as m (metricKey(m))}{@render metricRow(m)}{/each}
            </ul>
        {:else}
            <p class="note">{t('world.metricCount', { count: 0 })}</p>
        {/if}
        {#if relationships.length}
            <h3>
                {t('ontology.relationships')} <span class="count">{i18n.number(relationships.length)}</span>
            </h3>
            <ul class="rows">
                {#each relationships as r, i (i)}
                    {@const other =
                        world.nodes[r.source] === node ? world.nodes[r.target]! : world.nodes[r.source]!}
                    {@const outward = world.nodes[r.source] === node}
                    <li>
                        <span class="rel">
                            <a href={entityHref(other.name)} class="row-link">{other.name}</a>
                            <span class="cardinality"
                                >{(outward ? r.rel.cardinality : turned(r.rel.cardinality)) ??
                                    t('ontology.unknown')}</span
                            >
                        </span>
                        <Trust trust={r.trust} />
                        <span class="via mono" dir="ltr">{r.rel.via}</span>
                    </li>
                {/each}
            </ul>
        {/if}
        {#if e.evidence?.length}
            <h3>{t('ontology.evidence')}</h3>
            <ul class="bullets">
                {#each e.evidence as ev, i (i)}<li>{ev}</li>{/each}
            </ul>
        {/if}
        <p class="more">
            <a class="button small" href={viewer.href({ page: 'entity', id: node.name })}
                ><Icon kind="entity" size={16} />{t('world.openEntity')}</a
            >
        </p>
    {:else}
        <h2 id="world-panel-title" tabindex="-1" bind:this={heading} class="sr-only">{t('page.world')}</h2>
        {#if legacy}<p class="proposed legacy">{t('world.legacy')}</p>{/if}
        <div class="find">
            <label class="sr-only" for="world-find">{t('world.find')}</label>
            <Icon kind="search" size={16} />
            <input
                id="world-find"
                type="search"
                autocomplete="off"
                placeholder={t('world.findPlaceholder')}
                bind:value={query}
            />
        </div>
        {#if world.nodes.length}
            <h3>{t('world.entities')} <span class="count">{i18n.number(foundNodes.length)}</span></h3>
            <ul class="rows">
                {#each foundNodes.slice(0, LIMIT) as i (i)}
                    {@const n = world.nodes[i]!}
                    <li>
                        <a href={entityHref(n.name)} class="row-link"><TrustMark trust={n.trust} />{n.name}</a
                        >
                        <span class="note">{t('world.metricCount', { count: n.metrics.length })}</span>
                    </li>
                {/each}
            </ul>
            {#if foundNodes.length > LIMIT}<p class="note">
                    {t('world.more', { shown: LIMIT, total: foundNodes.length })}
                </p>{/if}
        {/if}
        {#if listed.length > world.loose.length}
            <h3>{t('world.metrics')} <span class="count">{i18n.number(foundMetrics.length)}</span></h3>
            <ul class="rows">
                {#each foundMetrics.slice(0, LIMIT) as m (metricKey(m))}{@render metricRow(m)}{/each}
            </ul>
            {#if foundMetrics.length > LIMIT}<p class="note">
                    {t('world.more', { shown: LIMIT, total: foundMetrics.length })}
                </p>{/if}
        {/if}
        {#if world.loose.length}
            <h3>{t('world.loose')} <span class="count">{i18n.number(foundLoose.length)}</span></h3>
            <ul class="rows">
                {#each foundLoose.slice(0, LIMIT) as m (metricKey(m))}{@render metricRow(m)}{/each}
            </ul>
            {#if foundLoose.length > LIMIT}<p class="note">
                    {t('world.more', { shown: LIMIT, total: foundLoose.length })}
                </p>{/if}
        {/if}
        {#if query.trim() && !foundNodes.length && !foundMetrics.length && !foundLoose.length}<p
                class="note"
                role="status"
            >
                {t('world.noMatch')}
            </p>{/if}
    {/if}
</section>

<style>
    .world-panel {
        grid-area: panel;
        min-block-size: 0;
        overflow: auto;
        padding: 14px 16px 18px;
        border: 1px solid var(--card-border);
        border-radius: var(--radius-card);
        background: var(--card);
    }
    h2 {
        margin: 6px 0 0;
        font-size: 22px;
        font-weight: 800;
        letter-spacing: -0.02em;
        overflow-wrap: anywhere;
    }
    h2:focus {
        outline: none;
    }
    h3 {
        display: flex;
        align-items: baseline;
        gap: 8px;
        margin: 20px 0 6px;
        font-size: 16px;
        font-weight: 800;
    }
    .back {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-size: 14px;
        font-weight: 700;
        text-decoration: none;
    }
    .back :global(.icon) {
        transform: scaleX(-1);
    }
    :global([dir='rtl']) .back :global(.icon) {
        transform: none;
    }
    .back:hover {
        text-decoration: underline;
    }
    .badges {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        margin: 10px 0 0;
    }
    .proposed {
        margin: 12px 0 0;
        padding: 10px 12px;
        border: 1px dashed var(--field-border);
        border-radius: 10px;
        color: var(--text-2);
        font-size: 14px;
    }
    .legacy {
        margin: 0 0 12px;
    }
    .hint {
        flex-basis: 100%;
        color: var(--text-3);
        font-size: 14px;
        overflow-wrap: anywhere;
    }
    .definition {
        margin: 12px 0 0;
        color: var(--text-2);
        overflow-wrap: anywhere;
    }
    .definition :global(p) {
        margin: 0 0 6px;
    }
    .details {
        margin-block-start: 14px;
    }
    .formula {
        margin: 0;
    }
    .find {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 0 12px;
        border: 1px solid var(--field-border);
        border-radius: var(--radius-pill);
        background: var(--field);
        color: var(--text-3);
    }
    .find:focus-within {
        outline: 2px solid var(--accent);
        outline-offset: 2px;
    }
    .find input {
        flex: 1;
        min-inline-size: 0;
        padding: 9px 0;
        border: 0;
        background: transparent;
        color: var(--text);
        font-size: 15px;
        outline: none;
    }
    .find input::placeholder {
        color: var(--text-3);
    }
    .rows {
        margin: 0;
        padding: 0;
        list-style: none;
    }
    .rows li {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 4px 10px;
        padding: 7px 0;
        border-block-end: 1px solid var(--rule);
        font-size: 14.5px;
    }
    .rows li:last-child {
        border-block-end: 0;
    }
    .row-link {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        min-inline-size: 0;
        font-weight: 700;
        overflow-wrap: anywhere;
    }
    .row-link.small {
        font-size: 14px;
    }
    .tags {
        display: inline-flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 6px;
    }
    .rel {
        display: inline-flex;
        flex-wrap: wrap;
        align-items: baseline;
        gap: 6px;
        font-size: 14px;
    }
    .rel .cardinality {
        color: var(--text-3);
        font-size: 14px;
    }
    .via {
        flex-basis: 100%;
        color: var(--text-3);
        font-size: 14px;
        overflow-wrap: anywhere;
    }
    .links {
        display: flex;
        flex-direction: column;
        gap: 4px;
        margin: 0;
        padding: 0;
        list-style: none;
        font-size: 14px;
        overflow-wrap: anywhere;
    }
    .more {
        margin: 18px 0 0;
    }
</style>
