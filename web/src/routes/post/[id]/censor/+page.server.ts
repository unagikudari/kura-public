import * as env from '$app/env/private';
import { error } from '@sveltejs/kit';
import type { PageServerLoad } from './$types';

const INTERNAL_API = env.INTERNAL_API_URL ?? 'http://kura-api:8000';

export const load: PageServerLoad = async ({ params }) => {
  const id = Number(params.id);
  if (!Number.isFinite(id)) throw error(400, 'bad post id');
  const res = await fetch(`${INTERNAL_API}/api/post/${id}`);
  if (!res.ok) throw error(res.status, 'post not found');
  return { post: await res.json() };
};
