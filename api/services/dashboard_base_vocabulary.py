"""Operator-facing view of a pinned base vocabulary report."""
import asyncio
import os
from pathlib import Path

from sqlalchemy import text

from api.services.dashboard_base_comparison import read_json

MODEL = "wd-eva02-large-tagger-v3"
REVISION = "b25b82a03f7282e41aa2f257a52c7583b710bd1c"


async def read_vocabulary(db, path=None):
    if path is None:
        state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        path = state / "kura-tagger/base-vocabulary" / f"{MODEL}@{REVISION[:12]}.json"
    report = await asyncio.to_thread(read_json, path)
    if (not isinstance(report, dict) or report.get("schema") != "tagger-base-vocabulary/2"
            or report.get("model") != MODEL or report.get("revision") != REVISION):
        return dict(state="unavailable")
    rows = report.get("unmapped_held")
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("name"), str)
        or not row["name"] or len(row["name"]) > 500
        or type(row.get("category")) is not int for row in rows
    ):
        return dict(state="unavailable")
    by_name = {row["name"]: row["category"] for row in rows}
    known = set()
    if by_name:
        known = set((await db.execute(text("""
            SELECT name FROM tag_name WHERE name=ANY(:names)
        """), {"names": list(by_name)})).scalars())
    return dict(state="available", tags=[
        dict(name=name, category=by_name[name]) for name in sorted(by_name.keys() - known)
    ])
