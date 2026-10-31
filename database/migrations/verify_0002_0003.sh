#!/usr/bin/env bash
# plan kura-1.0-implementation rev11 S-5/S-6 検証 9 項目。
# 期待エラー (制約が違反を弾くこと) は個別 psql で「失敗すれば PASS」と判定する。
set -uo pipefail
PSQL=(psql -h 127.0.0.1 -p 5540 -U desk -d kura1_verify -qtA)
pass=0; fail=0

# ── 誤爆ガード (2026-07-27、実際に 2 回やった) ──────────────────────────────
# 本スクリプトは fixture (scene 900 / entity 911,912 / observation 921.. 等) を
# INSERT する。observation は append-only で DELETE できないので、**probe 本体の DB に
# 走らせると DB を作り直すしかなくなる**。実際に kura1 (port 5541) へ 2 回走らせて
# 2 回作り直した。注意では再発するので機構で止める:
#   * probe DB は import 済なので asset_source に行がある → それを検出して中止
#   * 併せて dbname が *_verify で終わらなければ中止 (命名でも二重に守る)
guard_db=$("${PSQL[@]}" -c "SELECT current_database()" 2>/dev/null || echo "")
case "$guard_db" in
  *_verify) : ;;
  "") echo "FATAL: 検証 DB に接続できません (kura1_verify を作ってから実行)"; exit 2 ;;
  *) echo "FATAL: '$guard_db' は throwaway ではありません。本スクリプトは fixture を"
     echo "       INSERT し observation は append-only で消せないため、*_verify で"
     echo "       終わる DB でのみ実行します。"; exit 2 ;;
esac
probe_rows=$("${PSQL[@]}" -c "SELECT count(*) FROM kura.asset_source" 2>/dev/null || echo 0)
if [ "${probe_rows:-0}" -gt 0 ]; then
  echo "FATAL: '$guard_db' に probe データ ($probe_rows 件の asset_source) があります。"
  echo "       fixture で汚すと作り直しになるため中止します。"
  exit 2
fi

ok()   { echo "  PASS  $1"; pass=$((pass+1)); }
ng()   { echo "  FAIL  $1"; fail=$((fail+1)); }

# 値を取って期待値と比較
expect_val() { # $1=label $2=sql $3=expected
  local got; got=$("${PSQL[@]}" -c "SET search_path=kura,public; $2" 2>&1 | tr -d '[:space:]')
  [ "$got" = "$3" ] && ok "$1 (= $got)" || ng "$1 (期待 $3 / 実際 $got)"
}
# INSERT が失敗することを期待
expect_reject() { # $1=label $2=sql
  local out; out=$("${PSQL[@]}" -c "SET search_path=kura,public; $2" 2>&1)
  if echo "$out" | grep -qiE "ERROR|エラー"; then
    ok "$1 — 制約が弾いた"
  else
    ng "$1 — 弾かれずに通った"
  fi
}
# INSERT が成功することを期待
expect_accept() { # $1=label $2=sql
  local out; out=$("${PSQL[@]}" -c "SET search_path=kura,public; $2" 2>&1)
  if echo "$out" | grep -qiE "ERROR|エラー"; then
    ng "$1 — 通るべきなのに弾かれた: $(echo "$out" | head -2 | tr '\n' ' ')"
  else
    ok "$1"
  fi
}

echo "== 固定 (1-3, 5-7) =="
expect_val "1. 妥当でない FK が 0" \
  "SELECT count(*) FROM pg_constraint WHERE contype='f' AND NOT convalidated AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname='kura');" 0
expect_val "2. json/jsonb 列が 0 (ADR-0002)" \
  "SELECT count(*) FROM information_schema.columns WHERE table_schema='kura' AND data_type IN ('json','jsonb');" 0
expect_val "3. append-only trigger が 9 表" \
  "SELECT count(DISTINCT c.relname) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='kura' AND NOT t.tgisinternal AND t.tgname LIKE 'ao_%';" 9
expect_val "5. resolved_facet に facet_key 相当の列が無い" \
  "SELECT count(*) FROM information_schema.columns WHERE table_schema='kura' AND table_name='resolved_facet' AND column_name ILIKE '%facet_key%';" 0
expect_val "6. attribute.unit が unit 表への検証済 FK" \
  "SELECT count(*) FROM pg_constraint WHERE conname='fk_attribute_unit' AND contype='f' AND convalidated;" 1
expect_val "7. resolved_facet に unit/range の重複列が無い" \
  "SELECT count(*) FROM information_schema.columns WHERE table_schema='kura' AND table_name='resolved_facet' AND column_name IN ('unit','unit_slug','range_min','range_max');" 0

echo "== 準備 (fixture) =="
"${PSQL[@]}" -c "SET search_path=kura,public;
INSERT INTO scene(id) OVERRIDING SYSTEM VALUE VALUES (900) ON CONFLICT DO NOTHING;
INSERT INTO resolver_run(id, resolver_version) OVERRIDING SYSTEM VALUE VALUES (901,'v-a'),(902,'v-b') ON CONFLICT DO NOTHING;
INSERT INTO entity(id, scene_id, kind, gender_presentation, resolver_run_id) OVERRIDING SYSTEM VALUE
  VALUES (911,900,'person','female',901),(912,900,'person','male',902) ON CONFLICT DO NOTHING;
INSERT INTO entity_region(id, entity_id, region_slug, resolver_run_id) OVERRIDING SYSTEM VALUE
  VALUES (941,911,'hand',901) ON CONFLICT DO NOTHING;
INSERT INTO resolved_scene(id, scene_id, resolver_run_id, status) OVERRIDING SYSTEM VALUE
  VALUES (931,900,901,'current') ON CONFLICT DO NOTHING;
INSERT INTO observation(id, scene_id, subject_scope, domain, event_type, producer_kind, producer_ref, subject_ref)
  OVERRIDING SYSTEM VALUE VALUES
  (921,900,'entity','relation','human_annotation','human','verify','person_1'),
  (922,900,'entity','relation','human_annotation','human','verify','person_1'),
  (923,900,'entity','relation','human_annotation','human','verify','person_1'),
  (924,900,'entity','relation','human_annotation','human','verify','person_1')
  ON CONFLICT DO NOTHING;" >/dev/null 2>&1 && echo "  fixture OK"

echo "== 4. cross-run binding が複合 FK で弾かれる =="
expect_reject "4a. obs_relation subject 側 (entity 912 は run 902、行は run 901)" \
  "INSERT INTO obs_relation(observation_id,relation_slug,subject_entity_id,resolver_run_id) VALUES (921,'touching',912,901);"
expect_reject "4b. obs_relation object 側" \
  "INSERT INTO obs_relation(observation_id,relation_slug,object_entity_id,resolver_run_id) VALUES (921,'touching',912,901);"
expect_reject "4c. resolved_entity_binding" \
  "INSERT INTO resolved_entity_binding(resolver_run_id,observation_id,entity_id) VALUES (901,921,912);"
expect_accept "4d. 同一 run なら通る (entity 911 は run 901)" \
  "INSERT INTO resolved_entity_binding(resolver_run_id,observation_id,entity_id) VALUES (901,921,911);"

echo "== 8. 新 CHECK が違反を弾く =="
expect_reject "8a. contested が単一値を持てない" \
  "INSERT INTO resolved_facet(resolved_scene_id,domain,status,attribute_slug,value_slug) VALUES (931,'attribute','contested','breast_size','large');"
expect_accept "8b. contested は値なしなら通る" \
  "INSERT INTO resolved_facet(resolved_scene_id,domain,status,attribute_slug) VALUES (931,'attribute','contested','breast_size');"
expect_reject "8c. domain と ontology 参照の不一致 (pose なのに pose_slug なし)" \
  "INSERT INTO resolved_facet(resolved_scene_id,domain,status,attribute_slug) VALUES (931,'pose','resolved','breast_size');"
expect_reject "8d. 区間の逆転 (interval_low > value_numeric)" \
  "INSERT INTO resolved_facet(resolved_scene_id,domain,status,attribute_slug,value_numeric,interval_low,interval_high,aggregation) VALUES (931,'attribute','resolved','wetness',0.2,0.5,0.9,'median_of_models');"
expect_accept "8e. 正しい区間は通る" \
  "INSERT INTO resolved_facet(resolved_scene_id,domain,status,attribute_slug,value_numeric,interval_low,interval_high,aggregation) VALUES (931,'attribute','resolved','wetness',0.6,0.5,0.9,'median_of_models');"
expect_reject "8f. observation の新 CHECK — scope=entity で主体指定なし" \
  "INSERT INTO observation(scene_id,subject_scope,domain,event_type,producer_kind,producer_ref) VALUES (900,'entity','pose','human_annotation','human','verify');"
expect_accept "8g. scope=entity + subject_ref だけで通る (ingest 時の形)" \
  "INSERT INTO observation(scene_id,subject_scope,domain,event_type,producer_kind,producer_ref,subject_ref) VALUES (900,'entity','pose','human_annotation','human','verify','person_2');"

echo "== 9. region 参加者 =="
expect_accept "9a. grasping の subject を region(hand) にできる" \
  "INSERT INTO obs_relation(observation_id,relation_slug,subject_entity_region_id,object_entity_id,resolver_run_id) VALUES (922,'grasping',941,911,901);"
expect_reject "9b. entity と region の同時指定は弾かれる" \
  "INSERT INTO obs_relation(observation_id,relation_slug,subject_entity_id,subject_entity_region_id,resolver_run_id) VALUES (923,'grasping',911,941,901);"
expect_accept "9c. covering の object を region にできる" \
  "INSERT INTO obs_relation(observation_id,relation_slug,subject_entity_id,object_entity_region_id,resolver_run_id) VALUES (924,'covering',911,941,901);"

echo "== S-6(h) seed 追加 =="
expect_val "h1. poses に dogeza" "SELECT count(*) FROM pose WHERE slug='dogeza' AND scope='whole_body' AND category='posture';" 1
expect_val "h2. relations の spatial に behind (directed)" "SELECT count(*) FROM relation WHERE slug='behind' AND category='spatial' AND directionality='directed';" 1
expect_val "h3. belly_shape が region=abdomen に適用" "SELECT count(*) FROM attribute WHERE slug='belly_shape' AND applies_kind='region' AND applies_key='abdomen';" 1
expect_val "h4. pregnancy_state が state" "SELECT count(*) FROM attribute WHERE slug='pregnancy_state' AND category='state';" 1
expect_val "h5. yoga は意図的に未追加" "SELECT count(*) FROM pose WHERE slug='yoga';" 0
expect_val "h6. belly_shape の値が 4 種" "SELECT count(*) FROM attribute_value WHERE attribute_slug='belly_shape';" 4

echo
echo "PASS $pass / FAIL $fail"
[ "$fail" -eq 0 ] || exit 1
