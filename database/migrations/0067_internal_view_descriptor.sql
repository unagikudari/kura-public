-- Migration 0067 — 「中が描かれている」は画像の描き方であって部位ではない
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘:
-- 「x-ray で rectum を描く場合とどう分ける」
--
-- ── 0066 だけでは 2 つが混ざる ──
-- 0066 で `rectum` / `vaginal_canal` / `uterus` / `cervix` を足して
-- 「中に出した」を書けるようにしたが、それは**何が起きているか**であって
-- **どう描かれているか**ではない。両者は直交する:
--   (1) 断面で描かれ、中に出している      — cross_section + `→ rectum`
--   (2) 断面ではないが挿入されていて外に出ていない — `→ rectum` だけ (根拠は状況)
--   (3) 断面で描かれているが射精はしていない  — cross_section だけ
--   (4) 外にかかっている                  — `→ anus`
-- 部位に「断面で見えている rectum」という語を作ると (1) と (2) が別の部位になり、
-- **同じ事実が描き方で 2 語に割れる**。0045 (piercing を部位ごとに作らない)、
-- 0054 (punch/kick を作らない) と同じ理由で分けない。
--
-- ── 描き方の語彙に穴があった ──
-- `scene_descriptor` には camera_angle / composition / orientation / perspective /
-- rendering / setting / shot_type の 7 facet 38 語があるが、
-- **「内部が見える描き方」を言う語が 1 つも無かった**。
-- 実測 (production kura): cross-section 32,628 / x-ray 13,333。珍しくない描き方で、
-- 0066 の内側の部位を書く**最も強い根拠**でもある。
--
-- ── 根拠として要求はしない ──
-- (2) があるので、内側の部位を書くのに断面の descriptor を必須にはしない。
-- 「見えているものだけ書く」ではなく「**根拠があるものだけ書く**」が既定 (0053 と同じ線)。
--
-- ── evidence_type には入れない ──
-- `observation.evidence_type` は既にあるが、語彙は explicit_add / explicit_remove /
-- focused_retained / focused_uncertain の **レビュー工程の語**で、
-- 「どう見えているか」ではない。混ぜると 2 つの意味が 1 列に載る。
--
-- ロールバック: 追加した descriptor を DELETE (所見が付いていれば消せない)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO scene_descriptor(slug, display_name, facet, description) VALUES
  ('cross_section','断面図','rendering',
   '体を切った断面として内部を見せる描き方 (実測 cross-section 32,628)。'
   '**何が起きているかとは別の軸** — 「中に出している」は対象の部位で書く '
   '(`→ rectum` / `→ vaginal_canal`、0066)。断面で描かれていても射精しているとは限らず、'
   '断面でなくても中に出していることはある (0067、user 指摘)'),
  ('x_ray','透視','rendering',
   '体を透かして内部を見せる描き方 (実測 x-ray 13,333)。'
   '**断面 (cross_section) とは描き方が違う** — あちらは切り口、こちらは透過。'
   '内部の部位を書く根拠になるが、**根拠は断面や透視に限らない** — '
   '挿入されている / 外に出ていない、でも書ける')
ON CONFLICT (slug) DO NOTHING;

-- 内側の部位の側に、描き方は別軸だと書く
UPDATE body_region SET description = coalesce(description,'') ||
  '。**断面や透視で描かれているかは別軸** (scene_descriptor の cross_section / x_ray、0067) — '
  'そちらは描き方で、こちらは起きていること。断面でなくても書けるし、断面でも書かないことがある'
WHERE primary_class = 'internal_cavity'
  AND coalesce(description,'') NOT LIKE '%cross_section%';

COMMIT;
