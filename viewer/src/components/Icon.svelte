<script lang="ts">
    import { ICONS } from '../lib/icons';

    let { kind, size = 18 }: { kind: string; size?: number } = $props();
</script>

<svg
    class="icon"
    viewBox="0 0 24 24"
    width={size}
    height={size}
    aria-hidden="true"
    focusable="false"
    fill="none"
    stroke="currentColor"
    stroke-width="1.8"
    stroke-linecap="round"
    stroke-linejoin="round"
>
    <path d={ICONS[kind] ?? 'M5 12h14'} />
</svg>
