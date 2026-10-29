import { getContext } from 'svelte';
import { readable, type Writable, type Readable } from 'svelte/store';

export const TAG_LOCALE_CONTEXT = Symbol('tag locale');
export type TagLocale = 'ja' | 'en' | 'und';
export function readTagLocale(value: string | undefined): TagLocale {
  return value === 'en' || value === 'und' ? value : 'ja';
}
export function tagLocale(): Readable<TagLocale> {
  return getContext<Writable<TagLocale>>(TAG_LOCALE_CONTEXT) ?? readable<TagLocale>('ja');
}
export function tagDisplayName(name: string, localizedNames: Record<string, string> | undefined, locale: string): string {
  if (locale === 'und') return name;
  return localizedNames?.[locale] ?? localizedNames?.[locale.split('-')[0]] ?? name;
}
