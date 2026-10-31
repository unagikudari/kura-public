-- Migration 0064 — 一つの行為を一文で入れられるようにする (定型句)
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘:
-- 「一つの行為に**何か所もコントロールを操作しなければならない**のが非常に疲れる。
--   確定的な箇所はデフォルト値を入れてくれ。セマンティックで
--   『p1 sex p2 from behind』と入れたらそれらのコントロールが設定値になるとか」
--
-- ── 軸を分けた代償がそのまま入力の手間になっていた ──
-- 語を分けない方針 (0045 piercing、0054 striking、0062 噴出物) は**記録としては正しい**が、
-- 「背面から挿入している」を書くのに部位 2 つ・関係 2 本・主語と対象で **6 か所以上**
-- 触ることになっていた。分解は**保存の形**の話で、入力の形まで分解する必要は無い。
--
-- ── 推論ではなく定型 (ここが要点) ──
-- この表が持つのは「**人が書いた展開の型**」であって機械の推測ではない。
--   * 展開先は全部この表にある。読めば何が入るか判る
--   * 適用しても**コントロールが埋まるだけ**で DB には何も書かれない。
--     注釈者が画面で見て、直して、いつもどおり確定する
--   * 解釈できなかった語は**黙って捨てず**画面に出す
-- 非捏造の既定 (NULL は言っていない) を壊さないのはこの 3 点による。
--
-- ── jsonb で持たない (ADR-0002) ──
-- 最初 expansion を jsonb 1 列で書いたが **ADR-0002 (json/jsonb 列を持たない)** に
-- 触れた。正規化すると **FK がそのまま検査になる** — 存在しない関係や部位を指す型は
-- **DB が受け付けない**。jsonb 版では同じことを検査スクリプトで手書きしていた。
-- (「例外が少なく機構で守れる形を選ぶ」— feedback-fix-by-enforced-invariant)
--
-- ── 語彙を増やしていない ──
-- 展開先は既存の relation / body_region / pose だけ。`vaginal sex` という**語は作らない**
-- — 作れば `inserting_into` と二重の真実になる。あるのは入力の近道だけ。
--
-- ── 合成する ──
-- 1 行に複数の型を書ける。`p1 vaginal sex p2 from behind` は 2 つが順に当たる。
-- 巨大な型を 1 つ作るより組み合わせのほうが少ない型で足りる (0054 と同じ)。
--
-- ロールバック: 3 表を DROP (所見は持たないので安全に消せる)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

DROP TABLE IF EXISTS phrase_macro CASCADE;

CREATE TABLE phrase_macro (
  slug          text PRIMARY KEY,
  phrase        text NOT NULL UNIQUE,
  aliases       text[] NOT NULL DEFAULT '{}',
  description   text,
  display_order int NOT NULL DEFAULT 100
);
COMMENT ON TABLE phrase_macro IS
  '一文で複数のコントロールを埋めるための**人が書いた展開の型** (0064)。'
  '**機械の推測ではない** — 展開先は phrase_macro_relation / phrase_macro_pose に'
  '全部あり、適用しても**コントロールが埋まるだけで DB には何も書かれない**'
  ' (注釈者が見て直して確定する)。`$s` は 1 番目に書いた人物、`$o` は 2 番目。'
  '**語彙ではない** — 展開先は既存の relation / body_region / pose だけで、'
  '`vaginal sex` のような語は作っていない (作れば inserting_into と二重の真実になる)';

-- **FK がそのまま検査になる** — 存在しない関係や部位を指す型は DB が受け付けない
CREATE TABLE phrase_macro_relation (
  macro_slug        text NOT NULL REFERENCES phrase_macro(slug) ON DELETE CASCADE,
  ord               int  NOT NULL,
  relation_slug     text NOT NULL REFERENCES relation(slug),
  subject_ref       text NOT NULL,
  subject_region    text REFERENCES body_region(slug),
  subject_laterality text,
  object_ref        text,
  object_region     text REFERENCES body_region(slug),
  object_laterality text,
  object_surface    text REFERENCES scene_surface(slug),
  PRIMARY KEY (macro_slug, ord),
  CHECK (subject_laterality IS NULL OR subject_laterality IN ('left','right','na')),
  CHECK (object_laterality  IS NULL OR object_laterality  IN ('left','right','na')),
  -- 対象は人体か環境のどちらか (obs_relation の ck_obs_relation_object_one_kind と同じ線)
  CHECK (object_surface IS NULL OR (object_ref IS NULL AND object_region IS NULL))
);
COMMENT ON TABLE phrase_macro_relation IS
  '型が作る関係。ref は `$s` / `$o` (文に書いた 1 番目 / 2 番目の人物) か具体の ref。'
  '**部位は FK で縛ってある** — 存在しない部位を指す型は入らない (0064)';

CREATE TABLE phrase_macro_pose (
  macro_slug text NOT NULL REFERENCES phrase_macro(slug) ON DELETE CASCADE,
  subject_ref text NOT NULL,
  pose_slug  text NOT NULL REFERENCES pose(slug),
  PRIMARY KEY (macro_slug, subject_ref, pose_slug)
);
COMMENT ON TABLE phrase_macro_pose IS '型が作るポーズ。pose は FK で縛ってある (0064)';

INSERT INTO phrase_macro(slug, phrase, aliases, description, display_order) VALUES
 ('vaginal_sex','vaginal sex', ARRAY['膣性交','vaginal','挿入'],
  '陰茎が膣口に入っている。**「sex」だけでは書けない** — どこに入っているかを言わずに挿入は書けないので、膣 / 肛門 / 口を選ぶ', 10),
 ('anal_sex','anal sex', ARRAY['肛門性交','anal'], '陰茎が肛門に入っている', 11),
 ('oral_sex','oral sex', ARRAY['fellatio','口淫','oral'], '陰茎が口に入っている', 12),
 ('from_behind','from behind', ARRAY['背面から','後ろから','behind'],
  '$s が $o の後ろにいる。**関係だけを足す型** — 単独でも、性交の型と重ねても使える', 20),
 ('facing','facing', ARRAY['対面','正面から'], '$s が $o の正面にいる', 21),
 ('on_top','on top', ARRAY['上に','騎乗'], '$s が $o の上にいる', 22),
 ('handjob','handjob', ARRAY['手淫'], '$s の手が $o の陰茎を掴んでいる', 30),
 ('paizuri','paizuri', ARRAY['乳圧','パイズリ'], '$s の乳房が $o の陰茎に押し当てられている', 31),
 ('kissing','kissing', ARRAY['接吻','キス','kiss'], '$s の口が $o の口に触れている', 32),
 ('holding_own_legs','holding own legs', ARRAY['自分の脚を抱える','脚を抱える'],
  '$s が自分の手で自分の太ももを掴んでいる (左右 2 本)。**flat tag の leg_grab は誰の脚か言わない** — ここでは自己参照の関係で書く', 40),
 ('cum_on_face','cum on face', ARRAY['顔射','顔にかかる'], '$s の陰茎から $o の顔へ精液が飛んでいる', 50);

INSERT INTO phrase_macro_relation
  (macro_slug, ord, relation_slug, subject_ref, subject_region, subject_laterality,
   object_ref, object_region, object_laterality) VALUES
 ('vaginal_sex',1,'inserting_into','$s','penis',NULL,'$o','vaginal_opening',NULL),
 ('anal_sex',   1,'inserting_into','$s','penis',NULL,'$o','anus',NULL),
 ('oral_sex',   1,'inserting_into','$s','penis',NULL,'$o','mouth',NULL),
 ('from_behind',1,'behind',        '$s',NULL,   NULL,'$o',NULL,  NULL),
 ('facing',     1,'in_front_of',   '$s',NULL,   NULL,'$o',NULL,  NULL),
 ('on_top',     1,'above',         '$s',NULL,   NULL,'$o',NULL,  NULL),
 ('handjob',    1,'grasping',      '$s','hand', NULL,'$o','penis',NULL),
 ('paizuri',    1,'pressing_against','$s','breast',NULL,'$o','penis',NULL),
 ('kissing',    1,'touching',      '$s','mouth',NULL,'$o','mouth',NULL),
 ('holding_own_legs',1,'grasping','$s','hand','left', '$s','thigh','left'),
 ('holding_own_legs',2,'grasping','$s','hand','right','$s','thigh','right'),
 ('cum_on_face',1,'ejaculating_onto','$s','penis',NULL,'$o','face',NULL);

INSERT INTO phrase_macro_pose(macro_slug, subject_ref, pose_slug) VALUES
 ('holding_own_legs','$s','spread_legs');

COMMIT;
