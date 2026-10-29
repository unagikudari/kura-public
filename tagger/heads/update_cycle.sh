#!/usr/bin/env bash
# tagger head 自動更新サイクル:
#  ① 純人手GT + 検証還流で retrain ② ≥0.95 を機械適用 + 検証pool生成 ③ 世代経過poolを引退
set -uo pipefail
# Code lives in the kura repo (this file); ARTIFACTS do not. DATA is where models,
# logs and ledgers live -- outside any repo, so a clean checkout carries no state.
DATA="${TAGGER_HEADS_DATA:-$HOME/tagger-heads}"
export TAGGER_HEADS_DATA="$DATA"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DATA"
exec >> cycle.log 2>&1
echo "=== cycle start $(date -Is) ==="
# GPU 排他は gpu_batch_gate.py (--once --no-wait) に一本化。
# ExecStart 側の gate が HOLD なら rc75 で skip し、この cycle は起動しない。
# (旧: inline nvidia-smi used>10000 skip は plan kura-lora-schema-dashboard rev2 P2 で撤去)
# ── 失敗を broker から見えるようにする ────────────────────────────────────
# 2026-08-02〜03 に本サイクルは **5 回連続で失敗し 2 日間止まった**が誰も気づか
# なかった。journal に出るのは gate の `WORKER_EXIT rc=1` だけで、本文は cycle.log
# に埋もれる (上の exec で stdout/stderr をここへ流しているため)。
# **失敗が黙るなら監視は存在しないのと同じ**なので、結果を signal にする。
fail() {  # fail <段階名> — 直近のエラー行を添える (「失敗した」だけでは次の 1 手が無い)
  local last
  last=$(grep -E "Error|error:|Traceback|FAILED" cycle.log | tail -1 | cut -c1-300)
  python3 "$HERE/cycle_signal.py" 1 "$1 が失敗: ${last:-(詳細なし)}"
  echo "$1 FAILED"
  exit 1
}

# SECRET FROM THE ENVIRONMENT, NOT FROM A PATH. `cat .ktok` was the last reader of a
# secret file sitting beside the code -- which is exactly what makes a secret
# committable. Fail closed: a cycle that runs without a token would fail later, in
# the middle of an apply, instead of here.
if [ -z "${KTOK:-}" ] && [ -r "$DATA/.ktok" ]; then
    # transitional: the token still lives beside the artifacts, never in the repo.
    KTOK=$(cat "$DATA/.ktok")
fi
if [ -z "${KTOK:-}" ]; then
    echo "KTOK is not set and no token is readable under $DATA -- refusing to run."
    exit 78
fi
export KTOK
PY=${PY:-$HOME/.local/share/comfyui-venv/bin/python3}
python3 "$HERE/fetch_excluded.py" || fail exclusions
$PY "$HERE/train_heads.py" || fail train
GEN=$(( $(cat generation 2>/dev/null || echo 0) + 1 )); echo $GEN > generation
APPLY_SRC="trainer-head-g$GEN" $PY "$HERE/auto_apply.py" || fail apply
python3 "$HERE/retire_pools.py" --generations 3 || echo "retire skipped"
python3 "$HERE/record_generation.py" || echo "gen record skipped"
# ダッシュボード生成・push は退役 (2026-07-20, primary-cc-2 依頼)。
# primary は kura-web +page.svelte(iframe)→ /api/tagger-dashboard?raw=1 の schema 直結 動的render に
# 全面移行済み。この静的HTML(primary:kura-data/tagger-dashboard.html)は primary から配信されておらず
# (nginx/kura-api の static マウントに無し)、push しても誰も見ていない。
# retrain+auto_apply(本体作業)は継続。dashboard 3行だけを撤去した。
# 復活が要る場合は git 履歴 / update_cycle.sh.pre-gate.bak 参照。
python3 "$HERE/cycle_signal.py" 3 "cycle 完了 (generation=$GEN)"
echo "=== cycle done $(date -Is) ==="
