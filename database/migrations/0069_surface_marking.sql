-- Migration 0069 — 皮膚の上に付いた「図像」を書けるようにする
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘 (post 606981):
-- 「**落書きがある状態**を表現できない」
--
-- ── 印を言う軸が丸ごと無かった ──
-- 体表に付いているものを言う語は `surface_substance` (体液・汚れ・泡) だけで、
-- **文字・絵・入れ墨・口紅の跡** を書く手段が 1 つも無かった
-- (attribute / attribute_value / object_type のどこを探しても tattoo も writing も 0 件)。
-- 落書きだけの話ではない — 実測 (production kura):
-- tattoo 20,600 / lipstick_mark 13,702 (+ on_penis 17,383) / body_writing 6,714 /
-- graffiti 2,028 / pubic_tattoo 2,486 / bodypaint 399。
--
-- ── surface_substance と何が違うか (境界) ──
--   surface_substance — **付着している物質**。精液・汗・泥・泡。形を持たない
--   surface_marking   — **表面に付いた図像**。文字・絵・入れ墨・口紅の跡。**形が事実**
-- 口紅の跡は「口紅という物質が付いている」とも言えるが、**見えているのは唇の形**で、
-- 量ではない。物質として書くと `lipstick_mark_on_penis` 17,383 が
-- 「何かが付いている」に潰れる。
--
-- ── 部位ごとに付く (0065 の any_region) ──
-- flat tag は arm_tattoo 3,854 / shoulder_tattoo 3,500 / pubic_tattoo 2,486 /
-- leg_tattoo 1,938 / chest_tattoo 1,477 と **部位ごとの合成語**を作っている。
-- ここでは `applies_kind='any_region'` (0065) にして、**部位は所見ごとに指定する** —
-- 部位 × 種類の組合せ爆発を作らない (0045 の piercing と同じ)。
--
-- ── 永続かどうかは値が決めるので別軸にしない ──
-- 入れ墨は消えず、落書きは消える。**その差は値そのものが持っている**ので
-- `permanence` のような軸を足さない (0060 で「種族が段階を決めるなら書かない」と
-- したのと同じ判断。導けるものは保存しない)。
--
-- ── 書いていない内容 (既知の欠落) ──
-- **何と書いてあるか (文字列そのもの) は記録しない。** post 606981 の「正正」は
-- `writing` としか書けない。文字を読む軸は別の問題 (OCR) で、今の語彙の延長には無い。
--
-- ロールバック: 追加した attribute / attribute_value を DELETE。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('surface_marking', '表面の図像', 'appearance', 'categorical', 'any_region', 'any')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('surface_marking','writing'),
  ('surface_marking','drawing'),
  ('surface_marking','tattoo'),
  ('surface_marking','body_paint'),
  ('surface_marking','lipstick_mark'),
  ('surface_marking','other')
ON CONFLICT DO NOTHING;

UPDATE attribute SET description =
  '皮膚の上に付いた**図像**。writing / drawing / tattoo / body_paint / lipstick_mark / other。'
  '**付着物 (surface_substance) との違いは形を持つかどうか** — あちらは精液・汗・泥・泡のような'
  '**物質**で量の話、こちらは**形そのものが事実**。'
  '口紅の跡は「口紅が付いている」とも言えるが、見えているのは唇の形で量ではない '
  '(lipstick_mark 13,702 / on_penis 17,383 が「何かが付いている」に潰れてしまう)。'
  '**どの部位に付いているかは所見ごとに指定する** (any_region、0065) — '
  'flat tag が arm_tattoo 3,854 / shoulder_tattoo 3,500 / pubic_tattoo 2,486 と'
  '**部位ごとの合成語**を作っているのに対し、ここでは部位 × 種類の組合せを作らない。'
  '**永続かどうかは値が持っている**ので別軸にしない (入れ墨は消えず落書きは消える)。'
  '**何と書いてあるかは記録しない** — 文字を読む軸は別の問題 (既知の欠落)',
  display_order = 2
WHERE slug = 'surface_marking';

UPDATE attribute_value SET description = v.d, display_order = v.o FROM (VALUES
  ('surface_marking','writing',
   '文字が書かれている。落書き・「正」の字・書き込み (body_writing 6,714 / graffiti 2,028)。'
   '**何と書いてあるかは書かない**', 1),
  ('surface_marking','drawing',
   '絵が描かれている。文字ではない図像',                                         2),
  ('surface_marking','tattoo',
   '入れ墨。**消えない** — 落書きとの差は値そのものが持っているので別軸にしない '
   '(tattoo 20,600)',                                                            3),
  ('surface_marking','body_paint',
   '体に塗られた塗料。広い面を覆う (bodypaint 399)',                             4),
  ('surface_marking','lipstick_mark',
   '口紅の跡。**唇の形が残っている**のが事実で、量ではない '
   '(lipstick_mark 13,702 / on_penis 17,383)',                                   5),
  ('surface_marking','other',
   'どれにも当てはまらない図像。**判らないときにここへ入れる** — '
   '当てずっぽうで writing や tattoo と書かせないための値 (0045 と同じ)',        6)
) AS v(a,s,d,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

UPDATE attribute SET description = coalesce(description,'') ||
  '。**形を持つ図像は surface_marking** (0069) — 文字・絵・入れ墨・口紅の跡。'
  'こちらは物質で、量の話'
WHERE slug = 'surface_substance' AND coalesce(description,'') NOT LIKE '%surface_marking%';

COMMIT;
