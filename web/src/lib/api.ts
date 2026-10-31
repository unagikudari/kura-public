import * as env from '$app/env/public';
import { apiErrorMessage } from './apiError';
import type { PagedResult, Post, PoolSummary, TagSuggestion, TagDetail, TagCategory, TagLink } from './types';

// 空文字 = same-origin (nginx proxy経由)。reads は kura-api を直接叩く。
// dynamic/public + '' フォールバックにしている理由: static import だと
// PUBLIC_API_URL が未設定のままビルドすると誤った値 ('/api') が焼き込まれ、
// mediaUrl / fetchTagsBrowse が `/api/api/...` を生成 → サムネ全 404 +
// /tags/manage 500 になる事故が起きた (2026-06-15)。未設定なら same-origin。
export const BASE = env.PUBLIC_API_URL ?? '';
// 破壊的 / mutating な呼び出しは same-origin の SSR proxy (/m/...) を経由する。
// SSR 側 (web/src/routes/m/[...path]) が KURA_TOKEN をサーバ側で付与して kura-api
// に転送するので、token はブラウザに出ない。reads は認証不要なので直叩きのまま。
export const MUT = '/m';


/** /data/... のような相対URLにAPIベースを付与 */
export function mediaUrl(url: string): string {
  if (!url) return '';
  if (url.startsWith('/')) return BASE + url;
  return url;
}

export async function fetchPosts(
  query: string,
  offset: number,
  limit: number,
  fetchFn: typeof fetch = fetch,
  signal?: AbortSignal,
  sort?: string | null,
  dir?: string
): Promise<PagedResult<Post>> {
  const params = new URLSearchParams({ query, offset: String(offset), limit: String(limit) });
  // 'page' is sent too — the API honours dir for it now (ページ↑↓ used to be a no-op).
  if (sort) params.set('sort', sort);
  if (dir) params.set('dir', dir);
  const res = await fetchFn(`${BASE}/api/posts?localize=true&${params}`, { signal });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function searchTags(query: string, limit = 10): Promise<TagSuggestion[]> {
  if (!query.trim()) return [];
  const params = new URLSearchParams({ query, limit: String(limit) });
  const res = await fetch(`${BASE}/api/tags?localize=true&${params}`);
  if (!res.ok) return [];
  return res.json();
}

export async function uploadPost(file: File): Promise<Post> {
  const form = new FormData();
  form.append('file', file);
  const res = await fetch(`${MUT}/posts?localize=true`, { method: 'POST', body: form });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    // `error` is the envelope's slug (api/errors.py); callers branch on it, never on the sentence.
    throw Object.assign(new Error(apiErrorMessage(err, `Upload error: ${res.status}`)),
      { status: res.status, error: (err as { detail?: { error?: string } })?.detail?.error });
  }
  return res.json();
}

export async function fetchPoolHeader(poolId: number): Promise<Omit<PoolSummary, 'posts' | 'postCount'>> {
  const res = await fetch(`${BASE}/api/pools/${poolId}?localize=true&include_posts=false`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function createVerifyPoolFromPosts(
  name: string,
  postIds: number[],
): Promise<PoolSummary> {
  const created = await fetch(`${MUT}/pools`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ names: [name], verify_status: 'pending' }),
  });
  if (!created.ok) {
    const err = await created.json().catch(() => ({}));
    throw new Error(apiErrorMessage(err, `API error: ${created.status}`));
  }
  const pool = await created.json();
  const attached = await fetch(`${MUT}/pool/${pool.id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ posts: postIds, version: pool.version }),
  });
  if (!attached.ok) {
    const err = await attached.json().catch(() => ({}));
    throw new Error(apiErrorMessage(err, `API error: ${attached.status}`));
  }
  return attached.json();
}

export async function fetchPools(
  query: string,
  offset: number,
  limit: number,
  sort: string = 'id',
  dir: string = 'desc',
  signal?: AbortSignal,
  includeEmpty: boolean = false,
  hideVerify: boolean = false,
): Promise<PagedResult<PoolSummary>> {
  const params = new URLSearchParams({ query, offset: String(offset), limit: String(limit), sort, dir });
  if (includeEmpty) params.set('include_empty', 'true');
  if (hideVerify) params.set('hide_verify', 'true');
  const res = await fetch(`${BASE}/api/pools?${params}`, { signal });
  if (!res.ok) throw Object.assign(new Error(`API error: ${res.status}`), { status: res.status });
  return res.json();
}

export async function setPoolVerifyStatus(
  poolId: number,
  status: 'pending' | 'verified' | null,
  headCandidate?: { tag: string; reason: string },
): Promise<void> {
  const body: Record<string, unknown> = { status };
  // head_candidate (0075): tagger_head_candidate への自動登録フック。人間が
  // 検証プールを見た「今」しか判断できない情報(head化する価値があるか)なので、
  // verify 切替の同一アクションに乗せる — 別途「後で登録する」手順を作らない
  // (user指示「この機構は自動化すること」)。
  if (headCandidate) body.head_candidate = headCandidate;
  const res = await fetch(`${MUT}/pools/${poolId}/verify-status`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
}

export async function reorderPoolPosts(poolId: number, postIds: number[]): Promise<void> {
  const res = await fetch(`${MUT}/pools/${poolId}/order`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ postIds }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(apiErrorMessage(err, `API error: ${res.status}`));
  }
}

// Promote one of a pool's existing sources (aliases) to primary.
export async function setPrimarySource(poolId: number, url: string): Promise<PoolSummary> {
  const res = await fetch(`${MUT}/pools/${poolId}/primary-source`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(apiErrorMessage(err, `API error: ${res.status}`));
  }
  return res.json();
}

export async function setPoolCover(poolId: number, coverPostId: number): Promise<void> {
  const res = await fetch(`${MUT}/pools/${poolId}/cover`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ coverPostId }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(apiErrorMessage(err, `API error: ${res.status}`));
  }
}

export async function deletePost(id: number): Promise<void> {
  const res = await fetch(`${MUT}/post/${id}`, { method: 'DELETE' });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(apiErrorMessage(err, `Delete error: ${res.status}`));
  }
}

// ── Censor annotation (issue #2) — reads direct, writes via /m (token added SSR-side) ──
async function _mut(path: string, method: string, body?: unknown): Promise<any> {
  const res = await fetch(`${MUT}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(apiErrorMessage(err, `API error: ${res.status}`));
  }
  return res.status === 204 ? null : res.json();
}

export async function listCensorAnnotations(postId: number, fetchFn: typeof fetch = fetch): Promise<any[]> {
  const res = await fetchFn(`${BASE}/api/post/${postId}/censor-annotations`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return (await res.json()).results;
}

export function createCensorAnnotation(postId: number, body: unknown): Promise<any> {
  return _mut(`/post/${postId}/censor-annotation`, 'POST', body);
}

/** censored↔uncensored ペア (censor_pair)。ペアが無ければ null。 */
export async function getCensorPair(postId: number, fetchFn: typeof fetch = fetch): Promise<any | null> {
  const res = await fetchFn(`${BASE}/api/post/${postId}/censor-pair`);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

/** ペア探索状態 (censor_pair_search)。行が無ければ status:null。 */
export async function getPairSearch(postId: number, fetchFn: typeof fetch = fetch): Promise<any> {
  const res = await fetchFn(`${BASE}/api/post/${postId}/pair-search`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

/** 人間の探索フラグ (pending=探索対象 / excluded=対象外)。 */
export function putPairSearch(postId: number, status: 'pending' | 'excluded'): Promise<any> {
  return _mut(`/post/${postId}/pair-search`, 'PUT', { status });
}

export async function getPairSearchScope(poolId: number, fetchFn: typeof fetch = fetch): Promise<any> {
  const res = await fetchFn(`${BASE}/api/pool/${poolId}/pair-search-scope`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export function putPairSearchScope(poolId: number, enabled: boolean): Promise<any> {
  return _mut(`/pool/${poolId}/pair-search-scope`, 'PUT', { enabled });
}

/** 学習除外pool (tagger_auto_exclude_pool, migration 0099)。自分の生成物/写真
 * 管理用など、tagger自動タグ付けパイプラインに永続的に触らせたくないpool。 */
export async function getTaggerExclude(poolId: number, fetchFn: typeof fetch = fetch): Promise<any> {
  const res = await fetchFn(`${BASE}/api/pool/${poolId}/tagger-exclude`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export function putTaggerExclude(poolId: number, excluded: boolean): Promise<any> {
  return _mut(`/pool/${poolId}/tagger-exclude`, 'PUT', { excluded });
}

export async function fetchTeacherCandidates(
  offset = 0, limit = 48, only = 'suspect', fetchFn: typeof fetch = fetch,
): Promise<any> {
  const res = await fetchFn(
    `${BASE}/api/censor/teacher-candidates?offset=${offset}&limit=${limit}&only=${only}`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export function setTeacherExclusion(postId: number, excluded: boolean): Promise<any> {
  return _mut(`/post/${postId}/teacher-exclusion`, 'PUT', { excluded });
}

export function deleteCensorAnnotation(postId: number, aid: number, regionVersion: number): Promise<any> {
  return _mut(`/post/${postId}/censor-annotation/${aid}`, 'DELETE', { regionVersion });
}

export async function getCensorReview(postId: number, fetchFn: typeof fetch = fetch): Promise<any> {
  const res = await fetchFn(`${BASE}/api/post/${postId}/censor-review`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export function putCensorReview(postId: number, body: unknown): Promise<any> {
  return _mut(`/post/${postId}/censor-review`, 'PUT', body);
}

export async function fetchCensorStatus(
  postIds: number[],
  fetchFn: typeof fetch = fetch,
): Promise<Record<string, { annotationCount: number; reviewState: string; hasNoCensor: boolean }>> {
  if (!postIds.length) return {};
  const res = await fetchFn(`${BASE}/api/censor/status?post_ids=${postIds.join(',')}`);
  if (!res.ok) return {};
  return (await res.json()).results;
}

export type VariantStatus = {
  kind: string;
  reviewState: string;
  stale: boolean;
  contentUrl: string;
  thumbnailUrl: string;
  regionVersion: number;
};

export async function fetchVariantStatus(
  postIds: number[],
  fetchFn: typeof fetch = fetch,
): Promise<Record<string, VariantStatus>> {
  if (!postIds.length) return {};
  const res = await fetchFn(`${BASE}/api/variant/status?post_ids=${postIds.join(',')}`);
  if (!res.ok) return {};
  return (await res.json()).results;
}

/** Approve / reject a post's AI-reconstruction variant (auth via /m proxy). */
export async function setVariantReview(
  postId: number, kind: string, reviewState: string, regionVersion: number,
): Promise<VariantStatus> {
  const form = new FormData();
  form.set('review_state', reviewState);
  form.set('region_version', String(regionVersion));
  const res = await fetch(`${MUT}/post/${postId}/variant/${kind}`, { method: 'PATCH', body: form });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(apiErrorMessage(err, `API error: ${res.status}`));
  }
  return res.json();
}

export async function toggleFavorite(id: number): Promise<Post> {
  const res = await fetch(`${MUT}/post/${id}/favorite?localize=true`, { method: 'POST' });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(apiErrorMessage(err, `API error: ${res.status}`));
  }
  return res.json();
}

export async function patchPost(
  id: number,
  version: number,
  tags: string[],
  extra?: { safety?: string; source?: string }
): Promise<Post> {
  const res = await fetch(`${MUT}/post/${id}?localize=true`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ version, tags, ...extra }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const e = new Error(apiErrorMessage(err, `API error: ${res.status}`));
    // 呼び出し側が 409 (version conflict) を区別して再取得できるように
    (e as any).status = res.status;
    throw e;
  }
  return res.json();
}

/** 施錠/解錠 (PUT /post/{id}/tag-pin): state='present'=施錠 (positive GT 化+
 *  タガー上書き不可)、state=null=解錠 (pin 削除)。version 不要 (タグ本体は不変)。 */
export async function removeTagBulk(
  tag: string,
  postIds: number[],
): Promise<{ tag: string; removed: number; skipped: number }> {
  const res = await fetch(`${MUT}/posts/tag-remove-bulk`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tag, post_ids: postIds }),
  });
  if (!res.ok) throw new Error(`remove-bulk failed: ${res.status}`);
  return res.json();
}

export async function putTagPinBulk(
  tag: string,
  state: 'present' | null,
  postIds: number[]
): Promise<{ tag: string; locked?: number; unlocked?: number; skipped: number }> {
  const res = await fetch(`${MUT}/posts/tag-pin-bulk`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tag, state, post_ids: postIds }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(apiErrorMessage(err, `API error: ${res.status}`));
  }
  return res.json();
}

export async function putTagPin(
  id: number,
  tag: string,
  state: 'present' | null
): Promise<{ post_id: number; tag: string; pinned: string | null }> {
  const res = await fetch(`${MUT}/post/${id}/tag-pin`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tag, state }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const e = new Error(apiErrorMessage(err, `API error: ${res.status}`));
    (e as any).status = res.status;
    throw e;
  }
  return res.json();
}

/** 一括編集ビュー用: post 群の全 pin をまとめて取得 ({post_id: {tag: 'present'|'absent'}}) */
export async function fetchPostPinStates(
  ids: number[]
): Promise<Record<number, Record<string, 'present' | 'absent'>>> {
  if (!ids.length) return {};
  const res = await fetch(`${BASE}/api/post-pin-states?ids=${ids.join(',')}`);
  if (!res.ok) return {};
  const d = await res.json();
  return d.states ?? {};
}

/** 単一 post の再取得 (version 競合後の refetch 用) */
export async function fetchPost(id: number, fetchFn: typeof fetch = fetch): Promise<Post> {
  const res = await fetchFn(`${BASE}/api/post/${id}?localize=true`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function fetchTagCategories(fetchFn: typeof fetch = fetch): Promise<TagCategory[]> {
  const res = await fetchFn(`${BASE}/api/tag-categories`);
  if (!res.ok) return [];
  return res.json();
}

export async function fetchTagsBrowse(
  query: string,
  category: string,
  offset: number,
  limit: number,
  fetchFn: typeof fetch = fetch,
  options: { thumbnails?: boolean; signal?: AbortSignal; cleanup?: import('./tagCatalog').TagCleanup; tagId?: number } = {},
): Promise<PagedResult<TagDetail>> {
  const params = new URLSearchParams({ query, category, offset: String(offset), limit: String(limit) });
  if (options.thumbnails === false) params.set('thumbnails', 'false');
  if (options.tagId !== undefined) params.set('tag_id', String(options.tagId));
  if (options.cleanup && options.cleanup !== 'all') params.set('cleanup', options.cleanup);
  const res = await fetchFn(`${BASE}/api/tags?localize=true&${params}`, { signal: options.signal });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function patchTag(
  tagId: number,
  version: number,
  updates: { names?: string[]; category?: string; description?: string; localizedNames?: Record<string, string> },
): Promise<TagDetail> {
  const res = await fetch(`${MUT}/tag-by-id/${tagId}?localize=true`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ version, ...updates }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(apiErrorMessage(err, `API error: ${res.status}`));
  }
  return res.json();
}

export async function deleteTag(name: string, force = false): Promise<void> {
  const url = `${MUT}/tag/${encodeURIComponent(name)}${force ? '?force=true' : ''}`;
  const res = await fetch(url, { method: 'DELETE' });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(apiErrorMessage(err, `API error: ${res.status}`));
  }
}

export async function mergeTag(sourceName: string, targetName: string): Promise<TagDetail> {
  const res = await fetch(
    `${MUT}/tag/${encodeURIComponent(sourceName)}/merge-into/${encodeURIComponent(targetName)}?localize=true`,
    { method: 'POST' },
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(apiErrorMessage(err, `API error: ${res.status}`));
  }
  return res.json();
}

// 単一タグの詳細 (description 含む) — /tags ブラウザと同じ serializer の
// GET /api/tag/{name}。検証オーバーレイの hover ツールチップ等が lazy に使う。
export type TagRelationEdge = {
  a: string; aColor: string; b: string; bColor: string;
  kind: 'related' | 'exclusive'; note: string | null;
};

export async function fetchAllTagRelations(
  fetchFn: typeof fetch = fetch,
): Promise<TagRelationEdge[]> {
  const res = await fetchFn(`${BASE}/api/tag-relations`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export type TagRelation = { name: string; kind: 'related' | 'exclusive'; note: string | null };

export async function fetchTagRelations(
  name: string,
  fetchFn: typeof fetch = fetch,
): Promise<TagRelation[]> {
  const res = await fetchFn(`${BASE}/api/tag/${encodeURIComponent(name)}/relations`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function fetchTagDetail(
  name: string,
  fetchFn: typeof fetch = fetch,
): Promise<TagDetail> {
  const res = await fetchFn(`${BASE}/api/tag/${encodeURIComponent(name)}?localize=true`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function fetchTagLinks(
  name: string,
  fetchFn: typeof fetch = fetch,
): Promise<TagLink[]> {
  const res = await fetchFn(`${BASE}/api/tag/${encodeURIComponent(name)}/links`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export interface TagLinkRow extends TagLink {
  tagCategory?: string;
  tagName: string;
  poolId: number | null;
  poolName: string | null;
  poolCover: string | null; // /api/post/{id}/thumbnail
  /** What installed extensions attached to this link, keyed by extension id. */
  extensions?: Record<string, Record<string, unknown>>;
}

export async function fetchAllTagLinks(
  opts: { platform?: string; uncensored?: 0 | 1; verified?: boolean; limit?: number; tagIds?: number[]; excludeCategory?: string } = {},
  fetchFn: typeof fetch = fetch,
): Promise<TagLinkRow[]> {
  const p = new URLSearchParams();
  if (opts.excludeCategory) p.set('exclude_category', opts.excludeCategory);
  if (opts.tagIds) { if (!opts.tagIds.length) return []; p.set('tag_ids', opts.tagIds.join(',')); }
  if (opts.platform) p.set('platform', opts.platform);
  if (opts.uncensored !== undefined) p.set('uncensored', String(opts.uncensored));
  if (opts.verified !== undefined) p.set('verified', String(opts.verified));
  p.set('limit', String(opts.limit ?? 1000));
  const res = await fetchFn(`${BASE}/api/tag-links?${p}`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function addTagLink(
  name: string,
  link: {
    platform: string;
    url: string;
    handle?: string | null;
    uncensored?: 0 | 1 | null;
    verified?: boolean;
    note?: string | null;
  },
): Promise<TagLink> {
  const res = await fetch(`${MUT}/tag/${encodeURIComponent(name)}/links`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(link),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(apiErrorMessage(err, `API error: ${res.status}`));
  }
  return res.json();
}

export async function updateTagLink(
  id: number,
  updates: {
    platform?: string;
    url?: string;
    handle?: string | null;
    uncensored?: 0 | 1 | null;
    verified?: boolean;
    note?: string | null;
    clear_uncensored?: boolean;
  },
): Promise<TagLink> {
  const res = await fetch(`${MUT}/tag-link/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(apiErrorMessage(err, `API error: ${res.status}`));
  }
  return res.json();
}

export async function deleteTagLink(id: number): Promise<void> {
  const res = await fetch(`${MUT}/tag-link/${id}`, { method: 'DELETE' });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(apiErrorMessage(err, `API error: ${res.status}`));
  }
}

export async function regenerateThumbnail(id: number, seekSeconds: number): Promise<Post> {
  const res = await fetch(`${MUT}/post/${id}/regenerate-thumbnail?localize=true`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ seek_seconds: seekSeconds }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(apiErrorMessage(err, `API error: ${res.status}`));
  }
  return res.json();
}
