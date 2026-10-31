-- Migration 0019 — 画素寸法を記録し、機械で決まるファセットを人手注釈から外す
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-28:
-- 「画面の向きはアスペクト比から自動的に定まるのではないの？ そういうことを求めて
--   いるのではない？」。
--
-- ── 指摘は正しい。しかも欠陥は 2 段あった ──
-- (1) `scene_descriptor` に orientation (landscape / portrait / square) があり、
--     0018 で **注釈者に選ばせる UI まで作ってしまった**。これは判断ではなく
--     幅と高さの比較で決まる。人手に投げると「間違えうる」上に「観測」の名を借りた
--     計算結果が入り、非捏造の原則 (人が見ていない事を書かない) の逆をやることになる。
-- (2) より根が深いのは、**幅と高さがどこにも記録されていなかった**こと。
--     asset にも blob にも寸法列が無い。だから「自動で決まる」はずの事実を
--     計算する材料が無く、人手に落ちていた。
--     CRIT-A2 / 0004 / 0010 と同じ「宣言と着地の乖離」の 5 件目。
--
-- ── 直し方 ──
--   * 寸法は **blob (バイト列) の性質**なので blob に持たせる。同じバイト列は
--     常に同じ寸法なので、asset (scene への貼り付け方) 側ではない。
--   * orientation は**保存しない**。width/height があれば一意に決まるので、
--     行として持つと二重の真実になり食い違いうる (単一の source から評価する)。
--     語彙 (landscape/portrait/square) は消さない — 派生ビューや resolver が
--     使う正当な概念で、消すのは「人手に選ばせない」ことと別問題。
--   * どのファセットが機械で決まるかを `scene_facet.derived_from` で宣言し、
--     **注釈 UI はそれを読んで選択欄を出さない**。UI に slug を直書きしない
--     (0016 の is_multivalued と同じ形)。
--
-- ── 運用手順 ──
--   * 単一 transaction。列追加 (nullable) → 宣言の UPDATE。
--   * 寸法の実値は別スクリプト (backfill_blob_dimensions.py) で埋める。
--   * ロールバック: 追加した 3 列を DROP COLUMN。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- =====================================================================
-- 1. 画素寸法 — 「自動的に定まる」ための材料
-- =====================================================================
ALTER TABLE blob ADD COLUMN IF NOT EXISTS width  integer;
ALTER TABLE blob ADD COLUMN IF NOT EXISTS height integer;

ALTER TABLE blob ADD CONSTRAINT ck_blob_dimensions
    CHECK ((width IS NULL) = (height IS NULL)
           AND (width IS NULL OR (width > 0 AND height > 0))) NOT VALID;
ALTER TABLE blob VALIDATE CONSTRAINT ck_blob_dimensions;

COMMENT ON COLUMN blob.width IS
  '画素幅。**寸法は blob (バイト列) の性質** — 同じバイト列は常に同じ寸法なので '
  'asset 側ではなくここに持つ。NULL = 未計測 (取り込み時に読めなかった)。';
COMMENT ON COLUMN blob.height IS
  '画素高さ。width と対で入る (片方だけ NULL は CHECK が拒否する)。'
  '**画面の向き (landscape/portrait/square) はこの 2 列から導く** — '
  '行として保存すると二重の真実になり食い違いうるため保存しない。';

-- =====================================================================
-- 2. 機械で決まるファセットの宣言
-- =====================================================================
ALTER TABLE scene_facet ADD COLUMN IF NOT EXISTS derived_from text;

COMMENT ON COLUMN scene_facet.derived_from IS
  'NULL 以外 = このファセットは**機械で決まる**ので人手で注釈しない。値は何から '
  '導けるかの説明。注釈 UI はこの列を読んで選択欄を出さない — UI に slug を'
  '直書きしないための宣言 (0016 の attribute.is_multivalued と同じ形)。';

UPDATE scene_facet SET derived_from =
  'blob.width と blob.height の比較。width>height=landscape / width<height=portrait / '
  '等しい=square。人の判断が入る余地が無いので注釈者には出さない'
WHERE slug = 'orientation';

COMMIT;
