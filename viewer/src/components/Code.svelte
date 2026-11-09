<script lang="ts">
    import { t } from '../lib/i18n.svelte';
    import { lines } from '../lib/sql';
    import Command from './Command.svelte';

    let { versions, label }: { versions: { name: string; text: string }[]; label: string } = $props();
    let chosen = $state(0);
    const current = $derived(versions[Math.min(chosen, versions.length - 1)]);
    const rows = $derived(current ? lines(current.text) : []);
    const id = `code-${Math.random().toString(36).slice(2, 8)}`;
</script>

{#if current}
    <div class="code">
        <div class="code-bar">
            {#if versions.length > 1}
                <div class="segmented" role="tablist" aria-label={label}>
                    {#each versions as v, i (v.name)}
                        <button
                            type="button"
                            role="tab"
                            id="{id}-tab-{i}"
                            aria-selected={i === chosen}
                            aria-controls="{id}-panel"
                            tabindex={i === chosen ? 0 : -1}
                            onclick={() => (chosen = i)}
                            onkeydown={event => {
                                if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
                                    event.preventDefault();
                                    chosen =
                                        (chosen + (event.key === 'ArrowRight' ? 1 : -1) + versions.length) %
                                        versions.length;
                                    document.getElementById(`${id}-tab-${chosen}`)?.focus();
                                }
                            }}>{v.name}</button
                        >
                    {/each}
                </div>
            {:else}
                <span class="code-name">{current.name}</span>
            {/if}
            <span class="grow"></span>
            <Command text={current.text} plain label={t('code.copy')} />
        </div>
        <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
        <pre
            id="{id}-panel"
            role={versions.length > 1 ? 'tabpanel' : undefined}
            aria-labelledby={versions.length > 1 ? `${id}-tab-${chosen}` : undefined}
            aria-label={versions.length > 1 ? undefined : label}
            tabindex="0"
            dir="ltr"><code
                >{#each rows as row, i (i)}<span class="line"
                        ><span class="ln" aria-hidden="true">{i + 1}</span>{#each row as token, j (j)}<span
                                class="tk-{token.kind}">{token.text}</span
                            >{/each}{'\n'}</span
                    >{/each}</code
            ></pre>
    </div>
{/if}
