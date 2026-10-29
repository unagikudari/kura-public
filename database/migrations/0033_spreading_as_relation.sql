-- Migration 0033 — 自分の部位への接触は pose ではなく relation にする
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-29:
-- 「spreading_own_ はポーズではなく相手で表現したほうがよくない」
-- 「相手シリーズで広げている属性が欲しい」。
--
-- ── 指摘は正しく、しかも証拠が語彙の中にあった ──
-- `pose_category` に `self_contact` があり、その説明が
--   "Own limb/region contacting another own region"
-- = **2 つの部位の接触**。これは relation の定義そのもので、pose (体の幾何) ではない。
-- **pose に relation を紛れ込ませていた**ことが、カテゴリ名から読み取れる状態だった。
--
-- pose にしていた実害:
--   * **どちらの手か言えない** (0025 で relation に左右を足したが pose 側は 1 つの語)
--   * **何を広げているか言えない** — 小陰唇か大陰唇か臀部か。語ごとに増やすしかない
--     (spreading_own_labia / spreading_own_ass / … と合成語が増える = B1 の再来)
--   * 相手が自分でなくなった瞬間に別の語が要る (「相手の小陰唇を広げる」)
--
-- relation にすれば `spreading` 1 語で「誰の何が誰の何を広げているか」が全部言える。
-- 自己関係は ENTITY-RESOLUTION.md §5 が既に想定している
-- ("Self-relations (hand_on_own_hip) resolve within a single entity")。
--
-- ── 移す対象 ──
-- **2 つの部位の接触を名指すもの**だけ。使用数 0 なので FK を壊さない。
--   * `spreading_own_labia`  → relation spreading (新規)
--   * `hands_on_own_ass`     → relation touching (既存)
--   * `hugging_own_legs`     → relation holding (既存)
-- 残すもの:
--   * `looking_through_own_legs` — **接触ではない**。股の間から覗く体の構えなので pose。
--   * `hand_on_own_hip` — 使用数 1 (既に観測がある)。**消すと観測が壊れる**ので残し、
--     description に「新しく書くなら relation touching」と記す。append-only の DB では
--     語彙も勝手に消せない。
-- 併せて `self_contact` カテゴリを非推奨にする (説明で明示。行は残す — 上記 2 語が使う)。
--
-- ── 運用手順 ──
--   * 単一 transaction。relation 追加 → 適用先 → pose 削除 → 説明の更新。
--   * 削除する 3 語は使用数 0 を migration 内で確かめてから消す。
--   * ロールバック: 削除した pose を再 INSERT (description は失われる)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- =====================================================================
-- 1. 広げる関係
-- =====================================================================
INSERT INTO relation(slug, display_name, category, directionality) VALUES
  ('spreading', 'spreading', 'interaction', 'directed')
ON CONFLICT (slug) DO NOTHING;

UPDATE relation SET description =
  '左右に開いている。指で小陰唇を、手で臀部を、脚を抱えて開く 等。'
  '**主体も対象も部位**で指すので「誰の何が誰の何を」が全部言える — '
  '自分のを開くのも相手のを開くのも同じ 1 語で書ける (自己関係は主体と対象が同じ ref)。'
  'pose にすると「どちらの手か」「何を広げたか」が言えず、'
  'spreading_own_labia / spreading_own_ass … と合成語が増える'
WHERE slug = 'spreading';

INSERT INTO relation_applicability(relation_slug, role, participant_kind, participant_key) VALUES
  ('spreading', 'subject', 'region', 'any'),
  ('spreading', 'object',  'region', 'any')
ON CONFLICT DO NOTHING;

-- =====================================================================
-- 2. 接触を名指す pose を消す (使用数 0 を確かめてから)
-- =====================================================================
DO $$
DECLARE n integer;
BEGIN
  SELECT count(*) INTO n FROM obs_pose
   WHERE pose_slug IN ('spreading_own_labia','hands_on_own_ass','hugging_own_legs');
  IF n > 0 THEN
    RAISE EXCEPTION '削除対象の pose に観測が % 件ある — append-only なので消せない', n;
  END IF;
END $$;

-- pose を参照している派生表も一緒に消す (関節の期待値 / 関与部位。pose が消えるなら不要)
DELETE FROM pose_keypoint
 WHERE pose_slug IN ('spreading_own_labia','hands_on_own_ass','hugging_own_legs');
DELETE FROM pose_region
 WHERE pose_slug IN ('spreading_own_labia','hands_on_own_ass','hugging_own_legs');
DELETE FROM pose WHERE slug IN ('spreading_own_labia','hands_on_own_ass','hugging_own_legs');

-- =====================================================================
-- 3. 残す語に、なぜ残っているかを書く
-- =====================================================================
UPDATE pose SET description =
  '自分の股の間から覗いている。**接触ではないので pose のまま** — '
  '体の構えであって、2 つの部位が触れていることを言う語ではない'
WHERE slug = 'looking_through_own_legs';

UPDATE pose SET description =
  '自分の腰に手を当てている。**新しく書くなら relation touching** '
  '(person_1.hand touching person_1.waist) を使う — どちらの手かが言えるため。'
  'この語が残っているのは既に観測があるからで、append-only の DB では語彙も勝手に消せない'
WHERE slug = 'hand_on_own_hip';

UPDATE pose_category SET description =
  '**非推奨。** 自分の部位どうしの接触は relation で書く (0033) — pose にすると'
  'どちらの手か・何に触れているかが言えない。残っているのは既存の観測が参照しているため'
WHERE slug = 'self_contact';

COMMIT;
