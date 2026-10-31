-- Migration 0025 — 関係の参加者に左右を持たせる
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-29:
-- 「右手と左手で違うものをつかんでいるときに表現できない」。
--
-- ── 欠陥 ──
-- 0010 で `obs_attribute.laterality` と `obs_pose.laterality` を足した (「左腕は上げて
-- 右腕は背中」が書けなかったため)。しかし **`obs_relation` には足していなかった**。
-- 結果:
--   person_1.hand grasping person_2.breast
--   person_1.hand grasping person_2.leg
-- の 2 行が書けても、**どちらの手がどちらを掴んでいるか言えない**。bilateral な部位は
-- 65 あるので、手に限らず「左脚を絡め、右脚は伸ばす」等も同じく書けない。
--
-- **同じ穴を 0010 で 2 箇所だけ塞いで 1 箇所を見落としていた**。0010 が「宣言と着地の
-- 乖離」を直した migration であることを思うと、直し方そのものが不完全だった。
--
-- ── 解決形にも足す (0024 の教訓) ──
-- 観測に足すだけでは 0024 と同じことになる — `resolved_facet` が左右を持てなければ
-- 解決の段階で「どちらの手か」が落ちる。subject 側 (`region_laterality`) と object 側
-- (`object_region_laterality`) の両方を足す。
--
-- ── 値 ──
-- `na` / `left` / `right`。0010 と同じ語彙 (CHECK で縛る。語彙表を作るほどではない —
-- 3 値で増えないので、表にすると参照が増えるだけになる)。
-- NULL = 左右を問わない (midline の部位、または左右を区別しない観測)。
--
-- ── 運用手順 ──
--   * 単一 transaction。列追加 → CHECK (NOT VALID → VALIDATE)。
--   * 既存行は NULL なので VALIDATE は即通る。
--   * ロールバック: 追加した 4 列を DROP。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- =====================================================================
-- 1. 観測側
-- =====================================================================
ALTER TABLE obs_relation ADD COLUMN IF NOT EXISTS subject_laterality text;
ALTER TABLE obs_relation ADD COLUMN IF NOT EXISTS object_laterality  text;

ALTER TABLE obs_relation ADD CONSTRAINT ck_obs_relation_laterality
    CHECK ((subject_laterality IS NULL OR subject_laterality IN ('na','left','right'))
       AND (object_laterality  IS NULL OR object_laterality  IN ('na','left','right')))
    NOT VALID;
ALTER TABLE obs_relation VALIDATE CONSTRAINT ck_obs_relation_laterality;

COMMENT ON COLUMN obs_relation.subject_laterality IS
  '主体側の部位の左右 (na/left/right)。**これが無いと「右手で A を、左手で B を掴む」が'
  '書けない** — 同じ hand を主語にした 2 行が区別できないため (2026-07-29 指摘)。'
  '0010 で obs_attribute / obs_pose に左右を足したときに、ここだけ見落としていた。';
COMMENT ON COLUMN obs_relation.object_laterality IS
  '対象側の部位の左右。「左の乳房を掴む」等。NULL = 左右を問わない。';

-- =====================================================================
-- 2. 解決形にも足す (0024 の教訓 — 観測だけ足すと解決で落ちる)
-- =====================================================================
ALTER TABLE resolved_facet ADD COLUMN IF NOT EXISTS region_laterality        text;
ALTER TABLE resolved_facet ADD COLUMN IF NOT EXISTS object_region_laterality text;

ALTER TABLE resolved_facet ADD CONSTRAINT ck_resolved_facet_laterality
    CHECK ((region_laterality        IS NULL OR region_laterality        IN ('na','left','right'))
       AND (object_region_laterality IS NULL OR object_region_laterality IN ('na','left','right')))
    NOT VALID;
ALTER TABLE resolved_facet VALIDATE CONSTRAINT ck_resolved_facet_laterality;

-- 対象側の左右を持てるのは関係だけ (属性や部位に「相手」は無い)
ALTER TABLE resolved_facet ADD CONSTRAINT ck_resolved_facet_object_lat_only_relation
    CHECK (domain = 'relation' OR object_region_laterality IS NULL) NOT VALID;
ALTER TABLE resolved_facet VALIDATE CONSTRAINT ck_resolved_facet_object_lat_only_relation;

COMMENT ON COLUMN resolved_facet.region_laterality IS
  '主体側の部位の左右。domain=''attribute'' なら属性が付く部位の左右、'
  'domain=''relation'' なら主体側の参加部位の左右。';
COMMENT ON COLUMN resolved_facet.object_region_laterality IS
  '関係の対象側の部位の左右。domain=''relation'' のときだけ入る。';

COMMIT;
