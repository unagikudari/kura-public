/** One credential boundary for pages, commands and media. Never sent to JS. */
import crypto from 'node:crypto';
import * as env from '$app/env/private';
import { authHeaderFromSession, readSession, SESSION_COOKIE } from './session';

export const INTERNAL_API = (env.INTERNAL_API_URL || 'http://127.0.0.1:8001').replace(/\/$/, '');
type Credential = { authorization: string | null; machine: boolean };
const identities = new Map<string, number>();
const pending = new Map<string, Promise<boolean>>();
const MAX_IDENTITIES = 512;
const READ_TTL_MS = 5000;

function machineKey(value: string | null): boolean {
  const key = env.KURA_API_KEY;
  if (!value || !key) return false;
  const a = Buffer.from(value), b = Buffer.from(key);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

async function verify(authorization: string): Promise<boolean> {
  const response = await fetch(`${INTERNAL_API}/api/auth/whoami`, {
    headers: { authorization }, signal: AbortSignal.timeout(5000), redirect: 'error',
  });
  await response.body?.cancel();
  if (response.status === 401 || response.status === 403) return false;
  if (!response.ok) throw new Error('authentication_unavailable');
  return true;
}

export async function credential(request: Request, cookie: string | undefined): Promise<Credential | null> {
  const explicit = request.headers.get('authorization');
  const machine = machineKey(request.headers.get('x-api-key'));
  const authorization = explicit || authHeaderFromSession(readSession(cookie));
  const safe = request.method === 'GET' || request.method === 'HEAD';
  // Browser cookie authority must never be lent to a cross-site request. Machine
  // callers carry explicit credentials and do not depend on browser cookies.
  if (!safe && !explicit && !machine && request.headers.get('origin') !== new URL(request.url).origin) return null;
  if (machine) return { authorization: explicit, machine: true };
  if (!authorization || !authorization.startsWith('Token ')) return null;
  const digest = crypto.createHash('sha256').update(authorization).digest('hex');
  if (safe && (identities.get(digest) ?? 0) > Date.now()) return { authorization, machine: false };
  let check = safe ? pending.get(digest) : undefined;
  if (!check) {
    if (safe && pending.size >= 32) throw new Error('authentication_busy');
    check = verify(authorization);
    if (safe) pending.set(digest, check);
  }
  let valid;
  try { valid = await check; }
  finally { if (safe && pending.get(digest) === check) pending.delete(digest); }
  if (!valid) { identities.delete(digest); return null; }
  if (identities.size >= MAX_IDENTITIES) identities.delete(identities.keys().next().value!);
  identities.set(digest, Date.now() + READ_TTL_MS);
  return { authorization, machine: false };
}

export { SESSION_COOKIE };
