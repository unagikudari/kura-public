-- Migration 0063 — 石鹸の泡を書けるようにする
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘:
-- 「post 505110 に石鹸の泡があるが表現できない」
--
-- ── 付着物に体液と汚れしか無かった ──
-- `surface_substance` の値は semen / precum / vaginal_fluid / female_ejaculate /
-- smegma / saliva / sweat / tears / milk / urine / scat / dirt / mud / oil / hair の 15。
-- **体から出たものと汚れだけ**で、**外から付けたもの**が 1 つも無かった。
-- 泡は体液でも汚れでもないので、既存のどれにも寄せられない。
--
-- 実測 (production kura): soap_bubbles 3,440 / soap 1,766 / soapland 968 /
-- soap_bottle 460 / foam 194。flat tag 側は「泡」「石鹸」「ソープランド」「ボトル」を
-- **同じ平面に並べている** — 付着物なのか、物なのか、場所なのかが語からは判らない。
-- ここでは **体に付いている泡**だけを付着物として書き、石鹸そのもの (容器・固形) は
-- object、浴室は場所の話として分ける。
--
-- ── `hair` の隣に置く理由 ──
-- 0044 で transferred hair を「汚れ付着物の一種」にしたとき (user 判断)、
-- **由来が本人でなくても付着物**という線を引いた。泡も同じ — 由来は石鹸だが、
-- 見えているのは「その部位に付いている」という事実。
--
-- ── 泡そのものは物ではない ──
-- `object_type` に入れない。身につけているものではないし、
-- 0052 で「空中は物ではない」として object_type に入れなかったのと同じ線。
--
-- ロールバック: 追加した attribute_value を DELETE (所見が付いていれば消せない)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('surface_substance','soap_foam')
ON CONFLICT DO NOTHING;

UPDATE attribute_value SET
  description = '石鹸の泡。**体から出たものではない付着物**で、既存の値 (体液・汚れ) の'
                'どれにも寄せられなかった (0063、user 指摘 post 505110)。'
                '**石鹸そのもの (容器・固形) は object**、浴室は場所の話 — '
                'flat tag は soap_bubbles 3,440 / soap 1,766 / soapland 968 / '
                'soap_bottle 460 を同じ平面に並べていて区別が付かない。'
                '**泡そのものは物ではない**ので object_type には入れない (0052 と同じ線)',
  display_order = 16
WHERE attribute_slug = 'surface_substance' AND value_slug = 'soap_foam';

UPDATE attribute SET description = coalesce(description,'') ||
  '。**外から付いたものも入る** (0063) — 石鹸の泡のように由来が体でなくてよい '
  '(0044 で transferred hair を付着物にしたのと同じ線)'
WHERE slug = 'surface_substance' AND coalesce(description,'') NOT LIKE '%外から付いたもの%';

COMMIT;
