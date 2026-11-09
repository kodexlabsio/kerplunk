<script lang="ts">
    import { attr, display, KIND_NAMES, type LineageItem } from '../lib/model';
    import type { GraphNode, Owner, Usage } from '../lib/types';
    import type { Viewer } from '../lib/viewer.svelte';
    import Command from './Command.svelte';

    let { viewer }: { viewer: Viewer } = $props();
    // The viewer object is created once and never replaced, so reading it here is intended.
    // svelte-ignore state_referenced_locally
    const model = viewer.model;

    let panel: HTMLElement | undefined = $state();
    const open = $derived(!!(viewer.term || viewer.selected));
    const relation = $derived(viewer.selected ? model.node(viewer.selected) : undefined);
    const column = $derived(viewer.column ? model.node(viewer.column) : undefined);
    const term = $derived(viewer.term ? model.node(viewer.term) : undefined);

    $effect(() => {
        void viewer.selected;
        void viewer.column;
        void viewer.term;
        if (panel) panel.scrollTop = 0;
    });

    function ref(id: string): string {
        const text = model.label(id);
        return /^[\w.]+$/.test(text) ? text : JSON.stringify(text);
    }

    function quoted(name: string): string {
        return /^[\w.]+$/.test(name) ? name : JSON.stringify(name);
    }

    function selectColumn(id: string): void {
        const parent = model.node(id)?.parent;
        if (parent && model.inView.has(parent)) viewer.select(parent, id, true);
    }

    function relationsAround(id: string, adjacency: Map<string, string[]>): string[] {
        return (adjacency.get(id) ?? []).slice().sort();
    }

    function testsOf(n: GraphNode): [string, string][] {
        const found: [string, string][] = [];
        for (const target of [n.id, ...model.columnsOf(n.id).map(c => c.id)]) {
            for (const e of model.ins(target, 'tests')) found.push([e.source, target]);
        }
        return found;
    }

    function relationTerms(n: GraphNode): GraphNode[] {
        const seen = new Map<string, GraphNode>();
        for (const t of [
            ...model.termsOf(n.id),
            ...model.columnsOf(n.id).flatMap(c => model.termsOf(c.id)),
        ]) {
            seen.set(t.id, t);
        }
        return [...seen.values()];
    }

    function related(n: GraphNode): GraphNode[] {
        // A broader concept is a hierarchy, not a related term.
        return [...model.outs(n.id, 'relates_to'), ...model.ins(n.id, 'relates_to')]
            .filter(e => !e.attrs?.role)
            .map(e => model.node(e.source === n.id ? e.target : e.source))
            .filter((t): t is GraphNode => t !== undefined);
    }

    function owner(n: GraphNode): string {
        const o = attr<Owner>(n, 'owner');
        return o ? (o.role ? `${o.name}, ${o.role}` : o.name) : '';
    }

    const lineage = $derived.by(() => {
        if (!column) return { up: [] as LineageItem[], down: [] as LineageItem[] };
        return {
            up: viewer.direction !== 'downstream' ? model.columnLineage(column.id, true) : [],
            down: viewer.direction !== 'upstream' ? model.columnLineage(column.id, false) : [],
        };
    });
</script>

{#snippet section(title: string, count?: number)}
    <h3>
        {title}{#if count !== undefined}<small>{count}</small>{/if}
    </h3>
{/snippet}

{#snippet relationLink(id: string)}
    <button class="link" type="button" title={id} onclick={() => viewer.select(id, null, true)}
        >{model.label(id)}</button
    >
{/snippet}

{#snippet columnLink(id: string)}
    <button class="link" type="button" title={id} onclick={() => selectColumn(id)}>{model.label(id)}</button>
{/snippet}

{#snippet termsSection(items: GraphNode[])}
    {#if items.length}
        {@render section('Defined by', items.length)}
        <ul class="list">
            {#each items as t (t.id)}
                <li>
                    <button
                        class="link"
                        type="button"
                        title="Show the definition of {t.name}"
                        onclick={() => viewer.selectTerm(t.id)}>{t.name}</button
                    >
                    <span class="grow"></span>
                    <span class={viewer.status(t) === 'approved' ? 'ok' : 'meta'}>{viewer.status(t)}</span>
                </li>
            {/each}
        </ul>
    {/if}
{/snippet}

{#snippet conditions(items: string[])}
    {#if items.length}
        {@render section('Rows shaped by', items.length)}
        <div class="conditions">
            {#each items as c, i (i)}<code>{c}</code>{/each}
        </div>
    {/if}
{/snippet}

{#snippet formula(text: string)}
    <pre class="formula"><span class="eq">= </span>{text}</pre>
{/snippet}

<aside id="panel" hidden={!open} aria-label="Details" bind:this={panel}>
    <header>
        <div id="panel-head">
            {#if term}
                <p class="eyebrow">Business term</p>
                <h2>{term.name}</h2>
                <div class="tags">
                    <span class="tag">{viewer.status(term)}</span>
                    {#if owner(term)}<span class="tag">{owner(term)}</span>{/if}
                </div>
                {#if term.description}<p class="description">{term.description}</p>{/if}
            {:else if column && relation}
                <p class="eyebrow">Column of {(KIND_NAMES[relation.kind] ?? relation.kind).toLowerCase()}</p>
                <h2>{model.label(column.id)}</h2>
                {#if column.data_type}<p class="relation">{column.data_type.toLowerCase()}</p>{/if}
                {#if column.description}<p class="description">{column.description}</p>{/if}
                {@const used = attr<Usage>(column, 'usage')}
                {#if used}
                    <div class="tags">
                        <span class="tag"
                            >{used.queries.toLocaleString('en-GB')} queries, {used.users} users in {used.days} days</span
                        >
                    </div>
                {/if}
            {:else if relation}
                <p class="eyebrow">{KIND_NAMES[relation.kind] ?? relation.kind}</p>
                <h2>{relation.name}</h2>
                {#if relation.relation}<p class="relation">{relation.relation}</p>{/if}
                {#if relation.description}<p class="description">{relation.description}</p>{/if}
                <div class="tags">
                    {#each ['materialized', 'table_type', 'schedule', 'operator', 'orchestrator'] as key (key)}
                        {#if attr(relation, key)}<span class="tag">{display(attr(relation, key))}</span>{/if}
                    {/each}
                    {#each relation.origins ?? [] as origin (origin)}<span class="tag">{origin}</span>{/each}
                </div>
            {/if}
        </div>
        <button
            id="close"
            type="button"
            aria-label="Close details"
            title="Close (Esc)"
            onclick={() => viewer.clear()}>&#xD7;</button
        >
    </header>
    <div id="panel-body">
        {#if term}
            {@const facts = (
                [
                    ['Also called', (attr<string[]>(term, 'synonyms') ?? []).join(', ')],
                    ['Grain', attr<string>(term, 'grain')],
                    [
                        'Unit',
                        [attr<string>(term, 'unit'), attr<string>(term, 'currency')]
                            .filter(Boolean)
                            .join(' '),
                    ],
                    ['Approved', attr<string>(term, 'approved')],
                    ['Exceptions', attr<string>(term, 'exceptions')],
                ] as [string, string | undefined][]
            ).filter(([, v]) => v)}
            {#if facts.length}
                <ul class="list">
                    {#each facts as [k, v] (k)}<li>
                            <span class="meta">{k}</span><span class="grow">{v}</span>
                        </li>{/each}
                </ul>
            {/if}
            {@const filters = attr<string[]>(term, 'filters') ?? []}
            {#if filters.length}
                {@render section('Required filters', filters.length)}
                <div class="conditions">
                    {#each filters as f, i (i)}<code>{f}</code>{/each}
                </div>
            {/if}
            {@const bound = model.boundTo(term.id)}
            {@render section('Columns', bound.length)}
            {#if !bound.length}<p class="note">Not bound to any column yet.</p>{/if}
            {#each bound as id (id)}
                <div>
                    {#if model.node(id)?.kind === 'column'}{@render columnLink(
                            id,
                        )}{:else}{@render relationLink(id)}{/if}
                </div>
                {@const info = viewer.formulas[id]}
                {#if info && (info.c || info.e)}{@render formula((info.c || info.e)!)}{/if}
            {/each}
            {@const relatedTerms = related(term)}
            {#if relatedTerms.length}
                {@render section('Related terms', relatedTerms.length)}
                <ul class="list">
                    {#each relatedTerms as t (t.id)}
                        <li>
                            <button
                                class="link"
                                type="button"
                                title="Show the definition of {t.name}"
                                onclick={() => viewer.selectTerm(t.id)}>{t.name}</button
                            >
                        </li>
                    {/each}
                </ul>
            {/if}
            {#if term.path}
                {@render section('Where')}
                <ul class="list">
                    <li>
                        <span class="meta">File</span><span class="grow mono"
                            >{term.path}{attr(term, 'line') ? `:${attr(term, 'line')}` : ''}</span
                        >
                    </li>
                </ul>
            {/if}
            {@render section('Ask Konstruct')}
            <div class="commands"><Command text="konstruct define {quoted(term.name)}" /></div>
        {:else if column && relation}
            {@render termsSection(model.termsOf(column.id))}
            {@const info = viewer.formulas[column.id]}
            {@render section('Formula')}
            {#if info?.e}
                {@render formula(info.e)}
                {#if info.c && info.c !== info.e}
                    {@render section('In terms of sources')}
                    {@render formula(info.c)}
                    {#if info.s?.length}
                        <p class="note">
                            From {info.s.join(', ')}{info.p
                                ? '. Partial: some steps are not inlined, because they have no recorded expression or the formula would grow too long; the explain command shows each step.'
                                : ''}
                        </p>
                    {/if}
                {/if}
            {:else}
                <p class="note">
                    {model.ins(column.id, 'derived_from').length
                        ? 'Read directly from the warehouse or a seed.'
                        : model.outs(column.id, 'derived_from').length
                          ? 'The SQL that builds this column could not be reduced to a formula.'
                          : 'Read from a source; nothing upstream in the graph.'}
                </p>
            {/if}
            {#snippet lineageList(items: LineageItem[])}
                <ul class="list">
                    {#each items as item (item.id)}
                        <li style="padding-left: {(item.depth - 1) * 14}px">
                            {@render columnLink(item.id)}
                            <span class="grow"></span>
                            {#if item.transform}<span class="meta">{item.transform}</span>{/if}
                            {#if item.verified === 'confirmed'}<span class="ok">verified</span>{/if}
                        </li>
                    {/each}
                </ul>
            {/snippet}
            {#if viewer.direction !== 'downstream'}
                {@render section('Comes from', lineage.up.length)}
                {#if lineage.up.length}{@render lineageList(lineage.up)}{:else}<p class="note">
                        Nothing upstream.
                    </p>{/if}
            {/if}
            {#if viewer.direction !== 'upstream'}
                {@render section('Feeds', lineage.down.length)}
                {#if lineage.down.length}{@render lineageList(lineage.down)}{:else}<p class="note">
                        Nothing downstream reads this column.
                    </p>{/if}
            {/if}
            {@const refs = [
                ...new Set(
                    model
                        .outs(column.id, 'feeds')
                        .filter(e => attr(e, 'role') === 'reference')
                        .map(e => model.viewOf(e.target))
                        .filter((v): v is string => v !== null),
                ),
            ]}
            {#if refs.length}
                {@render section('Joined, filtered or grouped on by', refs.length)}
                <ul class="list">
                    {#each refs as id (id)}<li>{@render relationLink(id)}</li>{/each}
                </ul>
            {/if}
            {@render conditions(viewer.conditions[column.parent ?? ''] ?? [])}
            {@const tests = model
                .ins(column.id, 'tests')
                .map(e => model.node(e.source))
                .filter((t): t is GraphNode => !!t)}
            {#if tests.length}
                {@render section('Tests', tests.length)}
                <ul class="list">
                    {#each tests as t (t.id)}<li><span class="grow">{t.name}</span></li>{/each}
                </ul>
            {/if}
            {@render section('Ask Konstruct')}
            <div class="commands">
                <Command text="konstruct explain {ref(column.id)}" />
                <Command text="konstruct impact {ref(column.id)}" />
            </div>
        {:else if relation}
            {@const columns = model.columnsOf(relation.id)}
            {@const ups = relationsAround(relation.id, model.upAdj)}
            {@const downs = relationsAround(relation.id, model.downAdj)}
            {@const tests = testsOf(relation)}
            <div class="facts">
                {#each [[columns.length, 'columns'], [ups.length, 'upstream'], [downs.length, 'downstream'], [tests.length, 'tests']] as const as [value, text] (text)}
                    <div><b>{value}</b><span>{text}</span></div>
                {/each}
            </div>
            {@render termsSection(relationTerms(relation))}
            {#if columns.length}
                {@render section('Columns', columns.length)}
                <table class="columns">
                    <tbody>
                        {#each columns as c (c.id)}
                            <tr>
                                <td>
                                    <button
                                        class="link"
                                        type="button"
                                        title="Show the lineage of {c.name}"
                                        onclick={() => viewer.select(relation.id, c.id)}>{c.name}</button
                                    >
                                    {#if c.description}<div class="note">{c.description}</div>{/if}
                                </td>
                                <td class="type">{c.data_type ? c.data_type.toLowerCase() : ''}</td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            {/if}
            {@render conditions(viewer.conditions[relation.id] ?? [])}
            {#if ups.length}
                {@render section('Upstream', ups.length)}
                <ul class="list">
                    {#each ups as id (id)}<li>
                            {@render relationLink(id)}<span class="meta">{model.node(id)?.kind}</span>
                        </li>{/each}
                </ul>
            {/if}
            {#if downs.length}
                {@render section('Downstream', downs.length)}
                <ul class="list">
                    {#each downs as id (id)}<li>
                            {@render relationLink(id)}<span class="meta">{model.node(id)?.kind}</span>
                        </li>{/each}
                </ul>
            {/if}
            {#if tests.length}
                {@render section('Tests', tests.length)}
                <ul class="list">
                    {#each tests as [test, target], i (i)}
                        <li>
                            <span class="grow">{model.node(test)?.name ?? test}</span>
                            {#if target !== relation.id}<span class="meta">{model.node(target)?.name}</span
                                >{/if}
                        </li>
                    {/each}
                </ul>
            {/if}
            {@const runs = model
                .outs(relation.id, 'runs')
                .map(e => e.target)
                .filter(id => model.byId.has(id))}
            {#if runs.length}
                {@render section('Runs', runs.length)}
                <ul class="list">
                    {#each runs as id (id)}
                        <li>
                            {#if model.inView.has(id)}{@render relationLink(id)}{:else}<span class="grow"
                                    >{model.label(id)}</span
                                >{/if}
                            <span class="meta">{model.node(id)?.kind}</span>
                        </li>
                    {/each}
                </ul>
            {/if}
            {@const runBy = model
                .ins(relation.id, 'runs')
                .map(e => e.source)
                .filter(id => model.node(id)?.kind === 'task')}
            {#if runBy.length}
                {@render section('Run by', runBy.length)}
                <ul class="list">
                    {#each runBy as id (id)}<li>{@render relationLink(id)}</li>{/each}
                </ul>
            {/if}
            {@const where = (
                [
                    ['File', relation.path],
                    ['Line', attr(relation, 'line')],
                    ['SQL', attr(relation, 'sql_path')],
                    ['Sensors', attr(relation, 'sensors')],
                ] as [string, unknown][]
            ).filter(([, v]) => v)}
            {#if where.length}
                {@render section('Where')}
                <ul class="list">
                    {#each where as [k, v] (k)}<li>
                            <span class="meta">{k}</span><span class="grow mono">{display(v)}</span>
                        </li>{/each}
                </ul>
            {/if}
            {@render section('Ask Konstruct')}
            <div class="commands">
                <Command text="konstruct impact {ref(relation.id)}" />
                <Command text="konstruct describe {ref(relation.id)}" />
            </div>
        {/if}
    </div>
</aside>
