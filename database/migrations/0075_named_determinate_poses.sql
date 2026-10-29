-- Migration 0075 — 「確定的な名前のある形は 1 語。条件は説明に書く」を規則として書く
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘:
-- 「**正座とかもそうだろ**。腰膝をたたんでつま先を伸ばして脛から膝が地面に接している
--   というのを**両足に対して書くのか？**」
--
-- ── 既にそうなっているが、規則として書いていなかった ──
-- `seiza` / `wariza` / `dogeza` / `fetal_position` / `all_fours` は 1 語で、
-- 条件 (どこが接地しているか、関節がどうなっているか) は**説明に書いてある**。
-- 0058 で土下座の接地条件を列挙したのも説明の中で、**注釈者に部位ごとに書かせては
-- いない**。だが「なぜ 1 語なのか」がどこにも書いておらず、0074 の議論で
-- **私が自己接触を分解する方向へ倒した**。規則を表に書いて再発を止める。
--
-- ── 規則 ──
--   **確定的な名前のある形は 1 語**。条件は語の説明に書き、注釈者に分解させない。
--   分解して書かせるのは、**形が決まっていないとき**だけ:
--     * どちらの手か / どの部位かが**画像ごとに変わる** → relation
--     * 左右で違うことをしている                     → relation 2 本
--   逆に言うと、**語がすでに決めていることを別の軸で書かせない** —
--   0019 (向きを幅高から導く) / 0049 (亀頭の露出) / 0060 (獣化の段階) と同じ判断。
--
-- ── 抜けていた座り方を足す ──
-- 実測 (production kura): squatting 47,943 (あり) / crossed_legs 2,328 (あり) /
-- yokozuwari 489 / indian_style 440 / butterfly_sitting 54。
-- **横座り (yokozuwari) と あぐら (indian_style) が無かった** — どちらも
-- 確定的な形なので 1 語で入れる。butterfly_sitting は 54 件なので入れない
-- (必要になってから足す)。
--
-- ロールバック: 追加した pose を DELETE。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

COMMENT ON TABLE pose IS
  '体の構え。**確定的な名前のある形は 1 語**にし、条件 (接地している部位・関節の状態) は'
  '**この表の説明に書く** — 注釈者に部位ごとの分解を書かせない (0075、user 指摘 '
  '「正座を両足に対して書くのか」)。'
  '分解して書かせるのは**形が決まっていないとき**だけ: どちらの手か / どの部位かが'
  '画像ごとに変わるなら relation、左右で違うことをしているなら relation 2 本。'
  '**語がすでに決めていることを別の軸で書かせない** (0019 / 0049 / 0060 と同じ判断)';

INSERT INTO pose(slug, display_name, scope, category) VALUES
  ('yokozuwari',   '横座り',   'whole_body', 'posture'),
  ('indian_style', 'あぐら',   'whole_body', 'posture')
ON CONFLICT (slug) DO NOTHING;

UPDATE pose SET description = v.d FROM (VALUES
  ('yokozuwari',
   '横座り。両脚を体の同じ側へ流して座る。**脚をどう畳んでいるかは書かない** — '
   'この語が決めている (yokozuwari 489)'),
  ('indian_style',
   'あぐら。両脚を前で交差させて座る。**crossed_legs とは別** — あちらは'
   '立っているときや椅子でも成り立つ脚の組み方で、こちらは座り方 (indian_style 440)'),
  ('seiza',
   '正座。膝を折り、脛と甲を接地させ、尻を踵に乗せて座る。'
   '**接地している部位を 1 つずつ書かない** — この語が全部決めている (0075、user 指摘)。'
   '**尻が踵から離れているなら kneeling**')
) AS v(s,d) WHERE pose.slug = v.s;

UPDATE pose SET description = coalesce(description,'') ||
  '。**座り方としてのあぐらは indian_style** (0075) — こちらは立っていても成り立つ脚の組み方'
WHERE slug = 'crossed_legs' AND coalesce(description,'') NOT LIKE '%indian_style%';

COMMIT;
