// The map's colours are the viewer's design tokens (app.css), read from the page so the light and
// dark themes apply. The browser resolves each token to rgb() or rgba(); this turns that into floats.

export type Rgba = [number, number, number, number];

/** The tokens the shaders use, in the order of the colour array in render.wgsl. */
export const TOKENS = [
    '--card',
    '--card-hover',
    '--card-border',
    '--text',
    '--text-3',
    '--accent',
    '--edge',
    '--t-runs',
    '--rule',
    '--accent-soft',
    // Kind bars, as on the lineage page: sources and seeds, models and snapshots, tables, tasks,
    // exposures.
    '--t-renamed',
    '--t-cast',
    '--t-aggregate',
    '--t-window',
] as const;

const KIND_SLOT: Record<string, number> = {
    source: 10,
    seed: 10,
    model: 5,
    snapshot: 5,
    table: 11,
    task: 12,
    exposure: 13,
};

/** The colour slot of a kind's bar; anything else uses the edge colour. */
export function kindSlot(kind: string): number {
    return KIND_SLOT[kind] ?? 6;
}

const HEX = /^#([0-9a-f]{3,8})$/i;
const FUNCTION = /^rgba?\(\s*([^)]*)\)$/i;
const SRGB = /^color\(\s*srgb\s+([^)]*)\)$/i;

function channel(text: string, scale: number): number {
    const value = text.endsWith('%') ? (Number(text.slice(0, -1)) / 100) * scale : Number(text);
    if (!Number.isFinite(value)) throw new Error(`Not a colour channel: ${text}`);
    return Math.min(1, Math.max(0, value / scale));
}

/** A CSS colour in the forms getComputedStyle gives (and hex), as straight RGBA in 0..1. */
export function parseColour(text: string): Rgba {
    const value = text.trim();
    const hex = HEX.exec(value);
    if (hex) {
        let digits = hex[1]!;
        if (digits.length === 3 || digits.length === 4) digits = [...digits].map(d => d + d).join('');
        if (digits.length !== 6 && digits.length !== 8) throw new Error(`Not a colour: ${text}`);
        const n = (i: number): number => parseInt(digits.slice(i, i + 2), 16) / 255;
        return [n(0), n(2), n(4), digits.length === 8 ? n(6) : 1];
    }
    const fn = FUNCTION.exec(value) ?? SRGB.exec(value);
    if (!fn) throw new Error(`Not a colour: ${text}`);
    const srgb = value.toLowerCase().startsWith('color(');
    const parts = fn[1]!
        .split(/[\s,/]+/)
        .map(p => p.trim())
        .filter(Boolean);
    if (parts.length < 3) throw new Error(`Not a colour: ${text}`);
    const scale = srgb ? 1 : 255;
    return [
        channel(parts[0]!, scale),
        channel(parts[1]!, scale),
        channel(parts[2]!, scale),
        parts[3] === undefined ? 1 : channel(parts[3], 1),
    ];
}

/** Every token, resolved by the browser on ``probe`` and packed for the uniform buffer. */
export function readTokens(probe: HTMLElement): Float32Array {
    const out = new Float32Array(TOKENS.length * 4);
    TOKENS.forEach((token, i) => {
        probe.style.color = `var(${token})`;
        out.set(parseColour(getComputedStyle(probe).color), i * 4);
    });
    probe.style.color = '';
    return out;
}

export function groundOf(probe: HTMLElement): Rgba {
    probe.style.color = 'var(--ground)';
    const colour = parseColour(getComputedStyle(probe).color);
    probe.style.color = '';
    return colour;
}
