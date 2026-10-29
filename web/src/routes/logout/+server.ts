import * as env from '$app/env/private';
import { error, redirect } from '@sveltejs/kit';
import { authHeaderFromSession, readSession, SESSION_COOKIE } from '#lib/server/session.ts';
import { messagesFor, resolveUiLocale, UI_LOCALE_COOKIE } from '#lib/i18n/index.ts';
import type { RequestHandler } from './$types';

const INTERNAL = (env.INTERNAL_API_URL || 'http://127.0.0.1:8001').replace(/\/$/, '');

// Revoke the session's token server-side (so a leaked cookie can't be reused),
// then clear the cookie.
export const POST: RequestHandler = async ({ cookies, request, url }) => {
  if (request.headers.get('origin') !== url.origin) throw error(403, messagesFor(resolveUiLocale(cookies.get(UI_LOCALE_COOKIE), request.headers.get('accept-language'))).errors.sameOrigin);
  const session = readSession(cookies.get(SESSION_COOKIE));
  const auth = authHeaderFromSession(session);
  if (auth) {
    try {
      await fetch(`${INTERNAL}/api/auth/logout`, { method: 'POST', headers: { authorization: auth } });
    } catch {
      // best-effort revoke; cookie clear below still logs the browser out
    }
  }
  cookies.delete(SESSION_COOKIE, { path: '/' });
  throw redirect(303, '/');
};
