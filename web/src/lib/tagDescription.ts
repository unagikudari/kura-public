// タグ description の lazy cache (共有モジュール、2026-07-20 に VerifyTagOverlay
// から抽出 — サムネ内タグ表示 (ThumbCategoryTags) と共用するため)。
// hover 時に GET /api/tag/{name} (TagDetail 経路) を 1 回だけ叩き、in-flight の
// Promise ごとキャッシュして連続 hover の重複 fetch を防ぐ。失敗 entry は
// キャッシュから外す (次の hover で再試行できる)。
import { fetchTagDetail } from '#lib/api.ts';

const descCache = new Map<string, Promise<string | null>>();

export function tagDescription(name: string): Promise<string | null> {
  let p = descCache.get(name);
  if (!p) {
    p = fetchTagDetail(name).then(d => d.description);
    p.catch(() => { descCache.delete(name); });
    descCache.set(name, p);
  }
  return p;
}
