<script lang="ts">
    import { untrack } from 'svelte';
    import { i18n, LOCALES, t } from '../lib/i18n.svelte';
    import { kindName } from '../lib/labels';
    import type { Viewer } from '../lib/viewer.svelte';
    import Icon from './Icon.svelte';

    // The viewer is made once and never replaced.
    const props: { viewer: Viewer } = $props();
    const viewer = untrack(() => props.viewer);
    // svelte-ignore state_referenced_locally
    const model = viewer.model;

    interface Crumb {
        text: string;
        href?: string;
    }

    const crumbs: Crumb[] = $derived.by(() => {
        const r = viewer.route;
        const list: Crumb[] = [];
        if (r.page === 'node' && r.id) {
            const n = model.node(r.id);
            if (n) {
                if (n.kind === 'column' && n.parent) {
                    const parent = model.node(n.parent);
                    if (parent)
                        list.push({
                            text: kindName(parent.kind),
                            href: viewer.href({ page: 'browse', kind: parent.kind }),
                        });
                    list.push({
                        text: model.label(n.parent),
                        href: viewer.href({ page: 'node', id: n.parent }),
                    });
                    list.push({ text: n.name });
                } else if (n.kind === 'term') {
                    list.push({ text: t('page.glossary'), href: '#/glossary' }, { text: n.name });
                } else {
                    list.push({
                        text: kindName(n.kind),
                        href: n.kind === 'test' ? '#/tests' : viewer.href({ page: 'browse', kind: n.kind }),
                    });
                    const pkg = model.packageOf(n);
                    if (pkg && n.kind !== 'task')
                        list.push({ text: pkg, href: viewer.href({ page: 'browse', package: pkg }) });
                    list.push({ text: n.name });
                }
            }
        } else if (r.page === 'entity' && r.id)
            list.push({ text: t('page.ontology'), href: '#/ontology' }, { text: r.id });
        else if (r.page !== 'overview') list.push({ text: t(`page.${r.page}`) });
        return list;
    });
</script>

<header id="topbar">
    <button
        type="button"
        class="icon-button menu"
        aria-label={t('nav.toggle')}
        aria-expanded={viewer.navOpen}
        aria-controls="nav"
        onclick={() => (viewer.navOpen = !viewer.navOpen)}><Icon kind="menu" /></button
    >
    <a
        class="brand"
        href="#/"
        aria-label={t('app.home', { project: viewer.projectName() || t('app.graph') })}
    >
        <span class="mark" aria-hidden="true">&#x276F;</span>
        <span class="word">Konstruct</span>
        {#if viewer.projectName()}<span class="project">{viewer.projectName()}</span>{/if}
    </a>
    <nav class="crumbs" aria-label={t('app.breadcrumbs')}>
        <ol>
            {#each crumbs as crumb, i (i)}
                <li>
                    {#if crumb.href}<a href={crumb.href}>{crumb.text}</a>{:else}<span aria-current="page"
                            >{crumb.text}</span
                        >{/if}
                </li>
            {/each}
        </ol>
    </nav>
    <button
        type="button"
        id="search"
        class="search-button"
        onclick={() => (viewer.paletteOpen = true)}
        aria-haspopup="dialog"
        aria-keyshortcuts="Control+K /"
    >
        <Icon kind="search" />
        <span class="placeholder">{t('search.button')}</span>
        <kbd aria-hidden="true">Ctrl K</kbd>
    </button>
    <div class="tools">
        <a
            class="icon-button"
            id="open-lineage"
            href={viewer.href({ page: 'lineage' })}
            title={t('lineage.open')}
            aria-label={t('lineage.open')}
            aria-current={viewer.route.page === 'lineage' ? 'page' : undefined}><Icon kind="lineage" /></a
        >
        <label class="language">
            <span class="sr-only">{t('app.language')}</span>
            <Icon kind="language" />
            <select
                id="language"
                value={i18n.locale}
                onchange={event => i18n.pick((event.target as HTMLSelectElement).value)}
            >
                {#each LOCALES as locale (locale)}
                    <option value={locale} lang={locale}>{i18n.name(locale)}</option>
                {/each}
            </select>
        </label>
        <button
            type="button"
            class="icon-button"
            id="theme"
            title={viewer.theme === 'dark' ? t('app.lightTheme') : t('app.darkTheme')}
            aria-label={viewer.theme === 'dark' ? t('app.lightTheme') : t('app.darkTheme')}
            onclick={() => viewer.setTheme(viewer.theme === 'dark' ? 'light' : 'dark')}
            ><Icon kind={viewer.theme === 'dark' ? 'sun' : 'moon'} /></button
        >
        <button
            type="button"
            class="icon-button"
            id="help"
            title={t('help.title')}
            aria-label={t('help.title')}
            aria-keyshortcuts="?"
            onclick={() => (viewer.helpOpen = true)}><Icon kind="keyboard" /></button
        >
    </div>
</header>
