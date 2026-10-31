-- Migration 0015 — surface_substance に scat を足す
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-28:「汚れに scat がない」。
--
-- ── なぜ既存の値で代用できないか ──
-- 0010 の surface_substance には排泄物として urine しか無い。糞便は
--   * `dirt` に寄せる → 何が付いているか判らなくなる (情報損失)。土埃と同じ扱いになる。
--   * `mud` に寄せる → **別の物質を泥と書く** (捏造)。見た目が近いだけに実害が大きい。
-- urine が値としてあるのに scat が無いのは、単に語彙の穴。
-- 裏付け (kura コーパス実測): scat 1,362。
--
-- ── 並び順 ──
-- 排泄物どうしを隣に置く (urine → scat)。以降の外的な汚れを 1 つずつ後ろへ送る。
--
-- ── 運用手順 ──
--   * 単一 transaction。値 INSERT → 説明 → 並び順。
--   * 冪等 (ON CONFLICT DO NOTHING / 同値 UPDATE)。
--   * ロールバック: 追加した attribute_value を DELETE (観測が参照していれば FK が拒否する)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('surface_substance', 'scat')
ON CONFLICT DO NOTHING;

UPDATE attribute_value SET description = v.d FROM (VALUES
  ('surface_substance','scat',
   '糞便。**dirt (土埃) や mud (泥) に寄せない** — 見た目が近くても別の物質で、'
   '出所 (排泄) が違う')
) AS v(a,s,d) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

UPDATE attribute_value SET display_order = v.o FROM (VALUES
  ('surface_substance','semen',1),('surface_substance','precum',2),
  ('surface_substance','vaginal_fluid',3),('surface_substance','smegma',4),
  ('surface_substance','saliva',5),('surface_substance','sweat',6),
  ('surface_substance','tears',7),('surface_substance','milk',8),
  ('surface_substance','urine',9),('surface_substance','scat',10),
  ('surface_substance','dirt',11),('surface_substance','mud',12),
  ('surface_substance','oil',13)
) AS v(a,s,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

COMMIT;
