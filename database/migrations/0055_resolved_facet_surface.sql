-- Migration 0055 — 環境を解決形にも運ぶ
--
-- 0052 で `obs_relation.object_surface_slug` を足したが、**`resolved_facet` 側に
-- 列が無かった** — 書けても引けない状態 (「宣言と着地の乖離」の 12 件目)。
-- 0045 で object の分岐を足したときに同じことを踏んでおり、**列を足したら
-- 必ず解決形も見る**という手順が要ることを示している。
--
-- 併せて解決キーにも環境を含める (resolve.py) — 「地面へ」と「空中へ」は別の事実で、
-- 含めないと片方が他方を上書きする。
--
-- ロールバック: 追加した列を DROP。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

ALTER TABLE resolved_facet ADD COLUMN IF NOT EXISTS object_surface_slug text;
ALTER TABLE resolved_facet DROP CONSTRAINT IF EXISTS resolved_facet_object_surface_fkey;
ALTER TABLE resolved_facet ADD CONSTRAINT resolved_facet_object_surface_fkey
    FOREIGN KEY (object_surface_slug) REFERENCES scene_surface(slug) NOT VALID;
ALTER TABLE resolved_facet VALIDATE CONSTRAINT resolved_facet_object_surface_fkey;

COMMENT ON COLUMN resolved_facet.object_surface_slug IS
  '関係の対象が環境のとき (obs_relation.object_surface_slug をそのまま運ぶ)。'
  '**相手の部位なら object_region_slug** — 両方は立たない';

COMMIT;
