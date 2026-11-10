<script lang="ts">
    import { onMount, tick, untrack } from 'svelte';
    import { t } from '../lib/i18n.svelte';
    import type { MessageKey } from '../locales/en-GB';
    import type { Viewer } from '../lib/viewer.svelte';

    // The viewer is made once and never replaced.
    const props: { viewer: Viewer } = $props();
    const viewer = untrack(() => props.viewer);
    let close: HTMLButtonElement | undefined = $state();
    let dialog: HTMLElement | undefined = $state();

    const groups: [MessageKey, [string, MessageKey][]][] = [
        [
            'help.everywhere',
            [
                [
                    `${/Mac|iPhone|iPad/.test(navigator.platform ?? '') ? '\u2318 K' : 'Ctrl K'}, /`,
                    'help.search',
                ],
                ['L', 'help.dock'],
                ['Shift L', 'help.fullScreen'],
                ['?', 'help.shortcuts'],
                ['Esc', 'help.escape'],
            ],
        ],
        [
            'help.graph',
            [
                ['←  →', 'help.graphSides'],
                ['↑  ↓', 'help.graphLayer'],
                ['Enter', 'help.graphOpen'],
                ['Space', 'help.graphHighlight'],
                ['E', 'help.graphExpand'],
                ['F', 'help.graphFit'],
                ['C', 'help.graphColumns'],
                ['+  -', 'help.graphZoom'],
            ],
        ],
        [
            'help.lists',
            [
                ['↑  ↓', 'help.listMove'],
                ['→  ←', 'help.treeOpen'],
                ['Enter', 'help.listOpen'],
                ['Tab', 'help.paletteKinds'],
            ],
        ],
    ];

    let returnTo: HTMLElement | null = null;
    onMount(() => {
        returnTo = document.activeElement as HTMLElement | null;
        void tick().then(() => close?.focus());
        return () => returnTo?.isConnected && returnTo.focus();
    });

    function onkeydown(event: KeyboardEvent): void {
        if (event.key === 'Escape') {
            event.preventDefault();
            viewer.helpOpen = false;
        } else if (event.key === 'Tab') {
            // Keep focus inside the dialog: it has one control.
            event.preventDefault();
            close?.focus();
        }
    }
</script>

<div
    class="overlay"
    role="presentation"
    onclick={event => event.target === event.currentTarget && (viewer.helpOpen = false)}
>
    <div
        id="help-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="help-title"
        tabindex="-1"
        bind:this={dialog}
        {onkeydown}
    >
        <header>
            <h2 id="help-title">{t('help.title')}</h2>
            <button
                type="button"
                class="icon-button"
                bind:this={close}
                aria-label={t('app.close')}
                onclick={() => (viewer.helpOpen = false)}>&#xD7;</button
            >
        </header>
        {#each groups as [title, keys] (title)}
            <h3>{t(title)}</h3>
            <dl class="keys">
                {#each keys as [key, text] (text)}
                    <dt><kbd>{key}</kbd></dt>
                    <dd>{t(text)}</dd>
                {/each}
            </dl>
        {/each}
        <p class="note">{t('help.selectors')}</p>
    </div>
</div>
