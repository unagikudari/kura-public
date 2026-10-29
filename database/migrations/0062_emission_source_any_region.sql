-- Migration 0062 — 噴出物は「出どころの器官」に縛られない
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘:
-- 「全体の属性 (部位に紐づかないもの) は関係と言った。つまり **semen が
--   vaginal_opening から噴出している状態**や **scat が mouth から溢れている状態**を
--   表現できなければならない」
--
-- ── 0052 が出どころを器官に固定していた ──
-- relation_applicability の subject を
--   urinating_onto  → urethral_opening
--   ejaculating_onto → penis
--   defecating_onto  → anus
-- と決め打ちしていた。**これは「誰が作ったか」を出どころに混ぜている。**
-- 実際に画像で見えるのは「**どの部位から出ているか**」だけで、
--   * 膣口から精液が溢れている  (作ったのは相手)
--   * 口から便が溢れている
--   * 尻から尿が垂れている
-- はいずれも普通にある。器官に縛ると**これらが 1 つも書けない**。
-- 0053 で「排出は見えていなくても起きている行為だから部位に縛らない」と決めたのと
-- **同じ線** — あれは属性側、これは関係側。
--
-- ── 語は物質を指す。産出者は言わない (ここが要点) ──
-- `ejaculating_onto` の slug が言うのは**何が出ているか (精液)** であって、
-- **誰が射精したかではない**。膣口から溢れる精液も同じ語で書く。
-- 産出者を言いたいなら**別の所見**で言う (誰の陰茎が射精しているか)。
-- 語を「本人由来」と「逆流」に割ると、**同じ物質が 2 語になり**
-- 0054 で punch/kick を作らなかったのと同じ組合せ爆発になる。
-- 表示名も物質から始める形に直す (噴出物を主体に、user 指摘)。
--
-- ── 潮だけ関係が無かった ──
-- 0052 は 3 語しか作らず `squirting_state` に対応する関係が無かった。
-- 4 つで扱いが違うと注釈者が毎回考え直すので `squirting_onto` を足して揃える
-- (値空間を揃えた 0050 と同じ理由)。
--
-- ロールバック: 追加した relation を DELETE、applicability を器官固定に戻す。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO relation(slug, display_name, category, directionality) VALUES
  ('squirting_onto', '潮が出ている', 'interaction', 'directed')
ON CONFLICT (slug) DO NOTHING;

-- 出どころを器官に縛らない
UPDATE relation_applicability SET participant_key = 'any'
 WHERE relation_slug IN ('urinating_onto','ejaculating_onto','defecating_onto')
   AND role = 'subject';

INSERT INTO relation_applicability(relation_slug, role, participant_kind, participant_key) VALUES
  ('squirting_onto', 'subject', 'region', 'any'),
  ('squirting_onto', 'object',  'region', 'any')
ON CONFLICT DO NOTHING;

-- 表示名を物質から始める形に揃える
UPDATE relation SET display_name = v.n FROM (VALUES
  ('urinating_onto',   '尿が出ている'),
  ('ejaculating_onto', '精液が出ている'),
  ('defecating_onto',  '便が出ている'),
  ('squirting_onto',   '潮が出ている')
) AS v(s,n) WHERE relation.slug = v.s;

UPDATE relation SET description = v.d FROM (VALUES
  ('urinating_onto',
   '**尿**が どこから どこへ。出どころは**どの部位でもよい** (0062) — 見えているのは'
   '「どの部位から出ているか」だけで、器官に縛ると服の上からの染みや尻から垂れる形が書けない。'
   '対象は相手の部位か環境 (地面 / 空中 など)。行き先が見えないなら**対象を省く**。'
   '**出ているかだけなら属性 urination_state** (0050)'),
  ('ejaculating_onto',
   '**精液**が どこから どこへ。**語が言うのは物質であって産出者ではない** (0062、user 指摘) — '
   '膣口や口から溢れている精液も同じ語で書き、誰が射精したかは**別の所見**で言う。'
   '出どころは**どの部位でもよい**。'
   '**出ているかだけなら属性 ejaculation_state**、**着地した後は surface_substance=semen**'),
  ('defecating_onto',
   '**便**が どこから どこへ。出どころは**どの部位でもよい** (0062) — 口から溢れる形もある。'
   '**出ているかだけなら属性 defecation_state** (0051)'),
  ('squirting_onto',
   '**潮**が どこから どこへ。0052 は 3 語しか作らず潮だけ関係が無かったので揃えた (0062)。'
   '出どころは**どの部位でもよい**。**出ているかだけなら属性 squirting_state**')
) AS v(s,d) WHERE relation.slug = v.s;

UPDATE attribute SET description = coalesce(description,'') ||
  '。**どこへ飛んでいるかは関係** (squirting_onto、0062)'
WHERE slug = 'squirting_state' AND coalesce(description,'') NOT LIKE '%squirting_onto%';

COMMIT;
