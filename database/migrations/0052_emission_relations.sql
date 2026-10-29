-- Migration 0052 — 排出を「どこからどこへ」で書けるようにする
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘:
-- 「peeing や scat, ejact は関係がふさわしいと思う。すでに書いたものは欠落になるが、
--   **発射元と命中先が存在する**。また、この場合、**空中及び地面**があり得る」
--
-- ── 0050/0051 の属性では足りなかった ──
-- `urination_state` / `defecation_state` / `ejaculation_state` は「**出ているか**」までで、
-- **どこへ向かっているか**を持たない。`surface_substance='urine'` は「付いている」で、
-- **誰から飛んだか**を持たない。つまり軌跡が丸ごと落ちていた。
--
-- ── 環境を指す手段が無かった (ここが本題) ──
-- `obs_relation` は subject_ref/region → object_ref/region の形で、**人体の部位どうし**
-- しか指せない。`relation_applicability.participant_kind` も class / entity / region の
-- 3 つだけ。**地面や空中を指す列が無い**。
-- `object_type` の environment は stone_wall 1 語しかなく、そもそも
-- **空中は物ではない**ので object として持たせるのは筋が悪い (user 判断で専用語彙に)。
--
-- ── 設計 ──
-- (1) 排出ごとに 3 語: urinating_onto / ejaculating_onto / defecating_onto。
--     **何が飛んでいるかが語で判る**ので、obs_relation に substance 列を足さない
--     (足すと surface_substance の値と二重に見える)。
-- (2) 対象は**相手の部位 (既存の object_ref/object_region_slug) か、環境 (新しい
--     object_surface_slug)**。**どちらを指しているかが列で判る。**
-- (3) `scene_surface` 語彙: ground / air / water / wall / furniture / container。
--     **NULL は「言っていない」**、`air` は**見た上で空中** (着地点が無い/見えない)。
--     この 2 つを分けるのは 0036 (NULL と na) / 0048 (NULL と ambiguous) と同じ理由。
-- (4) **属性は残す。** 消せない (append-only) し役割が違う:
--       属性 = 出ているか (not_urinating / post_urination / precum)
--       関係 = どこからどこへ
--     「出ているが行き先が見えない」は**対象を省いた関係**で書ける
--     (validate は片側だけの関係を許している)。
--
-- ── 既に書いた所見は欠落のまま ──
-- 属性だけ書いた分に軌跡は入らない。append-only なので上書きはできず、
-- **後から関係を足す**形になる (撤回は不要、情報が増えるだけ)。user 了承済み。
--
-- ── 運用手順 ──
--   * 単一 transaction。語彙 → 列 + FK → relation → 適用先 → 説明。冪等。
--   * ロールバック: 追加した列・表・relation を DROP/DELETE。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- =====================================================================
-- 1. 環境を指す語彙
-- =====================================================================
CREATE TABLE IF NOT EXISTS scene_surface (
  slug          text PRIMARY KEY,
  display_name  text NOT NULL,
  description   text
);
COMMENT ON TABLE scene_surface IS
  '関係の対象になりうる**環境**。人体の部位ではないので body_region には入れない。'
  '**空中は物ではない**ので object_type にも入れない (0052、user 判断)';

INSERT INTO scene_surface(slug, display_name, description) VALUES
  ('ground',    '地面・床',   '地面 / 床 / 畳。着地している'),
  ('air',       '空中',       '**見た上で空中** — 着地点が画面に無い、または届いていない。'
                              '「どこへ行ったか言っていない」は NULL であって air ではない'),
  ('water',     '水面・水中', '風呂 / プール / 海など'),
  ('wall',      '壁',         '垂直面。地面と分けるのは飛ぶ向きが違うため'),
  ('furniture', '家具',       'ベッド / 椅子 / 机など'),
  ('container', '容器',       '便器 / 洗面器 / コップなど、受ける物')
ON CONFLICT (slug) DO NOTHING;

-- =====================================================================
-- 2. 関係の対象に環境を許す
-- =====================================================================
ALTER TABLE obs_relation ADD COLUMN IF NOT EXISTS object_surface_slug text;
ALTER TABLE obs_relation DROP CONSTRAINT IF EXISTS obs_relation_object_surface_fkey;
ALTER TABLE obs_relation ADD CONSTRAINT obs_relation_object_surface_fkey
    FOREIGN KEY (object_surface_slug) REFERENCES scene_surface(slug) NOT VALID;
ALTER TABLE obs_relation VALIDATE CONSTRAINT obs_relation_object_surface_fkey;

-- **対象は人体か環境のどちらか。** 両方書くと「顔に、かつ地面に」という
-- 解釈不能な行になる (どちらか一方なら 2 所見で書く)。
ALTER TABLE obs_relation DROP CONSTRAINT IF EXISTS ck_obs_relation_object_one_kind;
ALTER TABLE obs_relation ADD CONSTRAINT ck_obs_relation_object_one_kind
    CHECK (object_surface_slug IS NULL OR (object_ref IS NULL AND object_region_slug IS NULL))
    NOT VALID;
ALTER TABLE obs_relation VALIDATE CONSTRAINT ck_obs_relation_object_one_kind;

COMMENT ON COLUMN obs_relation.object_surface_slug IS
  '関係の対象が**環境**のとき (地面・空中・水面など)。'
  '**相手の部位なら object_ref + object_region_slug** を使う — 両方は書けない '
  '(CHECK。両方あると「顔に、かつ地面に」という解釈不能な行になる。'
  '両方に飛んでいるなら 2 所見で書く)。'
  '**NULL は「言っていない」**、``air`` は**見た上で空中**';

-- =====================================================================
-- 3. 排出の関係 3 語
-- =====================================================================
INSERT INTO relation(slug, display_name, category, directionality) VALUES
  ('urinating_onto',   '排尿がかかっている', 'interaction', 'directed'),
  ('ejaculating_onto', '射精がかかっている', 'interaction', 'directed'),
  ('defecating_onto',  '排便がかかっている', 'interaction', 'directed')
ON CONFLICT (slug) DO NOTHING;

UPDATE relation SET description = v.d FROM (VALUES
  ('urinating_onto',
   '尿が**どこからどこへ**飛んでいるか。主体は尿道口、対象は相手の部位か環境 '
   '(地面 / 空中 など)。**出ているかだけなら属性 urination_state** (0050) — '
   'こちらは軌跡を言う。行き先が見えないなら**対象を省く**'),
  ('ejaculating_onto',
   '精液が**どこからどこへ**飛んでいるか。主体は陰茎、対象は相手の部位か環境。'
   '**出ているかだけなら属性 ejaculation_state** (0013)。'
   '**付着した結果は surface_substance=semen** — あれは着地後で、誰から飛んだかは言わない'),
  ('defecating_onto',
   '便が**どこからどこへ**出ているか。主体は肛門、対象は相手の部位か環境 (容器など)。'
   '**出ているかだけなら属性 defecation_state** (0051)')
) AS v(s,d) WHERE relation.slug = v.s;

INSERT INTO relation_applicability(relation_slug, role, participant_kind, participant_key) VALUES
  ('urinating_onto',   'subject', 'region', 'urethral_opening'),
  ('urinating_onto',   'object',  'region', 'any'),
  ('ejaculating_onto', 'subject', 'region', 'penis'),
  ('ejaculating_onto', 'object',  'region', 'any'),
  ('defecating_onto',  'subject', 'region', 'anus'),
  ('defecating_onto',  'object',  'region', 'any')
ON CONFLICT DO NOTHING;

-- =====================================================================
-- 4. 隣の語に境界を書く
-- =====================================================================
UPDATE attribute SET description = coalesce(description,'') ||
  '。**どこへ飛んでいるかは関係** (urinating_onto、0052) — こちらは出ているかだけ'
WHERE slug = 'urination_state' AND coalesce(description,'') NOT LIKE '%urinating_onto%';

UPDATE attribute SET description = coalesce(description,'') ||
  '。**どこへ飛んでいるかは関係** (defecating_onto、0052)'
WHERE slug = 'defecation_state' AND coalesce(description,'') NOT LIKE '%defecating_onto%';

UPDATE attribute SET description = coalesce(description,'') ||
  '。**どこへ飛んでいるかは関係** (ejaculating_onto、0052) — こちらは出ているかだけで、'
  'precum のように軌跡を持たない値もある'
WHERE slug = 'ejaculation_state' AND coalesce(description,'') NOT LIKE '%ejaculating_onto%';

UPDATE attribute SET description = coalesce(description,'') ||
  '。**誰から飛んだかは関係** (urinating_onto / ejaculating_onto / defecating_onto、0052) — '
  'こちらは着地後の話で、由来を持たない'
WHERE slug = 'surface_substance' AND coalesce(description,'') NOT LIKE '%ejaculating_onto%';

COMMIT;
