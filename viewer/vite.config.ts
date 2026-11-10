import { svelte } from '@sveltejs/vite-plugin-svelte';
import type { Plugin } from 'vite';
import { viteSingleFile } from 'vite-plugin-singlefile';
import { defineConfig } from 'vitest/config';

/**
 * A Content-Security-Policy for the page, allowing its one inline script and one inline style
 * by their hashes: no 'unsafe-inline', no eval, nothing from the network but the serving
 * Konstruct's own address. The JSON the build fills in is data, not script, so filling it in
 * does not change the hashes.
 */
function contentSecurityPolicy(): Plugin {
    // Web Crypto, which Node also has, so the config needs no Node type definitions.
    const hash = async (text: string): Promise<string> => {
        const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text)));
        let binary = '';
        for (const byte of digest) binary += String.fromCharCode(byte);
        return `'sha256-${btoa(binary)}'`;
    };
    return {
        name: 'konstruct-csp',
        enforce: 'post',
        generateBundle: {
            order: 'post',
            async handler(_, bundle) {
                for (const file of Object.values(bundle)) {
                    if (file.type !== 'asset' || !file.fileName.endsWith('.html')) continue;
                    const html = String(file.source);
                    const modules = [...html.matchAll(/<script type="module"[^>]*>([\s\S]*?)<\/script>/g)];
                    // Style elements outside the script: the script itself writes <style> into exports.
                    const rest = modules.reduce((text, m) => text.replace(m[0], ''), html);
                    const scripts = await Promise.all(modules.map(m => hash(m[1]!)));
                    const styles = await Promise.all(
                        [...rest.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map(m => hash(m[1]!)),
                    );
                    const policy = [
                        "default-src 'none'",
                        `script-src ${scripts.join(' ')}`,
                        `style-src ${styles.join(' ')}`,
                        'font-src data:',
                        'img-src data:',
                        "connect-src 'self'",
                        "base-uri 'none'",
                        "form-action 'none'",
                        "object-src 'none'",
                    ].join('; ');
                    file.source = html.replace(
                        '<meta charset="utf-8" />',
                        `<meta charset="utf-8" />\n        <meta http-equiv="Content-Security-Policy" content="${policy}" />`,
                    );
                }
            },
        },
    };
}

// One self-contained HTML file: script, styles and fonts inlined, nothing loaded from the network.
// It is written into the Python package, where konstruct fills the data placeholders.
export default defineConfig({
    plugins: [svelte(), viteSingleFile({ removeViteModuleLoader: true }), contentSecurityPolicy()],
    build: {
        // Writes ../konstruct/outputs/viewer.html: the entry's name, in the package directory.
        outDir: '../konstruct/outputs',
        rollupOptions: { input: 'viewer.html' },
        emptyOutDir: false,
        assetsInlineLimit: Number.MAX_SAFE_INTEGER,
        cssCodeSplit: false,
        target: 'es2022',
        reportCompressedSize: false,
    },
    test: {
        environment: 'node',
        projects: [
            {
                extends: true,
                test: {
                    name: 'unit',
                    include: ['tests/**/*.test.ts'],
                    exclude: ['tests/**/*.timing.test.ts'],
                },
            },
            // Timings are compared within one process that has the processor to itself.
            {
                extends: true,
                test: {
                    name: 'timing',
                    include: ['tests/**/*.timing.test.ts'],
                    pool: 'forks',
                    maxWorkers: 1,
                    // A later group: it starts once the unit tests have finished.
                    sequence: { groupOrder: 1 },
                },
            },
        ],
    },
});
