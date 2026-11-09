// An indexed, read-only view of the graph: lookups, labels, groupings and the walks the viewer needs.

import { classify, type Transform } from './transform';
import type { Attrs, EdgeKind, GraphData, GraphEdge, GraphNode, NodeKind } from './types';

export const VIEW_KINDS: readonly NodeKind[] = [
    'source',
    'seed',
    'snapshot',
    'table',
    'model',
    'task',
    'exposure',
];
export const RELATION_KINDS: readonly NodeKind[] = ['source', 'seed', 'snapshot', 'table', 'model'];
/** Kinds with a page of their own. */
export const PAGE_KINDS: readonly NodeKind[] = [...VIEW_KINDS, 'column', 'dag', 'test', 'term'];

export interface LayoutEdge {
    source: string;
    target: string;
    kind: 'feeds' | 'runs';
}

export interface LineageItem {
    id: string;
    depth: number;
    /** The column one step nearer the start. */
    via: string;
    transform: Transform;
    verified?: string;
}

function push<K, V>(map: Map<K, V[]>, key: K, value: V): void {
    const list = map.get(key);
    if (list) list.push(value);
    else map.set(key, [value]);
}

export function attr<T = unknown>(item: { attrs?: Attrs } | undefined, key: string): T | undefined {
    return item?.attrs ? (item.attrs[key] as T | undefined) : undefined;
}

export function strings(value: unknown): string[] {
    if (Array.isArray(value)) return value.filter((v): v is string => typeof v === 'string');
    return typeof value === 'string' && value ? [value] : [];
}

/** Code-point order: fixed, so the same graph is laid out the same in every browser's locale. */
export function codePoints(a: string, b: string): number {
    return a < b ? -1 : a > b ? 1 : 0;
}

export function byName(a: GraphNode, b: GraphNode): number {
    return codePoints(a.name || '', b.name || '') || codePoints(a.id, b.id);
}

const NO_EDGES: GraphEdge[] = [];

export class GraphModel {
    readonly nodes: GraphNode[];
    readonly edges: GraphEdge[];
    readonly byId = new Map<string, GraphNode>();
    readonly viewNodes: GraphNode[];
    readonly inView: Set<string>;
    readonly layoutEdges: LayoutEdge[] = [];
    readonly upAdj: Map<string, string[]>;
    readonly downAdj: Map<string, string[]>;
    private readonly outE = new Map<string, Map<EdgeKind, GraphEdge[]>>();
    private readonly inE = new Map<string, Map<EdgeKind, GraphEdge[]>>();
    private readonly columnCache = new Map<string, GraphNode[]>();
    private readonly labels = new Map<string, string>();
    readonly byKind = new Map<string, GraphNode[]>();

    constructor(data: GraphData) {
        this.nodes = data.nodes ?? [];
        this.edges = data.edges ?? [];
        for (const n of this.nodes) {
            this.byId.set(n.id, n);
            push(this.byKind, n.kind, n);
        }
        for (const e of this.edges) {
            let out = this.outE.get(e.source);
            if (!out) this.outE.set(e.source, (out = new Map()));
            push(out, e.kind, e);
            let into = this.inE.get(e.target);
            if (!into) this.inE.set(e.target, (into = new Map()));
            push(into, e.kind, e);
        }
        this.viewNodes = this.nodes.filter(n => VIEW_KINDS.includes(n.kind));
        this.inView = new Set(this.viewNodes.map(n => n.id));
        // Relation-level edges: feeds between relations (a column stands in for its relation) and runs.
        const seen = new Set<string>();
        for (const e of this.edges) {
            if (e.kind !== 'feeds' && e.kind !== 'runs') continue;
            const s =
                e.kind === 'feeds' ? this.viewOf(e.source) : this.inView.has(e.source) ? e.source : null;
            const t =
                e.kind === 'feeds' ? this.viewOf(e.target) : this.inView.has(e.target) ? e.target : null;
            if (!s || !t || s === t) continue;
            const key = `${e.kind}\u0000${s}\u0000${t}`;
            if (seen.has(key)) continue;
            seen.add(key);
            this.layoutEdges.push({ source: s, target: t, kind: e.kind });
        }
        this.upAdj = new Map(this.viewNodes.map(n => [n.id, [] as string[]]));
        this.downAdj = new Map(this.viewNodes.map(n => [n.id, [] as string[]]));
        for (const e of this.layoutEdges) {
            this.downAdj.get(e.source)?.push(e.target);
            this.upAdj.get(e.target)?.push(e.source);
        }
    }

    node(id: string): GraphNode | undefined {
        return this.byId.get(id);
    }

    of(kind: NodeKind): GraphNode[] {
        return this.byKind.get(kind) ?? [];
    }

    outs(id: string, kind: EdgeKind): GraphEdge[] {
        return this.outE.get(id)?.get(kind) ?? NO_EDGES;
    }

    ins(id: string, kind: EdgeKind): GraphEdge[] {
        return this.inE.get(id)?.get(kind) ?? NO_EDGES;
    }

    columnsOf(id: string): GraphNode[] {
        let found = this.columnCache.get(id);
        if (!found) {
            found = this.outs(id, 'contains')
                .map(e => this.byId.get(e.target))
                .filter((n): n is GraphNode => n !== undefined && n.kind === 'column');
            this.columnCache.set(id, found);
        }
        return found;
    }

    /** The relation a node is drawn as: itself, or a column's relation. */
    viewOf(id: string): string | null {
        if (this.inView.has(id)) return id;
        const n = this.byId.get(id);
        return n && n.kind === 'column' && n.parent && this.inView.has(n.parent) ? n.parent : null;
    }

    label(id: string): string {
        let text = this.labels.get(id);
        if (text !== undefined) return text;
        const n = this.byId.get(id);
        if (!n) return id;
        const sourceName = attr<string>(n, 'source_name');
        const dag = attr<string>(n, 'dag');
        if (n.kind === 'column' && n.parent) text = `${this.label(n.parent)}.${n.name}`;
        else if (n.kind === 'source' && sourceName) text = `${sourceName}.${n.name}`;
        else if (n.kind === 'task' && dag) text = `${dag}.${n.name}`;
        else text = n.name;
        this.labels.set(id, text);
        return text;
    }

    /** The dbt package or project a node belongs to. */
    packageOf(n: GraphNode): string | undefined {
        const own = attr<string>(n, 'package') ?? attr<string>(n, 'repository');
        if (own) return own;
        if (n.kind === 'column' && n.parent) {
            const parent = this.byId.get(n.parent);
            return parent ? this.packageOf(parent) : undefined;
        }
        const parts = n.id.split('.');
        return ['model', 'seed', 'snapshot', 'source', 'test', 'exposure'].includes(n.kind) &&
            parts.length > 2
            ? parts[1]
            : undefined;
    }

    tagsOf(n: GraphNode): string[] {
        return strings(attr(n, 'tags'));
    }

    /** Database, schema and name of a relation's warehouse name. */
    schemaOf(n: GraphNode): { database: string; schema: string; name: string } | null {
        if (!n.relation) return null;
        const parts = n.relation.split('.');
        if (parts.length >= 3)
            return { database: parts[0]!, schema: parts[1]!, name: parts.slice(2).join('.') };
        if (parts.length === 2) return { database: '', schema: parts[0]!, name: parts[1]! };
        return { database: '', schema: '', name: parts[0]! };
    }

    boundTo(termId: string): string[] {
        return this.outs(termId, 'defines')
            .map(e => e.target)
            .filter(id => this.byId.has(id));
    }

    termsOf(id: string): GraphNode[] {
        return this.ins(id, 'defines')
            .map(e => this.byId.get(e.source))
            .filter((n): n is GraphNode => n !== undefined);
    }

    /** Tests on a node, and for a relation on its columns: [test, what it tests]. */
    testsOf(id: string, withColumns = true): [GraphNode, string][] {
        const found: [GraphNode, string][] = [];
        const targets = withColumns ? [id, ...this.columnsOf(id).map(c => c.id)] : [id];
        for (const target of targets) {
            for (const e of this.ins(target, 'tests')) {
                const test = this.byId.get(e.source);
                if (test) found.push([test, target]);
            }
        }
        return found;
    }

    /** What a test checks: the relation and, for a column test, the column. */
    testTargets(id: string): string[] {
        return this.outs(id, 'tests').map(e => e.target);
    }

    walk(starts: string[], adjacency: Map<string, string[]>, limit = Infinity): Map<string, number> {
        const depth = new Map(starts.map(s => [s, 0]));
        const queue = starts.slice();
        for (let i = 0; i < queue.length; i++) {
            const id = queue[i]!;
            const d = depth.get(id)!;
            if (d >= limit) continue;
            for (const other of adjacency.get(id) ?? []) {
                if (!depth.has(other)) {
                    depth.set(other, d + 1);
                    queue.push(other);
                }
            }
        }
        return depth;
    }

    ancestors(id: string): Set<string> {
        const found = new Set(this.walk([id], this.upAdj).keys());
        found.delete(id);
        return found;
    }

    descendants(id: string): Set<string> {
        const found = new Set(this.walk([id], this.downAdj).keys());
        found.delete(id);
        return found;
    }

    /** The kind of the edge from ``upstream`` into column ``downstream``. */
    transformOf(edge: GraphEdge): Transform {
        const down = this.byId.get(edge.source);
        const up = this.byId.get(edge.target);
        return classify(
            attr<string>(edge, 'transform'),
            attr<string>(down, 'expression'),
            down?.name,
            up?.name,
        );
    }

    /** Upstream follows derived_from out-edges; downstream follows them in reverse. */
    columnLineage(start: string, upstream: boolean, limit = Infinity): LineageItem[] {
        const depth = new Map<string, number>([[start, 0]]);
        const queue = [start];
        const found: LineageItem[] = [];
        for (let i = 0; i < queue.length; i++) {
            const id = queue[i]!;
            const d = (depth.get(id) ?? 0) + 1;
            if (d > limit) continue;
            const next = upstream ? this.outs(id, 'derived_from') : this.ins(id, 'derived_from');
            for (const e of next) {
                const other = upstream ? e.target : e.source;
                if (depth.has(other)) continue;
                depth.set(other, d);
                queue.push(other);
                found.push({
                    id: other,
                    depth: d,
                    via: id,
                    transform: this.transformOf(e),
                    verified: attr<string>(e, 'verified'),
                });
            }
        }
        return found;
    }

    /** Relations that join, filter or group on a column. */
    referencedBy(columnId: string): string[] {
        return [
            ...new Set(
                this.outs(columnId, 'feeds')
                    .filter(e => attr(e, 'role') === 'reference')
                    .map(e => this.viewOf(e.target))
                    .filter((v): v is string => v !== null),
            ),
        ];
    }
}

export function display(value: unknown): string {
    if (value === null || value === undefined) return '';
    if (Array.isArray(value)) return value.map(display).join(', ');
    return typeof value === 'object' ? JSON.stringify(value) : String(value);
}
