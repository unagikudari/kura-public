-- Migration 0016 — 属性に「同時に複数の値を取りうるか」を持たせる
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-28:
-- 「surface_substance はチェックボックスにして複数同時選択が可能にして」。
--
-- ── なぜ UI 側に slug を直書きしないか ──
-- 「複数取りうる」は **属性そのものの性質**であって、画面の都合ではない:
--   * `surface_substance` — 汗と精液が同時に付いていることはある。値どうしは排他でない。
--   * `breast_size` / `erection_state` / `pigmentation` — 順序尺度の 1 点。同時に
--     large でも huge でもあることはない。
-- UI に `if (slug === 'surface_substance')` と書くと、次に多値の属性が増えるたびに
-- コードを直すことになり、ontology を読んでも「どれが多値か」が判らない。
-- 列として持てば **UI は ontology を読むだけ**で済む (ontology-as-data)。
--
-- ── 観測側は既に多値を書ける ──
-- obs_attribute は 1 観測 1 行で、同じ属性の別の値は**別の observation** になる
-- (append-only)。よって書き込み経路の変更は不要 — 足りなかったのは「複数選んでよい」
-- という宣言だけ。
--
-- ── 運用手順 ──
--   * 単一 transaction。列追加 (NOT NULL DEFAULT false) → 該当属性を true に。
--   * 冪等。ロールバック: ALTER TABLE attribute DROP COLUMN is_multivalued;

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

ALTER TABLE attribute ADD COLUMN IF NOT EXISTS is_multivalued boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN attribute.is_multivalued IS
  'true = 同時に複数の値を取りうる (値どうしが排他でない)。surface_substance は '
  '「汗と精液が同時に付いている」があるので true。順序尺度の属性 (breast_size / '
  'pigmentation / erection_state 等) は 1 点しか取らないので false。'
  '観測側は 1 属性 1 行 (別の値は別 observation) なので書き込み経路は共通 — '
  'この列は「複数選んでよい」という宣言で、注釈 UI が入力方式を決めるのに使う。';

UPDATE attribute SET is_multivalued = true WHERE slug = 'surface_substance';

COMMIT;
