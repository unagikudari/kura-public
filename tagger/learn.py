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

The database owns execution state, retries and the registration outbox. A legacy
state file is read only for one-way recovery; new progress never depends on it. At most one pass runs per
host (flock); the lease keeps a second host off the same tag, and the API refuses a
registration whose lease or event went stale.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import ExitStack
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


# ── legacy import compatibility; new execution state is canonical PostgreSQL ──
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
def _upload_review_candidate(payload, base, authorization):
    """Stage immutable bytes before registering a production review candidate."""
    import hashlib
    import re

    sha = payload["artifact_sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError("invalid candidate artifact digest")
    key = hashlib.sha256(
        f'{payload["tag"]}@{payload["lifecycle_event_id"]}'.encode()
    ).hexdigest()[:24]
    root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    path = root / "kura-tagger" / "learning-jobs" / key / "received" / "result.json.bundle.pt"
    if path.is_symlink():
        raise ValueError("candidate artifact symlink")
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        if not 0 < size <= 512 * 1024**2:
            raise ValueError("candidate artifact size outside upload budget")
        if hashlib.file_digest(stream, "sha256").hexdigest() != sha:
            raise ValueError("candidate artifact digest mismatch")
        stream.seek(0)
        request = urllib.request.Request(
            base + "/tagger/artifact/" + sha, method="PUT", data=stream,
            headers={"Authorization": authorization, "Content-Length": str(size),
                     "Content-Type": "application/octet-stream"},
        )
        with urllib.request.urlopen(request, timeout=900) as response:
            accepted = json.loads(response.read())
    if (accepted.get("content_sha256") != sha or accepted.get("byte_size") != size
            or accepted.get("kind") != "content_head_bundle"):
        raise ValueError("candidate artifact upload receipt mismatch")


def post_registration(payload: dict, lease) -> tuple[int, dict | str]:
    from tagger.api_target import api_base
    user, token = os.environ.get("KURA_USER", ""), os.environ.get("KURA_TOKEN", "")
    if not user or not token:
        raise RuntimeError("KURA_USER / KURA_TOKEN are not set — load tagger/env")
    authorization = "Token " + base64.b64encode(f"{user}:{token}".encode()).decode()
    base = api_base()
    if (payload.get("metrics") or {}).get("production_review"):
        _upload_review_candidate(payload, base, authorization)
    body = dict(payload, lease_scope=lease.scope, fencing_token=lease.fencing_token)
    req = urllib.request.Request(
        base + "/tagger/registration-request", method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": authorization})
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
    from tagger.learning_execution import run
    return await run(tag,event_id,state,state_path,args)


def run_child(tag: str, event_id: int, timeout_s: int, args) -> dict | None:
    cmd = [args.tagger_python, "-m", "tagger.vnext_runner", "--tag", tag, "--event", str(event_id)]
    if getattr(args, "remote_config", None):
        cmd = [args.tagger_python, "-m", "tagger.remote_learning", "--tag", tag,
               "--event", str(event_id), "--config", args.remote_config,
               "--timeout-s", str(timeout_s)]
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
async def refresh_evaluations() -> bool:
    from tagger.canonical_evaluation import refresh_registered_evaluations

    async with async_session() as db:
        assessments = await refresh_registered_evaluations(db)
        await db.commit()
    log("production_evaluation", **assessments)
    return bool(assessments["unavailable"])


async def run_candidate_qualification() -> bool:
    failed = False
    trial_declaration = None
    trial_file = os.environ.get("KURA_TAGGER_WORKER_TRIAL_PLAN")
    if trial_file:
        from tagger.worker_trial_dispatch import generate_trial_declaration, run_trial_plan

        with Path(trial_file).open("rb") as source:
            raw = source.read(262145)
        if len(raw) > 262144:
            raise ValueError("tagger worker trial plan exceeds byte budget")
        state_root = (Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
                      / "kura-tagger" / "learning-jobs")
        declaration = json.loads(raw)
        if declaration.get("schema") == "tagger-automatic-worker-trials/1":
            declaration = await generate_trial_declaration(
                async_session, declaration, state_root=state_root)
        trial_declaration = declaration
        trials = await run_trial_plan(async_session, declaration,
                                      state_root=state_root,
                                      node_inventory=os.environ.get("KURA_TAGGER_TRIAL_NODE_INVENTORY"))
        unavailable = declaration.get("unavailable", [])
        log("candidate_worker_trials", results=trials, unavailable=unavailable,
            undeclared_domain=declaration.get("undeclared_domain", []),
            deferred_by_cap=declaration.get("deferred_by_cap", []))
        failed |= bool(unavailable) or any(r["state"] in ("failed", "unavailable") for r in trials)
    plan_file = os.environ.get("KURA_TAGGER_PUBLICATION_PLAN")
    if plan_file:
        from tagger.candidate_publication import generate_publication_plan, run_publication_plan

        with Path(plan_file).open("rb") as source:
            raw = source.read(262145)
        if len(raw) > 262144:
            raise ValueError("tagger publication plan exceeds byte budget")
        publication_plan = json.loads(raw)
        if publication_plan.get("schema") == "tagger-automatic-publication/1":
            if "verifier_user_id" in publication_plan:
                from tagger.candidate_publication import attest_qualified_candidates

                attested = await attest_qualified_candidates(
                    async_session, publication_plan,
                    trial_declaration=trial_declaration or {"trials": []})
                log("candidate_independent_verification", results=attested)
            publication_plan = await generate_publication_plan(
                async_session, publication_plan, trial_declaration=trial_declaration or {"trials": []})
        publications = await run_publication_plan(async_session, publication_plan)
        log("candidate_publication", results=publications)
    return failed


async def qualification_pass() -> int:
    from tagger import auto_review

    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "qualification.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("already-running", lock="qualification.lock")
            return 0
        from api.tagger_core.gt_sync import last_status

        status = last_status()
        if status is not None and not status["ok"]:
            # #161: review verdicts and publication compare a candidate's
            # training labels with the live GT; after a failed sync that GT is
            # out of date, so nothing is judged until a sync succeeds again.
            log("qualification_skipped", reason="gt_sync_failed", groups=status["failed"])
            return 1
        failed = await refresh_evaluations()
        review = await auto_review.run(review_only=True)
        log("auto_review", promoted=review.get("promoted"))
        failed |= await run_candidate_qualification()
        return int(failed)


async def measurement_pass(args) -> int:
    from tagger import reconciler

    state_path = STATE / "learn.json"
    state = load_state(state_path)
    async with async_session() as db:
        queued = (await db.execute(text(reconciler.QUEUED_TRAINING_SQL))).mappings().all()
    # Legacy pending payloads are read only for one-way recovery. Canonical
    # retry times/outbox live in PostgreSQL; do not prune or rewrite local files.
    failed = False
    measured = 0
    for r in queued:
        if measured >= args.limit:
            break
        result = await measure_one(r["tag"], r["event_id"], state, state_path, args)
        if result in ("registered", "refused", "failed", "deferred", "human", "settled"):
            measured += 1
        failed |= result == "failed"

    if getattr(args, "remote_config", None):
        # Finished jobs keep their results and records; their working copies
        # (dataset, baseline, code) are dropped here and on the worker.
        from tagger.learning_job_retention import prune_finished
        from tagger.remote_learning import job_root

        try:
            async with async_session() as db:
                pruned = await prune_finished(db, job_root("any", 1).parent,  # all jobs share this parent
                                              worker=getattr(args, "worker_profile", None))
            log("learning_jobs.pruned", **pruned)
        except Exception as exc:  # noqa: BLE001 - housekeeping never fails the pass
            log("learning_jobs.prune_failed", error=type(exc).__name__, detail=str(exc)[:200])
    return int(failed)


async def run_pass(args) -> int:
    from api.tagger_core import gt_sync
    from tagger import auto_review, reconciler

    _, gt_failed = await gt_sync.sync_and_record()
    if gt_failed:
        # #161: training, review and publication all read the GT (publication
        # and promotion compare the candidate's training labels with it), so
        # after a failed sync the whole pass waits for the next one -- the same
        # fail-closed rule the auto-cycle follows (gt_sync exits 1).
        log("gt_sync.failed", groups=gt_failed)
        log("pass_skipped", reason="gt_sync_failed")
        return 1
    failed = False
    rec = await reconciler.reconcile_once()
    log("reconcile", queued=rec.get("dispatched"), settled=rec.get("reopened_stale_training"),
        held=rec.get("backpressure_held"), failed=rec.get("failed"))

    from tagger.learning_execution import settle_consumed
    await settle_consumed(args)

    failed |= bool(await measurement_pass(args))

    if getattr(args, "remote_config", None):
        failed |= bool(await qualification_pass())
    else:
        review = await auto_review.run(undeclared_domain=args.undeclared_domain)
        log("auto_review", promoted=review.get("promoted"))
        failed |= await run_candidate_qualification()
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
    ap.add_argument("--remote-config", help="bounded offline worker profile; credentials stay here")
    ap.add_argument("--undeclared-domain", choices=("block", "always"), default="block",
                    help="a head for a tag with no domain rule: block it, or apply it to every image")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--measurement-only", action="store_true",
                      help="measure and register queued work on a separate offline worker")
    mode.add_argument("--qualification-only", action="store_true",
                    help="refresh checker evaluations, collect trials and apply publication plans")
    args = ap.parse_args()
    if args.lease_hours * 3600 <= LEASE_MARGIN_S + 600:
        ap.error("--lease-hours must leave more than an hour for the measurement")
    if args.measurement_only and not args.remote_config:
        ap.error("--measurement-only requires --remote-config")
    if args.remote_config and not args.qualification_only:
        from tagger.remote_learning import profile
        args.worker_profile = profile(args.remote_config)
    STATE.mkdir(parents=True, exist_ok=True)
    lock_names = []
    if not (args.qualification_only or args.measurement_only):
        lock_names.append("learn.lock")
    if getattr(args, "worker_profile", None):
        lock_names.append(f"learn-{args.worker_profile['host'].lower()}.lock")
    with ExitStack() as locks:
        for name in lock_names:
            lock = locks.enter_context((STATE / name).open("a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log("already-running", lock=name)
                return 0
        started = datetime.now(timezone.utc)
        if args.qualification_only:
            run = qualification_pass()
        elif args.measurement_only:
            run = measurement_pass(args)
        else:
            run = run_pass(args)
        rc = asyncio.run(run)
        log("done", seconds=round((datetime.now(timezone.utc) - started) / timedelta(seconds=1)), rc=rc)
        return rc


if __name__ == "__main__":
    sys.exit(main())
