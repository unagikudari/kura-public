-- Migration 0071 — 拘束を書けるようにし、羽交い締めを型で入れられるようにする
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘 (post 800021):
-- 「**羽交い締め**の語彙がない」
--
-- ── 拘束そのものが無かった ──
-- interaction の関係は grasping / holding / covering / occluding / spreading /
-- inserting_into / striking / biting / strangling / 排出 4 語で、
-- **「動きを封じている」を言う語が 1 つも無かった**。
-- 実測 (production kura): restrained 27,841 / full_nelson 13,066 / bondage 18,552。
-- 羽交い締めだけの話ではない。
--
-- ── grasping / holding との境界 ──
--   grasping    — 手指で握って把持している (握り方の話)
--   holding     — 腕全体で抱えている       (抱え方の話)
--   restraining — **相手の動きを封じている** (効果の話)
-- 同じ手の形でも「支えている」と「押さえつけている」は**別の事実**なので分ける。
-- 握らずに体重で押さえる形もあり、grasping には畳めない。
--
-- ── 羽交い締めという語は作らない (ここが要点) ──
-- 0054 で punch / kick を作らず `striking` + 主体の部位にしたのと同じ。
-- 羽交い締めは**部位の組み合わせで言える**:
--     behind      ($s → $o)                    後ろにいる
--     restraining ($s/arm → $o/axilla、左右)    腕を腋の下に通して封じている
-- 語にすると「腋の下を通しているか」「後ろからか」が語の中に埋まり、
-- **少し違う拘束が全部書けなくなる** (手首を掴む / 体重で押さえる / 縄で縛る)。
-- 代わりに **phrase_macro に `full nelson` を置く** (0064) — 入力は 1 行で済み、
-- 保存される所見は分解されたまま。**入力の形と保存の形を分ける**という 0064 の判断。
--
-- ── 主体は人とは限らない ──
-- 縄や拘束具も拘束する。0030 で entity_kind に object を入れてあるので、
-- subject を object の entity にすれば `bondage` 18,552 もこの語で書ける。
--
-- ロールバック: 追加した relation / 型を DELETE。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO relation(slug, display_name, category, directionality) VALUES
  ('restraining', '拘束している', 'interaction', 'directed')
ON CONFLICT (slug) DO NOTHING;

UPDATE relation SET description =
  '**相手の動きを封じている**。grasping (握って把持) / holding (腕で抱える) とは'
  '**効果の話**で別 — 同じ手の形でも「支えている」と「押さえつけている」は別の事実で、'
  '握らずに体重で押さえる形もある。'
  '**どう封じているかは部位が言う** — 腕を腋の下に通せば羽交い締め '
  '(restraining: arm → axilla + behind)、手首を掴むなら wrist。'
  '**羽交い締めという語は作らない** (0054 で punch/kick を作らなかったのと同じ) — '
  '語にすると少し違う拘束が全部書けなくなる。入力は phrase_macro の `full nelson` で 1 行。'
  '**主体は人とは限らない** — 縄や拘束具も拘束する (0030 で entity_kind に object がある)。'
  '実測 restrained 27,841 / full_nelson 13,066 / bondage 18,552'
WHERE slug = 'restraining';

INSERT INTO relation_applicability(relation_slug, role, participant_kind, participant_key) VALUES
  ('restraining', 'subject', 'region', 'any'),
  ('restraining', 'object',  'region', 'any')
ON CONFLICT DO NOTHING;

UPDATE relation SET description = coalesce(description,'') ||
  '。**動きを封じているなら restraining** (0071) — こちらは握り方の話'
WHERE slug = 'grasping' AND coalesce(description,'') NOT LIKE '%restraining%';

UPDATE relation SET description = coalesce(description,'') ||
  '。**動きを封じているなら restraining** (0071) — こちらは抱え方の話'
WHERE slug = 'holding' AND coalesce(description,'') NOT LIKE '%restraining%';

-- 入力の近道 (保存される所見は分解されたまま)
INSERT INTO phrase_macro(slug, phrase, aliases, description, display_order) VALUES
 ('full_nelson','full nelson', ARRAY['羽交い締め','はがいじめ'],
  '$s が $o の後ろから両腕を腋の下に通して動きを封じている。'
  '**羽交い締めという語は作っていない** — behind + restraining (arm → axilla) の'
  '組み合わせで、少し違う拘束も同じ語彙で書けるようにしてある (0071)', 25)
ON CONFLICT (slug) DO NOTHING;

INSERT INTO phrase_macro_relation
  (macro_slug, ord, relation_slug, subject_ref, subject_region, subject_laterality,
   object_ref, object_region, object_laterality) VALUES
 ('full_nelson',1,'behind',     '$s',NULL,  NULL,   '$o',NULL,    NULL),
 ('full_nelson',2,'restraining','$s','arm','left',  '$o','axilla','left'),
 ('full_nelson',3,'restraining','$s','arm','right', '$o','axilla','right')
ON CONFLICT DO NOTHING;

COMMIT;
