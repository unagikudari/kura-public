-- Migration 0008 — half_erect / 体毛の量 / 属性値の順序
--
-- plan kura-1.0-implementation の S-8 (人手注釈) 中の指摘、2026-07-28:
-- 「erect の状態には half erect がある。また属性には陰毛の量と腋毛の量が欲しい」。
-- あわせて、注釈中に見つけた**値の並び順の不具合**も直す。
--
-- ── 1. erection_state に half_erect ──
-- flaccid / erect の 2 値だと中間が書けず、注釈者は「どちらかに寄せる」= 捏造か、
-- 書かないか (情報損失) を選ばされる。flaccid < half_erect < erect の 3 段にする。
-- flat 側の裏付け: `half-erect` 57 件 (少ないが、タグが無いから書かれないだけで
-- 状態自体は頻出する — 語彙が観測を規定してしまう典型)。
--
-- ── 2. 体毛の量 — **属性を 2 つ足すのではなく region + 汎用属性にする** ──
-- 既存 ontology は体毛を **region (primary_class='hair')** としてモデル化している:
-- beard / eyebrow / eyelash / mustache / sideburns。そして hair_color / hair_length /
-- hair_style が **class='hair' に適用**される。
-- よって `pubic_hair_amount` / `armpit_hair_amount` という属性を 2 つ足すのは
-- **この設計と不整合**で、しかも体毛の部位が増えるたびに属性が増える。
-- 正しい形は「毛の生える場所を region に足し、量は class='hair' の汎用属性にする」:
--   * region `pubic_hair`  (parent=pubic_region, class=hair)
--   * region `armpit_hair` (parent=axilla,       class=hair)
--   * attribute `hair_density` (applies_kind=class, applies_key=hair)
-- これで眉やあごひげの濃さも同じ属性で書けるようになる (one obvious way)。
-- 注釈は 2 手 (region を挙げる → density を付ける) になるが、語彙は増えない。
--
-- 値は kura の実タグの粒度に合わせる:
--   sparse_pubic_hair 7,723 / (plain) pubic_hair 343,905 / excessive_pubic_hair 26,538
--   → none / sparse / moderate / dense の 4 段。armpit_hair 30,814 も同じ軸で書ける。
--
-- ── 3. attribute_value に display_order ──
-- UI は値を **アルファベット順**で出していた。順序尺度の属性ではこれが実害になる:
--   breast_size → flat, gigantic, huge, large, medium, small (無意味)
--   skin_tone   → colored, dark, deep, light, medium, pale, tan
-- 順序尺度なのに順序が崩れていると、注釈者は毎回読み直して選ぶことになる。
-- `display_order` を足し、順序尺度の属性に昇順を与える (名義尺度は 0 のまま = 従来順)。
--
-- ── 運用手順 ──
--   * 単一 transaction。列追加 (nullable 既定 0) → INSERT → UPDATE の順。
--   * 冪等: ON CONFLICT DO NOTHING と同値 UPDATE なので再実行安全。
--   * ロールバック: 追加した region/attribute/value を DELETE (観測が参照していれば
--     FK が拒否する — それが正しい)、display_order は DROP COLUMN。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- =====================================================================
-- 1. erection_state に中間段階
-- =====================================================================
INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('erection_state', 'half_erect')
ON CONFLICT (attribute_slug, value_slug) DO NOTHING;

UPDATE attribute SET description =
  '勃起の状態。flaccid / half_erect / erect の単一値。中間段階が無いと「どちらかに寄せる」'
  '(捏造) か「書かない」(情報損失) しか選べなくなるため 3 段にしてある'
WHERE slug = 'erection_state';

-- =====================================================================
-- 2. 体毛: 生える場所は region、量は class='hair' の汎用属性
-- =====================================================================
INSERT INTO body_region(slug, display_name, parent_slug, laterality, primary_class) VALUES
  ('pubic_hair',  'pubic hair',  'pubic_region', 'midline',   'hair'),
  ('armpit_hair', 'armpit hair', 'axilla',       'bilateral', 'hair')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO region_class_map(region_slug, class_slug) VALUES
  ('pubic_hair', 'hair'), ('armpit_hair', 'hair')
ON CONFLICT DO NOTHING;

UPDATE body_region SET description = v.d FROM (VALUES
  ('pubic_hair',  '陰毛。量は属性 hair_density で書く'),
  ('armpit_hair', '腋毛。量は属性 hair_density で書く')
) AS v(s,d) WHERE body_region.slug = v.s;

INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('hair_density', 'hair density', 'appearance', 'categorical', 'class', 'hair')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('hair_density', 'none'), ('hair_density', 'sparse'),
  ('hair_density', 'moderate'), ('hair_density', 'dense')
ON CONFLICT (attribute_slug, value_slug) DO NOTHING;

UPDATE attribute SET description =
  '毛の量。none / sparse / moderate / dense。**class=hair の region すべて**に付く — '
  '陰毛 (pubic_hair) 腋毛 (armpit_hair) だけでなく眉 (eyebrow) あごひげ (beard) 等にも '
  '同じ属性で書ける。部位ごとに専用属性を作らないための汎用軸'
WHERE slug = 'hair_density';

-- =====================================================================
-- 3. 属性値の並び順 — 順序尺度の属性に昇順を与える
-- =====================================================================
ALTER TABLE attribute_value ADD COLUMN IF NOT EXISTS display_order integer NOT NULL DEFAULT 0;

COMMENT ON COLUMN attribute_value.display_order IS
  '選択 UI での並び順。0 = 未指定 (名義尺度は 0 のままでよい)。順序尺度の属性 '
  '(breast_size / penis_size / skin_tone / hair_density 等) はアルファベット順だと '
  '意味が壊れるので昇順を明示する。';

UPDATE attribute_value SET display_order = v.o FROM (VALUES
  -- 小さい → 大きい
  ('breast_size','flat',1),('breast_size','small',2),('breast_size','medium',3),
  ('breast_size','large',4),('breast_size','huge',5),('breast_size','gigantic',6),
  ('penis_size','small',1),('penis_size','average',2),('penis_size','large',3),('penis_size','huge',4),
  -- 弛緩 → 勃起
  ('erection_state','flaccid',1),('erection_state','half_erect',2),('erection_state','erect',3),
  -- 淡い → 濃い
  ('skin_tone','pale',1),('skin_tone','light',2),('skin_tone','medium',3),('skin_tone','tan',4),
  ('skin_tone','dark',5),('skin_tone','deep',6),('skin_tone','colored',7),
  -- 無 → 多い
  ('hair_density','none',1),('hair_density','sparse',2),
  ('hair_density','moderate',3),('hair_density','dense',4),
  -- 平坦 → 張り出し
  ('belly_shape','flat',1),('belly_shape','soft',2),('belly_shape','rounded',3),('belly_shape','distended',4),
  -- 閉 → 開
  ('mouth_state','closed',1),('mouth_state','slightly_open',2),('mouth_state','open',3),
  ('mouth_state','smiling',4),
  -- 否定 → 肯定 → 不明
  ('pregnancy_state','not_pregnant',1),('pregnancy_state','pregnant',2),('pregnancy_state','unknown',3)
) AS v(a,s,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

COMMIT;
