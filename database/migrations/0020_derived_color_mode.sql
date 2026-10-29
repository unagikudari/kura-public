-- Migration 0020 — 色モードを「機械で決まる」側へ移す
--
-- plan kura-1.0-implementation の S-8、2026-07-28。0019 で orientation を人手から外した
-- 際に指摘した通り、`rendering` の色モードも画素から測れる。ユーザー判断「やる」。
--
-- ── なぜ facet 単位では切れないか ──
-- 0019 は `scene_facet.derived_from` (ファセット単位) で宣言した。しかし rendering には
-- **2 つの独立した軸**が混ざっている:
--   * 色モード  — greyscale / monochrome / sepia / colored / limited_palette / spot_color
--                 → 画素の彩度・色相・色数で測れる (人の判断が要らない)
--   * 線と塗り  — lineart / sketch
--                 → 「線が整理されているか」は判断。測れない
-- よって **descriptor 単位の宣言**が要る。facet 単位 (0019) と descriptor 単位 (ここ) の
-- どちらかが埋まっていれば「人手で書かない」。
--
-- ── 測り方と、その検証 ──
-- 実装は database/derive_image_facts.py。閾値は同ファイルに集約し、判定理由を文字列で
-- 返す (人が検算できるようにする — 機械が断定する以上、根拠を隠さない)。
-- **決め切れないときは何も出さない**。測れないのに断定するのは非捏造の原則に反する。
--
-- 検証で見つけて直した実際の誤り (捨てずに記録する — 同じ罠を踏まないため):
--   * 色相窓 ±20° では**肌色主体の普通のカラー絵 6/58 枚が sepia と誤判定**された。
--     肌 (25°) と赤いマフラー (5°) が同じ束に入るため。±6° に絞って解消。
--   * `limited_palette` の色数を数える前の縮小に補間を使っており、8 色の絵が 60 色に
--     化けていた。NEAREST に変更して解消。
--   * `spot_color` に「色相が 1 つに揃う」条件を課していたが、差し色には複数の色相が
--     入りうる。空間的に色が少ない (無彩色が大半) だけを条件に変更。
-- 合成した陽性対照 5/5 (greyscale / sepia / monochrome / limited_palette / spot_color)、
-- 実画像 58 枚で誤判定 0 を確認済み。
--
-- ── 運用手順 ──
--   * 単一 transaction。列追加 (nullable) → 宣言の UPDATE。
--   * ロールバック: ALTER TABLE scene_descriptor DROP COLUMN derived_from;

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

ALTER TABLE scene_descriptor ADD COLUMN IF NOT EXISTS derived_from text;

COMMENT ON COLUMN scene_descriptor.derived_from IS
  'NULL 以外 = この語は**機械で決まる**ので人手で注釈しない。値は何から導けるかの説明。'
  'facet 単位の宣言 (scene_facet.derived_from) と併用する — rendering のように '
  '1 つのファセットに「測れる軸 (色モード)」と「判断が要る軸 (線と塗り)」が同居する'
  '場合、facet 単位では切れないため。注釈 UI と annotate_scene.validate() の両方が'
  'この列を読む (UI で隠すだけでは手書き JSON から入りうる)。';

UPDATE scene_descriptor SET derived_from = v.d FROM (VALUES
  ('greyscale',
   '画素の彩度。色の付いた画素が 1% 以下なら greyscale (derive_image_facts.color_mode)'),
  ('monochrome',
   '画素の色相。色相が 1 点に ±6° / 95% 以上集中していれば monochrome。'
   '±20° では肌色主体のカラー絵を誤判定したため窓を絞ってある'),
  ('sepia',
   'monochrome のうち、代表色相が 15〜50° (橙〜茶) の帯に入るもの'),
  ('colored',
   '色の付いた画素が 25% 以上あり、色相が散っている (集中 90% 未満)'),
  ('limited_palette',
   '98% の画素を 12 色以下で覆えるかどうか。色数を数える前の縮小は NEAREST '
   '(補間すると中間色が生まれて色数が化ける)'),
  ('spot_color',
   '画素の 90% 以上が無彩色で、色が付いているのが 10% 以下。'
   '**色相の集中は条件にしない** — 差し色には複数の色相が入りうる')
) AS v(s,d) WHERE scene_descriptor.slug = v.s;

COMMIT;
