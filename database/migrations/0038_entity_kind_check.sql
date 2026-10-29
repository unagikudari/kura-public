-- Migration 0038 — entity の CHECK が kind を列挙していて creature を弾いていた
--
-- 2026-07-29、resolver 実行中に発覚:
--   ERROR: 新しい行は検査制約 "entity_check" に違反 — (…, creature, null, null, …)
--
-- ── 原因 (0030 で作った乖離。10 件目) ──
-- 旧 CHECK:
--   (kind='person' AND object_slug IS NULL) OR (kind='object' AND gender_presentation IS NULL)
-- **kind を列挙している**ので、0030 で `entity_kind` に `creature` を足したのに
-- entity 表がそれを受け取れなかった。語彙を足しても着地しない、という同じ形。
--
-- ── 直し方: kind を列挙しない ──
-- 「どの kind か」ではなく「**その列は誰のものか**」を書く:
--   * `object_slug` は object のときだけ
--   * `gender_presentation` は person のときだけ
-- こう書けば **kind が増えても CHECK を直す必要が無い** (新しい kind は既定でどちらの
-- 列も持たない)。列挙は「増えたら直す」を前提にしていて、実際に直し忘れた。
--
-- ── 運用手順 ──
--   * 単一 transaction。新 CHECK を足してから旧を落とす (不変条件が消える瞬間を作らない)。
--   * NOT VALID → VALIDATE。既存行は person のみなので即通る。
--   * ロールバック: 旧 CHECK を再作成 (ただし creature が入っていれば失敗する — それが正しい)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

ALTER TABLE entity ADD CONSTRAINT ck_entity_object_slug_only_object
    CHECK (object_slug IS NULL OR kind = 'object') NOT VALID;
ALTER TABLE entity VALIDATE CONSTRAINT ck_entity_object_slug_only_object;

ALTER TABLE entity ADD CONSTRAINT ck_entity_presentation_only_person
    CHECK (gender_presentation IS NULL OR kind = 'person') NOT VALID;
ALTER TABLE entity VALIDATE CONSTRAINT ck_entity_presentation_only_person;

ALTER TABLE entity DROP CONSTRAINT IF EXISTS entity_check;

COMMENT ON TABLE entity IS
  'Resolver の出力。run が所有する (0002)。**CHECK は kind を列挙しない** — '
  '「その列は誰のものか」で書いてあるので、kind が増えても直す必要が無い '
  '(0030 で creature を足したとき、列挙型の CHECK が受け取れず実行時に落ちた)。';

COMMIT;
