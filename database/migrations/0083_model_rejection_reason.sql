-- Migration 0083 — 不採用 (モデル観測の撤回) の理由を構造化する
--
-- plan kura1-model-observation、2026-07-31 の user 指摘:
-- 「不採用は学習データとして使えると思う。イベントとして蓄積できるか。…構造化も考えよう」
--
-- ── 既に event として貯まっている ──
-- 不採用 = 人間名義の撤回観測 (event_type='retraction' + retracts_id で対象の model 観測を指す)。
-- observation テーブル自体が append-only の event log で、kura タガーの自己改善ループと同じ構造。
-- 負例の対 = (対象 model 観測の内容) + (撤回の理由) は retracts_id で join すれば取れる。
--
-- ── free text より構造化した方が学習に効く ──
-- 撤回理由は producer_ref の `retract: <why>` に free text で入っていた。
-- **なぜ誤りか**を型で持つと、F0 抽出器が負例を種別ごとに扱える
-- (幻覚は「画像に無い」= 強い負例、過検出は「あるが主要でない」= 弱い負例、で重みが違う)。
--
-- ── 語彙表にする (0065 の教訓: ハードコードしない) ──
-- 種別を UI や抽出器に埋め込まず、**表から引く**。緩め方も表を足すだけ。
-- 撤回観測側は producer_ref を `retract:<code>: <why>` の形にして、code をこの表と照合する
-- (observation に列を足さない — 撤回は稀で、free text 前置で足りる。過剰設計を避ける)。
--
-- ロールバック: DROP TABLE。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

CREATE TABLE IF NOT EXISTS model_rejection_reason (
  code          text PRIMARY KEY,
  display_name  text NOT NULL,
  training_weight text NOT NULL DEFAULT 'normal',  -- strong / normal / weak
  description   text
);
COMMENT ON TABLE model_rejection_reason IS
  'モデル観測を不採用にした理由の種別 (0083)。撤回観測の producer_ref を '
  '`retract:<code>: <why>` にして code をここと照合する。'
  'F0 抽出器が負例を種別ごとに重み付けする (幻覚=strong / 過検出=weak)';

INSERT INTO model_rejection_reason(code, display_name, training_weight, description) VALUES
  ('hallucination','幻覚 (画像に無い)','strong',
   '画像に存在しないものをモデルが書いた。最も強い負例'),
  ('wrong_value','値違い','normal',
   '軸は合っているが値が違う (large を huge 等)'),
  ('wrong_region','部位違い','normal',
   '対象の部位・付着先が違う'),
  ('wrong_subject','帰属違い','normal',
   '別の人物のものをこの人物に帰属させた (leg_grab の「誰の」問題と同じ)'),
  ('over_detection','過検出','weak',
   '画像にはあるが、その場面で主要でない / 人間なら書かない。最も弱い負例'),
  ('other','その他','normal','上のどれにも当てはまらない')
ON CONFLICT (code) DO NOTHING;

COMMIT;
