<script lang="ts">
    import { untrack } from 'svelte';
    import { i18n, t } from '../../lib/i18n.svelte';
    import { kindName } from '../../lib/labels';
    import { attr, display } from '../../lib/model';
    import type { GraphNode } from '../../lib/types';
    import type { Viewer } from '../../lib/viewer.svelte';
    import Code from '../Code.svelte';
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
    const code = viewer.sql[id];

    const keys: [string, string][] = [
        ['schedule', t('detail.schedule')],
        ['operator', t('detail.operator')],
        ['orchestrator', t('detail.orchestrator')],
        ['type', t('detail.type')],
        ['maturity', t('detail.maturity')],
        ['label', t('detail.label')],
        ['url', t('detail.url')],
        ['test', t('detail.testType')],
        ['severity', t('detail.severity')],
        ['column', t('detail.column')],
        ['sql_path', t('detail.sql')],
        ['line', t('detail.line')],
        ['sensors', t('detail.sensors')],
    ];
    const details: [string, string][] = [
        ...(n.path ? ([[t('detail.path'), n.path]] as [string, string][]) : []),
        ...(n.kind === 'task' && attr(n, 'dag')
            ? ([[t('kind.dag'), display(attr(n, 'dag'))]] as [string, string][])
            : []),
        ...keys
            .filter(([k]) => attr(n, k) !== undefined && attr(n, k) !== '')
            .map(([k, label]) => [label, display(attr(n, k))] as [string, string]),
    ];

    // DAG: its tasks in run order. Task: what runs before and after it, and what it runs.
    const tasks =
        n.kind === 'dag'
            ? order(
                  model
                      .outs(id, 'runs')
                      .map(e => e.target)
                      .filter(x => model.node(x)?.kind === 'task'),
              )
            : [];
    const runs =
        n.kind === 'task'
            ? model
                  .outs(id, 'runs')
                  .map(e => e.target)
                  .filter(x => model.byId.has(x))
            : [];
    const before = n.kind === 'task' ? model.ins(id, 'precedes').map(e => e.source) : [];
    const after = n.kind === 'task' ? model.outs(id, 'precedes').map(e => e.target) : [];
    const ups = (model.upAdj.get(id) ?? []).filter(x => model.node(x)?.kind !== 'task');
    const downs = (model.downAdj.get(id) ?? []).filter(x => model.node(x)?.kind !== 'task');
    const targets = n.kind === 'test' ? model.testTargets(id) : [];
    const command = attr<string>(n, 'bash_command');

    function order(ids: string[]): string[] {
        // Tasks in the order "precedes" gives, ties by name.
        const set = new Set(ids);
        const indegree = new Map(ids.map(x => [x, 0]));
        for (const x of ids)
            for (const e of model.outs(x, 'precedes'))
                if (set.has(e.target)) indegree.set(e.target, (indegree.get(e.target) ?? 0) + 1);
        const out: string[] = [];
        const ready = ids.filter(x => !indegree.get(x)).sort();
        while (ready.length) {
            const x = ready.shift()!;
            out.push(x);
            for (const e of model.outs(x, 'precedes')) {
                if (!set.has(e.target)) continue;
                indegree.set(e.target, (indegree.get(e.target) ?? 0) - 1);
                if (!indegree.get(e.target)) ready.push(e.target);
            }
        }
        return out.concat(ids.filter(x => !out.includes(x)));
    }

    const versions = [
        ...(code?.s ? [{ name: t('code.source'), text: code.s }] : []),
        ...(code?.c ? [{ name: t('code.compiled'), text: code.c }] : []),
    ];
    const label = model.label(id);
    const quoted = /^[\w.]+$/.test(label) ? label : JSON.stringify(label);
</script>

{#if details.length}
    <dl class="details">
        {#each details as [k, v] (k)}<dt>{k}</dt>
            <dd class="mono">{v}</dd>{/each}
    </dl>
{/if}

{#if targets.length}
    <section aria-labelledby="h-targets">
        <h2 id="h-targets">{t('test.checks')}</h2>
        <ul class="plain-list">
            {#each targets as x (x)}<li><NodeLink {viewer} id={x} /></li>{/each}
        </ul>
    </section>
{/if}

{#if tasks.length}
    <section aria-labelledby="h-tasks">
        <h2 id="h-tasks">{t('dag.tasks')} <span class="count">{i18n.number(tasks.length)}</span></h2>
        <div class="table-wrap">
            <table class="data">
                <thead
                    ><tr
                        ><th scope="col">#</th><th scope="col">{t('kind.task')}</th><th scope="col"
                            >{t('detail.operator')}</th
                        ><th scope="col">{t('dag.runs')}</th></tr
                    ></thead
                >
                <tbody>
                    {#each tasks as task, i (task)}
                        {@const node = model.node(task)}
                        <tr>
                            <td class="mono">{i18n.number(i + 1)}</td>
                            <th scope="row"><NodeLink {viewer} id={task} label={node?.name} /></th>
                            <td class="mono">{display(attr(node, 'operator'))}</td>
                            <td
                                >{#each model
                                    .outs(task, 'runs')
                                    .map(e => e.target)
                                    .filter(x => model.byId.has(x)) as r (r)}<NodeLink
                                        {viewer}
                                        id={r}
                                    />{' '}{/each}</td
                            >
                        </tr>
                    {/each}
                </tbody>
            </table>
        </div>
    </section>
{/if}

{#if before.length || after.length || runs.length}
    <section aria-labelledby="h-order">
        <h2 id="h-order">{t('task.order')}</h2>
        <div class="grid three">
            <div>
                <h3>{t('task.after')}</h3>
                {#if before.length}<ul class="plain-list">
                        {#each before as x (x)}<li><NodeLink {viewer} id={x} /></li>{/each}
                    </ul>{:else}<p class="note">{t('task.first')}</p>{/if}
            </div>
            <div>
                <h3>{t('dag.runs')}</h3>
                {#if runs.length}<ul class="plain-list">
                        {#each runs as x (x)}<li>
                                <NodeLink {viewer} id={x} /><span class="grow"></span><span class="note"
                                    >{kindName(model.node(x)?.kind ?? '')}</span
                                >
                            </li>{/each}
                    </ul>{:else}<p class="note">{t('task.runsNothing')}</p>{/if}
            </div>
            <div>
                <h3>{t('task.before')}</h3>
                {#if after.length}<ul class="plain-list">
                        {#each after as x (x)}<li><NodeLink {viewer} id={x} /></li>{/each}
                    </ul>{:else}<p class="note">{t('task.last')}</p>{/if}
            </div>
        </div>
    </section>
{/if}

{#if ups.length || downs.length}
    <section aria-labelledby="h-io">
        <h2 id="h-io">{t('node.dependencies')}</h2>
        <div class="grid two">
            <div>
                <h3>{t('node.dependsOn')}</h3>
                {#if ups.length}<ul class="plain-list">
                        {#each ups as x (x)}<li><NodeLink {viewer} id={x} /></li>{/each}
                    </ul>{:else}<p class="note">{t('node.nothingUpstream')}</p>{/if}
            </div>
            <div>
                <h3>{t('node.referencedBy')}</h3>
                {#if downs.length}<ul class="plain-list">
                        {#each downs as x (x)}<li><NodeLink {viewer} id={x} /></li>{/each}
                    </ul>{:else}<p class="note">{t('node.nothingDownstream')}</p>{/if}
            </div>
        </div>
    </section>
{/if}

{#if command || versions.length}
    <section aria-labelledby="h-tcode">
        <h2 id="h-tcode">{t('node.code')}</h2>
        {#if versions.length}<Code {versions} label={t('node.code')} />{/if}
        {#if command}<Code
                versions={[{ name: t('task.command'), text: command }]}
                label={t('task.command')}
            />{/if}
    </section>
{/if}

{#if n.kind !== 'test' && n.kind !== 'dag'}
    <section aria-labelledby="h-oask">
        <h2 id="h-oask">{t('node.ask')}</h2>
        <div class="commands"><Command text="konstruct impact {quoted}" /></div>
    </section>
{/if}
