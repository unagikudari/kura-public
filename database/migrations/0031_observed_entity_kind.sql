-- Migration 0031 — 「その主体が人か非人間か」を観測として書けるようにする
--
-- 0030 で `entity_kind` に `creature` を足し、注釈 UI でも選べるようにしたが、
-- **観測にそれを書く列が無かった** (2026-07-29)。結果、注釈者が「これは触手の主で
-- 人ではない」と言っても DB に届かず、resolver は全 entity を person として作る。
-- CRIT-A2 / 0010 / 0019 / 0024 と同じ「宣言と着地の乖離」の **8 件目**。
--
-- 置き場は `obs_identity`。この表は「その主体が何であるか」(species / character) を
-- 記録する場所で、**kind はその最も粗い層**にあたる。別 domain を立てるより、
-- 同じ問いの答えを 1 か所にまとめる方が、後から「identity は何を言えるか」を
-- 読むときに追いやすい。
--
-- 既定は書かない (NULL)。NULL = 「人か非人間かを言っていない」であって
-- 「person だと言った」ではない — resolver は NULL を person として扱うが、
-- **それは resolver の既定であって観測の主張ではない**ことを列コメントに残す。
--
-- ── 運用手順 ──
--   * 単一 transaction。列追加 + FK (NOT VALID → VALIDATE)。既存行は NULL。
--   * ロールバック: ALTER TABLE obs_identity DROP COLUMN entity_kind;

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

ALTER TABLE obs_identity ADD COLUMN IF NOT EXISTS entity_kind text;
ALTER TABLE obs_identity ADD CONSTRAINT obs_identity_entity_kind_fkey
    FOREIGN KEY (entity_kind) REFERENCES entity_kind(slug) NOT VALID;
ALTER TABLE obs_identity VALIDATE CONSTRAINT obs_identity_entity_kind_fkey;

COMMENT ON COLUMN obs_identity.entity_kind IS
  'その主体が person / creature / object のどれか。**NULL は「言っていない」であって '
  '「person だと言った」ではない** — resolver は NULL を person として扱うが、それは'
  'resolver の既定であって観測の主張ではない。0030 で creature を足したのに、'
  'それを書く場所が無かったので追加 (宣言と着地の乖離の 8 件目)。';

COMMIT;
