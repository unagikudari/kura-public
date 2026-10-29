import * as env from '$app/env/private';
import type { PageServerLoad } from './$types';

const INTERNAL_API = env.INTERNAL_API_URL ?? 'http://kura-api:8000';

// 教師選別 (teacher selection): auto-imported censor regions scored worst-first.
// The reviewer fixes them in /post/<id>/censor or drops the post from training.
// (Replaces the old perceptual-hash cluster browser on this route.)
export const load: PageServerLoad = async ({ url, fetch }) => {
  const offset = Math.max(0, Number(url.searchParams.get('off') ?? '0'));
  const only = url.searchParams.get('only') ?? 'suspect';
  const limit = 48;
  const res = await fetch(
    `${INTERNAL_API}/api/censor/teacher-candidates?offset=${offset}&limit=${limit}&only=${only}`);
  if (!res.ok) return { results: [], total: 0, offset, limit, only };
  const data = await res.json();
  return { results: data.results, total: data.total, offset, limit, only };
};
