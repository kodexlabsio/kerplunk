// What the map shows around a focused card, where the arrow keys go, and the summary panel's
// numbers. Lineage comes from the viewer's model (GraphModel.ancestors and descendants).

import { codePoints, type GraphModel } from '../lib/model';
import type { Prepared } from './map';

export const UPSTREAM = 1;
export const DOWNSTREAM = 2;
export const FOCUS = 3;
export const HIDDEN = 4;

export interface Lineage {
    up: number;
    down: number;
}

/** Each card's state for render.wgsl: its role in the focused lineage, and whether it is filtered out. */
export function statesFor(
    model: GraphModel,
    prepared: Prepared,
    focus: number,
    hiddenKinds: ReadonlySet<string>,
): { states: Uint32Array; lineage: Lineage } {
    const { scene } = prepared;
    const states = new Uint32Array(scene.ids.length);
    scene.nodes.forEach((n, i) => {
        if (hiddenKinds.has(n.kind)) states[i] = HIDDEN;
    });
    const lineage = { up: 0, down: 0 };
    if (focus >= 0) {
        const id = scene.ids[focus]!;
        const ancestors = model.ancestors(id);
        const descendants = model.descendants(id);
        for (const other of ancestors) {
            const i = scene.index.get(other);
            if (i !== undefined) states[i] = (states[i]! & HIDDEN) | UPSTREAM;
        }
        for (const other of descendants) {
            const i = scene.index.get(other);
            if (i !== undefined) states[i] = (states[i]! & HIDDEN) | DOWNSTREAM;
        }
        states[focus] = (states[focus]! & HIDDEN) | FOCUS;
        lineage.up = ancestors.size;
        lineage.down = descendants.size;
    }
    return { states, lineage };
}

function centreY(prepared: Prepared, i: number): number {
    return prepared.rects[i * 4 + 1]! + prepared.rects[i * 4 + 3]! / 2;
}

/** The card an arrow key moves to from ``from``, or null when there is none that way. */
export function step(
    model: GraphModel,
    prepared: Prepared,
    from: number,
    key: 'ArrowLeft' | 'ArrowRight' | 'ArrowUp' | 'ArrowDown',
    shown: (i: number) => boolean,
): number | null {
    const { scene } = prepared;
    if (key === 'ArrowUp' || key === 'ArrowDown') {
        const row = scene.order[scene.layer[from]!]!;
        const delta = key === 'ArrowDown' ? 1 : -1;
        for (let r = scene.slot[from]! + delta; r >= 0 && r < row.length; r += delta)
            if (shown(row[r]!)) return row[r]!;
        return null;
    }
    const adjacency = key === 'ArrowRight' ? model.downAdj : model.upAdj;
    const y = centreY(prepared, from);
    let best: number | null = null;
    let distance = Infinity;
    for (const id of adjacency.get(scene.ids[from]!) ?? []) {
        const i = scene.index.get(id);
        if (i === undefined || !shown(i)) continue;
        const d = Math.abs(centreY(prepared, i) - y);
        if (d < distance || (d === distance && best !== null && i < best)) {
            distance = d;
            best = i;
        }
    }
    return best;
}

export interface Summary {
    /** Cards per package, largest first. */
    packages: { name: string; count: number }[];
    total: number;
    /** Cards with no edge at all. */
    orphans: number;
    /** Models without a test on them or their columns, as the overview and the tests page count them. */
    untested: number;
}

export function summarise(model: GraphModel, prepared: Prepared, shown: (i: number) => boolean): Summary {
    const { scene } = prepared;
    const counts = new Map<string, number>();
    let total = 0;
    let orphans = 0;
    let untested = 0;
    scene.ids.forEach((id, i) => {
        if (!shown(i)) return;
        total++;
        const name = scene.packages[i]!;
        counts.set(name, (counts.get(name) ?? 0) + 1);
        if (scene.degree[i] === 0) orphans++;
        if (scene.nodes[i]!.kind === 'model' && model.testsOf(id).length === 0) untested++;
    });
    const packages = [...counts]
        .map(([name, count]) => ({ name, count }))
        .sort((a, b) => b.count - a.count || codePoints(a.name, b.name));
    return { packages, total, orphans, untested };
}
