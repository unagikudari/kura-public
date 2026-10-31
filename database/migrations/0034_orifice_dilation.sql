-- Migration 0034 — 開口部の開き具合 (gaping) を書けるようにする
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-29:
-- 「post 729269 で gapping を表現する状態がない」。
--
-- ── 無かった軸: 開口部が開いているか ──
-- `mouth_state` (closed / clenched / slightly_open / open / smiling) は**口だけ**に
-- 用意されていて、**同じ問いを他の開口部に立てる手段が無かった**。
-- 肛門・膣口・尿道口は `wetness` / `pigmentation` / `surface_substance` しか持てず、
-- 「開いたままになっている」が書けない。
-- 実測: gaping 34,329 / spread_pussy 33,495 / spread_anus 26,809 / prolapse 444。
--
-- ── class に付ける (region ごとに属性を作らない) ──
-- 0008 で体毛の量を `pubic_hair_amount` / `armpit_hair_amount` と部位ごとに作らず
-- `class='hair'` の `hair_density` にしたのと同じ判断。開口部ごとに
-- `anus_dilation` / `vaginal_dilation` と作ると、開口部が増えるたびに属性が増える。
--
-- 対象クラスをどうするか: `anus` は perianal + surface_mucosa、`vaginal_opening` と
-- `urethral_opening` は surface_mucosa、`mouth` は mouth + surface_mucosa。
-- surface_mucosa に付けると `vulva` や `inner_lip` にも付いてしまい、
-- **「開く」という問いが立たない部位にまで出る**。
-- よって **`orifice` クラスを新設**し、開口部だけに付ける。
--
-- ── `spread_pussy` / `spread_anus` との違い (混ぜない) ──
-- あれは**手で広げている**という行為で、0033 の `spreading` 関係で書く。
-- こちらは**開いている状態**で、手が離れていても成り立つ (gaping はまさにそれ)。
-- 原因と状態を 1 語に混ぜると「手を離しても開いたまま」が書けなくなる。
--
-- ── `mouth_state` は残す ──
-- 口には `smiling` (口角が上がる) という**開き具合ではない値**があり、開口の軸に
-- 畳めない。口だけ別属性なのは冗長に見えるが、値空間が違うので統合すると
-- 「笑っている口」を dilation の段階に押し込むことになる。
--
-- ── 運用手順 ──
--   * 単一 transaction。class → class_map → attribute → 値 → 説明。冪等。
--   * ロールバック: 追加した attribute / class / map を DELETE。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- =====================================================================
-- 1. 開口部というクラス
-- =====================================================================
INSERT INTO region_class(slug, description) VALUES
  ('orifice',
   '体の開口部。肛門・膣口・尿道口・口。**「開いているか」という問いが立つ部位**だけを'
   '入れる — surface_mucosa に付けると vulva や inner_lip にも出てしまい、'
   '開く/閉じるが意味を持たない部位にまで属性が現れる')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO region_class_map(region_slug, class_slug) VALUES
  ('anus','orifice'), ('vaginal_opening','orifice'),
  ('urethral_opening','orifice'), ('mouth','orifice')
ON CONFLICT DO NOTHING;

-- =====================================================================
-- 2. 開き具合
-- =====================================================================
INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('dilation', 'dilation', 'state', 'categorical', 'class', 'orifice')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('dilation','closed'), ('dilation','slightly_open'),
  ('dilation','open'), ('dilation','gaping'), ('dilation','prolapsed')
ON CONFLICT DO NOTHING;

UPDATE attribute SET description =
  '開口部の開き具合。closed / slightly_open / open / gaping / prolapsed。'
  '**「手で広げている」(relation spreading) とは別** — こちらは状態で、手が離れていても'
  '成り立つ (gaping はまさにそれ)。原因と状態を 1 語に混ぜると「手を離しても開いたまま」が'
  '書けなくなる。class=orifice なので肛門・膣口・尿道口・口に同じ軸で付く',
  display_order = 1
WHERE slug = 'dilation';

UPDATE attribute_value SET description = v.d, display_order = v.o FROM (VALUES
  ('dilation','closed',        '閉じている',                                          1),
  ('dilation','slightly_open', 'わずかに開いている',                                  2),
  ('dilation','open',          '開いている',                                          3),
  ('dilation','gaping',        '大きく開いたままになっている。**支えが無くても閉じない**', 4),
  ('dilation','prolapsed',     '内壁が外へ出ている (脱)',                             5)
) AS v(a,s,d,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

-- 混同しやすい隣の語に区別を書く
UPDATE relation SET description = description ||
  '。**開いている「状態」は属性 dilation** — spreading は手で広げている行為で、'
  '手を離しても開いたままなら dilation=gaping'
WHERE slug = 'spreading' AND description NOT LIKE '%dilation%';

UPDATE attribute SET description = description ||
  '。**口以外の開口部は属性 dilation** (class=orifice)。口だけ別なのは smiling という'
  '開き具合でない値を持つためで、dilation の段階には畳めない'
WHERE slug = 'mouth_state' AND description NOT LIKE '%dilation%';

COMMIT;
