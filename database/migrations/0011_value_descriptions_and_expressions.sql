-- Migration 0011 — 属性「値」に説明を持たせ、表情の語彙を広げる
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-28:
-- 「表情の値が狭い。困惑している。無感情であるといったものがある。
--   また、説明が出ないので選びにくい」。
--
-- ── 2 つの欠陥 ──
-- (1) **値に説明が無い**。0006 で attribute には description を入れたが、
--     `attribute_value` には無い。`ahegao` と `grin` の違い、`neutral` と
--     `expressionless` の違いは値の側にしか書けない。属性を選べても値を選べない。
-- (2) **facial_expression が 10 語しかない** (ahegao angry crying embarrassed grin
--     laughing neutral sad smile surprised)。コーパス実測で頻度の高い
--     rolling_eyes 74,408 / half-closed_eyes 33,905 / expressionless 874 /
--     scared 3,396 / disgust 769 / pout 675 / smug 393 が書けなかった。
--     語彙が観測を規定してしまうので、書けない表情は「近い語に寄せる」(捏造) か
--     「書かない」(情報損失) のどちらかになる。
--
-- 「困惑」は confused、「無感情」は expressionless を足す。
-- なお `rolling_eyes` / `half-closed_eyes` は**表情ではなく目の状態**なので
-- `eye_state` (closed/half_open/open) 側に rolled_back を足して受ける — 表情と
-- 目の状態は独立に観測できる (白目を剥きながら笑っている等)。
--
-- ── 運用手順 ──
--   * 単一 transaction。列追加 (nullable) → 値 INSERT → 説明 UPDATE。
--   * 冪等。ロールバック: ALTER TABLE attribute_value DROP COLUMN description;

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

ALTER TABLE attribute_value ADD COLUMN IF NOT EXISTS description text;
COMMENT ON COLUMN attribute_value.description IS
  '値の定義。属性 (attribute.description) だけでは「どの値を選ぶか」が決められない — '
  'ahegao と grin、neutral と expressionless の違いは値の側にしか書けない。NULL は未記述。';

-- =====================================================================
-- 表情を広げる (コーパス頻度順)
-- =====================================================================
INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('facial_expression','expressionless'), ('facial_expression','confused'),
  ('facial_expression','scared'),         ('facial_expression','pout'),
  ('facial_expression','smug'),           ('facial_expression','disgust'),
  ('facial_expression','annoyed'),        ('facial_expression','bored'),
  ('facial_expression','pleading'),       ('facial_expression','ecstatic')
ON CONFLICT DO NOTHING;

-- 目の状態: 白目・半目は表情ではなく目の状態
INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('eye_state','rolled_back'), ('eye_state','squinting')
ON CONFLICT DO NOTHING;

-- =====================================================================
-- 値の説明 — 迷いやすいものを埋める
-- =====================================================================
UPDATE attribute_value SET description = v.d FROM (VALUES
  -- facial_expression
  ('facial_expression','neutral',        '特に感情が出ていない自然な顔。**意図的な無表情は expressionless**'),
  ('facial_expression','expressionless', '無感情。感情が抜け落ちた顔つき。neutral が「普通」なのに対しこちらは「感情が無い」'),
  ('facial_expression','confused',       '困惑している。戸惑い・当惑'),
  ('facial_expression','smile',          '微笑み。口角が上がる程度'),
  ('facial_expression','grin',           'にやり。歯を見せた大きな笑み。smile より強い'),
  ('facial_expression','laughing',       '声を上げて笑っている'),
  ('facial_expression','smug',           'したり顔。優越感のある笑み'),
  ('facial_expression','ahegao',         'アヘ顔。快感で表情が崩れ、目や舌が制御を失った状態'),
  ('facial_expression','ecstatic',       '恍惚。強い快感や陶酔。ahegao ほど崩れていない'),
  ('facial_expression','embarrassed',    '恥ずかしがっている。照れ'),
  ('facial_expression','crying',         '泣いている'),
  ('facial_expression','pleading',       '懇願している。訴えかける表情'),
  ('facial_expression','scared',         '怯えている。恐怖'),
  ('facial_expression','surprised',      '驚いている'),
  ('facial_expression','angry',          '怒っている'),
  ('facial_expression','annoyed',        'いらだっている。angry より弱い'),
  ('facial_expression','pout',           'むくれている。頬をふくらませる・唇を突き出す'),
  ('facial_expression','disgust',        '嫌悪。不快感'),
  ('facial_expression','sad',            '悲しんでいる'),
  ('facial_expression','bored',          '退屈している'),
  -- eye_state
  ('eye_state','open',        '目を開けている'),
  ('eye_state','half_open',   '半目。まぶたが半分下りている'),
  ('eye_state','closed',      '目を閉じている'),
  ('eye_state','squinting',   '目を細めている。half_open が「力が抜けた半目」なのに対し能動的に細める'),
  ('eye_state','rolled_back', '白目を剥いている。瞳が上を向いて白目が見える'),
  -- mouth_state
  ('mouth_state','closed',        '口を閉じている'),
  ('mouth_state','slightly_open', '口が少し開いている'),
  ('mouth_state','open',          '口を大きく開けている'),
  ('mouth_state','smiling',       '口角が上がっている (口の形としての笑み)'),
  -- gaze_direction
  ('gaze_direction','at_viewer', 'こちら (カメラ) を見ている'),
  ('gaze_direction','away',      '視線を逸らしている'),
  ('gaze_direction','up',        '上を見ている'),
  ('gaze_direction','down',      '下を見ている'),
  ('gaze_direction','left',      '左を見ている'),
  ('gaze_direction','right',     '右を見ている'),
  -- breast_size / penis_size は名前で分かるが両端だけ補足
  ('breast_size','flat',     'ほぼ膨らみが無い'),
  ('breast_size','gigantic', '身体に対して極端に大きい'),
  -- surface_substance
  ('surface_substance','semen',         '精液'),
  ('surface_substance','vaginal_fluid', '愛液'),
  ('surface_substance','saliva',        '唾液・よだれ'),
  ('surface_substance','sweat',         '汗'),
  ('surface_substance','tears',         '涙'),
  ('surface_substance','milk',          '母乳'),
  ('surface_substance','urine',         '尿'),
  ('surface_substance','dirt',          '汚れ・土埃'),
  ('surface_substance','mud',           '泥'),
  ('surface_substance','oil',           '油・ローション'),
  -- pigmentation
  ('pigmentation','light',     '色素沈着が薄い'),
  ('pigmentation','medium',    '中程度'),
  ('pigmentation','dark',      '濃い'),
  ('pigmentation','very_dark', '非常に濃い (黒ずみと呼べる程度)'),
  -- ejaculation_state
  ('ejaculation_state','not_ejaculating',  '射精していない'),
  ('ejaculation_state','ejaculating',      '射精の最中 (精液が出ている)'),
  ('ejaculation_state','post_ejaculation', '射精直後 (出た後の状態)'),
  -- erection_state
  ('erection_state','flaccid',    '弛緩している'),
  ('erection_state','half_erect', '半勃起'),
  ('erection_state','erect',      '勃起している'),
  -- hair_density
  ('hair_density','none',     '生えていない (剃毛・脱毛を含む — 画像からは区別できないため同じ値)'),
  ('hair_density','sparse',   '薄い'),
  ('hair_density','moderate', '普通'),
  ('hair_density','dense',    '濃い'),
  -- belly_shape
  ('belly_shape','flat',      '平ら'),
  ('belly_shape','soft',      '柔らかく少したるむ'),
  ('belly_shape','rounded',   '丸みがある'),
  ('belly_shape','distended', '張り出している'),
  -- skin_tone
  ('skin_tone','colored', '非現実的な着色 (青・緑など)。日焼けの濃淡ではない')
) AS v(a,s,d) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

-- 表情の並び順 (中立 → 快 → 負の感情 の順に寄せる)
UPDATE attribute_value SET display_order = v.o FROM (VALUES
  ('facial_expression','neutral',1),('facial_expression','expressionless',2),
  ('facial_expression','smile',3),('facial_expression','grin',4),
  ('facial_expression','laughing',5),('facial_expression','smug',6),
  ('facial_expression','ecstatic',7),('facial_expression','ahegao',8),
  ('facial_expression','embarrassed',9),('facial_expression','confused',10),
  ('facial_expression','surprised',11),('facial_expression','pleading',12),
  ('facial_expression','scared',13),('facial_expression','crying',14),
  ('facial_expression','sad',15),('facial_expression','pout',16),
  ('facial_expression','annoyed',17),('facial_expression','angry',18),
  ('facial_expression','disgust',19),('facial_expression','bored',20),
  ('eye_state','open',1),('eye_state','squinting',2),('eye_state','half_open',3),
  ('eye_state','closed',4),('eye_state','rolled_back',5)
) AS v(a,s,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

COMMIT;
