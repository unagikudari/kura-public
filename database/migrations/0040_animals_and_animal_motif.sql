-- Migration 0040 — 動物そのものを主体として名指せるようにし、擬人化を分解する
--
-- plan kura-1.0-implementation、2026-07-30 の user 指摘:
-- 「人物の表現に犬や馬がいない」
--
-- ── 実測: species は擬人化キャラだけだった ──
-- 29 行のうち動物は 1 つも無い。あるのは `dog_girl` / `horse_girl` / `catgirl` /
-- `fox_girl` / `kemonomimi` / `furry` / `monster_girl` 等**すべて「人」側**の語で、
-- **動物そのもの (犬・馬) を主体として書く手段が無かった**。
-- 0030 で `entity_kind='creature'` を足して「人でも物でもない動作主」は書けるように
-- なっていたのに、**その主体が何なのかを言う語が無い**という中途半端な状態。
--
-- 実害: 第 2 ラウンドの母集団 (pool 7897) の **639940 は犬の口が外陰部にある画面**で、
-- 注釈者はその主体を名指せない。選定時に「非人間の口」とだけ書いたのがその証拠。
-- 需要 (production kura 実測): horse 6,882 / dog 5,426 / monkey 4,739 / gorilla 4,172 /
-- ape 2,051 / pig 2,037 / cat 1,014 / bird 962 / wolf 562 / fish 555 / snake 362 /
-- cow 209 / bear 148。行為側の `bestiality` は 19,201。
--
-- ── もう 1 つ見つかった: `_girl` が 2 つの事実を 1 語に混ぜている ──
-- `species.marked_by` を見ると catgirl / dog_girl / horse_girl / kemonomimi は
-- **全部 `animal_ears`** で、違いは**どの動物か**だけ。しかも `_girl` は
-- **見た目の性別**を含んでおり、それは `presentation` (0037) が既に持っている列。
-- つまり 1 語に「人である」「動物の耳がある」「どの動物か」「女性に見える」の
-- 4 つが詰まっていて、**presentation は二重の真実**になっている。
-- 動物を足すだけだと `dog` と `dog_girl` が並び、注釈者が**どちらを選ぶか決められない**。
-- so 分解の軸を同時に入れる。
--
-- ── 軸の設計: 「何であるか」と「何に似せてあるか」を分ける ──
-- (1) `species` に**実在の動物**を足す。`entity_kind='creature'` の主体に付ける。
-- (2) `obs_identity.motif_species_slug` を足す。**species を再参照する** —
--     動物の一覧を 2 つ持たない (motif 用の値リストを別に作ると同じ動物が 2 箇所に住む)。
--     「その人物が持つ動物の特徴の由来」を指す。
--     `dog_girl` は species=human + motif_species=dog + region animal_ear/tail
--     + presentation=female に分解される。
-- (3) `_girl` 系は**消さない**。`horse_girl` に観測が 1 件あり append-only なので
--     語彙も勝手に消せない (0033 で hand_on_own_hip を残したのと同じ)。
--     description に分解の書き方を書いて非推奨にする。
--
-- ── 粒度をどこで止めるか ──
-- 実測のある動物 + `animal` (種が決まらないとき) まで。`monkey`/`gorilla` は
-- `primate` 1 語にする — 分類を細かくすると際限が無く、注釈者が迷う。
-- 足りなければ後から足せる (species は追加が安全な表)。
-- **`animal` を置くのは「犬か狼か決められないとき」に dog と書かせないため** (非捏造)。
--
-- ── 運用手順 ──
--   * 単一 transaction。species 追加 → 列 + FK (NOT VALID → VALIDATE) → 説明。冪等。
--   * ロールバック: 追加した species を DELETE、列を DROP。
--     (観測が付いていれば消せない — その場合は retract が先)

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

-- =====================================================================
-- 1. 実在の動物 (entity_kind='creature' の主体として使う)
-- =====================================================================
INSERT INTO species(slug, display_name, marked_by) VALUES
  ('dog',     'dog',     'quadruped_canine'),
  ('wolf',    'wolf',    'quadruped_canine'),
  ('horse',   'horse',   'quadruped_equine'),
  ('cat',     'cat',     'quadruped_feline'),
  ('pig',     'pig',     'quadruped'),
  ('cow',     'cow',     'quadruped'),
  ('bear',    'bear',    'quadruped'),
  ('primate', 'primate', 'primate'),
  ('bird',    'bird',    'feathers_wings'),
  ('fish',    'fish',    'fins_scales'),
  ('snake',   'snake',   'serpentine'),
  ('animal',  'animal (種は不明)', '-')
ON CONFLICT (slug) DO NOTHING;

UPDATE species SET display_name = v.d FROM (VALUES
  ('dog',     '犬'),
  ('wolf',    '狼'),
  ('horse',   '馬'),
  ('cat',     '猫'),
  ('pig',     '豚'),
  ('cow',     '牛'),
  ('bear',    '熊'),
  ('primate', '霊長類 (猿・類人猿・ゴリラ)'),
  ('bird',    '鳥'),
  ('fish',    '魚'),
  ('snake',   '蛇'),
  ('animal',  '動物 (種が決まらない)')
) AS v(s,d) WHERE species.slug = v.s;

-- =====================================================================
-- 2. 「何に似せてあるか」を species の再参照で書く
-- =====================================================================
ALTER TABLE obs_identity ADD COLUMN IF NOT EXISTS motif_species_slug text;
ALTER TABLE obs_identity DROP CONSTRAINT IF EXISTS obs_identity_motif_species_fkey;
ALTER TABLE obs_identity ADD CONSTRAINT obs_identity_motif_species_fkey
    FOREIGN KEY (motif_species_slug) REFERENCES species(slug) NOT VALID;
ALTER TABLE obs_identity VALIDATE CONSTRAINT obs_identity_motif_species_fkey;

COMMENT ON COLUMN obs_identity.motif_species_slug IS
  'その主体が持つ**動物の特徴の由来**。「何であるか」(species_slug) とは別 — '
  '猫耳の人間は species=human + motif_species=cat。'
  '**species を再参照する**ので動物の一覧を 2 箇所に持たない。'
  'NULL は「言っていない」。'
  '`catgirl` / `dog_girl` のような合成語を増やさないための軸 — '
  'あれは「人である」「動物の耳がある」「どの動物か」「女性に見える」の 4 つを '
  '1 語に混ぜており、最後は presentation (0037) と二重になる';

COMMENT ON COLUMN obs_identity.species_slug IS
  'その主体が**何であるか**。人なら human、犬なら dog。'
  '**似せてあるだけのものは species ではない** — 猫耳の人間は human で、'
  '猫らしさは motif_species_slug に書く';

-- =====================================================================
-- 3. `_girl` 系を非推奨にする (行は残す — 観測が参照している)
-- =====================================================================
UPDATE species SET display_name = display_name || ' [非推奨]'
 WHERE slug IN ('catgirl','dog_girl','fox_girl','horse_girl','kemonomimi',
                'demon_girl','dragon_girl','slime_girl','monster_girl','monster_boy')
   AND display_name NOT LIKE '%[非推奨]%';

-- =====================================================================
-- 4. 動物を主体にするときの kind を明記する
-- =====================================================================
COMMENT ON COLUMN obs_identity.entity_kind IS
  '主体の種別。person / creature / object。'
  '**動物は creature** — person ではないので presentation は付かない (0038 の CHECK)。'
  '「猫耳の人間」は person で、動物らしさは motif_species_slug に書く';

COMMIT;

-- =====================================================================
-- 5. 解決形にも運ぶ (**宣言だけして着地させないのを避ける**)
-- =====================================================================
-- 0037 で presentation を「検証していたのに列が無い」まま放置していた失敗を繰り返さない。
-- 観測に書けるだけでは query が引けないので、resolved_facet にも列を足す。
BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

ALTER TABLE resolved_facet ADD COLUMN IF NOT EXISTS motif_species_slug text;
ALTER TABLE resolved_facet DROP CONSTRAINT IF EXISTS resolved_facet_motif_species_fkey;
ALTER TABLE resolved_facet ADD CONSTRAINT resolved_facet_motif_species_fkey
    FOREIGN KEY (motif_species_slug) REFERENCES species(slug) NOT VALID;
ALTER TABLE resolved_facet VALIDATE CONSTRAINT resolved_facet_motif_species_fkey;

COMMENT ON COLUMN resolved_facet.motif_species_slug IS
  '**何に似せてあるか** (obs_identity.motif_species_slug をそのまま運ぶ)。'
  '「何であるか」は species_slug。猫耳の人間は species=human / motif=cat で 1 行に入る';

COMMIT;
