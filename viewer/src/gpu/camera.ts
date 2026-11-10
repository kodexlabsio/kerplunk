// The map's camera: the world point at the centre of the canvas and a scale in CSS pixels per world
// unit. The shaders apply it through one uniform, so panning and zooming never touch a buffer.

import type { Box } from './place';

export interface Camera {
    x: number;
    y: number;
    k: number;
}

export interface Size {
    w: number;
    h: number;
}

export const MIN_SCALE = 0.005;
export const MAX_SCALE = 4;
/** The scale a flight to one card ends at, unless the camera is closer already. */
export const READABLE = 0.9;

export function clampScale(k: number): number {
    return Math.min(MAX_SCALE, Math.max(MIN_SCALE, k));
}

export function toScreen(c: Camera, size: Size, x: number, y: number): [number, number] {
    return [(x - c.x) * c.k + size.w / 2, (y - c.y) * c.k + size.h / 2];
}

export function toWorld(c: Camera, size: Size, sx: number, sy: number): [number, number] {
    return [(sx - size.w / 2) / c.k + c.x, (sy - size.h / 2) / c.k + c.y];
}

/** Zoom by ``factor`` keeping the world point under the screen point where it is. */
export function zoomAt(c: Camera, size: Size, sx: number, sy: number, factor: number): Camera {
    const k = clampScale(c.k * factor);
    const [wx, wy] = toWorld(c, size, sx, sy);
    return { k, x: wx - (sx - size.w / 2) / k, y: wy - (sy - size.h / 2) / k };
}

export function pan(c: Camera, dx: number, dy: number): Camera {
    return { k: c.k, x: c.x - dx / c.k, y: c.y - dy / c.k };
}

/** The whole box in view, with ``margin`` CSS pixels around it. */
export function fit(box: Box | null, size: Size, margin = 32, max = 1): Camera {
    if (!box || size.w <= 0 || size.h <= 0) return { x: 0, y: 0, k: 1 };
    const k = clampScale(
        Math.min(max, (size.w - 2 * margin) / Math.max(box.w, 1), (size.h - 2 * margin) / Math.max(box.h, 1)),
    );
    return { x: box.x + box.w / 2, y: box.y + box.h / 2, k };
}

/** Whether a box lies wholly inside the view. */
export function inView(c: Camera, size: Size, box: Box): boolean {
    const [x0, y0] = toScreen(c, size, box.x, box.y);
    const [x1, y1] = toScreen(c, size, box.x + box.w, box.y + box.h);
    return x0 >= 0 && y0 >= 0 && x1 <= size.w && y1 <= size.h;
}

/** A camera that shows the box: readable, centred, and never further out than now. */
export function focusOn(c: Camera, size: Size, box: Box): Camera {
    const k = Math.max(c.k, Math.min(READABLE, (size.w * 0.8) / box.w, (size.h * 0.8) / box.h));
    return { x: box.x + box.w / 2, y: box.y + Math.min(box.h, size.h / k) / 2, k: clampScale(k) };
}

export function ease(t: number): number {
    return t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2;
}

/** A point on a flight from a to b: the centre moves with the eased time, the scale geometrically. */
export function between(a: Camera, b: Camera, t: number): Camera {
    const e = ease(Math.min(1, Math.max(0, t)));
    return { k: a.k * (b.k / a.k) ** e, x: a.x + (b.x - a.x) * e, y: a.y + (b.y - a.y) * e };
}
