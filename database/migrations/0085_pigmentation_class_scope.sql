-- Migration 0085 — pigmentation を class 限定にし、毛への肌属性の誤適用を構造的に消す
--
-- user 指摘 (2026-08-01、post 959560): モデルが pubic_hair に pigmentation や
-- smegma を検知する。@s2 実測: pigmentation=dark @pubic_hair ×8、smegma ×3、
-- 「毛に毛が付着」(surface_substance=hair @pubic_hair) ×6。
--
-- 原因: pigmentation が applies_kind='any_region' で毛にも問われていた。
-- 対処: applies_kind='class' + **空白区切りの複数 class** (region_attr_map 解決側が
-- split して読む — annotate_server / model_runner 同時変更)。
-- 対象 class = 人間の使用実績 85 件が全て収まる 4 class:
--   surface_skin (areola/face/…) + external_genital (glans/labia/scrotum/…)
--   + surface_mucosa + perianal (anus)
-- hair / eye / joint / bone_landmark 等は構造的に対象外になる。
--
-- surface_substance は any_region のまま (毛に精液は正当)。ただし値の基準で
-- smegma と hair の誤適用を封じる。
--
-- ロールバック: applies_kind='any_region', applies_key='any' に戻し、値説明の
-- 追記を削る。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

UPDATE attribute
   SET applies_kind = 'class',
       applies_key  = 'surface_skin external_genital surface_mucosa perianal'
 WHERE slug = 'pigmentation' AND applies_kind = 'any_region';

UPDATE attribute_value SET description = coalesce(description,'') ||
  '。**皮膚・粘膜にだけ言う — 毛 (pubic_hair 等) の色はこれではない** (毛は元々濃い)'
WHERE attribute_slug='pigmentation' AND value_slug IN ('dark','very_dark')
  AND coalesce(description,'') NOT LIKE '%毛 (pubic_hair%';

UPDATE attribute_value SET description = coalesce(description,'') ||
  '。**毛 (pubic_hair 等) や体表一般には言わない** — 溜まる場所は包皮の内側・陰唇の溝'
WHERE attribute_slug='surface_substance' AND value_slug='smegma'
  AND coalesce(description,'') NOT LIKE '%毛 (pubic_hair%';

UPDATE attribute_value SET description = coalesce(description,'') ||
  '。**抜け毛が肌・物の表面に付いているとき** — 毛の部位そのもの (pubic_hair 等) には言わない'
WHERE attribute_slug='surface_substance' AND value_slug='hair'
  AND coalesce(description,'') NOT LIKE '%抜け毛%';

COMMIT;
