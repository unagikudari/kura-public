// 取得元固有の表示知識を**任意で**受け取る口 (kura#66 G6)。
//
// `src/lib/site/index.ts` が在ればその表を使い、無ければ既定に落ちる。
// import.meta.glob は file が無くても build が通る (静的 import は通らない)。
// UI が site の一覧を持たない、というのがここの主張。

const mods = import.meta.glob('/src/lib/site/index.ts', { eager: true }) as Record<
  string,
  {
    platforms?: string[];
    uncensoredCapable?: Set<string>;
    badges?: Record<string, { label: string; color: string }>;
  }
>;

const site = Object.values(mods)[0] ?? {};

export const platforms: string[] = site.platforms ?? [];
export const uncensoredCapable: Set<string> = site.uncensoredCapable ?? new Set();
export const siteBadges: Record<string, { label: string; color: string }> =
  site.badges ?? {};

/** 出自 badge。表に無い domain は **badge を出さない**。
 *
 * 頭文字 + 既定色を返す実装を一度 deploy したが、`domain='default'` の pool
 * (取得元が特定できない = 大半) 全部に灰色の "D" が付いた。移設前は
 * `DOMAIN_BADGE[...] ?? null` で **無印**だったので、これは表示の変更であって
 * 移設ではない (2026-09-05、deploy 後の自分のスクリーンショットで発覚)。
 * 「知らない取得元でも壊れない」は null でも満たされる — 印が増えないだけ。 */
export function badgeFor(domain: string | null | undefined):
    { label: string; color: string } | null {
  if (!domain) return null;
  return siteBadges[domain] ?? null;
}
