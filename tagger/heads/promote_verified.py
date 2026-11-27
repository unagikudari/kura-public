"""Export corroborated, tag-specific human judgments into the heads ledger."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from api.tagger_core.post_exclusion import OPT_OUT_TAG, YOUTH_NAMED_TAGS, YOUTH_TAGS, not_excluded
from tagger.heads import verified_ledger as vl
from tagger.heads.canonical_gt import not_evaluation_audit  # noqa: E402

HOME = Path(os.environ.get("TAGGER_HEADS_DATA", os.path.expanduser("~/tagger-heads")))
KURA_ENV = Path(__file__).resolve().parents[2] / "api" / "env"


def _dsn_from_env() -> str:
    """DATABASE_URL from api/env, in process — never a shell grep (the DSN carries a
    password; a grep puts it in the process table, kura#43 item 3)."""
    for line in KURA_ENV.read_text().splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().strip('"').strip("'").replace("+asyncpg", "")
    raise SystemExit(f"DATABASE_URL not found in {KURA_ENV}")


ELIGIBLE = not_excluded("p.id")

EVIDENCE = f"""
SELECT {ELIGIBLE}, ARRAY(
    SELECT n.name FROM post_tag pt JOIN tag_name n ON n.tag_id=pt.tag_id
    WHERE pt.post_id=p.id AND n.name=ANY(%s)
), p.status, gt.verdict,
audit.id, CASE WHEN {not_evaluation_audit('audit.id')} THEN audit.actor_type END, audit.actor_id, audit.created_at,
%s=ANY(audit.added_tags), %s=ANY(audit.removed_tags),
review.id, review.actor_id, review.actor_name, review.received_at, review.decision,
CASE WHEN review.id IS NULL THEN false ELSE
    review.source_context #>> '{{observed,display_sha256}}' =
    COALESCE(encode(r.sha256,'hex'), CASE WHEN p.media_location='pg' THEN encode(sha256(p.data),'hex') END)
END
FROM post p
JOIN tag_name n ON n.name=%s AND n.ord=0
LEFT JOIN tagger_gt_label gt ON gt.post_id=p.id AND gt.tag_id=n.tag_id
LEFT JOIN post_rendition r ON r.post_id=p.id AND r.kind='view'
LEFT JOIN LATERAL (
    SELECT a.* FROM post_tag_audit a WHERE a.post_id=p.id
      AND (%s=ANY(a.added_tags) OR %s=ANY(a.removed_tags))
    ORDER BY a.created_at DESC,a.id DESC LIMIT 1
) audit ON true
LEFT JOIN LATERAL (
    SELECT e.* FROM tagger_human_review_event e WHERE e.post_id=p.id
      AND e.source_context #>> '{{observed,tag_id}}'=n.tag_id::text
      AND e.source_context #>> '{{queue,purpose}}' IN ('training_support','historical_label_recheck')
      AND e.source_context->>'provenance'='authenticated_explicit_user_response'
    ORDER BY e.received_at DESC,e.id DESC LIMIT 1
) review ON true
WHERE p.id=%s
"""


def decide_evidence(row, machine_at):
    if row is None:
        return None, ("post_or_tag_missing",)
    eligible, tags, status, gt, aid, kind, actor, at, added, removed, rid, uid, name, rt, decision, media_ok = row
    if not eligible:
        reasons = []
        if set(tags).intersection(YOUTH_TAGS + YOUTH_NAMED_TAGS):
            reasons.append("youth_tag")
        if OPT_OUT_TAG in tags:
            reasons.append("training_opt_out")
        return None, tuple(reasons or ["post_excluded"])
    if status != "ok":
        return None, ("post_unavailable",)
    if rid is not None and (at is None or rt >= at):
        if not media_ok or decision not in ("present", "absent") or rt <= machine_at:
            return None, ("review_evidence_unverified",)
        label = "positive" if decision == "present" else "negative"
        result = dict(label=label, source="human_review_event", actor=name, actor_id=uid,
                      at=rt.isoformat(), evidence_id=rid)
    elif aid is not None and kind == "human" and actor and at > machine_at and bool(added) != bool(removed):
        label = "positive" if added else "negative"
        result = dict(label=label, source="human_tag_audit", actor=actor, actor_id=actor,
                      at=at.isoformat(), evidence_id=aid)
    else:
        return None, ("tag_specific_evidence_missing",)
    if gt is not None and gt != label:
        return None, ("canonical_gt_conflict",)
    return result, ()


def lookup_evidence(cursor, post_id, concept, machine_at):
    cursor.execute(EVIDENCE, ((OPT_OUT_TAG,) + YOUTH_TAGS + YOUTH_NAMED_TAGS,
                              concept, concept, concept, concept, concept, post_id))
    return decide_evidence(cursor.fetchone(), machine_at)


def applied_times(home):
    from collections import defaultdict
    entries = defaultdict(dict)
    for line in (home / "applied_log.jsonl").read_text().splitlines():
        try:
            row = json.loads(line)
            timestamp = datetime.fromisoformat(row["t"])
            if timestamp.tzinfo is None:
                continue
            entries[row["pid"]][row["c"]] = max(timestamp, entries[row["pid"]].get(row["c"], timestamp))
        except (ValueError, TypeError, KeyError):
            continue
    return entries


def main() -> int:
    from collections import Counter
    import fcntl
    import psycopg2

    pools = [int(p) for p in sys.argv[1:]]
    if not pools:
        raise SystemExit("usage: promote_verified.py <pool_id> [<pool_id> ...]")
    conn = psycopg2.connect(_dsn_from_env())
    conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
    applied = applied_times(HOME)
    reasons = Counter()
    pending = []
    with conn, conn.cursor() as cursor:
        for pool in pools:
            cursor.execute("SELECT post_id FROM pool_post WHERE pool_id=%s", (pool,))
            for (pid,) in cursor.fetchall():
                for concept, timestamp in applied.get(pid, {}).items():
                    evidence, failures = lookup_evidence(cursor, pid, concept, timestamp)
                    reasons.update(failures)
                    if evidence:
                        pending.append(dict(schema=3, concept=concept, post_id=pid, pool=pool, **evidence))
    with (HOME / "verified_log.jsonl").open("a+") as ledger:
        fcntl.flock(ledger, fcntl.LOCK_EX)
        ledger.seek(0)
        seen = set()
        for line in ledger:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if vl.is_proven(row):
                seen.add((row["concept"], row["post_id"], row["source"], row["evidence_id"]))
        for row in pending:
            key = (row["concept"], row["post_id"], row["source"], row["evidence_id"])
            if key not in seen and vl.is_proven(row):
                ledger.write(json.dumps(row, ensure_ascii=False) + "\n")
                seen.add(key)
                reasons["written"] += 1
    print(json.dumps(reasons, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
