// Text measured with a canvas, so names are cut to the width of a card.

export const FONT_NAME = '700 15px Manrope, system-ui, sans-serif';
export const FONT_ROW = '500 14px Manrope, system-ui, sans-serif';
export const FONT_MONO = "500 14px 'JetBrains Mono', ui-monospace, monospace";

let context: CanvasRenderingContext2D | null = null;

function measure(font: string): CanvasRenderingContext2D | null {
    context ??= typeof document === 'undefined' ? null : document.createElement('canvas').getContext('2d');
    if (context) context.font = font;
    return context;
}

export function textWidth(text: string, font: string): number {
    return measure(font)?.measureText(text).width ?? text.length * 8;
}

const fitted = new Map<string, string>();

export function fitText(text: string, font: string, width: number): string {
    // Measuring is the slow part of drawing thousands of cards, and the same names recur.
    const key = `${font}\u0000${width}\u0000${text}`;
    let found = fitted.get(key);
    if (found === undefined) {
        found = fit(text, font, width);
        if (fitted.size > 50_000) fitted.clear();
        fitted.set(key, found);
    }
    return found;
}

function fit(text: string, font: string, width: number): string {
    if (textWidth(text, font) <= width) return text;
    let lo = 0;
    let hi = text.length;
    while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (textWidth(`${text.slice(0, mid)}…`, font) <= width) lo = mid;
        else hi = mid - 1;
    }
    return `${text.slice(0, lo)}…`;
}
