#!/bin/sh
# Description: the forced ssh command for the archive node's key on the source node — loads
#              the same environment the kura units do, then runs `original_archive
#              remote-dispatch` (docs/notes/kura-on-nodeb-plan.md, rev 8)
# Effects: whatever the dispatched command does (remote-offer / -read / -commit / -status)
#
# An ssh forced command inherits no EnvironmentFile: without this the dispatcher would run
# with the code defaults (DATA_DIR=/data, the database on localhost:5432) instead of the
# node's (codex review of 1.270.0). authorized_keys on the source node:
#   command="/home/<user>/kura/scripts/original_archive_dispatch.sh",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 …
set -eu
repo="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo"
set -a
. "${KURA_API_ENV:-$repo/api/env}"
archive_env="${KURA_ARCHIVE_ENV:-$HOME/.config/kura/original-archive-remote.env}"
[ -f "$archive_env" ] && . "$archive_env"
set +a
exec "${KURA_PYTHON:-$HOME/.local/share/kura-api/venv/bin/python}" -m scripts.original_archive remote-dispatch
