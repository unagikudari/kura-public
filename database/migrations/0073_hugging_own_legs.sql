-- Migration 0073 — 「両腕で自分の脚を抱えている」を型で出せるようにする
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘:
-- 「**両腕で自分の足を抱えている**ポーズを出したい」
--
-- ── これは pose ではなく関係 ──
-- 抱えるのは**接触**なので、この ontology では関係で書く。
-- `hand_on_own_hip` の説明に既に「新しく書くなら relation touching を使う —
-- **どちらの手かが言えるため**」と書いてあり、0068 でも同じ線を引いた。
-- pose にすると「どちらの腕が」「何を抱えているか」が語の中に埋まり、
-- hugging_own_legs / hugging_own_knees / … と合成語が増える。
--
-- ── 既にある型と何が違うか ──
--   `holding own legs` (0064) — **手で太ももを掴んで開く**。grasping (hand → thigh) +
--                               spread_legs。post 555215 のような形
--   `hugging own legs` (これ) — **腕全体で抱え込む**。holding (arm → thigh)。
--                               体育座りのように脚を体に引き寄せる形
-- 語の選び分けは既存の定義どおり — grasping は手指で握る、
-- **holding は腕全体で抱える** (0071 で restraining と分けたときに再確認した線)。
-- **脚の開き / 膝の位置は足さない** — 抱えているかと、開いているか (spread_legs) /
-- 膝を胸に寄せているか (knees_to_chest) は**別の事実**で、抱えた結果とは限らない。
-- 実測 hugging_own_legs 393 / knees_to_chest 3,162。
--
-- ── 既存の型の説明を直す ──
-- `holding own legs` は名前だけ見ると今回の形とも読めるので、
-- **手で掴んで開く方**だと説明に明記する。
--
-- ロールバック: 追加した型を DELETE。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO phrase_macro(slug, phrase, aliases, description, tag_name, display_order) VALUES
 ('hugging_own_legs','hugging own legs',
  ARRAY['両腕で脚を抱える','脚を抱え込む','体育座り'],
  '$s が**両腕で自分の脚を抱え込んでいる** (holding: arm → thigh、左右 2 本)。'
  '**pose ではなく関係** — 抱えるのは接触で、pose にすると「どちらの腕が」「何を」が'
  '語の中に埋まる (hand_on_own_hip / 0068 と同じ線)。'
  '**手で掴んで開く形は `holding own legs`** — あちらは grasping (hand → thigh) + spread_legs。'
  '**脚の開きや膝の位置は足さない** — 抱えているかとは別の事実 (0073)',
  'hugging_own_legs', 41)
ON CONFLICT (slug) DO NOTHING;

INSERT INTO phrase_macro_relation
  (macro_slug, ord, relation_slug, subject_ref, subject_region, subject_laterality,
   object_ref, object_region, object_laterality) VALUES
 ('hugging_own_legs',1,'holding','$s','arm','left', '$s','thigh','left'),
 ('hugging_own_legs',2,'holding','$s','arm','right','$s','thigh','right')
ON CONFLICT DO NOTHING;

UPDATE phrase_macro SET description =
  '$s が**自分の手で自分の太ももを掴んで開いている** (grasping: hand → thigh、左右 2 本) + spread_legs。'
  '**flat tag の leg_grab 26,103 は誰の脚か言わない** — ここでは自己参照の関係で書く。'
  '**両腕で抱え込む形は `hugging own legs`** (0073) — あちらは holding (arm → thigh) で、'
  '脚の開きは含まない'
WHERE slug = 'holding_own_legs';

COMMIT;
