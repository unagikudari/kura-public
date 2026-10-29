#!/usr/bin/env python3
# Description: Promote human-reviewed pool results into the curated GT ledger (kura#64).
# Kind: oneshot (hand-run on the heads node after a pool is reviewed)
# Effects: mutating (appends to verified_log.jsonl; PATCHes pool verify-status)
# Requires: DATABASE_URL in api/env, KTOK in env, TAGGER_HEADS_DATA; runs on the heads node
# Version: 2.0
"""Turn a human-reviewed pool into curated ground truth — and prove it was human.

usage: promote_verified.py <pool_id> [<pool_id> ...]

For each (concept, post) a head applied inside the pool, the residency tells the
direction — the tag is still there (a positive) or a human took it off (a
negative). Before kura#64 that was the whole story, and it was wrong: the script
wrote the row whether or not a human had ever looked, then marked the pool
"verified" itself, so "verified" meant "this script ran", not "a person judged".
Three tags became fake ground truth that way.

Now every row must be backed by INDEPENDENT human evidence in post_tag_audit, and
carries it (schema-2 ledger, see verified_ledger):

    negative  requires a human REMOVAL of that tag on that post
              (actor_type='human', the concept in removed_tags) -> source human_removed
    positive  requires that a human edited that post at all — they reviewed it and
              left the tag -> source human_pool_review

A (concept, post) with no human audit is SKIPPED and counted, never written. The
concepts are not hardcoded: they are whatever the machine applied-log recorded for
the pool's posts, so this tool follows the heads that actually ran rather than a
list that drifts.
"""
from __future__ import annotations

import base64
import json
import os
import ssl
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tagger.api_target import api_base  # noqa: E402
from tagger.heads import verified_ledger as vl  # noqa: E402

HOME = Path(os.environ.get("TAGGER_HEADS_DATA", os.path.expanduser("~/tagger-heads")))
KURA_ENV = Path(__file__).resolve().parents[2] / "api" / "env"


def _dsn_from_env() -> str:
    """DATABASE_URL from api/env, in process — never a shell grep (the DSN carries a
    password; a grep puts it in the process table, kura#43 item 3)."""
    for line in KURA_ENV.read_text().splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().strip('"').strip("'").replace("+asyncpg", "")
    raise SystemExit(f"DATABASE_URL not found in {KURA_ENV}")


def _require_ktok() -> str:
    tok = os.environ.get("KTOK")
    if not tok:
        raise SystemExit(
            "KTOK is not set. The token is read from the environment only — a secret\n"
            "reachable by file path is a secret that can be committed. Source it from\n"
            "your credential store before running (see tagger/heads/README.md).")
    return tok


def applied_by_post(home: Path) -> dict[int, set[str]]:
    """(post_id -> {concepts a head applied there}). The concept set is derived, not
    declared — this is what makes the tool follow the heads instead of a fixed list."""
    out: dict[int, set[str]] = {}
    af = home / "applied_log.jsonl"
    if af.exists():
        for line in af.read_text().splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                out.setdefault(int(d["pid"]), set()).add(d["c"])
            except Exception:
                pass
    return out


def classify(*, present: bool, human_edited_post: bool, human_removed_tag: bool):
    """The evidence rule, as a pure function so its boundaries are testable.

    Returns (source, label) to write, or None to SKIP (no human evidence).
        present + a human edited the post -> ("human_pool_review", "positive")
        not present + a human removed THIS tag -> ("human_removed", "negative")
        anything else -> None (fail-closed: unproven judgments are not written)
    """
    if present and human_edited_post:
        return ("human_pool_review", "positive")
    if (not present) and human_removed_tag:
        return ("human_removed", "negative")
    return None


def _seen(home: Path) -> set[tuple[str, int]]:
    seen: set[tuple[str, int]] = set()
    vf = home / "verified_log.jsonl"
    if vf.exists():
        for line in vf.read_text().splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                # tolerate both schema-2 and any legacy leftovers while reading dedup keys
                seen.add((d.get("concept") or d.get("c"), int(d.get("post_id") or d.get("pid"))))
            except Exception:
                pass
    return seen


def main() -> int:
    import psycopg2

    pools = [int(p) for p in sys.argv[1:]]
    if not pools:
        raise SystemExit("usage: promote_verified.py <pool_id> [<pool_id> ...]")

    conn = psycopg2.connect(_dsn_from_env())
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    seen = _seen(HOME)
    applied = applied_by_post(HOME)

    vf = HOME / "verified_log.jsonl"
    tot_written = tot_skipped = 0
    with open(vf, "a") as out:
        for pool_id in pools:
            c.execute(
                "SELECT pp.post_id, po.tags FROM pool_post pp "
                "JOIN post po ON po.id = pp.post_id WHERE pp.pool_id = %s",
                (pool_id,))
            rows = c.fetchall()
            pids = [pid for pid, _ in rows]
            # One round-trip each: which posts a human edited at all, and which
            # (post, tag) a human explicitly removed.
            human_edited: set[int] = set()
            human_removed: set[tuple[int, str]] = set()
            if pids:
                c.execute(
                    "SELECT DISTINCT post_id FROM post_tag_audit "
                    "WHERE actor_type = 'human' AND post_id = ANY(%s)", (pids,))
                human_edited = {r[0] for r in c.fetchall()}
                c.execute(
                    "SELECT post_id, unnest(removed_tags) FROM post_tag_audit "
                    "WHERE actor_type = 'human' AND post_id = ANY(%s)", (pids,))
                human_removed = {(r[0], r[1]) for r in c.fetchall()}

            npos = nneg = nskip = 0
            for pid, tags in rows:
                ts = set(tags or [])
                for concept in applied.get(pid, set()):
                    if (concept, pid) in seen:
                        continue
                    decided = classify(
                        present=concept in ts,
                        human_edited_post=pid in human_edited,
                        human_removed_tag=(pid, concept) in human_removed)
                    if decided is None:
                        nskip += 1
                        continue
                    source, label = decided
                    row = vl.make_row(concept=concept, post_id=pid, label=label,
                                      source=source, actor=_actor(), at=now, pool=pool_id)
                    out.write(json.dumps(row, ensure_ascii=False) + "\n")
                    seen.add((concept, pid))
                    if label == "positive":
                        npos += 1
                    else:
                        nneg += 1
            tot_written += npos + nneg
            tot_skipped += nskip
            print(f"pool {pool_id}: +{npos} pos, +{nneg} human-neg, {nskip} skipped "
                  f"(no human evidence — not written)")

    _mark_pools_verified(pools)
    print(f"TOTAL: {tot_written} written, {tot_skipped} skipped -> {vf.name}")
    return 0


def _actor() -> str:
    """Who the human judgments are attributed to. Not hardcoded: the owner of this
    kura, named once in the environment, so a different deployment attributes to its
    own owner rather than to a literal baked into this file."""
    return os.environ.get("KURA_OWNER_USERNAME", "owner")


def _mark_pools_verified(pools: list[int]) -> None:
    """Tell kura the pool is verified so the UI badge flips. This is a CONSEQUENCE
    of promotion, not its evidence — the evidence was the post_tag_audit human rows
    checked above, never this call."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    auth = {"Authorization": "Token " + base64.b64encode(
        f"{_actor()}:{_require_ktok()}".encode()).decode(),
        "Content-Type": "application/json"}
    base = api_base()
    for pool_id in pools:
        try:
            req = urllib.request.Request(
                f"{base}/pools/{pool_id}/verify-status",
                data=json.dumps({"status": "verified"}).encode(), headers=auth, method="PATCH")
            urllib.request.urlopen(req, timeout=20, context=ctx)
            print(f"pool {pool_id} -> verified (kura UI)")
        except Exception as e:
            print(f"pool {pool_id} verify-status mark failed: {e}")


if __name__ == "__main__":
    sys.exit(main())
