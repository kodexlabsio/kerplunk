<script lang="ts">
    import { untrack } from 'svelte';
    import type { Viewer } from '../lib/viewer.svelte';
    import Browse from './pages/Browse.svelte';
    import Conflicts from './pages/Conflicts.svelte';
    import Entity from './pages/Entity.svelte';
    import Glossary from './pages/Glossary.svelte';
    import MapPage from './pages/MapPage.svelte';
    import NodePage from './pages/NodePage.svelte';
    import NotFound from './pages/NotFound.svelte';
    import Ontology from './pages/Ontology.svelte';
    import Overview from './pages/Overview.svelte';
    import Tests from './pages/Tests.svelte';
    import Usage from './pages/Usage.svelte';
    import Warnings from './pages/Warnings.svelte';

    // The viewer is made once and never replaced.
    const props: { viewer: Viewer } = $props();
    const viewer = untrack(() => props.viewer);
    const r = $derived(viewer.route);
</script>

{#if r.page === 'overview'}
    <Overview {viewer} />
{:else if r.page === 'node'}
    {#if r.id && viewer.model.node(r.id)}
        {#key r.id}<NodePage {viewer} id={r.id} />{/key}
    {:else}
        <NotFound {viewer} what={r.id ?? ''} />
    {/if}
{:else if r.page === 'browse'}
    <Browse {viewer} />
{:else if r.page === 'glossary'}
    <Glossary {viewer} />
{:else if r.page === 'ontology'}
    <Ontology {viewer} />
{:else if r.page === 'entity'}
    {#key r.id}<Entity {viewer} name={r.id ?? ''} />{/key}
{:else if r.page === 'conflicts'}
    <Conflicts {viewer} />
{:else if r.page === 'usage'}
    <Usage {viewer} />
{:else if r.page === 'tests'}
    <Tests {viewer} />
{:else if r.page === 'warnings'}
    <Warnings {viewer} />
{:else if r.page === 'map'}
    <MapPage {viewer} />
{:else}
    <NotFound {viewer} what={r.id ?? ''} />
{/if}
