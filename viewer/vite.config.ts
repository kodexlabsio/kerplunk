import { svelte } from '@sveltejs/vite-plugin-svelte';
import { viteSingleFile } from 'vite-plugin-singlefile';
import { defineConfig } from 'vitest/config';

// One self-contained HTML file: script, styles and fonts inlined, nothing loaded from the network.
// It is written into the Python package, where konstruct fills the data placeholders.
export default defineConfig({
    plugins: [svelte(), viteSingleFile({ removeViteModuleLoader: true })],
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
        include: ['tests/**/*.test.ts'],
        environment: 'node',
    },
});
