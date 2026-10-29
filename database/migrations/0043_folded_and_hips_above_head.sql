-- Migration 0043 — 脚を頭の向こうへ折り返す / 腰が頭より高い を書けるようにする
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘:
-- 「post 796003 を示すポーズがない」
--
-- ── 何が書けなかったか (production の tag と突き合わせて特定) ──
-- 796003 の tag: `legs_over_head` / `folded` / `piledriver_(sex)` / `upside-down` /
-- `top-down_bottom-up`。kura1 側の pose は `knees_to_chest` (膝を胸に引き寄せる) が
-- 一番近いが、**脚が頭より向こうまで行く**ところは書けない。
-- また `body_orientation` (0032) は front / profile / back の**水平の向きだけ**で、
-- **腰が頭より高い**という上下の関係を言う語が無かった。
--
-- 需要 (production kura 実測): folded 35,837 / suspended_congress 8,460 /
-- legs_over_head 8,260 / upside-down 6,735 / piledriver 3,445 / knees_to_chest 3,157。
--
-- ── 合成語は入れない ──
-- `piledriver` も `folded` も**体位の名前**であって幾何ではない。0009 で
-- missionary / cowgirl / doggystyle を入れなかったのと同じ理由で入れない
-- (user 自身が「doggystyle は p1 pelvis pressing against p2 pelvis で表現した」と
--  言っているのが方針)。入れるのは**分解できる 2 つの幾何**だけ:
--
--   (1) `legs_over_head` — 脚が頭より向こうへ折り返されている。
--       `knees_to_chest` と**同じ軸の先** (股関節の屈曲が深い側)。
--       別の軸にしないのは、両方が同時に成り立つことが無いため。
--   (2) `hips_above_head` — 腰が頭より高い。**寝ている向きとは独立** —
--       仰向けで腰を上げた形 (piledriver) と、うつ伏せで尻を上げた形
--       (top-down_bottom-up) の**両方**がこれ。だから on_back / on_stomach と
--       組み合わせて使う。
--
-- ── なぜ `hips_above_head` を pose にするか (attribute にしない) ──
-- 上下の向きを `body_orientation` に足すと、あれは**水平面の向き**の 5 段なので
-- 値空間が混ざる (「背面向きで逆さま」が書けなくなる)。
-- 一方 `lying` / `on_back` は既に pose なので、体の幾何は pose に置くのが一貫している。
--
-- ── 運用手順 ──
--   * 単一 transaction。pose 追加 → 説明 → 隣の語に区別を書く。冪等。
--   * ロールバック: 追加した pose を DELETE (所見が付いていれば消せない)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- category は既存の語彙から選ぶ (arm_configuration / gesture / leg_configuration /
-- posture / self_contact / sexual_configuration)。新設しない — 分類軸を増やすと
-- UI のグループが増えて選ぶのが重くなる。
INSERT INTO pose(slug, display_name, scope, category) VALUES
  ('legs_over_head',  '脚を頭の向こうへ折り返す', 'legs',       'leg_configuration'),
  ('hips_above_head', '腰が頭より高い',           'whole_body', 'posture')
ON CONFLICT (slug) DO NOTHING;

UPDATE pose SET description =
  '脚が**頭より向こうまで折り返されている**。膝が顔の横か、それより先に来る。'
  '`knees_to_chest` と同じ軸の先 (股関節の屈曲が深い側) — 両方は同時に成り立たない。'
  '**体位の名前ではない** — piledriver / folded と呼ばれる形はこれ + `hips_above_head` + '
  '相手との relation で表す (合成語を足さない方針、0009)'
WHERE slug = 'legs_over_head';

UPDATE pose SET description =
  '**腰が頭より高い**。寝ている向きとは独立で、'
  '仰向けで腰を上げた形 (piledriver と呼ばれるもの) と'
  'うつ伏せで尻を上げた形 (top-down bottom-up) の**両方**がこれ。'
  'so `on_back` / `on_stomach` と組み合わせて使う。'
  '**水平の向きは body_orientation** (0032) — あちらは front/profile/back の 5 段で、'
  '上下を混ぜると「背面向きで逆さま」が書けなくなる'
WHERE slug = 'hips_above_head';

-- 隣の語に境界を書く
UPDATE pose SET description = description ||
  '。**脚が頭より向こうまで行くなら legs_over_head** (0043) — こちらは膝が胸まで'
WHERE slug = 'knees_to_chest' AND description NOT LIKE '%legs_over_head%';

UPDATE pose SET description = description ||
  '。**腰が頭より高いかは別 (hips_above_head)** — 仰向けのまま腰だけ上がる形がある'
WHERE slug = 'on_back' AND description NOT LIKE '%hips_above_head%';

UPDATE attribute SET description = description ||
  '。**上下 (腰が頭より高いか) は pose hips_above_head** — ここは水平面の向きだけ'
WHERE slug = 'body_orientation' AND description NOT LIKE '%hips_above_head%';

COMMIT;
