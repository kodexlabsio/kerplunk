// The message catalogue at run time: t(key, values) formats a message in the current locale, and
// numbers, dates, relative times and sorting follow the same locale. Changing the locale
// re-renders every message, because the locale is reactive state.

import type { MessageKey } from '../locales/en-GB';
import { CATALOGUES } from '../locales/index';
import { format, formatDate, formatNumber, parse, type Part, type Values } from './icu';
import { direction, resolveLocale, SOURCE_LOCALE } from './locale';

export const LOCALES: readonly string[] = Object.keys(CATALOGUES);

const STORE_KEY = 'konstruct-locale';

function stored(): string | null {
    try {
        return window.localStorage.getItem(STORE_KEY);
    } catch {
        return null;
    }
}

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
    ['year', 365 * 24 * 3600],
    ['month', 30 * 24 * 3600],
    ['week', 7 * 24 * 3600],
    ['day', 24 * 3600],
    ['hour', 3600],
    ['minute', 60],
    ['second', 1],
];

class I18n {
    locale = $state(SOURCE_LOCALE);
    private parsed = new Map<string, Part[]>();
    private collators = new Map<string, Intl.Collator>();

    readonly dir = $derived(direction(this.locale));

    /** The locale for this page: a ?lang= parameter, the reader's choice, the build's, the browser's. */
    choose(build?: string): void {
        let param: string | null = null;
        try {
            param = new URLSearchParams(window.location.search).get('lang');
        } catch {
            param = null;
        }
        const browser = typeof navigator !== 'undefined' ? (navigator.languages ?? [navigator.language]) : [];
        this.locale = resolveLocale([param, stored(), build, ...browser], LOCALES);
    }

    /** The reader picked a language: remember it for next time. */
    pick(locale: string): void {
        this.locale = resolveLocale([locale], LOCALES);
        try {
            window.localStorage.setItem(STORE_KEY, this.locale);
        } catch {
            // Storage can be blocked; the choice then lasts until the page closes.
        }
    }

    t = (key: MessageKey, values: Values = {}): string => {
        const locale = this.locale;
        const cacheKey = `${locale}\u0000${key}`;
        let parts = this.parsed.get(cacheKey);
        if (!parts) {
            const text = CATALOGUES[locale]?.[key] ?? CATALOGUES[SOURCE_LOCALE]![key] ?? key;
            parts = parse(text);
            this.parsed.set(cacheKey, parts);
        }
        return format(parts, values, locale);
    };

    number = (value: number, style?: 'percent'): string => formatNumber(this.locale, value, style);

    date = (value: Date | string, style?: 'short' | 'medium' | 'long'): string =>
        formatDate(this.locale, value instanceof Date ? value : new Date(value), style);

    /** "3 hours ago", in the locale. */
    relative = (value: Date | string, now = Date.now()): string => {
        const when = value instanceof Date ? value : new Date(value);
        const seconds = (when.getTime() - now) / 1000;
        if (!Number.isFinite(seconds)) return '';
        const format = new Intl.RelativeTimeFormat(this.locale, { numeric: 'auto' });
        for (const [unit, size] of UNITS) {
            if (Math.abs(seconds) >= size || unit === 'second')
                return format.format(Math.round(seconds / size), unit);
        }
        return '';
    };

    compare = (a: string, b: string): number => {
        let collator = this.collators.get(this.locale);
        if (!collator) {
            collator = new Intl.Collator(this.locale, { numeric: true, sensitivity: 'base' });
            this.collators.set(this.locale, collator);
        }
        return collator.compare(a, b);
    };

    /** The name of a locale in its own language, for the picker. */
    name(locale: string): string {
        try {
            const name = new Intl.DisplayNames([locale], { type: 'language' }).of(locale) ?? locale;
            return name.charAt(0).toLocaleUpperCase(locale) + name.slice(1);
        } catch {
            return locale;
        }
    }
}

export const i18n = new I18n();
export const t = i18n.t;
