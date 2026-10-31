-- Migration 0081 — 拘束具 (縄・手錠・猿轡) を「身につけているもの」で書けるようにする
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘:
-- 「**拘束されている 縛られている 手錠をかけられている状態がない**」
--
-- ── restraining (0071) は行為、これは物 ──
-- `restraining` は「**人が相手を拘束する行為**」(羽交い締め等)。user が言うのは
-- **縛られている状態**で、原因は**物** (縄・手錠) であることが多い。
-- 0071 で「主体は人とは限らない — 縄や拘束具も拘束する」と書いたが、それには
-- 縄の entity を作る必要があり重い。**着いている物として書く**方が素直。
--
-- ── object 機構に載せる (0045/0069 と同じ) ──
-- 手錠・猿轡は**部位に着く物** (手錠→手首、猿轡→口) なので、piercing と同じ
-- `objects` の欄で書ける。**部位ごとの語を作らない** — handcuffs を wrist に、
-- gag を mouth に、というのは所見ごとに region で指定する。
-- 実測 (production kura): bound 25,770 / restrained 27,845 / bondage 18,553 /
-- handcuffs 4,151 / shibari 4,760 / ball_gag 3,136 / armbinder 353。
--
-- ── 縄 (縛り) は全身に回る ──
-- shibari は 1 部位に収まらない。region を torso / whole にして書ける
-- (object の region は任意なので、部位を特定できないなら省く)。
--
-- ── 「動きを封じられている」は別に書かない ──
-- 拘束具が着いていれば動きは封じられている、は**導ける** (0019/0049/0060 と同じ)。
-- movement_restricted のような属性を足さない。
--
-- ── 目隠しも同じ枠 ──
-- blindfold は視界を塞ぐ物で、拘束具と同じく着いている物。ball_gag / blindfold /
-- rope / handcuffs / chain / armbinder を足す。
--
-- ロールバック: 追加した object_type を DELETE (所見が付いていれば消せない)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO object_type(slug, display_name, category, participation, slot, description) VALUES
  ('rope',      '縄',     'prop', 'wearing', NULL,
   '体に巻かれた縄。緊縛 (shibari 4,760)。**1 部位に収まらない**ので region は torso / '
   'whole にするか省く。**縛る行為者がいれば relation restraining も併記できる** (0071)'),
  ('handcuffs', '手錠',   'prop', 'wearing', NULL,
   '手錠。region=wrist。実測 handcuffs 4,151 / bound_wrists 10,961'),
  ('chain',     '鎖',     'prop', 'wearing', NULL, '鎖で繋がれている / 巻かれている'),
  ('armbinder', 'アームバインダー', 'prop', 'wearing', NULL,
   '両腕を背中で束ねる拘束具 (armbinder 353)'),
  ('gag',       '猿轡',   'prop', 'wearing', NULL,
   '口を塞ぐ拘束具。region=mouth。ball_gag 3,136 などは variant で分ける'),
  ('blindfold', '目隠し', 'prop', 'wearing', NULL,
   '視界を塞ぐ物。**拘束具と同じ枠** — 着いている物として書く')
ON CONFLICT (slug) DO NOTHING;

UPDATE relation SET description = coalesce(description,'') ||
  '。**縛られている「状態」は object** (rope / handcuffs、0081) — こちらは拘束する行為'
WHERE slug = 'restraining' AND coalesce(description,'') NOT LIKE '%0081%';

COMMIT;
