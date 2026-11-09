<script lang="ts">
    import { t } from '../lib/i18n.svelte';
    import { attr } from '../lib/model';
    import type { Viewer } from '../lib/viewer.svelte';

    let { viewer, id }: { viewer: Viewer; id: string } = $props();

    // dbt docs' one-letter badges, with the full name for screen readers and on hover.
    const LETTERS: Record<string, string> = {
        unique: 'U',
        not_null: 'N',
        accepted_values: 'A',
        relationships: 'R',
    };
    const tests = $derived(
        viewer.model
            .ins(id, 'tests')
            .map(e => viewer.model.node(e.source))
            .filter(n => n !== undefined),
    );
</script>

{#if tests.length}
    <ul class="test-badges">
        {#each tests as test (test.id)}
            {@const kind = String(attr(test, 'test') ?? '')}
            <li>
                <a class="test-badge" href={viewer.href({ page: 'node', id: test.id })} title={test.name}
                    ><span aria-hidden="true">{LETTERS[kind] ?? kind.charAt(0).toUpperCase() ?? 'T'}</span
                    ><span class="sr-only">{t('test.named', { name: kind || test.name })}</span></a
                >
            </li>
        {/each}
    </ul>
{/if}
