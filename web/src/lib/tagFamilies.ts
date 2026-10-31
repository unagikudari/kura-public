// Tag category → family (取り違えやすい仲間のまとめ)。
//
// なぜ family が要るか: kura のタグは画像単位なので、「行為」タグと「その行為の
// 対象部位」タグが別カテゴリに散る。例えば anilingus / fellatio / cunnilingus /
// licking は全部 sex_act だが、正しさは対象部位 (anus / male_genitals /
// female_genitals) 側のタグと突き合わせないと判定できない。カテゴリを ABC 順や
// フラットに並べると突き合わせができず、錯誤が見つけられない。
// そこで **錯誤が起きる組を隣接させる** family 順に並べる:
//   部位 → 行為 を隣接させると「anilingus と付いているのに anus が無い」が目で拾える。
//
// SoT: カテゴリ自体は DB (`tag_category`, GET /api/tag-categories) が SoT。
// ここは「表示上どの family に置くか」だけを持つ**表示側の純粋な写像**で、
// カテゴリを増やしても壊れない — 未登録カテゴリは末尾の 'other' family に落ちる
// (categoryColor.ts と同じ方針: DB に追加すればコード変更なしで UI に出る)。
import type { MicroTag } from '#lib/types.ts';
import type { Messages } from '#lib/i18n/index.ts';

export type TagFamilyKey = keyof Messages['tags']['families'];
export type TagFamily = { key: TagFamilyKey; categories: string[] };

/** family の並び順がそのまま表示順。部位と行為を隣接させるのが要点。 */
export const TAG_FAMILIES: TagFamily[] = [
  { key: 'identity', categories: ['artist', 'copyright', 'character'] },
  {
    key: 'region',
    categories: [
      'breast', 'female_genitals', 'male_genitals',
      'anus', 'pubic_hair', 'genital_accessory',
    ],
  },
  { key: 'act', categories: ['sex_act', 'sex_position', 'cum', 'excretion'] },
  { key: 'posture', categories: ['pose', 'viewpoint', 'expression'] },
  { key: 'attire', categories: ['clothing'] },
  { key: 'general', categories: ['general'] },
  { key: 'meta', categories: ['meta'] },
];

const OTHER: TagFamily = { key: 'other', categories: [] };

/** category → family key。未登録は 'other'。 */
const FAMILY_OF: Record<string, string> = (() => {
  const m: Record<string, string> = {};
  for (const f of TAG_FAMILIES) for (const c of f.categories) m[c] = f.key;
  return m;
})();

export type GroupedFamily = { key: TagFamilyKey; label: string; tags: MicroTag[] };

/**
 * tags を family 単位にまとめる。family 内は **category 優先 → 名前順** で並べる
 * ので、同カテゴリ (例 male_genitals) のタグが必ず固まり、部位の抜けが見える。
 * 空 family は返さない。未登録カテゴリは末尾 'other' に集約。
 * m は表示言語の messages (family の見出しに使う)。
 */
export function groupByFamily(tags: MicroTag[], m: Messages): GroupedFamily[] {
  const buckets = new Map<string, MicroTag[]>();
  for (const t of tags) {
    const cat = t.category || 'default';
    const fam = FAMILY_OF[cat] ?? OTHER.key;
    if (!buckets.has(fam)) buckets.set(fam, []);
    buckets.get(fam)!.push(t);
  }
  const out: GroupedFamily[] = [];
  for (const f of [...TAG_FAMILIES, OTHER]) {
    const got = buckets.get(f.key);
    if (!got?.length) continue;
    got.sort((a, b) => {
      const ca = a.category || 'default', cb = b.category || 'default';
      if (ca !== cb) return ca.localeCompare(cb);
      return a.names[0].localeCompare(b.names[0]);
    });
    out.push({ key: f.key, label: m.tags.families[f.key], tags: got });
  }
  return out;
}
