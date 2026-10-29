import { getContext } from 'svelte';
import { readable, type Readable } from 'svelte/store';
import type { MicroTag } from './types';

export const IDENTITY_DISPLAY_CONTEXT = Symbol('identity display');
export const IDENTITY_DISPLAY_COOKIE = 'kura_identity_display';
export const IDENTITY_CATEGORIES = [
  { key: 'artist', label: 'アーティスト', icon: 'artist' },
  { key: 'character', label: 'キャラクター', icon: 'character' },
  { key: 'copyright', label: '作品', icon: 'manga' },
] as const;
export type IdentityCategory = typeof IDENTITY_CATEGORIES[number]['key'];
export type IdentityDisplay = Record<IdentityCategory, boolean> & {
  viewer: boolean; namesPerCategory: 1 | 2 | 3 | 5;
};
export function readIdentityDisplay(raw?: string): IdentityDisplay {
  const result: IdentityDisplay = { viewer: true, artist: true, character: true, copyright: true, namesPerCategory: 2 };
  try {
    const value = JSON.parse(raw ?? '{}');
    if (!value || typeof value !== 'object') return result;
    for (const key of ['viewer', 'artist', 'character', 'copyright'] as const) {
      if (typeof value[key] === 'boolean') result[key] = value[key];
    }
    if ([1, 2, 3, 5].includes(value.namesPerCategory)) result.namesPerCategory = value.namesPerCategory;
  } catch { /* Invalid or old browser preferences use safe display defaults. */ }
  return result;
}
export function identityDisplay(): Readable<IdentityDisplay> {
  return getContext<Readable<IdentityDisplay>>(IDENTITY_DISPLAY_CONTEXT) ?? readable(readIdentityDisplay());
}
export function identityGroups(tags: MicroTag[], prefs: IdentityDisplay) {
  return IDENTITY_CATEGORIES.filter(category => prefs[category.key]).map(category => {
    const seen = new Set<string>();
    const matches = tags.filter(tag => {
      if (tag.category !== category.key || !tag.names[0]) return false;
      // names[] contains aliases of ONE identity, not additional people.
      const key = tag.id == null ? tag.names[0] : String(tag.id);
      if (seen.has(key)) return false;
      seen.add(key); return true;
    });
    return { ...category, tags: matches };
  }).filter(group => group.tags.length > 0);
}
