-- Migration 0002 — resolver-owned entity binding (run 整合)
--
-- plan kura-1.0-implementation rev11 (broker fact f0c1462f) S-5。
-- 詳細仕様: docs notes/kura-1.0-migration-0002-0003 §0002。
--
-- 閉じる指摘 (3 視点 adversarial review):
--   * 視点A CRIT-A1 (review fact 2a37c148) — observation.subject_entity_id が
--     append-only の不変 FK なので、resolver-specification.md §7 の「再解決は
--     新 resolver_run で行い過去を編集しない」と矛盾する。ingest 時に確定した
--     entity 帰属は再導出できず、entity 生成権限が ingest と resolver に二重化する。
--   * 視点A HIGH (review fact 8a259cfa) — entity に resolver_run_id を足すだけでは
--     binding が参照する entity と binding 自身の run が一致する保証にならない。
--     entity(id, resolver_run_id) の候補キーと複合 FK が要る。
--   * 視点A CRIT (同) — 既存 CHECK の DROP+再追加は additive ではない。
--
-- 設計の要点: **entity は resolver の出力であって観測の属性ではない**。
-- 観測は「主体を指し示す語」(subject_ref) だけを持ち、その語がどの entity を
-- 指すかは resolver_run ごとに resolved_entity_binding が決める。これで
-- append-only(ADR-0001) と versioned 再解決(§7) が両立する。
--
-- ── entity(id) 直参照の全箇所 (schema.sql を grep、視点A の low-confidence 留保への回答) ──
--   schema.sql:254  entity_region.entity_id        → (d) で複合 FK 化
--   schema.sql:268  observation.subject_entity_id  → **意図的に plain FK のまま**。
--                   observation は ingest 時(resolver 実行前)の記録なので run を持てない。
--                   ingest はこの列を NULL 固定にし、主体は subject_ref で指す((a)/(f))。
--                   human が既存の run-scoped entity を直接指名する場合のみ非 NULL を許す。
--   schema.sql:297  obs_relation.subject_entity_id → (d) で複合 FK 化
--   schema.sql:298  obs_relation.object_entity_id  → (d) で複合 FK 化
--   → 4 箇所すべてを本 migration が扱う。漏れなし。
--
-- ── 運用手順 (視点A CRIT: lock/rollback 設計) ──
--   * 単一 transaction。`SET LOCAL lock_timeout` で待ち続けない (取れなければ失敗して
--     再実行、テーブルを掴んだまま止まらない)。
--   * DDL 順は (a)列追加 → (b)候補キー → (c)新表 → (d)複合 FK → (f)CHECK 差し替え。
--     (f) を最後に置くのは、(a) の subject_ref が無いと新 CHECK が書けないため。
--   * CHECK 差し替えは **新を ADD ... NOT VALID → VALIDATE → 旧を DROP** の順。
--     旧(より強い)制約が生きている間に新を検証するので、不変条件が消える瞬間が無い。
--     DROP+再追加はしない (既存行が新制約を満たさないと失敗し、その間 無制約になる)。
--   * 再実行境界: transaction 単位。途中失敗なら全ロールバックされるので、原因を
--     直して丸ごと再実行する。部分適用の後始末は不要。
--   * ロールバック (適用後に戻す場合) — 逆順で:
--       ALTER TABLE observation ADD CONSTRAINT observation_check
--         CHECK ((subject_scope='entity') = (subject_entity_id IS NOT NULL));
--       ALTER TABLE observation DROP CONSTRAINT ck_observation_subject_scope;
--       ALTER TABLE obs_relation DROP CONSTRAINT fk_obs_relation_object_run;
--       ALTER TABLE obs_relation DROP CONSTRAINT fk_obs_relation_subject_run;
--       ALTER TABLE obs_relation DROP COLUMN resolver_run_id;
--       ALTER TABLE entity_region DROP CONSTRAINT fk_entity_region_entity_run;
--       ALTER TABLE entity_region DROP COLUMN resolver_run_id;
--       DROP TABLE resolved_entity_binding;
--       ALTER TABLE entity DROP CONSTRAINT ux_entity_id_run;
--       ALTER TABLE entity DROP COLUMN resolver_run_id;
--       ALTER TABLE observation DROP COLUMN subject_asset_region_id, DROP COLUMN subject_ref;
--     ※ 旧 CHECK を先に戻すこと (subject_ref を落とす前に、それを参照する新 CHECK を消す)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- (a) 観測は「主体を指し示す語」を持つ。entity は持たない。
--     subject_ref … producer 内で主体を識別する discriminator (例 'person_1')。
--     subject_asset_region_id … bbox で空間的に接地した主体 (人手注釈が入れる)。
ALTER TABLE observation ADD COLUMN subject_ref text;
ALTER TABLE observation ADD COLUMN subject_asset_region_id bigint REFERENCES asset_region(id);

COMMENT ON COLUMN observation.subject_ref IS
  'producer 内で主体を識別する語 (例 person_1)。どの entity を指すかは resolver_run ごとに resolved_entity_binding が決める。ingest は subject_entity_id を書かない。';
COMMENT ON COLUMN observation.subject_entity_id IS
  '非推奨 (0002)。ingest は NULL 固定。human が既存の run-scoped entity を直接指名する場合のみ使う。通常の主体指定は subject_ref。';

-- (b) entity は resolver の出力 → run で版付けし、複合 FK の参照先になる候補キーを作る。
--     id は PK なので (id, resolver_run_id) は自明に一意。この UNIQUE は
--     「複合 FK の参照先」を成立させるために必要。
ALTER TABLE entity ADD COLUMN resolver_run_id bigint REFERENCES resolver_run(id);
ALTER TABLE entity ADD CONSTRAINT ux_entity_id_run UNIQUE (id, resolver_run_id);

COMMENT ON COLUMN entity.resolver_run_id IS
  'この entity を生成した resolver_run。NULL は 0002 以前の行。resolver 由来の派生行 (entity_region / obs_relation / resolved_entity_binding) は複合 FK で同一 run に拘束される。';

-- (c) 「この run では この観測の主体は この entity」— run ごとに再導出できる。
--     観測自体は append-only のまま、binding だけを run ごとに作り直す。
CREATE TABLE resolved_entity_binding (
    resolver_run_id bigint NOT NULL REFERENCES resolver_run(id),
    observation_id  bigint NOT NULL REFERENCES observation(id),
    entity_id       bigint NOT NULL,
    PRIMARY KEY (resolver_run_id, observation_id),
    FOREIGN KEY (entity_id, resolver_run_id) REFERENCES entity(id, resolver_run_id));

COMMENT ON TABLE resolved_entity_binding IS
  'resolver_run ごとの「観測の主体 → entity」解。複合 FK により binding と entity の run が必ず一致する (cross-run 混在は DB 上不可能)。';

-- (d) 派生行も同一 run に拘束する。obs_relation は subject/object の双方に張る
--     (視点A の再検証で明示的に問われた点 — 片方だけでは両参加者が同一 run である
--      保証にならない)。
ALTER TABLE entity_region ADD COLUMN resolver_run_id bigint REFERENCES resolver_run(id);
ALTER TABLE entity_region ADD CONSTRAINT fk_entity_region_entity_run
    FOREIGN KEY (entity_id, resolver_run_id) REFERENCES entity(id, resolver_run_id);

ALTER TABLE obs_relation ADD COLUMN resolver_run_id bigint REFERENCES resolver_run(id);
ALTER TABLE obs_relation ADD CONSTRAINT fk_obs_relation_subject_run
    FOREIGN KEY (subject_entity_id, resolver_run_id) REFERENCES entity(id, resolver_run_id);
ALTER TABLE obs_relation ADD CONSTRAINT fk_obs_relation_object_run
    FOREIGN KEY (object_entity_id, resolver_run_id) REFERENCES entity(id, resolver_run_id);

-- (f) CHECK 差し替え — 新を NOT VALID で足して VALIDATE し、その後で旧を落とす。
--     新制約は対称性を保つ: scope='entity' ⇔ (entity_id か subject_ref のどちらかがある)。
--     これで ingest 時の形 (entity_id IS NULL AND subject_ref IS NOT NULL) が許され、
--     かつ scope が entity 以外なのに主体を指している矛盾は依然として弾かれる。
ALTER TABLE observation ADD CONSTRAINT ck_observation_subject_scope
    CHECK ( (subject_scope = 'entity')
            = (subject_entity_id IS NOT NULL OR subject_ref IS NOT NULL) ) NOT VALID;
ALTER TABLE observation VALIDATE CONSTRAINT ck_observation_subject_scope;

-- 旧制約はインライン CHECK なので名前が自動生成される。名前を決め打ちせず、
-- 「subject_scope と subject_entity_id を参照する CHECK のうち新制約でないもの」を
-- 動的に落とす (schema.sql を再 flatten しても壊れない)。
DO $$
DECLARE c record;
BEGIN
    FOR c IN
        SELECT con.conname
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = rel.relnamespace
        WHERE ns.nspname = 'kura' AND rel.relname = 'observation'
          AND con.contype = 'c'
          AND con.conname <> 'ck_observation_subject_scope'
          AND pg_get_constraintdef(con.oid) LIKE '%subject_scope%'
          AND pg_get_constraintdef(con.oid) LIKE '%subject_entity_id%'
    LOOP
        EXECUTE format('ALTER TABLE kura.observation DROP CONSTRAINT %I', c.conname);
        RAISE NOTICE '0002: dropped superseded CHECK %', c.conname;
    END LOOP;
END $$;

COMMIT;
