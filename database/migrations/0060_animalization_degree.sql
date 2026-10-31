-- Migration 0060 — 獣化には段階があり、種族が段階を確定させることがある
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘:
-- 「**けもの化には段階があるが、umamusume は一つのキャラクターとしてその段階が
--   確定されている**」
--
-- ── 段階を言う軸が無かった ──
-- 0040 で `motif_species` (何に似せてあるか) を入れたが、**どこまで似せてあるか**が
-- 書けなかった。実測 (production kura): animal_ears 66,827 / tail 41,721 /
-- furry 5,620 / furry_female 2,166 / body_fur 1,520 / animal_hands 1,107 /
-- animal_nose 775 — **耳だけ**から**全身**まで連続している。
-- 「猫耳の人間」と「顔以外ほぼ猫」を同じ human + motif=cat で書くと、
-- **その差が消える**。
--
-- ── 種族が段階を決めるなら書かない (ここが要点) ──
-- user 指摘のとおり **umamusume は作品として段階が確定している**。
-- minotaur (獣頭)・centaur (下半身が馬)・mermaid (下半身が魚)・dog (全身) も同じで、
-- **species を選んだ時点で段階は決まる**。
-- そこに段階を別途書くと**二重の真実**になる (orientation を width/height から導いた
-- 0019、亀頭の露出を foreskin_retraction から導く 0049 と同じ判断)。
--
-- **段階を書くのは `species=human` + `motif_species` のときだけ。**
-- あれは「人だが何かに似せてある」という形で、**どこまで似せてあるかが自由**だから。
--
-- ── 値 ──
-- ears_tail / extremities / pelage / head。**全身は species 側**なので値に入れない
-- (全身が犬なら species=dog であって human ではない)。
--
-- ロールバック: 追加した attribute / attribute_value を DELETE。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('animalization_degree', '獣化の段階', 'appearance', 'categorical', 'entity', 'person')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('animalization_degree','ears_tail'),
  ('animalization_degree','extremities'),
  ('animalization_degree','pelage'),
  ('animalization_degree','head')
ON CONFLICT DO NOTHING;

UPDATE attribute SET description =
  '**どこまで獣に似せてあるか**。ears_tail / extremities / pelage / head。'
  '**種族が段階を決めるなら書かない** — umamusume / minotaur / centaur / mermaid / dog は '
  'species を選んだ時点で段階が確定しており (user 指摘 2026-07-31)、'
  'そこに別途書くと二重の真実になる (0019 / 0049 と同じ判断)。'
  '**書くのは species=human + motif_species のときだけ** — '
  'あれは「人だが何かに似せてある」形で、どこまで似せてあるかが自由だから。'
  '**全身は値に入れない** — 全身が犬なら species=dog であって human ではない。'
  'NULL は「言っていない」',
  display_order = 3
WHERE slug = 'animalization_degree';

UPDATE attribute_value SET description = v.d, display_order = v.o FROM (VALUES
  ('animalization_degree','ears_tail',
   '耳と尾だけ。体は人のまま (実測 animal_ears 66,827 / tail 41,721 が最も多い)', 1),
  ('animalization_degree','extremities',
   '手足にも及ぶ。獣の手・足・爪 (animal_hands 1,107)',                          2),
  ('animalization_degree','pelage',
   '体毛や鱗が広い範囲を覆う (body_fur 1,520 / furry 5,620)',                    3),
  ('animalization_degree','head',
   '頭部が獣。**ただし頭が完全に獣なら species 側で書けることが多い** '
   '(minotaur / werewolf) — 種族が無い動物のときだけこの値を使う',               4)
) AS v(a,s,d,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

UPDATE attribute SET description = coalesce(description,'') ||
  '。**どこまで似せてあるかは animalization_degree** (0060) — '
  'ただし種族が段階を決めるなら書かない'
WHERE slug = 'motif_species' AND coalesce(description,'') NOT LIKE '%animalization_degree%';

COMMIT;
