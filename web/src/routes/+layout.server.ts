import * as env from '$app/env/private';
import { parseNavigationLinks } from '#lib/navigationLinks.ts';
import { readSession, SESSION_COOKIE } from '#lib/server/session.ts';
import { readIdentityDisplay, IDENTITY_DISPLAY_COOKIE } from '#lib/identityDisplay.ts';
import { readTagLocale } from '#lib/tagLocale.ts';
import type { LayoutServerLoad } from './$types';

const INTERNAL_API = env.INTERNAL_API_URL ?? 'http://kura-api:8000';
// AGPL-3.0: people using kura over the network are offered its source. A modified
// kura points this at its own source (KURA_SOURCE_URL in web/.env.server).
const SOURCE_URL = env.KURA_SOURCE_URL || 'https://github.com/unagikudari/kura-public';

// Surface the signed-in identity (email only — never the token) to the layout
// so the Header can show who is logged in.
//
// P4 (plan kura-web-componentization): category→color map を全 route に SSR
// 供給する単一点。DB tag_category.color が SoT — +layout.svelte が
// setCategoryColors() で categoryColor.ts に注入する。取得失敗時は null を
// 返してページは殺さない (categoryColor.ts のリフト表 fallback が同一画素で
// 引き受ける)。localhost の軽量 SELECT なので per-request で許容。
export const load: LayoutServerLoad = async ({ cookies }) => {
  const s = readSession(cookies.get(SESSION_COOKIE));
  let categoryColors: Record<string, string> | null = null;
  try {
    const res = await fetch(`${INTERNAL_API}/api/tag-categories`, {
      signal: AbortSignal.timeout(1500),
    });
    if (res.ok) {
      const cats: { name: string; color: string }[] = await res.json();
      if (cats.length) {
        categoryColors = Object.fromEntries(cats.map((c) => [c.name, c.color]));
      }
    }
  } catch {
    // kura-api down / timeout → fallback (明示、二重真実ではない)
  }
  return { navigationLinks: parseNavigationLinks(env.KURA_NAV_LINKS), identityDisplay: readIdentityDisplay(cookies.get(IDENTITY_DISPLAY_COOKIE)), tagLocale: readTagLocale(cookies.get('kura_tag_locale')), user: s ? { email: s.e, name: s.u } : null, categoryColors, sourceUrl: SOURCE_URL };
};
