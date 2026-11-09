<script lang="ts">
    import { untrack } from 'svelte';
    import { t } from '../../lib/i18n.svelte';
    import type { Viewer } from '../../lib/viewer.svelte';

    // The viewer is made once and never replaced.
    const props: { viewer: Viewer } = $props();
    const viewer = untrack(() => props.viewer);
    // svelte-ignore state_referenced_locally
    const warnings = viewer.data.warnings ?? [];
    let filter = $state('');
    const shown = $derived(warnings.filter(w => w.toLowerCase().includes(filter.trim().toLowerCase())));
</script>

<article class="page warnings">
    <header class="page-head">
        <h1 tabindex="-1">{t('page.warnings')}</h1>
        <p class="lead">
            {warnings.length ? t('warnings.lead', { count: warnings.length }) : t('warnings.none')}
        </p>
        {#if warnings.length > 10}
            <label class="inline-filter"
                ><span class="sr-only">{t('warnings.filter')}</span><input
                    type="search"
                    placeholder={t('warnings.filter')}
                    bind:value={filter}
                /></label
            >
        {/if}
    </header>
    {#if shown.length}
        <ol class="warning-list" id="warnings">
            {#each shown as w, i (i)}<li>{w}</li>{/each}
        </ol>
    {/if}
</article>
