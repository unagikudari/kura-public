-- Migration 0032 — 体がカメラに対してどちらを向いているかを**人物ごとに**書けるようにする
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-29:
-- 「post 734882 みたいに向こう側を向いているのはどう書けばいいの」→ **書けなかった**。
--
-- ── 無かった軸: 体の向き ──
-- 既存で「向き」に触れるものは全部別のことを言っている:
--   * `scene_descriptor` の camera_angle — from_above / high_angle 等で **上下の角度だけ**。
--     水平の向き (正面 / 背面 / 横) が無い。しかも **scene 単位**なので人物ごとに言えない。
--   * `looking_back` / `head_turned` — **頭の向き**。体は向こう向きのまま顔だけこちら、
--     という場面があるので体の向きとは独立。
--   * `gaze_direction` — **視線**。顔の向きとも独立 (顔は正面のまま目だけ逸らす)。
-- つまり体・頭・視線の 3 層のうち、**体の層だけ無かった**。
--
-- ── なぜ scene_descriptor ではなく entity の属性か (ここが要点) ──
-- 実測: `facing_away` が付いた 9,618 post のうち **7,240 (75%) が複数人**。
-- flat tag はこの 7,240 件で **誰が向こうを向いているか言えていない**。
-- scene 単位の descriptor にすると同じ欠陥を輸入することになる。
-- 体の向きは **人物ごとの事実**なので `applies_kind='entity'`。
-- (`from_behind` 66,909 はカメラ位置と被写体の向きを混ぜた語で、分解すると
--  「カメラが後方」= camera_angle と「体が向こう向き」= この属性 になる)
--
-- ── 値 ──
-- カメラに対する体の正面の向き。front / three_quarter_front / profile /
-- three_quarter_back / back の 5 段。連続的な角度の離散化なので display_order で
-- 正面→背面の順に並べる (アルファベット順では意味が壊れる)。
-- **左右は分けない** — 「右向きの横顔」は左右の別であって向きの別ではなく、
-- 混ぜると 5 値が 8 値に増えて選ぶのが重くなる。必要なら別の軸で足す。
--
-- ── 運用手順 ──
--   * 単一 transaction。attribute → 値 → 説明 → 並び順。冪等。
--   * ロールバック: 追加した attribute / attribute_value を DELETE。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('body_orientation', 'body orientation', 'state', 'categorical', 'entity', 'person')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('body_orientation','front'), ('body_orientation','three_quarter_front'),
  ('body_orientation','profile'), ('body_orientation','three_quarter_back'),
  ('body_orientation','back')
ON CONFLICT DO NOTHING;

UPDATE attribute SET description =
  'カメラに対する**体**の向き。front / three_quarter_front / profile / '
  'three_quarter_back / back。**頭の向き (pose head_turned / looking_back) とも視線 '
  '(gaze_direction) とも独立** — 体は向こう向きのまま顔だけこちら、がある。'
  '**人物ごとの事実**なので scene_descriptor ではない — flat tag の facing_away は '
  '9,618 件中 7,240 件 (75%) が複数人で、誰が向こう向きか言えていない',
  display_order = 2
WHERE slug = 'body_orientation';

UPDATE attribute_value SET description = v.d, display_order = v.o FROM (VALUES
  ('body_orientation','front',              'こちらを向いている (体の正面がカメラ側)', 1),
  ('body_orientation','three_quarter_front','斜め前を向いている',                      2),
  ('body_orientation','profile',            '真横を向いている',                        3),
  ('body_orientation','three_quarter_back', '斜め後ろを向いている',                    4),
  ('body_orientation','back',               '向こうを向いている (背中がカメラ側)',      5)
) AS v(a,s,d,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

-- 混同しやすい隣の語に、どこが違うかを書き足す
UPDATE pose SET description = description ||
  '。**体の向きは別** — 体が向こう向きのまま振り返っているなら '
  'attribute body_orientation=back と併記する'
WHERE slug = 'looking_back' AND description NOT LIKE '%体の向きは別%';

UPDATE attribute SET description = description ||
  '。**体の向き (body_orientation) とは独立** — 体は向こう向きでも視線はこちら、がある'
WHERE slug = 'gaze_direction' AND description NOT LIKE '%body_orientation%';

COMMIT;
