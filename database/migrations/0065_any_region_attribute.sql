-- Migration 0065 — 「どの部位にも付く属性」をスキーマ側で言えるようにする
--
-- plan kura-1.0-implementation、2026-07-31。語彙の横断検索を作っている最中に発見。
--
-- ── 宣言と着地の乖離 ──
-- `surface_substance` と `pigmentation` は **applies_kind='entity'** と宣言されているのに、
-- 実際の所見は**部位ごとに書かれている** (実測: penis 18 / breast 7 / head 6 / pelvis 6 /
-- face 5 / body 5 / vulva 4 / mouth 4)。宣言と実態が食い違っていた。
--
-- ── 例外が Python の定数に隠れていた (ここが要点) ──
-- UI は `ANY_REGION = {"pigmentation", "surface_substance"}` という**ハードコードの集合**で
-- この 2 つだけを特別扱いしていた。つまり
--   * DB を読んだだけでは「どの部位にも付く」と判らない
--   * 3 つ目を足す人は Python 側の集合を知らないと壊す
--   * 語彙を横断で引く機能は DB の宣言を信じて**部位なしで入れてしまう**
-- という形で、**例外が機構の外にあった**。
-- (「原則に従い、例外が少なく機構で守れる形を選ぶ」— feedback-fix-by-enforced-invariant)
--
-- ── 直し方 ──
-- `applies_kind` に **`any_region`** を足し、2 つをそこへ移す。
-- これで「どの部位にも付く」が**表の値として読める**ようになり、
-- UI 側のハードコードは消える。
--   region     — その部位だけ (breast_size → breast)
--   class      — そのクラスの部位全部 (wetness → surface_mucosa)
--   any_region — **どの部位にも付く** (surface_substance / pigmentation)
--   entity     — 部位に紐づかない (muscularity など)
--
-- ── 所見は触らない ──
-- `obs_attribute.region_slug` は既に入っている (append-only なので触れない)。
-- 変えるのは**宣言の側**だけで、実態に合わせる方向なので既存の所見は全部有効なまま。
--
-- ロールバック: applies_kind='entity' に戻し CHECK を元に戻す。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

ALTER TABLE attribute DROP CONSTRAINT IF EXISTS attribute_applies_kind_check;
ALTER TABLE attribute ADD CONSTRAINT attribute_applies_kind_check
  CHECK (applies_kind IN ('class','region','entity','any_region'));

UPDATE attribute SET applies_kind = 'any_region', applies_key = 'any'
 WHERE slug IN ('surface_substance','pigmentation');

COMMENT ON COLUMN attribute.applies_kind IS
  'その属性がどこに付くか。region = その部位だけ / class = そのクラスの部位全部 / '
  '**any_region = どの部位にも付く** (0065) / entity = 部位に紐づかない。'
  '**any_region を表の値にしたのは、例外が Python の定数に隠れていたため** — '
  'UI が `ANY_REGION = {pigmentation, surface_substance}` をハードコードしており、'
  'DB を読んだだけでは判らず、3 つ目を足す人が壊せる形だった (2026-07-31)';

UPDATE attribute SET description = coalesce(description,'') ||
  '。**どの部位に付いているかは所見ごとに指定する** (applies_kind=any_region、0065)'
WHERE slug IN ('surface_substance','pigmentation')
  AND coalesce(description,'') NOT LIKE '%any_region%';

COMMIT;
