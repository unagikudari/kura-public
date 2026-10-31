-- Migration 0003 — 非捏造の着地 + region 参加者 + seed 追加
--
-- plan kura-1.0-implementation rev11 (broker fact f0c1462f) S-6。
-- 詳細仕様: docs notes/kura-1.0-migration-0002-0003 §0003 と §region 参加者、
--           seed は docs notes/kura-1.0-seed-additions。
--
-- 閉じる指摘:
--   * 視点A CRIT-A2 (review fact 2a37c148) — unresolved の候補集合と contested の
--     保存先が schema に無く、resolver-specification.md §4.2/§6 が実装不能だった。
--     さらに facet_key を text にすると「全 term=data」に反し、表記揺れ・未知 key が
--     入る (LLM の context 汚染源)。
--   * 視点A HIGH (review fact 8a259cfa) — continuous facet に尺度が無い。
--     → 尺度は attribute 側 (range_min/range_max/unit) が既に一意に規定しているので
--        resolved_facet に重複列を作らず **導出**する。unit を語彙表 FK 化して
--        「全 term=data」を unit にも適用する。
--   * rev2 執筆時の自己発見 — resolved 側の着地が resolved_observation と
--     tag_projection しかなく、§5 の「median + [min,max] interval」= どの単一観測でも
--     ない集約値が表現できなかった。
--   * (g) **S-3 paper dry-run の発見** (broker fact 5403a1ee) —
--     relation_applicability.tsv は participant_kind に region を 5 行持つのに
--     obs_relation は両参加者が entity への FK で region を指せない。ontology が
--     宣言しているのに DDL が受け取れない = CRIT-A2 と同族。
--     **S-3 の gate 通過根拠に含めたので省略不可。**
--   * (h) **seed 追加** — S-3 で「seed 1 行で表現可能」と判定した 4 query
--     (A7/B1/B6/B7) は seed 行が無いと測定できない。(g) と同じ原理で省略不可。
--
-- ── 運用手順 ──
--   * 単一 transaction。`SET LOCAL lock_timeout`。
--   * DDL 順: 語彙表 → 参照する表 → attribute.unit の FK 化 (既存値を語彙表へ
--     seed した後に NOT VALID → VALIDATE) → obs_relation の region 参加者 → seed。
--   * 再実行境界: transaction 単位。失敗は全ロールバックなので丸ごと再実行。
--   * ロールバック: 逆順で DROP。attribute.unit の FK は
--       ALTER TABLE attribute DROP CONSTRAINT fk_attribute_unit;
--     を先に外してから unit 表を落とす。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- =====================================================================
-- (a) unresolved の候補集合 — resolver-specification.md §4.2
--     「候補 entity 集合とともに記録する」を実際に保存できるようにする。
--     rank で順位、exclusivity で「候補のうち 1 つだけが真か / 少なくとも 1 つか /
--     不明か」を型付きで持つ (視点A: 排他性・順位・採否が制約できない への回答)。
-- =====================================================================
CREATE TABLE candidate_exclusivity (slug text PRIMARY KEY, description text NOT NULL);
INSERT INTO candidate_exclusivity(slug, description) VALUES
  ('exactly_one',  '候補のうち厳密に 1 つが真'),
  ('at_least_one', '候補のうち少なくとも 1 つが真 (複数可)'),
  ('unknown',      '排他性そのものが不明');

CREATE TABLE unresolved_subject_candidate (
    resolver_run_id bigint NOT NULL REFERENCES resolver_run(id),
    observation_id  bigint NOT NULL REFERENCES observation(id),
    entity_id       bigint NOT NULL,
    rank            integer,
    exclusivity     text NOT NULL REFERENCES candidate_exclusivity(slug),
    PRIMARY KEY (resolver_run_id, observation_id, entity_id),
    FOREIGN KEY (entity_id, resolver_run_id) REFERENCES entity(id, resolver_run_id));

COMMENT ON TABLE unresolved_subject_candidate IS
  'subject_scope=unresolved の観測について「主体はこの候補集合のどれか」を run ごとに記録する。非捏造規則 (ENTITY-RESOLUTION.md §4.2) の物理的な受け皿 — 候補が絞れないことを「絞れない」として保存できるので、任意の 1 つへ推測帰属させる必要が無い。';

-- =====================================================================
-- (b)(c)(d) resolved_facet — 解決結果そのものの着地。
--     facet_key text を持たず、domain 毎の typed な ontology FK で指す。
--     continuous は集約値と区間を持つが unit/range は attribute から導出する。
-- =====================================================================
CREATE TABLE resolved_facet_status (slug text PRIMARY KEY, description text NOT NULL);
INSERT INTO resolved_facet_status(slug, description) VALUES
  ('resolved',   '単一の値に解決された'),
  ('unresolved', '解決できなかった (証拠不足)'),
  ('contested',  '同 tier の矛盾する証拠があり、collapse させない');

CREATE TABLE aggregation_method (slug text PRIMARY KEY, description text NOT NULL);
INSERT INTO aggregation_method(slug, description) VALUES
  ('human_point',      '人手の点推定 (§5: human が勝つ)'),
  ('median_of_models', 'モデル推定の median + [min,max] 区間 (§5)');

CREATE TABLE resolved_facet (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    resolved_scene_id bigint NOT NULL REFERENCES resolved_scene(id),
    domain            text NOT NULL REFERENCES observation_domain(slug),
    entity_id         bigint,                       -- scene-scoped facet は NULL
    status            text NOT NULL REFERENCES resolved_facet_status(slug),
    -- domain 毎の typed な ontology 参照 (text の自由 key は持たない)
    region_slug       text REFERENCES body_region(slug),
    attribute_slug    text REFERENCES attribute(slug),
    pose_slug         text REFERENCES pose(slug),
    relation_slug     text REFERENCES relation(slug),
    descriptor_slug   text REFERENCES scene_descriptor(slug),
    object_slug       text REFERENCES object_type(slug),
    species_slug      text REFERENCES species(slug),
    character_slug    text REFERENCES character(slug),
    presentation      text REFERENCES gender_presentation(slug),
    -- 値。categorical/boolean は value_slug、continuous は numeric + 区間。
    value_slug        text,
    value_numeric     numeric,
    interval_low      numeric,
    interval_high     numeric,
    aggregation       text REFERENCES aggregation_method(slug),
    -- domain と ontology 参照の対応を強制 (正しい列のみ非 NULL)
    CONSTRAINT ck_resolved_facet_domain_target CHECK (
        CASE domain
          WHEN 'body_region'      THEN region_slug     IS NOT NULL
          WHEN 'attribute'        THEN attribute_slug  IS NOT NULL
          WHEN 'pose'             THEN pose_slug       IS NOT NULL
          WHEN 'relation'         THEN relation_slug   IS NOT NULL
          WHEN 'scene_descriptor' THEN descriptor_slug IS NOT NULL
          WHEN 'object'           THEN object_slug     IS NOT NULL
          WHEN 'identity'         THEN species_slug IS NOT NULL OR character_slug IS NOT NULL
          WHEN 'entity_count'     THEN presentation    IS NOT NULL
          ELSE false
        END),
    -- 区間の整合。value_numeric があるときだけ効かせる。
    CONSTRAINT ck_resolved_facet_interval CHECK (
        value_numeric IS NULL
        OR ( (interval_low  IS NULL OR interval_low  <= value_numeric)
         AND (interval_high IS NULL OR value_numeric <= interval_high) )),
    -- 集約方法は continuous のときだけ意味を持つ
    CONSTRAINT ck_resolved_facet_aggregation CHECK (
        (aggregation IS NULL) = (value_numeric IS NULL)),
    -- contested は単一値を持たない (collapse させないため)
    CONSTRAINT ck_resolved_facet_contested CHECK (
        status <> 'contested' OR (value_slug IS NULL AND value_numeric IS NULL)));

COMMENT ON TABLE resolved_facet IS
  '解決結果そのもの。resolved_observation は「採用した観測」を指すだけなので、§5 の median+[min,max] のような「どの単一観測でもない集約値」を表せなかった。unit と range は attribute 表 (range_min/range_max/unit) が一意に規定するのでここには持たない — 重複させると第二の真実になる。';
COMMENT ON CONSTRAINT ck_resolved_facet_contested ON resolved_facet IS
  'contested は「矛盾していて決められない」状態。単一値を持たせないことで、任意の 1 つへ collapse させる実装ミスを DB が弾く。';

CREATE TABLE resolved_facet_evidence (
    resolved_facet_id bigint NOT NULL REFERENCES resolved_facet(id),
    observation_id    bigint NOT NULL REFERENCES observation(id),
    role              text NOT NULL CHECK (role IN ('accepted', 'conflicting')),
    PRIMARY KEY (resolved_facet_id, observation_id));

COMMENT ON TABLE resolved_facet_evidence IS
  'contested の対立観測を捨てずに残す。role=conflicting が「これと矛盾していた」証拠。';

-- =====================================================================
-- (e) unit を語彙表 FK 化 — 「全 term=data」を unit にも適用する。
--     既存 attribute.unit の distinct 値を先に seed してから FK を張る
--     (未 seed の値があると FK 追加で失敗するため順序が要る)。
-- =====================================================================
CREATE TABLE unit (slug text PRIMARY KEY, description text NOT NULL);

-- ontology seed (attribute-ontology/attributes.tsv) が使う unit を明示 seed する。
-- **migration の適用順に依存しないため**: 既存値の DISTINCT だけだと「0003 を先に
-- 適用してから attributes.tsv を seed する」順で FK 違反になる (seed 側の ratio /
-- degree が unit 表に無い)。明示 seed + DISTINCT 補完の 2 段で両方の順序に耐える。
INSERT INTO unit(slug, description) VALUES
  ('ratio',  '0..1 の比 (wetness / flush_intensity)'),
  ('degree', '角度 (joint_flexion)')
ON CONFLICT (slug) DO NOTHING;

-- 既に attribute が seed 済で、上に無い unit を使っている場合を拾う。
INSERT INTO unit(slug, description)
SELECT DISTINCT a.unit, a.unit
FROM attribute a
WHERE a.unit IS NOT NULL
ON CONFLICT (slug) DO NOTHING;

ALTER TABLE attribute ADD CONSTRAINT fk_attribute_unit
    FOREIGN KEY (unit) REFERENCES unit(slug) NOT VALID;
ALTER TABLE attribute VALIDATE CONSTRAINT fk_attribute_unit;

-- =====================================================================
-- (g) obs_relation に region 参加者 — S-3 の発見 (broker fact 5403a1ee)。
--     relation_applicability.tsv が participant_kind='region' を 5 行持つのに
--     obs_relation は entity しか指せなかった。
--     region 側は body_region.slug ではなく **entity_region(id)** を参照する:
--     こうすると「誰の手か」が保たれ、所有者不明の手は所有 entity が unresolved 側に
--     落ちるので非捏造規則と整合する。
-- =====================================================================
ALTER TABLE obs_relation ADD COLUMN subject_entity_region_id bigint REFERENCES entity_region(id);
ALTER TABLE obs_relation ADD COLUMN object_entity_region_id  bigint REFERENCES entity_region(id);

ALTER TABLE obs_relation ADD CONSTRAINT ck_obs_relation_subject_one CHECK (
    NOT (subject_entity_id IS NOT NULL AND subject_entity_region_id IS NOT NULL));
ALTER TABLE obs_relation ADD CONSTRAINT ck_obs_relation_object_one CHECK (
    NOT (object_entity_id IS NOT NULL AND object_entity_region_id IS NOT NULL));

COMMENT ON COLUMN obs_relation.subject_entity_region_id IS
  'subject が region のときの参加者 (relation_applicability の participant_kind=region)。例: grasping の subject=hand。entity_region 経由なので所有 entity が辿れる。subject_entity_id とは排他。';

-- =====================================================================
-- (h) seed 追加 — S-3 で「seed 1 行で表現可能」と判定した query を測定可能にする。
--     一覧と根拠: docs notes/kura-1.0-seed-additions
-- =====================================================================

-- A7 ヨガ・土下座のような特殊ポーズ (flat 側: dogeza 700 / prostration 149)
--   `yoga` は追加しない — コーパス 13 件かつ単一 pose でなく family なので
--   pose slug 1 つでは表せない。A7 の限界として plan S-12 に記録する。
INSERT INTO pose(slug, display_name, scope, category) VALUES
  ('dogeza',      'dogeza',      'whole_body', 'posture'),
  ('prostration', 'prostration', 'whole_body', 'posture')
ON CONFLICT (slug) DO NOTHING;

-- B1 行為ポジションの重複 / B6 all_fours 系の同時多発
--   spatial relation に「背後」が無かったのが原因。amazon/cowgirl/girl_on_top/
--   straddling や doggystyle は pose + directed interaction + 相対配置の合成で、
--   配置を表す語だけが欠けていた。
INSERT INTO relation(slug, display_name, category, arity, directionality) VALUES
  ('behind',      'behind',      'spatial', 'binary', 'directed'),
  ('in_front_of', 'in front of', 'spatial', 'binary', 'directed')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO relation_applicability(relation_slug, role, participant_kind, participant_key) VALUES
  ('behind',      'subject',     'entity', 'person'),
  ('behind',      'object',      'entity', 'person'),
  ('in_front_of', 'subject',     'entity', 'person'),
  ('in_front_of', 'object',      'entity', 'person');

-- B7 big_belly と pregnant — 別 domain の別属性に分けると競合が消える。
--   belly_shape は見た目 (region=abdomen、regions.tsv:49 に実在)、
--   pregnancy_state は状態 (entity=person)。腹部が丸いことと妊娠は独立に観測でき、
--   片方だけ分かる場合も表せる (非捏造)。
INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('belly_shape',     'belly shape',     'appearance', 'categorical', 'region', 'abdomen'),
  ('pregnancy_state', 'pregnancy state', 'state',      'categorical', 'entity', 'person')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('belly_shape', 'flat'), ('belly_shape', 'soft'),
  ('belly_shape', 'rounded'), ('belly_shape', 'distended'),
  ('pregnancy_state', 'not_pregnant'), ('pregnancy_state', 'pregnant'),
  ('pregnancy_state', 'unknown')
ON CONFLICT (attribute_slug, value_slug) DO NOTHING;

COMMIT;
