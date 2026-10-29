-- Migration 0053 — 排出は「全体の行為」であって部位の露出ではない
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘:
-- 「繰り返すが**排尿は尿道口の露出を意味しない**。服を着ている場合もあるし
--   横を向いている時は当然見えない。**これは男女ともに**。だから**全体行為に書くべき**」
--
-- ── 0050/0051 の置き場所が間違っていた (私の設計ミス) ──
-- `urination_state` を `applies_kind='region', applies_key='urethral_opening'` に置いたので、
-- **記録するには尿道口を「見えている部位」に挙げる必要があった**。
-- それはまさに「露出を意味する」形で、user が 2 度指摘したのはここ。
--   * 服を着たまま排尿している画像がある
--   * 横向きなら当然見えない
--   * 男女どちらでも同じ
-- **見えていなくても起きている行為**を、見えている部位に紐づけてはいけない。
--
-- ── 4 つとも entity に移す ──
-- 同じ欠陥は既存の 2 つにもある: 服の上から射精している / 潮を吹いているが
-- 局部が見えない、はいずれも普通にある。**値空間を揃えた (0050/0051) のと同じ理由で
-- 置き場所も揃える** — 4 つで扱いが違うと注釈者が毎回考え直すことになる。
-- 先例: `involuntary_motion` (震え・痙攣) は 0023 で「pose ではなく entity 全体の属性」
-- にした。`body_orientation` (0032) も entity。**体全体で起きていることは entity**。
--
-- ── 部位の情報は失わない ──
-- 0052 で `urinating_onto` / `ejaculating_onto` / `defecating_onto` を入れたので、
-- **「どの部位から どこへ」は関係が持つ**。属性は「出ているか」だけを言えばよく、
-- 部位に紐づける理由がもう無い。
--   属性 (entity)  — その人物が出しているか
--   関係 (region→) — どの部位から どこへ飛んでいるか
--
-- ── 既存 32 件の所見は壊れない ──
-- `obs_attribute` の行は `region_slug` を持ったまま残る (append-only。消さない)。
-- 部位が付いていること自体は誤りではない (見えていたなら書いてよい) ので、
-- **撤回は不要**。今後は部位を挙げなくても書けるようになる、という緩和。
--
-- ── 運用手順 ──
--   * 単一 transaction。applies_kind/applies_key の更新と説明のみ。冪等。
--   * ロールバック: applies_kind='region' に戻す (データは触っていない)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

UPDATE attribute SET applies_kind = 'entity', applies_key = 'person'
 WHERE slug IN ('urination_state', 'defecation_state',
                'ejaculation_state', 'squirting_state');

UPDATE attribute SET description = coalesce(description,'') ||
  '。**部位ではなく人物全体に付く** (0053) — 排出は**見えていなくても起きている行為**で、'
  '服を着ていたり横を向いていれば局部は見えない。記録するために部位を挙げさせると'
  '「露出を意味する」形になってしまう (user 指摘 2026-07-31)。'
  '**どの部位から どこへ飛んでいるかは関係** (0052) が持つ'
 WHERE slug IN ('urination_state', 'defecation_state',
                'ejaculation_state', 'squirting_state')
   AND coalesce(description,'') NOT LIKE '%人物全体に付く%';

-- 部位側にも「挙げなくてよい」と書く (0050/0051 で書いた注意書きの続き)
UPDATE body_region SET description = coalesce(description,'') ||
  '。**排尿を書くのにこの部位を挙げる必要はない** (0053) — urination_state は人物全体に付く'
 WHERE slug = 'urethral_opening'
   AND coalesce(description,'') NOT LIKE '%挙げる必要はない%';

UPDATE body_region SET description = coalesce(description,'') ||
  '。**排便を書くのにこの部位を挙げる必要はない** (0053) — defecation_state は人物全体に付く'
 WHERE slug = 'anus'
   AND coalesce(description,'') NOT LIKE '%挙げる必要はない%';

COMMIT;
