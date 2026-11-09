// SQL split into tokens for highlighting as text spans (the page never inserts markup).

export type TokenKind = 'keyword' | 'string' | 'number' | 'comment' | 'jinja' | 'function' | 'punct' | 'text';

export interface Token {
    kind: TokenKind;
    text: string;
}

const KEYWORDS = new Set(
    (
        'select from where group by order having limit offset join left right full inner outer cross on using as ' +
        'with recursive union all intersect except distinct case when then else end and or not in is null like ilike ' +
        'between exists insert into values update set delete create replace table view materialized temporary temp ' +
        'if over partition rows range unbounded preceding following current row qualify asc desc nulls first last ' +
        'cast true false interval lateral window filter within merge matched returning'
    ).split(' '),
);

const PATTERN =
    /(\{#[\s\S]*?#\}|\{\{[\s\S]*?\}\}|\{%[\s\S]*?%\})|(--[^\n]*|\/\*[\s\S]*?\*\/)|('(?:[^']|'')*'?)|("(?:[^"]|"")*"?|`[^`]*`?)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][\w$]*)(\s*\()?|([(),;.*=<>+\-/|%]+)/g;

export function tokenize(sql: string): Token[] {
    const tokens: Token[] = [];
    let last = 0;
    const text = (t: string): void => {
        if (!t) return;
        const prev = tokens[tokens.length - 1];
        if (prev?.kind === 'text') prev.text += t;
        else tokens.push({ kind: 'text', text: t });
    };
    for (const m of sql.matchAll(PATTERN)) {
        text(sql.slice(last, m.index));
        last = m.index + m[0].length;
        if (m[1]) tokens.push({ kind: 'jinja', text: m[1] });
        else if (m[2]) tokens.push({ kind: 'comment', text: m[2] });
        else if (m[3]) tokens.push({ kind: 'string', text: m[3] });
        else if (m[4]) text(m[4]);
        else if (m[5]) tokens.push({ kind: 'number', text: m[5] });
        else if (m[6]) {
            const word = m[6];
            if (KEYWORDS.has(word.toLowerCase())) tokens.push({ kind: 'keyword', text: word });
            else if (m[7]) tokens.push({ kind: 'function', text: word });
            else text(word);
            if (m[7]) text(m[7]);
        } else if (m[8]) tokens.push({ kind: 'punct', text: m[8] });
    }
    text(sql.slice(last));
    return tokens;
}

/** Tokens per line, for numbered lines. */
export function lines(sql: string): Token[][] {
    const out: Token[][] = [[]];
    for (const token of tokenize(sql.replace(/\r\n?/g, '\n'))) {
        const pieces = token.text.split('\n');
        pieces.forEach((piece, i) => {
            if (i > 0) out.push([]);
            if (piece) out[out.length - 1]!.push({ kind: token.kind, text: piece });
        });
    }
    return out;
}
