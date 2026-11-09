<script lang="ts">
    import { t } from '../../lib/i18n.svelte';
    import type { Viewer } from '../../lib/viewer.svelte';

    let { viewer, what }: { viewer: Viewer; what: string } = $props();
</script>

<article class="page">
    <header class="page-head">
        <h1 tabindex="-1">{t('notFound.title')}</h1>
        <p class="lead">{t('notFound.body', { id: what })}</p>
        <p><a href={viewer.href({ page: 'overview' })}>{t('notFound.back')}</a></p>
    </header>
</article>
