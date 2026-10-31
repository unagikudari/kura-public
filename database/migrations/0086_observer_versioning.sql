-- Migration 0086 — observer (観測モデル) の版管理をスキーマで整える
--
-- user 方向づけ 2026-08-01「kura の tagger dashboard と対になるもの」
-- 「きちんとデータ構造、スキーマから整える」。
-- それまでの版管理は producer_ref 文字列の手動 bump だけで、
--   (a) 版の意味 (コード commit / 語彙状態) が DB に無い
--   (b) 採点結果が stdout にしか出ない
--   (c) 採用 (adopt) が元の model 観測への参照を持たない
--   (d) 「最新世代」が max(observation.id) のヒューリスティック
-- を全て表で解く。ADR-0002 (json/jsonb 禁止)・単数形命名に従う。
--
-- ロールバック: 4 表を DROP、observation.adopted_from_id を DROP。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- ── (a) 版レジストリ ──
-- producer_ref が構成 (モデル × runner コード × 語彙状態) を一意に決める。
-- runner は起動時に「登録 or 照合」し、既存版と構成が違えば実行を拒否する
-- (= 同じ版名で中身が変わることを機構で封じる)。id 順 = 世代順 (「最新世代」の宣言的定義)。
CREATE TABLE observer_version (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    producer_ref  text NOT NULL UNIQUE,
    model_ref     text NOT NULL,          -- 重み/serving のモデル名
    runner_commit text NOT NULL,          -- 実行時の repo HEAD (short hash)
    ontology_head text NOT NULL,          -- 実行時の語彙 migration 先頭 (例 '0085')
    note          text NOT NULL,          -- 変更概要 1 行
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- 既存 5 世代の backfill。runner_commit は当時の HEAD の近似 (git 履歴から復元、
-- note に (backfill) と明記)。ontology_head は確定値。
INSERT INTO observer_version (producer_ref, model_ref, runner_commit, ontology_head, note)
VALUES
  ('qwen2.5-vl-7b@base', 'qwen2.5-vl-7b', 'e1bab4bef2', '0083',
   '一括プロンプト (S0 前) (backfill 近似)'),
  ('qwen2.5-vl-7b@s0',   'qwen2.5-vl-7b', '0080615197', '0083',
   'S0: 重基準属性を孤立プロンプトに + response_format (backfill)'),
  ('qwen2.5-vl-7b@s1',   'qwen2.5-vl-7b', '5da355ee54', '0084',
   '0084: pigmentation/state 属性の判定基準明記 (backfill 近似)'),
  ('qwen2.5-vl-7b@s2',   'qwen2.5-vl-7b', '9d1c870926', '0084',
   'entity_count / anchor パス追加 (backfill)'),
  ('qwen2.5-vl-7b@s3',   'qwen2.5-vl-7b', 'c039861925', '0085',
   '0085: pigmentation class 限定 + askable を region_attrs 単一ソース化 (backfill)')
ON CONFLICT (producer_ref) DO NOTHING;

-- ── (b) 採点の永続化 ──
-- score_model_observations が実行ごとに 1 run + 軸ごと 1 行を書く。
-- scorer_commit = 一致率の定義の版 (定義固定の規律: 合格ライン fact はこれを参照)。
CREATE TABLE observer_score_run (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    producer_ref  text NOT NULL REFERENCES observer_version(producer_ref),
    scorer_commit text NOT NULL,
    mode          text NOT NULL CHECK (mode IN ('exploratory', 'pass_fail')),
    scene_count   integer NOT NULL,
    scored_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE observer_score_axis (
    run_id         bigint NOT NULL REFERENCES observer_score_run(id),
    axis           text NOT NULL CHECK (axis IN
      ('species','presentation','region','pose','attribute','relation',
       'scene_descriptor','entity_count','relation_multi')),
    matched        integer NOT NULL,
    model_only     integer NOT NULL,
    human_only     integer NOT NULL,
    match_precision numeric,             -- pass_fail mode (anchor 対応付け後) のみ
    match_recall    numeric,
    passed          boolean,
    PRIMARY KEY (run_id, axis)
);

-- ── (c) P3 許可表 (どの軸を model に任せるか) ──
-- 空 = 何も任せていない (現状)。allowed の根拠は score_run への参照で残す。
CREATE TABLE observer_axis_policy (
    axis            text PRIMARY KEY CHECK (axis IN
      ('species','presentation','region','pose','attribute','relation',
       'scene_descriptor','entity_count')),
    allowed         boolean NOT NULL DEFAULT false,
    decided_by      text NOT NULL,
    evidence_run_id bigint REFERENCES observer_score_run(id),
    decided_at      timestamptz NOT NULL DEFAULT now()
);

-- ── (d) 採用の出所リンク (retracts_id と対称) ──
-- P4 の教師対 (モデル所見 × 人間の判断) を値一致の間接復元でなく参照で取る。
ALTER TABLE observation ADD COLUMN adopted_from_id bigint REFERENCES observation(id);
ALTER TABLE observation ADD CONSTRAINT ck_observation_adopted_by_human
  CHECK (adopted_from_id IS NULL OR producer_kind = 'human');
CREATE INDEX ix_observation_adopted_from ON observation (adopted_from_id)
  WHERE adopted_from_id IS NOT NULL;

COMMIT;
