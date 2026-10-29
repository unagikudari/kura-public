#!/usr/bin/env python3
# Description: one-shot triage of the open head_candidate backlog against the non-trainable predicate (Step 5 / E-M2)
# Kind: tool
# Effects: read-only by default (dry-run); --apply writes via tagger_governed_dismiss_head_candidate (run on primary ONLY)
# Runs-on: primary (--apply); any node for dry-run
"""tagger/vnext_triage.py — one-shot backlog triage for plan
tagger-vnext-deterministic-runner Step 5 (adopted finding E-M2).

The entry-point filters (pools.py verify-status, gt_sync global detection,
reconciler loop head) stop NEW non-trainable tags from becoming candidates,
but the existing backlog (112 open candidate rows at plan time, mixing in
'tagger-exclude' and character tags like 'Sakura🔞') predates them and would
keep manufacturing HOLD registrations. This script classifies every open
candidate with the SAME shared predicate (api/tagger_core/config.py::is_non_trainable)
and dismisses the non-trainable ones.

Dispositions:
  - non-trainable  -> dismiss (with --apply)
  - trainable      -> keep (untouched)
  - undecidable    -> keep + report (no tag row / category lookup failed;
                      plan Step 5 routes genuinely unclassifiable cases to
                      the E5 escalation, which is the runner's job -- this
                      one-shot never guesses)

DRY-RUN is the default and prints the full decision table. --apply performs
the governed dismissals and is meant to run ON PRIMARY ONLY: trainer must not
write to the kura DB (INV-R1) -- kura writes go through primary, and this script
enforces that with a hostname guard (override --force-host for a renamed
canonical host, never to write from a replica node).

Reads go to primary directly (not the replica): tagger_head_candidate is NOT in
the replication set (kura_mesh_pub), so the replica's table is empty and a
replica-side dry-run would vacuously triage nothing.
"""
from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.tagger_core.config import is_non_trainable  # noqa: E402

from tagger.kura_db import canonical_host as _canonical_host   # noqa: E402

def apply_host() -> str:
    """The only node allowed to write (INV-R1): the declared canonical node, resolved when
    --apply runs rather than at import."""
    return _canonical_host()


def fetch_candidates(conn) -> list[tuple[str, str | None, int]]:
    """Open candidates with their tag-category name (None = no tag row):
    (tag, category, n_candidate_rows), one entry per tag."""
    cur = conn.cursor()
    cur.execute(
        "SELECT hc.tag, min(tc.name) AS category, count(*) AS n "
        "FROM tagger_head_candidate hc "
        "LEFT JOIN tag_name tn ON tn.name = hc.tag AND tn.ord = 0 "
        "LEFT JOIN tag tg ON tg.id = tn.tag_id "
        "LEFT JOIN tag_category tc ON tc.id = tg.category_id "
        "WHERE hc.status = 'candidate' "
        "GROUP BY hc.tag ORDER BY hc.tag")
    rows = cur.fetchall()
    cur.close()
    return rows


def classify(rows: list[tuple[str, str | None, int]]) -> dict[str, list]:
    """Pure classification -- shared predicate, no writes."""
    out = {"dismiss": [], "keep": [], "undecidable": []}
    for tag, category, n in rows:
        if is_non_trainable(tag, category):
            out["dismiss"].append((tag, category, n))
        elif category is None:
            # No tag row / lookup failed: the literal check above already ran,
            # so this tag is not provably non-trainable. Keep, but surface it
            # for the E5 (unclassifiable) escalation path instead of guessing.
            out["undecidable"].append((tag, category, n))
        else:
            out["keep"].append((tag, category, n))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="perform the governed dismissals (primary only; default dry-run)")
    ap.add_argument("--force-host", action="store_true",
                    help="skip the primary hostname guard (renamed canonical host only "
                         "-- NEVER to write from a replica; INV-R1)")
    args = ap.parse_args()

    APPLY_HOST = apply_host() if args.apply else None
    if args.apply and not args.force_host and socket.gethostname() != APPLY_HOST:
        print(f"REFUSING --apply: hostname {socket.gethostname()!r} != {APPLY_HOST!r}. "
              f"kura DB writes happen on the canonical node only (INV-R1). Run this on {APPLY_HOST}.", file=sys.stderr)
        return 2

    from tagger.kura_db import connect_canonical   # KURA_CANONICAL_DSN, default primary
    conn = connect_canonical()
    conn.autocommit = True
    try:
        rows = fetch_candidates(conn)
        triage = classify(rows)

        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[vnext_triage] {mode}: {len(rows)} open candidate tag(s) "
              f"-> dismiss {len(triage['dismiss'])} / keep {len(triage['keep'])} "
              f"/ undecidable {len(triage['undecidable'])}")
        for verdict in ("dismiss", "keep", "undecidable"):
            for tag, category, n in triage[verdict]:
                print(f"  {verdict:11s} {tag!r} (category={category}, rows={n})")

        if not args.apply:
            print("[vnext_triage] dry-run complete -- nothing written. "
                  "Re-run with --apply ON PRIMARY to dismiss.")
            return 0

        dismissed = 0
        # P7 (「automation は人間の決定を上書きしない」) は仕様どおり RAISE する。
        # ここは autocommit なので、それを受けないと 1 件の保護されたタグで残り全部が
        # 未処理のまま main() を貫き、先行分は commit 済みなのに APPLY done も出ない
        # — 無音の部分適用になる (2026-09-04 の敵対的レビュー)。守られたタグは
        # 「飛ばした理由」として数え、最後にまとめて名乗る。
        protected: list[tuple[str, str]] = []
        cur = conn.cursor()
        for tag, category, _n in triage["dismiss"]:
            try:
                cur.execute(
                    "SELECT tagger_governed_dismiss_head_candidate(%s, %s)",
                    (tag, f"non-trainable (category={category}; plan "
                          f"tagger-vnext-deterministic-runner Step 5/E-M2 one-shot triage)"))
                n_rows = cur.fetchone()[0]
            except psycopg2.errors.RaiseException as e:
                # 人間の決定が立っている = このタグは触らないのが正しい結果。
                msg = str(e).strip().splitlines()[0]
                print(f"  SKIPPED {tag!r}: {msg}")
                protected.append((tag, msg))
                continue
            print(f"  dismissed {tag!r}: {n_rows} row(s)")
            dismissed += n_rows
        cur.close()
        print(f"[vnext_triage] APPLY done: {dismissed} candidate row(s) dismissed"
              + (f"; {len(protected)} skipped (人間の決定が立っている): "
                 + ", ".join(t for t, _ in protected) if protected else ""))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
