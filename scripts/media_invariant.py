#!/usr/bin/env python3
"""Missing media, counted (#56): a displayable post (status ok) must have a thumbnail
somewhere the API serves it from — a thumb rendition, the row's thumbnail, or the file the
legacy path redirects to. Read-only; prints one JSON line:

    {"observed": true, "checked": N, "missing": M, "sample": [post ids]}

`observed` is false (and nothing else is claimed) when the database cannot be read: an
invariant that was not looked at is never reported as holding.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SAMPLE = 20
QUERY = """
SELECT p.id FROM post p
 WHERE p.status = 'ok' AND p.thumbnail IS NULL
   AND NOT EXISTS (SELECT 1 FROM post_rendition r WHERE r.post_id = p.id AND r.kind = 'thumb')
 ORDER BY p.id
"""


def _is_thumbnail_file(path) -> bool:
    """A regular file at the path (through any link): absent is False; a directory or other
    non-file is False; an I/O error is not an answer and propagates (the caller says it could
    not observe)."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(st.st_mode)


def measure(dsn: str) -> dict:
    try:                    # any failure to look is "not observed", never "holds"
        import psycopg2
        from api.services import files
        with psycopg2.connect(dsn, options="-c default_transaction_read_only=on -c statement_timeout=600000") as pg:
            with pg.cursor() as cur:
                cur.execute(QUERY)
                no_stored = [r[0] for r in cur.fetchall()]
        missing = [pid for pid in no_stored if not _is_thumbnail_file(files.names("thumbnail", pid)[0])]
    except Exception as e:  # noqa: BLE001
        return {"observed": False, "error": f"{type(e).__name__}: {e}"[:300]}
    return {"observed": True, "checked": len(no_stored), "missing": len(missing), "sample": missing[:SAMPLE]}


def main() -> int:
    from api.config import DATABASE_URL_SYNC
    out = measure(DATABASE_URL_SYNC)
    print(json.dumps(out))
    return 0 if out["observed"] and out["missing"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
