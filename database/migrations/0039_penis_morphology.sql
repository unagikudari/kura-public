-- Migration 0039 — 陰茎の形態 (馬型・犬型 等) を書けるようにする
--
-- plan kura-1.0-implementation、2026-07-30 の user 指摘:
-- 「studio_kolietry にあるようなタグで、futanari と horse_penis が同時にあった場合、
--   それが当人に所属するものなのか、当人以外に horse_penis があるのかがタグ空間では
--   わからない」
--
-- ── 指摘のうち「所属」は既に解けている。「形態」が書けなかった ──
-- observation は `subject_ref` に束縛されているので、person_1 の region に penis を
-- 書けばそれは person_1 の陰茎。**futanari という語も要らない** —
-- presentation=female の subject が penis region を持つことが futanari の定義そのもので、
-- 別に語を持てば二重の真実になる (orientation を保存しなかった 0019 と同じ)。
-- 一方 penis に付く属性は penis_size / erection_state / ejaculation_state だけで、
-- **形が馬か犬か人かを言う軸が無かった**。
--
-- 実測 (production kura): futanari horse_penis 1,676 post。うち solo 923 (55%) だが
-- **-solo が 753 (45%)** で、2 人以上いるためタグ空間は持ち主を言えない
-- (2girls 474 / multiple_penises 203 / disembodied_penis 49 = 誰のものでもない可能性)。
-- 語彙の需要: animal_penis 13,797 / horse_penis 10,380 / knotted_penis 4,979 /
-- dog_penis 3,982。
--
-- ── 3 軸に分ける (1 語に混ぜない) ──
-- (1) **形態は属性** `penis_morphology`。`horse_penis` を region にすると
--     「馬の陰茎の亀頭」が別語彙になり、glans / urethral_opening を全形態ぶん複製する。
-- (2) **瘤 (knot) は部位**。glans が penis の子 region であるのと同じ層。
--     実測で knotted_penis (4,979) > dog_penis (3,982) なので**犬形態に従属させられない** —
--     形態に畳むと「瘤のある人型」が書けなくなる。解剖学名 `bulbus_glandis` を slug に
--     し、俗称 (knot) は description から引く (VIO を v_line にしなかったのと同じ)。
-- (3) **veiny は表面の質感**で形態ではない。136,128 件あるが本 migration では触らない —
--     形態の値に veiny を混ぜると「血管の浮いた馬型」が書けなくなる。
--
-- ── 値の選び方 ──
-- human / equine / canine / feline / other。**タグ名をそのまま値にしない** —
-- horse_penis は「馬の」+「陰茎」の合成語で、部位は region が既に持っている。
-- `other` を置くのは、形態が人型でないと判るが種が決まらない場合に
-- 「人型」と書かせないため (非捏造)。NULL は「言っていない」。
--
-- ── 運用手順 ──
--   * 単一 transaction。region → class_map → attribute → 値 → 説明。冪等。
--   * ロールバック: 追加した attribute / attribute_value / body_region を DELETE
--     (観測が付いていれば消せない — その場合は retract が先)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- =====================================================================
-- 1. 瘤は部位 (形態に畳まない)
-- =====================================================================
INSERT INTO body_region(slug, display_name, parent_slug, laterality, primary_class, description) VALUES
  ('bulbus_glandis', 'bulbus glandis', 'penis', 'midline', 'external_genital',
   '陰茎基部の膨らみ (**俗に knot / 瘤**)。犬型に多いが**犬型に従属させない** — '
   '実測で knotted_penis (4,979) は dog_penis (3,982) より多く、'
   '人型で瘤があるものが存在する。形態 (penis_morphology) とは独立に書く')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO region_class_map(region_slug, class_slug) VALUES
  ('bulbus_glandis', 'external_genital')
ON CONFLICT DO NOTHING;

-- =====================================================================
-- 2. 形態は属性
-- =====================================================================
INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('penis_morphology', 'penis morphology', 'appearance', 'categorical', 'region', 'penis')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('penis_morphology','human'), ('penis_morphology','equine'),
  ('penis_morphology','canine'), ('penis_morphology','feline'),
  ('penis_morphology','other')
ON CONFLICT DO NOTHING;

UPDATE attribute SET description =
  '陰茎の形。human / equine / canine / feline / other。'
  '**持ち主は region が既に担っている** — 観測は subject_ref に束縛されるので、'
  'person_1 の penis に equine と書けばそれは person_1 の陰茎である。'
  'flat tag の futanari + horse_penis は 1,676 件中 753 件 (45%) が複数人で、'
  '**どちらの horse_penis か言えていない**。'
  '**瘤は別** (region bulbus_glandis) — 形態に畳むと「瘤のある人型」が書けない。'
  '**血管の浮き (veiny) も別軸** — あれは表面の質感で形ではない。'
  'NULL は「言っていない」',
  display_order = 2
WHERE slug = 'penis_morphology';

UPDATE attribute_value SET description = v.d, display_order = v.o FROM (VALUES
  ('penis_morphology','human',  '人型',                                             1),
  ('penis_morphology','equine', '馬型 (亀頭が開く、竿が長い)',                        2),
  ('penis_morphology','canine', '犬型。**瘤があるなら region bulbus_glandis も併記する**', 3),
  ('penis_morphology','feline', '猫型',                                             4),
  ('penis_morphology','other',  '人型でないことは判るが種が決まらない。'
                                '**判らないときに human と書かないための値**',        5)
) AS v(a,s,d,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

-- =====================================================================
-- 3. 隣の語に区別を書く
-- =====================================================================
UPDATE attribute SET description = description ||
  '。**形 (penis_morphology) とは別軸** — 馬型で小さいものも人型で大きいものもある'
WHERE slug = 'penis_size' AND description NOT LIKE '%penis_morphology%';

UPDATE body_region SET description = description ||
  '。**馬型では開く (flare)** が、それは形態 (penis_morphology=equine) の側で言う'
WHERE slug = 'glans' AND description NOT LIKE '%flare%';

COMMIT;
