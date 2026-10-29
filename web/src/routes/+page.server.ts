import * as env from '$app/env/private';
import { gridScope, gridCookie, readGridSettings, defaultGridSettings } from '#lib/gridSettings.ts';
import { apiErrorMessage } from '#lib/apiError.ts';
import { messagesFor, resolveUiLocale, UI_LOCALE_COOKIE } from '#lib/i18n/index.ts';
import type { PageServerLoad } from './$types';

const INTERNAL_API = env.INTERNAL_API_URL ?? 'http://kura-api:8000';

export const load: PageServerLoad = async ({ url, cookies, request }) => {
  const q = url.searchParams.get('q') ?? '';
  let mode = url.searchParams.get('mode') ?? '';
  const rawSort = url.searchParams.get('sort') ?? '';
  let sort = ['id', 'updated', 'page'].includes(rawSort) ? rawSort : '';
  let dir: 'asc' | 'desc' = url.searchParams.get('dir') === 'asc' ? 'asc' : 'desc';
  const lb = url.searchParams.get('lb') ?? '';
  const lbp = url.searchParams.get('lbp') ?? '';
  // scroll_to defaults to the lightbox target (lb) — a direct/cold nav to
  // `?lb=<id>` (bookmark, reload, or the header jump-to-post box in lightbox
  // mode) must center the grid on that post same as an explicit ?scroll_to=
  // would, or afterNavigate's scrollTo() in +page.svelte has nothing to do.
  const scrollTo = url.searchParams.get('scroll_to') || lb;
  const bulk = url.searchParams.get('bulk') === '1';
  // bulkTag: タグ検証ボタン (POST /pools/from-tag) の 303 先が一括編集の
  // target タグをプリセットするための URL param (user 2026-08-02)。
  const bulkTag = url.searchParams.get('bulkTag') ?? '';
  // embed=1: iframe 埋め込み (タグブラウザの結果ペイン)。共通ヘッダーは親が出すので隠す。
  const embed = url.searchParams.get('embed') === '1';
  const showEmpty = url.searchParams.get('empty') === '1';
  // open=single|manga|lightbox: 埋め込み側 (タグブラウザ) がクリック時の開き方を
  // 明示指定する。埋め込みヘッダーは親 (/tags) が出すので、その openMode を
  // ここ経由でグリッド側に伝える (poolMode/bulk と同じ URL param 規約)。
  const openParamRaw = url.searchParams.get('open') ?? '';
  const open = (openParamRaw === 'single' || openParamRaw === 'manga' || openParamRaw === 'lightbox')
    ? openParamRaw : '';
  const idxParam = url.searchParams.get('idx');
  let initialOffset = idxParam ? Math.max(0, parseInt(idxParam) - 20) : 0;

  // Pool 一覧表示の記憶: 素の着地 (mode/query/embed/bulk/idx/lb いずれも無し) のときだけ
  // cookie で前回 pool 一覧だったかを復元する。URL が何かを明示していれば常に優先。
  // `?q=`（明示的な空検索）は has('q') で素の着地と区別する — 検索を空にして
  // 押し直したら pool 一覧でなく全件投稿を出す（!q だと空文字も復元発火してしまう）。
  // `lb` も明示的なターゲット指定なので同様に除外 — さもないと直接 URL で lightbox
  // へ入ろうとしても pool 一覧に横取りされ、lightbox が一切開かなくなる。
  if (!mode && !url.searchParams.has('q') && !embed && !bulk && !idxParam && !lb
      && cookies.get('kura_pool') === '1') {
    mode = 'pools';
  }

  const displayScope = gridScope(q, mode);
  const savedDisplay = embed ? null : readGridSettings(cookies.get(gridCookie(displayScope)), displayScope);
  const displaySettings = savedDisplay ?? defaultGridSettings(displayScope);
  sort = (sort === 'page' && displayScope !== 'pool') ? displaySettings.sort : (sort || displaySettings.sort);
  dir = ['asc', 'desc'].includes(url.searchParams.get('dir') ?? '') ? dir : displaySettings.dir;
  const effectiveDisplay = { ...displaySettings, sort, dir };
  const safeMode = cookies.get('kura_safe') === '1';

  // Pool 一覧モードではポスト取得不要
  if (mode === 'pools') {
    return { displaySettings: effectiveDisplay, hasSavedDisplay: !!savedDisplay, displayScope, posts: { results: [], total: 0, offset: 0, limit: 40, query: '' }, query: q, mode, scrollTo, initialOffset, bulk, bulkTag, embed, showEmpty, safeMode, lb, lbp, open, sort, dir, searchError: null };
  }

  // 直接/コールド nav で `?lb=<id>` が来た場合（ブックマーク・再読込・lightbox モード時の
  // ヘッダー jump-to-post box）は `idx` を伴わない。/post/[id] の index lookup と同じ
  // API で server 側にその post の位置を解決し、初回 fetch の window に必ず含める —
  // でなければ +page.svelte の data 駆動 lightbox-open effect が postsArr の中に
  // その post を見つけられず、lightbox も開かず grid のスクロールも合わない。
  if (!idxParam && lb) {
    const lbId = Number(lb);
    if (Number.isFinite(lbId) && lbId > 0) {
      try {
        const idxRes = await fetch(
          `${INTERNAL_API}/api/post/${lbId}/index?query=${encodeURIComponent(q)}${sort ? `&sort=${sort}&dir=${dir}` : ''}${safeMode ? '&safe=1' : ''}`
        );
        if (idxRes.ok) {
          const { idx } = await idxRes.json();
          if (Number.isFinite(idx)) initialOffset = Math.max(0, idx - 20);
        }
      } catch {
        // best-effort — 解決できなければ offset=0 のまま (従来挙動にフォールバック)
      }
    }
  }

  // セーフモードは API 側で cookie/param 駆動で一括強制する。query は raw のまま渡し、
  // safe=1 を付けるだけ（grid と post ページの index 空間を一致させ、漫画ビューの
  // クリック対象ズレを防ぐ）。
  const params = new URLSearchParams({ query: q, limit: '40', offset: String(initialOffset) });
  if (sort) { params.set('sort', sort); params.set('dir', dir); }
  if (safeMode) params.set('safe', '1');
  const res = await fetch(`${INTERNAL_API}/api/posts?localize=true&${params}`);
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const fix = body?.detail?.fix;
    const searchError = typeof fix === 'string' && fix.trim()
      ? fix
      : apiErrorMessage(body, messagesFor(resolveUiLocale(cookies.get(UI_LOCALE_COOKIE), request.headers.get('accept-language'))).home.grid.searchFailedHttp(res.status));
    return { displaySettings: effectiveDisplay, hasSavedDisplay: !!savedDisplay, displayScope, posts: { results: [], total: 0, offset: 0, limit: 40, query: q }, query: q, mode, scrollTo, initialOffset, bulk, bulkTag, embed, showEmpty, safeMode, lb, lbp, open, sort, dir, searchError };
  }
  const posts = await res.json();
  return { displaySettings: effectiveDisplay, hasSavedDisplay: !!savedDisplay, displayScope, posts, query: q, mode, scrollTo, initialOffset, bulk, bulkTag, embed, showEmpty, safeMode, lb, lbp, open, sort, dir, searchError: null };
};
