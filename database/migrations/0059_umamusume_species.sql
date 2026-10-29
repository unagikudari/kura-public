-- Migration 0059 — ウマ娘は種族であって horse_girl ではない
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘:
-- 「**ウマ娘は種族カテゴリであり、horse_girl ではない**。今 horse_girl にしている
--   kura1 検証を umamusume 種族に移し替えてそれぞれを分けてほしい」
--
-- ── 2 つの別物を 1 語に入れていた ──
--   * `horse_girl` — **馬の特徴を持つ人**という一般名。0040 で非推奨にし、
--     `species=human` + `motif_species=horse` に分解する形にした
--   * **ウマ娘** — 作品世界の**独立した種族**。人間とは別の種として設定されており、
--     「馬耳を付けた人間の女の子」ではない
-- 分解の対象 (motif) と、それ自体が種族であるものを同じ語に入れていたので、
-- **ウマ娘を書くと非推奨の警告が出る**という誤った案内になっていた。
--
-- 実測 (production kura): umamusume 12,739 / horse_girl 18,549 /
-- horse_ears 18,763 / horse_tail 16,971。**別々のタグとして併存している**。
--
-- ── 判定の線 ──
-- 0041 で書いた「**人体として描かれているか**」の 1 問がここでも効く:
--   * 人の体に馬の耳と尾が付いているだけ → species=human + motif_species=horse
--   * 作品世界で人間とは別の種族として設定されている → species=umamusume
-- **見た目が似ていても由来が違う**ので、迷ったら作品を見る (キャラクター名が
-- 分かるなら character 側にも書ける)。
--
-- ── 既存の所見 (post 206141) ──
-- `horse_girl` の所見が 1 件ある。**append-only なので書き換えない** — 撤回して
-- 注釈者が入れ直す (model が注釈を書かないという plan invariant を守るため)。
-- 撤回は別途 retract.py で行う。
--
-- ロールバック: 追加した species を DELETE (所見が付いていれば消せない)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO species(slug, display_name, marked_by) VALUES
  ('umamusume', 'ウマ娘', 'horse_ears_tail')
ON CONFLICT (slug) DO NOTHING;

-- species には description 列が無いので display_name に注記を寄せず、
-- 区別は horse_girl 側 (非推奨の理由) と 0041 の列コメントで担保する。
-- horse_girl の非推奨表示に「ウマ娘は別」であることを足す。
-- **`[非推奨]` の表記を崩さない。** annotate_scene の Ontology.deprecated_species が
-- `display_name LIKE '%[非推奨]%'` で拾っているので、括弧の中に文字を足すと
-- **validator の非推奨判定から外れる** (2026-07-31、verify がこれを捕まえた)。
UPDATE species SET display_name = 'horse girl [非推奨] — ウマ娘は umamusume'
 WHERE slug = 'horse_girl';

COMMENT ON COLUMN obs_identity.species_slug IS
  'その主体が**何であるか**。人なら human、犬なら dog、牛頭なら minotaur、'
  'ウマ娘なら umamusume。'
  '**判定は「人体として描かれているか」の 1 問** — '
  '獣の頭 (minotaur)・下半身が別の動物 (centaur / lamia / mermaid)・'
  '全身が別物 (slime / dragon)・**作品世界で別種族とされているもの (umamusume)** は species。'
  '**体は人のままで耳や尾だけ動物なら species=human + motif_species=<動物>** (0040)。'
  '人の頭に牛の角と耳が付いているだけなら minotaur ではないし、'
  '**馬耳を付けた人間の女の子は umamusume ではない** (0059) — '
  '見た目が似ていても由来が違うので迷ったら作品を見る';

COMMIT;
