-- Migration 0078 — 血を付着物として書けるようにする
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘 (post 893016):「**血の汚れ**」
--
-- ── 体液 10 種のうち血だけ無かった ──
-- `surface_substance` は semen / precum / vaginal_fluid / female_ejaculate / smegma /
-- saliva / sweat / tears / milk / urine / scat と体液を 11 種持つのに、
-- **血が入っていない**。汚れ側 (dirt / mud / oil / hair / soap_foam) にも無い。
--
-- 実測 (production kura): blood 12,917 / blood_on_face 1,129 / nosebleed 867 /
-- blood_from_mouth 446 / blood_on_clothes 404 / blood_splatter 276。
-- flat tag は**部位ごとの合成語** (blood_on_face / blood_from_mouth) を作っているが、
-- ここでは `surface_substance` が `any_region` (0065) なので**部位は所見ごとに指定する** —
-- 組合せを作らない (0069 の surface_marking と同じ)。
--
-- ── 出血しているかは別 (書かない) ──
-- 実測 bleeding は 260 件しかなく、**付いている血と出続けている血を分ける実需が薄い**。
-- 排出 4 種 (0053) のように状態の軸を足すのは、必要になってからにする
-- (**無い軸を先に埋めない** — 0054 で手の形を足さなかったのと同じ判断)。
--
-- ── 鼻血も同じ ──
-- `nosebleed` 867 は「鼻に血が付いている」なので `surface_substance=blood` +
-- 部位 `nose`。**専用の語を作らない**。
--
-- ロールバック: 追加した attribute_value を DELETE。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('surface_substance','blood')
ON CONFLICT DO NOTHING;

UPDATE attribute_value SET
  description = '血。**部位は所見ごとに指定する** (any_region、0065) — '
                'flat tag が blood_on_face 1,129 / blood_from_mouth 446 と'
                '**部位ごとの合成語**を作っているのに対し、組合せを作らない。'
                '**鼻血も専用の語を作らない** — nosebleed 867 は blood + 部位 nose。'
                '**出血しているかは書かない** — 実測 bleeding 260 件で実需が薄く、'
                '必要になってから軸を足す (0078)',
  display_order = 17
WHERE attribute_slug = 'surface_substance' AND value_slug = 'blood';

COMMIT;
