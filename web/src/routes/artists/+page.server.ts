import { fetchTagsBrowse, fetchAllTagLinks, type TagLinkRow } from '#lib/api.ts';
import { proxiedFetch } from '#lib/proxiedFetch.ts';
import { artistIntegration } from '#lib/artistWorkspace.ts';
import { messagesFor, resolveUiLocale, UI_LOCALE_COOKIE } from '#lib/i18n/index.ts';
import type { PageServerLoad } from './$types';
export const load: PageServerLoad = async ({ fetch, url, cookies, request }) => {
  const m = messagesFor(resolveUiLocale(cookies.get(UI_LOCALE_COOKIE), request.headers.get('accept-language'))).pages.artists;
  const read = proxiedFetch(fetch);
  const query = (url.searchParams.get('q') ?? '').slice(0, 200);
  const number = (value: string | null, fallback: number) => value && /^\d+$/.test(value) && Number.isSafeInteger(+value) && +value > 0 ? +value : fallback;
  const view = url.searchParams.get('view') === 'unassigned' ? 'unassigned' : 'artists';
  const current = Math.min(number(url.searchParams.get('page'), 1), 10000);
  const id = number(url.searchParams.get('id'), 0);
  const [catalog, feeds, otherLinks] = await Promise.allSettled([
    view === 'unassigned' ? Promise.resolve(null) : fetchTagsBrowse(query, 'artist', (current - 1) * 60, 60, read, id ? { tagId: id } : {}),
    artistIntegration ? artistIntegration.load(fetch) : Promise.resolve(null),
    fetchAllTagLinks({ excludeCategory: 'artist', limit:100 }, read),
  ]);
  const tags = catalog.status === 'fulfilled' ? catalog.value : null;
  const ids = tags?.results.map(a => a.id) ?? [];
  let links: TagLinkRow[] = [], linksError: string | null = null;
  try { links = await fetchAllTagLinks({ tagIds: ids, limit: 1000 }, read); }
  catch { linksError = m.linksError; }
  return { query, current, id, tags, links, linksError, linksLimited: links.length >= 1000,
    unclassified: otherLinks.status === 'fulfilled' ? otherLinks.value : [],
    unclassifiedError: otherLinks.status === 'rejected' ? m.unclassifiedError : null,
    catalogError: catalog.status === 'rejected' ? m.catalogError : null,
    sources: feeds.status === 'fulfilled' ? feeds.value : null,
    sourcesError: feeds.status === 'rejected' ? m.sourcesError : null,
    view,
    assignUrl: (url.searchParams.get('source') ?? '').slice(0,4096) };
};
