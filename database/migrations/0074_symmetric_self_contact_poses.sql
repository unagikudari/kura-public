-- Migration 0074 — 両手が揃った確定的な自己接触は pose にする
--
-- plan kura-1.0-implementation、2026-07-31 の user 判断:
-- 「**両手が揃ってその動作をしている確定的なポーズの時だけ**それを使えばいいだろ」
--
-- ── 0033 の線は行き過ぎだった ──
-- 0033 は「自分の部位どうしの接触は relation で書く。pose にすると**どちらの手か**・
-- 何に触れているかが言えない」として `pose_category=self_contact` を非推奨にした。
-- 片手の場合はそのとおり — `hand_on_own_hip` は右手か左手か言えない。
-- **だが両手が揃っている形では、左右は問いにならない。** laterality が足す情報が
-- 無いので、pose にしても失われるものが無い。
-- (0060 で「種族が段階を決めるなら段階を書かない」としたのと同じ形 —
--  **語がすでに決めているものを別軸で持たない**。)
--
-- ── 境界 ──
--   両手が揃っている → **pose** (hands_on_hips / hugging_own_legs …)
--   片手だけ         → **relation touching** (hand → region)。どちらの手かが言える
--   左右で違うことを → **relation 2 本**。pose では書けない
--
-- ── 部位ごとに語が増えるのは承知の上 ──
-- 実測 (production kura) の hands_on_own_* は 12 語:
-- ass 4,446 / knees 2,538 / hips 1,553 / chest 1,259 / thighs 878 / legs 874 /
-- face 645 / stomach 640 / cheeks 393 / breasts 242 / head 122 / chin 20。
-- **user の判断で、この範囲の増加は受け入れる。** 片手側 (hand_on_own_* は 20 語)
-- まで pose にすると 32 語になるので、そちらは relation のまま。
--
-- ── 二重の真実を作らない ──
-- 0073 で入れた型 `hugging own legs` は **関係 2 本**を書いていたが、
-- 同じ事実を pose でも書けるようになると 2 通りになる。**型は pose を書くほうへ移す**。
--
-- ロールバック: 追加した pose を DELETE、型の展開を関係へ戻す。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

UPDATE pose_category SET description =
  '自分の部位どうしの接触。**両手が揃って同じことをしている確定的な形だけ**を'
  'ここに入れる (0074、user 判断 2026-07-31) — 左右が決まっているので laterality が'
  '足す情報を持たず、pose にしても失われるものが無い。'
  '**片手なら relation touching** (hand → region) — どちらの手かが言えなくなるため。'
  '**左右で違うことをしているなら relation 2 本** — pose では書けない。'
  '(0033 は片手の場合を根拠に全部を非推奨にしていたが、両手の場合には当てはまらなかった)'
WHERE slug = 'self_contact';

INSERT INTO pose(slug, display_name, scope, category) VALUES
  ('hugging_own_legs','両腕で脚を抱えている','arms','self_contact'),
  ('hands_on_hips',   '両手を腰に当てている','hands','self_contact'),
  ('hands_on_knees',  '両手を膝に置いている','hands','self_contact'),
  ('hands_on_chest',  '両手を胸に当てている','hands','self_contact'),
  ('hands_on_breasts','両手を乳房に当てている','hands','self_contact'),
  ('hands_on_stomach','両手を腹に当てている','hands','self_contact'),
  ('hands_on_thighs', '両手を太ももに置いている','hands','self_contact'),
  ('hands_on_buttocks','両手を尻に当てている','hands','self_contact'),
  ('hands_on_face',   '両手を顔に当てている','hands','self_contact'),
  ('hands_on_cheeks', '両手を頬に当てている','hands','self_contact'),
  ('hands_on_head',   '両手を頭に当てている','hands','self_contact')
ON CONFLICT (slug) DO NOTHING;

UPDATE pose SET description = v.d FROM (VALUES
  ('hugging_own_legs','**両腕で**自分の脚を抱え込んでいる (hugging_own_legs 393)。'
   '**片腕だけなら relation holding** (arm → thigh) — どちらの腕かが言えるため。'
   '脚の開き (spread_legs) や膝の位置 (knees_to_chest) は**別の事実**'),
  ('hands_on_hips',   '**両手を**腰に当てている (hands_on_own_hips 1,553)。'
                      '片手なら relation touching (hand → waist)'),
  ('hands_on_knees',  '**両手を**膝に置いている (hands_on_own_knees 2,538)'),
  ('hands_on_chest',  '**両手を**胸に当てている (hands_on_own_chest 1,259)。'
                      '**乳房そのものに当てているなら hands_on_breasts**'),
  ('hands_on_breasts','**両手を**乳房に当てている (hands_on_own_breasts 242)'),
  ('hands_on_stomach','**両手を**腹に当てている (hands_on_own_stomach 640)'),
  ('hands_on_thighs', '**両手を**太ももに置いている (hands_on_own_thighs 878)'),
  ('hands_on_buttocks','**両手を**尻に当てている (hands_on_own_ass 4,446)'),
  ('hands_on_face',   '**両手を**顔に当てている (hands_on_own_face 645)。'
                      '**頬だけなら hands_on_cheeks**'),
  ('hands_on_cheeks', '**両手を**頬に当てている (hands_on_own_cheeks 393)'),
  ('hands_on_head',   '**両手を**頭に当てている (hands_on_own_head 122)。'
                      '**頭の後ろで組むなら arms_behind_head**')
) AS v(s,d) WHERE pose.slug = v.s;

-- 型は pose を書くほうへ移す (同じ事実を 2 通りで書かない)
DELETE FROM phrase_macro_relation WHERE macro_slug = 'hugging_own_legs';
INSERT INTO phrase_macro_pose(macro_slug, subject_ref, pose_slug) VALUES
  ('hugging_own_legs','$s','hugging_own_legs')
ON CONFLICT DO NOTHING;
UPDATE phrase_macro SET description =
  '$s が**両腕で自分の脚を抱え込んでいる**。**pose として書く** (0074) — '
  '両手が揃った確定的な形なので左右が問いにならず、pose にしても失われるものが無い。'
  '**片腕だけなら relation holding** (arm → thigh)。'
  '**手で掴んで開く形は `holding own legs`** (grasping + spread_legs)'
WHERE slug = 'hugging_own_legs';

-- 片手側の語に境界を書く
UPDATE pose SET description = coalesce(description,'') ||
  '。**両手なら hands_on_hips** (0074) — こちらは片手で、新しく書くなら relation touching'
WHERE slug = 'hand_on_own_hip' AND coalesce(description,'') NOT LIKE '%hands_on_hips%';

COMMIT;
