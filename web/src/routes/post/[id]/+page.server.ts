import * as env from '$app/env/private';
import { gridScope, gridCookie, readGridSettings, defaultGridSettings } from '#lib/gridSettings.ts';
import { error } from '@sveltejs/kit';
import { messagesFor, resolveUiLocale, UI_LOCALE_COOKIE } from '#lib/i18n/index.ts';
import type { PageServerLoad } from './$types';

const INTERNAL_API = env.INTERNAL_API_URL ?? 'http://kura-api:8000';

export const load: PageServerLoad = async ({ params, url, cookies, request }) => {
  const m = messagesFor(resolveUiLocale(cookies.get(UI_LOCALE_COOKIE), request.headers.get('accept-language')));
  const id = Number(params.id);
  const q = url.searchParams.get('q') ?? '';
  const idxParam = url.searchParams.get('idx');
  // Grid toolbar sort travels with the URL. The nearby window and the index
  // lookup MUST be built with the same ordering the grid displayed, or idx
  // points into a differently-ordered list and the viewer opens pages off
  // (「開く位置がずれる」 — the ±20 window can miss the post entirely, turning
  // the initial scroll into a silent no-op).
  const scope = gridScope(q);
  const preferences = readGridSettings(cookies.get(gridCookie(scope)), scope) ?? defaultGridSettings(scope);
  const requestedSort = url.searchParams.get('sort');
  const sort = requestedSort === 'id' || requestedSort === 'updated' || (scope === 'pool' && requestedSort === 'page') ? requestedSort : preferences.sort;
  const requestedDir = url.searchParams.get('dir');
  const dir = requestedDir === 'asc' || requestedDir === 'desc' ? requestedDir : preferences.dir;
  const op = (sort ? `&sort=${encodeURIComponent(sort)}` : '')
    + (dir ? `&dir=${encodeURIComponent(dir)}` : '');
  // Safe mode is enforced API-side; forward the signal so the post fetch, the
  // index lookup and the nearby strip all resolve against the SAME safe set the
  // grid used — otherwise the viewer opens on the wrong neighbours.
  const safe = cookies.get('kura_safe') === '1';
  const sp = safe ? '&safe=1' : '';

  // Post fetch starts immediately regardless of idx availability
  const postPromise = fetch(`${INTERNAL_API}/api/post/${id}?localize=true${safe ? '&safe=1' : ''}`);

  const fetchIdx = async (): Promise<number> => {
    const idxRes = await fetch(
      `${INTERNAL_API}/api/post/${id}/index?query=${encodeURIComponent(q)}${op}${sp}`);
    return idxRes.ok ? (await idxRes.json()).idx : 0;
  };
  const fetchNearby = async (at: number) => {
    const res = await fetch(
      `${INTERNAL_API}/api/posts?localize=true&query=${encodeURIComponent(q)}&offset=${Math.max(0, at - 20)}&limit=40${op}${sp}`);
    return res.ok ? await res.json() : { results: [], total: 0 };
  };

  let idx: number;
  if (idxParam !== null) {
    idx = Math.max(0, Number(idxParam));
  } else {
    idx = await fetchIdx();
  }

  const [postRes, nearbyFirst] = await Promise.all([postPromise, fetchNearby(idx)]);

  // In safe mode a non-safe post returns 404 (API hides it); show the safe-mode
  // message instead of a bare "not found".
  if (!postRes.ok) throw error(404, safe ? m.pages.post.notFoundSafe : m.pages.post.notFound);

  const post = await postRes.json();

  // Self-heal a stale/foreign idx: when the window built around idx does not
  // contain the post (grid state drifted, old bookmark, sort mismatch from an
  // outdated client), recompute the true index server-side and rebuild the
  // window once — first paint is then correct instead of silently mis-scrolled.
  let nearby = nearbyFirst;
  if (q && !nearby.results?.some((p: { id: number }) => p.id === id)) {
    const healed = await fetchIdx();
    if (healed !== idx) {
      idx = healed;
      nearby = await fetchNearby(idx);
    }
  }

  return { post, nearby, query: q, idx, sort, dir };
};
