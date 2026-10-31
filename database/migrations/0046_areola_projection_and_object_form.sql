-- Migration 0046 — puffy を乳輪へ移し、身につけているものに「形」と「数」を足す
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘 2 件:
--   「inverted, puffynipple は同時に存在し得る。puffynipple は areolea の属性だと思う」
--   「pierce は種類と数が入る」
--
-- ══════════════════════════════════════════════════════════════════════════
-- 1. puffy は乳首ではなく乳輪の事実
-- ══════════════════════════════════════════════════════════════════════════
-- `nipple_state` は単値 (inverted / flat / protruding / puffy) なので、
-- **陥没した乳首 + 盛り上がった乳輪**が書けなかった。指摘のとおり両立する。
-- しかも `puffy` の説明自体が「**乳輪ごと**盛り上がっている」と書いてあり、
-- **乳輪の事実を乳首の属性に置いていた**ことが説明から読み取れる状態だった。
--
-- **タグ空間も部位を取り違えている**: `puffy_nipples` 68,302 に対し
-- `puffy_areolae` は **0 件**。盛り上がっているのは乳輪なのに乳首の名前が付いている。
-- ここは所見側が正しく置き直せる。
--
-- `nipple_state='puffy'` は**所見 0 件**なので削除する (使用中なら残して非推奨にするが、
-- 誰も書いていないので消してよい — 0033 で使用数 0 の pose を消したのと同じ手順)。
--
-- ══════════════════════════════════════════════════════════════════════════
-- 2. 身につけているものの「形」と「数」
-- ══════════════════════════════════════════════════════════════════════════
-- 実測: nipple_rings 6,653 / nipple_bar 5,027 / hoop_earrings 12,826 /
-- stud_earrings 6,028 / barbell 295 / industrial_piercing 2,451 /
-- multiple_piercings 223。**タグ空間はここでも「部位 × 形」の合成語**で持っている。
-- 0045 で部位を軸にしたので、形も軸にすれば `piercing` 1 語 + 部位 + 形で全部書ける。
--
-- * **形は `object_variant`** — ring / stud / bar / hoop / chain。object ごとに表を
--   分けない (分けると earrings の ring と piercing の ring が別語になる)。
-- * **数は `item_count`** — その部位にいくつ。NULL は「言っていない」。
--   0 を許さない (「無い」は所見を書かないことで表す — 非捏造の既定)。
--
-- **解決形にも運ぶ。** 0045 で object の分岐を足したばかりなので、ここで列だけ足して
-- resolver を直さないと同じ乖離をまた作る。
--
-- ── 運用手順 ──
--   * 単一 transaction。属性 → 値 → 表 → 列 + FK/CHECK → 説明。冪等。
--   * ロールバック: 追加した列と表を DROP (所見が付いていれば消せない)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- =====================================================================
-- 1. 乳輪の隆起
-- =====================================================================
INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('areola_projection', '乳輪の隆起', 'appearance', 'categorical', 'region', 'areola')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('areola_projection','flat'), ('areola_projection','puffy'), ('areola_projection','conical')
ON CONFLICT DO NOTHING;

UPDATE attribute SET description =
  '**乳輪が盛り上がっているか**。乳首の状態 (nipple_state) とは**別の部位の別の事実**で、'
  '**陥没した乳首 + 盛り上がった乳輪**が同時に成り立つ (単値の nipple_state に puffy を'
  '置いていたときは書けなかった)。'
  '**広さは areola_size** — 広いことと盛り上がっていることは別。'
  'なお flat tag は puffy_nipples 68,302 に対し puffy_areolae 0 件で、'
  '**盛り上がっているのは乳輪なのに乳首の名前が付いている**',
  display_order = 2
WHERE slug = 'areola_projection';

UPDATE attribute_value SET description = v.d, display_order = v.o FROM (VALUES
  ('areola_projection','flat',    '盛り上がっていない',                            1),
  ('areola_projection','puffy',   '乳輪ごとドーム状に盛り上がっている',            2),
  ('areola_projection','conical', '円錐状に強く突き出している',                    3)
) AS v(a,s,d,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

-- nipple_state から puffy を外す (**所見 0 件を確かめてから**)
DO $$
DECLARE n integer;
BEGIN
  SELECT count(*) INTO n FROM obs_attribute
   WHERE attribute_slug = 'nipple_state' AND value_slug = 'puffy';
  IF n > 0 THEN
    RAISE EXCEPTION 'nipple_state=puffy に所見が % 件ある — 消さず非推奨にすること', n;
  END IF;
END $$;
DELETE FROM attribute_value WHERE attribute_slug='nipple_state' AND value_slug='puffy';

UPDATE attribute SET description = coalesce(description,'') ||
  '。**乳輪が盛り上がっているかは areola_projection** (0046) — 別の部位の別の事実で、'
  '陥没した乳首 + 盛り上がった乳輪が同時に成り立つ'
WHERE slug = 'nipple_state' AND coalesce(description,'') NOT LIKE '%areola_projection%';

-- =====================================================================
-- 2. 身につけているものの「形」
-- =====================================================================
CREATE TABLE IF NOT EXISTS object_variant (
  slug          text PRIMARY KEY,
  display_name  text NOT NULL,
  description   text
);
COMMENT ON TABLE object_variant IS
  '身につけているものの**形**。ring / stud / bar / hoop / chain。'
  '**object ごとに表を分けない** — 分けると earrings の ring と piercing の ring が'
  '別語になる。**部位は obs_object.region_slug** (0045) で、形とは独立';

INSERT INTO object_variant(slug, display_name, description) VALUES
  ('ring',  'リング',   '輪。nipple_rings / hoop_earrings と呼ばれる形'),
  ('stud',  'スタッド', '玉・粒状。stud_earrings と呼ばれる形'),
  ('bar',   'バー',     '棒状。barbell / nipple_bar と呼ばれる形'),
  ('chain', 'チェーン', '鎖でつながっている'),
  ('other', 'その他',   '上のどれでもないと判るが名前が決まらない。**判らないときに ring と書かせないための値**')
ON CONFLICT (slug) DO NOTHING;

ALTER TABLE obs_object ADD COLUMN IF NOT EXISTS variant_slug text;
ALTER TABLE obs_object DROP CONSTRAINT IF EXISTS obs_object_variant_fkey;
ALTER TABLE obs_object ADD CONSTRAINT obs_object_variant_fkey
    FOREIGN KEY (variant_slug) REFERENCES object_variant(slug) NOT VALID;
ALTER TABLE obs_object VALIDATE CONSTRAINT obs_object_variant_fkey;

COMMENT ON COLUMN obs_object.variant_slug IS
  'その物の**形** (ring / stud / bar / hoop 相当)。**部位 (region_slug) とは独立** — '
  'flat tag は nipple_rings / nipple_bar / hoop_earrings のように「部位 × 形」の'
  '合成語で持っているが、ここは piercing 1 語 + 部位 + 形で書く。NULL は「言っていない」';

-- =====================================================================
-- 3. 数
-- =====================================================================
ALTER TABLE obs_object ADD COLUMN IF NOT EXISTS item_count integer;
ALTER TABLE obs_object DROP CONSTRAINT IF EXISTS ck_obs_object_item_count_positive;
ALTER TABLE obs_object ADD CONSTRAINT ck_obs_object_item_count_positive
    CHECK (item_count IS NULL OR item_count > 0) NOT VALID;
ALTER TABLE obs_object VALIDATE CONSTRAINT ck_obs_object_item_count_positive;

COMMENT ON COLUMN obs_object.item_count IS
  'その部位にいくつ付いているか。**NULL は「言っていない」**。'
  '**0 は許さない** — 「無い」は所見を書かないことで表す (非捏造の既定)。'
  'flat tag の multiple_piercings (223) のような「複数」だけの語を作らず数で持つ';

-- =====================================================================
-- 4. 解決形にも運ぶ (0045 の直後に同じ乖離を作らない)
-- =====================================================================
ALTER TABLE resolved_facet ADD COLUMN IF NOT EXISTS object_variant_slug text;
ALTER TABLE resolved_facet DROP CONSTRAINT IF EXISTS resolved_facet_object_variant_fkey;
ALTER TABLE resolved_facet ADD CONSTRAINT resolved_facet_object_variant_fkey
    FOREIGN KEY (object_variant_slug) REFERENCES object_variant(slug) NOT VALID;
ALTER TABLE resolved_facet VALIDATE CONSTRAINT resolved_facet_object_variant_fkey;

ALTER TABLE resolved_facet ADD COLUMN IF NOT EXISTS object_count integer;

COMMENT ON COLUMN resolved_facet.object_variant_slug IS
  '身につけているものの形 (obs_object.variant_slug をそのまま運ぶ)';
COMMENT ON COLUMN resolved_facet.object_count IS
  'その部位にいくつ (obs_object.item_count をそのまま運ぶ)';

COMMIT;
