#!/usr/bin/env bash
# Description: phase-3 calibration grid for train_base_finetune.py — targeted anti-forgetting.
# Kind: tool
# Effects: read-only on the kura DB; writes result json + logs under $OUTDIR
# Runs-on: trainer (only CUDA node)
#
# WHY THIS GRID
# -------------
# Phase 2 swept kd_weight={1,5,20} and preservation degraded MONOTONICALLY in all three
# arms (conf_jaccard@0.35 = 0.893 / 0.896 / 0.916 at step 400, against a 0.98 gate). The
# diagnosis: the KD term is a mean over ~10,849 near-zero teacher columns, so the ~30
# columns that actually drift are 0.3% of it and kd_weight scales signal and filler
# together. kd_weight is therefore the wrong knob, and phase 3 turns the OTHER ones:
#
#   * the LOSS: --kd-mode band concentrates KD on (teacher p>=tau) U (teacher top-k), and
#     --anchor-weight adds an L2 on exactly the conf_jaccard@0.35 union set (see
#     kd_band_mask / anchor_loss in train_base_finetune.py);
#   * CAPACITY: lora rank 8 -> 4 -> 2. alpha is held at 2*r so the LoRA scale (alpha/r)
#     stays 2.0 across arms -- otherwise dropping r with alpha fixed would RAISE the
#     effective step size and confound "less capacity" with "bigger updates";
#   * STEP SIZE: lora lr 3e-4 / 1e-4 / 3e-5 / 1e-5. head_lr stays 1e-3 in every arm:
#     conf_jaccard is measured on non-GT columns only, so the head rows cannot move it,
#     and throttling them would only cost GT gain for no preservation benefit.
#
# The `head-only` arm (--lr 0) is the control the whole premise must beat: it freezes the
# backbone, so drift is zero BY CONSTRUCTION and whatever GT it gains is free. If it wins,
# the base fine-tune has degenerated into the logit-patch mechanism it was meant to replace.
#
# Every arm is --no-gate (calibrate, never abort) and shares --seed 0, so the seeded
# TABLESAMPLE hands all of them the identical KD and preservation draws. Arms differ only
# in the knob under test.
set -euo pipefail

PY=${PY:-$HOME/.local/share/comfyui-venv/bin/python}
REPO=${REPO:-$HOME/kura}
OUTDIR=${OUTDIR:-$HOME/tagger-heads/finetune}
LOGDIR="$OUTDIR/sweep3"
export KURA_FINETUNE_BLOB_CACHE=${KURA_FINETUNE_BLOB_CACHE:-$OUTDIR/blobs}
mkdir -p "$LOGDIR"

# Held identical to phase 2 so the arms are comparable to its numbers:
#   erection is below the live min_gt_positive floor (19 < 20) but is one of the largest
#   correction domains, so it trains via --extra-tags with LOW-CONFIDENCE metrics;
#   cum_in_cup has no row in the base vocab at all and the script reports it as skipped
#   (appending a row is build_release's "add" op, out of scope here).
COMMON=(--base release --extra-tags erection --neg-per-pos 20
        --kd-posts 800 --pres-val-posts 200 --max-val-posts 600
        --steps 400 --gate-every 100 --no-gate --seed 0 --outdir "$OUTDIR")

# name              r  alpha  lr     anchor_weight
ARMS=(
  "head-only        8  16     0      1"
  "band-r8-lr1e-4   8  16     1e-4   1"
  "band-r4-lr1e-4   4  8      1e-4   1"
  "band-r2-lr1e-4   2  4      1e-4   1"
  "band-r4-lr3e-4   4  8      3e-4   1"
  "band-r2-lr3e-5   2  4      3e-5   1"
  "band-r2-lr1e-5   2  4      1e-5   1"
  "band-r4-anc20    4  8      1e-4   20"
)

for arm in "${ARMS[@]}"; do
  read -r name r alpha lr anc <<<"$arm"
  echo "=== arm $name (r=$r alpha=$alpha lr=$lr anchor=$anc) ==="
  "$PY" "$REPO/tagger/train_base_finetune.py" "${COMMON[@]}" \
      --lora-r "$r" --lora-alpha "$alpha" --lr "$lr" --head-lr 1e-3 \
      --kd-mode band --kd-weight 1.0 --kd-topk 32 --kd-tau 0.1 \
      --anchor-weight "$anc" --anchor-thresh 0.35 \
      --run-name "$name" >"$LOGDIR/$name.log" 2>&1 \
    || echo "  arm $name exited $? (see $LOGDIR/$name.log)"
  tail -3 "$LOGDIR/$name.log" | head -1
done

echo "=== done; summarise with: $PY $REPO/tagger/summarize_finetune_sweep.py $OUTDIR ==="
