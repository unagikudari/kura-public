-- Migration 0068 — 腕の構えに穴があった (組む / 引き寄せる / 下ろす / 手を合わせる)
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘 (post 555461):
-- 「**腕を組んでいる**だとか、**腕を顔に寄せている**ポーズの表現がない」
--
-- ── 腕の軸が「上げる / 後ろに回す / 広げる」しか無かった ──
-- scope='arms' は arm_up / arms_up / arms_behind_back / arms_behind_head /
-- spread_arms の 5 語で、**全部「外へ / 上へ / 後ろへ」**。
-- **体の前に持ってくる**構えが 1 つも無い。scope='hands' に至っては
-- hand_on_own_hip の 1 語だけだった。
--
-- 実測 (production kura): crossed_arms 2,129 / own_hands_together 2,515 /
-- clenched_hands 2,197 / arms_at_sides 1,138 / hands_on_own_chest 1,259 /
-- hand_on_own_face 1,032。
--
-- ── 接触するものは関係で書く (既に引いてある線) ──
-- `hand_on_own_hip` の説明に「**新しく書くなら relation touching を使う** —
-- どちらの手かが言えるため」と書いてある。この migration もその線を守る:
--   * 顔に**触れている**       → relation touching (hand → face)。どちらの手かが言える
--   * 顔の近くに**引き寄せている** → pose (触れていないので接触先が無い)
-- **触れているかどうかで書き分ける**のであって、pose に接触先を焼き付けない。
-- だから `hands_on_own_chest` / `hand_on_own_face` に当たる語は**足さない** —
-- あれは接触なので関係の仕事。
--
-- ── crossed_arms は関係に潰せない ──
-- 腕を組むと自分の腕どうしが触れるので `touching (arm → arm)` とも書けるが、
-- それでは「**交差している**」が落ちる (触れているだけの構えと区別が付かない)。
-- 構成そのものを言う語が要る。crossed_legs が既にあるのと同じ。
--
-- ロールバック: 追加した pose を DELETE (所見が付いていれば消せない)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO pose(slug, display_name, scope, category) VALUES
  ('crossed_arms',  '腕を組んでいる',        'arms',  'arm_configuration'),
  ('arms_drawn_in', '腕を体の前に引き寄せている','arms', 'arm_configuration'),
  ('arms_at_sides', '両腕を体側に下ろしている','arms',  'arm_configuration'),
  ('hands_together','両手を合わせている',    'hands', 'gesture')
ON CONFLICT (slug) DO NOTHING;

UPDATE pose SET description = v.d FROM (VALUES
  ('crossed_arms',
   '両腕を胸の前で交差させている。**関係に潰せない** — 腕どうしが触れているので '
   '`touching (arm → arm)` とも書けるが、それでは「**交差している**」が落ちて'
   'ただ触れている構えと区別が付かない (crossed_legs が別語なのと同じ)。'
   '実測 crossed_arms 2,129'),
  ('arms_drawn_in',
   '両腕を体の前 — 顔・胸・腹の近く — に引き寄せて畳んでいる。'
   '**触れているなら pose ではなく relation touching で書く** (hand → face など)。'
   'どちらの手かが言えるため (hand_on_own_hip の説明と同じ線)。'
   'こちらは**触れていないが近い**構え。scope=arms は '
   'arm_up / arms_up / arms_behind_back / arms_behind_head / spread_arms と'
   '**全部「外へ / 上へ / 後ろへ」**で、体の前に持ってくる構えが無かった (0068)'),
  ('arms_at_sides',
   '両腕を体側に下ろして力を抜いている。実測 arms_at_sides 1,138'),
  ('hands_together',
   '両手を体の前で合わせている。**指を絡めたり握ったりしているなら relation** '
   '(grasping / holding) — こちらは合わせているだけの構え。実測 own_hands_together 2,515')
) AS v(s,d) WHERE pose.slug = v.s;

COMMIT;
