-- Migration 0066 — 「内側に出た」と「外側にかかった」を分ける
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘:
-- 「anus の**外側に射精している**のと anus に**挿入された状態で直腸に射精している**状態を
--   分割する表現がない」
--
-- ── 体内の管腔が 1 つも無かった ──
-- body_region にある開口部は `anus` / `vaginal_opening` / `urethral_opening` / `mouth` の
-- **入口だけ**で、その奥を指す語が無い。だから
--   `ejaculating_onto (penis → anus)`
-- が**外にかかった**のか**中に出した**のかを言えず、2 つが同じ所見になっていた。
--
-- ── flat tag は合成語で分けている (そしてそれが多数派) ──
-- 実測 (production kura): cum_in_pussy 174,790 / cum_on_pussy 1,611 /
-- cum_in_ass 48,809 / cum_on_ass 15,999 / cum_in_mouth 103,014 /
-- internal_cumshot 26,340。**内と外は圧倒的に頻出の区別**で、
-- flat tag 側は `cum_in_X` / `cum_on_X` という**部位と方向を焼き付けた合成語**で分けている。
-- ここで同じ合成語を作れば 0045 (piercing) / 0054 (striking) と同じ組合せ爆発になる。
-- **入口と奥を別の部位にすれば、既存の関係のまま区別が付く。**
--   外にかかった — ejaculating_onto → anus
--   中に出した   — ejaculating_onto → rectum
--
-- ── 見えないものを書くことになるが、それは既に許している ──
-- 直腸や子宮は外からは見えない。だが 0053 で「**排出は見えていなくても起きている行為**」
-- として属性を人物全体に移したのと同じ話で、注釈者が見ているのは**根拠**の方
-- (挿入されている / 外に出ていない / 断面図)。
-- 実測でも断面の描写は珍しくない — cross-section 32,628 / x-ray 13,333 /
-- uterus 21,329 / cervix 19,316。**推測できないなら NULL** (何も書かない) でよい。
-- これは 0049 で phimosis を値にしなかったのとは別 — あちらは「剥けない」という
-- **静止画から原理的に観察できない性質**で、こちらは**この 1 枚の状態**。
--
-- ── 新しいクラス internal_cavity ──
-- `surface_mucosa` に入れない。人体表面前提の属性 (skin_tone、surface_substance の
-- 「表面に付着」など) が自動で付いてしまう。**「外から見えない」ことを型で言う**
-- (nonhuman_appendage を別クラスにしたのと同じ判断)。
--
-- ── 入れなかったもの ──
-- 口の奥は足していない。`mouth` は既に開口部であり**口腔そのもの**を指すので、
-- cum_in_mouth はそのまま `→ mouth` で書ける。
--
-- ロールバック: 追加した region / class を DELETE (所見が付いていれば消せない)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO region_class(slug, description) VALUES
  ('internal_cavity',
   '体内の管腔。**外からは見えない** — 挿入されている / 外に出ていない / 断面図 といった'
   '根拠から書く。**surface_mucosa に入れない** (体表前提の属性が自動で付いてしまう)。'
   '推測できないなら NULL、が既定 (0066)')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO body_region(slug, display_name, parent_slug, laterality, primary_class, description) VALUES
  ('rectum','直腸','pelvis','midline','internal_cavity',
   '直腸。**anus の奥**。外にかかったのか中に出したのかは**対象の部位で分ける** — '
   '外は `→ anus`、中は `→ rectum` (0066、user 指摘)。'
   'flat tag は cum_in_ass 48,809 / cum_on_ass 15,999 という合成語で分けている'),
  ('vaginal_canal','膣','pelvis','midline','internal_cavity',
   '膣。**vaginal_opening の奥**。外は `→ vulva` / `→ vaginal_opening`、中は `→ vaginal_canal`。'
   'flat tag は cum_in_pussy 174,790 / cum_on_pussy 1,611'),
  ('uterus','子宮','pelvis','midline','internal_cavity',
   '子宮。**vaginal_canal よりさらに奥**。断面や透視で描かれる (uterus 21,329 / '
   'cross-section 32,628 / x-ray 13,333)。internal_cumshot 26,340 のうち'
   '子宮まで描いているものはこちら'),
  ('cervix','子宮口','uterus','midline','internal_cavity',
   '子宮口。uterus と vaginal_canal の境。**当たっている / 開いている**が問える部位 '
   '(cervix 19,316)')
ON CONFLICT (slug) DO NOTHING;

-- 入口の側に「奥は別の部位」と書く
UPDATE body_region SET description = coalesce(description,'') ||
  '。**奥は rectum** (0066) — 外にかかったなら anus、中に出したなら rectum'
WHERE slug = 'anus' AND coalesce(description,'') NOT LIKE '%rectum%';

UPDATE body_region SET description = coalesce(description,'') ||
  '。**奥は vaginal_canal** (0066) — 外にかかったなら vaginal_opening、中なら vaginal_canal'
WHERE slug = 'vaginal_opening' AND coalesce(description,'') NOT LIKE '%vaginal_canal%';

COMMIT;
