"""Capture VERSION + git commit/branch at module-import time.

Read by /api/info so clients can verify exactly which deployment they're
hitting. Falls back to "unknown" if VERSION or git data is unavailable
(e.g. running from a tarball without a .git dir).
"""
from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent  # api/build_info.py → repo


def _read_version() -> str:
    f = _REPO_ROOT / "VERSION"
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    return "0.0.0"


def _last_release_date() -> str:
    cl = _REPO_ROOT / "CHANGELOG.md"
    if not cl.exists():
        # The public tree ships without the CHANGELOG: its release is the commit.
        return _git(["git", "log", "-1", "--format=%cs"])
    # Match both legacy `## [x.y.z] - YYYY-MM-DD` and current
    # `## x.y.z — YYYY-MM-DD` (em-dash) header formats.
    m = re.search(
        r"^##\s*\[?(\d+\.\d+\.\d+)\]?\s*[-—]\s*(\d{4}-\d{2}-\d{2})",
        cl.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    return m.group(2) if m else "unknown"


def _git(cmd: list[str], fallback: str = "unknown") -> str:
    try:
        return subprocess.check_output(
            cmd,
            cwd=str(_REPO_ROOT),
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="replace").strip()
    except Exception:
        return fallback


VERSION: str = _read_version()
RELEASE_DATE: str = _last_release_date()
COMMIT: str = _git(["git", "rev-parse", "--short", "HEAD"])
BRANCH: str = _git(["git", "rev-parse", "--abbrev-ref", "HEAD"])
DIRTY: bool = bool(_git(
    ["git", "status", "--porcelain", "--untracked-files=no"],
    fallback="",
))
START_TIME: str = datetime.now(tz=timezone.utc).isoformat()


def as_dict() -> dict:
    return {
        "version": VERSION,
        "releaseDate": RELEASE_DATE,
        "commit": COMMIT,
        "branch": BRANCH,
        "dirty": DIRTY,
        "startTime": START_TIME,
    }
