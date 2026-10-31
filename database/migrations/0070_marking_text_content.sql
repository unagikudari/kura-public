-- Migration 0070 — 書いてある文字だけは自由入力にする
--
-- plan kura-1.0-implementation、2026-07-31 の user 質問:
-- 「マーキングはフリーテキストで入力しますか？」
--
-- ── 今まで自由文の欄は 1 つも無かった (意図的) ──
-- obs_* の text 列は**全部 FK か left/right/na の 3 値**で、自由に書ける欄が無い。
-- 語彙を閉じているから query が引ける、というのがこの ontology の前提。
-- **その前提は崩さない。** 崩すと「書けないことを全部そこに書く」ようになり、
-- 語彙が育たなくなる (自由文は軸を作らない言い訳になる)。
--
-- ── 文字列だけは語彙化できない ──
-- 0069 で `surface_marking='writing'` を入れたが、**何と書いてあるかは任意の文字列**で、
-- 語彙にはできない。post 606981 の「正正」は今 `writing` としか書けない。
-- flat tag も同じ限界を持つ — body_writing 6,714 は**内容を言わない**。
-- つまりここは「語彙にできないから捨てる」ではなく「**語彙にできないものだけ入れる**」
-- 場所として正しい。
--
-- ── 縛り方 (自由文が広がらないように) ──
--   * `attribute_slug='surface_marking'` のときだけ書ける (CHECK)。
--     メモ欄として他の属性に使えない
--   * 200 字まで (CHECK)。文章を書く場所ではない
--   * **NULL は「読んでいない / 読めない」** — 空文字とは違う。非捏造の既定どおり
--   * **翻訳しない・解釈しない**。見えている文字をそのまま
--
-- ── 解決形にも載せる ──
-- `resolved_facet.text_content` を同時に足す。**宣言だけして着地させない**のを
-- ここで繰り返さない — 0044 (object が resolver に無く 1 件も解決されなかった) と
-- 同じ失敗を作らないため。
--
-- ロールバック: 追加した列を DROP。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

ALTER TABLE obs_attribute ADD COLUMN IF NOT EXISTS text_content text;
ALTER TABLE obs_attribute DROP CONSTRAINT IF EXISTS ck_obs_attribute_text_only_marking;
ALTER TABLE obs_attribute ADD CONSTRAINT ck_obs_attribute_text_only_marking
  CHECK (text_content IS NULL
         OR (attribute_slug = 'surface_marking' AND length(text_content) <= 200))
  NOT VALID;
ALTER TABLE obs_attribute VALIDATE CONSTRAINT ck_obs_attribute_text_only_marking;

COMMENT ON COLUMN obs_attribute.text_content IS
  '**書いてある文字そのもの**。`surface_marking` のときだけ書ける (CHECK)。'
  'この ontology で**唯一の自由入力** — 文字列は語彙にできないから置いている '
  '(0070)。**メモ欄ではない** — 他の属性には書けず、200 字まで。'
  '**NULL は「読んでいない / 読めない」**で空文字とは違う。'
  '**翻訳も解釈もしない** — 見えている文字をそのまま';

ALTER TABLE resolved_facet ADD COLUMN IF NOT EXISTS text_content text;
COMMENT ON COLUMN resolved_facet.text_content IS
  '解決形に載せた書いてある文字 (0070)。**宣言だけして着地させないのを繰り返さない** — '
  'resolver に無いと query が引けず、0044 で object が 1 件も解決されなかったのと同じになる';

COMMIT;
