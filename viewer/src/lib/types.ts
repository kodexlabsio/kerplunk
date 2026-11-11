// The shapes konstruct embeds in the page: Graph.to_dict() and the explain summaries.

export type NodeKind =
    | 'source'
    | 'table'
    | 'column'
    | 'model'
    | 'seed'
    | 'snapshot'
    | 'dag'
    | 'task'
    | 'test'
    | 'exposure'
    | 'term';

export type EdgeKind =
    'contains' | 'feeds' | 'derived_from' | 'runs' | 'precedes' | 'tests' | 'defines' | 'relates_to';

export type Attrs = Record<string, unknown>;

export interface GraphNode {
    id: string;
    kind: NodeKind;
    name: string;
    parent?: string;
    relation?: string;
    description?: string;
    data_type?: string;
    path?: string;
    origins?: string[];
    attrs?: Attrs;
}

export interface GraphEdge {
    source: string;
    target: string;
    kind: EdgeKind;
    attrs?: Attrs;
}

export interface GraphMeta {
    root?: string;
    generated_at?: string;
    konstruct_version?: string;
    dialect?: string;
    /** Each dbt project's {% docs __overview__ %} text, by project name. */
    overviews?: Record<string, string>;
    [key: string]: unknown;
}

export interface GraphData {
    format_version?: number;
    meta?: GraphMeta;
    nodes?: GraphNode[];
    edges?: GraphEdge[];
    warnings?: string[];
}

/** One column's formula: e (expression), c (composed), s (sources), p (partial). */
export interface Formula {
    e?: string;
    c?: string;
    s?: string[];
    p?: number;
}

/** The SQL behind a node: s (as written), c (compiled by dbt), t (cut short). */
export interface Code {
    s?: string;
    c?: string;
    t?: number;
}

export interface OntologyEntity {
    name: string;
    key: string;
    keys?: string[];
    relations?: string[];
    evidence?: string[];
    term?: string;
    description?: string;
    key_ids?: string[];
    /** verified, declared or inferred; left out by Konstruct before 0.13.9. */
    confidence?: string;
    /** confirmed or failed, where konstruct verify checked its keys. */
    check?: string;
}

export interface MeasureColumn {
    column: string;
    id?: string;
    formula?: string;
    sources?: string[];
    complete?: boolean;
    verified?: number;
    edges?: number;
    usage?: number;
}

export interface OntologyMeasure {
    name: string;
    definition?: string;
    status?: string;
    owner?: string;
    approved?: string;
    entity?: string;
    grain?: string;
    unit?: string;
    currency?: string;
    filters?: string[];
    path?: string;
    expression?: string;
    columns?: MeasureColumn[];
    confidence?: string;
    evidence?: string[];
}

export interface OntologyRelationship {
    source: string;
    target: string;
    via: string;
    cardinality?: string;
    tested?: boolean;
    evidence?: string[];
    columns?: [string, string];
    confidence?: string;
    check?: string;
}

export interface OntologyData {
    entities?: OntologyEntity[];
    measures?: OntologyMeasure[];
    relationships?: OntologyRelationship[];
    concepts?: { name: string; broader?: string[]; narrower?: string[] }[];
    rules?: { name: string; description?: string; violations?: string[] }[];
}

/** A metric of the world model, defined or proposed (konstruct.semantics.world.Metric). */
export interface WorldMetric {
    name: string;
    /** definitions, vocabulary, dbt semantic layer, looker, ...; for a proposal aggregate,
     * measure column or entity count. */
    source: string;
    confidence?: string;
    /** Konstruct read it from the code; nothing defines or approves it. */
    proposed?: boolean;
    synonyms?: string[];
    definition?: string;
    status?: string;
    owner?: string;
    approved?: string;
    entity?: string;
    grain?: string;
    grain_confidence?: string;
    unit?: string;
    currency?: string;
    filters?: string[];
    formula?: string;
    aggregation?: string;
    additive?: boolean;
    evidence?: string[];
    path?: string;
    /** For a measure of a BI view or semantic model, the graph id of the exposure that defines it. */
    exposure?: string;
    /** The graph ids of the columns that hold it or that it reads. */
    columns?: string[];
}

export interface WorldData {
    metrics?: WorldMetric[];
}

export interface ConflictSide {
    column: string;
    label: string;
    relation_kind?: string;
    path?: string | null;
    expression?: string | null;
    composed?: string | null;
    sources?: string[];
    filters?: { relation: string; condition: string; path?: string | null }[];
    joins?: { relation: string; condition: string; path?: string | null }[];
    reach?: Record<string, number>;
    terms?: string[];
    carried_into?: string[];
    weight?: number;
}

export interface Conflict {
    idea: string;
    reason: string;
    score: number;
    a: ConflictSide;
    b: ConflictSide;
    differences: { kind: string; detail: string; side?: string; path?: string }[];
}

export interface ConflictsData {
    summary?: string;
    candidates?: number;
    groups?: number;
    conflicts?: Conflict[];
}

/** The second placeholder: what the page shows beside the graph. */
export interface Explanations {
    columns?: Record<string, Formula>;
    conditions?: Record<string, string[]>;
    sql?: Record<string, Code>;
    ontology?: OntologyData;
    /** The world model's metrics; a page written by an earlier Konstruct has none. */
    world?: WorldData;
    conflicts?: ConflictsData;
    /** A locale the build chose (KONSTRUCT_LOCALE). */
    locale?: string;
}

export interface Owner {
    name: string;
    role?: string | null;
}

export interface Usage {
    queries: number;
    users: number;
    days: number;
    score?: number;
}

export interface Point {
    x: number;
    y: number;
    h: number;
}
