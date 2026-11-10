// Card positions from the layout's order: the viewer's own stacking (lib/layout.ts), with each layer
// wrapped into columns so the whole map stays about as wide as it is tall.

import { HEAD, PAD, place, ROW, W, wrapHeight } from '../lib/layout';
import type { Scene } from './scene';

/** Column rows a card lists before it sums up the rest in one more row. */
export const MAX_ROWS = 40;

export function rowsOf(columns: number): number {
    return columns > MAX_ROWS ? MAX_ROWS + 1 : columns;
}

export function cardHeight(columns: number): number {
    const rows = rowsOf(columns);
    return rows ? HEAD + PAD + rows * ROW + PAD : HEAD;
}

/** x, y, width and height of every card, four floats each. */
export function placeCards(scene: Scene, order: number[][]): Float32Array {
    const ids = order.map(row => row.map(i => scene.ids[i]!));
    const heightOf = (id: string): number => cardHeight(scene.columns[scene.index.get(id)!]!.length);
    const at = place(ids, heightOf, wrapHeight(ids, heightOf));
    const rects = new Float32Array(scene.ids.length * 4);
    scene.ids.forEach((id, i) => {
        const p = at.get(id)!;
        rects.set([p.x, p.y, W, p.h], i * 4);
    });
    return rects;
}

export interface Box {
    x: number;
    y: number;
    w: number;
    h: number;
}

export function boundsOf(rects: Float32Array): Box | null {
    const n = rects.length / 4;
    if (!n) return null;
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (let i = 0; i < n; i++) {
        minX = Math.min(minX, rects[i * 4]!);
        minY = Math.min(minY, rects[i * 4 + 1]!);
        maxX = Math.max(maxX, rects[i * 4]! + rects[i * 4 + 2]!);
        maxY = Math.max(maxY, rects[i * 4 + 1]! + rects[i * 4 + 3]!);
    }
    return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
}

/** A stable fingerprint of a layout (FNV-1a over the coordinates), for tests and measurements. */
export function layoutHash(rects: Float32Array): string {
    const bytes = new Uint8Array(rects.buffer, rects.byteOffset, rects.byteLength);
    let hash = 0x811c9dc5;
    for (const b of bytes) hash = Math.imul(hash ^ b, 0x01000193) >>> 0;
    return hash.toString(16).padStart(8, '0');
}
