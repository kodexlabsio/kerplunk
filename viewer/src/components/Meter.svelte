<script lang="ts">
    import { i18n } from '../lib/i18n.svelte';

    let { label, done, total, detail }: { label: string; done: number; total: number; detail?: string } =
        $props();
    const share = $derived(total ? done / total : 0);
    const id = `meter-${Math.random().toString(36).slice(2, 8)}`;
</script>

<div class="meter">
    <div class="meter-head">
        <span class="meter-label" {id}>{label}</span>
        <span class="meter-value">{total ? i18n.number(share, 'percent') : ''}</span>
    </div>
    <div
        class="meter-track"
        role="meter"
        aria-labelledby={id}
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuenow={done}
        aria-valuetext={detail}
    >
        <div class="meter-fill" style="inline-size: {share * 100}%"></div>
    </div>
    {#if detail}<p class="meter-detail">{detail}</p>{/if}
</div>
