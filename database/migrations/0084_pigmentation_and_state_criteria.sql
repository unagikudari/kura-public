-- Migration 0084 — pigmentation と state 属性の判定基準を明記 (語は増やさない)
--
-- plan kura1-observer-selection S2 の実測 + user 指摘 (2026-08-01):
-- 1. モデルが pigmentation を「肌色の絶対スケール」として全部位に振っていた
--    (face medium ×69 — 人間の使用実績 85 件に face は 0)。紅潮も pigmentation に
--    吸われていた。意図は**基礎肌色との局所差分 (黒ずみ)**。
-- 2. 低頻度 state 属性 (潮吹き・排尿・排便・precum・痙攣) がほぼ毎人物に陽性
--    (squirting 191:58、人間の base rate ~1%)。S0 孤立プロンプトは「空にする」を
--    ほぼ消す (S2 実測) ので、**既定方向 (明確に描かれているときだけ) を基準そのもの
--    に書く**しかない。
--
-- runner は属性説明の**先頭 90 字**と値説明の**全文**を判定基準として渡す
-- (model_runner.py adesc/vlines)。だから既定方向は説明の先頭に置く。
--
-- ── 変更は説明だけ ── 語・値・列は一切足さない (ADR-0004)。
-- ロールバック: 追記・置換部分を戻すだけ (機能的な影響なし)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- ── pigmentation: 基礎肌色との局所差分であることを先頭に ──
UPDATE attribute SET description =
  '本人の基礎肌色より**局所的に**濃い黒ずみ (メラニン)。顔の赤み・紅潮は flush_intensity、キャラ全体の肌色は skin_tone — これではない。'
  || description
WHERE slug = 'pigmentation' AND description NOT LIKE '%基礎肌色%';

UPDATE attribute_value SET description =
  '基礎肌色との差がわずか (よく見れば黒ずみが判る程度)'
WHERE attribute_slug='pigmentation' AND value_slug='light'
  AND description NOT LIKE '%基礎肌色%';

UPDATE attribute_value SET description =
  '基礎肌色よりはっきり濃い (ひと目で黒ずみと判る)'
WHERE attribute_slug='pigmentation' AND value_slug='medium'
  AND description NOT LIKE '%基礎肌色%';

UPDATE attribute_value SET description =
  '基礎肌色より明確に濃い。**皮膚そのものの色**が濃い状態。白〜黄の付着物が溜まっているだけならこれではなく surface_substance=smegma'
WHERE attribute_slug='pigmentation' AND value_slug='dark'
  AND description NOT LIKE '%基礎肌色%';

UPDATE attribute_value SET description =
  '基礎肌色から著しく濃い (黒に近い黒ずみ)'
WHERE attribute_slug='pigmentation' AND value_slug='very_dark'
  AND description NOT LIKE '%基礎肌色%';

-- ── state 属性: 既定方向 (明確な描写があるときだけ) を先頭に ──
UPDATE attribute SET description =
  '**女性器からの噴出が明確に描かれているときだけ**言う。精液・付着液・泡は該当しない。判らなければ空。'
  || description
WHERE slug = 'squirting_state' AND description NOT LIKE '%明確に描かれているときだけ%';

UPDATE attribute_value SET description =
  '女性器から液体が噴き出す描写がはっきりある (精液の糸・体表の付着液では言わない)'
WHERE attribute_slug='squirting_state' AND value_slug='squirting'
  AND description NOT LIKE '%はっきり%';

UPDATE attribute SET description =
  '**尿の流れ・放物線が明確に描かれているときだけ**言う。他の液体では言わない。判らなければ空。'
  || description
WHERE slug = 'urination_state' AND description NOT LIKE '%明確に描かれているときだけ%';

UPDATE attribute_value SET description =
  '排尿中。尿道口からの流れ・放物線がはっきり見える (他の液体と区別できるときだけ)'
WHERE attribute_slug='urination_state' AND value_slug='urinating'
  AND description NOT LIKE '%はっきり%';

UPDATE attribute SET description =
  '**便そのものが描かれているときだけ**言う。汚れ・影・肛門が見えるだけでは言わない。判らなければ空。'
  || description
WHERE slug = 'defecation_state' AND description NOT LIKE '%描かれているときだけ%';

UPDATE attribute_value SET description =
  '出し終えた直後。**便そのものが見えている**ときだけ (単なる汚れ・影では言わない)'
WHERE attribute_slug='defecation_state' AND value_slug='post_defecation'
  AND description NOT LIKE '%便そのものが見えている%';

UPDATE attribute SET description =
  'precum は**尿道口から滲む透明な液が見えるときだけ** — 体表に付いた液では言わない。判らなければ空。'
  || description
WHERE slug = 'ejaculation_state' AND description NOT LIKE '%尿道口から滲む%';

UPDATE attribute_value SET description =
  '**尿道口から**滲む・垂れる透明な液が見えるときだけ。体や周囲に付いた液では言わない。精液が出ていれば ejaculating'
WHERE attribute_slug='ejaculation_state' AND value_slug='precum'
  AND description NOT LIKE '%尿道口から%';

UPDATE attribute SET description =
  '**震え線・ブレの描写があるときだけ**言う。判らなければ空。'
  || description
WHERE slug = 'involuntary_motion' AND description NOT LIKE '%震え線%';

COMMIT;
