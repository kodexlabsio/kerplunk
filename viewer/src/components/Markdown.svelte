<script lang="ts">
    import { t } from '../lib/i18n.svelte';
    import { cutDescription, parseMarkdown, type Block, type Inline } from '../lib/markdown';

    // Descriptions are Markdown, as in dbt's documentation; they are rendered as elements and
    // text, never as markup. A very long one is cut, as a long SQL file is.
    let { source }: { source: string } = $props();
    const shown = $derived(cutDescription(source));
    const blocks = $derived(parseMarkdown(shown.text));
</script>

{#snippet inlines(
    items: Inline[],
)}{#each items as item, i (i)}{#if item.t === 'text'}{item.v}{:else if item.t === 'code'}<code>{item.v}</code
            >{:else if item.t === 'br'}<br />{:else if item.t === 'em'}<em>{@render inlines(item.c)}</em
            >{:else if item.t === 'strong'}<strong>{@render inlines(item.c)}</strong
            >{:else if item.t === 'link'}<a href={item.href} target="_blank" rel="noopener noreferrer"
                >{@render inlines(item.c)}</a
            >{/if}{/each}{/snippet}

{#snippet render(items: Block[])}
    {#each items as block, i (i)}
        {#if block.t === 'p'}
            <p>{@render inlines(block.c)}</p>
        {:else if block.t === 'h'}
            <!-- Headings in a description sit below the page's own: shown as strong paragraphs. -->
            <p class="md-h md-h{block.level}"><strong>{@render inlines(block.c)}</strong></p>
        {:else if block.t === 'list'}
            {#if block.ordered}
                <ol start={block.start}>
                    {#each block.items as item, j (j)}<li>{@render inlines(item)}</li>{/each}
                </ol>
            {:else}
                <ul>
                    {#each block.items as item, j (j)}<li>{@render inlines(item)}</li>{/each}
                </ul>
            {/if}
        {:else if block.t === 'code'}
            <pre dir="ltr"><code>{block.v}</code></pre>
        {:else if block.t === 'quote'}
            <blockquote>{@render render(block.c)}</blockquote>
        {:else if block.t === 'hr'}
            <hr />
        {:else if block.t === 'table'}
            <div class="md-table">
                <table>
                    <thead>
                        <tr>
                            {#each block.head as cell, j (j)}<th
                                    scope="col"
                                    class="align-{block.align[j] ?? 'start'}">{@render inlines(cell)}</th
                                >{/each}
                        </tr>
                    </thead>
                    <tbody>
                        {#each block.rows as row, r (r)}
                            <tr>
                                {#each row as cell, j (j)}<td class="align-{block.align[j] ?? 'start'}"
                                        >{@render inlines(cell)}</td
                                    >{/each}
                            </tr>
                        {/each}
                    </tbody>
                </table>
            </div>
        {/if}
    {/each}
{/snippet}

<div class="markdown">
    {@render render(blocks)}
    {#if shown.cut}<p class="note">{t('markdown.cut')}</p>{/if}
</div>
