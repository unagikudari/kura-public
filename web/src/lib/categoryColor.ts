// Tag category → swatch color。P4 (plan kura-web-componentization) で DB 駆動化。
//
// SoT は DB `tag_category.color` (GET /api/tag-categories)。+layout.server.ts が
// 全 route の SSR で category→color map を取得し、+layout.svelte が
// setCategoryColors() でここに注入する。新カテゴリを DB に追加すれば
// **コード変更なしで** 全 UI (チップ / 検証オーバーレイ / サジェスト) に色が出る。
// (/tags 系は従来どおり TagDetail.categoryColor を直接使う — あちらも DB 直。)
//
// 明度リフト: DB の色は /tags ブラウザ (明背景) 基準で暗く、ダーク背景の
// チップでは読めない。リフトは**表示側の純粋変換**であり DB 値は変異しない
// (DB を書き換えると /tags ブラウザの色が変わってしまうため却下)。
// 既存カテゴリの表示値はカテゴリごとに手調整してあり、一様変換では
// ピクセル一致で再現できない — なので honesty 優先で CAT_LIFT は
// 「DB base hue → 手調整表示値」のリフト表として残す。各 entry は調整時点の
// DB 値 (base) とペアで凍結してあるので、DB 側で色を変えると base 不一致に
// なり自動的に一様変換 liftForDark(dbColor) へフォールスルーする。つまり
// この表は「特定の DB 値に対する表示オーバーライド」であって、DB と乖離した
// まま居座れる第二の真実にはなれない。
//
// 解決チェーン (catColor):
//   1. DB 色あり & CAT_LIFT.base 一致 → CAT_LIFT.display (手調整値、従来画素と同一)
//   2. DB 色あり (新カテゴリ / DB 側で hue 変更) → liftForDark(dbColor) (決定的一様変換)
//   3. DB 供給なし (offline / SSR 失敗) or DB に無い名前 (legacy 'series' 等) →
//      CAT_LIFT.display → LEGACY → '#ccc' (明示 fallback — 二重真実ではない)

/** DB base hue (調整時点の tag_category.color) → dark-bg 手調整表示値。 */
const CAT_LIFT: Record<string, { base: string; display: string }> = {
  general: { base: '#0288d1', display: '#58b5f0' },
  artist: { base: '#c00004', display: '#ff6b6b' },
  copyright: { base: '#a800aa', display: '#d97ce0' },
  character: { base: '#00ab2c', display: '#5fd68a' },
  meta: { base: '#ee8800', display: '#f0a050' },
  breast: { base: '#c0894a', display: '#e8b070' },
  female_genitals: { base: '#cf5c8a', display: '#f08cb0' },
  male_genitals: { base: '#4a7fc0', display: '#7aa8f0' },
  anus: { base: '#a0596a', display: '#c88090' },
  pubic_hair: { base: '#6c8f3a', display: '#a0c060' },
  genital_accessory: { base: '#4a9f9f', display: '#60c8c8' },
  pose: { base: '#8f6cc0', display: '#b090e0' },
  sex_position: { base: '#c05a5a', display: '#e87878' },
  cum: { base: '#e0e0ff', display: '#e8e8ff' },
  sex_act: { base: '#c0a830', display: '#f0e070' },
  excretion: { base: '#8a6a4a', display: '#b09060' }, // 排泄: scat+pee
};

// DB に存在しない互換名 (旧データ / カテゴリ無しタグの 'default')。
const LEGACY: Record<string, string> = { default: '#ccc', series: '#fca', genre: '#9f9' };

// +layout.svelte が SSR/hydration 時に注入する DB map。null = 未供給 (fallback)。
// module state はサーバ側で request を跨いで共有されるが、category→color は
// グローバル真実 (per-user でない) なので問題ない。
let dbColors: Record<string, string> | null = null;

export function setCategoryColors(map: Record<string, string> | null): void {
  dbColors = map;
}

/**
 * DB hue の決定的な dark-bg リフト (純粋変換)。CAT_LIFT に無い新カテゴリ用。
 * hue 維持 / 彩度 0.80 cap / 明度 +0.25 を [0.62, 0.95] に clamp。
 */
export function liftForDark(hex: string): string {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return hex; // 不正値はそのまま返す (throw で描画を殺さない)
  const n = parseInt(m[1], 16);
  const r = ((n >> 16) & 0xff) / 255, g = ((n >> 8) & 0xff) / 255, b = (n & 0xff) / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  const l = (max + min) / 2;
  let h = 0, s = 0;
  if (d > 0) {
    s = d / (1 - Math.abs(2 * l - 1));
    if (max === r) h = 60 * (((g - b) / d) % 6);
    else if (max === g) h = 60 * ((b - r) / d + 2);
    else h = 60 * ((r - g) / d + 4);
    if (h < 0) h += 360;
  }
  const s2 = Math.min(s, 0.8);
  const l2 = Math.min(0.95, Math.max(0.62, l + 0.25));
  const c = (1 - Math.abs(2 * l2 - 1)) * s2;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const mm = l2 - c / 2;
  let rr = 0, gg = 0, bb = 0;
  if (h < 60) [rr, gg, bb] = [c, x, 0];
  else if (h < 120) [rr, gg, bb] = [x, c, 0];
  else if (h < 180) [rr, gg, bb] = [0, c, x];
  else if (h < 240) [rr, gg, bb] = [0, x, c];
  else if (h < 300) [rr, gg, bb] = [x, 0, c];
  else [rr, gg, bb] = [c, 0, x];
  const to = (v: number) => Math.round((v + mm) * 255).toString(16).padStart(2, '0');
  return `#${to(rr)}${to(gg)}${to(bb)}`;
}

export function catColor(cat: string): string {
  const db = dbColors?.[cat];
  if (db) {
    const lift = CAT_LIFT[cat];
    if (lift && lift.base === db.toLowerCase()) return lift.display;
    return liftForDark(db);
  }
  return CAT_LIFT[cat]?.display ?? LEGACY[cat] ?? '#ccc';
}
