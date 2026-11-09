<script lang="ts">
    import { onMount, tick, untrack } from 'svelte';
    import { i18n, t } from '../lib/i18n.svelte';
    import { kindName } from '../lib/labels';
    import { attr } from '../lib/model';
    import type { MessageKey } from '../locales/en-GB';
    import { search, type Hit } from '../lib/search';
    import type { Viewer } from '../lib/viewer.svelte';
    import Icon from './Icon.svelte';

    // The viewer is made once and never replaced.
    const props: { viewer: Viewer } = $props();
    const viewer = untrack(() => props.viewer);
    // svelte-ignore state_referenced_locally
    const model = viewer.model;

    let input: HTMLInputElement | undefined = $state();
    let list: HTMLElement | undefined = $state();
    let active = $state(0);
    let kind = $state('');
    let returnTo: HTMLElement | null = null;

    // Kinds the graph has, for the filter chips.
    const kinds = [
        'model',
        'source',
        'seed',
        'snapshot',
        'table',
        'column',
        'task',
        'dag',
        'exposure',
        'test',
        'term',
    ].filter(k => model.of(k as never).length > 0);
    if ((viewer.ontology.entities?.length ?? 0) > 0) kinds.push('entity');

    interface Action {
        key: MessageKey;
        icon: string;
        run: () => void;
    }

    const actions: Action[] = [
        { key: 'page.overview', icon: 'overview', run: () => viewer.go({ page: 'overview' }) },
        { key: 'lineage.open', icon: 'lineage', run: () => viewer.go({ page: 'lineage' }) },
        { key: 'page.browse', icon: 'list', run: () => viewer.go({ page: 'browse' }) },
        { key: 'page.glossary', icon: 'term', run: () => viewer.go({ page: 'glossary' }) },
        { key: 'page.ontology', icon: 'entity', run: () => viewer.go({ page: 'ontology' }) },
        { key: 'page.conflicts', icon: 'conflicts', run: () => viewer.go({ page: 'conflicts' }) },
        { key: 'page.tests', icon: 'test', run: () => viewer.go({ page: 'tests' }) },
        { key: 'page.warnings', icon: 'warnings', run: () => viewer.go({ page: 'warnings' }) },
        {
            key: 'app.switchTheme',
            icon: 'moon',
            run: () => viewer.setTheme(viewer.theme === 'dark' ? 'light' : 'dark'),
        },
        { key: 'help.title', icon: 'keyboard', run: () => (viewer.helpOpen = true) },
    ];

    const commandMode = $derived(viewer.paletteQuery.startsWith('>'));
    const result = $derived(
        commandMode
            ? null
            : search(viewer.index, viewer.paletteQuery, kind ? { kinds: [kind] } : {}, i18n.compare),
    );
    const commands = $derived.by(() => {
        if (!commandMode) return [];
        const q = viewer.paletteQuery.slice(1).trim().toLowerCase();
        return actions.filter(a => t(a.key).toLowerCase().includes(q));
    });
    const count = $derived(commandMode ? commands.length : (result?.hits.length ?? 0));

    $effect(() => {
        void viewer.paletteQuery;
        void kind;
        active = 0;
    });

    onMount(() => {
        returnTo = document.activeElement as HTMLElement | null;
        void tick().then(() => {
            input?.focus();
            input?.select();
        });
        return () => {
            if (returnTo?.isConnected) returnTo.focus();
        };
    });

    function close(): void {
        viewer.paletteOpen = false;
    }

    function choose(index: number, lineage = false): void {
        if (commandMode) {
            const action = commands[index];
            close();
            action?.run();
            return;
        }
        const hit = result?.hits[index];
        if (!hit) return;
        close();
        const id = hit.entry.id;
        if (lineage && !id.startsWith('entity:'))
            viewer.go({ page: 'lineage', focus: id, select: undefined });
        else viewer.open(id);
    }

    function move(delta: number): void {
        if (!count) return;
        active = (active + delta + count) % count;
        void tick().then(() =>
            document.getElementById(`option-${active}`)?.scrollIntoView({ block: 'nearest' }),
        );
    }

    function onkeydown(event: KeyboardEvent): void {
        if (event.key === 'ArrowDown') move(1);
        else if (event.key === 'ArrowUp') move(-1);
        else if (event.key === 'PageDown') move(8);
        else if (event.key === 'PageUp') move(-8);
        else if (event.key === 'Enter') choose(active, event.altKey || event.shiftKey);
        else if (event.key === 'Escape') close();
        else if (event.key === 'Tab' && !event.shiftKey && !commandMode) {
            // Tab cycles the kind filter, as in many command palettes; Shift+Tab leaves it.
            const all = ['', ...kinds];
            kind = all[(all.indexOf(kind) + 1) % all.length]!;
        } else return;
        event.preventDefault();
    }

    /** The label split into matched and unmatched runs, for highlighting as text. */
    function pieces(hit: Hit): { text: string; mark: boolean }[] {
        const marks = new Set(hit.marks);
        const out: { text: string; mark: boolean }[] = [];
        for (let i = 0; i < hit.entry.label.length; i++) {
            const mark = marks.has(i);
            const last = out[out.length - 1];
            if (last && last.mark === mark) last.text += hit.entry.label[i];
            else out.push({ text: hit.entry.label[i]!, mark });
        }
        return out;
    }

    function context(hit: Hit): string {
        const n = model.node(hit.entry.id);
        if (!n) return '';
        if (n.kind === 'column') return n.data_type?.toLowerCase() ?? '';
        if (n.kind === 'term') return t(`status.${viewer.status(n)}` as MessageKey);
        return n.relation ?? String(attr(n, 'operator') ?? '') ?? '';
    }

    function iconFor(hit: Hit): string {
        return hit.entry.kind;
    }
</script>

<div class="overlay" role="presentation" onclick={event => event.target === event.currentTarget && close()}>
    <div id="palette" role="dialog" aria-modal="true" aria-labelledby="palette-title">
        <h2 id="palette-title" class="sr-only">{t('search.title')}</h2>
        <div class="palette-input">
            <Icon kind="search" />
            <input
                id="palette-input"
                bind:this={input}
                bind:value={viewer.paletteQuery}
                type="text"
                role="combobox"
                aria-expanded={count > 0}
                aria-controls="results"
                aria-autocomplete="list"
                aria-activedescendant={count ? `option-${active}` : undefined}
                aria-describedby="palette-hint"
                placeholder={t('search.placeholder')}
                autocomplete="off"
                spellcheck="false"
                {onkeydown}
            />
            <kbd aria-hidden="true">Esc</kbd>
        </div>
        {#if !commandMode}
            <div class="chips" role="group" aria-label={t('search.kinds')}>
                <button type="button" class="chip" aria-pressed={kind === ''} onclick={() => (kind = '')}
                    >{t('search.all')}</button
                >
                {#each kinds as k (k)}
                    <button
                        type="button"
                        class="chip"
                        data-kind={k}
                        aria-pressed={kind === k}
                        onclick={() => (kind = kind === k ? '' : k)}
                    >
                        <Icon kind={k} size={16} />{kindName(k)}
                    </button>
                {/each}
            </div>
        {/if}
        <div id="match-count" class="match-count" role="status" aria-live="polite">
            {#if commandMode}{t('search.commands', {
                    count: commands.length,
                })}{:else if result && (viewer.paletteQuery.trim() || kind)}{result.total
                    ? t('search.matches', { count: result.total, shown: result.hits.length })
                    : t('search.none')}{/if}
        </div>
        <ul id="results" role="listbox" aria-label={t('search.results')} bind:this={list}>
            {#if commandMode}
                {#each commands as action, i (action.key)}
                    <!-- svelte-ignore a11y_click_events_have_key_events: the combobox input handles the keys. -->
                    <li
                        id="option-{i}"
                        role="option"
                        aria-selected={i === active}
                        class:active={i === active}
                        onclick={() => choose(i)}
                        onmousemove={() => (active = i)}
                    >
                        <Icon kind={action.icon} />
                        <span class="label">{t(action.key)}</span>
                    </li>
                {/each}
            {:else if result}
                {#each result.hits as hit, i (hit.entry.id)}
                    <!-- svelte-ignore a11y_click_events_have_key_events: the combobox input handles the keys. -->
                    <li
                        id="option-{i}"
                        role="option"
                        aria-selected={i === active}
                        class:active={i === active}
                        onclick={event => choose(i, event.altKey || event.shiftKey)}
                        onmousemove={() => (active = i)}
                    >
                        <Icon kind={iconFor(hit)} />
                        <span class="label"
                            >{#each pieces(hit) as piece, j (j)}{#if piece.mark}<mark>{piece.text}</mark
                                    >{:else}{piece.text}{/if}{/each}</span
                        >
                        <span class="context">{context(hit)}</span>
                        <span class="kind">{kindName(hit.entry.kind)}</span>
                    </li>
                {/each}
            {/if}
        </ul>
        {#if !viewer.paletteQuery.trim() && !kind}
            <p class="palette-empty">{t('search.empty')}</p>
        {/if}
        <p id="palette-hint" class="palette-hint">{t('search.hint')}</p>
    </div>
</div>
