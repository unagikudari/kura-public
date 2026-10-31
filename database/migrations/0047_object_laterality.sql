-- Migration 0047 — 身につけているものにも左右を書けるようにする
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘:「両耳に一つずつは？」
--
-- ── 書けなかった ──
-- `ear` は `laterality='bilateral'` なのに `obs_object` に左右の列が無く、
-- **「ear に 2 個」としか書けない**。それでは
--   * 両耳に 1 個ずつ
--   * 片耳に 2 個
-- が同じ所見になる。0025 で「右手で A / 左手で A が同じキーに潰れる」を直したのと
-- 同じ欠陥が、object にだけ残っていた。
--
-- ── 同じ軸が 1 箇所だけ抜けていた ──
-- laterality は既に obs_attribute (0010) / obs_pose (0010) / obs_relation (0025) にあり、
-- 意味も 0036 で「**その人物にとっての左右** (解剖学的左右)」と定義済み。
-- `resolved_facet.region_laterality` も既にある。**obs_object だけが仲間外れ**だった。
-- 新しい軸を作るのではなく、既にある軸を届いていない表へ通すだけ。
--
-- ── 値の意味は 0036 と同じ ──
--   * NULL — **言っていない** (左右を見ていない / 判らない)
--   * 'na'  — **左右の別が無い** (midline の部位、または両側に等しく当てはまる)
--   * left / right — その人物にとっての左右
-- 「両耳に 1 個ずつ」は **left に 1 / right に 1 の 2 所見**。'na' で 2 個ではない
-- ('na' は「左右を区別しない」であって「両方」ではない)。
--
-- ── 運用手順 ──
--   * 単一 transaction。列 + CHECK (NOT VALID → VALIDATE) → 説明。冪等。
--   * ロールバック: 追加した列を DROP (所見が付いていれば消せない)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

ALTER TABLE obs_object ADD COLUMN IF NOT EXISTS laterality text;
ALTER TABLE obs_object DROP CONSTRAINT IF EXISTS ck_obs_object_laterality;
ALTER TABLE obs_object ADD CONSTRAINT ck_obs_object_laterality
    CHECK (laterality IS NULL OR laterality IN ('na','left','right')) NOT VALID;
ALTER TABLE obs_object VALIDATE CONSTRAINT ck_obs_object_laterality;

COMMENT ON COLUMN obs_object.laterality IS
  'その物が付いている部位の、**その人物にとっての左右** (解剖学的左右、0036)。'
  'カメラから見た左右ではない。'
  '**「両耳に 1 個ずつ」は left に 1 / right に 1 の 2 所見** — '
  '「ear に 2 個」とだけ書くと片耳に 2 個と区別できない。'
  '**NULL は「言っていない」**、''na'' は**左右の別が無い** (midline の部位)。'
  '''na'' は「両方」という意味ではない';

COMMIT;
