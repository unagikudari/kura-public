#!/usr/bin/env bash
# Description: canonical (primary) と replica (trainer) の実スキーマ差を機械照合する — 制約 (pg_constraint) と NOT NULL
# Kind: oneshot
# Effects: read-only (両ノードとも SELECT のみ; trainer へは read-only ssh)
# Requires: ssh trainer (read-only), psql
# Runs-on: primary
#
# なぜ: 2026-09-04、trainer-cc-primary の手動照合で 12 件の残差 (0057 の伝播漏れ
# 1 件を含む) が見つかった。この照合を人が思い出して行う限り、次の drift も
# 人が見つけるまで潜伏する — クエリを script にして「実行すれば分かる」に倒す。
# 判定境界は docs/notes/replica-ddl-propagation.md の強制クラス:
#   apply が強制する (CHECK / NOT NULL / PK・UNIQUE) の差 = 危険 (要即対応)
#   apply が発火させない (FK / trigger) の差 = 整列推奨だが非常事態ではない
# 既知の意図的差: tagger_gt_exclude (trainer ローカル作業表) は除外する。
set -uo pipefail

REPLICA_HOST="${1:-trainer}"

# **照合範囲は publication のメンバーだけ** (2026-09-04、#60 の timer 化直後に
# 実測して判明): 非複製テーブル (tagger_candidate_event / registration_request の
# review_class 等) は行が流れないので apply は原理的に落ちず、primary にだけ存在する
# のが正常。全 public を比較すると、正しい publisher-only 宣言が毎回 drift として
# 鳴り、**backstop が alert fatigue で死ぬ** — 危険な差 (apply が強制するクラス)
# と無害な差を同じ音で鳴らしてはいけない。
# 判定境界は docs/notes/replica-ddl-propagation.md の強制クラスと同じ根拠:
# 「apply が tuple 単位で強制するか」= 複製される表かどうか、が前提。
#
# メンバー一覧は **publisher (primary) で解決してリテラルにする**: replica は
# subscriber なので pg_publication_tables は空 — 同じサブクエリを両側で走らせると
# replica 側が 0 行を返し、「取得空を差分ゼロと読まない」ガードが exit 2 に落ちる
# (照合できていないのに照合したつもり、の一歩手前)。
PUB_LIST="$(PGPASSWORD=kura psql -h 127.0.0.1 -U kura -d kura -Atc \
  "SELECT string_agg(quote_literal(tablename), ',') FROM pg_publication_tables WHERE pubname='kura_mesh_pub'")" || exit 1
[[ -n "$PUB_LIST" ]] || { echo "publication メンバーが取得できない — 照合範囲が決まらない" >&2; exit 2; }
PUB_FILTER="r.relname IN ($PUB_LIST)"
Q_CON="SELECT conrelid::regclass::text || '|' || conname || '|' || contype::text || '|' || pg_get_constraintdef(c.oid)
FROM pg_constraint c JOIN pg_class r ON r.oid=c.conrelid JOIN pg_namespace n ON n.oid=r.relnamespace
WHERE n.nspname='public' AND r.relname <> 'tagger_gt_exclude' AND $PUB_FILTER ORDER BY 1"
Q_NN="SELECT a.attrelid::regclass::text || '.' || a.attname || '|notnull'
FROM pg_attribute a JOIN pg_class r ON r.oid=a.attrelid JOIN pg_namespace n ON n.oid=r.relnamespace
WHERE n.nspname='public' AND r.relkind='r' AND a.attnum>0 AND NOT a.attisdropped
  AND a.attnotnull AND r.relname <> 'tagger_gt_exclude' AND $PUB_FILTER ORDER BY 1"
# 列の存在と型。名乗った強制クラス「列の追加/削除/型」を実際に見る段 — これが
# 無いあいだ、nullable・制約なしの列を 1 本取りこぼした replica に対して、この
# audit は 26 分ごとに「(なし)」を出し続けた (2026-09-04 の敵対的レビュー)。
# subscriber はその列を持たない表への最初の複製行で
# "target relation is missing replicated column" を出し、全 publication を止める。
Q_COL="SELECT a.attrelid::regclass::text || '.' || a.attname || '|' || format_type(a.atttypid, a.atttypmod)
FROM pg_attribute a JOIN pg_class r ON r.oid=a.attrelid JOIN pg_namespace n ON n.oid=r.relnamespace
WHERE n.nspname='public' AND r.relkind='r' AND a.attnum>0 AND NOT a.attisdropped
  AND r.relname <> 'tagger_gt_exclude' AND $PUB_FILTER ORDER BY 1"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
PGPASSWORD=kura psql -h 127.0.0.1 -U kura -d kura -Atc "$Q_CON" > "$tmp/primary.con" || exit 1
PGPASSWORD=kura psql -h 127.0.0.1 -U kura -d kura -Atc "$Q_NN" > "$tmp/primary.nn" || exit 1
PGPASSWORD=kura psql -h 127.0.0.1 -U kura -d kura -Atc "$Q_COL" > "$tmp/primary.col" || exit 1
ssh "$REPLICA_HOST" "PGPASSWORD=kura psql -h 127.0.0.1 -U kura -d kura -Atc \"$Q_CON\"" > "$tmp/rep.con" || {
    echo "replica ($REPLICA_HOST) に届かない — 照合できていない (ゼロ差分の主張はしない)" >&2; exit 2; }
ssh "$REPLICA_HOST" "PGPASSWORD=kura psql -h 127.0.0.1 -U kura -d kura -Atc \"$Q_NN\"" > "$tmp/rep.nn" || exit 2
ssh "$REPLICA_HOST" "PGPASSWORD=kura psql -h 127.0.0.1 -U kura -d kura -Atc \"$Q_COL\"" > "$tmp/rep.col" || exit 2

# 取得が空 = 失敗したクエリのゼロは測定ではない (trainer-cc-primary の教訓)
for f in primary.con rep.con primary.nn rep.nn primary.col rep.col; do
    [[ -s "$tmp/$f" ]] || { echo "$f が空 — クエリ失敗の疑い、差分ゼロとは読まない" >&2; exit 2; }
done

status=0
echo "== 制約 (canonical にあって replica に無い / 定義差) =="
if comm -23 <(sort "$tmp/primary.con") <(sort "$tmp/rep.con") | grep .; then status=1; else echo "(なし)"; fi
echo "== 制約 (replica のみ) =="
comm -13 <(sort "$tmp/primary.con") <(sort "$tmp/rep.con") | grep . && status=1 || echo "(なし)"
echo "== NOT NULL (canonical にあって replica に無い) — apply 強制クラス、要対応 =="
if comm -23 <(sort "$tmp/primary.nn") <(sort "$tmp/rep.nn") | grep .; then status=1; else echo "(なし)"; fi
echo "== NOT NULL (replica のみ) — replica の方が厳しい = apply が落ち得る、最優先 =="
comm -13 <(sort "$tmp/primary.nn") <(sort "$tmp/rep.nn") | grep . && status=1 || echo "(なし)"
echo "== 列 (canonical にあって replica に無い / 型差) — apply が最初の複製行で止まる、最優先 =="
if comm -23 <(sort "$tmp/primary.col") <(sort "$tmp/rep.col") | grep .; then status=1; else echo "(なし)"; fi
echo "== 列 (replica のみ) — 余分な NOT NULL 列があれば apply が落ち得る =="
comm -13 <(sort "$tmp/primary.col") <(sort "$tmp/rep.col") | grep . && status=1 || echo "(なし)"
exit $status
