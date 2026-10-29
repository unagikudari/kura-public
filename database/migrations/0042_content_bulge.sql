-- Migration 0042 — 体表に中身の形が浮き出ること (腹部の盛り上がり等) を書けるようにする
--
-- plan kura-1.0-implementation、2026-07-30 の user 指摘:
-- 「腹部の盛り上がりを入れよう」
--
-- ── `belly_shape` では書けない ──
-- 既に `belly_shape` (flat / soft / rounded / distended) があるが、あれは
-- **腹の輪郭そのもの**。指摘されているのは**中身の形が体表に浮き出る**現象で、別の事実:
--   * distended (張り出している) — 妊娠・膨満。中身の形は判らない
--   * 盛り上がり — **何が入っているかの形が判る**。手を離しても残るわけではなく、
--     中身が動けば形も動く
-- 1 語に混ぜると「丸く張り出しているが中身の形は判らない」が書けなくなる。
--
-- ── 腹だけの語にしない (ここが要点) ──
-- 実測 (production kura): stomach_bulge 34,222 / cheek_bulge 4,680 / throat_bulge 431。
-- **同じ現象が腹・喉・頬で起きている**。`stomach_bulge` のように部位ごとの語を作ると、
-- 部位が増えるたびに語が増える (0008 で体毛を部位ごとに作らず `hair_density` に
-- したのと同じ判断)。
--
-- そこで **`soft_tissue` クラスを新設**し、そのクラスに `content_bulge` を付ける。
-- 対象は abdomen / throat / cheek — **中身が押して形が出る柔らかい部位**だけ。
-- surface_skin に付けると腕や背中にも出てしまい、問いが立たない部位に属性が現れる。
--
-- ── 何が入っているかは書かない ──
-- 原因は既に relation で表せる (penis inserting_into vaginal_opening 等)。
-- 属性側に「penis による」「液体による」と書くと**原因が 2 か所に住む**。
-- ここは**見え方の強さ**だけを段階で持つ。
--
-- ── 値 ──
-- none / faint / clear / silhouetted。
--   * faint — 盛り上がっているのは判るが形は判らない
--   * clear — 形が判る
--   * silhouetted — 輪郭がはっきり出ている (中の物体の形が追える)
-- NULL は「言っていない」。**none を置くのは「見たが無かった」と「見ていない」を
-- 分けるため** (非捏造)。
--
-- ── 運用手順 ──
--   * 単一 transaction。class → class_map → attribute → 値 → 説明。冪等。
--   * ロールバック: 追加した attribute / class / map を DELETE。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- =====================================================================
-- 1. 中身が押して形が出る部位というクラス
-- =====================================================================
INSERT INTO region_class(slug, description) VALUES
  ('soft_tissue',
   '中身が押すと体表に形が出る柔らかい部位。腹・喉・頬。'
   '**surface_skin に付けない** — 腕や背中では「中身の形が浮き出る」という問いが立たず、'
   '属性だけが並ぶことになる')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO region_class_map(region_slug, class_slug) VALUES
  ('abdomen','soft_tissue'), ('throat','soft_tissue'), ('cheek','soft_tissue')
ON CONFLICT DO NOTHING;

-- =====================================================================
-- 2. 中身の形が浮き出ているか
-- =====================================================================
INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('content_bulge', '中身の盛り上がり', 'state', 'categorical', 'class', 'soft_tissue')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('content_bulge','none'), ('content_bulge','faint'),
  ('content_bulge','clear'), ('content_bulge','silhouetted')
ON CONFLICT DO NOTHING;

UPDATE attribute SET description =
  '**中身の形が体表に浮き出ているか**。腹・喉・頬に同じ軸で付く (class=soft_tissue) — '
  'stomach_bulge / throat_bulge / cheek_bulge と部位ごとの語を作らないため。'
  '**輪郭そのものは belly_shape** — 丸く張り出していても中身の形が判らないことがあり、'
  '1 語に混ぜるとそれが書けなくなる。'
  '**何が入っているかは書かない** — 原因は relation (inserting_into 等) が持っているので、'
  'ここに書くと原因が 2 か所に住む。NULL は「言っていない」、none は「見たが無かった」',
  display_order = 2
WHERE slug = 'content_bulge';

UPDATE attribute_value SET description = v.d, display_order = v.o FROM (VALUES
  ('content_bulge','none',        '浮き出ていない',                                   1),
  ('content_bulge','faint',       '盛り上がっているのは判るが**形は判らない**',        2),
  ('content_bulge','clear',       '中身の形が判る',                                   3),
  ('content_bulge','silhouetted', '輪郭がはっきり出て**中の物体の形が追える**',        4)
) AS v(a,s,d,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

-- 隣の語に区別を書く
UPDATE attribute SET description = description ||
  '。**中身の形が浮き出るのは別軸** (content_bulge) — こちらは腹の輪郭だけを言う'
WHERE slug = 'belly_shape' AND description NOT LIKE '%content_bulge%';

COMMIT;
