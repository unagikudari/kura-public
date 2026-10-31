-- Migration 0080 — 頭髪が body_region に無かった
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘 (post 386556):
-- 「**髪をつかむ**が表現できない。**頭をつかむとは違う**」
--
-- ── hair クラスに頭髪だけが無かった ──
-- primary_class='hair' の部位は armpit_hair / beard / eyebrow / eyelash /
-- mustache / perianal_hair / perineal_hair / pubic_hair / sideburns の 9 つで、
-- **体中の毛が揃っているのに頭の毛だけが無い**。
-- `scalp` (頭皮) はあるが、あれは surface_skin であって毛ではない。
-- だから「髪を掴む」を書こうとすると `head` か `scalp` になり、
-- **user 指摘のとおり「頭を掴む」と区別が付かない**。
--
-- ── 掴む対象として区別が要る ──
--   髪を掴む — grasping (hand → head_hair)。引っ張れる、痛みの向きが違う
--   頭を掴む — grasping (hand → head)。頭部そのものを押さえる
-- 同じ手の位置でも**別の事実**なので、部位で分ける (0068 で crossed_arms を
-- touching に潰さなかったのと同じ — 潰すと区別が落ちる)。
--
-- ── flat tag 側は薄い ──
-- 実測 (production kura): hair_pull 174 / hair_pulled_back 2,703。
-- `hair_grab` / `grabbing_hair` は **0 件**。掴む対象としての髪を言う語がほぼ無い。
--
-- ── 髪型は部位ではない (作らない) ──
-- ponytail / twintails のような**髪型**は足さない。あれは形の話で、
-- 掴む対象としての部位ではない。必要になったら属性として別に考える。
--
-- ── 親は scalp ──
-- 毛は頭皮から生えるので parent=scalp。他の毛も同じ形 (pubic_hair の parent は
-- pubic_region、armpit_hair は axilla)。
--
-- ロールバック: 追加した region を DELETE (所見が付いていれば消せない)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO body_region(slug, display_name, parent_slug, laterality, primary_class, description) VALUES
  ('head_hair','頭髪','scalp','midline','hair',
   '頭の毛。**掴む対象として head / scalp と区別する** (0080、user 指摘) — '
   '髪を掴む (grasping: hand → head_hair) と頭を掴む (hand → head) は'
   '**同じ手の位置でも別の事実**。`scalp` は頭皮 (surface_skin) であって毛ではない。'
   'hair クラスには腋毛・眉・睫毛・髭・陰毛が揃っていたのに**頭の毛だけが無かった**。'
   '**髪型 (ponytail 等) は部位ではない**ので作らない — あれは形の話。'
   '実測 hair_pull 174 / hair_grab 0 件 — flat tag 側は掴む対象としての髪をほぼ持たない')
ON CONFLICT (slug) DO NOTHING;

UPDATE body_region SET description = coalesce(description,'') ||
  '。**毛は head_hair** (0080) — こちらは頭皮 (皮膚) で、掴む対象としては別'
WHERE slug = 'scalp' AND coalesce(description,'') NOT LIKE '%head_hair%';

COMMIT;
