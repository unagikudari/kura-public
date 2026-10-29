"""Where a node keeps runtime state that is neither code (git) nor the database.

Runtime state lives outside tracked paths so the production tree stays clean (/api/build's
`dirty` flag is ground truth). Resolution, first match wins:

    KURA_REAP_AUTHORITY_PATH            the file itself (existing override, kept)
    KURA_STATE_DIR/tagger/<name>        a node's state directory (nodeb: ~/kura-data/state)
    <repo>/data/tagger/<name>           the default, as before

One resolver for every reader and writer: until 2026-10-25 the pools dashboard read
`tagger/reap_authorized.json`, a path the file had left in kura#44, and showed no reap
authority at all.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def state_dir(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    configured = (environ.get("KURA_STATE_DIR") or "").strip()
    return Path(configured) if configured else REPO / "data"


def reap_authority_path(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    explicit = (environ.get("KURA_REAP_AUTHORITY_PATH") or "").strip()
    return Path(explicit) if explicit else state_dir(environ) / "tagger" / "reap_authorized.json"
