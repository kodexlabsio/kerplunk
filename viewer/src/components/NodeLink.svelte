<script lang="ts">
    import type { Viewer } from '../lib/viewer.svelte';
    import Icon from './Icon.svelte';

    let {
        viewer,
        id,
        label,
        icon = true,
    }: { viewer: Viewer; id: string; label?: string; icon?: boolean } = $props();
    const n = $derived(viewer.model.node(id));
</script>

{#if n}
    <a class="node-link" href={viewer.href({ page: 'node', id })} title={id}
        >{#if icon}<Icon kind={n.kind} size={16} />{/if}<span
            >{label ?? (n.kind === 'term' ? n.name : viewer.model.label(id))}</span
        ></a
    >
{:else}
    <span class="mono">{label ?? id}</span>
{/if}
