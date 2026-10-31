-- Migration 0077 — 2 者を繋いでいる体液の糸を書けるようにする
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘 (post 886257):
-- 「**涎の二者間のストリング状態**を表現できない」
--
-- ── 付着物では書けない ──
-- 体液について今あるのは `surface_substance` (その部位に**付いている**) だけで、
-- **2 点を繋いでいる**という形が書けない。付着は 1 点の事実、糸は 2 点の関係で、
-- 「口に唾液が付いている」を 2 人分書いても**繋がっていることは言えない**。
--
-- 実測 (production kura): saliva_trail 25,508 / saliva 216,074 / saliva_drip 9,861 /
-- drooling 45,147。**確定的で有名な形** (ADR-0004) なので 1 語で持つ。
--
-- ── 唾液に限らない ──
-- 精液や愛液でも同じ糸ができる。**何の糸かは substance 側で言い**、
-- 語は「糸で繋がっている」だけを言う — `cum_string` / `saliva_string` と
-- 物質ごとに語を作ると 0045 (piercing) と同じ組合せ爆発になる。
--
-- ── 向きが無い ──
-- 糸に「どちらから」は無いので `symmetric`。`touching` と同じ扱い。
--
-- ── 片端は環境でもよい ──
-- 口から垂れて宙で切れている形もある。0052 の `object_surface_slug` がそのまま使えるので、
-- 対象に `air` / `ground` を置ける。
--
-- ロールバック: 追加した relation / 属性を DELETE。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO relation(slug, display_name, category, directionality) VALUES
  ('fluid_string', '体液の糸で繋がっている', 'contact', 'symmetric')
ON CONFLICT (slug) DO NOTHING;

UPDATE relation SET description =
  '**2 点が体液の糸で繋がっている**。口と口、口と陰茎、口と宙 など。'
  '**付着 (surface_substance) では書けない** — あちらは 1 点に付いている事実で、'
  '2 人分書いても**繋がっていることは言えない** (user 指摘 2026-07-31、post 886257)。'
  '**何の糸かは属性 string_substance で言う** — saliva_string / cum_string と'
  '物質ごとに語を作ると組合せ爆発になる (0045 と同じ)。'
  '**向きは無い** (symmetric)。**片端は環境でもよい** — 宙で切れているなら object に air。'
  '実測 saliva_trail 25,508'
WHERE slug = 'fluid_string';

INSERT INTO relation_applicability(relation_slug, role, participant_kind, participant_key) VALUES
  ('fluid_string', 'participant', 'region', 'any')
ON CONFLICT DO NOTHING;

-- 何の糸か。値は surface_substance と揃える (注釈者が考え直さずに済む)
INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('string_substance', '糸になっている体液', 'state', 'categorical', 'entity', 'person')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('string_substance','saliva'),
  ('string_substance','semen'),
  ('string_substance','vaginal_fluid'),
  ('string_substance','other')
ON CONFLICT DO NOTHING;

UPDATE attribute SET description =
  '`fluid_string` で繋がっているのが**何か** (0077)。saliva / semen / vaginal_fluid / other。'
  '**値は surface_substance と揃えてある** — 同じ物質が別の名前になると注釈者が'
  '毎回考え直すことになる (0050 で排出の値空間を揃えたのと同じ)。'
  '**NULL は「言っていない」**、`other` は「見たが判らない」',
  display_order = 1
WHERE slug = 'string_substance';

UPDATE attribute SET description = coalesce(description,'') ||
  '。**2 点を繋いでいる糸は関係 fluid_string** (0077) — こちらは 1 点に付いている事実'
WHERE slug = 'surface_substance' AND coalesce(description,'') NOT LIKE '%fluid_string%';

COMMIT;
