<script lang="ts">
    import type { Trust } from '../lib/world';

    // The trust level's shape, as the world map draws it, so it never rests on colour alone: a tick
    // for verified, a solid ring for declared, a dashed ring for inferred, a warning triangle for a
    // failed check and a thin ring for no rating.
    let { trust, size = 16 }: { trust: Trust; size?: number } = $props();
</script>

<svg
    class="trust-mark t-{trust}"
    viewBox="0 0 16 16"
    width={size}
    height={size}
    aria-hidden="true"
    focusable="false"
>
    {#if trust === 'failed'}
        <path class="fill" d="M8 1.6 15 14H1z" />
        <path class="cut" d="M8 6v3.6M8 11.6v.1" />
    {:else if trust === 'verified'}
        <circle class="fill" cx="8" cy="8" r="7" />
        <path class="cut" d="M4.8 8.2l2.2 2.2 4.2-4.6" />
    {:else}
        <circle class="ring" cx="8" cy="8" r="6" />
    {/if}
</svg>
