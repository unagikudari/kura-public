import type { PageServerLoad } from './$types';
import { BASE } from '#lib/api.ts';
import type { TagCategory, TagDetail } from '#lib/types.ts';

type TagChoice = Pick<TagDetail, 'id' | 'names' | 'localizedNames' | 'category' | 'categoryColor' | 'usages'>;
export const load: PageServerLoad = async ({ fetch }) => {
  try {
    const response = await fetch(`${BASE}/api/tag-browser`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const sidebar: { categories: TagCategory[]; tagsByCat: Record<string, TagChoice[]> } = await response.json();
    return { ...sidebar, sidebarError: '' };
  } catch {
    return { categories: [] as TagCategory[], tagsByCat: {} as Record<string, TagChoice[]>,
      sidebarError: 'タグ一覧を取得できませんでした。ページを再読み込みしてください。' };
  }
};
