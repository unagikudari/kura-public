-- Migration 0022 — 「耐えている」「歯を食いしばっている」を表せるようにする
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-29:
-- 「表情に耐えているがない」「歯を食いしばっているとか」。
--
-- ── 2 つの軸に分ける (0011 と同じ切り方) ──
-- 0011 では `rolling_eyes` / `half-closed_eyes` を **表情ではなく目の状態**として
-- `eye_state` に落とした。白目を剥きながら笑っていることがあるので、表情と目の状態は
-- 独立に観測できるからである。ここでも同じ判断をする:
--   * **歯を食いしばっている = 口の形** → `mouth_state` に `clenched`
--   * **耐えている = 情動**           → `facial_expression` に `enduring`
-- 食いしばりながら怒っていることも、笑いながら食いしばっていることもある。1 つの値に
-- 混ぜると「口が閉じているのか」と「何を感じているのか」が分離できなくなる。
--
-- ── `pain` も併せて足す ──
-- 現行 20 語に苦痛が無かった。`enduring` (こらえている) と `pain` (苦痛が出ている) は
-- 別の状態で、前者は「出さないようにしている」後者は「出ている」。寄せると区別が消える。
--
-- 裏付け (kura コーパス実測): clenched_teeth 69,963 / restrained 27,791 /
-- furrowed_brow 1,563 / wince 1,186 / pain 580 / grimace 466。
-- `clenched_teeth` は 7 万件近くあり、表情語彙の穴としては大きい。
--
-- ── 足さないもの ──
-- `trembling` (115,552) は表情でも口の形でもなく**体全体の状態**で、置き場が無い
-- (pose でも attribute でもない)。ここでは足さず、語彙の穴として記録に留める。
-- `furrowed_brow` は `eyebrow` region の属性になるべきもので、1,563 件と少ないので見送る。
--
-- ── 運用手順 ──
--   * 単一 transaction。値 INSERT → 説明 → 並び順の再割り当て。
--   * 冪等 (ON CONFLICT DO NOTHING / 同値 UPDATE)。
--   * ロールバック: 追加した attribute_value を DELETE (観測が参照していれば FK が拒否)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- =====================================================================
-- 1. 情動 — 耐えている / 苦痛
-- =====================================================================
INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('facial_expression', 'enduring'),
  ('facial_expression', 'pain')
ON CONFLICT DO NOTHING;

-- =====================================================================
-- 2. 口の形 — 食いしばり
-- =====================================================================
INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('mouth_state', 'clenched')
ON CONFLICT DO NOTHING;

UPDATE attribute_value SET description = v.d FROM (VALUES
  ('facial_expression','enduring',
   'こらえている。苦痛や快感を**出さないようにしている**顔。'
   '**出てしまっている場合は pain / ecstatic**。歯の食いしばりは口の形なので '
   'mouth_state=clenched で別に書く (併記できる)'),
  ('facial_expression','pain',
   '苦痛が出ている。**こらえている場合は enduring**'),
  ('mouth_state','clenched',
   '歯を食いしばっている。**口の形であって情動ではない** — こらえているのか怒って'
   'いるのかは facial_expression 側で書く。歯が見えるかどうかは問わない')
) AS v(a,s,d) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

-- =====================================================================
-- 3. 並び順の再割り当て (中立 → 快 → 負の感情。pain/enduring は負の側)
-- =====================================================================
UPDATE attribute_value SET display_order = v.o FROM (VALUES
  ('facial_expression','neutral',1),('facial_expression','expressionless',2),
  ('facial_expression','smile',3),('facial_expression','grin',4),
  ('facial_expression','laughing',5),('facial_expression','smug',6),
  ('facial_expression','ecstatic',7),('facial_expression','ahegao',8),
  ('facial_expression','embarrassed',9),('facial_expression','confused',10),
  ('facial_expression','surprised',11),('facial_expression','pleading',12),
  ('facial_expression','enduring',13),('facial_expression','pain',14),
  ('facial_expression','scared',15),('facial_expression','crying',16),
  ('facial_expression','sad',17),('facial_expression','pout',18),
  ('facial_expression','annoyed',19),('facial_expression','angry',20),
  ('facial_expression','disgust',21),('facial_expression','bored',22),
  -- 閉じている → 開いている の順。clenched は closed より強く閉じている
  ('mouth_state','clenched',1),('mouth_state','closed',2),
  ('mouth_state','slightly_open',3),('mouth_state','open',4),
  ('mouth_state','smiling',5)
) AS v(a,s,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

COMMIT;
