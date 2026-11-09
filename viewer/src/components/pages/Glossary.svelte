<script lang="ts">
    import { untrack } from 'svelte';
    import { i18n, t } from '../../lib/i18n.svelte';
    import { attr, strings } from '../../lib/model';
    import type { Owner } from '../../lib/types';
    import type { Viewer } from '../../lib/viewer.svelte';

    // The viewer is made once and never replaced.
    const props: { viewer: Viewer } = $props();
    const viewer = untrack(() => props.viewer);
    // svelte-ignore state_referenced_locally
    const model = viewer.model;
    let filter = $state('');

    const terms = $derived(
        model
            .of('term')
            .filter(n => {
                const q = filter.trim().toLowerCase();
                return (
                    !q ||
                    `${n.name} ${strings(attr(n, 'synonyms')).join(' ')} ${n.description ?? ''}`
                        .toLowerCase()
                        .includes(q)
                );
            })
            .sort((a, b) => i18n.compare(a.name, b.name)),
    );
    // Grouped by first letter, in the locale's alphabet.
    const groups = $derived.by(() => {
        const out: [string, typeof terms][] = [];
        for (const term of terms) {
            const letter = term.name.charAt(0).toLocaleUpperCase(i18n.locale);
            const last = out[out.length - 1];
            if (last && i18n.compare(last[0], letter) === 0) last[1].push(term);
            else out.push([letter, [term]]);
        }
        return out;
    });

    function owner(o: Owner | string | undefined): string {
        if (!o) return '';
        return typeof o === 'string' ? o : o.role ? `${o.name}, ${o.role}` : o.name;
    }
</script>

<article class="page glossary">
    <header class="page-head">
        <h1 tabindex="-1">{t('page.glossary')}</h1>
        <p class="lead">{t('glossary.lead', { count: model.of('term').length })}</p>
        <label class="inline-filter">
            <span class="sr-only">{t('glossary.filter')}</span>
            <input type="search" placeholder={t('glossary.filter')} bind:value={filter} />
        </label>
    </header>
    {#if groups.length > 3}
        <nav class="letters" aria-label={t('glossary.letters')}>
            {#each groups as [letter] (letter)}<button
                    type="button"
                    class="link"
                    onclick={() => document.getElementById(`letter-${letter}`)?.scrollIntoView()}
                    >{letter}</button
                >{/each}
        </nav>
    {/if}
    {#each groups as [letter, list] (letter)}
        <section aria-labelledby="letter-{letter}">
            <h2 id="letter-{letter}" class="letter">{letter}</h2>
            <dl class="glossary-list">
                {#each list as term (term.id)}
                    <div class="entry">
                        <dt>
                            <a href={viewer.href({ page: 'node', id: term.id })}>{term.name}</a>
                            <span class="badge status-{viewer.status(term)}"
                                >{t(`status.${viewer.status(term)}` as never)}</span
                            >
                        </dt>
                        <dd>
                            {#if term.description}<p>{term.description}</p>{/if}
                            <p class="note">
                                {#if strings(attr(term, 'synonyms')).length}{t('glossary.also', {
                                        names: strings(attr(term, 'synonyms')).join(', '),
                                    })}{' '}{/if}
                                {#if owner(attr(term, 'owner'))}{t('glossary.owner', {
                                        owner: owner(attr(term, 'owner')),
                                    })}{' '}{/if}
                                {t('glossary.bound', { count: model.boundTo(term.id).length })}
                            </p>
                        </dd>
                    </div>
                {/each}
            </dl>
        </section>
    {/each}
    {#if !terms.length}<p class="note">
            {model.of('term').length ? t('nav.nothing') : t('glossary.none')}
        </p>{/if}
</article>
