#!/usr/bin/env python3
"""kura#50 Phase 2 executor — batched media NULL-out on the replica, gate-first.

RUNS ON THE REPLICA (trainer), dispatched to its lane. Never on the authority.

The kura#49 lesson, mechanized: every batch begins by re-verifying the gate
artifacts (`media_replication_gate.py verify-artifacts`) plus a live
replication-health check. A missing/stale/tampered artifact or a sick
subscription stops the run between batches — the operator cannot argue with it,
only refresh the evidence. The required artifact set is the plan's R1-R3 output:

    census.json attestation.json free-space.json
    topology-publisher.json topology-subscriber.json column-list.json
    rehydration.json    (the tested rollback path — produced in Phase 1)
    user-approval.json  (explicit PRE-approval of the irreversible step,
                         recorded before the first batch; kura#49 made the
                         retrospective version of this a blocking finding)

Resumable: rows are processed in post_id order above --resume-after; each batch
commits independently. VACUUM/pg_repack is NOT invoked here — reclaim of dead
space is its own gated step (see the plan's R5 rev2: pg_repack chosen, VACUUM
FULL fallback with a declared window).
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

GATE = str(pathlib.Path(__file__).resolve().parent / "media_replication_gate.py")


def _gate(args: list[str]) -> bool:
    return subprocess.run([sys.executable, GATE, *args]).returncode == 0


def _psql(dsn: str, q: str) -> str:
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-Atc", q],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"psql failed, halting: {r.stderr.strip()}")
    return r.stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True, help="replica DSN (local)")
    ap.add_argument("--artifacts", nargs="+", required=True,
                    help="kind=path pairs (must include user-approval=...); "
                         "verified before EVERY batch")
    ap.add_argument("--approval-scope", required=True,
                    help="the named operation the user-approval must bind to, "
                         "e.g. 'kura50-phase2-replica-media-null'")
    ap.add_argument("--max-artifact-age-days", type=float, default=7.0)
    ap.add_argument("--batch-size", type=int, default=5000)
    ap.add_argument("--resume-after", type=int, default=0)
    ap.add_argument("--max-batches", type=int, default=0, help="0 = run to completion")
    a = ap.parse_args()

    kinds = {s.split("=", 1)[0] for s in a.artifacts if "=" in s}
    if "user-approval" not in kinds:
        print("refusing: --artifacts must include user-approval=<path> — the "
              "irreversible step requires explicit PRE-approval (kura#49)", file=sys.stderr)
        return 2
    done = 0
    last = a.resume_after
    while True:
        if not _gate(["verify-artifacts", "--require", *a.artifacts,
                      "--max-age-days", str(a.max_artifact_age_days),
                      "--expect-approval-scope", a.approval_scope]):
            print(f"gate refused before batch {done + 1}; resume with "
                  f"--resume-after {last} once the evidence is refreshed", file=sys.stderr)
            return 2
        if not _gate(["replication-health", "--dsn", a.dsn, "--out", "/tmp/mrr-health.json"]):
            print(f"replication unhealthy before batch {done + 1}; halting at {last}",
                  file=sys.stderr)
            return 2
        row = _psql(a.dsn,
                    f"WITH batch AS (SELECT id FROM post WHERE id > {last} "
                    f"AND (data IS NOT NULL OR thumbnail IS NOT NULL) "
                    f"ORDER BY id LIMIT {a.batch_size}), "
                    f"upd AS (UPDATE post p SET data = NULL, thumbnail = NULL "
                    f"FROM batch b WHERE p.id = b.id RETURNING p.id) "
                    f"SELECT max(id), count(*) FROM upd HAVING count(*) > 0")
        if not row:
            print(f"complete: no media bytes remain above post_id {a.resume_after}; "
                  f"{done} batch(es) this run")
            return 0
        last_s, n_s = row.split("|")
        last, n = int(last_s), int(n_s)
        done += 1
        print(f"batch {done}: {n} rows nulled through post_id {last}")
        if a.max_batches and done >= a.max_batches:
            print(f"stopping at --max-batches {a.max_batches}; resume with --resume-after {last}")
            return 0


if __name__ == "__main__":
    sys.exit(main())
