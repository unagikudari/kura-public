-- Migration 0023 — 不随意の動き (震え・痙攣) を表せるようにする
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-29:「trembling も入れて」。
-- 0022 で「置き場が無い」として見送ったものを、置き場を作って入れる。
--
-- ── なぜ既存のどれでもないか ──
--   * `pose` ではない。pose は**体の構え**で、震えは構えではない。立って震えることも
--     座って震えることもあり、pose と独立に共存する (`straddling` と `trembling` は
--     同時に成り立つ)。pose に入れると「構えが 2 つある」ことになり、
--     `same scope conflict → latest wins` の解決規則が誤って片方を捨てる。
--   * `facial_expression` / `mouth_state` ではない。0022 で分けた 2 軸のどちらでもなく、
--     **体全体**の状態。
--   * `surface_substance` ではない。付着物ではない。
-- よって **entity 全体に付く新しい属性**が要る。0022 で「置き場が無い」と書いたのは
-- 正しい観測だったが、結論 (見送る) が誤りだった — 置き場が無いなら作ればよい。
--
-- ── 値 ──
-- 実測: trembling 115,552 / twitching 3,313 / shaking 26。
-- `shaking` は 26 件しかなく `trembling` と区別が付かないので値にしない (寄せる)。
--   * `none`      — 震えていない
--   * `trembling` — 小刻みに震えている
--   * `twitching` — びくつく。断続的で振幅が大きい (trembling は持続的で小刻み)
-- **`orgasm` (36,436) は入れない** — 不随意の動きは絶頂の**兆候の 1 つ**にすぎず、
-- 「震えている」と「絶頂している」は別の主張。後者は観測から直接は言えないことが多く、
-- 混ぜると震えを書いただけで絶頂を主張したことになる (非捏造に反する)。
--
-- ── 表示先 ──
-- 部位に紐づかないので UI では「全体」カテゴリに出す (pregnancy_state を胴体に出すのと
-- 同じ仕組み: annotate_server.ENTITY_ATTR_GROUP)。観測側は region_slug NULL のまま。
--
-- ── 運用手順 ──
--   * 単一 transaction。attribute → 値 → 説明 → 並び順。冪等。
--   * ロールバック: 追加した attribute / attribute_value を DELETE。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('involuntary_motion', 'involuntary motion', 'state', 'categorical', 'entity', 'person')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('involuntary_motion', 'none'),
  ('involuntary_motion', 'trembling'),
  ('involuntary_motion', 'twitching')
ON CONFLICT DO NOTHING;

UPDATE attribute SET description =
  '不随意の体の動き。none / trembling / twitching。**pose (体の構え) とは独立** — '
  '立って震えることも座って震えることもあり、同時に成り立つ。'
  '部位に紐づかない (体全体の状態) ので観測側の region_slug は NULL のまま'
WHERE slug = 'involuntary_motion';

UPDATE attribute_value SET description = v.d FROM (VALUES
  ('involuntary_motion','none',      '震えていない'),
  ('involuntary_motion','trembling', '小刻みに震えている。持続的'),
  ('involuntary_motion','twitching', 'びくついている。断続的で振幅が大きい '
                                     '(trembling は持続的で小刻み)')
) AS v(a,s,d) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

UPDATE attribute_value SET display_order = v.o FROM (VALUES
  ('involuntary_motion','none',1),('involuntary_motion','trembling',2),
  ('involuntary_motion','twitching',3)
) AS v(a,s,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

COMMIT;
