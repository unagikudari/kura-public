// Server-only session helper for local human login.
//
// The kura UserToken is kept in an httpOnly, signed cookie — the browser stores
// it but page JS can never read it, and it never appears in any response body.
// Signed (HMAC-SHA256) so a forged cookie is rejected.
//
// The signing key used to be KURA_SECRET, falling back to the literal
// 'change-me'. Both halves of that were wrong (kura#59):
//
//   * KURA_SECRET also names every file on disk — {post_id}_{16hex}.{ext} — so
//     it could not be rotated without renaming two million files, and it was
//     therefore never rotated. On this deployment its value is a six-character
//     dictionary word, and that value has already appeared in plaintext in an
//     nginx access log. A cookie carrying a UserToken was signed with it.
//   * the fallback signs with a key published in this file. An unset variable
//     did not mean "no key", it meant "everyone's key".
//
// So: a dedicated variable, no fallback, and a refusal to reuse KURA_SECRET.
// Unset means sessions do not work — which is visible immediately — rather than
// sessions that anyone can forge, which is not.
import crypto from 'node:crypto';
import * as env from '$app/env/private';

export const SESSION_COOKIE = 'kura_session';

/** The signing key, or null when this deployment has none configured. */
function secret(): string | null {
  const v = env.KURA_SESSION_SECRET;
  if (!v) return null;
  // Sharing a key with the filename salt is how the last one became
  // un-rotatable. Refuse rather than let it happen again by copy-paste.
  //
  // Both names, and by name: after the kura#43-2 split the variable that names
  // two million files is KURA_FS_NAME_SALT, and checking only the retired
  // KURA_SECRET would leave the exact re-join this guard exists to refuse
  // (api/dashboard_key.py iterates both for the same reason). The comparison is
  // against the value, so an operator pasting "the key we already have" into
  // KURA_SESSION_SECRET is refused whichever of the two they copied it from.
  for (const other of [env.KURA_FS_NAME_SALT, env.KURA_SECRET]) {
    if (other && v === other) return null;
  }
  return v;
}

export interface Session {
  u: string; // kura username
  t: string; // kura UserToken
  e: string; // account email (for display)
  exp?: number; // old cookies are rejected; every new signature has a deadline
}

export class SessionSecretMissing extends Error {
  constructor() {
    super(
      'KURA_SESSION_SECRET is not set (or is the same value as ' +
      'KURA_FS_NAME_SALT / KURA_SECRET), so sessions cannot be signed. Set it ' +
      "to a fresh random value in the web service's environment and restart. " +
      'It must not be KURA_FS_NAME_SALT — that one names files on disk and ' +
      'cannot be rotated independently once shared.',
    );
  }
}

export function signSession(s: Session): string {
  const key = secret();
  if (key === null) throw new SessionSecretMissing();
  const payload = Buffer.from(JSON.stringify({ ...s, exp: Math.floor(Date.now() / 1000) + 30 * 86400 })).toString('base64url');
  const sig = crypto.createHmac('sha256', key).update(payload).digest('base64url');
  return `${payload}.${sig}`;
}

export function readSession(value: string | undefined | null): Session | null {
  if (!value) return null;
  const key = secret();
  // No key means no session is valid. Verifying against a fallback would accept
  // cookies signed with a key anyone can read — the shape kura#59 was about.
  if (key === null) return null;
  const dot = value.lastIndexOf('.');
  if (dot < 0) return null;
  const payload = value.slice(0, dot);
  const sig = value.slice(dot + 1);
  const expect = crypto.createHmac('sha256', key).update(payload).digest('base64url');
  const a = Buffer.from(sig);
  const b = Buffer.from(expect);
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return null;
  try {
    const s = JSON.parse(Buffer.from(payload, 'base64url').toString()) as Session;
    if (typeof s.u !== 'string' || !s.u || typeof s.t !== 'string' || !s.t || typeof s.e !== 'string'
        || !Number.isSafeInteger(s.exp) || s.exp! <= Date.now() / 1000) return null;
    return s;
  } catch {
    return null;
  }
}

/** szurubooru-scheme upstream auth header from a session, or null. */
export function authHeaderFromSession(s: Session | null): string | null {
  if (!s) return null;
  return 'Token ' + Buffer.from(`${s.u}:${s.t}`).toString('base64');
}
