// What a column-level edge does to the data, read from the graph's transform and the downstream
// column's own expression. The graph records only "direct" or "transformed"; the viewer tells
// casts, aggregations and windows apart so the legend and the lists say more than "transformed".

export const TRANSFORMS = [
    'direct',
    'renamed',
    'cast',
    'expression',
    'aggregate',
    'window',
    'reference',
] as const;
export type Transform = (typeof TRANSFORMS)[number];

const AGGREGATE =
    /\b(sum|count|avg|average|min|max|stddev\w*|variance|var_\w+|median|mode|array_agg|string_agg|listagg|group_concat|approx_\w+|percentile\w*|any_value|bool_and|bool_or|every|countif|count_if|sumif|sum_if|collect_list|collect_set|first|last)\s*\(/i;
const WINDOW = /\bover\s*\(/i;
const CAST = /^\s*(?:try_|safe_)?cast\s*\(.*\)\s*$|::\s*[\w ]+(\(\s*\d+(\s*,\s*\d+)?\s*\))?\s*$/is;

/**
 * The kind of an edge from an upstream column into ``downstream``. ``transform`` is the edge's own
 * attribute; ``expression`` the downstream column's SQL expression, if recorded.
 */
export function classify(
    transform: string | undefined,
    expression: string | undefined,
    downstreamName?: string,
    upstreamName?: string,
): Transform {
    if (transform === 'direct' || (!transform && !expression)) {
        return downstreamName && upstreamName && downstreamName.toLowerCase() !== upstreamName.toLowerCase()
            ? 'renamed'
            : 'direct';
    }
    const sql = expression ?? '';
    if (WINDOW.test(sql)) return 'window';
    if (AGGREGATE.test(sql)) return 'aggregate';
    if (CAST.test(sql) && !/[+\-*/|]/.test(sql.replace(/::[\w ]+/g, '').replace(/^\s*\w+\s*\(|\)\s*$/g, '')))
        return 'cast';
    return 'expression';
}

/** Line patterns, so an edge's kind never rests on colour alone (WCAG 1.4.1). */
export const DASH: Record<Transform | 'feeds' | 'runs', string> = {
    feeds: '',
    runs: '7 5',
    direct: '',
    renamed: '12 4',
    cast: '2 3',
    expression: '9 3 2 3',
    aggregate: '',
    window: '9 3 2 3 2 3',
    reference: '1 5',
};
