-- Migration 0051 — 排便を書けるようにする
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘:「これは排便もそう」
-- (0050 の排尿と同じ話 — 部位を挙げることは行為を意味しない)。
--
-- ── 排出の四番目 ──
--   * `ejaculation_state`  (region=penis)            — 0013
--   * `squirting_state`    (region=vulva)            — 0017
--   * `urination_state`    (region=urethral_opening) — 0050
--   * **排便 — 無い**
-- `anus` は region として存在し、`dilation` (0034) も付くのに、
-- **排出の軸だけ無かった**。
--
-- ── 部位を挙げることは行為を意味しない ──
-- 「肛門が見えている」は見えている部位の話。部位で行為を代用すると
-- **見えているだけの画像が全部「排便」になる**。
-- `surface_substance='scat'` (付着した便) とも別 — あれは**結果**で、
-- 出ている最中かは言わない。
--
-- ── `dilation` とも別 ──
-- 0034 の `dilation` は**開き具合**で、開いていても出ていないことはある。
-- 逆に出ている最中は開いているので同時に成り立つ。1 語に混ぜない。
--
-- ── 値は既存 3 つと揃える ──
-- not_defecating / defecating / post_defecation。**値空間を揃えるのは意図的** —
-- 4 つの排出が同じ形なら注釈者が毎回考え直さずに済む。
-- `not_defecating` は「**見たが出ていない**」で、NULL (言っていない) と分ける。
--
-- ── 運用手順 ──
--   * 単一 transaction。属性 → 値 → 説明 → 隣の語に境界。冪等。
--   * ロールバック: 追加した attribute / attribute_value を DELETE。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('defecation_state', '排便の状態', 'state', 'categorical', 'region', 'anus')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('defecation_state','not_defecating'),
  ('defecation_state','defecating'),
  ('defecation_state','post_defecation')
ON CONFLICT DO NOTHING;

UPDATE attribute SET description =
  '排便の状態。not_defecating / defecating / post_defecation。'
  '**部位を挙げることは行為を意味しない** — 「肛門が見えている」は見えている部位の話で、'
  '排便しているかは別の事実。部位で行為を代用すると'
  '**見えているだけの画像が全部「排便」になる**。'
  '**付着した便は surface_substance=scat** — あれは結果で、出ている最中かは言わない。'
  '**開き具合は dilation** (0034) — 開いていても出ていないことがあり、'
  '出ている最中は開いているので同時に成り立つ。1 語に混ぜない。'
  '`ejaculation_state` / `squirting_state` / `urination_state` と**値空間を揃えてある**。'
  '**NULL は「言っていない」**、`not_defecating` は「見たが出ていない」',
  display_order = 2
WHERE slug = 'defecation_state';

UPDATE attribute_value SET description = v.d, display_order = v.o FROM (VALUES
  ('defecation_state','not_defecating',  '出ていない (**見た上で**そうだと言う)',   1),
  ('defecation_state','defecating',      '排便中。出ている最中',                    2),
  ('defecation_state','post_defecation', '出し終えた直後。付着や汚れが残っている',  3)
) AS v(a,s,d,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

UPDATE attribute SET description = coalesce(description,'') ||
  '。**排便は defecation_state** (0051) — こちらは付着している物の話'
WHERE slug = 'surface_substance' AND coalesce(description,'') NOT LIKE '%defecation_state%';

UPDATE attribute SET description = coalesce(description,'') ||
  '。**出ている最中かは別軸** (defecation_state / urination_state) — '
  '開いていても出ていないことがある'
WHERE slug = 'dilation' AND coalesce(description,'') NOT LIKE '%defecation_state%';

UPDATE body_region SET description = coalesce(description,'') ||
  '。**この部位を挙げることは排便を意味しない** — 排便しているかは属性 '
  'defecation_state (0051) で書く'
WHERE slug = 'anus' AND coalesce(description,'') NOT LIKE '%defecation_state%';

COMMIT;
