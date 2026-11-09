// The numbers on the project overview: counts, coverage of descriptions, tests, lineage and
// verification, and what most depends on.

import { attr, codePoints, RELATION_KINDS, type GraphModel } from './model';

export interface Coverage {
    done: number;
    total: number;
}

export interface Overview {
    counts: Record<string, number>;
    columnEdges: number;
    descriptions: Coverage;
    columnDescriptions: Coverage;
    tested: Coverage;
    lineage: Coverage;
    verified: Coverage | null;
    terms: { total: number; approved: number; bound: number };
    /** Relations with the most downstream, most first. */
    central: [string, number][];
    /** Warnings grouped by the input or step that raised them. */
    warnings: [string, number][];
}

export function share(c: Coverage): number {
    return c.total ? c.done / c.total : 0;
}

export function overview(model: GraphModel, warnings: string[] = []): Overview {
    const counts: Record<string, number> = {};
    for (const n of model.nodes) counts[n.kind] = (counts[n.kind] ?? 0) + 1;
    const documented = model.nodes.filter(
        n => n.kind === 'model' || n.kind === 'source' || n.kind === 'seed' || n.kind === 'snapshot',
    );
    const descriptions = {
        done: documented.filter(n => n.description?.trim()).length,
        total: documented.length,
    };
    const ownColumns = documented.flatMap(n => model.columnsOf(n.id));
    const columnDescriptions = {
        done: ownColumns.filter(c => c.description?.trim()).length,
        total: ownColumns.length,
    };
    const models = model.of('model');
    const tested = { done: models.filter(n => model.testsOf(n.id).length > 0).length, total: models.length };
    // Columns that something builds: those of models, snapshots and tables a task or SQL file writes.
    const built = model.nodes.filter(
        n =>
            n.kind === 'model' ||
            n.kind === 'snapshot' ||
            (n.kind === 'table' && model.ins(n.id, 'feeds').length > 0),
    );
    const builtColumns = built.flatMap(n => model.columnsOf(n.id));
    const lineage = {
        done: builtColumns.filter(c => model.outs(c.id, 'derived_from').length > 0).length,
        total: builtColumns.length,
    };
    const columnEdges = model.edges.filter(e => e.kind === 'derived_from');
    const confirmed = columnEdges.filter(e => attr(e, 'verified') === 'confirmed').length;
    const verified =
        model.edges.some(e => attr(e, 'verified')) || model.nodes.length === 0
            ? { done: confirmed, total: columnEdges.length }
            : null;
    const terms = model.of('term');
    const central = model.viewNodes
        .filter(n => RELATION_KINDS.includes(n.kind))
        .map(n => [n.id, model.descendants(n.id).size] as [string, number])
        .filter(([, d]) => d > 0)
        .sort((a, b) => b[1] - a[1] || codePoints(a[0], b[0]))
        .slice(0, 6);
    const groups = new Map<string, number>();
    for (const w of warnings) {
        const head = /^([\w -]{1,24}):/.exec(w)?.[1] ?? '';
        groups.set(head, (groups.get(head) ?? 0) + 1);
    }
    return {
        counts,
        columnEdges: columnEdges.length,
        descriptions,
        columnDescriptions,
        tested,
        lineage,
        verified,
        terms: {
            total: terms.length,
            approved: terms.filter(t => attr(t, 'status') === 'approved').length,
            bound: terms.filter(t => model.boundTo(t.id).length > 0).length,
        },
        central,
        warnings: [...groups].sort((a, b) => b[1] - a[1]),
    };
}
