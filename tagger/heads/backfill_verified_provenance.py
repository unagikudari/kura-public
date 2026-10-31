#!/usr/bin/env python3
# Description: Convert the pre-#64 verified_log to the schema-2 ledger, stamping provenance.
# Kind: migration
# Effects: mutating with --apply (rewrites verified_log.jsonl after a backup); dry-run otherwise
# Requires: TAGGER_HEADS_DATA (applied_log.jsonl + verified_log.jsonl); runs on the heads node
# Version: 1.0
"""Give every verified_log row the provenance it never had (kura#64).

The old rows could not say whether a judgment was human or machine. The only
evidence that survives to reconstruct it is the machine applied-log: a
(concept, post) that ALSO appears there was applied by a head, so the verified row
duplicating it is a machine guess, not a human confirmation — it is demoted to
`machine_applied` and stops being curated. A row that does NOT appear in the
applied-log is independent of any head: the hand-imports and seed pools kura#64
kept. Those become human rows under the actor you name.

This is the one place that decision lives; the row schema and the pos/neg-vs-source
consistency are enforced by verified_ledger (imported, not re-implemented here).

Default is DRY-RUN: it prints the split per concept so you can check it against
what you know (the labia tags should come out ~100% machine; the phimosis /
small_penis hand-sets should come out human) BEFORE anything is written. `--apply`
backs the file up and rewrites it. Already-migrated (schema-2) rows are left as
they are, so re-running is safe.

    TAGGER_HEADS_DATA=~/tagger-heads python3 backfill_verified_provenance.py --actor owner          # dry-run
    TAGGER_HEADS_DATA=~/tagger-heads python3 backfill_verified_provenance.py --actor owner --apply
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tagger.heads import verified_ledger as vl  # noqa: E402


def _data_dir() -> Path:
    d = os.environ.get("TAGGER_HEADS_DATA")
    if not d:
        raise SystemExit("TAGGER_HEADS_DATA is not set — it names the heads data dir "
                         "(applied_log.jsonl + verified_log.jsonl). Set it and re-run.")
    return Path(d).expanduser()


def load_applied(path: Path) -> set[tuple[str, int]]:
    applied: set[tuple[str, int]] = set()
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                applied.add((d["c"], int(d["pid"])))
            except Exception:
                pass
    return applied


def convert(rows: list[dict], applied: set[tuple[str, int]], actor: str):
    """(new_rows, per-concept counts). Legacy rows are migrated; schema-2 rows pass
    through untouched so the migration is idempotent."""
    out: list[dict] = []
    counts: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in rows:
        if isinstance(r, dict) and r.get("schema") == vl.SCHEMA:
            out.append(r)                                  # already migrated
            counts[r["concept"]]["already"] += 1
            continue
        concept, pid = r["c"], int(r["pid"])
        machine = (concept, pid) in applied
        new = vl.migrate_legacy(r, machine_applied=machine, actor_for_human=actor)
        out.append(new)
        counts[concept]["machine" if machine else "human"] += 1
    return out, counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--actor", required=True,
                    help="who the independent (non-machine) judgments belong to. "
                         "Required rather than defaulted: the backfill cannot know it, "
                         "the operator asserts it.")
    ap.add_argument("--apply", action="store_true",
                    help="rewrite verified_log.jsonl (after a backup). Default: dry-run.")
    args = ap.parse_args()

    data = _data_dir()
    vf = data / "verified_log.jsonl"
    if not vf.exists():
        raise SystemExit(f"{vf} does not exist")
    applied = load_applied(data / "applied_log.jsonl")

    rows = []
    for line in vf.read_text().splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass

    new_rows, counts = convert(rows, applied, args.actor)

    print(f"[backfill] {len(rows)} rows, applied-log entries {len(applied)}")
    print(f"{'concept':28} {'human':>7} {'machine':>8} {'already':>8}")
    tot = collections.Counter()
    for c in sorted(counts):
        k = counts[c]
        print(f"  {c:26} {k['human']:>7} {k['machine']:>8} {k['already']:>8}")
        tot.update(k)
    print(f"  {'TOTAL':26} {tot['human']:>7} {tot['machine']:>8} {tot['already']:>8}")
    curated = sum(1 for r in new_rows if vl.is_curated(r))
    print(f"[backfill] curated after conversion: {curated} / {len(new_rows)}")

    if not args.apply:
        print("[backfill] DRY-RUN — nothing written. Re-run with --apply to rewrite.")
        return 0

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = vf.with_name(f"verified_log.jsonl.bak-{ts}")
    backup.write_text(vf.read_text())
    tmp = vf.with_name("verified_log.jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in new_rows))
    os.replace(tmp, vf)
    print(f"[backfill] wrote {len(new_rows)} schema-2 rows -> {vf} (backup {backup.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
