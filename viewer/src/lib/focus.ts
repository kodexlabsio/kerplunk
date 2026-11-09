// What a highlighted node lights up: the relations in its lineage, and for a column or term the
// columns. This is the impact path drawn on top of whatever the selector shows.

import { attr, type GraphModel } from './model';

export interface Focus {
    /** Relation ids in the lineage of the highlighted node. */
    relations: Set<string> | null;
    /** Column ids in the lineage of the highlighted column, or the columns a term binds. */
    columns: Set<string> | null;
    /** [column id, relation id] for joins and filters on the column path. */
    references: [string, string][];
}

export const NO_FOCUS: Focus = { relations: null, columns: null, references: [] };

export function computeFocus(model: GraphModel, id: string | null | undefined): Focus {
    const n = id ? model.node(id) : undefined;
    if (!n || !id) return NO_FOCUS;
    if (n.kind === 'term') {
        const bound = model.boundTo(id);
        return {
            columns: new Set(bound.filter(b => model.node(b)?.kind === 'column')),
            relations: new Set(bound.map(b => model.viewOf(b)).filter((b): b is string => b !== null)),
            references: [],
        };
    }
    if (n.kind === 'column') {
        const relation = model.viewOf(id);
        if (!relation) return NO_FOCUS;
        const columns = new Set([id]);
        for (const item of model.columnLineage(id, true)) columns.add(item.id);
        for (const item of model.columnLineage(id, false)) columns.add(item.id);
        const relations = new Set([relation]);
        const references: [string, string][] = [];
        for (const c of columns) {
            const v = model.viewOf(c);
            if (v) relations.add(v);
            for (const e of model.outs(c, 'feeds')) {
                if (attr(e, 'role') !== 'reference') continue;
                const t = model.viewOf(e.target);
                if (t) {
                    references.push([c, t]);
                    relations.add(t);
                }
            }
        }
        return { relations, columns, references };
    }
    if (!model.inView.has(id)) {
        if (n.kind === 'dag') {
            const tasks = model
                .outs(id, 'runs')
                .map(e => e.target)
                .filter(t => model.inView.has(t));
            return tasks.length ? { relations: new Set(tasks), columns: null, references: [] } : NO_FOCUS;
        }
        return NO_FOCUS;
    }
    const relations = new Set([id]);
    for (const r of model.walk([id], model.upAdj).keys()) relations.add(r);
    for (const r of model.walk([id], model.downAdj).keys()) relations.add(r);
    return { relations, columns: null, references: [] };
}
