-- Migration 0021 — 人物の位置を「点」でも指せるようにする
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-28:
-- 「bbox について。基本的に人体はカメラのほとんどを占有しているのであまり意味がない
--   気がする。複数人が重なっていることも多い。人間が位置から判定するのも難しいので
--   あらかじめ機械側が算出したものをこちらで多少四隅をドラッグして補正するか、
--   中心位置を取るとか。例えば頭の中心とか」。
--
-- ── 指摘の裏付け (kura コーパス実測) ──
--   * `solo` 162,218 / 全 756,534 post = **21.4%**。裏返すと **約 79% が 2 人以上**。
--     つまり「重なっていて box では切り分けられない」方が多数派で、例外ではない。
--   * 1 人の場合、box はほぼ画面全体になり **情報量がゼロ**。それでも人手を使わせていた。
--
-- ── なぜ box だけでは足りないか (機能の話) ──
-- asset_region の役目は observation.subject_asset_region_id 経由の **主体の同定** —
-- 「その巨乳は誰のものか」を画素で言えるようにすること。Kura 1.0 の主張そのもの。
-- ところが絡み合った 2 人の box はほぼ同一になり、**box では同定できない**。
-- 一方、頭の中心は 2 人で明確に別の位置にある。同定という目的に対して、
-- 点の方が box より情報量が大きい場面がある。
-- 入力コストも 1 クリック対ドラッグで、人が間違えにくい。
--
-- ── なぜ退化した box (x_min=x_max) にしないか ──
-- 「領域」と「点」は別の主張で、後から区別できなくなる。また **何の点か** (頭の中心か
-- 体の重心か) は事実の一部なので、`anchor_kind` として語彙で持つ。
-- box は消さない — 集合写真で人が十分に離れていれば box の方が素直に効く。
-- どちらか一方に決めず、**両方持てて少なくとも片方は要る**という制約にする。
--
-- ── 運用手順 ──
--   * 単一 transaction。語彙表 → 列追加 → CHECK (NOT VALID → VALIDATE)。
--   * 既存行は 0 件なので VALIDATE は即通る。
--   * ロールバック: 追加した 3 列と CHECK を DROP。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- =====================================================================
-- 1. 何を指した点なのか (語彙)
-- =====================================================================
CREATE TABLE IF NOT EXISTS asset_region_anchor (
    slug        text PRIMARY KEY,
    description text NOT NULL
);

INSERT INTO asset_region_anchor(slug, description) VALUES
  ('head_center',
   '頭の中心。絡み合った人物でも別々の位置になるので、主体の同定に最も効く。'
   '顔が見えていなくても頭の位置は分かることが多い'),
  ('face_center',
   '顔の中心。頭が隠れていて顔だけ見えている場合に使う'),
  ('body_center',
   '胴の中心。頭が画面外の場合の代替。頭より同定力は落ちる')
ON CONFLICT (slug) DO NOTHING;

-- =====================================================================
-- 2. 点を持てるようにする
-- =====================================================================
ALTER TABLE asset_region ADD COLUMN IF NOT EXISTS anchor_x    real;
ALTER TABLE asset_region ADD COLUMN IF NOT EXISTS anchor_y    real;
ALTER TABLE asset_region ADD COLUMN IF NOT EXISTS anchor_kind text
    REFERENCES asset_region_anchor(slug);

COMMENT ON COLUMN asset_region.anchor_x IS
  '点の x (0..1 の相対座標)。box が使えない場面 (人物が重なっている / 1 人で画面全体) '
  'でも主体を同定できるようにするための代替。box と併存できる。';
COMMENT ON COLUMN asset_region.anchor_kind IS
  '**何を指した点か**。頭の中心と胴の中心は別の事実なので、点だけ持って種類を'
  '持たないと後から解釈できない。語彙は asset_region_anchor。';

-- 三つ組は揃うか揃わないか
ALTER TABLE asset_region ADD CONSTRAINT ck_asset_region_anchor_complete
    CHECK ((anchor_x IS NULL) = (anchor_y IS NULL)
       AND (anchor_x IS NULL) = (anchor_kind IS NULL)) NOT VALID;
ALTER TABLE asset_region VALIDATE CONSTRAINT ck_asset_region_anchor_complete;

-- box も四隅が揃うか揃わないか (これまで制約が無く、半端な box を作れた)
ALTER TABLE asset_region ADD CONSTRAINT ck_asset_region_box_complete
    CHECK ((x_min IS NULL) = (y_min IS NULL)
       AND (x_min IS NULL) = (x_max IS NULL)
       AND (x_min IS NULL) = (y_max IS NULL)) NOT VALID;
ALTER TABLE asset_region VALIDATE CONSTRAINT ck_asset_region_box_complete;

-- 何も指していない region は作らせない (空の主体同定は主張として無意味)
ALTER TABLE asset_region ADD CONSTRAINT ck_asset_region_not_empty
    CHECK (x_min IS NOT NULL OR anchor_x IS NOT NULL) NOT VALID;
ALTER TABLE asset_region VALIDATE CONSTRAINT ck_asset_region_not_empty;

COMMIT;
