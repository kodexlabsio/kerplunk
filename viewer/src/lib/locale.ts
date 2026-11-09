// Choosing a locale: the reader's choice, then the browser's languages, falling back from a region
// to its language (pt-BR, then pt, then any Portuguese) and finally to British English, the
// language the messages are written in.

export const SOURCE_LOCALE = 'en-GB';

/** Locales whose script runs right to left. */
const RTL = new Set(['ar', 'he', 'fa', 'ur', 'ps', 'yi', 'dv', 'ckb', 'sd', 'ug']);

const ALIASES: Record<string, string> = {
    'zh-cn': 'zh-Hans',
    'zh-sg': 'zh-Hans',
    'zh-my': 'zh-Hans',
    zh: 'zh-Hans',
    'zh-hans-cn': 'zh-Hans',
    pt: 'pt-BR',
    en: 'en-GB',
    nb: 'no',
};

export function language(tag: string): string {
    return tag.split(/[-_]/)[0]!.toLowerCase();
}

export function direction(tag: string): 'ltr' | 'rtl' {
    return RTL.has(language(tag)) ? 'rtl' : 'ltr';
}

/** The best available locale for the requested tags, in order of preference. */
export function resolveLocale(
    requested: readonly (string | null | undefined)[],
    available: readonly string[],
): string {
    const byLower = new Map(available.map(a => [a.toLowerCase(), a]));
    for (const raw of requested) {
        if (!raw) continue;
        const tag = raw.replace(/_/g, '-').trim();
        if (!tag) continue;
        // The tag itself, then shorter and shorter prefixes: zh-Hans-CN, zh-Hans, zh.
        const parts = tag.split('-');
        for (let n = parts.length; n > 0; n--) {
            const prefix = parts.slice(0, n).join('-').toLowerCase();
            const exact = byLower.get(prefix);
            if (exact) return exact;
            const alias = ALIASES[prefix];
            if (alias && byLower.has(alias.toLowerCase())) return byLower.get(alias.toLowerCase())!;
        }
        // Any variant of the same language: pt-PT reads Brazilian Portuguese before English.
        const lang = language(tag);
        const same = available.find(a => language(a) === lang);
        if (same) return same;
    }
    return available.includes(SOURCE_LOCALE) ? SOURCE_LOCALE : (available[0] ?? SOURCE_LOCALE);
}
