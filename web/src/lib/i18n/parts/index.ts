// One file per screen group: export const en = {...}; export const ja: Shape<typeof en> = {...}.
// Each joins the catalog under its namespace (m.home.*, m.tags.*, …).
import * as home from './home.ts';
import * as tags from './tags.ts';
import * as collector from './collector.ts';
import * as pages from './pages.ts';

export const parts = {
  en: { home: home.en, tags: tags.en, collector: collector.en, pages: pages.en },
  ja: { home: home.ja, tags: tags.ja, collector: collector.ja, pages: pages.ja },
};
