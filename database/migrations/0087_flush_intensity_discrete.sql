-- Migration 0087 — flush_intensity を判定基準つき離散値にする (紅潮の受け皿を実働させる)
--
-- user 承認 2026-08-01。経緯: 0085 で flush_intensity (class=surface_skin) が
-- 出題されるようになり、@s5 で連続値の格納経路も直った (wetness ×2 で実証) が、
-- **flush_intensity は 0 件** — 連続値属性は一括パスで「(数値0..1)」と提示される
-- だけで、モデルは数値をほぼ volunteer しない (@s5 実測: 連続値回答 2/108 scene)。
-- 一方、判定基準つき離散値は S0 の孤立プロンプトに乗り、回答される (S2 で実証)。
-- 顔の紅潮が pigmentation@face に誤帰属する残余 (11 件) の正しい受け皿を作る。
--
-- 変更: value_type continuous → categorical、値 3 つ (none/light/strong) を
-- 判定基準つきで追加。既存観測は 0 件 (無影響を確認済み)。
-- ロールバック: attribute_value 3 行を消し value_type='continuous' に戻す。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

UPDATE attribute
   SET value_type = 'categorical',
       description = '紅潮 (赤らみ)。頬・顔・肌の赤み — **黒ずみ (pigmentation) や肌色 (skin_tone) とは別物**。漫画では斜線・ハッチングでも描かれる。'
 WHERE slug = 'flush_intensity' AND value_type = 'continuous';

INSERT INTO attribute_value (attribute_slug, value_slug, display_order, description)
VALUES
  ('flush_intensity', 'none', 1,
   '赤み・紅潮の描写が無い (**見た上で**そうだと言う)'),
  ('flush_intensity', 'light', 2,
   '薄い赤み。頬にほんのり色が差す・まばらな斜線 (恥じらい程度)'),
  ('flush_intensity', 'strong', 3,
   '濃くはっきりした赤み。顔の広い範囲が赤い・密な斜線や塗り (のぼせ・強い興奮がひと目で判る)')
ON CONFLICT (attribute_slug, value_slug) DO NOTHING;

COMMIT;
