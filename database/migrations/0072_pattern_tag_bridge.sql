-- Migration 0072 — 型を「入力の近道」から「検索の単位」と「従来タグとの橋」にする
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘 2 連:
-- 「そういった語句は**全体ポーズの関係で一発で引けるようにしてほしい**。
--   **右手左手で指定するのは限界がある**」
-- 「**従来のタグとの関係性にもつながる**」
--
-- ── 分解の代償が今度は「引けない」に出た ──
-- 0071 で羽交い締めを behind + restraining(arm→axilla、左右) の 3 本に分解した。
-- 記録としては正しいが、**引くときに 3 本を join して左右まで合わせる**必要が出る。
-- 入力の手間は 0064 の型で消したのに、**検索の手間は残っていた**。
--
-- ── 型は既に答えを持っている ──
-- `phrase_macro_relation` は「羽交い締めとは何の組み合わせか」を**既に書いてある**。
-- 同じ定義を**照合にも使えば**、語彙を増やさずに 1 語で引ける。
-- **導けるものは保存しない** (0019 / 0049 / 0060 と同じ線) — 羽交い締めという
-- 所見を作らず、型に当たるかどうかを**その都度導く**。
--
-- ── 左右は照合しない ──
-- user 指摘のとおり右手左手の指定には限界がある (見えていないことも、
-- 対称で区別に意味が無いことも多い)。**照合では laterality を見ない** —
-- 書いてあれば残るが、引くときの条件にはしない。
-- 型の region が NULL の行も**部位を問わない**として扱う。
--
-- ── 従来タグとの橋 (ここが plan の本題に効く) ──
-- `tag_name` を持たせると、型は**「この flat tag は primitive で言うとこれ」**という
-- 対応表になる。同じ画像集合に対して
--     flat tag が付いている集合  vs  型に当たる集合
-- を突き合わせられるので、**どちらが何を取りこぼすか**が測れる。
-- タグの意味を言葉で説明するのではなく、**分解した定義そのものが説明になる**。
--
-- ロールバック: 追加した列を DROP。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

ALTER TABLE phrase_macro ADD COLUMN IF NOT EXISTS tag_name text;
COMMENT ON COLUMN phrase_macro.tag_name IS
  'この型に対応する**従来の flat tag** (0072)。'
  '型は「その tag を primitive で言うとどうなるか」の定義でもあるので、'
  '**同じ画像集合を突き合わせて取りこぼしを測れる**。'
  'NULL は「対応する tag が無い / まだ書いていない」';

COMMENT ON TABLE phrase_macro IS
  '**人が書いた展開の型** (0064)。3 つの役目を持つ:'
  ' (1) 入力の近道 — 一文で複数のコントロールを埋める。'
  ' (2) **検索の単位** (0072) — 同じ定義で resolved_facet を照合し、'
  '分解された所見を**1 語で引く**。**照合では laterality を見ない** '
  '(右手左手の指定には限界がある、user 指摘) し、型の region が NULL なら部位を問わない。'
  ' (3) **従来タグとの橋** (0072) — tag_name と突き合わせて取りこぼしを測る。'
  '**機械の推測ではない** — 展開先は表にあり、適用してもコントロールが埋まるだけで'
  'DB には何も書かれない。**羽交い締めという所見は作らない** — 型に当たるかは'
  'その都度導く (0019/0049/0060 と同じ「導けるものは保存しない」)';

UPDATE phrase_macro SET tag_name = v.t FROM (VALUES
  ('full_nelson','full_nelson'), ('vaginal_sex','sex'), ('anal_sex','anal'),
  ('oral_sex','fellatio'), ('paizuri','paizuri'), ('handjob','handjob'),
  ('kissing','kiss'), ('cum_on_face','facial'), ('from_behind','from_behind'),
  ('on_top','girl_on_top'), ('crossed_arms','crossed_arms')
) AS v(s,t) WHERE phrase_macro.slug = v.s;

COMMIT;
