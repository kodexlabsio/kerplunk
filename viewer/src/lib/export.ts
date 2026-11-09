// The graph as a standalone SVG or PNG file: the drawn cards and edges, with the styles they use
// resolved to plain values and the fonts embedded, so the file looks the same anywhere.

import type { Bounds } from './layout';

const GRAPH_RULE = /(^|[\s,>])(\.node|\.edge|\.row|\.card|\.handle|\.lod|\.cursor|#arrow|svg)\b/;

/** Replace var(--name) with the value ``resolve`` gives, keeping any fallback when it has none. */
export function resolveVars(css: string, resolve: (name: string) => string): string {
    let out = css;
    // Nested var() fallbacks are rare in the viewer's styles; a few passes resolve them.
    for (let i = 0; i < 4 && out.includes('var('); i++) {
        out = out.replace(
            /var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*))?\)/g,
            (_, name: string, fallback?: string) => {
                const value = resolve(name).trim();
                return value || (fallback ?? '').trim();
            },
        );
    }
    return out;
}

/** The document's style rules for the graph, and its font faces. */
export function collectStyles(sheets: Iterable<CSSStyleSheet>, resolve: (name: string) => string): string {
    const parts: string[] = [];
    for (const sheet of sheets) {
        let rules: CSSRuleList;
        try {
            rules = sheet.cssRules;
        } catch {
            continue;
        }
        for (const rule of Array.from(rules)) {
            if (rule.constructor.name === 'CSSFontFaceRule' || rule.cssText.startsWith('@font-face'))
                parts.push(rule.cssText);
            else if ('selectorText' in rule && GRAPH_RULE.test(String((rule as CSSStyleRule).selectorText)))
                parts.push(rule.cssText);
        }
    }
    return resolveVars(parts.join('\n'), resolve);
}

export function escapeXml(text: string): string {
    return text.replace(
        /[<>&"']/g,
        c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&apos;' })[c]!,
    );
}

/** A standalone SVG document around the drawn graph. */
export function svgDocument(
    inner: string,
    defs: string,
    box: Bounds,
    css: string,
    background: string,
    title: string,
    pad = 40,
): string {
    const w = Math.ceil(box.w + 2 * pad);
    const h = Math.ceil(box.h + 2 * pad);
    return (
        `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="${escapeXml(title)}">` +
        `<title>${escapeXml(title)}</title>` +
        `<style>${css.replace(/<\/style/gi, '<\\/style')}</style>` +
        `<rect width="100%" height="100%" fill="${escapeXml(background)}"/>` +
        `<defs>${defs}</defs>` +
        `<g transform="translate(${pad - box.x},${pad - box.y})">${inner}</g></svg>`
    );
}

/** The scale for a PNG of ``w`` by ``h``: 2x, smaller when a browser canvas could not hold it. */
export function pngScale(w: number, h: number, wanted = 2, maxSide = 16000, maxArea = 120_000_000): number {
    return Math.max(0.05, Math.min(wanted, maxSide / w, maxSide / h, Math.sqrt(maxArea / (w * h))));
}

export function fileName(project: string, extension: string): string {
    const base = (project || 'lineage').replace(/[^\w.-]+/g, '-').replace(/^-+|-+$/g, '') || 'lineage';
    return `${base}-lineage.${extension}`;
}

/** Save a data URL as a file, without leaving the page. */
export function save(dataUrl: string, name: string): void {
    const a = document.createElement('a');
    a.href = dataUrl;
    a.download = name;
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
}

export function svgDataUrl(svg: string): string {
    return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

/** Draw the SVG on a canvas and return it as a PNG data URL. */
export async function pngDataUrl(svg: string, width: number, height: number, scale: number): Promise<string> {
    const image = new Image();
    image.decoding = 'async';
    const loaded = new Promise<void>((resolve, reject) => {
        image.onload = () => resolve();
        image.onerror = () => reject(new Error('the SVG could not be drawn'));
    });
    image.src = svgDataUrl(svg);
    await loaded;
    const canvas = document.createElement('canvas');
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    const context = canvas.getContext('2d');
    if (!context) throw new Error('no canvas');
    context.scale(scale, scale);
    context.drawImage(image, 0, 0, width, height);
    return canvas.toDataURL('image/png');
}
