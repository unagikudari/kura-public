-- Migration 0013 — ejaculation_state に precum を足す
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-28:
-- 「ejaculation_state に precum がない。透明な液が出ている状態」。
--
-- ── なぜ既存の値で代用できないか ──
-- 0010 で入れた 3 値は not_ejaculating / ejaculating / post_ejaculation。
-- カウパー腺液が出ている状態は、
--   * `ejaculating` に寄せる → **射精していないのに射精と書く** (捏造)。
--   * `not_ejaculating` に寄せる → 見えている液体が消える (情報損失)。
-- 0008 の half_erect と同じ形の欠陥で、語彙が観測を規定してしまっている。
--
-- ── 属性 surface_substance との住み分け ──
-- 「陰茎の表面に透明な液が付いている」は surface_substance でも書けるが、
-- `surface_substance` に precum を足すのではなく **ejaculation_state の値**にする:
--   * 出所 (陰茎から出ている) が言えるのは state 側だけ。付着物としてだけ書くと、
--     相手の唾液・愛液が付いているのと区別できない。
--   * 実際に「どこに付いているか」を書きたい場合は surface_substance を併用できる
--     (semen とは別物なので、透明な液には値が要る → 併せて足す)。
-- よって **両方**に足す。state = 出ている / substance = 付いている、で軸が違う。
--
-- 裏付け (kura コーパス実測): precum 25,571 / precum_drip 1,554。
-- 単独の語として定着しており、他の語への言い換えが効かない。
--
-- ── 並び順 ──
-- not_ejaculating < precum < ejaculating < post_ejaculation。
-- precum は射精の前段なので ejaculating の手前に置く。
--
-- ── 運用手順 ──
--   * 単一 transaction。値 INSERT → 説明 → 並び順。
--   * 冪等 (ON CONFLICT DO NOTHING / 同値 UPDATE)。
--   * ロールバック: 追加した attribute_value を DELETE (観測が参照していれば FK が拒否する)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('ejaculation_state', 'precum'),
  ('surface_substance', 'precum')
ON CONFLICT DO NOTHING;

UPDATE attribute_value SET description = v.d FROM (VALUES
  ('ejaculation_state','precum',
   'カウパー腺液が出ている (透明な液が滲む・垂れる)。**射精ではない** — '
   '精液が出ていれば ejaculating を選ぶ'),
  ('surface_substance','precum',
   'カウパー腺液 (透明)。白濁した精液 (semen) とは別の値')
) AS v(a,s,d) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

UPDATE attribute SET description =
  '射精の状態。not_ejaculating / precum / ejaculating / post_ejaculation。'
  'erection_state と同じく penis に付く state 属性で、勃起の度合いとは独立に書ける。'
  '**precum は射精ではない** (透明な液が出ている段階) ので独立した値にしてある'
WHERE slug = 'ejaculation_state';

UPDATE attribute_value SET display_order = v.o FROM (VALUES
  ('ejaculation_state','not_ejaculating',1),('ejaculation_state','precum',2),
  ('ejaculation_state','ejaculating',3),('ejaculation_state','post_ejaculation',4),
  -- surface_substance は precum を semen の直後へ入れ、以降を 1 つずつ後ろへ送る
  ('surface_substance','semen',1),('surface_substance','precum',2),
  ('surface_substance','vaginal_fluid',3),('surface_substance','saliva',4),
  ('surface_substance','sweat',5),('surface_substance','tears',6),
  ('surface_substance','milk',7),('surface_substance','urine',8),
  ('surface_substance','dirt',9),('surface_substance','mud',10),
  ('surface_substance','oil',11)
) AS v(a,s,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

COMMIT;
