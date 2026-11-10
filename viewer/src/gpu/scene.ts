// The map's scene: every relation and task of the project as a card, and the edges between them,
// indexed 0..n-1 in name order, in the lineage page's layers and order (lib/layout.ts). Built once
// per graph; nothing here depends on the camera or the language.

import { byName, type GraphModel } from '../lib/model';
import type { GraphNode } from '../lib/types';

export interface Scene {
    ids: string[];
    nodes: GraphNode[];
    /** The dbt package or project of each card, or ''. */
    packages: string[];
    index: Map<string, number>;
    /** Each card's columns, in the order of the graph. */
    columns: GraphNode[][];
    /** Cards in layers, left to right, each layer in its order top to bottom. */
    order: number[][];
    /** Each card's layer, and its position in that layer: the keyboard's up and down. */
    layer: Uint32Array;
    slot: Uint32Array;
    /** Edges as card indices; runs marks an edge from a task to what it runs. */
    edgeSource: Uint32Array;
    edgeTarget: Uint32Array;
    edgeRuns: Uint8Array;
    /** Edges at each card, both ways. */
    degree: Uint32Array;
}

/** The scene, given the order of every node in view (``computeOrder``) as rows of ids. */
export function buildScene(model: GraphModel, rows: string[][]): Scene {
    const ids = model.viewNodes
        .slice()
        .sort(byName)
        .map(n => n.id);
    const n = ids.length;
    const index = new Map(ids.map((id, i) => [id, i]));
    const nodes = ids.map(id => model.node(id)!);
    const order = rows.map(row => row.map(id => index.get(id)!));
    const layer = new Uint32Array(n);
    const slot = new Uint32Array(n);
    order.forEach((row, l) =>
        row.forEach((i, r) => {
            layer[i] = l;
            slot[i] = r;
        }),
    );
    const links = model.layoutEdges;
    const m = links.length;
    const edgeSource = new Uint32Array(m);
    const edgeTarget = new Uint32Array(m);
    const edgeRuns = new Uint8Array(m);
    const degree = new Uint32Array(n);
    links.forEach((e, k) => {
        const s = index.get(e.source)!;
        const t = index.get(e.target)!;
        edgeSource[k] = s;
        edgeTarget[k] = t;
        edgeRuns[k] = e.kind === 'runs' ? 1 : 0;
        degree[s]!++;
        degree[t]!++;
    });
    return {
        ids,
        nodes,
        packages: nodes.map(node => model.packageOf(node) ?? ''),
        index,
        columns: ids.map(id => model.columnsOf(id)),
        order,
        layer,
        slot,
        edgeSource,
        edgeTarget,
        edgeRuns,
        degree,
    };
}
