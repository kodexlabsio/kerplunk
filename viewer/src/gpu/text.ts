// Labels from a glyph atlas: each glyph of the viewer's fonts is rasterised once with a 2D canvas,
// turned into a signed distance field, and drawn by the GPU as an instanced quad at any zoom.

/** The two faces the map draws: card names, and column rows. */
export const FACES = ['700 40px Manrope, system-ui, sans-serif', '500 40px Manrope, system-ui, sans-serif'];
/** The pixel size glyphs are rasterised at, and how far (in those pixels) the distance field reaches. */
export const ATLAS_PX = 40;
export const SPREAD = 6;
const PADDING = SPREAD + 1;
const ATLAS_WIDTH = 2048;
const MAX_HEIGHT = 8192;
const FAR = 1e20;

export interface Glyph {
    /** Advance, in em. */
    advance: number;
    /** Index in the glyph table, or -1 for a glyph with no ink (a space). */
    index: number;
}

export interface Atlas {
    width: number;
    height: number;
    /** The distance field: 0.5 on the outline, more inside. */
    pixels: Uint8Array;
    /** Per drawn glyph: u0, v0, u1, v1, then left, top, right, bottom in em from the pen. */
    table: Float32Array;
    glyphs: Map<string, Glyph>;
    /** Characters with ink left out because the atlas was full. */
    dropped: number;
}

export function glyphKey(face: number, char: string): string {
    return `${face}\u0000${char}`;
}

/** The characters of every label, by face, each once, in first-seen order. */
export function charactersOf(labels: [number, string][]): [number, string][] {
    const seen = new Set<string>();
    const found: [number, string][] = [];
    for (const [face, text] of labels)
        for (const char of text) {
            const key = glyphKey(face, char);
            if (!seen.has(key)) {
                seen.add(key);
                found.push([face, char]);
            }
        }
    return found;
}

// Felzenszwalb and Huttenlocher's squared distance transform along one line of the grid.
function edt1d(
    grid: Float64Array,
    offset: number,
    stride: number,
    length: number,
    f: Float64Array,
    v: Uint32Array,
    z: Float64Array,
): void {
    for (let q = 0; q < length; q++) f[q] = grid[offset + q * stride]!;
    v[0] = 0;
    z[0] = -FAR;
    z[1] = FAR;
    for (let q = 1, k = 0; q < length; q++) {
        let s: number;
        do {
            const r = v[k]!;
            s = (f[q]! - f[r]! + q * q - r * r) / (q - r) / 2;
        } while (s <= z[k]! && --k > -1);
        k++;
        v[k] = q;
        z[k] = s;
        z[k + 1] = FAR;
    }
    for (let q = 0, k = 0; q < length; q++) {
        while (z[k + 1]! < q) k++;
        const r = v[k]!;
        grid[offset + q * stride] = f[r]! + (q - r) * (q - r);
    }
}

function edt(grid: Float64Array, width: number, height: number): void {
    const size = Math.max(width, height);
    const f = new Float64Array(size);
    const v = new Uint32Array(size);
    const z = new Float64Array(size + 1);
    for (let x = 0; x < width; x++) edt1d(grid, x, width, height, f, v, z);
    for (let y = 0; y < height; y++) edt1d(grid, y * width, 1, width, f, v, z);
}

/**
 * A signed distance field from coverage (0..255): each pixel's distance to the outline, with the
 * coverage of edge pixels standing for where the outline crosses them. 0.5 (127.5) is the outline;
 * ``spread`` pixels outside is 0 and ``spread`` inside is 255.
 */
export function distanceField(
    alpha: ArrayLike<number>,
    width: number,
    height: number,
    spread: number,
): Uint8Array {
    const outer = new Float64Array(width * height);
    const inner = new Float64Array(width * height);
    for (let i = 0; i < width * height; i++) {
        const a = alpha[i]! / 255;
        if (a >= 1) {
            outer[i] = 0;
            inner[i] = FAR;
        } else if (a <= 0) {
            outer[i] = FAR;
            inner[i] = 0;
        } else {
            const d = 0.5 - a;
            outer[i] = d > 0 ? d * d : 0;
            inner[i] = d < 0 ? d * d : 0;
        }
    }
    edt(outer, width, height);
    edt(inner, width, height);
    const out = new Uint8Array(width * height);
    for (let i = 0; i < width * height; i++) {
        const d = Math.sqrt(outer[i]!) - Math.sqrt(inner[i]!);
        out[i] = Math.round(255 * Math.min(1, Math.max(0, 0.5 - d / (2 * spread))));
    }
    return out;
}

interface Placed {
    key: string;
    face: number;
    char: string;
    x: number;
    y: number;
    w: number;
    h: number;
    left: number;
    ascent: number;
    right: number;
    descent: number;
    advance: number;
}

/** Rasterises every character with a 2D canvas and packs the fields into one atlas, in shelves. */
export function buildAtlas(chars: [number, string][]): Atlas {
    const canvas = document.createElement('canvas');
    const context = canvas.getContext('2d', { willReadFrequently: true })!;
    const placed: Placed[] = [];
    const glyphs = new Map<string, Glyph>();
    let x = 0;
    let y = 0;
    let shelf = 0;
    let dropped = 0;
    for (const [face, char] of chars) {
        context.font = FACES[face]!;
        const m = context.measureText(char);
        const key = glyphKey(face, char);
        const left = Math.ceil(m.actualBoundingBoxLeft);
        const right = Math.ceil(m.actualBoundingBoxRight);
        const ascent = Math.ceil(m.actualBoundingBoxAscent);
        const descent = Math.ceil(m.actualBoundingBoxDescent);
        const advance = m.width / ATLAS_PX;
        if (left + right <= 0 || ascent + descent <= 0) {
            glyphs.set(key, { advance, index: -1 });
            continue;
        }
        const w = left + right + 2 * PADDING;
        const h = ascent + descent + 2 * PADDING;
        if (x + w > ATLAS_WIDTH) {
            x = 0;
            y += shelf;
            shelf = 0;
        }
        // Glyphs past the largest atlas are left out, and their labels show without them.
        if (y + h > MAX_HEIGHT) {
            glyphs.set(key, { advance, index: -1 });
            dropped++;
            continue;
        }
        glyphs.set(key, { advance, index: placed.length });
        placed.push({ key, face, char, x, y, w, h, left, ascent, right, descent, advance });
        x += w;
        shelf = Math.max(shelf, h);
    }
    const width = ATLAS_WIDTH;
    const height = Math.max(4, y + shelf);
    canvas.width = width;
    canvas.height = height;
    context.fillStyle = '#fff';
    context.textBaseline = 'alphabetic';
    for (const p of placed) {
        context.font = FACES[p.face]!;
        context.fillText(p.char, p.x + PADDING + p.left, p.y + PADDING + p.ascent);
    }
    const rgba = context.getImageData(0, 0, width, height).data;
    const alpha = new Uint8Array(width * height);
    for (let i = 0; i < alpha.length; i++) alpha[i] = rgba[i * 4 + 3]!;
    // The padding around each glyph is wider than the spread, so one transform serves every glyph.
    const pixels = distanceField(alpha, width, height, SPREAD);
    const table = new Float32Array(placed.length * 8);
    placed.forEach((p, i) => {
        table.set(
            [
                p.x / width,
                p.y / height,
                (p.x + p.w) / width,
                (p.y + p.h) / height,
                -(p.left + PADDING) / ATLAS_PX,
                -(p.ascent + PADDING) / ATLAS_PX,
                (p.right + PADDING) / ATLAS_PX,
                (p.descent + PADDING) / ATLAS_PX,
            ],
            i * 8,
        );
    });
    return { width, height, pixels, table, glyphs, dropped };
}

export interface Line {
    /** Glyph table index and pen position in em, for each glyph with ink. */
    glyphs: [number, number][];
    width: number;
}

/**
 * The pen position of each glyph of a label set in one face. Labels arrive already cut to the
 * card (lib/text.ts's fitText); a missing glyph advances by nothing.
 */
export function setLine(text: string, face: number, glyphs: Map<string, Glyph>): Line {
    const out: [number, number][] = [];
    let pen = 0;
    for (const c of text) {
        const g = glyphs.get(glyphKey(face, c));
        if (g && g.index >= 0) out.push([g.index, pen]);
        pen += g?.advance ?? 0;
    }
    return { glyphs: out, width: pen };
}
