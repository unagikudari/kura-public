-- Migration 0079 — 背景の観衆を「数えるが個体にしない」形で書けるようにする
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘 (post 378469):
-- 「直接行為に関係ない**無数の背景観衆**をどう表現するか」
--
-- ── 人数の仕組みは既にある ──
-- `obs_entity_count` は presentation ごとに `count_min` + `count_exact` を持つ。
-- **「男性が 10 人以上、正確には数えていない」は既に書ける**ので、
-- 背景に 20 人いても entity を 20 個作る必要は無い。
--
-- ── 足りないのは「参加しているか」 ──
-- 数えられるが、**行為に加わっている人と見ているだけの人が同じ数に混ざる**。
-- post 378469 のように 3 人が接触していて 20 人が眺めている場面で、
-- 「男性 23 人」としか言えないと、A4 (人数の誤り) も A6 (ペニスの持ち主) も歪む。
--
-- 実測 (production kura): crowd 16,456 / gangbang 54,322 / public_use 2,999 /
-- audience 1,905。**flat tag は「群衆がいる」までで、何人がどちらかを言わない**。
--
-- ── 新しい entity を作らない (ここが要点) ──
-- 「群衆」という entity_kind を足す案もあるが、**それは 1 つの動作主ではない**。
-- 0030 で entity_kind を person / creature / object に分けたのは**動作主かどうか**が
-- 基準で、群衆は動作主の集合であって単一の主体ではない。
-- **数の話は数の表に置く**のが筋なので、`obs_entity_count` に列を足す。
--
-- ── 値 ──
-- participant / bystander。**NULL は「言っていない」** — 従来の所見は全部 NULL のままで、
-- 意味は変わらない (「参加も傍観も言っていない」)。
-- **`crowd` のような語を作らない** — 何人が傍観しているかは count_min が持つので、
-- 「群衆がいる」は count_min が大きい bystander から**導ける** (0019/0049/0060 と同じ)。
--
-- ── 解決形にも載せる ──
-- 宣言だけして着地させないのを繰り返さない (0044 / 0070 と同じ)。
--
-- ロールバック: 追加した列を DROP。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

ALTER TABLE obs_entity_count ADD COLUMN IF NOT EXISTS involvement text;
ALTER TABLE obs_entity_count DROP CONSTRAINT IF EXISTS ck_obs_entity_count_involvement;
ALTER TABLE obs_entity_count ADD CONSTRAINT ck_obs_entity_count_involvement
  CHECK (involvement IS NULL OR involvement IN ('participant','bystander')) NOT VALID;
ALTER TABLE obs_entity_count VALIDATE CONSTRAINT ck_obs_entity_count_involvement;

COMMENT ON COLUMN obs_entity_count.involvement IS
  'その人数が**行為に加わっているか** (0079)。participant / bystander。'
  '**背景の観衆を個体にしないための列** — count_min + count_exact=false で'
  '「10 人以上、正確には数えていない」と書けるが、参加者と混ざると人数の問いが歪む '
  '(user 指摘 2026-07-31、post 378469)。'
  '**NULL は「言っていない」** — 従来の所見は全部 NULL のままで意味は変わらない。'
  '**`crowd` のような語は作らない** — 「群衆がいる」は count_min が大きい bystander から導ける';

ALTER TABLE resolved_facet ADD COLUMN IF NOT EXISTS involvement text;
COMMENT ON COLUMN resolved_facet.involvement IS
  '解決形に載せた「行為に加わっているか」 (0079)。**宣言だけして着地させないのを繰り返さない** — '
  'resolver に無いと query が引けず、0044 で object が 1 件も解決されなかったのと同じになる';

COMMIT;
