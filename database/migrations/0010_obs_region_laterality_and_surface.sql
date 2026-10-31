-- Migration 0010 — 観測に「どの部位か / 左右どちらか」を持たせ、色素沈着・付着物・射精を表せるようにする
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-28:
-- 「色素沈着が表現できない。汚れが表現できない。性器の動きが表現できない。射精しているとか。
--   ポーズ及び部位について… 両腕で違う動きがあれば追加する形」。
--
-- ── 根本原因 (これまでと同じ族: 宣言と着地の乖離) ──
-- `attribute.applies_kind='region'` は「属性は region に付く」と宣言しているのに、
-- `obs_attribute` の列は observation_id / attribute_slug / value_slug / value_numeric /
-- interval_low / interval_high しか無く **どの region の話かを書けない**。
--   * `skin_tone=dark` と書いても「顔が濃い」のか「乳輪が濃い」のか区別できない。
--   * bilateral な region (breast / labia_minora 等) の左右も言えない。
-- 同様に `obs_pose` は pose_slug だけで **laterality が無い**ため、
-- 「左腕は上げて右腕は背中」が書けない (arm_up と arms_behind_back を並べても
--  どちらの腕か不明で、非捏造の観点では書けないのと同じ)。
--
-- CRIT-A2 (contested/candidate の保存先が無い) / 0004 (obs_relation の ref) と同型で、
-- **実際に注釈してみないと出てこない**種類の欠陥。これで 4 件目。
--
-- ── 足す語彙 (コーパス実測で頻度を確認したもの) ──
--   * `pigmentation` — 色素沈着の濃さ。`skin_tone` は applies_key='surface_skin' なので
--     labia_minora (external_genital + surface_mucosa) に届かなかった。
--     applies_kind='entity' にして **region は観測側 (obs_attribute.region_slug) で指す**。
--     裏付け: dark_labia_minora 5,557 / dark_areolae 14,700 / dark_nipples 31,658。
--   * `surface_substance` — 表面に付着している物質 (汚れ)。region_slug と組めば
--     「顔に精液」「胸に汗」が書ける。裏付け: sweat 569,121 / saliva 214,048 /
--     cum_in_pussy 171,695 / cum_on_body 136,345 / tears 128,030 / cum_on_breasts 95,059 /
--     lactation 29,241 / dirty 4,634 / mud 383。
--   * `ejaculation_state` — 射精の状態。erection_state と同じ region=penis の state 属性。
--     裏付け: ejaculation 129,326。
--
-- ── 運用手順 ──
--   * 単一 transaction。列追加 → CHECK (NOT VALID → VALIDATE) → 語彙 INSERT の順。
--   * 既存行は新列が NULL なので VALIDATE は即通る。
--   * ロールバック: obs_attribute / obs_pose から DROP COLUMN、追加した attribute を DELETE。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- =====================================================================
-- 1. 観測に「どの部位か / 左右どちらか」を持たせる
-- =====================================================================
ALTER TABLE obs_attribute ADD COLUMN region_slug text REFERENCES body_region(slug);
ALTER TABLE obs_attribute ADD COLUMN laterality  text;
ALTER TABLE obs_attribute ADD CONSTRAINT ck_obs_attribute_laterality
    CHECK (laterality IS NULL OR laterality IN ('na','left','right')) NOT VALID;
ALTER TABLE obs_attribute VALIDATE CONSTRAINT ck_obs_attribute_laterality;

COMMENT ON COLUMN obs_attribute.region_slug IS
  'この属性がどの region の話かを指す。NULL = 主体 (entity) 全体の属性。'
  '`attribute.applies_kind=region` の属性でも、bilateral な region の左右や、'
  'class に適用される属性 (skin_tone / pigmentation) の対象部位はここでしか言えない。';
COMMENT ON COLUMN obs_attribute.laterality IS
  'bilateral な region の左右 (na/left/right)。左右で値が違う場合に別々の観測として書ける。';

ALTER TABLE obs_pose ADD COLUMN laterality text;
ALTER TABLE obs_pose ADD CONSTRAINT ck_obs_pose_laterality
    CHECK (laterality IS NULL OR laterality IN ('na','left','right')) NOT VALID;
ALTER TABLE obs_pose VALIDATE CONSTRAINT ck_obs_pose_laterality;

COMMENT ON COLUMN obs_pose.laterality IS
  '左右非対称な肢の pose を書き分けるための左右 (na/left/right)。'
  '「左腕は上げて右腕は背中」は arm_up(left) と arms_behind_back(right) の 2 観測になる。'
  'NULL / na は左右を問わない (両側 or 該当なし)。';

-- =====================================================================
-- 2. 色素沈着 — skin_tone が届かない region のために region 指定で使う
-- =====================================================================
INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('pigmentation', 'pigmentation', 'appearance', 'categorical', 'entity', 'person')
ON CONFLICT (slug) DO NOTHING;
INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('pigmentation','light'),('pigmentation','medium'),
  ('pigmentation','dark'),('pigmentation','very_dark')
ON CONFLICT DO NOTHING;

-- =====================================================================
-- 3. 付着物 (汚れ) — どこに何が付いているか
-- =====================================================================
INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('surface_substance', 'surface substance', 'state', 'categorical', 'entity', 'person')
ON CONFLICT (slug) DO NOTHING;
INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('surface_substance','semen'),('surface_substance','saliva'),('surface_substance','sweat'),
  ('surface_substance','tears'),('surface_substance','milk'),('surface_substance','urine'),
  ('surface_substance','vaginal_fluid'),('surface_substance','dirt'),
  ('surface_substance','mud'),('surface_substance','oil')
ON CONFLICT DO NOTHING;

-- =====================================================================
-- 4. 射精の状態
-- =====================================================================
INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('ejaculation_state', 'ejaculation state', 'state', 'categorical', 'region', 'penis')
ON CONFLICT (slug) DO NOTHING;
INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('ejaculation_state','not_ejaculating'),('ejaculation_state','ejaculating'),
  ('ejaculation_state','post_ejaculation')
ON CONFLICT DO NOTHING;

-- =====================================================================
-- 5. 説明と並び順
-- =====================================================================
UPDATE attribute SET description = v.d FROM (VALUES
  ('pigmentation',
   '色素沈着の濃さ。light / medium / dark / very_dark。**どの部位かは観測側で指定する** — '
   '乳輪・乳首・小陰唇・肛門周囲など部位ごとに別々の観測として書ける。'
   'skin_tone は applies_key=surface_skin なので粘膜や外性器には届かない (そのための属性)'),
  ('surface_substance',
   '表面に付着している物質。semen / saliva / sweat / tears / milk / urine / vaginal_fluid / '
   'dirt / mud / oil。**どこに付いているかは観測側で部位を指定する** — 「顔に精液」「胸に汗」が'
   '部位ごとの観測として書ける'),
  ('ejaculation_state',
   '射精の状態。not_ejaculating / ejaculating / post_ejaculation。'
   'erection_state と同じく penis に付く state 属性で、勃起の度合いとは独立に書ける')
) AS v(s,d) WHERE attribute.slug = v.s;

UPDATE attribute_value SET display_order = v.o FROM (VALUES
  ('pigmentation','light',1),('pigmentation','medium',2),
  ('pigmentation','dark',3),('pigmentation','very_dark',4),
  ('ejaculation_state','not_ejaculating',1),('ejaculation_state','ejaculating',2),
  ('ejaculation_state','post_ejaculation',3),
  -- 体液 → 汚れ の順 (名義尺度だが探しやすさのため頻度順に寄せる)
  ('surface_substance','semen',1),('surface_substance','vaginal_fluid',2),
  ('surface_substance','saliva',3),('surface_substance','sweat',4),
  ('surface_substance','tears',5),('surface_substance','milk',6),
  ('surface_substance','urine',7),('surface_substance','dirt',8),
  ('surface_substance','mud',9),('surface_substance','oil',10)
) AS v(a,s,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

COMMIT;
