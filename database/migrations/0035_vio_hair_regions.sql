-- Migration 0035 — 陰毛を生える場所ごとに書けるようにする (VIO)
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-29:
-- 「post 485820 VIO それぞれの陰毛を分けて表現したい」。
--
-- ── 0008 の型をそのまま当てる ──
-- 0008 で「毛の量」を `pubic_hair_amount` / `armpit_hair_amount` という属性にせず、
-- **毛の生える場所を region にして量は class='hair' の `hair_density`** にした。
-- VIO も同じで、**足すのは region 2 つだけ**。属性は増えない。
--   * V (前面)   = 既存の `pubic_hair` (parent=pubic_region)
--   * I (会陰)   = **新規** `perineal_hair` (parent=perineum)
--   * O (肛門周) = **新規** `perianal_hair` (parent=perianal_region)
-- これで「V は濃いが I/O は無い」が hair_density 3 つで書ける。
-- 裏付け: anal_hair 24,451 (O だけで 2.4 万件ある)。
--
-- ── VIO は語彙にしない ──
-- V/I/O は脱毛業界の呼び名で解剖学の名前ではない。slug に使うと、その業界の分類を
-- 知らないと引けなくなる。**解剖学名を slug にし、VIO は description に書く** —
-- 注釈 UI の絞り込みは description も見るので「Iライン」と打てば引ける。
--
-- ── 運用手順 ──
--   * 単一 transaction。region → class_map → 説明。冪等。
--   * ロールバック: 追加した region を DELETE (観測が参照していれば FK が拒否)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO body_region(slug, display_name, parent_slug, laterality, primary_class) VALUES
  ('perineal_hair', 'perineal hair', 'perineum',        'midline', 'hair'),
  ('perianal_hair', 'perianal hair', 'perianal_region', 'midline', 'hair')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO region_class_map(region_slug, class_slug) VALUES
  ('perineal_hair','hair'), ('perianal_hair','hair')
ON CONFLICT DO NOTHING;

UPDATE body_region SET description = v.d FROM (VALUES
  ('pubic_hair',
   '陰毛 (前面。脱毛でいう **V ライン**)。量は属性 hair_density で書く。'
   '会陰は perineal_hair、肛門周囲は perianal_hair と分けて書ける'),
  ('perineal_hair',
   '会陰の毛 (性器と肛門の間。脱毛でいう **I ライン**)。量は属性 hair_density で書く'),
  ('perianal_hair',
   '肛門周囲の毛 (脱毛でいう **O ライン**)。量は属性 hair_density で書く')
) AS v(s,d) WHERE body_region.slug = v.s;

COMMIT;
