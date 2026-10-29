-- Migration 0050 — 排尿を書けるようにする
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘 (post 454961):
-- 「peeing が表現できない。**peeing は尿道口の露出を意味しない**」
--
-- ── 三兄弟のうち 1 つだけ無かった ──
-- 排出の状態はこの ontology では**部位に付く属性**として持っている:
--   * `ejaculation_state` (region=penis) — not_ejaculating / precum / ejaculating / post_ejaculation
--   * `squirting_state`   (region=vulva) — not_squirting / squirting / post_squirting
--   * **排尿 — 無い**
-- `urethral_opening` は region として存在するのに、そこに付く排出の軸が無かった。
--
-- ── 部位を挙げることは行為を意味しない (user 指摘の核心) ──
-- 「尿道口が見えている」は**見えている部位**の話で、「排尿している」とは別の事実。
-- 部位で行為を代用すると、**見えているだけの画像が全部「排尿」になる**。
-- 同じ理由で `surface_substance='urine'` (付着した尿) とも別 — あれは**結果**で、
-- 流れが出ているかは言わない。乾いた跡だけが残っている画像もある。
--
-- 実測 (production kura): peeing 9,539 / pee 9,037。urination / wetting は 0 件。
--
-- ── 値は既存 2 つと揃える ──
-- not_urinating / urinating / post_urination。**値空間を揃えるのは意図的** —
-- 3 つの排出が同じ形なら、注釈者が毎回考え直さずに済む。
-- **`not_urinating` を置くのは「見たが出ていない」と「言っていない」(NULL) を
-- 分けるため** (非捏造の既定)。
--
-- ── 運用手順 ──
--   * 単一 transaction。属性 → 値 → 説明 → 隣の語に境界。冪等。
--   * ロールバック: 追加した attribute / attribute_value を DELETE。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('urination_state', '排尿の状態', 'state', 'categorical', 'region', 'urethral_opening')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('urination_state','not_urinating'),
  ('urination_state','urinating'),
  ('urination_state','post_urination')
ON CONFLICT DO NOTHING;

UPDATE attribute SET description =
  '排尿の状態。not_urinating / urinating / post_urination。'
  '**部位を挙げることは行為を意味しない** — 「尿道口が見えている」は見えている部位の話で、'
  '排尿しているかは別の事実 (user 指摘 2026-07-31)。部位で行為を代用すると'
  '**見えているだけの画像が全部「排尿」になる**。'
  '**付着した尿は surface_substance=urine** — あれは結果で、流れが出ているかは言わない '
  '(乾いた跡だけの画像もある)。'
  '`ejaculation_state` / `squirting_state` と**値空間を揃えてある** — '
  '3 つの排出が同じ形なら注釈者が毎回考え直さずに済む。'
  '**NULL は「言っていない」**、`not_urinating` は「見たが出ていない」',
  display_order = 1
WHERE slug = 'urination_state';

UPDATE attribute_value SET description = v.d, display_order = v.o FROM (VALUES
  ('urination_state','not_urinating',  '出ていない (**見た上で**そうだと言う)',       1),
  ('urination_state','urinating',      '排尿中。流れが出ている',                      2),
  ('urination_state','post_urination', '出し終えた直後。滴りや濡れが残っている',      3)
) AS v(a,s,d,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

-- 隣の語に境界を書く
UPDATE attribute SET description = coalesce(description,'') ||
  '。**排尿は urination_state** (0050) — こちらは付着している物の話で、'
  '流れが出ているかは言わない'
WHERE slug = 'surface_substance' AND coalesce(description,'') NOT LIKE '%urination_state%';

UPDATE body_region SET description = coalesce(description,'') ||
  '。**この部位を挙げることは排尿を意味しない** — 排尿しているかは属性 '
  'urination_state (0050) で書く'
WHERE slug = 'urethral_opening' AND coalesce(description,'') NOT LIKE '%urination_state%';

COMMIT;
