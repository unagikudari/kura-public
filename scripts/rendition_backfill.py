#!/usr/bin/env python3
"""Copy legacy post.data / post.thumbnail bytes into content-addressed rendition files.

docs/notes/rendition-store-ideal.md, step 2. A copy, never a transcode: the
file is byte-identical to the column, named by its own SHA-256.

Each batch runs in one transaction that reads its rows FOR SHARE, so no writer
can change a column between "bytes read" and "rendition row committed". Files
are published before the rows that name them (api/services/renditions.py), so
an interruption leaves at most unreferenced files, never a row without bytes.
Re-running converges: rows that already have a rendition are skipped, and the
same bytes land on the same file.

Capacity: the run stops BEFORE a batch would take the rendition filesystem
below --reserve-gib free. The legacy columns are not freed until the night-time
NULL + VACUUM FULL, so every byte written here is additional until then.

    rendition_backfill.py --dry-run                 # count what remains, write nothing
    rendition_backfill.py --max-rows 10000          # one bounded pass
    rendition_backfill.py --reserve-gib 80 --verify # re-read each file and compare
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2  # noqa: E402

from api.services import renditions  # noqa: E402

KIND_COLUMN = {"view": "data", "thumb": "thumbnail"}

REMAINING = """
SELECT count(*) FILTER (WHERE p.data IS NOT NULL AND v.post_id IS NULL),
       count(*) FILTER (WHERE p.thumbnail IS NOT NULL AND t.post_id IS NULL)
  FROM post p
  LEFT JOIN post_rendition v ON v.post_id = p.id AND v.kind = 'view'
  LEFT JOIN post_rendition t ON t.post_id = p.id AND t.kind = 'thumb'
"""

# Ids still missing a rendition for either kind, in id order, after a cursor.
NEXT_IDS = """
SELECT p.id FROM post p
 WHERE p.id > %(after)s
   AND ((p.data IS NOT NULL AND NOT EXISTS
          (SELECT 1 FROM post_rendition r WHERE r.post_id = p.id AND r.kind = 'view'))
     OR (p.thumbnail IS NOT NULL AND NOT EXISTS
          (SELECT 1 FROM post_rendition r WHERE r.post_id = p.id AND r.kind = 'thumb')))
 ORDER BY p.id LIMIT %(limit)s
"""

LOCK_ROWS = """
SELECT p.id, p.data, p.thumbnail,
       EXISTS (SELECT 1 FROM post_rendition r WHERE r.post_id = p.id AND r.kind = 'view'),
       EXISTS (SELECT 1 FROM post_rendition r WHERE r.post_id = p.id AND r.kind = 'thumb')
  FROM post p WHERE p.id = ANY(%(ids)s) ORDER BY p.id FOR SHARE OF p
"""


def free_bytes(root: Path) -> int:
    probe = root
    while not probe.exists():
        probe = probe.parent
    return shutil.disk_usage(probe).free


def dsn() -> str:
    url = os.environ.get("DATABASE_URL", "")
    return url.replace("postgresql+asyncpg://", "postgresql://")


def run(conn, root: Path, batch: int, max_rows: int, reserve: int, verify: bool, log) -> dict:
    stats = {"posts": 0, "view": 0, "thumb": 0, "bytes_written": 0, "stopped": "done"}
    after = 0
    while not max_rows or stats["posts"] < max_rows:
        limit = batch if not max_rows else min(batch, max_rows - stats["posts"])
        with conn.cursor() as cur:
            cur.execute(NEXT_IDS, {"after": after, "limit": limit})
            ids = [r[0] for r in cur.fetchall()]
        conn.rollback()
        if not ids:
            break
        with conn:
            with conn.cursor() as cur:
                cur.execute(LOCK_ROWS, {"ids": ids})
                rows = cur.fetchall()
                need = sum(len(r[1] or b"") * (not r[3]) + len(r[2] or b"") * (not r[4]) for r in rows)
                if free_bytes(root) - need < reserve:
                    stats["stopped"] = "capacity"
                    conn.rollback()
                    break
                for post_id, data, thumb, has_view, has_thumb in rows:
                    for kind, blob, has in (("view", data, has_view), ("thumb", thumb, has_thumb)):
                        if blob is None or has:
                            continue
                        stored = renditions.store(bytes(blob), root, sync=False)
                        if verify and not renditions.verify(stored.path, stored.sha256):
                            raise RuntimeError(f"verify failed post {post_id} {kind}: {stored.path}")
                        obj, ref = renditions.attach_params(
                            post_id, kind, stored, renditions.LEGACY_RECIPE[kind])
                        cur.execute(renditions.psycopg(renditions.UPSERT_OBJECT), obj)
                        cur.execute(renditions.psycopg(renditions.ATTACH), ref)
                        stats[kind] += 1
                        stats["bytes_written"] += stored.byte_size
                    stats["posts"] += 1
                # Files before rows: everything this batch wrote is durable before
                # the transaction that names it commits (on leaving `with conn`).
                renditions.flush()
        after = ids[-1]
        log({"progress": stats, "after": after, "free_gib": round(free_bytes(root) / 2**30, 1)})
    return stats


# No cursor: a restored column leaves this set (and the trigger detaches its
# rendition row), so re-asking for the first N until empty visits every
# (post, kind) exactly once. A post_id cursor skipped the second kind of a post
# split across a batch boundary (review 2026-10-24).
RESTORE_IDS = """
SELECT r.post_id, r.kind, o.sha256, o.mime FROM post_rendition r JOIN rendition_object o USING (sha256)
 JOIN post p ON p.id = r.post_id
 WHERE (r.kind = 'view' AND p.data IS NULL) OR (r.kind = 'thumb' AND p.thumbnail IS NULL)
 ORDER BY r.post_id, r.kind LIMIT %(limit)s
"""


def restore_columns(conn, root: Path, batch: int, log) -> dict:
    """Rollback aid: write rendition bytes back into post.data / post.thumbnail.

    For returning to code that only reads the columns. The 0177 trigger drops the
    rendition row of every column it fills, so the result is exactly the legacy
    shape. Each file is checked against its content address before it is used.
    """
    stats = {"view": 0, "thumb": 0}
    while True:
        with conn:
            with conn.cursor() as cur:
                cur.execute(RESTORE_IDS, {"limit": batch})
                todo = cur.fetchall()
                if not todo:
                    break
                for post_id, kind, sha, mime in todo:
                    path = renditions.path_for(bytes(sha), mime, root)
                    if not renditions.verify(path, bytes(sha)):
                        raise RuntimeError(f"rendition file differs from its address: {path}")
                    cur.execute(f"UPDATE post SET {KIND_COLUMN[kind]} = %s WHERE id = %s",
                                (path.read_bytes(), post_id))
                    stats[kind] += 1
        log({"restored": stats})
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--max-rows", type=int, default=0, help="0 = until done")
    ap.add_argument("--reserve-gib", type=float, default=80.0,
                    help="free space to leave on the rendition filesystem (WAL, slots, VACUUM FULL)")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--root", type=Path, default=None)
    ap.add_argument("--restore-columns", action="store_true",
                    help="rollback: copy rendition bytes back into post.data/thumbnail")
    a = ap.parse_args(argv)
    root = a.root or renditions.ROOT
    conn = psycopg2.connect(dsn())
    log = lambda obj: print(json.dumps(obj, ensure_ascii=False), flush=True)  # noqa: E731
    with conn.cursor() as cur:
        cur.execute(REMAINING)
        view_left, thumb_left = cur.fetchone()
    conn.rollback()
    log({"remaining": {"view": view_left, "thumb": thumb_left}, "root": str(root),
         "free_gib": round(free_bytes(root) / 2**30, 1), "reserve_gib": a.reserve_gib})
    if a.dry_run:
        return 0
    if a.restore_columns:
        log({"result": restore_columns(conn, root, a.batch, log)})
        return 0
    t0 = time.monotonic()
    stats = run(conn, root, a.batch, a.max_rows, int(a.reserve_gib * 2**30), a.verify, log)
    stats["seconds"] = round(time.monotonic() - t0, 1)
    log({"result": stats})
    return 0 if stats["stopped"] == "done" else 3


if __name__ == "__main__":
    sys.exit(main())
