// Every catalogue the page carries, by locale. en-GB is the source; the rest translate it.
// To add a locale, add its file here (see CONTRIBUTING.md).

import ar from './ar';
import de from './de';
import type { Catalogue } from './en-GB';
import enGB from './en-GB';
import enUS from './en-US';
import es from './es';
import fr from './fr';
import hi from './hi';
import it from './it';
import ja from './ja';
import ko from './ko';
import nl from './nl';
import pl from './pl';
import ptBR from './pt-BR';
import zhHans from './zh-Hans';

export const CATALOGUES: Record<string, Catalogue> = {
    'en-GB': enGB,
    'en-US': enUS,
    ar: ar,
    de: de,
    es: es,
    fr: fr,
    hi: hi,
    it: it,
    ja: ja,
    ko: ko,
    nl: nl,
    pl: pl,
    'pt-BR': ptBR,
    'zh-Hans': zhHans,
};
