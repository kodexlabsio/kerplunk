<script lang="ts">
    import { untrack } from 'svelte';
    import { i18n, t } from '../../lib/i18n.svelte';
    import { kindName, kindNoun } from '../../lib/labels';
    import { display, VIEW_KINDS } from '../../lib/model';
    import { overview } from '../../lib/stats';
    import type { Viewer } from '../../lib/viewer.svelte';
    import Icon from '../Icon.svelte';
    import Markdown from '../Markdown.svelte';
    import Meter from '../Meter.svelte';
    import NodeLink from '../NodeLink.svelte';

    // The viewer is made once and never replaced.
    const props: { viewer: Viewer } = $props();
    const viewer = untrack(() => props.viewer);
    // svelte-ignore state_referenced_locally
    const model = viewer.model;
    // svelte-ignore state_referenced_locally
    const meta = viewer.data.meta ?? {};
    // svelte-ignore state_referenced_locally
    const stats = overview(model, viewer.data.warnings ?? []);
    // svelte-ignore state_referenced_locally
    const conflicts = viewer.conflicts.conflicts ?? [];
    // svelte-ignore state_referenced_locally
    const ontology = viewer.ontology;
    const world =
        (ontology.entities?.length ?? 0) + (viewer.world.metrics?.length ?? ontology.measures?.length ?? 0) >
        0;
    const verification = meta.verification as Record<string, number> | undefined;
    const sources =
        (meta.sources as
            { input?: string; name?: string; path?: string; detail?: string; note?: string }[] | undefined) ??
        [];
    const tiles = [
        'model',
        'source',
        'seed',
        'snapshot',
        'table',
        'exposure',
        'dag',
        'task',
        'test',
        'term',
    ].filter(k => stats.counts[k]);
    const generated = meta.generated_at ? String(meta.generated_at) : '';
    // The dbt projects' {% docs __overview__ %} blocks, which dbt's site opens on.
    const overviews = Object.entries((meta.overviews ?? {}) as Record<string, string>);
    // svelte-ignore state_referenced_locally
    const conflictsRun = viewer.conflicts.candidates !== undefined;
</script>

<article class="page overview">
    <header class="page-head">
        <p class="eyebrow">{t('overview.eyebrow')}</p>
        <h1 tabindex="-1">{viewer.projectName() || t('app.graph')}</h1>
        <p class="lead">
            {#if generated}<span title={i18n.date(generated, 'long')}
                    >{t('app.built', { when: i18n.relative(generated) })}</span
                >{/if}
            {#if meta.konstruct_version}<span>Konstruct {display(meta.konstruct_version)}</span>{/if}
            {#if meta.dialect}<span>{t('overview.dialect', { dialect: display(meta.dialect) })}</span>{/if}
            {#if meta.build_seconds}<span
                    >{t('overview.buildTime', { seconds: Number(meta.build_seconds) })}</span
                >{/if}
        </p>
        <div class="actions">
            <a class="button primary" href={viewer.href({ page: 'lineage' })}
                ><Icon kind="lineage" />{t('lineage.open')}</a
            >
            {#if world}
                <a class="button" href={viewer.href({ page: 'world' })}
                    ><Icon kind="world" />{t('world.open')}</a
                >
            {/if}
            <button type="button" class="button" onclick={() => (viewer.paletteOpen = true)}
                ><Icon kind="search" />{t('search.button')}</button
            >
        </div>
    </header>

    {#each overviews as [name, text] (name)}
        <section class="card about" aria-label={name}>
            <Markdown source={text} />
        </section>
    {/each}

    <section aria-labelledby="ov-counts">
        <h2 id="ov-counts" class="sr-only">{t('overview.counts')}</h2>
        <ul class="tiles" id="stats">
            {#each tiles as kind (kind)}
                <li>
                    <a
                        class="tile"
                        href={kind === 'term'
                            ? '#/glossary'
                            : kind === 'test'
                              ? '#/tests'
                              : viewer.href({ page: 'browse', kind })}
                    >
                        <Icon {kind} />
                        <b>{i18n.number(stats.counts[kind] ?? 0)}</b>
                        <span>{kindNoun(kind, stats.counts[kind] ?? 0)}</span>
                    </a>
                </li>
            {/each}
            <li>
                <div class="tile">
                    <Icon kind="column" />
                    <b>{i18n.number(stats.counts.column ?? 0)}</b>
                    <span>{t('overview.columns', { count: stats.counts.column ?? 0 })}</span>
                </div>
            </li>
            <li>
                <div class="tile">
                    <Icon kind="lineage" />
                    <b>{i18n.number(stats.columnEdges)}</b>
                    <span>{t('overview.columnEdges', { count: stats.columnEdges })}</span>
                </div>
            </li>
        </ul>
    </section>

    <div class="grid two">
        <section class="card" aria-labelledby="ov-coverage">
            <h2 id="ov-coverage">{t('overview.coverage')}</h2>
            <Meter
                label={t('overview.lineage')}
                done={stats.lineage.done}
                total={stats.lineage.total}
                detail={t('overview.ofColumns', { done: stats.lineage.done, total: stats.lineage.total })}
            />
            {#if stats.verified}
                <Meter
                    label={t('overview.verified')}
                    done={stats.verified.done}
                    total={stats.verified.total}
                    detail={t('overview.ofEdges', { done: stats.verified.done, total: stats.verified.total })}
                />
            {/if}
            <Meter
                label={t('overview.descriptions')}
                done={stats.descriptions.done}
                total={stats.descriptions.total}
                detail={t('overview.ofRelations', {
                    done: stats.descriptions.done,
                    total: stats.descriptions.total,
                })}
            />
            <Meter
                label={t('overview.columnDescriptions')}
                done={stats.columnDescriptions.done}
                total={stats.columnDescriptions.total}
                detail={t('overview.ofColumns', {
                    done: stats.columnDescriptions.done,
                    total: stats.columnDescriptions.total,
                })}
            />
            <Meter
                label={t('overview.tested')}
                done={stats.tested.done}
                total={stats.tested.total}
                detail={t('overview.ofModels', { done: stats.tested.done, total: stats.tested.total })}
            />
            {#if !stats.verified}<p class="note">{t('overview.notVerified')}</p>{/if}
        </section>

        <section class="card" aria-labelledby="ov-health">
            <h2 id="ov-health">{t('overview.health')}</h2>
            <ul class="facts-list">
                <li>
                    <Icon kind="conflicts" />
                    {#if conflictsRun}
                        <a href="#/conflicts">{t('overview.conflicts', { count: conflicts.length })}</a>
                    {:else}
                        <span>{t('overview.conflictsNotRun')}</span>
                    {/if}
                </li>
                <li>
                    <Icon kind="warnings" />
                    <a href="#/warnings"
                        >{t('overview.warnings', { count: viewer.data.warnings?.length ?? 0 })}</a
                    >
                </li>
                {#if stats.terms.total}
                    <li>
                        <Icon kind="term" />
                        <a href="#/glossary"
                            >{t('overview.terms', {
                                count: stats.terms.total,
                                approved: stats.terms.approved,
                                bound: stats.terms.bound,
                            })}</a
                        >
                    </li>
                {/if}
                {#if ontology.entities?.length}
                    <li>
                        <Icon kind="entity" />
                        <a href="#/ontology"
                            >{t('overview.entities', {
                                count: ontology.entities.length,
                                relationships: ontology.relationships?.length ?? 0,
                            })}</a
                        >
                    </li>
                {/if}
                {#if verification}
                    <li>
                        <Icon kind="check" />
                        <span
                            >{t('overview.verification', {
                                confirmed: verification.confirmed ?? 0,
                                claimed: verification.claimed ?? 0,
                                models: verification.models ?? 0,
                            })}</span
                        >
                    </li>
                {/if}
                {#if meta.usage}
                    <li>
                        <Icon kind="usage" />
                        <a href="#/usage"
                            >{t('overview.usage', {
                                days: Number((meta.usage as Record<string, unknown>).days ?? 0),
                            })}</a
                        >
                    </li>
                {/if}
            </ul>
            {#if stats.warnings.length}
                <h3>{t('overview.warningsBy')}</h3>
                <ul class="plain-list">
                    {#each stats.warnings as [head, count] (head)}
                        <li>
                            <span class="grow">{head || t('overview.other')}</span><span class="mono"
                                >{i18n.number(count)}</span
                            >
                        </li>
                    {/each}
                </ul>
            {/if}
        </section>
    </div>

    <div class="grid two">
        {#if stats.central.length}
            <section class="card" aria-labelledby="ov-central">
                <h2 id="ov-central">{t('overview.central')}</h2>
                <ol class="plain-list">
                    {#each stats.central as [id, count] (id)}
                        <li>
                            <NodeLink {viewer} {id} /><span class="grow"></span><span class="mono"
                                >{t('overview.downstream', { count })}</span
                            >
                        </li>
                    {/each}
                </ol>
            </section>
        {/if}
        {#if sources.length}
            <section class="card" aria-labelledby="ov-inputs">
                <h2 id="ov-inputs">{t('overview.inputs')}</h2>
                <ul class="plain-list">
                    {#each sources as s, i (i)}
                        <li>
                            <span class="badge">{s.input}</span>
                            <span class="grow"
                                >{s.name ?? s.path ?? ''}{#if s.detail}<span class="note">
                                        &#x00B7; {s.detail}</span
                                    >{/if}</span
                            >
                            {#if s.note}<span class="note">{s.note}</span>{/if}
                        </li>
                    {/each}
                </ul>
            </section>
        {/if}
    </div>

    {#if conflicts.length}
        <section class="card" aria-labelledby="ov-top-conflicts">
            <h2 id="ov-top-conflicts">{t('overview.topConflicts')}</h2>
            <ul class="plain-list">
                {#each conflicts.slice(0, 3) as c, i (i)}
                    <li>
                        <span class="badge warn">{c.idea}</span>
                        <NodeLink {viewer} id={c.a.column} />
                        <span aria-hidden="true">&#x2194;</span><span class="sr-only"
                            >{t('conflicts.versus')}</span
                        >
                        <NodeLink {viewer} id={c.b.column} />
                    </li>
                {/each}
            </ul>
            <p><a href="#/conflicts">{t('overview.allConflicts')}</a></p>
        </section>
    {/if}

    <section class="card" aria-labelledby="ov-kinds">
        <h2 id="ov-kinds">{t('overview.browse')}</h2>
        <ul class="chips-row">
            {#each VIEW_KINDS.filter(k => stats.counts[k]) as kind (kind)}
                <li>
                    <a class="chip" href={viewer.href({ page: 'browse', kind })}
                        ><Icon {kind} size={16} />{kindName(kind)}
                        <b>{i18n.number(stats.counts[kind] ?? 0)}</b></a
                    >
                </li>
            {/each}
        </ul>
    </section>
</article>
