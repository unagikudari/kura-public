-- Migration 0049 — 包皮がどこまで剥けているかを書けるようにする
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘:「phimosis はどう表現する」
--
-- ── region はあるが軸が無かった ──
-- `foreskin` は既に body_region にある (penis の子。glans / frenulum / penile_shaft /
-- bulbus_glandis も同様)。しかし **被覆の程度を言う属性が無い**ので、
-- 「包皮がある」とは書けても「**亀頭が覆われている**」が書けなかった。
-- penis / glans に付く属性は penis_size / erection_state / ejaculation_state /
-- penis_morphology だけ。
--
-- 実測 (production kura): phimosis 15,049 / foreskin 30,124。
-- retracted_foreskin / uncut_penis / circumcised は **0 件** — flat tag 側は
-- 「包茎か否か」の 1 語しか持っておらず、**程度を持たない**。
--
-- ── `phimosis` を値にしない (ここが要点) ──
-- 真性包茎 = **剥けない**という性質で、**1 枚の静止画からは観察できない**。
-- 観察できるのは「**今どこまで覆われているか**」だけ。
-- 0034 で「開いている状態 (dilation)」と「手で広げる行為 (spreading)」を分けたのと
-- 同じ線で、**診断と観察を分ける**。
-- 「覆われている」所見が積み上がれば phimosis は**後から導ける** (導けるものは保存しない
--  — orientation を保存しなかった 0019 と同じ判断)。
--
-- ── どの部位に付けるか ──
-- `foreskin` に付ける。「包皮がどこまで下がっているか」は包皮の状態であって
-- 亀頭の状態ではない。**亀頭が見えるかどうかは、この値から導ける**ので
-- glans 側に別の属性を作らない (二重の真実を避ける)。
--
-- ── Q16 との関係 ──
-- smegma は包皮の下に溜まるので、**被覆の程度と付着物は一緒に問える**ようになる。
-- flat tag は smegma 201,092 / phimosis 15,049 を別々の語で持つだけで、
-- 「覆われている状態で付着物があるか」を問えない。
--
-- ── 運用手順 ──
--   * 単一 transaction。属性 → 値 → 説明 → 隣の語に境界。冪等。
--   * ロールバック: 追加した attribute / attribute_value を DELETE。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('foreskin_retraction', '包皮の被り具合', 'state', 'categorical', 'region', 'foreskin')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('foreskin_retraction','covering_glans'),
  ('foreskin_retraction','partially_retracted'),
  ('foreskin_retraction','fully_retracted')
ON CONFLICT DO NOTHING;

UPDATE attribute SET description =
  '包皮が**どこまで下がっているか**。covering_glans / partially_retracted / fully_retracted。'
  '**`phimosis` は値に入れない** — 真性包茎は「剥けない」という性質で、'
  '**1 枚の静止画からは観察できない**。観察できるのは「今どこまで覆われているか」だけで、'
  '診断は所見が積み上がれば後から導ける (導けるものは保存しない、0019 と同じ判断)。'
  '**亀頭が見えるかはこの値から導ける**ので glans 側に別の属性を作らない。'
  'flat tag は phimosis 15,049 / foreskin 30,124 の 2 語しか持たず**程度を持たない** '
  '(retracted_foreskin / uncut_penis / circumcised はいずれも 0 件)。'
  'なお smegma は包皮の下に溜まるので、**被覆の程度と付着物を一緒に問える**ようになる',
  display_order = 1
WHERE slug = 'foreskin_retraction';

UPDATE attribute_value SET description = v.d, display_order = v.o FROM (VALUES
  ('foreskin_retraction','covering_glans',
   '亀頭が包皮に覆われている。**「剥けない」とは言っていない** — 見えているのは今の状態だけ', 1),
  ('foreskin_retraction','partially_retracted',
   '一部が下がり亀頭の一部が出ている',                                                        2),
  ('foreskin_retraction','fully_retracted',
   '完全に下がり亀頭が全部出ている',                                                          3)
) AS v(a,s,d,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

-- 隣の語に境界を書く
UPDATE attribute SET description = coalesce(description,'') ||
  '。**包皮の被り具合は別軸** (foreskin_retraction、0049) — 勃起していても覆われたままのことがある'
WHERE slug = 'erection_state' AND coalesce(description,'') NOT LIKE '%foreskin_retraction%';

UPDATE body_region SET description = coalesce(description,'') ||
  '。**どこまで下がっているかは属性 foreskin_retraction** (0049)'
WHERE slug = 'foreskin' AND coalesce(description,'') NOT LIKE '%foreskin_retraction%';

COMMIT;
