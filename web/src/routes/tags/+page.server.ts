import type { PageServerLoad } from './$types';
import { BASE } from '#lib/api.ts';
import { messagesFor, resolveUiLocale, UI_LOCALE_COOKIE } from '#lib/i18n/index.ts';
import type { TagCategory, TagDetail } from '#lib/types.ts';

type TagChoice = Pick<TagDetail, 'id' | 'names' | 'localizedNames' | 'category' | 'categoryColor' | 'usages'>;
export const load: PageServerLoad = async ({ fetch, cookies, request }) => {
  try {
    const response = await fetch(`${BASE}/api/tag-browser`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const sidebar: { categories: TagCategory[]; tagsByCat: Record<string, TagChoice[]> } = await response.json();
    return { ...sidebar, sidebarError: '' };
  } catch {
    return { categories: [] as TagCategory[], tagsByCat: {} as Record<string, TagChoice[]>,
      sidebarError: messagesFor(resolveUiLocale(cookies.get(UI_LOCALE_COOKIE), request.headers.get('accept-language'))).tags.browse.sidebarError };
  }
};
