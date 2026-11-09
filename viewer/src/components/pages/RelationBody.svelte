<script lang="ts">
    import { untrack } from 'svelte';
    import { i18n, t } from '../../lib/i18n.svelte';
    import { kindName, transformName } from '../../lib/labels';
    import { attr, display, type GraphModel } from '../../lib/model';
    import { reference } from '../../lib/selector';
    import { classify, DASH, type Transform } from '../../lib/transform';
    import type { GraphNode, Usage } from '../../lib/types';
    import type { Viewer } from '../../lib/viewer.svelte';
    import Code from '../Code.svelte';
    import Command from '../Command.svelte';
    import NodeLink from '../NodeLink.svelte';
    import TestBadges from '../TestBadges.svelte';

    // Pages are keyed by node, so a body is made for one node and never sees it change.
    const props: { viewer: Viewer; n: GraphNode } = $props();
    const viewer = untrack(() => props.viewer);
    const n = untrack(() => props.n);
    // svelte-ignore state_referenced_locally
    const model: GraphModel = viewer.model;
    // svelte-ignore state_referenced_locally
    const id = n.id;
    const columns = model.columnsOf(id);
    const ups = (model.upAdj.get(id) ?? [])
        .slice()
        .sort((a, b) => i18n.compare(model.label(a), model.label(b)));
    const downs = (model.downAdj.get(id) ?? [])
        .slice()
        .sort((a, b) => i18n.compare(model.label(a), model.label(b)));
    const allUp = model.ancestors(id).size;
    const allDown = model.descendants(id).size;
    const tests = model.testsOf(id);
    // svelte-ignore state_referenced_locally
    const conditions = viewer.conditions[id] ?? [];
    // svelte-ignore state_referenced_locally
    const code = viewer.sql[id];
    const viewSql = attr<string>(n, 'view_sql');
    const runBy = model
        .ins(id, 'runs')
        .map(e => e.source)
        .filter(s => model.node(s)?.kind === 'task');
    const usage = columns.some(c => attr(c, 'usage'));

    /** What a column is built by: its own transform kind, from its incoming edges. */
    function kindOf(c: GraphNode): Transform | null {
        const inputs = model.outs(c.id, 'derived_from');
        if (!inputs.length) return null;
        const kinds = inputs.map(e => model.transformOf(e));
        const rank: Transform[] = ['window', 'aggregate', 'expression', 'cast', 'renamed', 'direct'];
        return rank.find(k => kinds.includes(k)) ?? classify(undefined, attr<string>(c, 'expression'));
    }

    let filter = $state('');
    let limit = $state(200);
    const shown = $derived(
        filter.trim()
            ? columns.filter(c =>
                  `${c.name} ${c.description ?? ''} ${c.data_type ?? ''}`
                      .toLowerCase()
                      .includes(filter.trim().toLowerCase()),
              )
            : columns,
    );

    const versions = [
        ...(code?.s ? [{ name: t('code.source'), text: code.s }] : []),
        ...(code?.c ? [{ name: t('code.compiled'), text: code.c }] : []),
        ...(viewSql ? [{ name: t('code.view'), text: viewSql }] : []),
    ];
    const expressions = columns
        .filter(c => attr<string>(c, 'expression'))
        .map(c => `${attr<string>(c, 'expression')} AS ${c.name}`)
        .join(',\n    ');

    const details = (
        [
            [t('detail.path'), n.path],
            [t('detail.relation'), n.relation],
            [t('detail.package'), model.packageOf(n)],
            [t('detail.materialized'), attr(n, 'materialized')],
            [t('detail.tableType'), attr(n, 'table_type')],
            [t('detail.dbtName'), attr(n, 'dbt_name')],
            [t('detail.fqn'), (attr<string[]>(n, 'fqn') ?? []).join('.')],
            [t('detail.loadedBy'), attr(n, 'loaded_by')],
            [t('detail.partitioned'), attr(n, 'partitioned_by')],
            [t('detail.identifiers'), attr(n, 'identifier_fields')],
            [t('detail.lineage'), attr(n, 'lineage')],
        ] as [string, unknown][]
    ).filter(([, v]) => v !== undefined && v !== null && v !== '' && !(Array.isArray(v) && !v.length));

    function jump(event: MouseEvent, target: string): void {
        // In-page links must not change the route: scroll instead.
        event.preventDefault();
        const el = document.getElementById(target);
        el?.scrollIntoView({
            block: 'start',
            behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
        });
        el?.querySelector<HTMLElement>('h2')?.focus({ preventScroll: true });
    }

    const ref = reference(model, id);
    const command = /^[\w.]+$/.test(model.label(id)) ? model.label(id) : JSON.stringify(model.label(id));
</script>

<nav class="sections" aria-label={t('node.sections')}>
    <ul>
        {#if columns.length}<li>
                <button type="button" class="link" onclick={e => jump(e, 'sec-columns')}
                    >{t('node.columns')} <span class="count">{i18n.number(columns.length)}</span></button
                >
            </li>{/if}
        <li>
            <button type="button" class="link" onclick={e => jump(e, 'sec-deps')}
                >{t('node.dependencies')}</button
            >
        </li>
        {#if versions.length || expressions}<li>
                <button type="button" class="link" onclick={e => jump(e, 'sec-code')}>{t('node.code')}</button
                >
            </li>{/if}
        {#if tests.length}<li>
                <button type="button" class="link" onclick={e => jump(e, 'sec-tests')}
                    >{t('node.tests')} <span class="count">{i18n.number(tests.length)}</span></button
                >
            </li>{/if}
        <li>
            <button type="button" class="link" onclick={e => jump(e, 'sec-details')}
                >{t('node.details')}</button
            >
        </li>
    </ul>
</nav>

<div class="stats-strip">
    <div>
        <b>{i18n.number(columns.length)}</b><span>{t('node.columnsCount', { count: columns.length })}</span>
    </div>
    <div>
        <b>{i18n.number(ups.length)}</b><span
            >{t('node.upstreamCount', { count: ups.length, all: allUp })}</span
        >
    </div>
    <div>
        <b>{i18n.number(downs.length)}</b><span
            >{t('node.downstreamCount', { count: downs.length, all: allDown })}</span
        >
    </div>
    <div><b>{i18n.number(tests.length)}</b><span>{t('node.testsCount', { count: tests.length })}</span></div>
</div>

{#if columns.length}
    <section id="sec-columns" aria-labelledby="h-columns">
        <div class="section-head">
            <h2 id="h-columns" tabindex="-1">
                {t('node.columns')} <span class="count">{i18n.number(columns.length)}</span>
            </h2>
            {#if columns.length > 8}
                <label class="inline-filter">
                    <span class="sr-only">{t('node.filterColumns')}</span>
                    <input type="search" placeholder={t('node.filterColumns')} bind:value={filter} />
                </label>
            {/if}
        </div>
        <div class="table-wrap" role="region" aria-labelledby="h-columns" tabindex="-1">
            <table class="data columns" id="col-table">
                <thead>
                    <tr>
                        <th scope="col">{t('table.name')}</th>
                        <th scope="col">{t('table.type')}</th>
                        <th scope="col">{t('table.description')}</th>
                        <th scope="col">{t('table.tests')}</th>
                        <th scope="col">{t('table.terms')}</th>
                        <th scope="col">{t('table.transform')}</th>
                        <th scope="col">{t('table.formula')}</th>
                    </tr>
                </thead>
                <tbody>
                    {#each shown.slice(0, limit) as c (c.id)}
                        {@const kind = kindOf(c)}
                        {@const info = viewer.formulas[c.id]}
                        {@const formula = info?.c || info?.e || attr<string>(c, 'expression')}
                        {@const terms = model.termsOf(c.id)}
                        {@const used = attr<Usage>(c, 'usage')}
                        <tr data-column={c.id}>
                            <th scope="row"
                                ><a class="col-link" href={viewer.href({ page: 'node', id: c.id })}
                                    >{c.name}</a
                                >
                                {#if used}<span class="note">
                                        {t('node.queries', { count: used.queries })}</span
                                    >{/if}</th
                            >
                            <td class="mono type">{c.data_type?.toLowerCase() ?? ''}</td>
                            <td class="desc">{c.description ?? ''}</td>
                            <td><TestBadges {viewer} id={c.id} /></td>
                            <td>
                                {#each terms as term (term.id)}<a
                                        class="badge term"
                                        href={viewer.href({ page: 'node', id: term.id })}>{term.name}</a
                                    >{/each}
                            </td>
                            <td>
                                {#if kind}
                                    <span class="transform t-{kind}"
                                        ><svg viewBox="0 0 28 10" aria-hidden="true"
                                            ><path d="M1,5 L27,5" stroke-dasharray={DASH[kind]} /></svg
                                        >{transformName(kind)}</span
                                    >
                                {/if}
                            </td>
                            <td class="formula-cell">
                                {#if formula}
                                    <code dir="ltr">{formula}</code>
                                    <a
                                        class="explain"
                                        href={viewer.href({ page: 'node', id: c.id })}
                                        aria-label={t('node.explainColumn', { column: c.name })}
                                        >{t('node.explain')}</a
                                    >
                                {/if}
                            </td>
                        </tr>
                    {/each}
                </tbody>
            </table>
        </div>
        {#if shown.length > limit}
            <button type="button" class="button" onclick={() => (limit = shown.length)}
                >{t('node.showAll', { count: shown.length })}</button
            >
        {/if}
        {#if !shown.length}<p class="note">{t('node.noColumnsMatch')}</p>{/if}
    </section>
{/if}

<section id="sec-deps" aria-labelledby="h-deps">
    <h2 id="h-deps" tabindex="-1">{t('node.dependencies')}</h2>
    <div class="grid two">
        <div>
            <h3>{t('node.dependsOn')} <span class="count">{i18n.number(ups.length)}</span></h3>
            {#if ups.length}
                <ul class="plain-list">
                    {#each ups as up (up)}<li>
                            <NodeLink {viewer} id={up} /><span class="grow"></span><span class="note"
                                >{kindName(model.node(up)?.kind ?? '')}</span
                            >
                        </li>{/each}
                </ul>
                {#if allUp > ups.length}<p class="note">
                        <a href={viewer.href({ page: 'lineage', select: `+${ref}`, focus: id })}
                            >{t('node.allUpstream', { count: allUp })}</a
                        >
                    </p>{/if}
            {:else}<p class="note">{t('node.nothingUpstream')}</p>{/if}
        </div>
        <div>
            <h3>{t('node.referencedBy')} <span class="count">{i18n.number(downs.length)}</span></h3>
            {#if downs.length}
                <ul class="plain-list">
                    {#each downs as down (down)}<li>
                            <NodeLink {viewer} id={down} /><span class="grow"></span><span class="note"
                                >{kindName(model.node(down)?.kind ?? '')}</span
                            >
                        </li>{/each}
                </ul>
                {#if allDown > downs.length}<p class="note">
                        <a href={viewer.href({ page: 'lineage', select: `${ref}+`, focus: id })}
                            >{t('node.allDownstream', { count: allDown })}</a
                        >
                    </p>{/if}
            {:else}<p class="note">{t('node.nothingDownstream')}</p>{/if}
        </div>
    </div>
    {#if runBy.length}
        <h3>{t('node.runBy')}</h3>
        <ul class="plain-list">
            {#each runBy as task (task)}<li><NodeLink {viewer} id={task} /></li>{/each}
        </ul>
    {/if}
    {#if conditions.length}
        <h3>{t('node.conditions')} <span class="count">{i18n.number(conditions.length)}</span></h3>
        <div class="conditions">
            {#each conditions as c, i (i)}<code dir="ltr">{c}</code>{/each}
        </div>
    {/if}
</section>

{#if versions.length || expressions}
    <section id="sec-code" aria-labelledby="h-code">
        <h2 id="h-code" tabindex="-1">{t('node.code')}</h2>
        {#if versions.length}
            <Code {versions} label={t('node.code')} />
            {#if code?.t}<p class="note">{t('code.cut')}</p>{/if}
        {:else}
            <p class="note">{t('code.fromExpressions')}</p>
            <Code
                versions={[{ name: t('code.expressions'), text: `SELECT\n    ${expressions}` }]}
                label={t('code.expressions')}
            />
        {/if}
    </section>
{/if}

{#if tests.length}
    <section id="sec-tests" aria-labelledby="h-tests">
        <h2 id="h-tests" tabindex="-1">
            {t('node.tests')} <span class="count">{i18n.number(tests.length)}</span>
        </h2>
        <div class="table-wrap">
            <table class="data">
                <thead
                    ><tr
                        ><th scope="col">{t('table.test')}</th><th scope="col">{t('table.type')}</th><th
                            scope="col">{t('table.column')}</th
                        ><th scope="col">{t('table.severity')}</th></tr
                    ></thead
                >
                <tbody>
                    {#each tests as [test, target], i (i)}
                        <tr>
                            <th scope="row"
                                ><a href={viewer.href({ page: 'node', id: test.id })}>{test.name}</a></th
                            >
                            <td>{display(attr(test, 'test'))}</td>
                            <td
                                >{#if target !== id}<a href={viewer.href({ page: 'node', id: target })}
                                        >{model.node(target)?.name}</a
                                    >{/if}</td
                            >
                            <td>{display(attr(test, 'severity'))}</td>
                        </tr>
                    {/each}
                </tbody>
            </table>
        </div>
    </section>
{/if}

{#if usage}
    <section aria-labelledby="h-usage">
        <h2 id="h-usage">{t('page.usage')}</h2>
        <ul class="plain-list">
            {#each columns
                .filter(c => attr(c, 'usage'))
                .sort((a, b) => (attr<Usage>(b, 'usage')?.queries ?? 0) - (attr<Usage>(a, 'usage')?.queries ?? 0)) as c (c.id)}
                {@const u = attr<Usage>(c, 'usage')!}
                <li>
                    <NodeLink {viewer} id={c.id} label={c.name} /><span class="grow"></span><span class="mono"
                        >{t('node.usage', { queries: u.queries, users: u.users, days: u.days })}</span
                    >
                </li>
            {/each}
        </ul>
    </section>
{/if}

<section id="sec-details" aria-labelledby="h-details">
    <h2 id="h-details" tabindex="-1">{t('node.details')}</h2>
    <dl class="details">
        {#each details as [k, v] (k)}<dt>{k}</dt>
            <dd class="mono">{display(v)}</dd>{/each}
    </dl>
    <h3>{t('node.ask')}</h3>
    <div class="commands">
        <Command text="konstruct impact {command}" />
        <Command text="konstruct describe {command}" />
        <Command text="konstruct trace {command}" />
    </div>
</section>
