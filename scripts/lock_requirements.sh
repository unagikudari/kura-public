#!/usr/bin/env bash
# Re-pin api/requirements.lock from api/requirements.txt. uv keeps the existing pins as
# preferences, so a security fix moves only what it names:
#   scripts/lock_requirements.sh --upgrade-package starlette
# Everything:  scripts/lock_requirements.sh --upgrade
# Then: tests, `python3 scripts/vuln_watch.py --dry-run`, review, and install the lock
# where kura runs (`~/.local/share/kura-api/venv/bin/pip install -r api/requirements.lock`).
set -euo pipefail
cd "$(dirname "$0")/.."
uv=$(command -v uv || echo "$HOME/.local/bin/uv")
"$uv" pip compile api/requirements.txt --python-version 3.14 --no-header --no-annotate --generate-hashes -q \
    -o api/requirements.lock "$@"
