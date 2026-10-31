-- Migration 0082 — 「踏みつけ」「足を載せる」の用途を説明に足す (語は増やさない)
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘 (post 405485):
-- 「載せている。踏みつけている状態」
--
-- ── 既存の 2 語で書ける ──
-- 載せている   = resting_on   (foot → 相手の部位)。荷重を預けるが持ち上げない
-- 踏みつけている = pressing_against (foot → 相手の部位)。接触 + 加重
-- 実測でも trampling / stepping_on は 0 件、foot_on_head 1 件で、flat tag 側も
-- 専用語をほぼ持たない。**新しい語を作らない** (ADR-0004、resting_on/pressing_against
-- の主体を foot にすれば書ける)。
--
-- ── striking (0054) と混同しない ──
-- 踏みつけは**持続する加重**で、蹴り (striking, subject=foot) の**瞬間の衝突**とは別。
-- 説明にこの線を書いて、注釈者が striking を選ばないようにする。
--
-- ── 変更は説明だけ ──
-- 語・値・列は一切足さない。検索で「踏み」「載せ」から辿れるようにするための追記。
--
-- ロールバック: 追記部分を削るだけ (機能的な影響なし)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

UPDATE relation SET description = coalesce(description,'') ||
  '。**足を載せる** (foot → 相手の部位) もこれ (0082) — 荷重を預けるだけ'
WHERE slug = 'resting_on' AND coalesce(description,'') NOT LIKE '%足を載せる%';

UPDATE relation SET description = coalesce(description,'') ||
  '。**踏みつけ** (foot → 相手の頭・背中など) もこれ (0082) — 接触 + 加重。'
  '**蹴り (striking) とは別** — あちらは瞬間の衝突、踏みつけは持続する加重'
WHERE slug = 'pressing_against' AND coalesce(description,'') NOT LIKE '%踏みつけ%';

COMMIT;
