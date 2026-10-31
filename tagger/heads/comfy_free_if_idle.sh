#!/usr/bin/env bash
# comfy_free_if_idle.sh — free ComfyUI's *idle* model cache before the GPU
# batch gate probes VRAM, so an idle-but-cached ComfyUI does not falsely HOLD
# the tagger update cycle.
#
# 恒久対策 (a) / task 44b099ab.  Why this exists:
#   gpu_batch_gate.py HOLDs the tagger when total GPU VRAM exceeds
#   foreign_vram_mb_max (1500MB, trainer gpu_policy). ComfyUI keeps its models
#   resident (~6.5GB) even when idle (util=0, queue empty), which trips a FALSE
#   HOLD:foreign_gpu — the GPU is actually free, just cached. Raising the
#   threshold (option b) was rejected: it would weaken the "tagger yields to
#   ComfyUI" policy and blur the busy-vs-idle judgment. Instead we unload only
#   the idle cache so the gate's very next probe sees the GPU free and ADMITs
#   in the same run. ComfyUI reloads its models automatically on next use.
#
# Safety — the pinpoint part of the fix:
#   We free ONLY when ComfyUI is idle (queue_remaining == 0). If ComfyUI is
#   generating, we touch nothing; the gate then still HOLDs on util, which is
#   the intended policy. Active jobs are never disturbed.
#
# Wiring: ExecStartPre= of tagger-head-update.service (runs BEFORE the gate).
#   Any other gate-wrapped cycle that shares this GPU can reuse it the same way.
#   ALWAYS exits 0 — a hiccup here must never fail/block the cycle.
set -uo pipefail

COMFY="${COMFY_URL:-http://127.0.0.1:8188}"
# Keep in sync with trainer gpu_policy.batch.foreign_vram_mb_max (1500).
THRESHOLD_MB="${FOREIGN_VRAM_MB_MAX:-1500}"
WAIT_S="${COMFY_FREE_WAIT_S:-15}"

log() { echo "[comfy-free] $*"; }

# 1. Reachable + idle?  queue_remaining covers running + pending.
remaining=$(curl -s -m 5 "$COMFY/prompt" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["exec_info"]["queue_remaining"])' 2>/dev/null) || {
    log "ComfyUI unreachable at $COMFY — nothing to free, gate proceeds"; exit 0; }

if [ "${remaining:-1}" != "0" ]; then
  log "ComfyUI busy (queue_remaining=$remaining) — leaving models resident; gate will HOLD as intended"
  exit 0
fi

# 2. Idle → ask ComfyUI to unload models and free its cache (async flags).
code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' -X POST "$COMFY/free" \
  -H 'Content-Type: application/json' \
  -d '{"unload_models": true, "free_memory": true}') || code=000
log "idle → POST /free -> HTTP ${code}"

# 3. The unload is processed asynchronously by ComfyUI's worker loop. Wait
#    (bounded) for VRAM to actually drop below the gate threshold so the gate's
#    probe right after this ExecStartPre sees the freed state.
nvsmi=$(command -v nvidia-smi || true)
if [ -n "$nvsmi" ]; then
  for _ in $(seq 1 "$WAIT_S"); do
    used=$("$nvsmi" --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
    [ -z "${used:-}" ] && break
    if [ "$used" -lt "$THRESHOLD_MB" ]; then
      log "VRAM settled at ${used}MB (< ${THRESHOLD_MB}MB) — gate should ADMIT"
      exit 0
    fi
    sleep 1
  done
  log "VRAM still ${used:-?}MB after ${WAIT_S}s (unload lagging) — proceeding; gate re-probes anyway"
fi
exit 0
