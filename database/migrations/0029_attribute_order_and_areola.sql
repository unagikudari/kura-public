-- Migration 0029 — 属性に表示順を持たせる / 乳輪と乳首の形を表せるようにする
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-29:
--   (1)「pubic hair color はあまり注目する要素ではないので表示順を下げてほしい」
--   (2)「areola のサイズがない。サイズというか領域の広さというか」
--
-- ── (1) 属性に表示順が無かった ──
-- `attribute_value` には 0008 で `display_order` を入れたのに、**`attribute` には無い**。
-- UI は slug のアルファベット順で出すので、`pubic_hair` の属性は
--   hair_color / hair_density / hair_length / hair_style / …
-- となり、**0008 でわざわざ足した hair_density が hair_color の後ろに来る**。
-- 順序尺度の値をアルファベット順で出すのが誤りだったのと同じ誤りが、属性の側に残っていた。
--
-- 方針: **その部位を見たときに最初に問うことを先に出す**。
--   1..9   その部位固有の主要な事実 (breast_size / erection_state / facial_expression 等)
--   10..19 副次的だが部位固有 (hair_length / hair_style / eye_state 等)
--   20..29 どの部位にも付く汎用 (pigmentation / skin_tone / surface_substance)
--   30..   注目度が低いもの (hair_color)
-- 0 は未指定 = 従来どおり slug 順で末尾に寄る。
--
-- ── (2) 乳輪・乳首の大きさが書けなかった ──
-- `breast_size` はあるが、**乳輪の広さ**は別の事実。実測でも別タグとして定着している:
-- puffy_nipples 68,340 / large_areolae 67,059 / dark_areolae 14,718 /
-- inverted_nipples 14,563。乳房が小さくて乳輪が広い、逆もある — 独立に観測できる。
--
-- `areola_size` (region=areola) を足す。**`breast_size` と同じ「頭基準」にはしない** —
-- 乳輪は乳房に対する比で見るのが自然で、頭と比べる意味が無い (0012 が頭基準にしたのは
-- 乳房が体格に依らない基準を要したため)。比で言えるよう乳房幅に対する割合で刻む。
--
-- 併せて `nipple_state` (region=nipple) を足す。**大きさではなく形状**の軸:
-- 突出しているか / 埋没しているか / 腫れているか。`puffy_nipples` は乳輪ごと膨らんだ
-- 状態で、`large_areolae` (広いだけ) とは別。inverted は真逆なので同じ軸に載る。
--
-- ── 運用手順 ──
--   * 単一 transaction。列追加 → 表示順 → 属性/値の追加。冪等。
--   * ロールバック: display_order を DROP、追加した attribute を DELETE。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- =====================================================================
-- 1. 属性の表示順
-- =====================================================================
ALTER TABLE attribute ADD COLUMN IF NOT EXISTS display_order integer NOT NULL DEFAULT 0;

COMMENT ON COLUMN attribute.display_order IS
  '選択 UI での並び順。0 = 未指定 (slug 順で末尾に寄る)。'
  '**その部位を見たときに最初に問うことを先に出す** — 1..9 部位固有の主要な事実 / '
  '10..19 副次的 / 20..29 どの部位にも付く汎用 / 30.. 注目度が低いもの。'
  'attribute_value.display_order (0008) と同じ理由 — アルファベット順は意味を壊す。';

-- =====================================================================
-- 2. 乳輪の広さ
-- =====================================================================
INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('areola_size', 'areola size', 'appearance', 'categorical', 'region', 'areola')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('areola_size','small'), ('areola_size','medium'),
  ('areola_size','large'), ('areola_size','very_large')
ON CONFLICT DO NOTHING;

UPDATE attribute SET description =
  '乳輪の広さ。**乳房の大きさ (breast_size) とは独立** — 乳房が小さくて乳輪が広い、'
  'その逆もある。頭基準にはしない (0012 が頭基準なのは乳房が体格に依らない基準を'
  '要したため。乳輪は乳房との比で見るのが自然)。**乳房の幅に対する割合**で刻む'
WHERE slug = 'areola_size';

UPDATE attribute_value SET description = v.d, display_order = v.o FROM (VALUES
  ('areola_size','small',     '乳房の幅の 1/5 未満',            1),
  ('areola_size','medium',    '乳房の幅の 1/5 〜 1/3 程度',      2),
  ('areola_size','large',     '乳房の幅の 1/3 〜 1/2 程度',      3),
  ('areola_size','very_large','乳房の幅の 1/2 以上',            4)
) AS v(a,s,d,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

-- =====================================================================
-- 3. 乳首の形状 (大きさではない別の軸)
-- =====================================================================
INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('nipple_state', 'nipple state', 'appearance', 'categorical', 'region', 'nipple')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('nipple_state','flat'), ('nipple_state','protruding'),
  ('nipple_state','puffy'), ('nipple_state','inverted')
ON CONFLICT DO NOTHING;

UPDATE attribute SET description =
  '乳首の形状。flat / protruding / puffy / inverted。**広さ (areola_size) とは別の軸** — '
  'puffy は乳輪ごと膨らんだ状態で、乳輪が広いだけの large とは違う。inverted は'
  '真逆なので同じ軸に載る'
WHERE slug = 'nipple_state';

UPDATE attribute_value SET description = v.d, display_order = v.o FROM (VALUES
  ('nipple_state','inverted',  '陥没している',                                    1),
  ('nipple_state','flat',      '平坦。突出していない',                            2),
  ('nipple_state','protruding','突出している',                                    3),
  ('nipple_state','puffy',     '乳輪ごと盛り上がっている。**広いだけなら areola_size**', 4)
) AS v(a,s,d,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

-- =====================================================================
-- 4. 表示順 — **INSERT の後に置く。** 先に書くと新規属性に当たらない
--    (2026-07-29 実測: areola_size / nipple_state が末尾に出た)。
-- =====================================================================
UPDATE attribute SET display_order = v.o FROM (VALUES
  -- 部位固有の主要な事実
  ('breast_size',1), ('areola_size',1), ('nipple_state',1), ('penis_size',1),
  ('erection_state',1), ('ejaculation_state',2), ('squirting_state',1),
  ('facial_expression',1), ('mouth_state',1), ('eye_color',1), ('eye_state',2),
  ('gaze_direction',3), ('hair_density',1), ('belly_shape',1), ('foot_contact',1),
  ('muscularity',1), ('adiposity',2), ('pregnancy_state',3), ('involuntary_motion',4),
  -- 副次的だが部位固有
  ('hair_length',10), ('hair_style',11), ('joint_flexion',12), ('wetness',13),
  -- どの部位にも付く汎用
  ('pigmentation',20), ('skin_tone',21), ('surface_substance',22), ('flush_intensity',23),
  -- 注目度が低い
  ('hair_color',30)
) AS v(s,o) WHERE attribute.slug = v.s;


COMMIT;
