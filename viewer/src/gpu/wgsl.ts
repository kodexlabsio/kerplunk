// WGSL is shipped as text inside the page. Comments and spare whitespace cost bytes and nothing else,
// so the build removes them (see the wgsl plugin in vite.config.ts).

/** Punctuation that never needs a space beside it. "-", "<" and ">" keep theirs: "- -1", ">>". */
const TIGHT = /\s*([{}()[\];,:=+*/&|!@.])\s*/g;

export function minifyWgsl(source: string): string {
    return source
        .replace(/\/\*[\s\S]*?\*\//g, ' ')
        .replace(/\/\/[^\n]*/g, ' ')
        .replace(/\s+/g, ' ')
        .replace(TIGHT, '$1')
        .trim();
}
