#!/usr/bin/env bash
# Description: replica_schema_audit.sh を定期実行し、結果を broker signal に載せる (#60 の backstop)
# Kind: oneshot
# Effects: read-only (audit 自体が read-only; broker への signal 送信のみ)
# Requires: scripts/replica_schema_audit.sh, ~/broker/scripts/report_signal_cli.py
# Runs-on: primary
#
# なぜ (trainer-cc-primary の指摘、2026-09-04, task 71b8ab9e):
# #60 の self-stamp が入るまで、replica の schema drift は 2 つの検出器が捕えて
# いた — `alembic-head-agrees-across-nodes` (台帳の一致) と vnext の
# `replica_schema_matches`。どちらも**台帳**を見る。self-stamp は台帳を進める
# ので、`REPLICA_DDL=None` の宣言そのものが間違っていた場合 (DDL が要るのに
# publisher-only と宣言した)、replica は DDL 無しで台帳だけ進め、**両検出器が
# 揃って黙る**。実スキーマを直接照合する replica_schema_audit.sh だけが残る
# backstop になり、その重みが self-stamp の導入で変わった。
#
# 手で思い出して叩く運用のままでは backstop が「あるが動いていない」— 今日
# 何度も見た dead state の形そのもの。timer で定期実行し、結果を signal にする。
#
# exit の意味 (audit から引き継ぐ):
#   0 = 差分なし        -> signal は送らない (静かな正常)
#   1 = 差分あり        -> urgency=1 (apply 強制クラスの差は crash-loop の種)
#   2 = 照合できなかった -> urgency=2。**「差分ゼロ」とは決して読まない**
#       (取得失敗のゼロを測定と読まない、という audit 自身の規律の延長)
set -uo pipefail

REPO="${KURA_REPO:-$HOME/kura}"
AUDIT="$REPO/scripts/replica_schema_audit.sh"
REPORT_CLI="${KURA_REPORT_SIGNAL_CLI:-$HOME/broker/scripts/report_signal_cli.py}"
REPLICA_HOST="${1:-trainer}"

out="$(bash "$AUDIT" "$REPLICA_HOST" 2>&1)"
rc=$?
echo "$out"

report() {  # report <key> <urgency> <detail>
    [[ -x "$REPORT_CLI" || -f "$REPORT_CLI" ]] || return 0
    python3 "$REPORT_CLI" replica-schema "$1" "$2" "$3" || true
}

# detail は先頭 800 字に丸める (signal は要約であって全文の置き場ではない)
detail="$(printf '%s' "$out" | tail -c 800)"

case "$rc" in
    0) : ;;  # 正常時は黙る — 定期実行の静かなときに noise を作らない
    1) report "drift-$REPLICA_HOST" 1 "replica schema drift detected: $detail" ;;
    2) report "audit-unavailable-$REPLICA_HOST" 2 "replica schema audit could NOT run (not a pass): $detail" ;;
    *) report "audit-unexpected-$REPLICA_HOST" 2 "audit exited $rc (unexpected): $detail" ;;
esac

exit "$rc"
