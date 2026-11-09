<script lang="ts">
    import { untrack } from 'svelte';
    import { i18n, t } from '../../lib/i18n.svelte';
    import { attr, display } from '../../lib/model';
    import type { Viewer } from '../../lib/viewer.svelte';
    import NodeLink from '../NodeLink.svelte';

    // The viewer is made once and never replaced.
    const props: { viewer: Viewer } = $props();
    const viewer = untrack(() => props.viewer);
    // svelte-ignore state_referenced_locally
    const model = viewer.model;
    const groups = new Map<string, string[]>();
    for (const test of model.of('test')) {
        const kind = display(attr(test, 'test')) || t('tests.singular');
        const list = groups.get(kind) ?? [];
        groups.set(kind, list);
        list.push(test.id);
    }
    const sorted = [...groups].sort((a, b) => b[1].length - a[1].length || i18n.compare(a[0], b[0]));
    const untested = model.of('model').filter(m => !model.testsOf(m.id).length);
</script>

<article class="page tests">
    <header class="page-head">
        <h1 tabindex="-1">{t('page.tests')}</h1>
        <p class="lead">{t('tests.lead', { count: model.of('test').length, types: groups.size })}</p>
    </header>
    {#each sorted as [kind, ids] (kind)}
        <section aria-labelledby="tests-{kind}">
            <h2 id="tests-{kind}">{kind} <span class="count">{i18n.number(ids.length)}</span></h2>
            <div class="table-wrap">
                <table class="data">
                    <thead
                        ><tr
                            ><th scope="col">{t('table.test')}</th><th scope="col">{t('test.checks')}</th><th
                                scope="col">{t('table.severity')}</th
                            ></tr
                        ></thead
                    >
                    <tbody>
                        {#each ids as id (id)}
                            <tr>
                                <th scope="row"><NodeLink {viewer} {id} icon={false} /></th>
                                <td
                                    >{#each model.testTargets(id) as x (x)}<NodeLink
                                            {viewer}
                                            id={x}
                                        />{' '}{/each}</td
                                >
                                <td>{display(attr(model.node(id), 'severity'))}</td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            </div>
        </section>
    {/each}
    {#if untested.length}
        <section aria-labelledby="h-untested">
            <h2 id="h-untested">
                {t('tests.untested')} <span class="count">{i18n.number(untested.length)}</span>
            </h2>
            <ul class="plain-list">
                {#each untested as m (m.id)}<li><NodeLink {viewer} id={m.id} /></li>{/each}
            </ul>
        </section>
    {/if}
</article>
