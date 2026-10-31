-- Migration 0005 — 注釈中に判明した語彙の欠落を埋める
--
-- plan kura-1.0-implementation の S-8 (人手注釈) は「**語彙が足りなくて書けなかった事実**」
-- を明示的な出力として求めている (語彙の不足そのものが測定対象)。本 migration は
-- その 1 件目を反映する。0003(h) と同じ「seed 1 行で表現可能」の系列。
--
-- ── 発見 1: 馬娘 (horse girl) の species が無い ──
-- 注釈者 (user) の指摘、2026-07-27、post 206141 の注釈中:「スピーシーズに umamusume がない」。
-- 実際に species は 28 語で、`catgirl` / `dog_girl` / `fox_girl` / `kitsune` /
-- `kemonomimi`(animal_ears) と獣耳系が揃っているのに **馬の系統だけ欠けていた**。
--
-- slug を `umamusume` にしない理由: `umamusume` は特定 IP (ウマ娘) の名前で、
-- **species は種であって franchise ではない**。BOOTSTRAP_COMMON の naming 規則
-- 「振る舞いで命名 / repo 固有語は最外殻 env のみ」に従い、既存の `catgirl` /
-- `dog_girl` / `fox_girl` と同じ一般名 `horse_girl` を採る。IP としての「ウマ娘」は
-- `franchise` 表 (現在 11 件、umamusume は未登録) の領域で、必要になったら別途足す。
-- こうしておけば、ウマ娘以外の作品の馬娘も同じ species で表せる。
--
-- marked_by は既存の獣耳系と同じ `animal_ears`。
--
-- ── 運用手順 ──
--   * 単一 transaction、`SET LOCAL lock_timeout`。INSERT のみで DDL 変更なし。
--   * ON CONFLICT DO NOTHING なので再実行は安全 (冪等)。
--   * ロールバック: DELETE FROM species WHERE slug='horse_girl';
--     ただし既に observation が参照していると FK で拒否される (それが正しい挙動 —
--     観測が指している語彙は消せない)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO species(slug, display_name, marked_by) VALUES
  ('horse_girl', 'horse girl', 'animal_ears')
ON CONFLICT (slug) DO NOTHING;

COMMIT;
