#!/usr/bin/env python3
"""Night-time cutover: the legacy post.data / post.thumbnail columns stop holding bytes.

docs/notes/rendition-store-ideal.md, step 5.

    rendition_cutover.py check [--verify-all]   report: every legacy column has a rendition,
                                                sampled hashes match (read-only)
    rendition_cutover.py cutover                the one controlled step (below)
    rendition_cutover.py auto                   run on a timer: does nothing until ready, then
                                                cutover → vacuum-full once (state in --state-dir)
    rendition_cutover.py vacuum-full            give the freed TOAST space back to the filesystem
                                                (ACCESS EXCLUSIVE on post: waits until kura is idle —
                                                no claimed import task, few other active queries)

`cutover` drops a post's legacy bytes only when, at that moment and under a
row lock, all of these hold for every non-NULL column of that post:
  - a rendition is attached and its bytes hash equals the column's bytes;
  - the rendition file exists and hashes to its name;
  - that file is listed, with that digest, in the media manifest of the last
    backup that was independently restored and published (job receipt →
    snapshot manifest + restore proof, checked with the publication rule).
Membership in the backup is decided by the manifest, not by timestamps: a
rendition committed after the backup's DB snapshot is simply not listed.
The transition trigger stays: only this step's own UPDATE runs with
kura.rendition_cutover=on, which the trigger honours. Any other write — including
one to a post this step left in place — still detaches the stale rendition.
Rows that fail any check keep their bytes and are reported by reason.

The publication keeps the two columns: once NULL they carry nothing. Dropping
the columns is a later migration with replica DDL. Replica-side reclaim:
scripts/media_reclaim_run.py.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2  # noqa: E402

from api.services import renditions  # noqa: E402

BACKUP_RECEIPT = Path.home() / ".local/state/kura-library-backup/job.json"

MISSING = """
SELECT count(*) FILTER (WHERE p.data IS NOT NULL AND v.post_id IS NULL),
       count(*) FILTER (WHERE p.thumbnail IS NOT NULL AND t.post_id IS NULL), count(*)
  FROM post p
  LEFT JOIN post_rendition v ON v.post_id = p.id AND v.kind = 'view'
  LEFT JOIN post_rendition t ON t.post_id = p.id AND t.kind = 'thumb'
 WHERE p.data IS NOT NULL OR p.thumbnail IS NOT NULL
"""
SAMPLE_POSTS = "SELECT post_id FROM post_rendition ORDER BY random() LIMIT %(n)s"
MISMATCH = """
SELECT count(*) FROM post p
  JOIN post_rendition r ON r.post_id = p.id
 WHERE (%(all)s OR p.id = ANY(%(ids)s))
   AND ((r.kind = 'view' AND p.data IS NOT NULL AND sha256(p.data) <> r.sha256)
     OR (r.kind = 'thumb' AND p.thumbnail IS NOT NULL AND sha256(p.thumbnail) <> r.sha256))
"""
OBJECTS = "SELECT sha256, mime, byte_size FROM rendition_object"
NEXT = """
SELECT p.id FROM post p
 WHERE p.id > %(after)s AND (p.data IS NOT NULL OR p.thumbnail IS NOT NULL)
 ORDER BY p.id LIMIT %(limit)s
"""
LOCK_BATCH = """
SELECT p.id, p.data, p.thumbnail, v.sha256, vo.mime, t.sha256, tob.mime
  FROM post p
  LEFT JOIN post_rendition v ON v.post_id = p.id AND v.kind = 'view'
  LEFT JOIN rendition_object vo ON vo.sha256 = v.sha256
  LEFT JOIN post_rendition t ON t.post_id = p.id AND t.kind = 'thumb'
  LEFT JOIN rendition_object tob ON tob.sha256 = t.sha256
 WHERE p.id = ANY(%(ids)s) ORDER BY p.id FOR UPDATE OF p
"""
INACTIVE_SLOTS = "SELECT coalesce(array_agg(slot_name::text), '{}') FROM pg_replication_slots WHERE slot_type = 'logical' AND NOT active"
SLOT_LAG = """
SELECT coalesce(max(pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)), 0)
  FROM pg_replication_slots WHERE slot_type = 'logical' AND active
"""


def log(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def one(conn, sql, args=None):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        row = cur.fetchone()
    conn.rollback()
    return row


# ── backup evidence ───────────────────────────────────────────────────────────

def proof_matches(manifest: dict, proof: dict) -> bool:
    """The rule scripts/library_recovery_schedule.py applies before publishing."""
    return (proof.get("format") == "kura-library-restore-proof-v1"
            and proof.get("database_restore_verified") is True
            and proof.get("reopened_after_clean_shutdown") is True
            and proof.get("database_sha256") == manifest.get("database_sha256")
            and proof.get("tables_verified") == len(manifest["tables"])
            and proof.get("constraints_verified") == len(manifest["constraints"])
            and proof.get("media_files_verified") == len(manifest["media"]))


def fetch_snapshot(host: str, directory: str) -> tuple[dict, dict]:
    with tempfile.TemporaryDirectory(prefix="kura-cutover-") as tmp:
        for name in ("manifest.json", "restore-proof.json"):
            subprocess.run(["scp", "-q", "-o", "BatchMode=yes", f"{host}:{directory}/{name}", f"{tmp}/{name}"],
                           check=True, timeout=1800)
        return (json.loads(Path(tmp, "manifest.json").read_text()),
                json.loads(Path(tmp, "restore-proof.json").read_text()))


def read_receipt(receipt: Path) -> dict:
    try:
        return json.loads(receipt.read_text())
    except (OSError, ValueError):
        return {}


def backed_up_renditions(receipt: Path, host: str, root: str, fetch=fetch_snapshot,
                         job: dict | None = None) -> tuple[str, set[bytes]] | None:
    """(snapshot, rendition sha256s) of the last verified, published backup, or None.

    Pass `job` to judge a receipt already read: a caller that decided on one
    snapshot must not have the evidence read from a newer one under it."""
    job = read_receipt(receipt) if job is None else job
    if job.get("format") != "kura-library-backup-job-v1" or job.get("state") != "verified" or not job.get("snapshot"):
        return None
    manifest, proof = fetch(host, f"{root.rstrip('/')}/{job['snapshot']}")
    if not proof_matches(manifest, proof):
        return None
    shas = set()
    for item in manifest["media"]:
        path = Path(item["path"])
        if path.parts[0] == "rendition" and path.stem == item.get("sha256"):
            shas.add(bytes.fromhex(path.stem))
    return job["snapshot"], shas


# ── steps ─────────────────────────────────────────────────────────────────────

def check(conn, verify_all: bool, sample: int) -> int:
    view_missing, thumb_missing, total = one(conn, MISSING)
    with conn.cursor() as cur:
        cur.execute(SAMPLE_POSTS, {"n": sample})
        ids = [r[0] for r in cur.fetchall()]
    (mismatch,) = one(conn, MISMATCH, {"all": verify_all, "ids": ids})
    with conn.cursor() as cur:
        cur.execute(OBJECTS)
        objects = cur.fetchall()
    conn.rollback()
    chosen = objects if verify_all else random.Random(0).sample(objects, min(sample, len(objects)))
    bad = []
    for sha, mime, size in chosen:
        path = renditions.path_for(bytes(sha), mime)
        if not path.exists() or path.stat().st_size != size or not renditions.verify(path, bytes(sha)):
            bad.append(str(path))
    ok = view_missing == 0 and thumb_missing == 0 and mismatch == 0 and not bad
    log({"check": "pass" if ok else "fail", "legacy_rows": total, "missing_view": view_missing,
         "missing_thumb": thumb_missing, "mismatch": mismatch, "files_checked": len(chosen),
         "files_bad": bad[:20]})
    return 0 if ok else 1


def _column_ok(blob, sha, mime, backed_up: set[bytes]) -> str | None:
    """None when this column may be dropped, else the reason it may not."""
    if blob is None:
        return None
    if sha is None:
        return "no_rendition"
    sha = bytes(sha)
    if hashlib.sha256(bytes(blob)).digest() != sha:
        return "column_differs"
    if sha not in backed_up:
        return "not_in_verified_backup"
    path = renditions.path_for(sha, mime)
    if not path.exists() or not renditions.verify(path, sha):
        return "file_missing_or_corrupt"
    return None


def cutover(conn, batch: int, max_lag: int, evidence) -> int:
    if evidence is None:
        log({"refused": "no verified, published backup whose manifest can be read"})
        return 2
    snapshot, backed_up = evidence
    # A stopped or disabled subscriber would let every NULLed row pile up as
    # retained WAL with nobody consuming it; refuse rather than fill the disk.
    (inactive,) = one(conn, INACTIVE_SLOTS)
    if inactive:
        log({"refused": "logical replication slot not active", "slots": inactive})
        return 2
    log({"backup": snapshot, "renditions_in_backup": len(backed_up)})
    after, cleared, kept = 0, 0, collections.Counter()
    while True:
        while True:
            # A replica may stop after the initial check or during a lag wait.
            # Never treat its disappearance from the active-slot lag as zero.
            (inactive,) = one(conn, INACTIVE_SLOTS)
            if inactive:
                log({"refused": "logical replication slot not active", "slots": inactive})
                return 2
            lag = int(one(conn, SLOT_LAG)[0])
            if lag <= max_lag:
                break
            log({"waiting": "replication slot lag", "lag_bytes": lag})
            time.sleep(10)
        with conn.cursor() as cur:
            cur.execute(NEXT, {"after": after, "limit": batch})
            ids = [r[0] for r in cur.fetchall()]
        conn.rollback()
        if not ids:
            break
        with conn, conn.cursor() as cur:
            cur.execute(LOCK_BATCH, {"ids": ids})
            ok = []
            for pid, data, thumb, vsha, vmime, tsha, tmime in cur.fetchall():
                reasons = [r for r in (_column_ok(data, vsha, vmime, backed_up),
                                       _column_ok(thumb, tsha, tmime, backed_up)) if r]
                if reasons:
                    kept.update(reasons)
                else:
                    ok.append(pid)
            if ok:
                # Only this verified UPDATE bypasses the transition trigger.
                cur.execute("SET LOCAL kura.rendition_cutover = 'on'")
                cur.execute("UPDATE post SET data = NULL, thumbnail = NULL WHERE id = ANY(%s)", (ok,))
                cleared += cur.rowcount
        after = ids[-1]
        log({"cleared": cleared, "kept": dict(kept), "after": after})
    log({"result": {"cleared": cleared, "kept": dict(kept)}})
    return 0 if not kept else 1


ACTIVE_QUERIES = """
SELECT count(*) FROM pg_stat_activity
 WHERE state = 'active' AND pid <> pg_backend_pid() AND datname = current_database()
"""
# Work an extension has claimed: any public table named *_task with a status column
# (an extension's work queue). None in a core-only install.
CLAIMED_TASKS = """
SELECT COALESCE(sum((xpath('/row/n/text()', query_to_xml(
         format('SELECT count(*) AS n FROM public.%I WHERE status = ''claimed''', table_name),
         false, true, '')))[1]::text::int), 0)
  FROM information_schema.columns
 WHERE table_schema = 'public' AND column_name = 'status' AND table_name LIKE '%\\_task'
"""


def wait_until_idle(conn, max_active: int, quiet_seconds: float, poll: float, deadline: float) -> bool:
    """True once kura has stayed idle for `quiet_seconds` of wall time; False at the deadline.

    Imports run heaviest at night (user, 2026-10-24), so idleness is measured, not
    assumed from the clock. The exclusive lock would stall every writer meanwhile.
    """
    quiet_since = None
    while time.monotonic() < deadline:
        claimed, active = one(conn, CLAIMED_TASKS)[0], one(conn, ACTIVE_QUERIES)[0]
        now = time.monotonic()
        if claimed == 0 and active <= max_active:
            quiet_since = quiet_since if quiet_since is not None else now
            if now - quiet_since >= quiet_seconds:
                return True
        else:
            quiet_since = None
        log({"waiting": "quiet" if quiet_since is not None else "kura busy",
             "claimed_tasks": claimed, "active_queries": active})
        time.sleep(poll)
    return False


def vacuum_full(conn, max_active: int = 2, quiet_seconds: float = 60.0, poll: float = 10.0,
                max_wait: float = 6 * 3600, lock_timeout: str = "2s") -> int:
    """VACUUM FULL post once idle. The lock is requested with a short timeout: a
    queued ACCESS EXCLUSIVE request blocks every reader behind it, so if it cannot
    be had at once we go back to waiting for idleness instead of holding the queue."""
    if one(conn, "SELECT count(*) FROM post WHERE data IS NOT NULL OR thumbnail IS NOT NULL")[0]:
        log({"refused": "rows still hold bytes — finish cutover first"})
        return 2
    deadline = time.monotonic() + max_wait
    before = one(conn, "SELECT pg_total_relation_size('post')")[0]
    while True:
        if not wait_until_idle(conn, max_active, quiet_seconds, poll, deadline):
            log({"refused": "kura never stayed idle long enough within the wait limit"})
            return 3
        conn.autocommit = True
        t0 = time.monotonic()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET lock_timeout = '{lock_timeout}'")
                cur.execute("VACUUM (FULL, ANALYZE) post")
        except psycopg2.errors.LockNotAvailable:
            log({"waiting": "lock not available at once; back to idle wait"})
            continue
        finally:
            conn.autocommit = False
        break
    after = one(conn, "SELECT pg_total_relation_size('post')")[0]
    log({"vacuum_full": {"seconds": round(time.monotonic() - t0, 1), "bytes_before": before,
                         "bytes_after": after}})
    return 0


# Legacy bytes that still lack a rendition (the backfill's own measure of "done").
BACKFILL_REMAINING = """
SELECT count(*) FROM post p
 WHERE (p.data IS NOT NULL AND NOT EXISTS
          (SELECT 1 FROM post_rendition r WHERE r.post_id = p.id AND r.kind = 'view'))
    OR (p.thumbnail IS NOT NULL AND NOT EXISTS
          (SELECT 1 FROM post_rendition r WHERE r.post_id = p.id AND r.kind = 'thumb'))
"""


AUTO_LOCK = 98432110   # one auto tick at a time across processes (session advisory lock)


def _write_json(path: Path, obj) -> None:
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with open(tmp, "w") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    renditions._fsync_dir(path.parent)


def auto(conn, state_dir: Path, receipt: Path, host: str, root: str, batch: int, max_lag: int,
         fetch=None, vacuum=None) -> int:
    """One timer tick. Not ready → log why and exit 0. Ready → cutover, then VACUUM FULL, once.

    Ready means: the backfill has no legacy bytes left without a rendition, and a
    backup that STARTED after that moment was restored and verified. The first
    tick that sees the backfill complete records the moment; if legacy bytes
    without a rendition reappear, the record is dropped so the next completion
    is measured again. One receipt read decides the whole tick (time check,
    evidence, done marker). Ticks are serialized by a session advisory lock.
    """
    if not one(conn, "SELECT pg_try_advisory_lock(%s)", (AUTO_LOCK,))[0]:
        log({"auto": "another tick is running"})
        return 0
    try:
        return _auto_locked(conn, state_dir, receipt, host, root, batch, max_lag, fetch, vacuum)
    finally:
        one(conn, "SELECT pg_advisory_unlock(%s)", (AUTO_LOCK,))


def _auto_locked(conn, state_dir, receipt, host, root, batch, max_lag, fetch, vacuum) -> int:
    state_dir.mkdir(parents=True, exist_ok=True)
    done, complete = state_dir / "cutover-done.json", state_dir / "backfill-complete.json"
    if done.exists():
        log({"auto": "already done", "state": json.loads(done.read_text())})
        return 0
    (remaining,) = one(conn, BACKFILL_REMAINING)
    if remaining:
        complete.unlink(missing_ok=True)   # completion must be observed again
        log({"auto": "not ready", "why": "backfill remaining", "posts": remaining})
        return 0
    if not complete.exists():
        (now,) = one(conn, "SELECT now()")
        _write_json(complete, {"observed_at": now.isoformat()})
    complete_at = json.loads(complete.read_text())["observed_at"]
    job = read_receipt(receipt)
    if job.get("format") != "kura-library-backup-job-v1" or job.get("state") != "verified" \
            or not job.get("started_at") or not job.get("snapshot"):
        log({"auto": "not ready", "why": "no verified backup receipt", "receipt_state": job.get("state")})
        return 0
    (newer,) = one(conn, "SELECT %s::timestamptz > %s::timestamptz", (job["started_at"], complete_at))
    if not newer:
        log({"auto": "not ready", "why": "verified backup predates backfill completion",
             "backup_started": job["started_at"], "backfill_complete": complete_at})
        return 0
    evidence = backed_up_renditions(receipt, host, root, job=job, **({"fetch": fetch} if fetch else {}))
    if evidence is None or evidence[0] != job["snapshot"]:
        log({"auto": "not ready", "why": "backup evidence did not verify", "snapshot": job["snapshot"]})
        return 0
    rc = cutover(conn, batch, max_lag, evidence)
    if rc != 0:
        log({"auto": "cutover incomplete; will retry next tick", "rc": rc})
        return rc
    rc = (vacuum or vacuum_full)(conn)
    if rc == 0:
        _write_json(done, {"backup": job["snapshot"], "backfill_complete": complete_at})
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("step", choices=["check", "cutover", "auto", "vacuum-full"])
    ap.add_argument("--verify-all", action="store_true", help="check: hash every rendition file (slow)")
    ap.add_argument("--sample", type=int, default=2000)
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--max-slot-lag-mib", type=int, default=512)
    ap.add_argument("--backup-receipt", type=Path, default=BACKUP_RECEIPT)
    ap.add_argument("--backup-host", default="trainer")
    ap.add_argument("--backup-root", default="/data/kura-recovery")
    ap.add_argument("--max-active-queries", type=int, default=2, help="vacuum-full: idle threshold")
    ap.add_argument("--max-wait-hours", type=float, default=6.0, help="vacuum-full: give up after this")
    ap.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/kura-rendition-cutover")
    a = ap.parse_args(argv)
    conn = psycopg2.connect(os.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://"))
    if a.step == "check":
        return check(conn, a.verify_all, a.sample)
    if a.step == "cutover":
        evidence = backed_up_renditions(a.backup_receipt, a.backup_host, a.backup_root)
        return cutover(conn, a.batch, a.max_slot_lag_mib * 2**20, evidence)
    if a.step == "auto":
        return auto(conn, a.state_dir, a.backup_receipt, a.backup_host, a.backup_root, a.batch,
                    a.max_slot_lag_mib * 2**20,
                    vacuum=lambda c: vacuum_full(c, a.max_active_queries, max_wait=a.max_wait_hours * 3600))
    return vacuum_full(conn, a.max_active_queries, max_wait=a.max_wait_hours * 3600)


if __name__ == "__main__":
    sys.exit(main())
