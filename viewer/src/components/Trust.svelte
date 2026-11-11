<script lang="ts">
    import { t } from '../lib/i18n.svelte';
    import type { Trust } from '../lib/world';
    import TrustMark from './TrustMark.svelte';

    // A trust level as the badges elsewhere are drawn: its shape and its name, with what it means on hover.
    let { trust }: { trust: Trust } = $props();
</script>

<span
    class="badge trust t-{trust}"
    class:ok={trust === 'verified'}
    class:warn={trust === 'failed'}
    class:quiet={trust === 'inferred' || trust === 'unrated'}
    title={t(`trust.${trust}Help`)}><TrustMark {trust} />{t(`trust.${trust}`)}</span
>
