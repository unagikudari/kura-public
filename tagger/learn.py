#!/usr/bin/env python3
# Description: one pass of the tagger learning loop — corrections → GT → queue → measure → promote
# Kind: tool
# Effects: mutating (kura DB: GT labels, lifecycle events, registrations, patches, head policies)
# Runs-on: the machine that holds the database (README "Learning from your corrections")
"""tagger/learn.py — the learning loop on one machine (docs/notes/tagger-learn-local.md).

Run by kura-tagger-learn.timer in the API's venv, with api/env and tagger/env loaded:

  1. gt_sync          human corrections → tagger_gt_label
  2. reconcile        tags that pass the gates go candidate → training (that write is
                      the queue entry)
  3. measure          the oldest queued tag whose backoff has passed: hold its
                      lifecycle lease, run tagger/vnext_runner.py in the tagger venv,
                      send the registration it prints under the lease
  4. auto_review      judge registrations, promote heads, then settle judged tags

The database decides what is done (a registration for the event); the state file
only holds backoffs and a payload whose reply was lost. At most one pass runs per
host (flock); the lease keeps a second host off the same tag, and the API refuses a
registration whose lease or event went stale.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sqlalchemy import text  # noqa: E402

from api.db.session import async_session  # noqa: E402
from api.tagger_core.transitions import (  # noqa: E402
    TransitionError, acquire_lease, record_lifecycle_event, release_lease)

STATE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "kura-tagger"
BACKOFF_FIRST_S = 30 * 60
BACKOFF_MAX_S = 24 * 3600
# The child is killed this long before the lease would expire, so it never
# outlives the lease it was started under.
LEASE_MARGIN_S = 3600


def log(event: str, **kv) -> None:
    parts = " ".join(f"{k}={v}" for k, v in kv.items())
    print(f"[learn] {event} {parts}".rstrip(), flush=True)


# ── state file (backoff and pending payloads only) ──────────────────────────
def load_state(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return {"backoff": {}, "pending": {}}
    return {"backoff": doc.get("backoff") or {}, "pending": doc.get("pending") or {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)
    dirfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)


def key_of(tag: str, event_id: int) -> str:
    return f"{tag}@{event_id}"


def backoff(state: dict, key: str, error: str, *, at_least: int = 0) -> int:
    """Record one more failure for (tag, event) and return the wait in seconds."""
    n = int(state["backoff"].get(key, {}).get("n", 0)) + 1
    wait = max(at_least, min(BACKOFF_FIRST_S * 2 ** (n - 1), BACKOFF_MAX_S))
    state["backoff"][key] = {"n": n, "next": time.time() + wait, "last": error[:300]}
    return wait


def due(state: dict, key: str, now: float) -> bool:
    return float(state["backoff"].get(key, {}).get("next", 0)) <= now


# ── environment for the child ──────────────────────────────────────────────
def child_env(environ: dict) -> dict:
    """The child reads the database directly (psycopg2); point both DSNs at the
    API's database unless they are set, and keep the rest of our environment
    (tagger/env gives it KURA_URL, KURA_USER and KURA_TOKEN)."""
    env = dict(environ)
    url = env.get("DATABASE_URL", "")
    libpq = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    if libpq.startswith("postgresql://"):
        env.setdefault("KURA_CANONICAL_DSN", libpq)
        env.setdefault("KURA_READ_DSN", libpq)
    return env


def parse_child_output(stdout: str) -> dict | None:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


# ── registration under the lease ───────────────────────────────────────────
def post_registration(payload: dict, lease) -> tuple[int, dict | str]:
    from tagger.api_target import api_base
    user, token = os.environ.get("KURA_USER", ""), os.environ.get("KURA_TOKEN", "")
    if not user or not token:
        raise RuntimeError("KURA_USER / KURA_TOKEN are not set — load tagger/env")
    body = dict(payload, lease_scope=lease.scope, fencing_token=lease.fencing_token)
    req = urllib.request.Request(
        api_base() + "/tagger/registration-request", method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Token " + base64.b64encode(f"{user}:{token}".encode()).decode()})
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:500]


# ── one tag ────────────────────────────────────────────────────────────────
async def still_queued(db, tag: str, event_id: int) -> bool:
    from tagger.reconciler import QUEUED_TRAINING_SQL
    rows = (await db.execute(text(QUEUED_TRAINING_SQL))).mappings().all()
    return any(r["tag"] == tag and r["event_id"] == event_id for r in rows)


async def latest_disposition(db, tag: str):
    return (await db.execute(text(
        "SELECT seq, disposition, actor_type FROM tagger_candidate_disposition "
        "WHERE tag_id = tagger_tag_id_of(:tag) ORDER BY seq DESC LIMIT 1"), {"tag": tag})).first()


async def defer_structural(db, tag: str, event_id: int, lease, reasons: str) -> str:
    """Write the automation defer first, then leave 'training' (a crash in
    between is finished by settle_judged_training). A human decision wins: we
    write nothing over it and only back off."""
    prev = await latest_disposition(db, tag)
    if prev is not None and prev.actor_type == "human":
        return "human"
    await db.execute(text(
        "SELECT tagger_governed_record_candidate_disposition("
        ":tag, 'defer', 'structural_precondition', :reason, 'tagger/learn.py', 'automation', "
        "now() + interval '24 hours', NULL, :prev)"),
        {"tag": tag, "reason": f"measurement of event {event_id} stopped before it began: {reasons}"[:1000],
         "prev": prev.seq if prev is not None else None})
    await db.commit()
    await record_lifecycle_event(
        db, tag=tag, from_stage="training", to_stage="candidate",
        reason=f"deferred 24 h by tagger/learn.py: {reasons}"[:500], lease=lease)
    return "deferred"


async def measure_one(tag: str, event_id: int, state: dict, state_path: Path, args) -> str:
    key = key_of(tag, event_id)
    lease_s = int(args.lease_hours * 3600)
    async with async_session() as db:
        try:
            lease = await acquire_lease(db, f"lifecycle:{tag}", lease_seconds=lease_s)
        except TransitionError as e:
            log("lease.busy", tag=tag, detail=str(e)[:160])
            return "busy"
        try:
            queued = await still_queued(db, tag, event_id)
            await db.rollback()             # no transaction stays open while measuring
            if not queued:
                log("not-queued", tag=tag, ev=event_id)
                return "moved"
            payload = state["pending"].get(key)
            if payload is None:
                outcome = await asyncio.to_thread(run_child, tag, event_id, lease_s - LEASE_MARGIN_S, args)
                kind = outcome.get("outcome") if outcome else None
                for e in (outcome or {}).get("escalations") or []:
                    log("escalation", tag=tag, code=e.get("code"), blocking=e.get("blocking"),
                        summary=e.get("summary"))
                payload = (outcome or {}).get("registration")
                if payload is None:
                    if kind == "escalated":
                        blocking = [e for e in outcome.get("escalations") or [] if e.get("blocking")]
                        unverified = [e for e in blocking if "missing_check" in (e.get("detail") or {})]
                        if blocking and not unverified:
                            reasons = "; ".join(e.get("summary", "") for e in blocking)
                            done = await defer_structural(db, tag, event_id, lease, reasons)
                            if done == "human":
                                wait = backoff(state, key, "structural precondition; a human decision stands",
                                               at_least=BACKOFF_MAX_S)
                                save_state(state_path, state)
                                log("held.human", tag=tag, ev=event_id, wait_s=wait, reasons=reasons)
                            else:
                                state["backoff"].pop(key, None)
                                save_state(state_path, state)
                                log("deferred", tag=tag, ev=event_id, reasons=reasons)
                            return done
                    if kind and kind.endswith("(gt-unchanged)"):
                        # The result already registered for an earlier event stands.
                        await record_lifecycle_event(
                            db, tag=tag, from_stage="training", to_stage="candidate",
                            reason="settled: GT unchanged since the last registration", lease=lease)
                        state["backoff"].pop(key, None)
                        save_state(state_path, state)
                        log("settled.gt-unchanged", tag=tag, ev=event_id)
                        return "settled"
                    if kind == "noop":
                        return "moved"
                    wait = backoff(state, key, f"no result ({kind or 'child failed'})")
                    save_state(state_path, state)
                    log("failed", tag=tag, ev=event_id, outcome=kind, retry_in_s=wait)
                    return "failed"
                given = payload.get("lifecycle_event_id")
                if given not in (None, 0, event_id):
                    log("stale-result", tag=tag, ev=event_id, result_event=given)
                    return "moved"
                payload["lifecycle_event_id"] = event_id
                state["pending"][key] = payload
                save_state(state_path, state)
            try:
                status, reply = await asyncio.to_thread(post_registration, payload, lease)
            except Exception as e:  # noqa: BLE001 — the API is down or unreachable
                wait = backoff(state, key, f"registration not sent: {type(e).__name__}: {e}")
                save_state(state_path, state)
                log("registration.unsent", tag=tag, ev=event_id, error=type(e).__name__,
                    retry_in_s=wait)
                return "failed"
            if status == 200:
                state["pending"].pop(key, None)
                state["backoff"].pop(key, None)
                save_state(state_path, state)
                log("registered", tag=tag, ev=event_id, id=reply.get("id"))
                return "registered"
            if status == 409:
                state["pending"].pop(key, None)
                save_state(state_path, state)
                log("refused", tag=tag, ev=event_id, detail=reply)
                return "refused"
            wait = backoff(state, key, f"registration HTTP {status}: {reply}")
            save_state(state_path, state)
            log("registration.failed", tag=tag, ev=event_id, status=status, retry_in_s=wait)
            return "failed"
        finally:
            try:
                await release_lease(db, lease)
            except TransitionError:
                pass


def run_child(tag: str, event_id: int, timeout_s: int, args) -> dict | None:
    cmd = [args.tagger_python, "-m", "tagger.vnext_runner", "--tag", tag, "--event", str(event_id)]
    log("measure.start", tag=tag, ev=event_id, timeout_s=timeout_s)
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=REPO, env=child_env(dict(os.environ)), stdout=subprocess.PIPE,
                           text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        log("measure.timeout", tag=tag, ev=event_id, after_s=timeout_s)
        return None
    except OSError as e:
        log("measure.cannot-start", tag=tag, error=str(e)[:200])
        return None
    out = parse_child_output(p.stdout)
    log("measure.end", tag=tag, ev=event_id, rc=p.returncode,
        seconds=round(time.time() - t0), outcome=(out or {}).get("outcome"))
    return out


# ── the pass ───────────────────────────────────────────────────────────────
async def run_pass(args) -> int:
    from api.tagger_core import gt_sync
    from tagger import auto_review, reconciler

    failed = False
    summary = await gt_sync.run_sync()
    if any("error" in v for v in summary.values()):
        failed = True
        log("gt_sync.failed", groups=[k for k, v in summary.items() if "error" in v])
    rec = await reconciler.run_reconcile()
    log("reconcile", queued=rec.get("dispatched"), settled=rec.get("reopened_stale_training"),
        held=rec.get("backpressure_held"), failed=rec.get("failed"))

    state_path = STATE / "learn.json"
    state = load_state(state_path)
    now = time.time()
    async with async_session() as db:
        queued = (await db.execute(text(reconciler.QUEUED_TRAINING_SQL))).mappings().all()
    live = {key_of(r["tag"], r["event_id"]) for r in queued}
    for section in ("backoff", "pending"):          # forget events that left the queue
        for k in [k for k in state[section] if k not in live]:
            state[section].pop(k)
    save_state(state_path, state)
    measured = 0
    for r in queued:
        if measured >= args.limit:
            break
        key = key_of(r["tag"], r["event_id"])
        if not due(state, key, now):            # pending payloads wait out their backoff too
            continue
        result = await measure_one(r["tag"], r["event_id"], state, state_path, args)
        if result in ("registered", "refused", "failed", "deferred", "human", "settled"):
            measured += 1
        failed |= result == "failed"

    review = await auto_review.run(undeclared_domain=args.undeclared_domain)
    log("auto_review", promoted=review.get("promoted"))
    async with async_session() as db:
        settled = await reconciler.settle_judged_training(db)
    if settled:
        log("settled", tags=settled)
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=1, help="tags measured per pass (default 1)")
    ap.add_argument("--lease-hours", type=float, default=8.0,
                    help="how long one measurement may take, plus an hour (default 8)")
    ap.add_argument("--tagger-python",
                    default=str(Path.home() / ".local/share/kura-tagger/venv/bin/python"),
                    help="the tagger venv's python (torch, timm)")
    ap.add_argument("--undeclared-domain", choices=("block", "always"), default="block",
                    help="a head for a tag with no domain rule: block it, or apply it to every image")
    args = ap.parse_args()
    if args.lease_hours * 3600 <= LEASE_MARGIN_S + 600:
        ap.error("--lease-hours must leave more than an hour for the measurement")
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "learn.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("already-running")
            return 0
        started = datetime.now(timezone.utc)
        rc = asyncio.run(run_pass(args))
        log("done", seconds=round((datetime.now(timezone.utc) - started) / timedelta(seconds=1)), rc=rc)
        return rc


if __name__ == "__main__":
    sys.exit(main())
