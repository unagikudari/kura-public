-- Migration 0054 — 打撃の関係を足し、環境がどの関係でも対象になることを明示する
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘 3 連:
--   「別に地面や空中はほかの行為にあってもいい。宙をつかんでもいいし、
--     地面やシーツを grasp してもいい」
--   「人を punch してもいいし地面を kick してもいい」
--   「壁も」
--
-- ══════════════════════════════════════════════════════════════════════════
-- 1. 環境は排出専用ではない (0052 の一般化)
-- ══════════════════════════════════════════════════════════════════════════
-- `obs_relation.object_surface_slug` も CHECK も **relation 共通**なので、
-- 実は最初から全関係で使える。ただし 0052 の説明が排出の文脈でしか書いていなかったので、
-- **どの関係でも対象にできる**ことを表のコメントに明記する。
--   * 宙を掴む      — grasping    → air
--   * シーツを握る  — grasping    → bedding
--   * 地面に手をつく — resting_on  → ground
--   * 壁を蹴る      — striking    → wall
-- `bedding` (シーツ・布団) を追加済み — **家具と分ける**。掴む対象になるのは骨組みでは
-- なく布の方で、実測 bed_sheet 78,387 / sheet_grab 20,823 と掴む描写に強く結びついている。
--
-- ══════════════════════════════════════════════════════════════════════════
-- 2. 打撃の関係が丸ごと無かった
-- ══════════════════════════════════════════════════════════════════════════
-- contact 系は touching / pressing_against / resting_on / supporting しかなく、
-- **叩く・蹴るを表す語が 1 つも無かった**。
-- 実測: strangling 8,403 / spanking 4,401 / biting 1,216 / slapping 1,187 /
-- kicking 505 / punching 335。
--
-- ── punch / kick を別々の語にしない (ここが要点) ──
-- **どこで打つかは `subject_region_slug` が既に持っている**。
--   punch    = striking (subject=hand)
--   kick     = striking (subject=foot)
--   headbutt = striking (subject=head)
--   spanking = striking (subject=hand, object=buttocks)
-- 語を分けると部位ごとに語が増え、`piercing` を部位ごとに作らなかった 0045 と
-- 同じ組合せ爆発になる。**1 語 + 部位**で全部書ける。
--
-- ── 噛む・絞めるは別の語 ──
-- `biting` は歯で挟む、`strangling` は締め続ける — どちらも**打撃ではない**
-- (瞬間の衝突ではなく持続する接触)。`striking` に畳むと「殴っている」と「噛んでいる」が
-- 同じ事実になるので分ける。
--
-- ── 拳か平手かは書けない (既知の限界) ──
-- punch と slap は**どちらも手**で、違いは手の形。手の形を言う語は現状 0 件
-- (`clenched_hand` 585 に相当する pose が無い)。**無い軸を埋めるより先に、
-- 打撃そのものを書けるようにする** — 手の形は必要になったら別途足す。
--
-- ── 運用手順 ──
--   * 単一 transaction。relation → 適用先 → 説明。冪等。
--   * ロールバック: 追加した relation を DELETE (所見が付いていれば消せない)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO relation(slug, display_name, category, directionality) VALUES
  ('striking',   '打つ・蹴る', 'interaction', 'directed'),
  ('biting',     '噛んでいる', 'interaction', 'directed'),
  ('strangling', '絞めている', 'interaction', 'directed')
ON CONFLICT (slug) DO NOTHING;

UPDATE relation SET description = v.d FROM (VALUES
  ('striking',
   '打つ・蹴る。**どこで打つかは主体の部位が持つ** — '
   'punch は subject=hand、kick は subject=foot、headbutt は subject=head、'
   'spanking は subject=hand + object=buttocks。'
   '**部位ごとの語を作らない** (0045 で piercing を部位ごとに作らなかったのと同じ)。'
   '対象は相手の部位でも**環境でもよい** — 壁を蹴る / 地面を叩く。'
   '**噛む (biting) / 絞める (strangling) は別** — あちらは持続する接触で、'
   '瞬間の衝突ではない。'
   '**拳か平手かは今は書けない** — 手の形を言う語が無い (既知の限界)'),
  ('biting',
   '歯で挟んでいる。**打撃ではない** (持続する接触)。主体は口 / 歯'),
  ('strangling',
   '締め続けている。**打撃ではない** (持続する接触)。主体は手 / 腕 / 紐など')
) AS v(s,d) WHERE relation.slug = v.s;

INSERT INTO relation_applicability(relation_slug, role, participant_kind, participant_key) VALUES
  ('striking',   'subject', 'region', 'any'), ('striking',   'object', 'region', 'any'),
  ('biting',     'subject', 'region', 'any'), ('biting',     'object', 'region', 'any'),
  ('strangling', 'subject', 'region', 'any'), ('strangling', 'object', 'region', 'any')
ON CONFLICT DO NOTHING;

-- 環境がどの関係でも対象になることを表に明記する
COMMENT ON TABLE scene_surface IS
  '関係の対象になりうる**環境**。人体の部位ではないので body_region には入れない。'
  '**空中は物ではない**ので object_type にも入れない (0052、user 判断)。'
  '**排出専用ではない** (0054、user 指摘) — 宙を掴む (grasping → air)、'
  'シーツを握る (grasping → bedding)、地面に手をつく (resting_on → ground)、'
  '壁を蹴る (striking → wall) のように**どの関係でも対象にできる**。'
  '列も CHECK も relation 共通なので、新しい関係を足しても自動的に使える';

COMMIT;
