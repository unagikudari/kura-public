#!/usr/bin/env bash
# kura auto-tagger host-agnostic installer.
#
# Creates a venv under ~/.local/share/kura-tagger/, installs ML deps,
# pre-downloads the three ONNX models so the first timer fire is fast,
# renders systemd user service + timer pointing at the current kura repo
# clone, and enables the timer.
#
# Usage:
#   bash tagger/install.sh                     install + enable + start
#   bash tagger/install.sh --uninstall         stop + disable + remove units (venv kept)
#   bash tagger/install.sh --disable-legacy    also stop/disable wd14-tagger.{service,timer}
#
# Prerequisites (must exist before running):
#   tagger/env   EnvironmentFile with KURA_URL, KURA_USER, KURA_TOKEN
#                e.g.
#                  KURA_URL=https://archive.example.invalid
#                  KURA_USER=owner
#                  KURA_TOKEN=<kura user token>

set -euo pipefail

# ── retired pending worker consolidation (plan:kura-tagger-lifecycle-redesign
# Step 4/12, 2026-07-20) ────────────────────────────────────────────────
# This installer's kura-tagger.service is the pre-consolidation, non-
# release-aware worker (PixAI+ml-danbooru+WD14, no claim guard, no
# generation/provider heartbeat check). It raced against the newer
# release-aware kura-autotag.service on nodec/nodeb for the same claim queue
# (non-deterministic per-post model selection, confirmed live 2026-07-20),
# and separately drifted back to `enabled` on its own after a plain
# `systemctl --user disable` (confirmed on primary/desk same day) -- this
# script is exactly the mechanism that would silently resurrect it again.
# Refuse by default until Step 12 (tagger/worker.py consolidation +
# claim-guard integration) ships; a deliberate operator can still force it.
if [[ "${1:-}" != "--uninstall" && -z "${KURA_TAGGER_INSTALL_FORCE:-}" ]]; then
    echo "ERROR: tagger/install.sh is disabled pending worker consolidation" >&2
    echo "       (plan:kura-tagger-lifecycle-redesign Step 12 -- see this script's" >&2
    echo "       header comment). Set KURA_TAGGER_INSTALL_FORCE=1 to override if" >&2
    echo "       you specifically intend to run the pre-consolidation worker." >&2
    exit 1
fi

SERVICE_NAME="kura-tagger"
KURA_REPO="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$HOME/.local/share/kura-tagger/venv"
MODELS_DIR="$HOME/.cache/kura-tagger/models"
SYSTEMD_DIR="$HOME/.config/systemd/user"
ENV_FILE="$KURA_REPO/tagger/env"
DISABLE_LEGACY=0

for arg in "$@"; do
    case "$arg" in
        --disable-legacy) DISABLE_LEGACY=1 ;;
    esac
done

# ── uninstall ────────────────────────────────────────────────────────
if [[ "${1:-}" == "--uninstall" ]]; then
    systemctl --user stop "$SERVICE_NAME.timer" 2>/dev/null || true
    systemctl --user stop "$SERVICE_NAME.service" 2>/dev/null || true
    systemctl --user disable "$SERVICE_NAME.timer" 2>/dev/null || true
    systemctl --user disable "$SERVICE_NAME.service" 2>/dev/null || true
    rm -f "$SYSTEMD_DIR/$SERVICE_NAME.service" "$SYSTEMD_DIR/$SERVICE_NAME.timer"
    systemctl --user daemon-reload
    echo "[kura-tagger] systemd units removed. venv + models kept at $VENV_DIR / $MODELS_DIR"
    exit 0
fi

# ── pre-flight ───────────────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE not found." >&2
    echo "Create it with the following keys before running install:" >&2
    echo "  KURA_URL=https://archive.example.invalid" >&2
    echo "  KURA_USER=<username>" >&2
    echo "  KURA_TOKEN=<kura user token>" >&2
    exit 1
fi

# ── venv + deps ──────────────────────────────────────────────────────
echo "[kura-tagger] venv -> $VENV_DIR"
mkdir -p "$(dirname "$VENV_DIR")"
if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
"$VENV_DIR/bin/pip" install -r "$KURA_REPO/tagger/requirements.txt"

# ── pre-download models (avoid first-run surprise + cross-host HF throttle) ──
mkdir -p "$MODELS_DIR"
echo "[kura-tagger] pre-downloading ONNX models -> $MODELS_DIR"
echo "              (first run may take several minutes, subsequent installs reuse the cache)"
KURA_REPO="$KURA_REPO" KURA_MODELS_DIR="$MODELS_DIR" "$VENV_DIR/bin/python" - <<'PY'
import os, sys
sys.path.insert(0, os.environ["KURA_REPO"])
from tagger.infer import load_all_models
load_all_models()
print("[kura-tagger] models ready")
PY

# ── legacy wd14-tagger coexistence ───────────────────────────────────
if systemctl --user list-unit-files --no-legend 2>/dev/null | grep -qE '^wd14-tagger\.(service|timer)'; then
    if (( DISABLE_LEGACY == 1 )); then
        echo "[kura-tagger] stopping legacy wd14-tagger units"
        systemctl --user stop wd14-tagger.timer 2>/dev/null || true
        systemctl --user stop wd14-tagger.service 2>/dev/null || true
        systemctl --user disable wd14-tagger.timer 2>/dev/null || true
        systemctl --user disable wd14-tagger.service 2>/dev/null || true
    else
        echo "[kura-tagger] WARNING: legacy wd14-tagger units are present and active."
        echo "              The two taggers can coexist (writes are idempotent via ON CONFLICT),"
        echo "              but it wastes compute on double inference."
        echo "              Rerun with --disable-legacy once this worker proves stable."
    fi
fi

# ── systemd units ────────────────────────────────────────────────────
mkdir -p "$SYSTEMD_DIR"

sed \
    -e "s|__KURA_REPO__|$KURA_REPO|g" \
    -e "s|__VENV__|$VENV_DIR|g" \
    -e "s|__MODELS_DIR__|$MODELS_DIR|g" \
    "$KURA_REPO/tagger/systemd/$SERVICE_NAME.service" > "$SYSTEMD_DIR/$SERVICE_NAME.service"


chmod 600 "$ENV_FILE"

# ── enable + start ───────────────────────────────────────────────────
systemctl --user daemon-reload
# legacy timer cleanup (was oneshot+timer; now persistent service)
systemctl --user stop    "$SERVICE_NAME.timer" 2>/dev/null || true
systemctl --user disable "$SERVICE_NAME.timer" 2>/dev/null || true
rm -f "$SYSTEMD_DIR/$SERVICE_NAME.timer"

systemctl --user enable --now "$SERVICE_NAME.service"

echo "[kura-tagger] installed."
echo "  service:  $SYSTEMD_DIR/$SERVICE_NAME.service"
echo "  venv:     $VENV_DIR"
echo "  models:   $MODELS_DIR"
echo "  env file: $ENV_FILE (chmod 600)"
echo
systemctl --user status "$SERVICE_NAME.service" --no-pager || true
