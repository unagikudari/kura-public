// target タグの関係一覧 (tag_relation スキーマ、migration 0064) の lazy cache。
// 一括編集のサムネ内帯 (ThumbCategoryTags) が target 変更ごとに 1 回だけ引く。
// 値は 相手タグ primary name → kind の Map。失敗 entry はキャッシュから外す。
import { fetchTagRelations } from '#lib/api.ts';

const relCache = new Map<string, Promise<Map<string, 'related' | 'exclusive'>>>();

export function tagRelations(name: string): Promise<Map<string, 'related' | 'exclusive'>> {
  let p = relCache.get(name);
  if (!p) {
    p = fetchTagRelations(name).then(rs => new Map(rs.map(r => [r.name, r.kind])));
    p.catch(() => { relCache.delete(name); });
    relCache.set(name, p);
  }
  return p;
}
