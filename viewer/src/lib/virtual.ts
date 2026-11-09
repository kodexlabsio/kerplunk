// Windowing for long lists: only the rows near the visible part of a scroller are rendered.

export interface Window {
    start: number;
    end: number;
    /** Space above and below the rendered rows, in pixels. */
    before: number;
    after: number;
}

export function windowOf(
    count: number,
    rowHeight: number,
    scrollTop: number,
    height: number,
    overscan = 8,
): Window {
    if (count <= 0) return { start: 0, end: 0, before: 0, after: 0 };
    const first = Math.floor(Math.max(0, scrollTop) / rowHeight);
    const visible = Math.ceil(Math.max(height, rowHeight) / rowHeight);
    const start = Math.max(0, first - overscan);
    const end = Math.min(count, first + visible + overscan);
    return { start, end, before: start * rowHeight, after: (count - end) * rowHeight };
}

/** The scrollTop that brings row ``index`` into view, or null if it already is. */
export function scrollFor(
    index: number,
    rowHeight: number,
    scrollTop: number,
    height: number,
): number | null {
    const top = index * rowHeight;
    if (top < scrollTop) return top;
    if (top + rowHeight > scrollTop + height) return top + rowHeight - height;
    return null;
}
