-- Migration 0088 — flush_intensity に既定方向 (明確な描写があるときだけ) を明記
--
-- @s6 実測 (2026-08-01): 離散値化 (0087) で flush は出題・回答されるようになったが、
-- **散布が再発** — model flush 254 件、human 判定 (pool 8009 の blush 判定 38 件) との
-- 突き合わせで「human=none に model=strong」×14。0084 で state 属性に入れた
-- 既定方向の一文を 0087 で flush に入れ忘れたのが原因 (同じ轍)。
-- ロールバック: 先頭の追記を削る。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

UPDATE attribute SET description =
  '**赤み・紅潮の描写 (色や斜線) がはっきりあるときだけ**言う。迷ったら none か空。'
  || description
WHERE slug = 'flush_intensity' AND description NOT LIKE '%はっきりあるときだけ%';

UPDATE attribute_value SET description =
  '濃くはっきりした赤み。**顔の広い範囲**が赤い・密な斜線や塗り (のぼせ・強い興奮がひと目で判る)。頬だけなら light'
WHERE attribute_slug='flush_intensity' AND value_slug='strong'
  AND description NOT LIKE '%頬だけなら light%';

COMMIT;
