import { fetchTagsBrowse, fetchTagCategories } from '#lib/api.ts';
import { error } from '@sveltejs/kit';
import { readTagCleanup, catalogReturn } from '#lib/tagCatalog.ts';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ fetch, url }) => {
  const query = url.searchParams.get('q') ?? '';
  const category = url.searchParams.get('cat') ?? '';
  const cleanup = readTagCleanup(url.searchParams.get('cleanup'));
  const returnTo = catalogReturn(url.searchParams.get('from'));
  const targetParam = url.searchParams.get('tagId');
  if (targetParam !== null) {
    const tagId = Number(targetParam);
    if (!Number.isSafeInteger(tagId) || tagId <= 0) error(400, 'タグIDが不正です。');
    const [result, categories] = await Promise.all([
      fetchTagsBrowse('', '', 0, 60, fetch, { thumbnails: false, tagId }), fetchTagCategories(fetch)]);
    const target = result.results[0];
    if (!target) error(404, 'タグが見つかりません。');
    return { tagsData: { results: [target], total: 1, offset: 0, limit: 60, query }, categories,
      query, category, cleanup, returnTo, target };
  }
  const [tagsData, categories] = await Promise.all([
    fetchTagsBrowse(query, category, 0, 60, fetch, { thumbnails: false, cleanup }),
    fetchTagCategories(fetch),
  ]);
  return { tagsData, categories, query, category, cleanup, returnTo, target: null };
};
