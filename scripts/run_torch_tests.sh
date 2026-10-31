#!/usr/bin/env bash
# Description: torch を要するテストだけを、torch の入る interpreter で走らせる。
# Kind: oneshot (gate)
# Effects: read-only (pytest のみ)
# Runs-on: torch を持つ node
#
# WHY THIS EXISTS
#
# `train_heads.py` の損失を per-sample 重みへ変えたとき、「既存タグの学習は変わらない」を
# torch で実測して確かめた。しかし kura の test venv に torch は無く、その 2 本は
# `importorskip` で **skip されていた** — 実測は済んでいるのに**回帰は守れていない**状態。
#
# 「skip されたテストはテストではない」(tests/test_mega_key_pending_db.py の言い方) が
# ここでも当てはまる。敵対的レビュー (primary-codex, 2026-09-04) の blocking:
#
#   > A one-off measurement does not protect future changes. Add a required
#   > torch-capable CI/job and make its result a merge/release gate.
#
# **venv は 1 つも変更しない。** 3 つの venv はすべて Python 3.14.4 なので、torch を持つ
# venv の site-packages を PYTHONPATH で足すだけで、pytest 側から import できる。
# torch を test venv に入れると数 GB、comfyui venv に pytest を入れると本番 ML 環境を
# 触ることになる — どちらも避ける。
set -uo pipefail

PYTEST_PY="${KURA_TEST_PY:-$HOME/.local/share/kura-api/venv/bin/python}"

# torch を持つ site-packages を探す。見つからなければ **skip ではなく失敗** させる。
# 見つからないことを「torch テストは無かった」と読ませないため。
TORCH_SP=""
for cand in "$HOME/tagger-heads/.venv314" "$HOME/.local/share/comfyui-venv" "$HOME/ComfyUI/.venv" "$HOME/kura1-detect-venv"; do
    sp="$cand/lib/python3.14/site-packages"
    if [ -d "$sp/torch" ]; then TORCH_SP="$sp"; break; fi
done
if [ -z "$TORCH_SP" ]; then
    echo "[torch-gate] FAIL: torch を持つ site-packages が見つからない。" >&2
    echo "  探した場所: ~/tagger-heads/.venv314, ~/.local/share/comfyui-venv, ~/ComfyUI/.venv, ~/kura1-detect-venv" >&2
    echo "  ここで skip すると、損失の意味を変える変更が無検査で通る。" >&2
    exit 1
fi
echo "[torch-gate] torch site-packages: $TORCH_SP"

cd "$(dirname "${BASH_SOURCE[0]}")/.."
out=$(PYTHONPATH="$TORCH_SP${PYTHONPATH:+:$PYTHONPATH}" "$PYTEST_PY" -m pytest \
      -m torch -q --no-header 2>&1)
rc=$?
echo "$out" | tail -5

# **collect が 0 件なら失敗。** 「全部通った」と「1 本も走らなかった」を区別する。
# マーカーの綴りを 1 文字間違えるだけで後者になり、緑に見える。
if echo "$out" | grep -qE "no tests ran|collected 0 items"; then
    echo "[torch-gate] FAIL: torch マーカーのテストが 1 本も収集されなかった。" >&2
    echo "  マーカー名の綴りか、テスト側の @pytest.mark.torch を確認すること。" >&2
    exit 1
fi
[ $rc -eq 0 ] && echo "[torch-gate] OK" || echo "[torch-gate] FAIL (pytest rc=$rc)" >&2
exit $rc
