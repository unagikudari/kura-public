-- Migration 0004 — obs_relation の ingest 経路 (ref ベースの参加者)
--
-- plan kura-1.0-implementation rev11 (broker fact f0c1462f) S-6 の続き。
-- S-8 (人手注釈) の writer を書いていて発覚した穴。
--
-- ── 何が抜けていたか ──
-- 0002 は「**entity は resolver の出力であって観測の属性ではない**」という設計に
-- 変えた。観測は `observation.subject_ref` (主体を指す語、例 'person_1') だけを持ち、
-- どの entity を指すかは resolver_run ごとに resolved_entity_binding が決める。
--
-- ところが `obs_relation` の参加者は 0003(g) の後でも **entity ベースのみ**だった:
--     subject_entity_id / object_entity_id            → entity(id)
--     subject_entity_region_id / object_entity_region_id → entity_region(id)
-- entity は注釈時点に存在しないので、**人手注釈で relation を 1 件も書けない**。
-- 0003(g) が「ontology は region 参加者を宣言しているのに DDL が受け取れない」
-- という乖離だったのに対し、こちらは「0002 が ref 経路を作ったのに obs_relation に
-- 同じ経路が無い」という乖離。同じ族で、原因も同じ (宣言と着地の不一致)。
--
-- 影響: S-3 の分類表 (broker fact 3a9a523e) で「additive migration で表現可能」と
-- 判定し **gate の分子に数えた** A2 (行為の対象領域) / A5 (腕が多数) / A6 (ペニスが多数)
-- は全て relation の query。ここが書けないと 3 本まとめて測定不能になる。
-- 0003(g) と同じ原理で **省略不可**。
--
-- ── 設計 ──
-- role (subject / object) ごとに参加者の指し方を 3 通り許し、CHECK で排他にする:
--   1. ref 形 (**ingest の通常経路**) — `*_ref` + 任意で `*_region_slug`。
--      「person_1 の hand」を entity 無しで表せる。region は body_region の語彙で
--      指すので、誰の region かは ref が担う。
--   2. entity 形 — human が既存の run-scoped entity を直接指名する場合。
--   3. entity_region 形 (0003 g) — resolver 解決後の形。
-- resolver は ref 形の観測を読み、run ごとに 2/3 の形へ解決した結果を
-- resolved_facet / resolved_entity_binding に書く。観測自体は append-only のまま。
--
-- ── 運用手順 ──
--   * 単一 transaction、`SET LOCAL lock_timeout`。列追加 → CHECK 追加の順。
--   * 既存行は全列 NULL なので新 CHECK を即 VALIDATE できる (NOT VALID 不要)。
--     ただし将来の再実行に備えて NOT VALID → VALIDATE の 2 段で書く (0002 と同じ流儀)。
--   * 再実行境界: transaction 単位。失敗なら全ロールバックで丸ごと再実行。
--   * ロールバック:
--       ALTER TABLE obs_relation DROP CONSTRAINT ck_obs_relation_object_participant;
--       ALTER TABLE obs_relation DROP CONSTRAINT ck_obs_relation_subject_participant;
--       ALTER TABLE obs_relation DROP CONSTRAINT ck_obs_relation_object_one;   -- 0003 の旧 CHECK を戻す
--       ALTER TABLE obs_relation DROP CONSTRAINT ck_obs_relation_subject_one;
--       ALTER TABLE obs_relation DROP COLUMN object_region_slug, DROP COLUMN object_ref,
--                                 DROP COLUMN subject_region_slug, DROP COLUMN subject_ref;
--     ※ 0003 の ck_obs_relation_{subject,object}_one は本 migration が置き換えるので、
--        戻す場合は 0003 の定義を再追加すること (ヘッダに定義を残す):
--        CHECK (NOT (subject_entity_id IS NOT NULL AND subject_entity_region_id IS NOT NULL))
--        CHECK (NOT (object_entity_id  IS NOT NULL AND object_entity_region_id  IS NOT NULL))

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- ref 形の参加者。observation.subject_ref と同じ「producer 内で主体を指す語」。
ALTER TABLE obs_relation ADD COLUMN subject_ref         text;
ALTER TABLE obs_relation ADD COLUMN subject_region_slug text REFERENCES body_region(slug);
ALTER TABLE obs_relation ADD COLUMN object_ref          text;
ALTER TABLE obs_relation ADD COLUMN object_region_slug  text REFERENCES body_region(slug);

COMMENT ON COLUMN obs_relation.subject_ref IS
  'ingest の通常経路。producer 内で主体を指す語 (observation.subject_ref と同じ規約)。entity は resolver の出力なので注釈時点では指せない。subject_region_slug と併せて「person_1 の hand」を表す。';
COMMENT ON COLUMN obs_relation.subject_region_slug IS
  'ref 形での region 指定 (relation_applicability の participant_kind=region に対応)。誰の region かは subject_ref が担うので body_region の語彙で足りる。';

-- 0003 の「entity と entity_region の同時指定を禁止」を、ref を含む 3 択の排他へ拡張する。
ALTER TABLE obs_relation DROP CONSTRAINT ck_obs_relation_subject_one;
ALTER TABLE obs_relation DROP CONSTRAINT ck_obs_relation_object_one;

-- role ごとに「entity 形 / entity_region 形 / ref 形」のうち **高々 1 つ**だけ使う。
-- 0 個 (参加者未指定) は許す — relation の片側だけが分かっている観測を表せる
-- (非捏造: 分からない側を埋めない)。
ALTER TABLE obs_relation ADD CONSTRAINT ck_obs_relation_subject_participant CHECK (
    (CASE WHEN subject_entity_id        IS NOT NULL THEN 1 ELSE 0 END)
  + (CASE WHEN subject_entity_region_id IS NOT NULL THEN 1 ELSE 0 END)
  + (CASE WHEN subject_ref              IS NOT NULL THEN 1 ELSE 0 END) <= 1) NOT VALID;
ALTER TABLE obs_relation ADD CONSTRAINT ck_obs_relation_object_participant CHECK (
    (CASE WHEN object_entity_id        IS NOT NULL THEN 1 ELSE 0 END)
  + (CASE WHEN object_entity_region_id IS NOT NULL THEN 1 ELSE 0 END)
  + (CASE WHEN object_ref              IS NOT NULL THEN 1 ELSE 0 END) <= 1) NOT VALID;

-- region_slug は ref 形のときだけ意味を持つ (entity/entity_region 形では region は
-- そちら側が担うので、両方書かれていたら二重指定 = 誤り)。
ALTER TABLE obs_relation ADD CONSTRAINT ck_obs_relation_subject_region_needs_ref CHECK (
    subject_region_slug IS NULL OR subject_ref IS NOT NULL) NOT VALID;
ALTER TABLE obs_relation ADD CONSTRAINT ck_obs_relation_object_region_needs_ref CHECK (
    object_region_slug IS NULL OR object_ref IS NOT NULL) NOT VALID;

ALTER TABLE obs_relation VALIDATE CONSTRAINT ck_obs_relation_subject_participant;
ALTER TABLE obs_relation VALIDATE CONSTRAINT ck_obs_relation_object_participant;
ALTER TABLE obs_relation VALIDATE CONSTRAINT ck_obs_relation_subject_region_needs_ref;
ALTER TABLE obs_relation VALIDATE CONSTRAINT ck_obs_relation_object_region_needs_ref;

COMMIT;
