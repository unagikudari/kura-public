import * as env from '$app/env/private';
import { credential, SESSION_COOKIE } from './access';
import type { RequestHandler } from '@sveltejs/kit';
import { messagesFor, resolveUiLocale, UI_LOCALE_COOKIE, type Messages } from '#lib/i18n/index.ts';

// Server-side auth proxy.
//
// The browser calls /m/<path> (same-origin, NO credentials in the page). This
// Shared by /api, /m and /data. Every read and command requires a verified
// session or explicit service credential. Secrets stay in server-side headers.
const INTERNAL = (env.INTERNAL_API_URL || 'http://127.0.0.1:8001').replace(/\/$/, '');

// kura-api gates the machine-gated extension routes behind a machine key (api/main.py
// `_MACHINE_GATE` -> api/auth.py require_api_key, enforce mode). It is attached HERE,
// server-side, for exactly the reason the session token is: the browser must never hold it.
// Measured 2026-09-16 — without it EVERY write to those routes through this proxy is a 401 that
// no amount of signing in can clear, because that gate is a router-level dependency
// and runs before token auth. The gate shipped in observe mode (kura d6dc51f), so
// the break only appeared when enforce was switched on.
const API_KEY = env.KURA_API_KEY || '';

// Request headers worth forwarding upstream (besides content-type/authorization).
// `range`/`if-*` let media GETs and conditional requests work through the proxy.
const FWD_REQ_HEADERS = ['range', 'accept', 'if-none-match', 'if-modified-since'];
// Response headers worth forwarding back so ranged/streamed media and downloads
// behave (the proxy used to drop everything but content-type).
const FWD_RESP_HEADERS = [
  'content-type', 'content-length', 'accept-ranges', 'content-range',
  'cache-control', 'content-disposition', 'etag', 'last-modified',
];

// A rejected fetch does not establish whether the upstream applied a write.
// Only safe reads may be retried without an end-to-end idempotency contract.
// undici re-sends a request when the upstream answers an authentication challenge.
// Measured 2026-09-16 against a real server: 401 and 407 are the ONLY statuses that
// do this — 200/301/302/400/403/405/409/422/429/500/503 all pass a streamed body
// through untouched. A streamed body cannot be replayed, so that re-send fails with
// this exact cause and the real status never surfaces: the caller was told
// `upstream_result_unknown` ("it may have been submitted") for a request the upstream
// had in fact refused. Bodies stay streamed — replayability is what makes a lost
// write repeatable, and that is the property this proxy must not have — so the
// challenge is REPORTED, never retried.
//
// The string is undici's, not ours. An unrecognised failure falls through to the
// unchanged `transportFailure` path, so if undici rewords it the behaviour degrades
// to today's, never to a wrong claim; `tests/proxy-transport.test.mjs` drives a real
// 401 through this route so the reword shows up as a failing test.
function isUnreplayableAuthChallenge(err: unknown): boolean {
  const cause = (err as { cause?: { message?: string } } | undefined)?.cause;
  return cause?.message === 'expected non-null body source';
}

// An auth challenge is refusal, not an unknown outcome: kura-api's key gate and its
// token auth are both router-level dependencies, so they answer before any handler
// runs and nothing was applied.
function authChallenge(m: Messages): Response {
  return new Response(JSON.stringify({
    detail: {
      error: 'upstream_auth_rejected',
      fix: m.pages.proxy.authRejected,
    },
  }), { status: 401, headers: { 'content-type': 'application/json' } });
}

// The previous `catch {}` bound nothing and printed nothing. Diagnosing the 401
// above therefore took a reproduction from scratch, because the one process that
// had seen the error had thrown it away.
function logProxyFailure(method: string, path: string, err: unknown, retried: boolean): void {
  const e = err as { name?: string; message?: string; cause?: { code?: string; message?: string } } | undefined;
  const cause = e?.cause ? ` | cause: ${e.cause.code ?? e.cause.message ?? ''}` : '';
  console.error(`[m-proxy] ${method} /api/${path} upstream fetch failed`
    + `${retried ? ' (retry)' : ''}: ${e?.name ?? 'Error'}: ${e?.message ?? String(err)}${cause}`);
}

function transportFailure(m: Messages, safeRead: boolean, retried: boolean): Response {
  return new Response(JSON.stringify({
    detail: {
      error: safeRead ? 'upstream_response_unavailable' : 'upstream_result_unknown',
      context: { retried },
      fix: safeRead
        ? m.pages.proxy.responseUnavailable
        : m.pages.proxy.resultUnknown,
    },
  }), { status: 502, headers: { 'content-type': 'application/json' } });
}

const handler: RequestHandler = async ({ params, request, url, cookies }) => {
  const m = messagesFor(resolveUiLocale(cookies.get(UI_LOCALE_COOKIE), request.headers.get('accept-language')));
  const path = params.path ?? '';
  // Path-traversal guard: params.path is percent-decoded, and fetch() would
  // normalize `..` segments, letting `/m/%2e%2e/<route>` escape the `/api/`
  // prefix while still carrying the token. Reject any `..` segment.
  if (path.includes('..') || /[\\\x00-\x1f]/.test(path)) {
    return new Response('bad path', { status: 400 });
  }
  const media = url.pathname.startsWith('/data/');
  const target = `${INTERNAL}/${media ? 'data' : 'api'}/${path.split('/').map(encodeURIComponent).join('/')}${url.search}`;

  const method = request.method;
  const login = !media && path === 'auth/login' && method === 'POST';
  let identity;
  try { identity = login ? null : await credential(request, cookies.get(SESSION_COOKIE)); }
  catch { return new Response(JSON.stringify({ detail: { error: 'authentication_unavailable', fix: m.pages.proxy.authUnavailable } }), { status: 503, headers: { 'content-type': 'application/json', 'cache-control': 'no-store' } }); }
  if (!login && !identity) {
    return new Response(JSON.stringify({ detail: m.pages.proxy.signInRequired }), {
      status: 401,
      headers: { 'content-type': 'application/json' },
    });
  }

  const headers = new Headers();
  const ct = request.headers.get('content-type');
  if (ct) headers.set('content-type', ct);
  if (identity?.authorization) headers.set('authorization', identity.authorization);
  if (identity && API_KEY) headers.set('x-api-key', API_KEY);
  // Collector reads are authenticated too; preserve the browser's safety gate.
  // Forward only this preference, never the session cookie to upstream adapters.
  if (path.startsWith('collector-next/') && cookies.get('kura_safe') === '1') {
    headers.set('cookie', 'kura_safe=1');
  }
  for (const h of FWD_REQ_HEADERS) {
    const v = request.headers.get(h);
    if (v) headers.set(h, v);
  }

  // Stream the request body straight through instead of buffering it with
  // arrayBuffer(): large uploads (video) would otherwise sit fully in memory
  // (this process is capped at MemoryMax=1G) and anything over BODY_SIZE_LIMIT
  // threw on the buffer read → 500. Streaming keeps memory flat; kura-api
  // spools the multipart to disk on its side.
  const init: RequestInit = { method, headers, redirect: 'manual' };
  if (method !== 'GET' && method !== 'HEAD' && request.body !== null) {
    init.body = request.body;
    // `duplex: 'half'` is required by the fetch spec when sending a stream body.
    (init as unknown as { duplex: 'half' }).duplex = 'half';
  }
  const safeRead = method === 'GET' || method === 'HEAD';
  let upstream: Response;
  try {
    upstream = await fetch(target, init);
  } catch (err) {
    logProxyFailure(method, path, err, false);
    if (isUnreplayableAuthChallenge(err)) return authChallenge(m);
    if (!safeRead) return transportFailure(m, false, false);
    try {
      upstream = await fetch(target, init);
    } catch (retryErr) {
      logProxyFailure(method, path, retryErr, true);
      if (isUnreplayableAuthChallenge(retryErr)) return authChallenge(m);
      return transportFailure(m, true, true);
    }
  }

  // Stream the response body through instead of buffering it with
  // arrayBuffer(): buffering pins large media (video/original) in the
  // 1G-capped web process and breaks range requests. Forward the headers
  // that make ranged/streamed media and downloads work.
  const respHeaders = new Headers();
  for (const h of FWD_RESP_HEADERS) {
    const v = upstream.headers.get(h);
    if (v) respHeaders.set(h, v);
  }
  // Authentication runs before conditional/range responses too. Private permits
  // the browser's existing thumbnail cache without sharing library content in
  // a public intermediary cache.
  const cache = respHeaders.get('cache-control');
  respHeaders.set('cache-control', cache ? cache.replace(/\bpublic\b/g, 'private') : 'private, no-cache');
  const location = upstream.headers.get('location');
  if (location) {
    const redirect = new URL(location, target);
    respHeaders.set('location', redirect.origin === new URL(INTERNAL).origin ? redirect.pathname + redirect.search : redirect.href);
  }
  return new Response(upstream.body, {
    status: upstream.status,
    headers: respHeaders,
  });
};

export const GET = handler;
export const POST = handler;
export const PUT = handler;
export const PATCH = handler;
export const DELETE = handler;
