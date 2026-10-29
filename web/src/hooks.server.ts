import { redirect } from '@sveltejs/kit';
import type { Handle } from '@sveltejs/kit/hooks';
import { SESSION_COOKIE } from '#lib/server/session.ts';
import { MACHINE_HEADER, machineIdentity } from '#lib/server/machine.ts';
import { credential } from '#lib/server/access.ts';
import { error } from '@sveltejs/kit';

// L1 UI guard: every page requires a signed-in session; an unauthenticated
// request to a page is redirected to /login. Public routes: the login + logout
// endpoints. API and media handlers use the same verifier and return 401 rather
// than a page redirect. Static assets are public and contain no library data.
const PUBLIC = ['/login', '/logout'];

export const handle: Handle = async ({ event, resolve }) => {
  const { pathname } = event.url;
  // GHSA-pfhg-q3qv-9q6r (SvelteKit < 3, Low): an enhanced form on a path starting with
  // `//` can be turned into an open redirect. No kura route accepts such a path; refuse
  // them before routing, as the advisory's workaround says, until kit 3.
  if (pathname.startsWith('//')) throw error(404, 'Not found');
  const isPublic = PUBLIC.some((p) => pathname === p || pathname.startsWith(p + '/'));
  const isProxy = ['/m/', '/api/', '/data/'].some(prefix => pathname.startsWith(prefix));
  if (event.route.id !== null && !isPublic && !isProxy) {
    let identity;
    try { identity = await credential(event.request, event.cookies.get(SESSION_COOKIE)); }
    catch { throw error(503, '認証サービスに接続できません。しばらくしてから再試行してください。'); }
    if (!identity) {
      // A fleet agent presenting its broker key reads the pages anonymously
      // (no session, no user token — see #lib/server/machine). Anything else
      // goes to /login as before.
      const who = await machineIdentity(event.request.headers.get(MACHINE_HEADER));
      if (!who) throw redirect(303, '/login');
      event.locals.machine = who;
    }
  }
  const response = await resolve(event);
  response.headers.set('x-content-type-options', 'nosniff');
  response.headers.set('x-frame-options', 'SAMEORIGIN');
  response.headers.set('content-security-policy', "frame-ancestors 'self'; object-src 'none'; base-uri 'self'");
  if (event.url.protocol === 'https:') response.headers.set('strict-transport-security', 'max-age=86400');
  if (response.headers.get('content-type')?.includes('text/html')) {
    // SSR 文書は一切キャッシュさせない (no-store — 最も単純な方針)。
    // 経緯: Cache-Control 無し時代の文書がブラウザに URL 毎の「汚染キャッシュ項目」
    // として残り、リンク遷移でその項目自身のヘッダー (無指定+ETag=ヒューリスティック
    // 鮮度) により再検証なしで再利用され続けた — サーバ側で新応答にヘッダーを
    // 足しても既存項目は治らない。よって下の Clear-Site-Data で一度だけ全パージし、
    // 以後は no-store で二度と文書をキャッシュさせない (2026-07-20 根治)。
    // hashed immutable assets (/_app/immutable/*) は text/html でないため対象外。
    response.headers.set('cache-control', 'no-store');
    // 汚染済みキャッシュの一回限りの掃除: cookie を目印に、初回応答でだけ
    // ブラウザの HTTP キャッシュを全消去させる (assets は再取得一回で復帰)。
    if (!event.cookies.get('kura_cachefix1')) {
      response.headers.set('clear-site-data', '"cache"');
      response.headers.append('set-cookie',
        'kura_cachefix1=1; Path=/; Max-Age=31536000; SameSite=Lax');
    }
  }
  return response;
};
