-- Migration 0028 — 位置の点が指す先を body_region にする (頭以外も指せるように)
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-29:
-- 「人物の位置関係、画像位置選択で頭ではなく性器だとか腕だけを指定したい」。
--
-- ── 欠陥: 語彙を二重に持っていた ──
-- 0021 で `asset_region_anchor` (head_center / face_center / body_center) という
-- **3 語の専用語彙**を作った。しかしこれは `body_region` (119 語) の劣化コピーでしかない —
-- `head_center` は「**`head` の中心**」であり、`face_center` は `face`、`body_center` は
-- `body`。3 つとも body_region に実在する。
--
-- 結果、**頭・顔・体幹しか指せず、性器や腕を指す手段が無かった**。語彙を足して
-- 追いかけると `penis_center` `arm_center` … と 119 語を複製することになる。
--
-- 正しい形は「点は **body_region の中心**を指す」— 参照先を body_region にすれば、
-- 部位が増えたとき自動で指せるようになる。専用語彙は消す (二重の真実を残さない)。
--
-- ── 移行 ──
-- 既存は 11 行すべて head_center。`<slug>_center` から `_center` を落として body_region
-- を引く。3 語とも body_region に存在するので取りこぼしは無い (実行後に検証)。
--
-- ── 運用手順 ──
--   * 単一 transaction。列追加 → backfill → CHECK 差し替え → 旧列と旧表を削除。
--   * CHECK は NOT VALID → VALIDATE。**旧 CHECK を先に落とさない** — 先に落とすと
--     途中で不変条件が消える瞬間ができる。新 CHECK を足してから旧を落とす。
--   * ロールバック: anchor_kind を戻すには asset_region_anchor の再作成が要るので、
--     実質不可逆。ただし失う情報は無い (body_region の方が真に細かい)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- =====================================================================
-- 1. 点が指す部位
-- =====================================================================
ALTER TABLE asset_region ADD COLUMN IF NOT EXISTS anchor_region_slug text
    REFERENCES body_region(slug);

UPDATE asset_region
   SET anchor_region_slug = regexp_replace(anchor_kind, '_center$', '')
 WHERE anchor_kind IS NOT NULL AND anchor_region_slug IS NULL;

-- 取りこぼしが無いことを migration 内で確かめる (後で気付くのでは遅い)
DO $$
DECLARE n integer;
BEGIN
  SELECT count(*) INTO n FROM asset_region
   WHERE anchor_kind IS NOT NULL AND anchor_region_slug IS NULL;
  IF n > 0 THEN
    RAISE EXCEPTION '移行できなかった anchor_kind が % 行ある', n;
  END IF;
END $$;

COMMENT ON COLUMN asset_region.anchor_region_slug IS
  '点が指す部位。**点は「その部位の中心」を意味する。** 0021 では 3 語の専用語彙 '
  '(asset_region_anchor) を持っていたが、それは body_region の劣化コピーで、'
  '頭・顔・体幹しか指せなかった (2026-07-29 指摘)。body_region を参照すれば'
  '部位が増えたとき自動で指せる。';

-- =====================================================================
-- 2. 不変条件を新しい三つ組へ移す (先に足してから旧を落とす)
-- =====================================================================
ALTER TABLE asset_region ADD CONSTRAINT ck_asset_region_anchor_complete2
    CHECK ((anchor_x IS NULL) = (anchor_y IS NULL)
       AND (anchor_x IS NULL) = (anchor_region_slug IS NULL)) NOT VALID;
ALTER TABLE asset_region VALIDATE CONSTRAINT ck_asset_region_anchor_complete2;

ALTER TABLE asset_region DROP CONSTRAINT IF EXISTS ck_asset_region_anchor_complete;

-- =====================================================================
-- 3. 二重の真実を消す
-- =====================================================================
ALTER TABLE asset_region DROP COLUMN IF EXISTS anchor_kind;
DROP TABLE IF EXISTS asset_region_anchor;

COMMIT;
