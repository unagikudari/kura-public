-- Migration 0014 — surface_substance に smegma を足す
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-28:
-- 「汚れの種類に smegma がない」。
--
-- ── なぜ既存の値で代用できないか ──
-- 0010 で入れた surface_substance は体液 (semen / vaginal_fluid / saliva / sweat /
-- tears / milk / urine) と外的な汚れ (dirt / mud / oil) の 2 系統しか無い。恥垢は
--   * `semen` に寄せる → **別の物質を精液と書く** (捏造)。しかも出所が違う。
--   * `dirt` に寄せる → 何が付いているか判らなくなる (情報損失)。
-- 0008 の half_erect / 0013 の precum と同じ形の欠陥。
--
-- ── 色素沈着との混同を description で潰す ──
-- 恥垢は **pigmentation (色素沈着) と誤認されやすい**。判別基準は既に確立してある
-- (memory smegma-vs-dark-pigmentation-visual-criterion): 恥垢は白〜黄の**付着物**で
-- 縁がはっきりし、めくれた皮の内側や溝に溜まる。色素沈着は**皮膚そのものの色**で
-- 境界がぼやけ、面として広がる。両者は独立に観測できる (色素沈着した部位に恥垢が
-- 付いていることもある) ので、値ではなく **属性が別**なのが正しい。
-- この区別を値の説明に書いて、注釈時に迷わないようにする。
--
-- ── 運用手順 ──
--   * 単一 transaction。値 INSERT → 説明 → 並び順。
--   * 冪等 (ON CONFLICT DO NOTHING / 同値 UPDATE)。
--   * ロールバック: 追加した attribute_value を DELETE (観測が参照していれば FK が拒否する)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('surface_substance', 'smegma')
ON CONFLICT DO NOTHING;

UPDATE attribute_value SET description = v.d FROM (VALUES
  ('surface_substance','smegma',
   '恥垢。白〜黄の付着物で、めくれた包皮の内側や陰唇の溝に溜まる。'
   '**色素沈着 (属性 pigmentation) と混同しない** — 恥垢は「付いている物」なので縁が'
   'はっきりして溝に沿う。色素沈着は「皮膚そのものの色」なので境界がぼやけて面で広がる'),
  -- 対になる側にも書いておく (どちらから見ても迷わないように)
  ('pigmentation','dark',
   '濃い。**皮膚そのものの色**が濃い状態。白〜黄の付着物が溜まっているだけなら'
   'これではなく surface_substance=smegma')
) AS v(a,s,d) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

-- 体液 → 分泌由来の付着物 → 外的な汚れ の順。smegma は precum の次に置く。
UPDATE attribute_value SET display_order = v.o FROM (VALUES
  ('surface_substance','semen',1),('surface_substance','precum',2),
  ('surface_substance','vaginal_fluid',3),('surface_substance','smegma',4),
  ('surface_substance','saliva',5),('surface_substance','sweat',6),
  ('surface_substance','tears',7),('surface_substance','milk',8),
  ('surface_substance','urine',9),('surface_substance','dirt',10),
  ('surface_substance','mud',11),('surface_substance','oil',12)
) AS v(a,s,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

COMMIT;
