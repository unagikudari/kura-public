// タグ一括編集の共有ロジック (view-agnostic)。
// グリッド (?bulk=1, routes/+page.svelte) とマンガビューア
// (post/[id]/+page.svelte の一括編集バー) が同じ選択モデル・適用エンジンを
// 使う — 2026-07-19 まで両ページに二重実装されていたものの一本化。
//
// 選択モデル (partition 意味論):
//   target タグ付与済み post → クリックで「解除候補」(removeIds) をトグル
//   未付与 post            → クリックで「追加候補」(addIds) をトグル
// 適用エンジンはマンガ実装の意味論 (version-aware PATCH、409 は該当 post のみ
// 最新再取得 + 1 回リトライ、per-post 進捗/成功コールバック) を採用。
import type { Post } from '#lib/types.ts';

/** per-post の枠色状態: 青=付与済み / 緑=追加選択中 / 赤=選択中 (解除または施錠の対象) */
export type BulkFrameState = 'has' | 'adding' | 'removing' | null;

export interface BulkApplyResult {
  added: number;
  removed: number;
  locked: number;
  fail: number;
  lastError?: string;
}

export interface BulkProgress {
  done: number;
  total: number;
}

/** version-aware PATCH の依存 (api.ts の patchPost / fetchPost を渡す) */
export interface PatchDeps {
  patchPost: (id: number, version: number, tags: string[]) => Promise<Post>;
  fetchPost: (id: number) => Promise<Post>;
  /** 1 タグ × N post を 1 往復で外す (api.ts)。未指定なら従来の逐次 PATCH に落ちる。 */
  removeTagBulk?: (tag: string, postIds: number[]) => Promise<{ removed: number; skipped: number }>;
}

export interface BulkApplyDeps extends PatchDeps {
  /** view 側の post ストアから id で引く (見つからなければ fail 扱い) */
  getPost: (id: number) => Post | undefined;
  /** per-post 成功: view が枠色/表示を即時更新する */
  onUpdated: (post: Post) => void;
  /** per-post 進捗 (成功・失敗どちらも 1 と数える) */
  onProgress?: (done: number, total: number) => void;
  /** 施錠 (PUT /post/{id}/tag-pin state=present)。省略時は toLock を fail 扱い。 */
  pinTag?: (id: number, tag: string) => Promise<unknown>;
}

/** クリック選択: 付与済み→選択(赤)トグル / 未付与→追加候補トグル。
 *  (3 状態サイクルは 2026-08-02 で撤回 — 施錠は右上の鍵バッジ+「選択を施錠」
 *  ボタンに分離。選択色は赤/青/緑のまま。)
 *  新しい Set を返す (Svelte の $state リアクティビティ前提の immutable 更新)。 */
export function toggleCandidate(
  hasTag: boolean,
  id: number,
  addIds: Set<number>,
  removeIds: Set<number>
): { addIds: Set<number>; removeIds: Set<number> } {
  if (hasTag) {
    const next = new Set(removeIds);
    if (next.has(id)) next.delete(id); else next.add(id);
    return { addIds, removeIds: next };
  }
  const next = new Set(addIds);
  if (next.has(id)) next.delete(id); else next.add(id);
  return { addIds: next, removeIds };
}

/** 全選択/全解除 (トグル式): ids が全て選択済みなら全部外し、そうでなければ全部足す。 */
export function toggleAll(ids: number[], cur: Set<number>): Set<number> {
  const all = ids.length > 0 && ids.every(id => cur.has(id));
  const next = new Set(cur);
  for (const id of ids) {
    if (all) next.delete(id); else next.add(id);
  }
  return next;
}

/** 枠色状態の導出。優先順位: removing > adding > has。 */
export function frameState(
  hasTag: boolean,
  id: number,
  addIds: Set<number>,
  removeIds: Set<number>
): BulkFrameState {
  if (removeIds.has(id)) return 'removing';
  if (addIds.has(id)) return 'adding';
  return hasTag ? 'has' : null;
}

/** version-aware PATCH — 409 (version conflict) は該当 post のみ最新を再取得して
 *  1 回リトライ。mutate は「現在のタグ名リスト → 新リスト」の純関数。 */
export async function patchTagVersionAware(
  p: Post,
  mutate: (tags: string[]) => string[],
  deps: PatchDeps
): Promise<Post> {
  try {
    return await deps.patchPost(p.id, p.version, mutate(p.tags.map(t => t.names[0])));
  } catch (e: unknown) {
    if ((e as { status?: number })?.status === 409) {
      const fresh = await deps.fetchPost(p.id);
      return await deps.patchPost(fresh.id, fresh.version, mutate(fresh.tags.map(t => t.names[0])));
    }
    throw e;
  }
}

/** 適用エンジン: 追加候補 → 解除候補の順に 1 post ずつ PATCH。
 *  成功ごとに onUpdated (view が枠を即時更新)、各 post 処理後に onProgress。 */
export async function applyBulkTag(
  target: string,
  toAdd: number[],
  toRemove: number[],
  deps: BulkApplyDeps,
  toLock: number[] = []
): Promise<BulkApplyResult> {
  let added = 0, removed = 0, locked = 0, fail = 0, lastError = '';
  const total = toAdd.length + toRemove.length + toLock.length;
  let done = 0;
  const step = () => { done++; deps.onProgress?.(done, total); };

  for (const id of toLock) {
    if (!deps.pinTag) { fail++; step(); continue; }
    try {
      await deps.pinTag(id, target);
      locked++;
    } catch (e) {
      lastError = String(e);
      console.error('bulk apply lock failed', id, e);
      fail++;
    }
    step();
  }

  for (const id of toAdd) {
    const p = deps.getPost(id);
    if (!p) { fail++; step(); continue; }
    try {
      deps.onUpdated(await patchTagVersionAware(p, tags => tags.includes(target) ? tags : [...tags, target], deps));
      added++;
    } catch (e) {
      lastError = String(e);
      console.error('bulk apply add failed', id, e);
      fail++;
    }
    step();
  }

  // 解除は 1 リクエストで (2026-09-06)。以前はここも post ごとに直列 PATCH で、
  // 本番の実測では適用ループ内の間隔が中央値 101 ms = PATCH 1 件の往復そのもの
  // だった (100 件で 10 秒、500 件で 50 秒)。遅いのは DB でもクエリでもなく
  // 「選択件数ぶんの往復を 1 件ずつ待つ」形だった。
  //
  // 併せて安全でもある: PATCH は post の全タグを read-modify-write で送るので
  // 並行編集を丸ごと上書きしうる (だから version 照合が要った)。bulk 版は
  // (post, tag) 1 組だけを狙って外すので、その事故が起こりえない。
  if (toRemove.length && deps.removeTagBulk) {
    for (let i = 0; i < toRemove.length; i += 1000) {   // server の上限に合わせる
      const chunk = toRemove.slice(i, i + 1000);
      try {
        const r = await deps.removeTagBulk(target, chunk);
        removed += r.removed;
        // 表示は**取り直さずに**更新する。何が変わったかは分かっている
        // (target タグが 1 つ消えた) ので、N 件の GET を撃つ理由が無い —
        // それをやると往復を 1 回にした意味が消える。version も server が
        // +1 したぶんを合わせておく (次の楽観ロックがずれないように)。
        for (const id of chunk) {
          const p = deps.getPost(id);
          if (p) deps.onUpdated({ ...p, version: p.version + 1,
                                  tags: p.tags.filter(tg => tg.names[0] !== target) });
          step();
        }
      } catch (e) {
        lastError = String(e);
        console.error('bulk apply remove failed', chunk.length, e);
        fail += chunk.length;
        chunk.forEach(step);
      }
    }
  } else {
    for (const id of toRemove) {
      const p = deps.getPost(id);
      if (!p) { fail++; step(); continue; }
      try {
        deps.onUpdated(await patchTagVersionAware(p, tags => tags.filter(n => n !== target), deps));
        removed++;
      } catch (e) {
        lastError = String(e);
        console.error('bulk apply remove failed', id, e);
        fail++;
      }
      step();
    }
  }

  return { added, removed, locked, fail, lastError: lastError || undefined };
}
