<script lang="ts">
    import { t } from '../lib/i18n.svelte';

    let { text, plain = false, label }: { text: string; plain?: boolean; label?: string } = $props();
    let code: HTMLElement | undefined = $state();
    let copied = $state(false);

    function selectText(): void {
        if (!code) return;
        const range = document.createRange();
        range.selectNodeContents(code);
        const selection = window.getSelection();
        selection?.removeAllRanges();
        selection?.addRange(range);
    }

    function copy(): void {
        const done = (): void => {
            copied = true;
            setTimeout(() => (copied = false), 1400);
        };
        // file:// pages are not a secure context, so the clipboard API may be missing: select instead.
        if (navigator.clipboard && window.isSecureContext)
            navigator.clipboard.writeText(text).then(done, selectText);
        else selectText();
    }
</script>

{#if plain}
    <code class="sr-only" bind:this={code}>{text}</code>
    <button type="button" class="copy" onclick={copy}
        ><span aria-live="polite">{copied ? t('code.copied') : (label ?? t('code.copy'))}</span></button
    >
{:else}
    <div class="command">
        <code bind:this={code}>{text}</code>
        <button
            type="button"
            class="copy"
            onclick={copy}
            aria-label={t('code.copyCommand', { command: text })}
            ><span aria-live="polite">{copied ? t('code.copied') : t('code.copy')}</span></button
        >
    </div>
{/if}
