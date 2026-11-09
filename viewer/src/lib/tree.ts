// The navigation trees: the project by package and folder, the warehouse by database and schema,
// pipelines by DAG, and the business vocabulary. Flattened into rows for a virtualised list.

import { attr, RELATION_KINDS, type GraphModel } from './model';
import type { GraphNode, OntologyData } from './types';

export interface TreeItem {
    key: string;
    label: string;
    /** An icon name (a node kind or "folder"). */
    icon: string;
    /** The hash the item opens, if it opens anything. */
    href?: string;
    children?: TreeItem[];
    /** Extra words the filter matches, such as a relation's warehouse name. */
    also?: string;
}

export interface TreeRow {
    item: TreeItem;
    depth: number;
    expanded: boolean;
    /** Position among siblings, for aria-posinset and aria-setsize. */
    index: number;
    siblings: number;
    parent: string | null;
}

export type Compare = (a: string, b: string) => number;
export type TreeLabels = Record<
    'sources' | 'exposures' | 'project' | 'noSchema' | 'terms' | 'entities' | 'tests',
    string
>;

export type TreeTab = 'project' | 'database' | 'pipelines' | 'business';

function nodeHref(id: string): string {
    return `#/node/${encodeURIComponent(id)}`;
}

function leaf(model: GraphModel, n: GraphNode, label = n.name): TreeItem {
    return { key: n.id, label, icon: n.kind, href: nodeHref(n.id), also: n.relation };
}

function sortItems(items: TreeItem[], compare: Compare): TreeItem[] {
    // Folders before leaves, each in the locale's order.
    return items.sort((a, b) => Number(!a.children) - Number(!b.children) || compare(a.label, b.label));
}

/** Folders from paths, merged: models/staging/stg_orders.sql sits in models, then staging. */
function folders(prefix: string, entries: [string[], TreeItem][], compare: Compare): TreeItem[] {
    const here: TreeItem[] = [];
    const deeper = new Map<string, [string[], TreeItem][]>();
    for (const [path, item] of entries) {
        if (path.length === 0) here.push(item);
        else {
            const list = deeper.get(path[0]!) ?? [];
            list.push([path.slice(1), item]);
            deeper.set(path[0]!, list);
        }
    }
    for (const [name, list] of deeper) {
        const key = `${prefix}/${name}`;
        here.push({ key, label: name, icon: 'folder', children: folders(key, list, compare) });
    }
    return sortItems(here, compare);
}

export function buildTree(
    model: GraphModel,
    tab: TreeTab,
    labels: TreeLabels,
    compare: Compare,
    ontology?: OntologyData,
): TreeItem[] {
    if (tab === 'database') {
        const databases = new Map<string, Map<string, TreeItem[]>>();
        for (const n of model.nodes) {
            if (!RELATION_KINDS.includes(n.kind)) continue;
            const where = model.schemaOf(n);
            if (!where) continue;
            const schemas = databases.get(where.database) ?? new Map<string, TreeItem[]>();
            databases.set(where.database, schemas);
            const list = schemas.get(where.schema) ?? [];
            schemas.set(where.schema, list);
            list.push(leaf(model, n, where.name));
        }
        const items: TreeItem[] = [];
        for (const [database, schemas] of databases) {
            const children: TreeItem[] = [];
            for (const [schema, list] of schemas) {
                children.push({
                    key: `db:${database}.${schema}`,
                    label: schema || labels.noSchema,
                    icon: 'schema',
                    children: sortItems(list, compare),
                });
            }
            items.push({
                key: `db:${database}`,
                label: database || labels.noSchema,
                icon: 'database',
                children: sortItems(children, compare),
            });
        }
        // One database is the usual case: show its schemas at the top.
        return items.length === 1 ? items[0]!.children! : sortItems(items, compare);
    }
    if (tab === 'pipelines') {
        const items: TreeItem[] = [];
        const byDag = new Map<string, TreeItem[]>();
        for (const task of model.of('task')) {
            const dag = String(attr(task, 'dag') ?? '');
            const list = byDag.get(dag) ?? [];
            byDag.set(dag, list);
            list.push(leaf(model, task));
        }
        const dags = new Map(model.of('dag').map(d => [d.name, d]));
        for (const [dag, tasks] of byDag) {
            const node = dags.get(dag);
            items.push({
                key: node?.id ?? `dag:${dag}`,
                label: dag || labels.project,
                icon: 'dag',
                href: node ? nodeHref(node.id) : undefined,
                children: tasks.sort((a, b) => compare(a.label, b.label)),
            });
        }
        for (const [name, node] of dags) if (!byDag.has(name)) items.push(leaf(model, node));
        return items.sort((a, b) => compare(a.label, b.label));
    }
    if (tab === 'business') {
        const items: TreeItem[] = [];
        const terms = model.of('term').map(t => leaf(model, t));
        if (terms.length)
            items.push({
                key: 'business:terms',
                label: labels.terms,
                icon: 'term',
                href: '#/glossary',
                children: terms.sort((a, b) => compare(a.label, b.label)),
            });
        const entities = (ontology?.entities ?? []).map(e => ({
            key: `entity:${e.name}`,
            label: e.name,
            icon: 'entity',
            href: `#/entity/${encodeURIComponent(e.name)}`,
            also: e.term,
        }));
        if (entities.length)
            items.push({
                key: 'business:entities',
                label: labels.entities,
                icon: 'entity',
                href: '#/ontology',
                children: entities.sort((a, b) => compare(a.label, b.label)),
            });
        return items;
    }
    // The project: each package's files by folder, then sources by source name, then exposures.
    const packages = new Map<string, [string[], TreeItem][]>();
    const sources = new Map<string, TreeItem[]>();
    const exposures: TreeItem[] = [];
    const loose: TreeItem[] = [];
    for (const n of model.nodes) {
        if (n.kind === 'source') {
            const group = String(attr(n, 'source_name') ?? model.schemaOf(n)?.schema ?? '');
            const list = sources.get(group) ?? [];
            sources.set(group, list);
            list.push(leaf(model, n));
        } else if (n.kind === 'exposure') exposures.push(leaf(model, n));
        else if (
            n.kind === 'model' ||
            n.kind === 'seed' ||
            n.kind === 'snapshot' ||
            (n.kind === 'test' && n.path?.endsWith('.sql'))
        ) {
            const pkg = model.packageOf(n) ?? labels.project;
            const list = packages.get(pkg) ?? [];
            packages.set(pkg, list);
            const parts = (n.path ?? '').split('/').filter(Boolean);
            list.push([parts.slice(0, -1), leaf(model, n)]);
        } else if (n.kind === 'table' && n.path) loose.push(leaf(model, n));
    }
    const items: TreeItem[] = [];
    const single = packages.size === 1;
    for (const [pkg, entries] of [...packages].sort((a, b) => compare(a[0], b[0]))) {
        const children = folders(`pkg:${pkg}`, entries, compare);
        if (single) items.push(...children);
        else items.push({ key: `pkg:${pkg}`, label: pkg, icon: 'package', children });
    }
    if (sources.size) {
        const groups = [...sources].map(([name, list]) => ({
            key: `sources:${name}`,
            label: name || labels.noSchema,
            icon: 'folder',
            children: list.sort((a, b) => compare(a.label, b.label)),
        }));
        items.push({
            key: 'sources',
            label: labels.sources,
            icon: 'source',
            children: groups.sort((a, b) => compare(a.label, b.label)),
        });
    }
    if (exposures.length)
        items.push({
            key: 'exposures',
            label: labels.exposures,
            icon: 'exposure',
            children: exposures.sort((a, b) => compare(a.label, b.label)),
        });
    return items.concat(loose.sort((a, b) => compare(a.label, b.label)));
}

function matches(item: TreeItem, filter: string): boolean {
    return item.label.toLowerCase().includes(filter) || !!item.also?.toLowerCase().includes(filter);
}

/** The visible rows: expanded branches, or with a filter every branch that leads to a match. */
export function flatten(items: TreeItem[], expanded: ReadonlySet<string>, filter = ''): TreeRow[] {
    const rows: TreeRow[] = [];
    const needle = filter.trim().toLowerCase();
    const keep = new Map<TreeItem, boolean>();
    const visit = (item: TreeItem): boolean => {
        let found = needle ? matches(item, needle) : true;
        if (item.children) for (const child of item.children) if (visit(child)) found = true;
        keep.set(item, found);
        return found;
    };
    if (needle) items.forEach(visit);
    const walk = (list: TreeItem[], depth: number, parent: string | null): void => {
        const shown = needle ? list.filter(i => keep.get(i)) : list;
        shown.forEach((item, index) => {
            const open = !!item.children && (needle ? true : expanded.has(item.key));
            rows.push({ item, depth, expanded: open, index, siblings: shown.length, parent });
            if (open && item.children) walk(item.children, depth + 1, item.key);
        });
    };
    walk(items, 0, null);
    return rows;
}

/** The keys of the branches that lead to ``key``, to open the tree at the current page. */
export function pathTo(items: TreeItem[], key: string): string[] | null {
    for (const item of items) {
        if (item.key === key) return [];
        if (item.children) {
            const below = pathTo(item.children, key);
            if (below) return [item.key, ...below];
        }
    }
    return null;
}
