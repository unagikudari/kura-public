"""Drop the bulky working copies of finished learning jobs (owner 2026-11-24:
「成果の方に価値があるのなら…終わったら捨てていったら無駄がない」).

A job directory (primary ``learning-jobs/<key>``, worker ``offline-jobs/<key>``)
holds three kinds of files:

* working copies, needed only while the job runs: ``dataset/`` (the frozen
  images, ~3 GB), ``baseline.pt`` (the incumbent model) and ``code/``;
* the result: ``received/`` on primary (measurement, review frame, worker-trial
  images, candidate bundle) and ``result/`` on the worker (the registration's
  ``artifact_ref`` points there);
* small records: ``inputs.json``, ``snapshot.json``, ``job.json``, manifests.

Once the job's lifecycle event reached a terminal learning-execution state
(``registered``, ``refused``, ``settled``) nothing reads the working copies
again: tagger/learning_execution.py returns before dispatching such an event.
They are removed then; results and records stay. ``job.json`` keeps the
dataset/code/baseline sha256, and the dataset can be re-exported from
``snapshot.json``, so provenance survives. A ``pruned.json`` records what went.
"""
import fcntl
import json
import re
import shutil
from pathlib import Path

TERMINAL = ("registered", "refused", "settled")
WORKING = ("dataset", "baseline.pt", "code")
KEY = re.compile(r"[0-9a-f]{24}")


def job_key(tag, event):
    import hashlib

    return hashlib.sha256(f"{tag}@{event}".encode()).hexdigest()[:24]


def identity(job_dir):
    """(tag, event) of a job directory, from its inputs; None when unknown or
    when the directory name is not that job's key (a copied or renamed dir)."""
    try:
        doc = json.loads((Path(job_dir) / "inputs.json").read_text())
        tag, event = doc["tag"], doc["event"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not (isinstance(tag, str) and type(event) is int and event > 0):
        return None
    return (tag, event) if Path(job_dir).name == job_key(tag, event) else None


def _size(path):
    if path.is_symlink() or path.is_file():
        return path.lstat().st_size
    return sum(p.lstat().st_size for p in path.rglob("*") if not p.is_symlink() and p.is_file())


def prune_dir(job_dir, lock_name="controller.lock"):
    """Remove the working copies of one job unless a controller holds its lock.

    Returns the bytes freed, or None when the job is busy."""
    job_dir = Path(job_dir)
    lock_path = job_dir / lock_name
    with open(lock_path, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        removed, freed = [], 0
        for name in WORKING + tuple(p.name for p in job_dir.glob(".code-staging-*")):
            path = job_dir / name
            if not (path.exists() or path.is_symlink()):
                continue
            size = _size(path)
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append(name)
            freed += size
        if removed:
            record = job_dir / "pruned.json"
            try:
                previous = json.loads(record.read_text())
            except (OSError, ValueError):
                previous = {}
            previous = {"removed": previous.get("removed", []), "bytes": previous.get("bytes", 0)}
            record.write_text(json.dumps({
                "schema": "tagger-learning-job-pruned/1",
                "removed": sorted(set(previous["removed"]) | set(removed)),
                "bytes": previous["bytes"] + freed,
            }, sort_keys=True))
        return freed


async def terminal_events(db, events):
    from sqlalchemy import text

    if not events:
        return set()
    rows = await db.execute(text(
        "SELECT event_id FROM tagger_learning_execution WHERE event_id = ANY(:ids) AND state = ANY(:terminal)"
    ), {"ids": sorted(events), "terminal": list(TERMINAL)})
    return {row[0] for row in rows}


REMOTE_SCRIPT = r"""
import fcntl, json, shutil, sys
from pathlib import Path
root = Path(sys.argv[1]); freed = 0; done = []
failed = []
for key in sys.argv[2:]:
    job = root / key
    if not job.is_dir():
        continue
    try:
      with open(job / "job.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            continue
        for name in ("dataset", "baseline.pt", "code"):
            path = job / name
            if path.is_dir() and not path.is_symlink():
                freed += sum(p.lstat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                freed += path.lstat().st_size
                path.unlink()
    except OSError as exc:
        failed.append([key, type(exc).__name__, str(exc)[:200]])
        continue
    done.append(key)
print(json.dumps({"jobs": len(done), "bytes": freed, "failed": failed}))
"""


def prune_worker(worker, keys):
    """Same pruning on the worker node, over ssh, for jobs primary found terminal."""
    from .remote_learning import ssh

    keys = sorted(k for k in keys if KEY.fullmatch(k))
    if not keys:
        return {"jobs": 0, "bytes": 0}
    return json.loads(ssh(worker, [worker["python"], "-c", REMOTE_SCRIPT, worker["root"], *keys], timeout=300))


async def prune_finished(db, root, worker=None):
    """Prune every finished job under ``root`` (primary) and, given a worker profile,
    the same jobs on that worker. Unknown or still-running jobs are left alone."""
    root = Path(root)
    jobs = {}
    if root.is_dir():
        for job_dir in root.iterdir():
            if job_dir.is_dir() and KEY.fullmatch(job_dir.name):
                found = identity(job_dir)
                if found:
                    jobs[job_dir.name] = (job_dir, found[1])
    finished = await terminal_events(db, {event for _, event in jobs.values()})
    summary = {"jobs": 0, "bytes": 0, "busy": 0, "failed": [], "worker": None}
    done = []
    for key, (job_dir, event) in sorted(jobs.items()):
        if event not in finished:
            continue
        try:
            freed = prune_dir(job_dir)
        except OSError as exc:  # one bad directory (e.g. an unmounted target) never stops the rest
            summary["failed"].append([key, type(exc).__name__, str(exc)[:200]])
            continue
        if freed is None:
            summary["busy"] += 1
            continue
        done.append(key)
        if freed:
            summary["jobs"] += 1
            summary["bytes"] += freed
    if worker is not None and done:
        try:
            summary["worker"] = prune_worker(worker, done)
        except Exception as exc:  # noqa: BLE001 - the worker may be offline
            summary["worker"] = {"error": type(exc).__name__, "detail": str(exc)[:200]}
    return summary
