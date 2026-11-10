// Picking on the CPU: a uniform grid over the cards answers "what is under the pointer" in a few
// comparisons, with no GPU round trip (a GPU ID buffer needs a copy and a mapAsync per query).

import { HEAD, PAD, ROW } from '../lib/layout';

export interface Hit {
    node: number;
    /** The column row under the point, or -1 for the card's head. */
    row: number;
}

export class PickGrid {
    private readonly x0: number;
    private readonly y0: number;
    private readonly cols: number;
    private readonly rows: number;
    private readonly cell: number;
    private readonly starts: Uint32Array;
    private readonly items: Uint32Array;

    constructor(
        private readonly rects: Float32Array,
        cell = 512,
    ) {
        const n = rects.length / 4;
        let x0 = Infinity;
        let y0 = Infinity;
        let x1 = -Infinity;
        let y1 = -Infinity;
        for (let i = 0; i < n; i++) {
            x0 = Math.min(x0, rects[i * 4]!);
            y0 = Math.min(y0, rects[i * 4 + 1]!);
            x1 = Math.max(x1, rects[i * 4]! + rects[i * 4 + 2]!);
            y1 = Math.max(y1, rects[i * 4 + 1]! + rects[i * 4 + 3]!);
        }
        if (!n) x0 = y0 = x1 = y1 = 0;
        this.x0 = x0;
        this.y0 = y0;
        this.cell = cell;
        this.cols = Math.max(1, Math.ceil((x1 - x0) / cell));
        this.rows = Math.max(1, Math.ceil((y1 - y0) / cell));
        // Two passes, counting then filling, give each cell a slice of one flat array.
        const counts = new Uint32Array(this.cols * this.rows + 1);
        const each = (i: number, visit: (c: number) => void): void => {
            const [a, b, c, d] = this.span(i);
            for (let y = b; y <= d; y++) for (let x = a; x <= c; x++) visit(y * this.cols + x);
        };
        for (let i = 0; i < n; i++) each(i, c => counts[c + 1]!++);
        for (let c = 0; c < this.cols * this.rows; c++) counts[c + 1]! += counts[c]!;
        this.starts = counts.slice();
        this.items = new Uint32Array(counts[this.cols * this.rows]!);
        const fill = counts.slice();
        for (let i = 0; i < n; i++) each(i, c => (this.items[fill[c]!++] = i));
    }

    private clampX(x: number): number {
        return Math.min(this.cols - 1, Math.max(0, Math.floor((x - this.x0) / this.cell)));
    }

    private clampY(y: number): number {
        return Math.min(this.rows - 1, Math.max(0, Math.floor((y - this.y0) / this.cell)));
    }

    private span(i: number): [number, number, number, number] {
        const r = this.rects;
        return [
            this.clampX(r[i * 4]!),
            this.clampY(r[i * 4 + 1]!),
            this.clampX(r[i * 4]! + r[i * 4 + 2]!),
            this.clampY(r[i * 4 + 1]! + r[i * 4 + 3]!),
        ];
    }

    /** The card at a world point, or -1. */
    at(x: number, y: number): number {
        const cx = Math.floor((x - this.x0) / this.cell);
        const cy = Math.floor((y - this.y0) / this.cell);
        if (cx < 0 || cy < 0 || cx >= this.cols || cy >= this.rows) return -1;
        const c = cy * this.cols + cx;
        const r = this.rects;
        for (let p = this.starts[c]!; p < this.starts[c + 1]!; p++) {
            const i = this.items[p]!;
            const rx = r[i * 4]!;
            const ry = r[i * 4 + 1]!;
            if (x >= rx && y >= ry && x <= rx + r[i * 4 + 2]! && y <= ry + r[i * 4 + 3]!) return i;
        }
        return -1;
    }

    /** The card and, within it, the row at a world point. */
    hit(x: number, y: number, rowCount: (node: number) => number): Hit | null {
        const node = this.at(x, y);
        if (node < 0) return null;
        const offset = y - this.rects[node * 4 + 1]! - HEAD - PAD;
        const row = offset >= 0 ? Math.floor(offset / ROW) : -1;
        return { node, row: row < rowCount(node) ? row : -1 };
    }
}
