-- Migration 0024 — 解決済みの関係に「誰の何に」を持たせ、関係の適用先を宣言する
--
-- plan kura-1.0-implementation の S-9 実装中に発覚 (2026-07-29)。user の注釈
-- 「doggystyle は p1.pelvis pressing_against p2.pelvis と置いて表現した」を検算して出た。
--
-- ── 欠陥 1: resolved_facet が関係の相手を持てない (これが重い) ──
--   obs_relation   person_2.penis  inserting_into  person_1.vaginal_opening
--                            ↓ resolver
--   resolved_facet entity 77       inserting_into  (相手も部位も空)
-- `resolved_facet` は `entity_id` を 1 つしか持たず、**object 側の entity と region を
-- 書く列が無い**。observation は 0003(g)/0004 で表現できるのに、解決形が表現できない。
-- CRIT-A2 / 0010 / 0019 と同じ「宣言と着地の乖離」の **6 件目**。
--
-- これは致命的で、**「その巨乳は誰のものか」を保つのが目的の企画で「誰が誰に挿入して
-- いるか」が解決側で落ちる**。S-11 の測定は解決側で行うので、このままでは関係を使う
-- query (A2/A5/A6 = S-3 で additive migration と判定した 3 本) が測れない。
--
-- 対処: `object_entity_id` / `object_region_slug` を足し、subject 側は既存の
-- `entity_id` + `region_slug` を使う (domain='relation' のとき region_slug は
-- **subject の部位**という意味になる — 用途が domain で決まるのは他の列と同じ)。
--
-- ── 欠陥 2: relation_applicability に 5 関係が未宣言 ──
-- `pressing_against` / `above` / `below` / `inside` / `overlaps` は 1 行も無い。
-- 「その関係が entity 同士か region 同士か」を宣言する表なのに空白なので、
-- **正しい使い方が記録されていない**。user が pelvis↔pelvis で書いた
-- `pressing_against` は妥当だが、妥当だと言える根拠がどこにも無かった。
--
-- 併せて `inserting_into` の宣言も直す: 現行は subject=entity/any だが、実際の注釈は
-- `person_2.penis` = **region** を主語にしている。挿入する側は部位 (陰茎・指・道具) で
-- あって人物ではないので、**注釈の方が正しく宣言が誤り**。
--
-- ── 運用手順 ──
--   * 単一 transaction。列追加 → FK → CHECK (NOT VALID → VALIDATE) → seed 修正。
--   * 既存 resolved_facet 行は新列が NULL なので VALIDATE は即通る。
--   * ロールバック: 追加した 2 列を DROP、applicability の追加行を DELETE。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- =====================================================================
-- 1. 解決済みの関係に相手を持たせる
-- =====================================================================
ALTER TABLE resolved_facet ADD COLUMN IF NOT EXISTS object_entity_id   bigint;
ALTER TABLE resolved_facet ADD COLUMN IF NOT EXISTS object_region_slug text;

ALTER TABLE resolved_facet
  ADD CONSTRAINT resolved_facet_object_entity_id_fkey
      FOREIGN KEY (object_entity_id) REFERENCES entity(id) NOT VALID;
ALTER TABLE resolved_facet VALIDATE CONSTRAINT resolved_facet_object_entity_id_fkey;

ALTER TABLE resolved_facet
  ADD CONSTRAINT resolved_facet_object_region_slug_fkey
      FOREIGN KEY (object_region_slug) REFERENCES body_region(slug) NOT VALID;
ALTER TABLE resolved_facet VALIDATE CONSTRAINT resolved_facet_object_region_slug_fkey;

-- 相手を持てるのは関係だけ。属性や部位に相手は無い。
ALTER TABLE resolved_facet ADD CONSTRAINT ck_resolved_facet_object_only_relation
    CHECK (domain = 'relation'
           OR (object_entity_id IS NULL AND object_region_slug IS NULL)) NOT VALID;
ALTER TABLE resolved_facet VALIDATE CONSTRAINT ck_resolved_facet_object_only_relation;

COMMENT ON COLUMN resolved_facet.object_entity_id IS
  '関係の対象側の entity。**subject 側は既存の entity_id**。'
  'これが無いと解決形が「誰が誰に」を保てず、観測 (obs_relation) が持っている情報を'
  '解決の段階で落とすことになる (2026-07-29 に実測で発覚)。';
COMMENT ON COLUMN resolved_facet.object_region_slug IS
  '関係の対象側の部位。domain=''relation'' のとき **region_slug は subject の部位**を'
  '意味する — 列の用途が domain で決まるのは他の typed 列と同じ。';

-- =====================================================================
-- 2. 未宣言だった関係の適用先
-- =====================================================================
-- pressing_against は directed で、押し付けるのも押し付けられるのも**部位**
-- (骨盤を骨盤に、背中を壁に)。人物まるごとが押し付けるという言い方はしない。
INSERT INTO relation_applicability(relation_slug, role, participant_kind, participant_key) VALUES
  ('pressing_against', 'subject',     'region', 'any'),
  ('pressing_against', 'object',      'region', 'any'),
  -- 空間関係は人物どうしの位置なので entity
  ('above',            'subject',     'entity', 'person'),
  ('above',            'object',      'entity', 'person'),
  ('below',            'subject',     'entity', 'person'),
  ('below',            'object',      'entity', 'person'),
  -- overlaps は視覚的な重なりなので、人物でも部位でも成り立つ (symmetric)
  ('overlaps',         'participant', 'region', 'any'),
  -- inside は包含。中身も容器も部位でありうる (衣服の内側、口の中)
  ('inside',           'subject',     'region', 'any'),
  ('inside',           'object',      'region', 'any')
ON CONFLICT DO NOTHING;

-- =====================================================================
-- 3. inserting_into の主語は部位 (宣言の方が誤っていた)
-- =====================================================================
-- 挿入する側は陰茎・指・道具であって**人物ではない**。実際の注釈も
-- `person_2.penis inserting_into person_1.vaginal_opening` と部位で書かれている。
DELETE FROM relation_applicability
 WHERE relation_slug = 'inserting_into' AND role = 'subject' AND participant_kind = 'entity';
INSERT INTO relation_applicability(relation_slug, role, participant_kind, participant_key) VALUES
  ('inserting_into', 'subject', 'region', 'any')
ON CONFLICT DO NOTHING;

UPDATE relation SET description =
  '挿入している。主体が挿入する側、対象が挿入される側。**主体も対象も部位**で指す '
  '(陰茎・指・道具 → 膣口・肛門・口)。人物まるごとが挿入するとは言わない'
WHERE slug = 'inserting_into';

UPDATE relation SET description =
  '押し付けている。接触に加えて力がかかっている。**部位どうし**で指す — '
  '「骨盤を骨盤に」「背中を壁に」。結合部が見えない性交で、挿入を主張せずに'
  '密着だけを書くのに使える (見えないものを書かないため)'
WHERE slug = 'pressing_against';

COMMIT;
