-- Migration 0006 — ontology の語に description を持たせる
--
-- plan kura-1.0-implementation の S-8 (人手注釈) 中に判明した欠陥。
--
-- ── 何が問題だったか ──
-- 注釈者 (user) の指摘、2026-07-27:「ポーズの description がないのでどれが一番適して
-- いるのか分からない。胸をつかまれて宙づりになっているのだが、どれが当てはまるのか分からない」。
--
-- 実際に `pose` / `body_region` / `relation` / `scene_descriptor` / `object_type` /
-- `attribute` の**どの表にも description 列が無い**。slug と display_name と構造的な列
-- (scope/category/parent/laterality 等) だけで、**語が定義されていない**。
-- `seiza` と `wariza` の違いも、新しい状況にどの語が当たるかも判断できない。
--
-- これは production kura に対する**退行**でもある: kura は全 16,650 タグに
-- `tag.description` を持たせてある (2026-07-19 の作業) のに、Kura 1.0 の ontology は
-- 定義ゼロで出発していた。「ontology は data」を掲げるなら、**定義も data** であるべき。
--
-- ── 何を入れたか / 入れなかったか ──
-- 曖昧さが実害になる 3 系統に description を入れる:
--   * relation (17) — 「宙づり」の正解が `supporting` だと分かる必要がある。今回の直接原因
--   * pose (41)     — seiza/wariza/reclining/lying 等の区別が名前から読めない
--   * attribute (17)— 何に付く属性で、値が何を意味するか
--   * scene_descriptor (47) — cowboy_shot 等の業界語は定義なしでは選べない
-- `body_region` (117) は **あえて NULL のまま**にする: 解剖学名なので prose よりも
-- 構造 (parent 連鎖 + laterality + class) の方が情報量が多く、UI 側でそれを見せる方が
-- 有効。117 件の散文を書いても曖昧さは減らない。`object_type` (38) も後回し (衣服名は
-- 自明なものが多く、必要になった語から埋める)。
-- **NULL = 未記述** であって「説明が無い語」ではない。埋める順序は実際に迷った語から。
--
-- ── 運用手順 ──
--   * 単一 transaction、`SET LOCAL lock_timeout`。列追加 (nullable、既定 NULL) → UPDATE。
--   * 既存行に影響なし。制約も増えないので NOT VALID / VALIDATE は不要。
--   * 冪等: 列追加は IF NOT EXISTS、UPDATE は同じ値の再代入なので再実行安全。
--   * ロールバック: 6 表から `DROP COLUMN description`。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

ALTER TABLE pose             ADD COLUMN IF NOT EXISTS description text;
ALTER TABLE body_region      ADD COLUMN IF NOT EXISTS description text;
ALTER TABLE relation         ADD COLUMN IF NOT EXISTS description text;
ALTER TABLE scene_descriptor ADD COLUMN IF NOT EXISTS description text;
ALTER TABLE object_type      ADD COLUMN IF NOT EXISTS description text;
ALTER TABLE attribute        ADD COLUMN IF NOT EXISTS description text;

COMMENT ON COLUMN pose.description IS
  '語の定義。NULL は未記述であって「定義が不要な語」ではない。注釈者が語を選べないのは ontology の欠陥なので、迷った語から埋める。';

-- =====================================================================
-- relation (17) — 今回の直接原因。「誰が誰をどうしているか」の語彙
-- =====================================================================
UPDATE relation SET description = v.d FROM (VALUES
  ('adjacent_to',        '隣接している。接触は問わない (触れていなくてもよい)'),
  ('overlaps',           '視覚的に重なっている。前後関係は問わない (どちらが手前かは occluding で言う)'),
  ('inside',             '内部にある。包含関係 (容器と中身、衣服の内側 等)'),
  ('above',              '上方にある。重力方向で上 (画面上ではなく世界の上)'),
  ('below',              '下方にある。above の逆'),
  ('behind',             '背後にある。視点から見て奥ではなく、対象の背中側'),
  ('in_front_of',        '正面側にある。behind の逆'),
  ('touching',           '接触している。荷重や把持を含まない単なる接触'),
  ('supporting',         '**体重や荷重を支えている**。支えられている側は自力で位置を保っていない。「抱え上げる」「宙づりにする」はこれ (pose ではなく relation で表す)'),
  ('resting_on',         '**乗っている / もたれている**。荷重を預けているが持ち上げられてはいない。supporting の逆向きで、主体が預ける側'),
  ('pressing_against',   '押し付けている。接触に加えて力がかかっている'),
  ('grasping',           '**掴んでいる**。手指で握って把持している。何を掴んでいるかは object 側の region で言う'),
  ('holding',            '持っている / 抱えている。掴む (grasping) より広く、腕全体で抱える場合を含む'),
  ('covering',           '覆っている。視界や部位を隠している (手で隠す、布が覆う 等)'),
  ('occluding',          '手前にあって隠している。covering と違い「隠す意図や接触」を含まない単なる遮蔽'),
  ('contacting_genital', '性器が接触している。挿入は含まない (挿入は inserting_into)'),
  ('inserting_into',     '挿入している。主体が挿入する側、対象が挿入される側')
) AS v(s,d) WHERE relation.slug = v.s;

-- =====================================================================
-- pose (41) — 名前から区別が読めない語が多い
-- =====================================================================
UPDATE pose SET description = v.d FROM (VALUES
  -- whole_body / posture
  ('standing',            '立っている。両脚で体重を支える'),
  ('sitting',             '座っている。臀部で体重を支える。脚の形は問わない'),
  ('seiza',               '正座。両膝を折って揃え、臀部を踵の上に乗せる。日本式の正式な座り方'),
  ('wariza',              '割座 (ぺたんこ座り)。膝を折って両足を左右へ開き、臀部を床に付ける。正座と違い足が外を向く'),
  ('kneeling',            '膝立ち。膝で体重を支え、臀部は踵に乗せない (乗せると正座)'),
  ('squatting',           'しゃがんでいる。足裏を付けたまま膝を深く曲げ、臀部は床に付かない'),
  ('crouching',           'かがんでいる。squatting より浅く、上体を前に落とした姿勢'),
  ('all_fours',           '四つん這い。両手と両膝 (または足) の 4 点で支える'),
  ('bent_over',           '前屈。立位または膝立ちのまま上体だけを前に倒す'),
  ('arched_back',         '背中を反らせている。胸を張り腰を落とす反り'),
  ('leaning_forward',     '前傾。上体を前へ傾ける (bent_over より浅い)'),
  ('leaning_back',        '後傾。上体を後ろへ傾ける'),
  ('lying',               '横たわっている。向きを特定しない総称 (向きが分かるなら on_back / on_stomach / on_side を使う)'),
  ('on_back',             '仰向け。背中を下にして寝ている'),
  ('on_stomach',          'うつ伏せ。腹を下にして寝ている'),
  ('on_side',             '横向きに寝ている'),
  ('reclining',           'もたれて半分起きている。完全に寝てはいない (背もたれや腕で上体を支える)'),
  ('fetal_position',      '胎児姿勢。横向きで膝を胸へ抱え込む'),
  ('dogeza',              '土下座。膝と両手を床に付け、額を床へ近づける最大限の平伏'),
  ('prostration',         '平伏。腹または胸を床に付けて伏せる (dogeza より宗教的・儀礼的な広い語)'),
  -- whole_body / self_contact
  ('hugging_own_legs',    '自分の脚を抱えている'),
  ('looking_through_own_legs', '自分の股の間から覗いている'),
  -- legs / leg_configuration
  ('spread_legs',         '脚を左右に開いている'),
  ('m_legs',              'M 字開脚。膝を立てて左右に開き、脚が M の形になる'),
  ('crossed_legs',        '脚を組んでいる'),
  ('knees_up',            '膝を立てている。足裏が接地している'),
  ('knees_to_chest',      '膝を胸に引き寄せている'),
  ('legs_up',             '両脚を持ち上げている。接地していない'),
  ('leg_up',              '片脚を上げている'),
  ('leg_lift',            '脚を持ち上げられている / 上げている動作。leg_up より動きを含意'),
  ('standing_on_one_leg', '片脚立ち'),
  ('standing_split',      '立位で開脚。片脚を高く上げて縦に開く'),
  ('bowlegged_pose',      'O 脚に見える立ち方。膝が外へ張る'),
  -- arms / arm_configuration
  ('arms_up',             '両腕を上げている'),
  ('arm_up',              '片腕を上げている'),
  ('arms_behind_back',    '両腕を背中に回している'),
  ('arms_behind_head',    '両腕を頭の後ろに回している'),
  ('hands_up',            '両手を上げている。arms_up より手の位置に焦点'),
  ('spread_arms',         '両腕を左右に広げている'),
  -- hands
  ('hand_on_own_hip',     '自分の腰に手を当てている'),
  ('hands_on_own_ass',    '自分の臀部に手を当てている')
) AS v(s,d) WHERE pose.slug = v.s;

-- =====================================================================
-- attribute (17) — 何に付く属性で、値が何を意味するか
-- =====================================================================
UPDATE attribute SET description = v.d FROM (VALUES
  ('skin_tone',        '肌の色調。pale/light/medium/tan/dark/deep と、非現実的な着色を表す colored。**surface_skin クラスの region にのみ適用** (粘膜や外性器には届かない — 色素沈着を粘膜側に記録したい場合は語彙が不足している)'),
  ('hair_color',       '髪の色'),
  ('hair_length',      '髪の長さ'),
  ('hair_style',       '髪型'),
  ('eye_color',        '目の色。**1 つの値**を取る。左右で違う場合は heterochromia を使う (blue と red を同時に付けるのではない)'),
  ('eye_state',        '目の状態 (開閉など)'),
  ('gaze_direction',   '視線の向き'),
  ('mouth_state',      '口の状態 (閉/半開/開/笑み)'),
  ('facial_expression','表情'),
  ('wetness',          '濡れの度合い。0..1 の連続値。surface_mucosa クラスに適用'),
  ('flush_intensity',  '赤みの強さ。0..1 の連続値'),
  ('joint_flexion',    '関節の屈曲角。度数'),
  ('breast_size',      '乳房の大きさ。flat/small/medium/large/huge/gigantic の**単一値**。breast region に付くので、複数人物がいる場合は人物ごとに別の値を持てる'),
  ('penis_size',       '陰茎の大きさ。small/average/large/huge の単一値'),
  ('erection_state',   '勃起の状態。flaccid / erect の単一値'),
  ('belly_shape',      '腹部の形状。flat/soft/rounded/distended。**見た目の形**であり妊娠の有無とは別 (妊娠は pregnancy_state)'),
  ('pregnancy_state',  '妊娠の状態。not_pregnant / pregnant / unknown。**belly_shape とは独立** — 腹が丸いことと妊娠は別に観測でき、片方だけ分かる場合も表せる')
) AS v(s,d) WHERE attribute.slug = v.s;

-- =====================================================================
-- scene_descriptor (47) — 業界語は定義なしでは選べない
-- =====================================================================
UPDATE scene_descriptor SET description = v.d FROM (VALUES
  ('eye_level',            'カメラが被写体の目の高さ'),
  ('high_angle',           'カメラが被写体より高い位置から見下ろす'),
  ('low_angle',            'カメラが被写体より低い位置から見上げる'),
  ('from_above',           '真上寄りから見下ろす。high_angle より角度が強い'),
  ('from_below',           '真下寄りから見上げる。low_angle より角度が強い'),
  ('birds_eye_view',       '真上からの俯瞰'),
  ('worms_eye_view',       '地面すれすれからの仰視'),
  ('dutch_angle',          'カメラを傾けて水平線が斜めになる'),
  ('over_the_shoulder',    '人物の肩越しの視点'),
  ('pov',                  '登場人物の視点そのもの (一人称)'),
  ('extreme_close_up',     '極端な寄り。部位の一部で画面が埋まる'),
  ('close_up',             '寄り。顔や単一部位が主'),
  ('medium_close_up',      '胸から上'),
  ('medium_shot',          '腰から上'),
  ('cowboy_shot',          '腿の中間から上。膝上で切る構図 (西部劇の腰の銃を入れる画角に由来)'),
  ('full_body_shot',       '全身が入る'),
  ('wide_shot',            '全身と周囲の空間が入る'),
  ('extreme_wide_shot',    '人物が小さく、環境が主'),
  ('eye_level_perspective','目線の高さの遠近感'),
  ('foreshortening',       '手前の部位が誇張して大きく見える短縮遠近'),
  ('wide_angle',           '広角。周辺が引き伸ばされる'),
  ('fisheye',              '魚眼。強い樽型の歪み'),
  ('one_point',            '一点透視'),
  ('two_point',            '二点透視'),
  ('three_point',          '三点透視'),
  ('centered',             '被写体が画面中央'),
  ('rule_of_thirds',       '三分割構図'),
  ('symmetrical',          '左右対称構図'),
  ('diagonal',             '対角線構図'),
  ('framed',               '前景の要素で被写体を囲む構図'),
  ('portrait',             '縦長'),
  ('landscape',            '横長')
) AS v(s,d) WHERE scene_descriptor.slug = v.s;

COMMIT;
