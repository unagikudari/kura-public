-- Migration 0026 — 体格 (筋肉量・脂肪量) を表せるようにする
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-29:
-- 「筋肉量が表現できない。筋肉質かどうか。肥満体かどうか」。
--
-- ── なぜ 1 つの軸にしないか ──
-- 「筋肉質」と「肥満」は**独立に増減する**。太っていて筋肉もある体 (力士、bara)、
-- 痩せていて筋肉が無い体、痩せていて筋肉がある体、太っていて筋肉が無い体 — 4 通り
-- すべてが実在する。1 つの `body_type` に skinny/muscular/fat と並べると、
-- **1 軸に潰れて「筋肉質な肥満」が書けなくなる**。
-- コーパスも別語として持っている: muscular 33,377 / fat 93,001 + fat_man 108,161 /
-- plump 22,174 / obese 8,635 / toned 4,832 / skinny 1,522。
-- よって **2 つの属性**にする:
--   * `muscularity` — 筋肉の量。none / slight / toned / muscular / massive
--   * `adiposity`   — 脂肪の量。gaunt / slim / average / plump / obese
--
-- ── 部位ではなく人物に付ける ──
-- 体格は体全体の性質で、特定の部位のものではない (`abs` 15,797 のような「腹筋が見える」は
-- 部位 `abdomen` の話だが、それは筋肉量が高いことの**現れ**であって体格そのものではない)。
-- よって applies_kind='entity'。UI では「全体」カテゴリに出す。
--
-- ── 足さないもの ──
--   * `thick_thighs` (29,861) / `wide_hips` (2,807) — **体格ではなく部位の大きさ**。
--     部位ごとのサイズ属性が要る話で、体格の軸に混ぜると「太ももだけ太い」が
--     肥満として記録されてしまう。別の穴として記録に留める。
--   * `curvy` (9,834) / `petite` (6,547) — 体型の印象語で、筋肉量とも脂肪量とも
--     独立でない (curvy は脂肪分布 + 骨格)。分解できるまで足さない。
--   * `bara` (535) — 画風・ジャンルであって体格ではない。
--
-- ── 運用手順 ──
--   * 単一 transaction。attribute → 値 → 説明 → 並び順。冪等。
--   * ロールバック: 追加した attribute / attribute_value を DELETE。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('muscularity', 'muscularity', 'appearance', 'categorical', 'entity', 'person'),
  ('adiposity',   'adiposity',   'appearance', 'categorical', 'entity', 'person')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('muscularity','none'),   ('muscularity','slight'), ('muscularity','toned'),
  ('muscularity','muscular'),('muscularity','massive'),
  ('adiposity','gaunt'),    ('adiposity','slim'),     ('adiposity','average'),
  ('adiposity','plump'),    ('adiposity','obese')
ON CONFLICT DO NOTHING;

UPDATE attribute SET description = v.d FROM (VALUES
  ('muscularity',
   '筋肉の量。none / slight / toned / muscular / massive。**脂肪の量 (adiposity) とは'
   '独立** — 太っていて筋肉もある体 (力士) も、痩せていて筋肉が無い体もある。'
   '1 軸に潰すと「筋肉質な肥満」が書けなくなる'),
  ('adiposity',
   '脂肪の量。gaunt / slim / average / plump / obese。**筋肉の量 (muscularity) とは独立**。'
   '太ももだけ太い等の**部位のサイズ**はここではない (体格は体全体の性質)')
) AS v(s,d) WHERE attribute.slug = v.s;

UPDATE attribute_value SET description = v.d FROM (VALUES
  ('muscularity','none',    '筋肉の起伏が見えない'),
  ('muscularity','slight',  'わずかに引き締まっている'),
  ('muscularity','toned',   '筋肉の輪郭が見える (腹筋の線が出る程度)'),
  ('muscularity','muscular','明確に筋肉質。筋の隆起がはっきりしている'),
  ('muscularity','massive', '極端に発達している (ボディビル体型)'),
  ('adiposity','gaunt',     'やせ細っている。骨が浮く'),
  ('adiposity','slim',      '細い'),
  ('adiposity','average',   '標準'),
  ('adiposity','plump',     'ふくよか。丸みがある'),
  ('adiposity','obese',     '肥満。体幹に明確な脂肪の蓄積がある')
) AS v(a,s,d) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

UPDATE attribute_value SET display_order = v.o FROM (VALUES
  ('muscularity','none',1),('muscularity','slight',2),('muscularity','toned',3),
  ('muscularity','muscular',4),('muscularity','massive',5),
  ('adiposity','gaunt',1),('adiposity','slim',2),('adiposity','average',3),
  ('adiposity','plump',4),('adiposity','obese',5)
) AS v(a,s,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

COMMIT;
