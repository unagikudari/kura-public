-- Migration 0030 — 非人間の体を書けるようにする (触手が表現できなかった)
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-29:
-- 「post 734537 について tentacles を表現するのが難しい」。
--
-- ── 3 つの穴 ──
-- (1) `entity_kind` が person / object の 2 つだけ。触手の主は**人でも物でもない**。
--     物にすると「動作主でない」ことになり、grasping / inserting_into の主語にできない。
-- (2) `body_region` 119 語が**すべて人体**。触手・尾・翼・角・爪が無い。
--     `species` には lamia / slime_girl / harpy / monster_girl がいるのに、
--     **その体を記述する語が 1 つも無い**。宣言と着地の乖離の 7 件目。
-- (3) 主が見えない。触手は壁 (tentacle_pit) から出ていて本体が画面外。
--
-- 実測 (kura コーパス): tail 40,627 / horns 30,588 / wings 13,317 / tentacles 7,943 /
-- claws 1,224 / disembodied_limb 1,131。**触手だけの話ではない** — 尾と角の方が多い。
--
-- ── 触手を「何本目か」で区別しない (意図的) ──
-- 734537 には 8 本以上の触手が別々のことをしている (膣・肛門へ挿入 / 首を絞める /
-- 腕を拘束 / 吊る)。「触手 #1 が膣、#3 が首」と書きたくなるが、**同じ触手かどうかは
-- 画像から決まらない**ことが多い。番号を振ると、決まっていない同一性を主張することになる。
-- よって instance 番号は**足さない**。書けるのは
--   creature_1.tentacle inserting_into person_1.vaginal_opening
--   creature_1.tentacle grasping       person_1.neck
-- の 2 つの事実で、これは**実際に観測できることと過不足なく一致する**。
-- 位置で区別したいときは 0028 の anchor (点は任意の部位を指せる) で個々の触手を指す。
--
-- ── 運用手順 ──
--   * 単一 transaction。entity_kind → region_class → body_region → 関係の適用先。
--   * 冪等。ロールバック: 追加した seed 行を DELETE (観測が参照していれば FK が拒否)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- =====================================================================
-- 1. 人でも物でもない動作主
-- =====================================================================
INSERT INTO entity_kind(slug, description) VALUES
  ('creature',
   '非人間の動作主。person でも object でもないもの (触手の主、獣、スライム等)。'
   '**object と分けるのは動作主かどうかで決まる** — 触手は掴み挿入するので object では'
   '説明できない。person と分けるのは、人としての属性 (presentation 等) を要求しないため')
ON CONFLICT (slug) DO NOTHING;

-- =====================================================================
-- 2. 非人間の部位のクラス
-- =====================================================================
INSERT INTO region_class(slug, description) VALUES
  ('nonhuman_appendage',
   '非人間の付属肢。触手・尾・翼・角・爪など。人体の region とは別クラスにするのは、'
   '人体前提の属性 (skin_tone 等) が自動で付かないようにするため')
ON CONFLICT (slug) DO NOTHING;

-- =====================================================================
-- 3. 非人間の部位
-- =====================================================================
INSERT INTO body_region(slug, display_name, parent_slug, laterality, primary_class) VALUES
  ('tentacle', 'tentacle', 'body', 'none',      'nonhuman_appendage'),
  ('tail',     'tail',     'body', 'midline',   'nonhuman_appendage'),
  ('wing',     'wing',     'body', 'bilateral', 'nonhuman_appendage'),
  ('horn',     'horn',     'head', 'bilateral', 'nonhuman_appendage'),
  ('claw',     'claw',     'body', 'bilateral', 'nonhuman_appendage'),
  ('animal_ear','animal ear','head','bilateral','nonhuman_appendage'),
  ('fin',      'fin',      'body', 'bilateral', 'nonhuman_appendage')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO region_class_map(region_slug, class_slug) VALUES
  ('tentacle','nonhuman_appendage'), ('tail','nonhuman_appendage'),
  ('wing','nonhuman_appendage'),     ('horn','nonhuman_appendage'),
  ('claw','nonhuman_appendage'),     ('animal_ear','nonhuman_appendage'),
  ('fin','nonhuman_appendage')
ON CONFLICT DO NOTHING;

UPDATE body_region SET description = v.d FROM (VALUES
  ('tentacle',
   '触手。**本数は数えない** — 同じ触手が別の場所にあるのか別の触手なのかは画像から'
   '決まらないことが多く、番号を振ると決まっていない同一性を主張することになる。'
   '個々を指したいときは位置の点 (anchor) で指す'),
  ('tail',      '尾'),
  ('wing',      '翼'),
  ('horn',      '角'),
  ('claw',      '爪 (獣の鉤爪。人の指の爪は fingernail)'),
  ('animal_ear','獣耳。人の耳は ear'),
  ('fin',       'ひれ')
) AS v(s,d) WHERE body_region.slug = v.s;

-- =====================================================================
-- 4. 非人間の部位が関係に参加できるようにする
-- =====================================================================
-- grasping の主語は 0003 seed で participant_key='hand' に限定されていた。
-- 触手が掴む・尾が巻き付くのを書けるようにするには any へ広げる必要がある。
-- **人体前提を語彙に焼き込んでいた**ので、その前提を外す。
UPDATE relation_applicability SET participant_key = 'any'
 WHERE relation_slug IN ('grasping','holding') AND role = 'subject'
   AND participant_kind = 'region' AND participant_key = 'hand';

UPDATE relation SET description = description ||
  '。**掴む主体は手とは限らない** — 触手・尾・口も掴む (0030 で人体前提を外した)'
WHERE slug = 'grasping' AND description NOT LIKE '%手とは限らない%';

COMMIT;
