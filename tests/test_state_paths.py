"""Runtime state (the reap authority file) resolves through one function (api/state_paths.py).

Until 2026-10-25 the pools dashboard read tagger/reap_authorized.json, where the file had
not been since kura#44, so it showed no reap authority; the writer, the gate and the backfill
each spelled the path themselves. KURA_STATE_DIR lets a node keep the file outside the
checkout (nodeb: ~/kura-data/state, docs/notes/kura-on-nodeb-plan.md).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from api import state_paths

REPO = Path(__file__).resolve().parents[1]


def test_resolution_order(tmp_path):
    assert state_paths.reap_authority_path({}) == REPO / "data" / "tagger" / "reap_authorized.json"
    assert state_paths.reap_authority_path({"KURA_STATE_DIR": str(tmp_path)}) == \
        tmp_path / "tagger" / "reap_authorized.json"
    explicit = tmp_path / "x.json"
    assert state_paths.reap_authority_path(
        {"KURA_STATE_DIR": "/elsewhere", "KURA_REAP_AUTHORITY_PATH": str(explicit)}) == explicit


def test_every_user_goes_through_the_resolver():
    users = ["api/services/tagging.py", "api/routers/pools.py",
             "scripts/backfill_head_policy.py", "tagger/reap_authorization_eval.py"]
    for rel in users:
        src = (REPO / rel).read_text()
        assert "reap_authority_path" in src, rel
        assert '"reap_authorized.json")' not in src, f"{rel} spells the path itself"


def test_the_writer_imports_the_resolver_when_run_by_path():
    """Run from another directory with the repo off sys.path, as the unit does (by path).
    Codex 1.267.2: the first version opened a relative path from tagger/ and passed on the
    resulting FileNotFoundError; it now requires a clean exit."""
    script = REPO / "tagger" / "reap_authorization_eval.py"
    out = subprocess.run([sys.executable, "-c",
                          "import runpy,sys; "
                          f"sys.path[:] = [p for p in sys.path if p not in ('', {str(REPO)!r})]; "
                          f"runpy.run_path({str(script)!r}, run_name='not_main'); "
                          "import api.state_paths as s; print(s.reap_authority_path())"],
                         cwd="/", capture_output=True, text=True, timeout=60,
                         env={k: v for k, v in __import__("os").environ.items() if k != "PYTHONPATH"})
    assert out.returncode == 0, out.stderr[-800:]
    assert out.stdout.strip().endswith("reap_authorized.json")
