import { fetchTagsBrowse, fetchTagCategories } from '#lib/api.ts';
import { error } from '@sveltejs/kit';
import { readTagCleanup, catalogReturn } from '#lib/tagCatalog.ts';
import { messagesFor, resolveUiLocale, UI_LOCALE_COOKIE } from '#lib/i18n/index.ts';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ fetch, url, cookies, request }) => {
  const query = url.searchParams.get('q') ?? '';
  const category = url.searchParams.get('cat') ?? '';
  const cleanup = readTagCleanup(url.searchParams.get('cleanup'));
  const returnTo = catalogReturn(url.searchParams.get('from'));
  const targetParam = url.searchParams.get('tagId');
  if (targetParam !== null) {
    const m = messagesFor(resolveUiLocale(cookies.get(UI_LOCALE_COOKIE), request.headers.get('accept-language')));
    const tagId = Number(targetParam);
    if (!Number.isSafeInteger(tagId) || tagId <= 0) error(400, m.tags.edit.badTagId);
    const [result, categories] = await Promise.all([
      fetchTagsBrowse('', '', 0, 60, fetch, { thumbnails: false, tagId }), fetchTagCategories(fetch)]);
    const target = result.results[0];
    if (!target) error(404, m.tags.edit.tagNotFound);
    return { tagsData: { results: [target], total: 1, offset: 0, limit: 60, query }, categories,
      query, category, cleanup, returnTo, target };
  }
  const [tagsData, categories] = await Promise.all([
    fetchTagsBrowse(query, category, 0, 60, fetch, { thumbnails: false, cleanup }),
    fetchTagCategories(fetch),
  ]);
  return { tagsData, categories, query, category, cleanup, returnTo, target: null };
};
