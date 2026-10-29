import { fetchTagsBrowse, fetchTagCategories } from '#lib/api.ts';
import { redirect } from '@sveltejs/kit';
import { TAG_MANAGE_CATEGORY_COOKIE, readTagCleanup } from '#lib/tagCatalog.ts';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ fetch, url, cookies }) => {
  // Resolve the preference before reading tags, and put even "all" in the URL
  // so a later cookie change cannot rewrite this entry's browser history.
  if (!url.searchParams.has('cat')) {
    const remembered = cookies.get(TAG_MANAGE_CATEGORY_COOKIE) ?? '';
    const params = new URLSearchParams(url.searchParams);
    params.set('cat', remembered.length <= 128 ? remembered : '');
    throw redirect(307, '/tags/manage?' + params);
  }
  const query = url.searchParams.get('q') ?? '';
  const category = url.searchParams.get('cat') ?? '';
  const cleanup = readTagCleanup(url.searchParams.get('cleanup'));
  const view = url.searchParams.get('view') === 'list' ? 'list' : 'cards';
  const [tagsData, categories] = await Promise.all([
    fetchTagsBrowse(query, category, 0, 200, fetch, { thumbnails: view !== 'list', cleanup }),
    fetchTagCategories(fetch),
  ]);
  return { tagsData, categories, query, category, cleanup, view };
};
