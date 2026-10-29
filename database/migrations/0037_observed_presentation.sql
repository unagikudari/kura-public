-- Migration 0037 — 人物の presentation を観測として書けるようにする
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-29:
-- 「人数は人物を追加したカウントで確認できるのでは」→ そのとおりで、確認したところ
-- **presentation がどこにも書かれていなかった**。
--
-- ── 発覚したこと ──
-- 注釈 UI は人物カードごとに presentation (female/male/…) を選ばせ、
-- `annotate_scene.validate()` は語彙を**検証している**のに、`write()` が**書いていない**。
-- 下書きには female 17 / male 14 が入っているのに DB には 1 件も無く、
-- `entity.gender_presentation` は 337 行すべて NULL。
-- CRIT-A2 / 0010 / 0019 / 0024 / 0031 と同じ「宣言と着地の乖離」の **9 件目**。
--
-- ── 人数は導く。別に持たない ──
-- 人物を列挙できる scene では、人数は **entity を数えれば出る**。`entity_count` を
-- 別に書くと同じ事実が 2 か所になり食い違いうる (0019 で orientation を保存しなかった
-- のと同じ判断)。`entity_count` が要るのは **列挙できない場合だけ** —
-- 群衆で「6 人以上」しか言えないとき、`count_min` + `count_exact=false` でしか書けない。
-- よって列挙済みの scene に entity_count は書かない。
--
-- これで「1girl と 2girls が同時に立つ」ことが**構造上起こらない** — 1 scene の
-- presentation ごとの数は entity を数えた 1 つの値しかない。B4 の主張そのもの。
--
-- ── 置き場は obs_identity ──
-- 0031 で kind を置いたのと同じ理由。identity は「その主体が何であるか」を記録する場所で、
-- presentation はその層 (kind / species / character と同じ問いの別の面)。
--
-- ── 運用手順 ──
--   * 単一 transaction。列追加 + FK (NOT VALID → VALIDATE)。既存行は NULL。
--   * ロールバック: ALTER TABLE obs_identity DROP COLUMN presentation;

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

ALTER TABLE obs_identity ADD COLUMN IF NOT EXISTS presentation text;
ALTER TABLE obs_identity ADD CONSTRAINT obs_identity_presentation_fkey
    FOREIGN KEY (presentation) REFERENCES gender_presentation(slug) NOT VALID;
ALTER TABLE obs_identity VALIDATE CONSTRAINT obs_identity_presentation_fkey;

COMMENT ON COLUMN obs_identity.presentation IS
  '見た目の性別表現。**解剖 (body_region) とは独立** — 表現と体は一致しない場合がある。'
  'NULL は「言っていない」(判らない / 見えていない)。'
  '**人数はここから数える** — presentation ごとの entity 数がその scene の人数で、'
  'entity_count を別に書くのは二重 (0019 で orientation を保存しなかったのと同じ)。'
  'entity_count が要るのは**列挙できない場合だけ** (群衆で「6 人以上」等)。';

COMMENT ON COLUMN entity.gender_presentation IS
  'resolver が identity 観測から埋める。**1 scene の presentation ごとの entity 数が'
  'その scene の人数** — 1girl と 2girls が同時に立つことが構造上起こらない。';

COMMIT;
