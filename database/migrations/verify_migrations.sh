#!/usr/bin/env bash
# plan kura-1.0-implementation S-8/S-9 で入れた migration (0005 以降) の検証。
#
# なぜ要るか: 0002/0003 には verify_0002_0003.sh があるのに、S-8/S-9 で足した分には
# 自動検査が無かった (2026-07-28)。これらは「語彙が足りず書けなかった」を埋めた
# migration なので、**壊れても気付けるのは注釈中の人だけ**という状態だった。
#
# **ファイル名に番号を入れない** (2026-07-29 に verify_0005_0035.sh から改名)。
# 番号入りの名前は migration を足すたびに改名が要り、実際 0036-0038 を足したときに
# 名前だけ古いまま残った。名前が中身に追従しないなら、名前から中身を外す。
#
# 検査するのは 3 種類:
#   (a) 語彙・列が実在するか (足したものが本当に入っているか)
#   (b) 不変条件が**機構として**効いているか (CHECK / FK が違反を弾くか)
#   (c) 二重の真実を作っていないか (orientation を保存しない、等)
#
# 誤爆ガードは verify_0002_0003.sh と同じ — fixture を INSERT するので probe 本体
# (kura1) に走らせると observation が append-only で消せず作り直しになる。
set -uo pipefail
PSQL=(psql -h 127.0.0.1 -p 5540 -U desk -d "${VERIFY_DB:-kura1_v2_verify}" -qtA)
pass=0; fail=0

guard_db=$("${PSQL[@]}" -c "SELECT current_database()" 2>/dev/null || echo "")
case "$guard_db" in
  *_verify) : ;;
  "") echo "FATAL: 検証 DB に接続できません (kura1_verify を作ってから実行)"; exit 2 ;;
  *) echo "FATAL: '$guard_db' は throwaway ではありません。"; exit 2 ;;
esac
probe_rows=$("${PSQL[@]}" -c "SELECT count(*) FROM kura.asset_source" 2>/dev/null || echo 0)
if [ "${probe_rows:-0}" -gt 0 ]; then
  echo "FATAL: '$guard_db' に probe データ ($probe_rows 件) があります。中止。"; exit 2
fi

ok() { echo "  PASS  $1"; pass=$((pass+1)); }
ng() { echo "  FAIL  $1"; fail=$((fail+1)); }
expect_val() {
  local got; got=$("${PSQL[@]}" -c "SET search_path=kura,public; $2" 2>&1 | tr -d '[:space:]')
  [ "$got" = "$3" ] && ok "$1 (= $got)" || ng "$1 (期待 $3 / 実際 $got)"
}
expect_reject() {
  local out; out=$("${PSQL[@]}" -c "SET search_path=kura,public; $2" 2>&1)
  if echo "$out" | grep -qiE "ERROR|エラー"; then ok "$1 — 制約が弾いた"
  else ng "$1 — 弾かれずに通った"; fi
}
expect_accept() {
  local out; out=$("${PSQL[@]}" -c "SET search_path=kura,public; $2" 2>&1)
  if echo "$out" | grep -qiE "ERROR|エラー"; then
    ng "$1 — 通るべきなのに弾かれた: $(echo "$out" | head -2 | tr '\n' ' ')"
  else ok "$1"; fi
}

echo "== (a) 足した語彙が実在するか =="
expect_val "0005 species horse_girl" \
  "SELECT count(*) FROM species WHERE slug='horse_girl';" 1
expect_val "0008 erection_state に half_erect" \
  "SELECT count(*) FROM attribute_value WHERE attribute_slug='erection_state' AND value_slug='half_erect';" 1
expect_val "0008 体毛は region + hair_density (専用属性を作っていない)" \
  "SELECT count(*) FROM attribute WHERE slug LIKE '%hair%amount%' OR slug LIKE 'pubic_hair%';" 0
expect_val "0008 region pubic_hair / armpit_hair が class=hair" \
  "SELECT count(*) FROM body_region WHERE slug IN ('pubic_hair','armpit_hair') AND primary_class='hair';" 2
expect_val "0009 head scope の pose (0 件だった)" \
  "SELECT count(*) FROM pose WHERE scope='head';" 5
expect_val "0009 合成語を足していない (missionary/cowgirl/doggystyle)" \
  "SELECT count(*) FROM pose WHERE slug IN ('missionary','cowgirl_position','doggystyle','amazon_position');" 0
expect_val "0011/0022 facial_expression が 22 語" \
  "SELECT count(*) FROM attribute_value WHERE attribute_slug='facial_expression';" 22
expect_val "0022 耐えている/苦痛は表情、食いしばりは口の形 (軸を混ぜていない)" \
  "SELECT (SELECT count(*) FROM attribute_value WHERE attribute_slug='facial_expression'
             AND value_slug IN ('enduring','pain'))
        + (SELECT count(*) FROM attribute_value WHERE attribute_slug='mouth_state'
             AND value_slug='clenched')
        + (SELECT count(*) FROM attribute_value WHERE attribute_slug='facial_expression'
             AND value_slug='clenched');" 3
expect_val "0013/0017 ejaculation_state に precum、squirting_state が別属性" \
  "SELECT (SELECT count(*) FROM attribute_value WHERE attribute_slug='ejaculation_state' AND value_slug='precum')
        + (SELECT count(*) FROM attribute WHERE slug='squirting_state');" 2
expect_val "0017 squirting_state に precum は無い (値空間を混ぜていない)" \
  "SELECT count(*) FROM attribute_value WHERE attribute_slug='squirting_state' AND value_slug='precum';" 0
expect_val "0014/0015/0017 surface_substance に smegma/scat/female_ejaculate" \
  "SELECT count(*) FROM attribute_value WHERE attribute_slug='surface_substance'
     AND value_slug IN ('smegma','scat','female_ejaculate');" 3

expect_val "0023 震え・痙攣は entity 全体の属性 (pose ではない)" \
  "SELECT count(*) FROM attribute WHERE slug='involuntary_motion' AND applies_kind='entity';" 1
expect_val "0023 trembling を pose に足していない (構えと状態を混ぜない)" \
  "SELECT count(*) FROM pose WHERE slug IN ('trembling','twitching','shaking');" 0
expect_val "0023 orgasm を値にしていない (震え = 絶頂ではない)" \
  "SELECT count(*) FROM attribute_value WHERE attribute_slug='involuntary_motion'
     AND value_slug='orgasm';" 0

expect_val "0035 VIO は region 3 つ + 共有の hair_density (属性を増やしていない)" \
  "SELECT (SELECT count(*) FROM body_region WHERE slug IN ('pubic_hair','perineal_hair','perianal_hair'))
        + (SELECT count(*) FROM attribute WHERE slug LIKE '%hair%amount%' OR slug LIKE '%_hair_density');" 3
expect_val "0035 VIO を slug にしていない (解剖学名 + description で引ける)" \
  "SELECT (SELECT count(*) FROM body_region WHERE slug LIKE 'v_line%' OR slug LIKE 'i_line%' OR slug LIKE 'o_line%')
        + (SELECT count(*) FROM body_region WHERE description LIKE '%I ライン%');" 1

# 0036 は列コメントだけの migration なので、検査対象も**定義文そのもの**になる。
# 定義が消えれば left/right は再び解釈不能になる — コメントが消えたことに気付ける
# のはこの検査だけ。
expect_val "0036 laterality が解剖学的左右と定義されている (3 表)" \
  "SELECT count(*) FROM (
     SELECT col_description(c.oid, a.attnum) d
       FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
       JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='kura' AND a.attname='laterality'
        AND c.relname IN ('obs_attribute','obs_pose')
     UNION ALL
     SELECT col_description(c.oid, a.attnum)
       FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
       JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='kura' AND c.relname='obs_relation'
        AND a.attname IN ('subject_laterality','object_laterality')) x
   WHERE d LIKE '%その人物にとっての左右%';" 4
expect_val "0036 NULL (言っていない) と na (左右の別が無い) を別物と定義している" \
  "SELECT count(*) FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
     JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='kura' AND c.relname='obs_attribute' AND a.attname='laterality'
      AND col_description(c.oid,a.attnum) LIKE '%言っていない%';" 1
expect_val "0036 body_region.laterality (対で存在するか) を観測側と混同していない" \
  "SELECT count(*) FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
     JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='kura' AND c.relname='body_region' AND a.attname='laterality'
      AND col_description(c.oid,a.attnum) LIKE '%別の概念%';" 1

expect_val "0037 見た目の性別表現を観測に書ける (検証だけして捨てていない)" \
  "SELECT count(*) FROM information_schema.columns WHERE table_schema='kura'
     AND table_name='obs_identity' AND column_name='presentation';" 1
expect_reject "0037 未知の presentation" \
  "INSERT INTO obs_identity(observation_id,presentation) VALUES (-1,'unknown_pres');"
expect_val "0037 人数を別に保存する列を作っていない (entity を数える)" \
  "SELECT count(*) FROM information_schema.columns WHERE table_schema='kura'
     AND column_name IN ('entity_count','person_count','girl_count','boy_count');" 0

expect_val "0038 entity の CHECK が kind を列挙していない (kind が増えても壊れない)" \
  "SELECT count(*) FROM pg_constraint
    WHERE conrelid='kura.entity'::regclass AND contype='c'
      AND pg_get_constraintdef(oid) LIKE '%creature%';" 0
expect_val "0038 列の持ち主で書いた CHECK が 2 本とも妥当 (NOT VALID のままでない)" \
  "SELECT count(*) FROM pg_constraint
    WHERE conrelid='kura.entity'::regclass AND convalidated
      AND conname IN ('ck_entity_object_slug_only_object','ck_entity_presentation_only_person');" 2
expect_val "0038 kind を列挙していた旧 entity_check が残っていない" \
  "SELECT count(*) FROM pg_constraint
    WHERE conrelid='kura.entity'::regclass AND conname='entity_check';" 0

expect_val "0039 陰茎の形は penis の属性 1 本 (horse_penis を region にしていない)" \
  "SELECT (SELECT count(*) FROM attribute WHERE slug='penis_morphology' AND applies_key='penis')
        + (SELECT count(*) FROM body_region WHERE slug LIKE '%horse%' OR slug LIKE '%dog_penis%');" 1
expect_val "0039 瘤は部位で、形態の値に畳んでいない (瘤のある人型が書ける)" \
  "SELECT (SELECT count(*) FROM body_region WHERE slug='bulbus_glandis' AND parent_slug='penis')
        + (SELECT count(*) FROM attribute_value WHERE attribute_slug='penis_morphology'
             AND value_slug LIKE '%knot%');" 1
expect_val "0039 判らないときに human と書かせない値がある" \
  "SELECT count(*) FROM attribute_value WHERE attribute_slug='penis_morphology'
     AND value_slug='other';" 1
expect_val "0039 表面の質感 (veiny) を形態の値に混ぜていない" \
  "SELECT count(*) FROM attribute_value WHERE attribute_slug='penis_morphology'
     AND value_slug IN ('veiny','veined');" 0
expect_reject "0039 未知の形態" \
  "INSERT INTO obs_attribute(observation_id,attribute_slug,value_slug)
     VALUES (-1,'penis_morphology','dragon');"

expect_val "0040 動物そのものが species にある (犬・馬を主体として名指せる)" \
  "SELECT count(*) FROM species WHERE slug IN ('dog','horse','cat','wolf','primate');" 5
expect_val "0040 種が決まらないときに dog と書かせない値がある" \
  "SELECT count(*) FROM species WHERE slug='animal';" 1
expect_val "0040 動物の一覧を 2 箇所に持っていない (motif は species を再参照)" \
  "SELECT count(*) FROM pg_constraint
    WHERE conname='obs_identity_motif_species_fkey' AND convalidated
      AND confrelid='kura.species'::regclass;" 1
expect_val "0040 motif が解決形にも運ばれる (宣言だけで着地しないのを避ける)" \
  "SELECT count(*) FROM information_schema.columns WHERE table_schema='kura'
     AND table_name='resolved_facet' AND column_name='motif_species_slug';" 1
expect_val "0040 _girl 系は残っているが非推奨と明記されている (観測が参照するので消せない)" \
  "SELECT count(*) FROM species WHERE slug IN ('catgirl','dog_girl','horse_girl')
     AND display_name LIKE '%[非推奨]%';" 3
expect_reject "0040 未知の motif" \
  "INSERT INTO obs_identity(observation_id,motif_species_slug) VALUES (-1,'unicorn_x');"

expect_val "0041 獣頭・半人半獣が species にある (ミノタウロス等)" \
  "SELECT count(*) FROM species WHERE slug IN
     ('minotaur','centaur','werewolf','tengu','dwarf','dragon','slime');" 7
expect_val "0041 _girl 側だけあって本体が無い非対称が残っていない" \
  "SELECT count(*) FROM species s WHERE s.slug LIKE '%_girl'
     AND NOT EXISTS (SELECT 1 FROM species b
                      WHERE b.slug = replace(s.slug,'_girl','')) 
     AND s.slug NOT IN ('monster_girl','demon_girl','fox_girl','catgirl','kemonomimi');" 0
expect_val "0041 species と motif の境界が列コメントに書かれている" \
  "SELECT count(*) FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
     JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='kura' AND c.relname='obs_identity'
      AND a.attname IN ('species_slug','motif_species_slug')
      AND col_description(c.oid,a.attnum) LIKE '%人体として描かれているか%'
       OR (n.nspname='kura' AND c.relname='obs_identity'
           AND a.attname='motif_species_slug'
           AND col_description(c.oid,a.attnum) LIKE '%motif ではなく species%');" 2

expect_val "0042 中身の盛り上がりは class=soft_tissue の 1 属性 (部位ごとに作っていない)" \
  "SELECT (SELECT count(*) FROM attribute WHERE slug='content_bulge' AND applies_key='soft_tissue')
        + (SELECT count(*) FROM attribute WHERE slug IN ('stomach_bulge','throat_bulge','cheek_bulge'));" 1
expect_val "0042 soft_tissue は腹・喉・頬の 3 部位だけ (surface_skin に付けていない)" \
  "SELECT count(*) FROM region_class_map WHERE class_slug='soft_tissue';" 3
expect_val "0042 輪郭 (belly_shape) と中身の形 (content_bulge) を分けている" \
  "SELECT (SELECT count(*) FROM attribute WHERE slug='belly_shape')
        + (SELECT count(*) FROM attribute WHERE slug='content_bulge');" 2
expect_val "0042 「見たが無かった」と「言っていない」を分ける値がある" \
  "SELECT count(*) FROM attribute_value WHERE attribute_slug='content_bulge' AND value_slug='none';" 1
expect_val "0042 原因 (何が入っているか) を属性側に書いていない" \
  "SELECT count(*) FROM attribute_value WHERE attribute_slug='content_bulge'
     AND value_slug IN ('penis','fluid','object','cum');" 0

expect_val "0043 脚を頭の向こうへ / 腰が頭より高い が書ける" \
  "SELECT count(*) FROM pose WHERE slug IN ('legs_over_head','hips_above_head');" 2
expect_val "0043 体位の合成語を足していない (piledriver / folded)" \
  "SELECT count(*) FROM pose WHERE slug IN ('piledriver','folded','mating_press',
     'suspended_congress','top_down_bottom_up');" 0
expect_val "0043 上下 (腰が頭より高い) を body_orientation の値に混ぜていない" \
  "SELECT count(*) FROM attribute_value WHERE attribute_slug='body_orientation'
     AND value_slug IN ('inverted','upside_down','upright');" 0
expect_val "0043 hips_above_head は寝ている向きと独立 (whole_body scope)" \
  "SELECT count(*) FROM pose WHERE slug='hips_above_head' AND scope='whole_body';" 1
expect_val "0043 pose の category を新設していない" \
  "SELECT count(DISTINCT category) FROM pose;" 6

expect_val "0044 付着した毛が汚れの値にある" \
  "SELECT count(*) FROM attribute_value WHERE attribute_slug='surface_substance'
     AND value_slug='hair';" 1
expect_val "0044 生えている毛は region のまま (属性に持ち込んでいない)" \
  "SELECT (SELECT count(*) FROM body_region WHERE slug='pubic_hair' AND primary_class='hair')
        + (SELECT count(*) FROM attribute_value WHERE attribute_slug='surface_substance'
             AND value_slug IN ('pubic_hair','armpit_hair','beard'));" 1
expect_val "0044 由来の人物を値に持ち込んでいない (semen 等と同じ非対称を作らない)" \
  "SELECT count(*) FROM attribute_value WHERE attribute_slug='surface_substance'
     AND (value_slug LIKE '%partner%' OR value_slug LIKE '%own_%');" 0

expect_val "0045 身につけているものに部位が付く (ピアスの位置が言える)" \
  "SELECT count(*) FROM information_schema.columns WHERE table_schema='kura'
     AND table_name='obs_object' AND column_name='region_slug';" 1
expect_val "0045 部位を語に畳んでいない (nipple_piercing のような合成語を作らない)" \
  "SELECT (SELECT count(*) FROM object_type WHERE slug='piercing')
        + (SELECT count(*) FROM object_type WHERE slug LIKE '%\\_piercing');" 1
expect_reject "0045 未知の部位" \
  "INSERT INTO obs_object(observation_id,object_slug,region_slug)
     VALUES (-1,'piercing','nowhere_x');"
expect_val "0045 解決形にも object の列がある (書けても引けない状態を作らない)" \
  "SELECT count(*) FROM information_schema.columns WHERE table_schema='kura'
     AND table_name='resolved_facet' AND column_name IN ('object_slug','region_slug');" 2
expect_val "0045 object_type の category を新設していない" \
  "SELECT count(DISTINCT category) FROM object_type;" 4

expect_val "0046 乳輪の隆起は乳輪の属性 (乳首に置いていない)" \
  "SELECT (SELECT count(*) FROM attribute WHERE slug='areola_projection' AND applies_key='areola')
        + (SELECT count(*) FROM attribute_value WHERE attribute_slug='nipple_state'
             AND value_slug='puffy');" 1
expect_val "0046 陥没した乳首と盛り上がった乳輪が同時に書ける (別属性)" \
  "SELECT count(*) FROM attribute WHERE slug IN ('nipple_state','areola_projection');" 2
expect_val "0046 広さ (areola_size) と隆起 (areola_projection) を分けている" \
  "SELECT count(*) FROM attribute WHERE applies_key='areola';" 2
expect_val "0046 形は object ごとに表を分けていない" \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='kura'
     AND table_name LIKE '%_variant';" 1
expect_val "0046 部位 × 形 の合成語を作っていない" \
  "SELECT count(*) FROM object_type WHERE slug LIKE 'nipple\\_%' OR slug LIKE '%\\_rings'
     OR slug LIKE 'hoop\\_%' OR slug LIKE 'stud\\_%';" 0
expect_reject "0046 数に 0 は書けない (無いは所見を書かないことで表す)" \
  "INSERT INTO obs_object(observation_id,object_slug,item_count) VALUES (-1,'piercing',0);"
expect_val "0046 形と数が解決形にも運ばれる" \
  "SELECT count(*) FROM information_schema.columns WHERE table_schema='kura'
     AND table_name='resolved_facet'
     AND column_name IN ('object_variant_slug','object_count');" 2

expect_val "0047 身につけているものにも左右がある (4 つの obs_ 表で軸が揃う)" \
  "SELECT count(DISTINCT table_name) FROM information_schema.columns
    WHERE table_schema='kura' AND table_name LIKE 'obs\\_%'
      AND column_name LIKE '%laterality%';" 4
expect_reject "0047 未知の左右" \
  "INSERT INTO obs_object(observation_id,object_slug,laterality)
     VALUES (-1,'piercing','middle');"
expect_val "0047 左右の意味が 0036 の定義を指している" \
  "SELECT count(*) FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
     JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='kura' AND c.relname='obs_object' AND a.attname='laterality'
      AND col_description(c.oid,a.attnum) LIKE '%その人物にとっての左右%';" 1

expect_val "0048 断定できない / 中性的 / 言っていない が書き分けられている" \
  "SELECT count(*) FROM gender_presentation
    WHERE (slug='ambiguous' AND description LIKE '%見たが断定できない%')
       OR (slug='androgynous' AND description LIKE '%積極的に判断%');" 2
expect_val "0048 断定不能が両系に共通の制約だと明記されている" \
  "SELECT count(*) FROM gender_presentation
    WHERE slug='ambiguous' AND description LIKE '%タグ空間でも同じ%';" 1
expect_val "0048 人数の取りこぼしが列コメントに書かれている" \
  "SELECT count(*) FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
     JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='kura' AND c.relname='obs_identity' AND a.attname='presentation'
      AND col_description(c.oid,a.attnum) LIKE '%どの人数にも入らない%';" 1

expect_val "0049 包皮の被り具合が書ける (程度を持つ)" \
  "SELECT count(*) FROM attribute_value WHERE attribute_slug='foreskin_retraction';" 3
expect_val "0049 診断 (phimosis) を値にしていない — 観察できるのは今の状態だけ" \
  "SELECT count(*) FROM attribute_value WHERE attribute_slug='foreskin_retraction'
     AND value_slug IN ('phimosis','uncut','circumcised');" 0
expect_val "0049 亀頭側に二重の属性を作っていない (被覆から導ける)" \
  "SELECT count(*) FROM attribute WHERE applies_key='glans';" 0
expect_val "0049 包皮の属性は foreskin に付く" \
  "SELECT count(*) FROM attribute WHERE slug='foreskin_retraction' AND applies_key='foreskin';" 1

expect_val "0050/0051 排出の四つが揃っている" \
  "SELECT count(*) FROM attribute
    WHERE slug IN ('ejaculation_state','squirting_state','urination_state','defecation_state');" 4
expect_val "0050 三つの排出で値空間が揃っている (注釈者が考え直さずに済む)" \
  "SELECT count(DISTINCT n) FROM (
     SELECT attribute_slug, count(*) n FROM attribute_value
      WHERE attribute_slug IN ('squirting_state','urination_state','defecation_state')
      GROUP BY 1) x;" 1
expect_val "0050 「見たが出ていない」と「言っていない」を分ける値がある" \
  "SELECT count(*) FROM attribute_value WHERE attribute_slug='urination_state'
     AND value_slug='not_urinating';" 1
expect_val "0050 部位を挙げることが行為を意味しないと明記されている" \
  "SELECT count(*) FROM body_region WHERE slug='urethral_opening'
     AND description LIKE '%排尿を意味しない%';" 1

expect_val "0051 肛門でも「部位 ≠ 行為」が明記されている" \
  "SELECT count(*) FROM body_region WHERE slug='anus' AND description LIKE '%排便を意味しない%';" 1
expect_val "0051 開き具合 (dilation) と出ている最中を分けている" \
  "SELECT count(*) FROM attribute WHERE slug='dilation' AND description LIKE '%defecation_state%';" 1

expect_val "0053 排出 4 つとも人物全体に付く (部位の露出を要求しない)" \
  "SELECT count(*) FROM attribute
    WHERE slug IN ('ejaculation_state','squirting_state','urination_state','defecation_state')
      AND applies_kind='entity';" 4
expect_val "0053 部位側に「挙げる必要はない」と書かれている" \
  "SELECT count(*) FROM body_region WHERE slug IN ('urethral_opening','anus')
     AND description LIKE '%挙げる必要はない%';" 2
expect_val "0053 軌跡は関係が持つ (属性に部位を戻していない)" \
  "SELECT count(*) FROM relation WHERE slug IN
     ('urinating_onto','ejaculating_onto','defecating_onto');" 3

expect_val "0054 打撃の関係がある (部位ごとの語を作っていない)" \
  "SELECT (SELECT count(*) FROM relation WHERE slug='striking')
        + (SELECT count(*) FROM relation WHERE slug IN ('punching','kicking','slapping','headbutt'));" 1
expect_val "0054 持続する接触は打撃と分けている" \
  "SELECT count(*) FROM relation WHERE slug IN ('biting','strangling');" 2
expect_val "0054 環境が排出専用でないと表に明記されている" \
  "SELECT count(*) FROM pg_description d JOIN pg_class c ON c.oid=d.objoid
    WHERE c.relname='scene_surface' AND d.objsubid=0 AND d.description LIKE '%排出専用ではない%';" 1
expect_val "0055 環境が解決形にも運ばれる" \
  "SELECT count(*) FROM information_schema.columns WHERE table_schema='kura'
     AND table_name='resolved_facet' AND column_name='object_surface_slug';" 1
expect_val "0052 対象は人体か環境のどちらか (両方は書けない)" \
  "SELECT count(*) FROM pg_constraint WHERE conname='ck_obs_relation_object_one_kind' AND convalidated;" 1

expect_val "0056 姿勢の語に顔の向きを混ぜていない (顔を上げた土下座が書ける)" \
  "SELECT count(*) FROM pose WHERE slug IN ('dogeza','prostration')
     AND description LIKE '%顔の向きは含まない%';" 2
expect_val "0056 頭の向きは head scope が持つ (5 語)" \
  "SELECT count(*) FROM pose WHERE scope='head';" 5

expect_val "0058 土下座と四つん這いを接地部位の数で分けている (数えられる基準)" \
  "SELECT count(*) FROM pose WHERE slug IN ('dogeza','all_fours')
     AND description LIKE '%接地している部位の数%';" 2
expect_val "0058 土下座の条件が具体的に列挙されている" \
  "SELECT count(*) FROM pose WHERE slug='dogeza'
     AND description LIKE '%手・肘・膝・足の甲のすべてが接地%';" 1

expect_val "0059 ウマ娘が独立した種族としてある" \
  "SELECT count(*) FROM species WHERE slug='umamusume';" 1
expect_val "0059 horse_girl と混同しないよう非推奨表示に書かれている" \
  "SELECT count(*) FROM species WHERE slug='horse_girl'
     AND display_name LIKE '%umamusume%';" 1
expect_val "0060 獣化の段階を言う軸がある" \
  "SELECT count(*) FROM attribute_value WHERE attribute_slug='animalization_degree';" 4
expect_val "0060 全身を値に入れていない (全身なら species 側)" \
  "SELECT count(*) FROM attribute_value WHERE attribute_slug='animalization_degree'
     AND value_slug IN ('full','whole_body','complete');" 0
expect_val "0060 段階は人物全体の属性 (部位ではない)" \
  "SELECT count(*) FROM attribute WHERE slug='animalization_degree' AND applies_kind='entity';" 1
expect_val "0060 種族が段階を決めるなら書かないと明記されている" \
  "SELECT count(*) FROM attribute WHERE slug='animalization_degree'
     AND description LIKE '%種族が段階を決めるなら書かない%';" 1

expect_val "0061 脚側に「腰を上げている」がある" \
  "SELECT count(*) FROM pose WHERE slug='hips_raised' AND scope='legs';" 1
expect_val "0061 相対的な高さ (hips_above_head) と脚の構えを分けている" \
  "SELECT count(*) FROM pose WHERE slug='hips_above_head' AND description LIKE '%hips_raised%';" 1

expect_val "0062 潮の関係も揃っている" \
  "SELECT count(*) FROM relation WHERE slug='squirting_onto';" 1
expect_val "0062 排出の出どころを器官に縛っていない" \
  "SELECT count(*) FROM relation_applicability
     WHERE relation_slug IN ('urinating_onto','ejaculating_onto','defecating_onto','squirting_onto')
       AND role='subject' AND participant_key <> 'any';" 0
expect_val "0063 石鹸の泡を付着物として書ける" \
  "SELECT count(*) FROM attribute_value
     WHERE attribute_slug='surface_substance' AND value_slug='soap_foam';" 1
expect_val "0063 泡を物として持たせていない" \
  "SELECT count(*) FROM object_type WHERE slug ~ 'foam|bubble';" 0

expect_val "0064 一文で入れる型がある" \
  "SELECT (count(*) >= 10)::int FROM phrase_macro;" 1
# 展開先は **FK で縛ってある** ので「存在しない語を指す型」は DB が受け付けない。
# 検査するのは**縛りが外れていないこと**の方 (jsonb 1 列に戻すと全部消える)。
expect_val "0064 型の関係・部位・ポーズが FK で縛られている" \
  "SELECT count(*) FROM pg_constraint
    WHERE contype='f'
      AND conrelid IN ('phrase_macro_relation'::regclass,'phrase_macro_pose'::regclass);" 7
expect_val "0064 型が作る関係がある" \
  "SELECT (count(*) >= 12)::int FROM phrase_macro_relation;" 1

# **件数で縛らない。** 2 固定にしていた版は 0069 で 3 つ目が増えた途端に落ちた —
# 検査したいのは「この 2 つがそこに居る」ことで、全体の数ではない。
expect_val "0065 どの部位にも付く属性をスキーマで言える" \
  "SELECT count(*) FROM attribute
     WHERE applies_kind='any_region' AND slug IN ('surface_substance','pigmentation');" 2
expect_val "0065 付着物と色素沈着は entity ではない (実態は部位ごと)" \
  "SELECT count(*) FROM attribute
     WHERE slug IN ('surface_substance','pigmentation') AND applies_kind='entity';" 0
expect_val "0065 applies_kind の許容値に any_region が入っている" \
  "SELECT count(*) FROM pg_constraint
    WHERE conrelid='attribute'::regclass AND contype='c'
      AND pg_get_constraintdef(oid) LIKE '%any_region%';" 1

expect_val "0066 体内の管腔を指す部位がある" \
  "SELECT count(*) FROM body_region WHERE primary_class='internal_cavity';" 4
expect_val "0066 内側は体表クラスに入れていない (体表前提の属性が付かないように)" \
  "SELECT count(*) FROM body_region
     WHERE slug IN ('rectum','vaginal_canal','uterus','cervix')
       AND primary_class IN ('surface_mucosa','surface_skin');" 0
expect_val "0066 入口の側に奥の部位が書いてある" \
  "SELECT count(*) FROM body_region
     WHERE (slug='anus' AND description LIKE '%rectum%')
        OR (slug='vaginal_opening' AND description LIKE '%vaginal_canal%');" 2

expect_val "0067 内部が見える描き方を言える" \
  "SELECT count(*) FROM scene_descriptor WHERE slug IN ('cross_section','x_ray');" 2
expect_val "0067 描き方を部位に焼き付けていない" \
  "SELECT count(*) FROM body_region WHERE slug ~ 'xray|x_ray|cross_section|section';" 0
expect_val "0067 レビュー工程の語 (evidence_type) に混ぜていない" \
  "SELECT count(*) FROM evidence_type WHERE slug IN ('cross_section','x_ray');" 0
expect_val "0067 内側の部位に「描き方は別軸」と書いてある" \
  "SELECT count(*) FROM body_region
     WHERE primary_class='internal_cavity' AND description LIKE '%cross_section%';" 4

expect_val "0068 腕を体の前に持ってくる構えがある" \
  "SELECT count(*) FROM pose
     WHERE slug IN ('crossed_arms','arms_drawn_in','arms_at_sides') AND scope='arms';" 3
expect_val "0068 接触するものを pose に焼き付けていない (関係の仕事)" \
  "SELECT count(*) FROM pose WHERE slug ~ 'hand_on_own_(face|chest)|hand_to_own';" 0
expect_val "0068 引き寄せは触れている場合と書き分けると明記されている" \
  "SELECT count(*) FROM pose WHERE slug='arms_drawn_in'
     AND description LIKE '%relation touching%';" 1

expect_val "0069 皮膚の上の図像を書ける" \
  "SELECT count(*) FROM attribute_value WHERE attribute_slug='surface_marking';" 6
expect_val "0069 部位ごとに指定する (部位×種類の合成語を作っていない)" \
  "SELECT count(*) FROM attribute WHERE slug='surface_marking' AND applies_kind='any_region';" 1
expect_val "0069 部位を焼き付けた語を作っていない" \
  "SELECT count(*) FROM attribute_value
     WHERE attribute_slug='surface_marking' AND value_slug ~ 'arm|leg|chest|pubic|shoulder';" 0
expect_val "0069 永続かどうかの別軸を作っていない" \
  "SELECT count(*) FROM attribute WHERE slug ~ 'permanen|durab';" 0
expect_val "0069 付着物との境界が両側に書いてある" \
  "SELECT count(*) FROM attribute
     WHERE (slug='surface_marking'   AND description LIKE '%surface_substance%')
        OR (slug='surface_substance' AND description LIKE '%surface_marking%');" 2

expect_val "0070 自由入力は 1 か所だけ (メモ欄が増えていない)" \
  "SELECT count(*) FROM information_schema.columns
    WHERE table_schema='kura' AND table_name LIKE 'obs\\_%'
      AND column_name IN ('text_content','note','notes','memo','free_text','comment');" 1
expect_val "0070 自由入力は surface_marking に縛られている" \
  "SELECT count(*) FROM pg_constraint
    WHERE conrelid='obs_attribute'::regclass AND contype='c'
      AND pg_get_constraintdef(oid) LIKE '%text_content%'
      AND pg_get_constraintdef(oid) LIKE '%surface_marking%';" 1
expect_val "0070 長さの上限がある (文章を書く場所にしない)" \
  "SELECT count(*) FROM pg_constraint
    WHERE conrelid='obs_attribute'::regclass AND contype='c'
      AND pg_get_constraintdef(oid) LIKE '%text_content%'
      AND pg_get_constraintdef(oid) LIKE '%length%';" 1
expect_val "0070 解決形にも載っている (宣言だけで終わらせない)" \
  "SELECT count(*) FROM information_schema.columns
    WHERE table_schema='kura' AND table_name='resolved_facet' AND column_name='text_content';" 1

expect_val "0071 拘束を言う関係がある" \
  "SELECT count(*) FROM relation WHERE slug='restraining';" 1
# **0076 で判断が変わった** (user 指摘「そういう関係を内包したセットなんだから」)。
# 名前が部品を決めている形は 1 語。守るのは「**内包しているものを別に書かせない**」方
expect_val "0071/0076 羽交い締めは 1 語で書く" \
  "SELECT count(*) FROM relation WHERE slug='full_nelson';" 1
expect_val "0071 握り方・抱え方との境界が両側に書いてある" \
  "SELECT count(*) FROM relation
     WHERE slug IN ('grasping','holding') AND description LIKE '%restraining%';" 2
expect_val "0076 型も 1 語だけを書く (内包しているものを重ねない)" \
  "SELECT count(*) FROM phrase_macro_relation WHERE macro_slug='full_nelson';" 1

expect_val "0072 型に従来タグとの対応がある" \
  "SELECT (count(*) >= 10)::int FROM phrase_macro WHERE tag_name IS NOT NULL;" 1
expect_val "0072 型は検索の単位でもあると表に書いてある" \
  "SELECT count(*) FROM pg_description d
     JOIN pg_class c ON c.oid=d.objoid AND d.objsubid=0
    WHERE c.relname='phrase_macro' AND d.description LIKE '%検索の単位%';" 1
expect_val "0076 内包しているものを別に書かせない (behind を重ねない)" \
  "SELECT count(*) FROM phrase_macro_relation
     WHERE macro_slug='full_nelson' AND relation_slug <> 'full_nelson';" 0
expect_val "0076 形が決まっていない拘束は残してある" \
  "SELECT count(*) FROM relation WHERE slug='restraining';" 1
expect_val "0076 判定基準 (名前が部品を決めているか) が書いてある" \
  "SELECT count(*) FROM relation WHERE slug='full_nelson'
     AND description LIKE '%名前が部品を決めているか%';" 1

expect_val "0077 2 点を繋ぐ糸を書ける" \
  "SELECT count(*) FROM relation WHERE slug='fluid_string' AND directionality='symmetric';" 1
expect_val "0077 物質ごとに語を作っていない" \
  "SELECT count(*) FROM relation WHERE slug ~ 'saliva|cum_string|semen_string';" 0
expect_val "0077 糸の物質を言う軸がある" \
  "SELECT count(*) FROM attribute_value WHERE attribute_slug='string_substance';" 4
expect_val "0077 付着物との境界が両側に書いてある" \
  "SELECT count(*) FROM attribute a
    WHERE a.slug='surface_substance' AND a.description LIKE '%fluid_string%';" 1

expect_val "0078 血を付着物として書ける" \
  "SELECT count(*) FROM attribute_value
     WHERE attribute_slug='surface_substance' AND value_slug='blood';" 1
expect_val "0078 部位を焼き付けた血の語を作っていない" \
  "SELECT count(*) FROM attribute_value
     WHERE value_slug ~ 'blood_on|nosebleed|blood_from';" 0

# 0079 相当: **既存の下書きが全部いまの語彙で確定できるか。**
# 語彙を変えると、既に書いた下書きが取り残される (2026-07-31: horse_girl 非推奨で 6 件、
# 空文字の取りこぼしで 6 件が確定できなくなった)。投入まで走らせて rollback する
if python3 "$(dirname "$0")/../drafts_are_committable.py" >/tmp/dac.out 2>&1; then
  ok "下書きが全部いまの語彙で確定できる ($(tail -1 /tmp/dac.out | tr -d '\n'))"
else
  ng "確定できない下書きがある — $(grep -m1 'NG' /tmp/dac.out)"
fi

# 重複判定の不変条件: 同じ内容は二度書かない / **撤回した事実は入れ直せる**
if python3 "$(dirname "$0")/../dedup_invariants.py" >/tmp/dedup.out 2>&1; then
  ok "重複判定 ($(tail -1 /tmp/dedup.out | tr -d '\n'))"
else
  ng "重複判定が壊れている — $(grep -m1 'NG' /tmp/dedup.out)"
fi

expect_val "0079 参加者と傍観者を分けられる" \
  "SELECT count(*) FROM information_schema.columns
    WHERE table_schema='kura' AND table_name='obs_entity_count' AND column_name='involvement';" 1
expect_val "0079 値は participant / bystander だけ" \
  "SELECT count(*) FROM pg_constraint
    WHERE conrelid='obs_entity_count'::regclass AND contype='c'
      AND pg_get_constraintdef(oid) LIKE '%bystander%';" 1
expect_val "0079 群衆の語を作っていない (count_min から導く)" \
  "SELECT count(*) FROM scene_descriptor WHERE slug ~ 'crowd|audience|onlooker';" 0
expect_val "0079 解決形にも載っている" \
  "SELECT count(*) FROM information_schema.columns
    WHERE table_schema='kura' AND table_name='resolved_facet' AND column_name='involvement';" 1

expect_val "0080 頭髪が部位にある" \
  "SELECT count(*) FROM body_region WHERE slug='head_hair' AND primary_class='hair';" 1
expect_val "0080 髪型を部位にしていない" \
  "SELECT count(*) FROM body_region WHERE slug ~ 'ponytail|twintail|bun|braid';" 0
expect_val "0080 頭皮との違いが両側に書いてある" \
  "SELECT count(*) FROM body_region
     WHERE (slug='head_hair' AND description LIKE '%scalp%')
        OR (slug='scalp' AND description LIKE '%head_hair%');" 2

expect_val "0081 拘束具を身につけているもので書ける" \
  "SELECT count(*) FROM object_type WHERE slug IN ('rope','handcuffs','gag','armbinder','blindfold','chain');" 6
expect_val "0081 部位ごとの拘束語を作っていない" \
  "SELECT count(*) FROM object_type WHERE slug ~ 'wrist_cuff|ankle_cuff|mouth_gag';" 0
expect_val "0081 拘束状態の別軸を作っていない (物から導く)" \
  "SELECT count(*) FROM attribute WHERE slug ~ 'movement_restrict|bound_state|restrained';" 0

expect_val "0082 踏みつけの語を新設していない (既存 2 語で書く)" \
  "SELECT count(*) FROM relation WHERE slug ~ 'trampl|stepping|stomp';" 0
expect_val "0082 踏みつけの用途が pressing_against に書いてある" \
  "SELECT count(*) FROM relation WHERE slug='pressing_against' AND description LIKE '%踏みつけ%';" 1

# ADR-0004: すでに有名で確定的なタグは分解しない
if grep -q "名前が部品を決めているか" "$(dirname "$0")/../../docs/adr/ADR-0004-named-determinate-terms.md" 2>/dev/null; then
  ok "ADR-0004 が判定基準を書いている"
else
  ng "ADR-0004 が無い / 判定基準が書かれていない"
fi

# **0074 で判断が変わった。** 0073 は抱える形を関係 2 本で書いていたが、
# user 判断「両手が揃った確定的なポーズならそれを使えばいい」により pose に移した。
# 検査も追随させる — 古い不変条件を残すと、新しい判断のほうが「壊れている」ことになる。
expect_val "0073/0074 両腕で抱える形が型から入る" \
  "SELECT count(*) FROM phrase_macro_pose
     WHERE macro_slug='hugging_own_legs' AND pose_slug='hugging_own_legs';" 1
expect_val "0073 掴んで開く型と抱え込む型が別々にある" \
  "SELECT count(*) FROM phrase_macro WHERE slug IN ('holding_own_legs','hugging_own_legs');" 2
expect_val "0073 抱える型に脚の開きを混ぜていない" \
  "SELECT count(*) FROM phrase_macro_pose
     WHERE macro_slug='hugging_own_legs' AND pose_slug <> 'hugging_own_legs';" 0
# 0074 で hugging_own_legs は**意図して** pose になった (両手が揃った確定的な形)。
# 守りたいのは「**片手や部位ごとの合成語**を増やさない」ことの方
expect_val "0074 片手や部位ごとの合成語を増やしていない" \
  "SELECT count(*) FROM pose
     WHERE slug ~ '^hand_on_own_' AND slug <> 'hand_on_own_hip';" 0

expect_val "0074 両手が揃った自己接触が pose にある" \
  "SELECT (count(*) >= 10)::int FROM pose WHERE category='self_contact'
     AND slug LIKE 'hands\\_on\\_%';" 1
expect_val "0074 片手の語を pose に増やしていない (relation の仕事)" \
  "SELECT count(*) FROM pose WHERE slug ~ '^hand_on_' AND slug <> 'hand_on_own_hip';" 0
expect_val "0074 両手の形だけという規則が表に書いてある" \
  "SELECT count(*) FROM pose_category
     WHERE slug='self_contact' AND description LIKE '%両手が揃って%';" 1
expect_val "0074 同じ事実を pose と関係の 2 通りで書かせていない" \
  "SELECT count(*) FROM phrase_macro_relation WHERE macro_slug='hugging_own_legs';" 0
expect_val "0074 抱える形は pose として型に入っている" \
  "SELECT count(*) FROM phrase_macro_pose
     WHERE macro_slug='hugging_own_legs' AND pose_slug='hugging_own_legs';" 1

expect_val "0075 「確定的な形は 1 語」が表に書いてある" \
  "SELECT count(*) FROM pg_description d JOIN pg_class c ON c.oid=d.objoid AND d.objsubid=0
    WHERE c.relname='pose' AND d.description LIKE '%確定的な名前のある形は 1 語%';" 1
expect_val "0075 正座の条件は説明にあり、部位ごとに書かせない" \
  "SELECT count(*) FROM pose WHERE slug='seiza'
     AND description LIKE '%1 つずつ書かない%';" 1
expect_val "0075 座り方の語が揃っている (横座り・あぐら)" \
  "SELECT count(*) FROM pose WHERE slug IN ('yokozuwari','indian_style');" 2

echo "== (a') 説明が埋まっているか (語を選べないのは ontology の欠陥) =="
expect_val "0007 body_region の未記述が 0" \
  "SELECT count(*) FROM body_region WHERE description IS NULL;" 0
expect_val "0018 scene_descriptor の未記述が 0" \
  "SELECT count(*) FROM scene_descriptor WHERE description IS NULL;" 0
expect_val "0018 scene_facet に display_name が入っている" \
  "SELECT count(*) FROM scene_facet WHERE display_name IS NULL;" 0
expect_val "0011 順序尺度の属性に display_order が入っている (アルファベット順で壊れない)" \
  "SELECT count(*) FROM attribute_value WHERE attribute_slug='breast_size' AND display_order=0;" 0

echo "== (b) 不変条件が機構として効くか =="
expect_val "0010 obs_attribute に region_slug / laterality" \
  "SELECT count(*) FROM information_schema.columns WHERE table_schema='kura'
     AND table_name='obs_attribute' AND column_name IN ('region_slug','laterality');" 2
expect_reject "0010 laterality に妥当でない値" \
  "INSERT INTO obs_attribute(observation_id,attribute_slug,laterality) VALUES (-1,'breast_size','middle');"
expect_val "0016 is_multivalued が宣言されている (UI に slug を直書きしない)" \
  "SELECT count(*) FROM attribute WHERE is_multivalued;" 1
expect_val "0019/0020 機械で決まる語が derived_from を持つ" \
  "SELECT (SELECT count(*) FROM scene_facet WHERE derived_from IS NOT NULL)
        + (SELECT count(*) FROM scene_descriptor WHERE derived_from IS NOT NULL);" 7
expect_val "0020 lineart/sketch は人手のまま (判断が要る軸を機械側にしていない)" \
  "SELECT count(*) FROM scene_descriptor WHERE slug IN ('lineart','sketch') AND derived_from IS NOT NULL;" 0
expect_val "0019 blob に width/height" \
  "SELECT count(*) FROM information_schema.columns WHERE table_schema='kura'
     AND table_name='blob' AND column_name IN ('width','height');" 2
expect_reject "0019 blob の寸法が片方だけ" \
  "INSERT INTO blob(sha256,backend_slug,external_ref,width) VALUES ('\\x01','kura_data_images','/tmp/x',100);"

expect_val "0024 解決形が関係の相手を持てる (誰が誰に が落ちない)" \
  "SELECT count(*) FROM information_schema.columns WHERE table_schema='kura'
     AND table_name='resolved_facet' AND column_name IN ('object_entity_id','object_region_slug');" 2
expect_reject "0024 関係以外に相手を書く" \
  "INSERT INTO resolved_facet(resolved_scene_id,domain,status,attribute_slug,object_region_slug)
     VALUES (-1,'attribute','resolved','breast_size','breast');"
expect_val "0024 適用先が未宣言の関係は 0" \
  "SELECT count(*) FROM relation r WHERE NOT EXISTS
     (SELECT 1 FROM relation_applicability a WHERE a.relation_slug=r.slug);" 0
expect_val "0024 inserting_into の主語は部位 (人物ではない)" \
  "SELECT count(*) FROM relation_applicability WHERE relation_slug='inserting_into'
     AND role='subject' AND participant_kind='region';" 1

echo "== (b') 0021 位置 — box でも点でもよいが空は作らせない =="
expect_val "0028 点は body_region を指す (専用語彙の複製を持たない)" \
  "SELECT (SELECT count(*) FROM information_schema.tables WHERE table_schema='kura'
             AND table_name='asset_region_anchor')
        + (SELECT count(*) FROM information_schema.columns WHERE table_schema='kura'
             AND table_name='asset_region' AND column_name='anchor_kind');" 0
expect_val "0028 anchor_region_slug が body_region への FK" \
  "SELECT count(*) FROM information_schema.columns WHERE table_schema='kura'
     AND table_name='asset_region' AND column_name='anchor_region_slug';" 1
expect_reject "0028 未知の部位を指す点" \
  "INSERT INTO asset_region(asset_id,anchor_x,anchor_y,anchor_region_slug)
     VALUES (-1,0.5,0.5,'nose_tip');"
expect_reject "何も指していない asset_region" \
  "INSERT INTO asset_region(asset_id) VALUES (-1);"
expect_reject "点の三つ組が欠けている (部位無し)" \
  "INSERT INTO asset_region(asset_id,anchor_x,anchor_y) VALUES (-1,0.5,0.5);"
expect_reject "box の四隅が欠けている" \
  "INSERT INTO asset_region(asset_id,x_min,y_min) VALUES (-1,0.1,0.1);"

expect_val "0025 関係の参加者に左右がある (右手と左手で違うものを掴める)" \
  "SELECT count(*) FROM information_schema.columns WHERE table_schema='kura'
     AND table_name='obs_relation' AND column_name IN ('subject_laterality','object_laterality');" 2
expect_val "0025 解決形にも左右が運ばれる (0024 の教訓)" \
  "SELECT count(*) FROM information_schema.columns WHERE table_schema='kura'
     AND table_name='resolved_facet' AND column_name IN ('region_laterality','object_region_laterality');" 2
expect_reject "0025 妥当でない左右" \
  "INSERT INTO obs_relation(observation_id,relation_slug,subject_laterality)
     VALUES (-1,'grasping','middle');"
expect_val "0026 筋肉量と脂肪量は独立した 2 軸 (1 軸に潰していない)" \
  "SELECT count(*) FROM attribute WHERE slug IN ('muscularity','adiposity')
     AND applies_kind='entity';" 2
expect_val "0026 body_type のような 1 軸の属性を作っていない" \
  "SELECT count(*) FROM attribute WHERE slug IN ('body_type','build','physique');" 0

expect_val "0027 接地の仕方は姿勢と独立した属性 (pose にしていない)" \
  "SELECT (SELECT count(*) FROM attribute WHERE slug='foot_contact' AND applies_key='foot')
        + (SELECT count(*) FROM pose WHERE slug IN ('tiptoes','sonkyo','on_tiptoes'));" 1
expect_val "0027 squatting の定義から接地が外れている (蹲踞が書ける)" \
  "SELECT count(*) FROM pose WHERE slug='squatting' AND description LIKE '%接地の仕方は含まない%';" 1

expect_val "0029 属性に表示順がある (hair_color が hair_density より後ろ)" \
  "SELECT count(*) FROM attribute a, attribute b
    WHERE a.slug='hair_density' AND b.slug='hair_color' AND a.display_order < b.display_order;" 1
expect_val "0029 乳輪の広さと乳首の形は別の軸" \
  "SELECT count(*) FROM attribute WHERE slug IN ('areola_size','nipple_state')
     AND display_order > 0;" 2
expect_val "0029 areola_size を breast_size と同じ頭基準にしていない" \
  "SELECT count(*) FROM attribute_value WHERE attribute_slug='areola_size'
     AND description LIKE '%頭%';" 0

expect_val "0030 人でも物でもない動作主 (creature)" \
  "SELECT count(*) FROM entity_kind WHERE slug='creature';" 1
expect_val "0030 非人間の部位が 7 語" \
  "SELECT count(*) FROM body_region WHERE primary_class='nonhuman_appendage';" 7
expect_val "0030 grasping の主語から人体前提 (hand) が外れている" \
  "SELECT count(*) FROM relation_applicability WHERE relation_slug='grasping'
     AND role='subject' AND participant_key='hand';" 0
expect_val "0030 触手に本数の語彙を作っていない (決まらない同一性を主張しない)" \
  "SELECT count(*) FROM attribute WHERE slug LIKE '%tentacle%' OR slug LIKE '%instance%';" 0
expect_val "0031 主体の kind を観測に書ける" \
  "SELECT count(*) FROM information_schema.columns WHERE table_schema='kura'
     AND table_name='obs_identity' AND column_name='entity_kind';" 1
expect_reject "0031 未知の kind" \
  "INSERT INTO obs_identity(observation_id,entity_kind) VALUES (-1,'alien');"

expect_val "0032 体の向きは人物ごとの属性 (scene_descriptor ではない)" \
  "SELECT (SELECT count(*) FROM attribute WHERE slug='body_orientation' AND applies_kind='entity')
        + (SELECT count(*) FROM scene_descriptor WHERE slug IN ('facing_away','from_behind'));" 1
expect_val "0032 体 / 頭 / 視線が 3 つの独立な軸になっている" \
  "SELECT (SELECT count(*) FROM attribute WHERE slug IN ('body_orientation','gaze_direction'))
        + (SELECT count(*) FROM pose WHERE slug IN ('head_turned','looking_back'));" 4

expect_val "0033 広げるは relation (pose に合成語を増やしていない)" \
  "SELECT (SELECT count(*) FROM relation WHERE slug='spreading')
        + (SELECT count(*) FROM pose WHERE slug LIKE 'spreading_own%');" 1
# **0033 の線は 0074 で狭められた** (user 判断 2026-07-31)。
# 0033 は片手の場合を根拠に自己接触の pose を全部非推奨にしたが、
# **両手が揃っている形では左右が問いにならない**ので当てはまらなかった。
# 守るのは「片手の形を pose にしない」ことだけ。
expect_val "0033/0074 自己接触の pose は両手の形だけ (片手は relation)" \
  "SELECT count(*) FROM pose
    WHERE category='self_contact' AND slug ~ '^hand_' AND slug !~ '^hands_';" 0
expect_val "0033 spreading は主体も対象も部位" \
  "SELECT count(*) FROM relation_applicability WHERE relation_slug='spreading'
     AND participant_kind='region';" 2

expect_val "0034 開口部の開き具合は class=orifice の 1 属性 (部位ごとに作っていない)" \
  "SELECT (SELECT count(*) FROM attribute WHERE slug='dilation' AND applies_key='orifice')
        + (SELECT count(*) FROM attribute WHERE slug IN ('anus_dilation','vaginal_dilation'));" 1
expect_val "0034 orifice は開く問いが立つ 4 部位だけ (vulva/inner_lip に付けていない)" \
  "SELECT count(*) FROM region_class_map WHERE class_slug='orifice';" 4
expect_val "0034 状態 (dilation) と行為 (spreading) を分けている" \
  "SELECT (SELECT count(*) FROM attribute_value WHERE attribute_slug='dilation'
             AND value_slug='gaping')
        + (SELECT count(*) FROM relation WHERE slug='spreading');" 2

echo "== (c) 二重の真実を作っていないか =="
expect_val "orientation を保存する列が無い (width/height から導く)" \
  "SELECT count(*) FROM information_schema.columns WHERE table_schema='kura'
     AND column_name IN ('orientation','aspect_ratio','color_mode');" 0
expect_val "landscape/portrait/square は語彙としては残っている (派生ビュー用)" \
  "SELECT count(*) FROM scene_descriptor WHERE slug IN ('landscape','portrait','square');" 3
expect_val "ADR-0002 json/jsonb 列が 0" \
  "SELECT count(*) FROM information_schema.columns WHERE table_schema='kura'
     AND data_type IN ('json','jsonb');" 0
expect_val "妥当でない FK / CHECK が 0 (NOT VALID のまま放置していない)" \
  "SELECT count(*) FROM pg_constraint WHERE NOT convalidated
     AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname='kura');" 0

echo
# **画面が出す語を validator が弾かないこと。** 個別に直しても同じ形はまた作れるので
# 規則そのものを検査する (2026-07-31 の実害: 0059 で非推奨にした horse_girl が
# 候補に残り、確定が毎回落ちていた)
if python3 "$(dirname "$0")/../vocab_offered_is_valid.py" >/tmp/vov.out 2>&1; then
  ok "画面が出す語を validator が弾かない ($(tail -1 /tmp/vov.out | tr -d '\n'))"
else
  ng "画面が出す語を validator が弾く — $(grep -m1 "NG" /tmp/vov.out)"
fi

echo "PASS $pass / FAIL $fail"
[ "$fail" -eq 0 ]
