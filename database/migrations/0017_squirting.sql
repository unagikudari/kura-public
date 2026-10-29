-- Migration 0017 — 潮吹きを表せるようにする
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-28:
-- 「潮吹きしているようだが表す語彙がない」。
--
-- ── なぜ既存の語で代用できないか ──
-- (1) `surface_substance = vaginal_fluid` (愛液) に寄せる → **別の現象を愛液と書く**。
--     コーパス実測でも pussy_juice 137,294 と female_ejaculation 32,169 は別タグとして
--     定着しており、注釈者も別物として見ている。粘性のある分泌物と、噴出する水様の
--     液体を同じ値にすると、どちらも検索で引けなくなる。
-- (2) `ejaculation_state` を使う → この属性は applies_key='penis' で、値は
--     not_ejaculating / precum / ejaculating / post_ejaculation。**vulva に適用すると
--     precum を選べてしまう** — 値空間が男性器のものなので意味が壊れる。
--
-- ── 0013 (precum) と同じ二軸で入れる ──
--   * **state (出ている)** — 新しい属性 `squirting_state` を vulva に付ける。
--     ejaculation_state と同じ形 (状態 3 段) で、値空間だけ女性器のものにする。
--     1 つの属性に男女を兼ねさせず、部位ごとに正しい値空間を持たせる方が
--     「選べない値が候補に出ない」という 0008/0011 の方針と一貫する。
--   * **substance (付いている)** — `surface_substance` に `female_ejaculate` を足す。
--     体や床に飛んだ液体は付着物として部位ごとに書ける (vaginal_fluid とは別値)。
--
-- ── 運用手順 ──
--   * 単一 transaction。attribute → 値 → 説明 → 並び順。
--   * 冪等 (ON CONFLICT DO NOTHING / 同値 UPDATE)。
--   * ロールバック: 追加した attribute / attribute_value を DELETE
--     (観測が参照していれば FK が拒否する — それが正しい)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- =====================================================================
-- 1. state — 潮を噴いている状態 (ejaculation_state の女性器版)
-- =====================================================================
INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('squirting_state', 'squirting state', 'state', 'categorical', 'region', 'vulva')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('squirting_state','not_squirting'),
  ('squirting_state','squirting'),
  ('squirting_state','post_squirting')
ON CONFLICT DO NOTHING;

UPDATE attribute SET description =
  '潮吹きの状態。not_squirting / squirting / post_squirting。'
  'ejaculation_state (penis) と同じ形の state 属性で、**値空間だけ女性器のもの**にしてある — '
  '1 つの属性で男女を兼ねると vulva に precum を選べてしまう。'
  '飛んだ液体が体や床に付いているのは属性 surface_substance=female_ejaculate で別に書く'
WHERE slug = 'squirting_state';

UPDATE attribute_value SET description = v.d FROM (VALUES
  ('squirting_state','not_squirting',  '潮を噴いていない'),
  ('squirting_state','squirting',      '潮を噴いている最中 (液体が噴出している)'),
  ('squirting_state','post_squirting', '噴いた直後 (出た後の状態)')
) AS v(a,s,d) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

UPDATE attribute_value SET display_order = v.o FROM (VALUES
  ('squirting_state','not_squirting',1),('squirting_state','squirting',2),
  ('squirting_state','post_squirting',3)
) AS v(a,s,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

-- =====================================================================
-- 2. substance — 飛んだ潮が付いている
-- =====================================================================
INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('surface_substance', 'female_ejaculate')
ON CONFLICT DO NOTHING;

UPDATE attribute_value SET description = v.d FROM (VALUES
  ('surface_substance','female_ejaculate',
   '潮。噴出する水様の液体。**粘性のある愛液 (vaginal_fluid) とは別の値** — '
   'コーパスでも pussy_juice と female_ejaculation は別タグとして定着している'),
  ('surface_substance','vaginal_fluid',
   '愛液。粘性のある分泌物。噴出する水様の液体は female_ejaculate')
) AS v(a,s,d) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

-- 体液 → 分泌由来の付着物 → 排泄物 → 外的な汚れ
UPDATE attribute_value SET display_order = v.o FROM (VALUES
  ('surface_substance','semen',1),('surface_substance','precum',2),
  ('surface_substance','vaginal_fluid',3),('surface_substance','female_ejaculate',4),
  ('surface_substance','smegma',5),('surface_substance','saliva',6),
  ('surface_substance','sweat',7),('surface_substance','tears',8),
  ('surface_substance','milk',9),('surface_substance','urine',10),
  ('surface_substance','scat',11),('surface_substance','dirt',12),
  ('surface_substance','mud',13),('surface_substance','oil',14)
) AS v(a,s,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

COMMIT;
