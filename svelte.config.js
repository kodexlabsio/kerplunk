import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { fileURLToPath } from 'node:url';

export default {
    preprocess: vitePreprocess(),
    // Svelte names each component's scoped styles after its path relative to rootDir, which is the
    // working directory unless set. The viewer's own directory keeps those names, and so the built
    // page, independent of where the toolchain runs from.
    compilerOptions: { rootDir: fileURLToPath(new URL('./viewer', import.meta.url)) },
};
