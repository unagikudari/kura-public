-- Migration 0041 — 獣頭・半人半獣の種を足し、species と motif の境界を明文化する
--
-- plan kura-1.0-implementation、2026-07-30 の user 指摘:
-- 「ミノタウロスが選択できない」
--
-- ── 単なる漏れだった (0040 の続き) ──
-- `lamia` (tail) / `harpy` (wings) / `mermaid` (fish_tail) は**既に species にある**。
-- つまりこの ontology は「半人半獣の空想種は species に 1 語で持つ」方針で来ている。
-- なのに minotaur / centaur / werewolf が無い。**方針の問題ではなく列挙の漏れ。**
--
-- しかも 0040 で直したのと**同じ非対称**が残っていた: `dragon_girl` と `slime_girl` は
-- あるのに **`dragon` と `slime` が無い**。`dog_girl` はあるが `dog` が無かったのと同型。
--
-- 需要 (production kura 実測): dwarf 1,512 / werewolf 872 / minotaur 413 / dragon 347 /
-- centaur 299 / tengu 172。slime 1,593 (slime_girl とは別に「スライムそのもの」)。
-- 足さないもの: satyr 10 / kobold 0 / alraune 5 — 需要が無く、選択肢を増やすと選ぶのが重い。
--
-- ── species と motif_species の境界を明文化する (ここが本題) ──
-- 0040 で `motif_species` (何に似せてあるか) を入れたので、
-- **「ミノタウロス = human + motif=cow ではないのか」**という迷いが生まれる。
-- その迷いを残すと dog / dog_girl のときと同じ「どちらを選ぶか決められない」に戻る。
-- 境界はこう:
--   * **species** = 何であるか。**人でない体を持つ**もの。獣の頭 (minotaur)、
--     下半身が別の動物 (centaur / lamia / mermaid)、全身が別物 (slime / dragon)。
--   * **motif_species** = 何に似せてあるか。**体は人のまま**で耳や尾だけ動物 (kemonomimi)。
-- 判定は「**人体として描かれているか**」の 1 問。牛の頭なら species=minotaur、
-- 人の頭に牛の角と耳が付いているだけなら species=human + motif_species=cow。
--
-- ── 運用手順 ──
--   * 単一 transaction。species 追加 → 説明 → 列コメントに境界を書く。冪等。
--   * ロールバック: 追加した species を DELETE (所見が付いていれば消せない)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO species(slug, display_name, marked_by) VALUES
  ('minotaur', 'ミノタウロス (牛頭)',        'bovine_head'),
  ('centaur',  'ケンタウロス (下半身が馬)',  'equine_lower_body'),
  ('werewolf', '狼人間',                     'canine_head_fur'),
  ('tengu',    '天狗',                       'long_nose_wings'),
  ('dwarf',    'ドワーフ',                   'short_stature'),
  ('dragon',   '竜 (竜そのもの)',            'scales_wings'),
  ('slime',    'スライム (スライムそのもの)', 'amorphous')
ON CONFLICT (slug) DO NOTHING;

-- `_girl` 側との関係を書く。0040 で非推奨にしたものと同じ扱い
UPDATE species SET display_name = display_name || ' [非推奨]'
 WHERE slug IN ('dragon_girl','slime_girl')
   AND display_name NOT LIKE '%[非推奨]%';

-- =====================================================================
-- species と motif_species の境界 (列コメントに固定する)
-- =====================================================================
COMMENT ON COLUMN obs_identity.species_slug IS
  'その主体が**何であるか**。人なら human、犬なら dog、牛頭なら minotaur。'
  '**判定は「人体として描かれているか」の 1 問** — '
  '獣の頭 (minotaur)・下半身が別の動物 (centaur / lamia / mermaid)・'
  '全身が別物 (slime / dragon) は species。'
  '**体は人のままで耳や尾だけ動物なら species=human + motif_species=<動物>** '
  '(0040)。人の頭に牛の角と耳が付いているだけなら minotaur ではない';

COMMENT ON COLUMN obs_identity.motif_species_slug IS
  'その主体が持つ**動物の特徴の由来**。「何であるか」(species_slug) とは別 — '
  '**体が人のまま**で耳や尾だけ動物のとき使う (kemonomimi)。'
  '猫耳の人間は species=human + motif_species=cat。'
  '**獣の頭や別の下半身を持つなら motif ではなく species** (minotaur / centaur)。'
  '**species を再参照する**ので動物の一覧を 2 箇所に持たない。NULL は「言っていない」';

COMMIT;
