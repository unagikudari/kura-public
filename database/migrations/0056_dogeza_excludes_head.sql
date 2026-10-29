-- Migration 0056 — 土下座の定義から顔の向きを外す
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘:
-- 「**土下座であっても顔を上げることはできる**。head のポーズは別にあるから
--   そちらで補えばいい」
--
-- ── 説明に 2 つの事実を混ぜていた ──
-- `dogeza` の説明は「膝と両手を床に付け、**額を床へ近づける**最大限の平伏」で、
-- **体の姿勢**と**顔の向き**を 1 語の定義に welding していた。
-- そのため「顔を上げた土下座」が書けない — 額が床に付いていないと dogeza を
-- 選べないことになり、体は土下座なのに `all_fours` で代用する羽目になる。
--
-- **head scope の pose が既にある** (head_back / head_down / head_tilt / head_turned /
-- looking_back の 5 語、0009)。顔の向きはそちらの仕事で、姿勢の語が持つ必要はない。
-- 0027 で `squatting` の定義から接地を外して蹲踞を書けるようにしたのと**同じ形**の修正。
--
-- ── 直すのは説明だけ ──
-- 語も所見も変えない。**定義を狭く書きすぎていた**のを緩めるだけなので、
-- 既存の所見は引き続き妥当 (額が床に付いた土下座も dogeza のまま)。
--
-- ── 併せて prostration との境界を書き直す ──
-- prostration は「腹または胸を床に付けて伏せる」で**胴の接地**が定義。
-- dogeza は膝と手。どちらも顔の向きは含まない。
--
-- ロールバック: 不要 (説明のみ)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

UPDATE pose SET description =
  '土下座。膝と両手を床に付け、上体を低くする姿勢。'
  '**顔の向きは含まない** — 顔を上げた土下座もある (user 指摘 2026-07-31)。'
  '頭の向きは head scope の pose (head_down / head_back / looking_back 等) で別に書く。'
  '定義に額の位置を入れていた版では「顔を上げた土下座」が書けず、'
  '体は土下座なのに all_fours で代用することになっていた '
  '(0027 で squatting の定義から接地を外して蹲踞を書けるようにしたのと同じ修正)。'
  '**腰が頭より高いかも別** (hips_above_head、0043) — '
  'danbooru が dogeza と top-down_bottom-up を同時に付けている post が 342 件あるのは、'
  '2 つが独立した事実である証拠。'
  '**胴を床に付けるのは prostration** — あちらは腹または胸の接地が定義'
WHERE slug = 'dogeza';

UPDATE pose SET description =
  '平伏。腹または胸を床に付けて伏せる (dogeza より宗教的・儀礼的な広い語)。'
  '**胴の接地が定義**で、膝と手で支える dogeza とはそこが違う。'
  '**顔の向きは含まない** — head scope の pose で別に書く'
WHERE slug = 'prostration';

COMMIT;
