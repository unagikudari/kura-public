-- Migration 0045 — 身につけているものに「どこに」を足し、ピアスを書けるようにする
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘:「ピアスもない」
--
-- ── ピアスを足すだけでは足りなかった (調べて 2 つ出た) ──
-- (1) **`obs_object` に部位が無い。** 列は observation_id / object_slug / state_slug だけで、
--     「どこに付いているか」を言えない。ピアスは**部位が本体**の対象で、
--     nipple / navel / ear / tongue / clitoris のどれかを言えなければ書く意味が無い。
--     実測 (production kura): piercing 49,529 / ear_piercing 23,871 /
--     nipple_piercing 13,708 / navel_piercing 4,490 / clitoris_piercing 3,841 /
--     tongue_piercing 2,970 — **タグ空間は部位ごとに語を作って対応している**。
--     部位を持てば `piercing` 1 語で全部書ける (0008 で体毛を部位ごとに作らず
--     hair_density にしたのと同じ)。
-- (2) **`resolve.py` に object の分岐が無かった。** `resolved_facet.object_slug` 列は
--     あるのに、domain='object' の所見は**確定所見にならない**。つまり書けても引けない。
--     「宣言と着地の乖離」の 11 件目 — 0037 (presentation を検証だけして列が無い) と同型。
--     本 migration の DDL と合わせて resolve.py 側も直す。
--
-- ── 部位は NULL 可 ──
-- 服 (dress / socks) は部位を言わなくても成立する。ピアスのように部位が要るものだけ
-- 書けばよい。**NULL は「言っていない」** — 既定の意味を他の列と揃える。
--
-- ── ピアスの語は 1 つ ──
-- `piercing` だけ入れる。`nipple_piercing` のように部位を語に畳むと、
-- 部位が増えるたびに語が増え、タグ空間と同じ組合せ爆発になる
-- (実際 transferred_pubic_hair_* は 5 語に分かれていて 3 語は 0 件だった)。
-- 併せて実測需要のある装身具を足す: earrings 137,291 / necklace は既存 /
-- ring 相当が無いので `ring`、`navel_piercing` 等は入れない。
--
-- ── 運用手順 ──
--   * 単一 transaction。列 + FK (NOT VALID → VALIDATE) → object_type → 説明。冪等。
--   * ロールバック: 追加した列と object_type を DROP/DELETE。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- =====================================================================
-- 1. 身につけているものに「どこに」を足す
-- =====================================================================
ALTER TABLE obs_object ADD COLUMN IF NOT EXISTS region_slug text;
ALTER TABLE obs_object DROP CONSTRAINT IF EXISTS obs_object_region_fkey;
ALTER TABLE obs_object ADD CONSTRAINT obs_object_region_fkey
    FOREIGN KEY (region_slug) REFERENCES body_region(slug) NOT VALID;
ALTER TABLE obs_object VALIDATE CONSTRAINT obs_object_region_fkey;

COMMENT ON COLUMN obs_object.region_slug IS
  'その物が**どこに付いているか**。ピアスのように部位が本体の対象で要る — '
  '部位を言えなければ nipple / navel / ear / tongue / clitoris を区別できない。'
  '**NULL は「言っていない」** — 服のように部位を言わなくても成立するものは NULL のまま。'
  '**部位を語に畳まない** (nipple_piercing のような合成語を作らない) ため、'
  'object は `piercing` 1 語で、どこかは この列が持つ';

-- =====================================================================
-- 2. ピアスと、実測需要のある装身具
-- =====================================================================
-- category は既存の語彙から選ぶ (新設しない — UI のグループが増えると選ぶのが重い)
-- participation も既存の語彙 (holding / present_in_scene / wearing) から選ぶ。
-- **slot は付けない** — slot は headwear / neckwear のような「服の枠」で、
-- ピアスの位置は body_region が持つ (枠と部位を二重に持たない)。
INSERT INTO object_type(slug, display_name, category, participation) VALUES
  ('piercing',  'ピアス',       'accessory', 'wearing'),
  ('earrings',  'イヤリング',   'accessory', 'wearing'),
  ('ring',      '指輪',         'accessory', 'wearing'),
  ('anklet',    'アンクレット', 'accessory', 'wearing'),
  ('bracelet',  'ブレスレット', 'accessory', 'wearing'),
  ('choker',    'チョーカー',   'accessory', 'wearing')
ON CONFLICT (slug) DO NOTHING;

UPDATE object_type SET description =
  '**部位は obs_object.region_slug で言う** — nipple_piercing のような合成語を作らない。'
  'タグ空間は ear/nipple/navel/tongue/clitoris と部位ごとに語を持っているが、'
  'ここは `piercing` 1 語 + 部位で全部書ける'
WHERE slug = 'piercing' AND (description IS NULL OR description NOT LIKE '%region_slug%');

COMMIT;
