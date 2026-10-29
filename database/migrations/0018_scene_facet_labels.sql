-- Migration 0018 — カメラ・構図の語をファセットごとに分けられるようにする
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-28:「カメラ構図のカテゴリを分けて」。
--
-- ── なぜ列を足すのか ──
-- `scene_descriptor` は 47 語を平坦に 1 つのドロップダウンで出していた。部位 (117 語) と
-- pose (49 語) は既にカテゴリに割ってあるのに、ここだけ平坦なまま残っていた。
-- 分類軸は既に `scene_facet` (7 行) として存在するので、**新しい分類を作る必要はない** —
-- 足りないのは「画面に出す短い名前」だけ。
--   * `scene_facet.description` は英語の 1 文で、見出しには長すぎる
--     ("Vertical/horizontal viewpoint relative to the subject")。
--   * 他の語彙表 (pose / attribute / body_region) は `display_name` を持っている。
--     scene_facet だけ持っていないのは単なる不整合。
-- よって `display_name` を足し、説明も日本語にする (注釈者は日本語で考える — 0007 と同じ判断)。
--
-- ── あわせて未記述の descriptor を埋める ──
-- 0006 で 32/47 に説明を入れたが、rendering (8) / setting (6) / square が未記述だった。
-- 「語を選べないのは ontology の欠陥」なので埋める。
--
-- ── 運用手順 ──
--   * 単一 transaction。列追加 (nullable) → UPDATE。
--   * 冪等 (同値 UPDATE)。
--   * ロールバック: ALTER TABLE scene_facet DROP COLUMN display_name;

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

ALTER TABLE scene_facet ADD COLUMN IF NOT EXISTS display_name text;
COMMENT ON COLUMN scene_facet.display_name IS
  '選択 UI の見出しに出す短い名前。description は 1 文なので見出しには長すぎる。'
  'pose / attribute / body_region と同じく display_name を持たせて揃えた。';

ALTER TABLE scene_facet ADD COLUMN IF NOT EXISTS display_order integer NOT NULL DEFAULT 0;
COMMENT ON COLUMN scene_facet.display_order IS
  'カテゴリの並び順。0 = 未指定。カメラの決め方の順 (どこまで写す → どの角度から → '
  'どう配置する → どう描く → どこか) に並べる。アルファベット順にしない。';

UPDATE scene_facet SET display_name = v.n, display_order = v.o, description = v.d FROM (VALUES
  ('shot_type',    '画角・距離', 1,
   '被写体をどこまで画面に入れるか (寄り / 引き)'),
  ('camera_angle', 'カメラ角度', 2,
   '被写体に対してどの高さ・向きから見ているか'),
  ('perspective',  '遠近・レンズ', 3,
   '遠近法とレンズの性格 (歪み・消失点の数)'),
  ('composition',  '構図',       4,
   '画面の中で被写体をどう配置しているか'),
  ('orientation',  '画面の向き', 5,
   '画面の縦横比・向き'),
  ('rendering',    '描画・色',   6,
   '色と描画の様式 (線画・モノクロ・彩色など)'),
  ('setting',      '場所',       7,
   'どこで起きているか (環境・場所の性格)')
) AS v(s,n,o,d) WHERE scene_facet.slug = v.s;

-- =====================================================================
-- 未記述だった descriptor (rendering / setting / square) を埋める
-- =====================================================================
UPDATE scene_descriptor SET description = v.d FROM (VALUES
  -- orientation
  ('square',          '正方形の画面'),
  -- rendering
  ('colored',         '彩色されている (通常のカラー)'),
  ('greyscale',       'グレースケール。無彩色の濃淡のみ'),
  ('monochrome',      '単色。**greyscale が灰色の濃淡なのに対し、こちらは色味のある単色**'),
  ('sepia',           'セピア調'),
  ('limited_palette', '色数を絞った彩色。数色だけで塗られている'),
  ('spot_color',      '一部だけ着色。地は無彩色で特定の要素にだけ色が乗る'),
  ('lineart',         '線画。塗りが無く線だけ'),
  ('sketch',          'ラフ・下描き。線が整理されていない'),
  -- setting
  ('indoor',          '屋内。どの部屋かは特定しない'),
  ('outdoor',         '屋外。どこかは特定しない'),
  ('bedroom',         '寝室'),
  ('bathroom',        '浴室・洗面所・便所'),
  ('nature',          '自然の中 (森・海・草原など。人工物が主でない屋外)'),
  ('studio',          'スタジオ。背景が作り込まれていない撮影環境')
) AS v(s,d) WHERE scene_descriptor.slug = v.s;

COMMIT;
