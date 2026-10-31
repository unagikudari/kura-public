-- Migration 0007 — body_region の 117 語に日本語の説明を入れる
--
-- plan kura-1.0-implementation の S-8 (人手注釈) 中の指摘、2026-07-28:
-- 「部位のドロップダウンリストにも日本語の説明を追加して」。
--
-- 0006 では body_region を **あえて未記述**にした。理由は「解剖学名なので prose より
-- 構造 (parent 連鎖 + laterality + class) の方が情報量が多い」— これは**誤りだった**。
--   * `frenulum` / `clitoral_hood` / `laryngeal_prominence` / `iliac_crest` / `mons` /
--     `gluteal_cleft` のような語は、英語名を見ても何を指すか判らない。
--   * 注釈者は日本語で考えるので、`nape` が「うなじ」だと分かる方が速い。
--   * 構造 (parent=neck) は「首の一部」までしか教えてくれない。
-- 語を選べないのは ontology の欠陥である、という 0006 の判断を body_region にも適用する。
--
-- 方針: 日本語名を先頭に置き、**近縁の語と紛れるものだけ**区別を書き足す
-- (例 midriff と abdomen、areola と nipple、perineum と perianal_region)。
-- 冗長な解説は書かない — ドロップダウンに 1 行で出るので長いと読まれない。
--
-- ── 運用手順 ──
--   * 単一 transaction。UPDATE のみ (description 列は 0006 で追加済)。
--   * 冪等: 同じ値の再代入なので再実行安全。
--   * ロールバック: UPDATE body_region SET description = NULL;

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

UPDATE body_region SET description = v.d FROM (VALUES
  -- 最上位 / 大区分
  ('body',                 '身体全体'),
  ('head',                 '頭部'),
  ('neck',                 '首'),
  ('torso',                '胴体'),
  ('pelvis',               '骨盤部。股・臀部・外性器・肛門を含む領域'),
  ('arm',                  '腕 (肩から手まで全体)'),
  ('leg',                  '脚 (股関節から足まで全体)'),
  -- 頭部
  ('face',                 '顔'),
  ('scalp',                '頭皮'),
  ('skull',                '頭蓋 (骨の輪郭)'),
  ('ear',                  '耳'),
  ('earlobe',              '耳たぶ'),
  -- 顔
  ('forehead',             '額'),
  ('temple',               'こめかみ'),
  ('cheek',                '頬'),
  ('cheekbone',            '頬骨'),
  ('chin',                 '顎先 (下顎の先端)'),
  ('jaw',                  '顎 (骨としての下顎)'),
  ('jawline',              '顎のライン (輪郭線)'),
  ('nose',                 '鼻'),
  ('nose_bridge',          '鼻筋 (鼻梁)'),
  ('nostril',              '鼻孔 (鼻の穴)'),
  ('eyebrow',              '眉'),
  ('beard',                'あごひげ'),
  ('mustache',             '口ひげ'),
  ('sideburns',            'もみあげ'),
  -- 目
  ('eye',                  '目'),
  ('iris',                 '虹彩 (瞳の色の付いた輪)'),
  ('pupil',                '瞳孔 (虹彩の中心の黒い穴)'),
  ('sclera',               '白目 (強膜)'),
  ('upper_eyelid',         '上まぶた'),
  ('lower_eyelid',         '下まぶた'),
  ('eyelash',              'まつげ'),
  -- 口
  ('mouth',                '口'),
  ('upper_lip',            '上唇'),
  ('lower_lip',            '下唇'),
  ('inner_lip',            '唇の内側 (粘膜面)'),
  ('teeth',                '歯'),
  ('gums',                 '歯茎'),
  ('tongue',               '舌'),
  -- 首
  ('throat',               '喉 (首の前面)'),
  ('nape',                 'うなじ (首の後ろ)'),
  ('clavicle',             '鎖骨'),
  ('laryngeal_prominence', '喉仏 (喉の突起)'),
  -- 胴体
  ('chest',                '胸部 (胸郭の前面。乳房を含む上位)'),
  ('breast',               '乳房'),
  ('areola',               '乳輪 (乳首の周囲の色の違う輪)'),
  ('nipple',               '乳首'),
  ('sternum',              '胸骨 (胸の中央の骨)'),
  ('rib',                  '肋骨'),
  ('abdomen',              '腹部 (胴体前面の下半分。臍を含む)'),
  ('navel',                '臍 (へそ)'),
  ('midriff',              '腹まわりの露出面。腹部 (abdomen) と違い「胸下から腰までの見える帯」を指す'),
  ('waist',                'ウエスト (胴のくびれ)'),
  ('flank',                '脇腹'),
  ('back',                 '背中'),
  ('upper_back',           '背中の上部'),
  ('lower_back',           '腰 (背中の下部)'),
  ('spine',                '背骨のライン'),
  ('scapula',              '肩甲骨'),
  -- 骨盤部
  ('buttock',              '臀部 (尻の片側)'),
  ('gluteal_cleft',        '尻の割れ目'),
  ('groin',                '股の付け根 (鼠径部)'),
  ('iliac_crest',          '腰骨の張り出し (腸骨稜)'),
  ('pubic_region',         '陰部の前面 (下腹部と外性器の間)'),
  ('mons',                 '恥丘 (陰部前面の膨らみ)'),
  ('perineum',             '会陰 (外性器と肛門の間の皮膚面)'),
  ('perianal_region',      '肛門の周囲。perineum が「性器と肛門の間」なのに対しこちらは肛門を囲む領域'),
  ('anus',                 '肛門'),
  -- 外性器 (共通)
  ('external_genitalia',   '外性器 (男女を問わない上位区分)'),
  ('urethral_opening',     '尿道口'),
  -- 男性外性器
  ('penis',                '陰茎'),
  ('penile_shaft',         '陰茎の竿 (亀頭を除く軸)'),
  ('glans',                '亀頭'),
  ('foreskin',             '陰茎の皮 (包皮)'),
  ('frenulum',             '裏筋 (亀頭と包皮をつなぐ帯)'),
  ('scrotum',              '陰嚢 (袋)'),
  ('testis',              '精巣 (玉。陰嚢の中身)'),
  -- 女性外性器
  ('vulva',                '外陰部 (女性外性器の全体)'),
  ('labia_majora',         '大陰唇 (外側の唇)'),
  ('labia_minora',         '小陰唇 (内側の唇)'),
  ('clitoris',             'クリトリス'),
  ('clitoral_hood',        'クリトリスを覆う皮 (包皮)'),
  ('vaginal_opening',      '膣口'),
  -- 腕
  ('shoulder',             '肩 (関節)'),
  ('upper_arm',            '上腕 (肩から肘)'),
  ('elbow',                '肘'),
  ('forearm',              '前腕 (肘から手首)'),
  ('wrist',                '手首'),
  ('axilla',               '脇の下'),
  -- 手
  ('hand',                 '手'),
  ('palm',                 '手のひら'),
  ('back_of_hand',         '手の甲'),
  ('thumb',                '親指'),
  ('index_finger',         '人差し指'),
  ('middle_finger',        '中指'),
  ('ring_finger',          '薬指'),
  ('little_finger',        '小指'),
  ('fingertip',            '指先'),
  ('fingernail',           '指の爪'),
  ('knuckle',              '指の関節 (拳の突起)'),
  -- 脚
  ('hip',                  '股関節 (脚の付け根)'),
  ('thigh',                '太もも'),
  ('inner_thigh',          '内もも'),
  ('knee',                 '膝'),
  ('patella',              '膝の皿 (膝蓋骨)'),
  ('shin',                 'すね (脛の前面)'),
  ('calf',                 'ふくらはぎ'),
  ('ankle',                '足首'),
  -- 足
  ('foot',                 '足 (足首から先)'),
  ('sole',                 '足の裏'),
  ('instep',               '足の甲'),
  ('arch',                 '土踏まず'),
  ('heel',                 'かかと'),
  ('toe',                  '足の指'),
  ('big_toe',              '親指 (足)'),
  ('toenail',              '足の爪')
) AS v(s,d) WHERE body_region.slug = v.s;

COMMIT;
