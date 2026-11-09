// Every catalogue the page carries, by locale. en-GB is the source; the rest translate it.
// To add a locale, add its file here (see CONTRIBUTING.md).

import type { Catalogue } from './en-GB';
import enGB from './en-GB';

export const CATALOGUES: Record<string, Catalogue> = {
    'en-GB': enGB,
};
