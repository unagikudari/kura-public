-- Migration 0009 — 顔・頭の pose と性交時の体の構え
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-28:
-- 「ポーズは性交状態のポーズも必要だと思う。ポーズについて腕の動き、顔の動き、
--   足の動き、全体はカテゴリを分けて特出ししてほしい」。
--
-- ── 分かったこと ──
-- pose_scope には whole_body / upper_body / lower_body / legs / arms / hands / head が
-- あるのに、**head scope の pose が 1 件も無かった** (顔・頭の動きが書けない)。
-- また性交時の体の構えのうち、合成でないもの (straddling / leg_lock) が欠けていた。
--
-- ── 足すもの / 足さないもの ──
-- **足す = 体の構えそのもの**:
--   straddling (84,143) / leg_lock (7,653) / spreading_own_labia (33,402 の spread_pussy)
--   head 系: head_tilt / head_back / head_down / head_turned / looking_back (57,145)
-- **足さない = 合成語**:
--   missionary (49,714) / cowgirl_position (50,184) / doggystyle / amazon_position 等。
--   これらは「体の構え + 誰が誰に + 相対配置」の合成で、独立した語ではない。
--   語として足すと **B1 の指摘 (行為ポジションが複数出すぎ・定義が定まってない) を
--   そのまま輸入する**ことになる。実測でも amazon+cowgirl の 99.9% が girl_on_top と
--   straddling を同時に持っており、1 つの配置を複数の独立 boolean で言い直していた。
--   代わりに**分解の仕方を description に書く** — 語を増やさずに書けるようにする。
--
-- gaze (looking_at_viewer 284,838 / looking_up / looking_down) は **pose ではなく
-- 既存の attribute `gaze_direction`** (class=eye) の領域なので pose に足さない。
-- 「顔の向き (head_turned)」と「視線の向き (gaze_direction)」は別物で、
-- 顔は正面のまま目だけ逸らす場合があるため分けて正しい。
--
-- ── 運用手順 ──
--   * 単一 transaction。語彙表 (pose_category) → pose → description の順。
--   * 冪等 (ON CONFLICT DO NOTHING / 同値 UPDATE)。
--   * ロールバック: 追加した pose を DELETE (観測が参照していれば FK が拒否する)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- 性交時の体の構えを識別できるようにする (scope とは別軸の分類)
INSERT INTO pose_category(slug, description) VALUES
  ('sexual_configuration', '性交時の体の構え。行為の名前 (騎乗位 等) ではなく体の形そのもの')
ON CONFLICT (slug) DO NOTHING;

-- ── 顔・頭の動き (head scope が空だった) ──
INSERT INTO pose(slug, display_name, scope, category) VALUES
  ('head_tilt',    'head tilt',    'head', 'posture'),
  ('head_back',    'head back',    'head', 'posture'),
  ('head_down',    'head down',    'head', 'posture'),
  ('head_turned',  'head turned',  'head', 'posture'),
  ('looking_back', 'looking back', 'head', 'posture')
ON CONFLICT (slug) DO NOTHING;

-- ── 性交時の体の構え (合成語ではないもの) ──
INSERT INTO pose(slug, display_name, scope, category) VALUES
  ('straddling',          'straddling',          'whole_body', 'sexual_configuration'),
  ('leg_lock',            'leg lock',            'legs',       'sexual_configuration'),
  ('spreading_own_labia', 'spreading own labia', 'hands',      'self_contact')
ON CONFLICT (slug) DO NOTHING;

UPDATE pose SET description = v.d FROM (VALUES
  ('head_tilt',    '首をかしげている (頭が左右に傾く)'),
  ('head_back',    '頭を後ろへ反らせている (顎が上がる)'),
  ('head_down',    'うつむいている (顎が引かれ視線が下を向く姿勢)'),
  ('head_turned',  '顔を横へ向けている。**視線の向きは別** — 目だけ逸らす場合は属性 gaze_direction で書く'),
  ('looking_back', '振り返っている。頭と上体をひねって後方を見る姿勢'),
  ('straddling',   'またがっている。対象を挟むように両脚を左右へ置く体の構え。'
                   '**騎乗位はこれ単独では表さない** — straddling + relation inserting_into + '
                   'relation above (誰が上か) の組み合わせで書く'),
  ('leg_lock',     '相手の胴や腰に脚を絡めている構え。'
                   '**誰に絡めているかは relation (touching / pressing_against) 側で書く**'),
  ('spreading_own_labia', '自分の小陰唇を指で左右に開いている')
) AS v(s,d) WHERE pose.slug = v.s;

-- 既存の合成しやすい語にも「どう組むか」を書き足す (語を増やさずに書けるようにする)
UPDATE pose SET description = description ||
  '。**後背位はこれ + relation behind + relation inserting_into で書く** (doggystyle という語は置かない — '
  '1 つの配置を複数の独立 boolean で言い直すと B1 の問題が再来するため)'
WHERE slug = 'all_fours' AND description NOT LIKE '%後背位%';

UPDATE pose SET description = description ||
  '。正常位はこれ + relation above (相手が上) + relation inserting_into で書く'
WHERE slug = 'on_back' AND description NOT LIKE '%正常位%';

COMMIT;
