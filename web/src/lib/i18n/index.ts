// The UI's language. One request, one language: +layout.server.ts resolves it (the
// kura_ui_locale cookie, else the browser's Accept-Language, else English) and the layout
// puts it in context, so server rendering never shares a language across users. Changing it
// reloads the page; components read it once.
//
// Kept in-house on purpose: the usual library (Paraglide) fetches and runs plugin code from
// a CDN on every build, and every other dependency here is pinned by hash.
import { getContext } from 'svelte';
import { enMessages as en, type Messages } from './en.ts';
import { ja } from './ja.ts';

export type UiLocale = 'en' | 'ja';
export const UI_LOCALES: readonly UiLocale[] = ['en', 'ja'];
export const UI_LOCALE_COOKIE = 'kura_ui_locale';
export const UI_LOCALE_CONTEXT = Symbol('ui locale');
const CATALOG: Record<UiLocale, Messages> = { en, ja };

export function readUiLocale(value: string | undefined | null): UiLocale | null {
  return value === 'en' || value === 'ja' ? value : null;
}

/** Server side only. The person's choice (cookie) wins; then the install's default
 * (KURA_UI_LOCALE in web/.env.server, for an operator who wants one language for everyone
 * who has not chosen); then the browser's languages; then English. */
export function resolveUiLocale(cookie: string | undefined, acceptLanguage: string | null): UiLocale {
  const chosen = readUiLocale(cookie) ?? readUiLocale(globalThis.process?.env?.KURA_UI_LOCALE);
  if (chosen) return chosen;
  const ranked = (acceptLanguage ?? '').split(',').map((part, index) => {
    const [tag, ...params] = part.trim().toLowerCase().split(';');
    const q = Number(params.find((p) => p.trim().startsWith('q='))?.split('=')[1] ?? 1);
    return { tag, q: Number.isFinite(q) ? q : 0, index };
  }).filter((r) => r.tag && r.q > 0).sort((a, b) => b.q - a.q || a.index - b.index);
  for (const { tag } of ranked) {
    const base = tag.split('-')[0];
    if (base === 'ja' || base === 'en') return base;
  }
  return 'en';
}

export function messagesFor(locale: UiLocale): Messages {
  return CATALOG[locale];
}

/** The current request's language (inside a component). */
export function uiLocale(): UiLocale {
  return getContext<UiLocale>(UI_LOCALE_CONTEXT) ?? 'en';
}

/** The current request's messages (inside a component). */
export function t(): Messages {
  return messagesFor(uiLocale());
}

export type { Messages };
