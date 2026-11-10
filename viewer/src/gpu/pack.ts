// The instance buffers the GPU draws from, packed once per layout. Cards are 32 bytes, edges and
// glyphs 16. Edges and glyphs refer to their card by index, so the shaders place them from the
// card buffer and a new layout rewrites only that buffer.

import { HEAD, PAD, ROW, W } from '../lib/layout';
import { fitText, FONT_NAME, FONT_ROW } from '../lib/text';
import { kindSlot } from './colour';
import { MAX_ROWS, rowsOf } from './place';
import type { Scene } from './scene';
import { setLine, type Glyph } from './text';

export const NODE_WORDS = 8;
export const EDGE_WORDS = 4;
export const GLYPH_WORDS = 4;

/** Text sizes in world units, and where text starts in a card (past the kind bar). */
export const NAME_SIZE = 15;
export const ROW_SIZE = 14;
export const TEXT_X = 18;
const NAME_BASELINE = HEAD / 2 + 5.5;
const ROW_BASELINE = ROW / 2 + 5;
/** The width a label may take, in world units: the card less the text's inset on both sides. */
export const TEXT_WIDTH = W - TEXT_X - 14;

/** The on-screen pixel sizes below which names and column rows are left out. */
export const NAME_MIN_PX = 3.5;
export const ROW_MIN_PX = 6;

export const FACE_NAME = 0;
export const FACE_ROW = 1;

/** x, y, w, h, the kind's colour slot, the number of rows, the number of columns, and a spare word. */
export function packNodes(scene: Scene, rects: Float32Array): ArrayBuffer {
    const n = scene.ids.length;
    const buffer = new ArrayBuffer(n * NODE_WORDS * 4);
    const f = new Float32Array(buffer);
    const u = new Uint32Array(buffer);
    for (let i = 0; i < n; i++) {
        f.set(rects.subarray(i * 4, i * 4 + 4), i * NODE_WORDS);
        u[i * NODE_WORDS + 4] = kindSlot(scene.nodes[i]!.kind);
        u[i * NODE_WORDS + 5] = rowsOf(scene.columns[i]!.length);
        u[i * NODE_WORDS + 6] = scene.columns[i]!.length;
    }
    return buffer;
}

/** Source card, target card, and 1 for a runs edge. */
export function packEdges(scene: Scene): Uint32Array {
    const m = scene.edgeSource.length;
    const out = new Uint32Array(m * EDGE_WORDS);
    for (let k = 0; k < m; k++) {
        out[k * EDGE_WORDS] = scene.edgeSource[k]!;
        out[k * EDGE_WORDS + 1] = scene.edgeTarget[k]!;
        out[k * EDGE_WORDS + 2] = scene.edgeRuns[k]!;
    }
    return out;
}

/**
 * Every label of every card: its name, then its column rows, the last summing up the rest, each
 * cut to the card as the lineage page cuts its names. lib/text.ts's fonts are the atlas's faces at
 * NAME_SIZE and ROW_SIZE, so a world unit is a pixel of the measurement.
 */
export function labelsOf(scene: Scene, more: (count: number) => string): [number, string][][] {
    const name = (text: string): [number, string] => [FACE_NAME, fitText(text, FONT_NAME, TEXT_WIDTH)];
    const row = (text: string): [number, string] => [FACE_ROW, fitText(text, FONT_ROW, TEXT_WIDTH)];
    return scene.nodes.map((n, i) => {
        const columns = scene.columns[i]!;
        const labels: [number, string][] = [name(n.name || scene.ids[i]!)];
        for (const c of columns.slice(0, MAX_ROWS)) labels.push(row(c.name));
        if (columns.length > MAX_ROWS) labels.push(row(more(columns.length - MAX_ROWS)));
        return labels;
    });
}

export interface Glyphs {
    data: Uint32Array;
    /** The names come first: this many glyphs, then every column row's. */
    names: number;
}

/**
 * Pen x and baseline y (world units, from the card's corner), the glyph and its face, the card.
 * Names first, so a frame too far out for rows draws only the start of the buffer.
 */
export function packGlyphs(labels: [number, string][][], glyphs: Map<string, Glyph>): Glyphs {
    const lines: [number, number, number, ReturnType<typeof setLine>][] = [];
    let count = 0;
    let names = 0;
    for (const pass of [0, 1])
        labels.forEach((list, node) =>
            list.forEach(([face, text], row) => {
                if ((row === 0) !== (pass === 0)) return;
                const line = setLine(text, face, glyphs);
                lines.push([node, row, face, line]);
                count += line.glyphs.length;
                if (row === 0) names += line.glyphs.length;
            }),
        );
    const out = new Uint32Array(count * GLYPH_WORDS);
    const f = new Float32Array(out.buffer);
    let at = 0;
    for (const [node, row, face, line] of lines) {
        const size = face === FACE_NAME ? NAME_SIZE : ROW_SIZE;
        const y = row === 0 ? NAME_BASELINE : HEAD + PAD + (row - 1) * ROW + ROW_BASELINE;
        for (const [index, pen] of line.glyphs) {
            f[at] = TEXT_X + pen * size;
            f[at + 1] = y;
            out[at + 2] = index | (face << 24);
            out[at + 3] = node;
            at += GLYPH_WORDS;
        }
    }
    return { data: out, names };
}

/** How many glyphs a frame at scale ``k`` draws: all, the names, or none. */
export function glyphsAt(k: number, glyphs: { total: number; names: number }, labels: boolean): number {
    if (!labels || NAME_SIZE * k < NAME_MIN_PX) return 0;
    return ROW_SIZE * k < ROW_MIN_PX ? glyphs.names : glyphs.total;
}
