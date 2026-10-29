-- Migration 0061 — 脚側に「腰を上げている」を足す
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘:
-- 「post 505110 は dogeza である。しかし、**leg 側に hipup がない**。それがあれば表現できる」
--
-- ── whole_body の hips_above_head では足りない ──
-- 0043 で `hips_above_head` (腰が頭より高い) を入れたが、あれは **whole_body scope の
-- 相対的な事実** (腰と頭のどちらが高いか)。脚で腰を持ち上げているという**脚の構え**は
-- 別で、UI でも「脚・足」カテゴリに出ない。
--
-- **2 つは独立に成り立つ**:
--   * 膝立ちで腰を上げる — hips_raised は真、hips_above_head は偽 (頭の方が高い)
--   * 仰向けで腰だけ持ち上げる — 両方真
--   * 土下座で尻を上げる (505110) — 両方真
-- 片方だけにすると、膝立ちで踵から尻を離した形が書けなくなる。
--
-- ── 土下座と組む ──
-- 505110 は **dogeza + hips_raised** で書ける (0058 の接地条件は満たしている)。
-- danbooru の `top-down_bottom-up` に相当する形はこの組み合わせ。
--
-- ロールバック: 追加した pose を DELETE (所見が付いていれば消せない)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO pose(slug, display_name, scope, category) VALUES
  ('hips_raised', '腰を上げている', 'legs', 'leg_configuration')
ON CONFLICT (slug) DO NOTHING;

UPDATE pose SET description =
  '**脚で腰を持ち上げている**。股関節や膝を伸ばして尻を高くする脚の構え。'
  '**腰が頭より高いか (hips_above_head、0043) とは別** — あちらは腰と頭のどちらが'
  '高いかという相対的な事実で、こちらは脚の構え。'
  '**2 つは独立に成り立つ**: 膝立ちで踵から尻を離した形はこちらだけ真 (頭の方が高い)、'
  '仰向けで腰だけ持ち上げた形は両方真。'
  '**土下座と組める** — post 505110 は dogeza + hips_raised '
  '(danbooru の top-down_bottom-up に相当する形、user 指摘 2026-07-31)'
WHERE slug = 'hips_raised';

UPDATE pose SET description = coalesce(description,'') ||
  '。**脚で持ち上げているかは別軸** (hips_raised、0061) — 膝立ちで踵から尻を離した形は'
  'そちらだけが真になる'
WHERE slug = 'hips_above_head' AND coalesce(description,'') NOT LIKE '%hips_raised%';

COMMIT;
