import * as env from '$app/env/private';
import { fail, redirect } from '@sveltejs/kit';
import { apiErrorMessage } from '#lib/apiError.ts';
import { SESSION_COOKIE, signSession } from '#lib/server/session.ts';
import type { Actions } from './$types';

const INTERNAL = (env.INTERNAL_API_URL || 'http://127.0.0.1:8001').replace(/\/$/, '');

export const actions: Actions = {
  // email + key → local kura-api verifier. The httpOnly signed session cookie
  // keeps the returned Token credential out of page data.
  default: async ({ request, cookies, url }) => {
    if (request.headers.get('origin') !== url.origin) return fail(403, { error: '同じ画面からサインインしてください', email: '' });
    const formData = await request.formData();
    const email = String(formData.get('email') ?? '').trim();
    const key = String(formData.get('key') ?? '').trim();
    if (!email || !key) {
      return fail(400, { error: 'メールアドレスと鍵を入力してください', email });
    }

    let res: Response;
    try {
      res = await fetch(`${INTERNAL}/api/auth/login`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ email, key }),
      });
    } catch {
      return fail(502, { error: 'kura-api に接続できません', email });
    }

    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      const status = [401, 403, 503].includes(res.status) ? res.status : 400;
      return fail(status, { error: apiErrorMessage(detail, 'ログインに失敗しました'), email });
    }

    const { username, token, email: resolved } = await res.json();
    let signed: string;
    try {
      signed = signSession({ u: username, t: token, e: resolved });
    } catch (e) {
      // KURA_SESSION_SECRET missing: the credentials were right, the server
      // cannot mint a session. Say that, rather than throwing a 500 that reads
      // like a login failure — the operator fix is one env var (kura#59).
      console.error('[login]', (e as Error).message);
      return fail(503, {
        error: 'サーバ側の設定不備でセッションを発行できません (管理者に連絡してください)',
        email,
      });
    }
    cookies.set(SESSION_COOKIE, signed, {
      path: '/',
      httpOnly: true,
      secure: true,
      sameSite: 'lax',
      maxAge: 60 * 60 * 24 * 30,
    });
    throw redirect(303, '/');
  },
};
