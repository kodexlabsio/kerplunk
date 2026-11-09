// Message keys for the graph's own vocabulary: node kinds, transforms and test types.

import type { MessageKey } from '../locales/en-GB';
import { t } from './i18n.svelte';
import type { Transform } from './transform';

export const KIND_KEYS: Record<string, MessageKey> = {
    source: 'kind.source',
    seed: 'kind.seed',
    snapshot: 'kind.snapshot',
    table: 'kind.table',
    model: 'kind.model',
    task: 'kind.task',
    exposure: 'kind.exposure',
    column: 'kind.column',
    test: 'kind.test',
    dag: 'kind.dag',
    term: 'kind.term',
    entity: 'kind.entity',
};

export const TRANSFORM_KEYS: Record<Transform | 'feeds' | 'runs', MessageKey> = {
    feeds: 'edge.feeds',
    runs: 'edge.runs',
    direct: 'transform.direct',
    renamed: 'transform.renamed',
    cast: 'transform.cast',
    expression: 'transform.expression',
    aggregate: 'transform.aggregate',
    window: 'transform.window',
    reference: 'transform.reference',
};

export function kindName(kind: string): string {
    return t(KIND_KEYS[kind] ?? 'kind.other');
}

export function transformName(kind: string): string {
    return t(TRANSFORM_KEYS[kind as Transform] ?? 'transform.expression');
}

/** The plural noun alone, for a number shown large beside it. */
export function kindNoun(kind: string, count: number): string {
    const key = NOUN_KEYS[kind];
    return key ? t(key, { count }) : kind;
}

const NOUN_KEYS: Record<string, MessageKey> = {
    source: 'noun.source',
    seed: 'noun.seed',
    snapshot: 'noun.snapshot',
    table: 'noun.table',
    model: 'noun.model',
    task: 'noun.task',
    exposure: 'noun.exposure',
    test: 'noun.test',
    dag: 'noun.dag',
    term: 'noun.term',
};
