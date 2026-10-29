-- Migration 0012 — 胸の大きさに「頭との比較」という再現可能な基準を与える
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-28:
-- 「胸の大きさに基準がなく選びづらいので相対的な基準を作る。骨格が入っており
--   変化しづらい頭との比較にする。large は頭よりは小さい、huge は頭より大きい、
--   gigantic は頭の倍以上の体積」。
--
-- ── なぜ効くか ──
-- flat/small/medium/large/huge/gigantic は**名前だけでは主観**で、注釈者が変われば
-- (あるいは同じ注釈者でも日が変われば) 境界がずれる。境界がずれると、この属性を
-- 使う判定 (層A の「排他ペアが同一 cut に共存する」= 2 人の胸の大きさが違う) が
-- 注釈者の気分に依存してしまう。
-- **頭は骨格由来で体格差の影響を受けにくい**ので、同じ画像内の比較対象として安定する。
-- 「頭より大きいか」は 1 枚の絵の中で完結し、他の画像と見比べる必要がない。
--
-- user が与えた 3 点 (large < 頭 ≦ huge、gigantic ≧ 頭の 2 倍) を軸に、
-- small / medium は同じ「頭に対する体積比」で内挿する。単調な梯子になるよう
-- 境界を連続させ、隣接する値の境目がどこかを各説明に書く。
--
-- 適用は attribute_value.description の更新のみ (語彙も順序も変えない)。
-- ロールバック: 該当行の description を 0011 時点の値に戻す (flat / gigantic のみ設定済だった)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

UPDATE attribute_value SET description = v.d FROM (VALUES
  ('flat',     '膨らみがほぼ無い。胸郭の輪郭とほぼ同じ'),
  ('small',    '膨らみはあるが小さい。片胸の体積が**頭の 1/4 未満**'),
  ('medium',   '片胸の体積が**頭の 1/4 〜 1/2 程度**'),
  ('large',    '片胸の体積が**頭の 1/2 以上、ただし頭より小さい**'),
  ('huge',     '片胸の体積が**頭より大きい**。ただし頭の 2 倍未満'),
  ('gigantic', '片胸の体積が**頭の 2 倍以上**')
) AS v(s,d) WHERE attribute_value.attribute_slug = 'breast_size'
             AND attribute_value.value_slug = v.s;

UPDATE attribute SET description =
  '乳房の大きさ。flat / small / medium / large / huge / gigantic の**単一値**。'
  '**基準は同じ絵の中の頭との体積比** — 頭は骨格由来で体格差の影響を受けにくく、'
  '1 枚で完結して比較できるため境界が注釈者間でぶれない。breast region に付くので、'
  '複数人物がいる場合は人物ごとに別の値を持てる'
WHERE slug = 'breast_size';

COMMIT;
