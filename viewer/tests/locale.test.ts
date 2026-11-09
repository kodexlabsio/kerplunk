import { describe, expect, it } from 'vitest';
import { direction, resolveLocale } from '../src/lib/locale';

const available = ['en-GB', 'en-US', 'pt-BR', 'zh-Hans', 'de', 'ar'];

describe('choosing a locale', () => {
    it('takes the first requested locale it has, falling back by region and language', () => {
        expect(resolveLocale(['de-AT', 'en-US'], available)).toBe('de');
        expect(resolveLocale(['pt-BR'], available)).toBe('pt-BR');
        expect(resolveLocale(['pt-PT'], available)).toBe('pt-BR');
        expect(resolveLocale(['pt'], available)).toBe('pt-BR');
        expect(resolveLocale(['zh-CN'], available)).toBe('zh-Hans');
        expect(resolveLocale(['zh-Hans-SG'], available)).toBe('zh-Hans');
        expect(resolveLocale(['en-AU'], available)).toBe('en-GB');
        expect(resolveLocale(['en_US'], available)).toBe('en-US');
    });

    it('skips what it does not have and ends at British English', () => {
        expect(resolveLocale([null, '', 'xx', 'fr'], available)).toBe('en-GB');
        expect(resolveLocale([undefined, 'ar-EG'], available)).toBe('ar');
    });

    it('knows which scripts run right to left', () => {
        expect(direction('ar')).toBe('rtl');
        expect(direction('he-IL')).toBe('rtl');
        expect(direction('de')).toBe('ltr');
    });
});
