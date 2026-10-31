# Description: One derivation of "what is the tagger doing right now", shared by the HTML dashboard and the JSON endpoint.
# Kind: lib
# Effects: read-only
# Requires: kura DB
# Runs-on: the API host
# Version: 1.0
"""The tagger's situation, derived once and rendered twice.

WHY THIS FILE EXISTS
--------------------
Every fact below was already computed — inline, inside the HTML dashboard — and
none of it was reachable without a browser session. On 2026-09-05 an agent asked
four questions about this system and got four wrong answers, each from a source
that looked authoritative:

  * "is the tagger finished?"        — read `auto_tagme` (a PROVENANCE MARKER, not
                                       a queue) and concluded 68% unprocessed.
  * "are nodec/nodeb on CPU?"          — read `pixai chars loaded (…, CPU)` and
                                       concluded the workers were on CPU. That
                                       line names the pixai component, which is
                                       pinned to CPU by design; the workers are
                                       on CUDA and ROCm.
  * "is character tagging running?"  — sampled posts, found no character tags,
                                       and did not see that the dashboard already
                                       states `0 character adds/24h` beside
                                       `5/5 pixai hosts`.
  * "how big is the backlog?"        — the dashboard said `0 remaining` while
                                       941,116 posts were queued (see
                                       `sweep_coverage` below).

The answers existed. The path to them did not. So: one derivation, in the schema,
behind the same machine key the extension surfaces use, and named so that the reading
that misled cannot be made from the field name alone.

WHAT `pending` MEANS, AND DOES NOT
----------------------------------
`post.tagging_status IN ('pending','claimed')` is the QUEUE. It is not "never
tagged": the sweep re-queues already-tagged posts so a new release re-tags the
archive. Measured 2026-09-05 — 999,472 posts carry an audit row for release 14,
941,116 are queued, and there are 1,027,004 posts in total. The first two sum to
more than the third, so by pigeonhole at least 913,584 posts are BOTH processed
and queued. A reader who takes `pending` for `untagged` overstates the untagged
population by roughly an order of magnitude.

`auto_tagme` is not the queue either. An importer attaches it to every post it
brings in as a provenance marker; nothing removes it when tagging completes. Its
count answers "how many posts arrived through that importer", which is a
different question that happens to sound like this one.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.services import tagging_queue

#: The category whose members are character names. Looked up BY NAME, never by id:
#: the id was 40 on 2026-09-05 and was written into three separate queries, where a
#: renumbering would not raise — it would return zero and read as "no character tags
#: anywhere", which is a sentence this system can produce for real reasons and so
#: would be believed. A name that stops resolving raises instead.
CHARACTER_CATEGORY_NAME = "character"


async def character_category_id(db: AsyncSession) -> int:
    """The id behind `CHARACTER_CATEGORY_NAME`, or a refusal naming what is missing.

    Raising beats returning a sentinel here. Every caller uses this in a WHERE
    clause, and a wrong-but-valid id turns a broken lookup into a confident zero.
    """
    row = (await db.execute(text(
        "SELECT id FROM tag_category WHERE name = :n"
    ), {"n": CHARACTER_CATEGORY_NAME})).scalar_one_or_none()
    if row is None:
        raise LookupError(
            f"no tag_category named {CHARACTER_CATEGORY_NAME!r} — character coverage "
            f"cannot be derived. This is a schema/seed problem, not an empty result: "
            f"returning 0 here would read as 'nothing is tagged'.")
    return int(row)

#: Freshness thresholds for a worker's last report, in the fleet's shared
#: vocabulary. Derived in SQL from `now()` so the answer does not depend on the
#: API host's clock.
_LIVENESS_SQL = (
    "CASE WHEN n.reported_at IS NULL THEN 'missing' "
    "     WHEN now() - n.reported_at < interval '5 minutes' THEN 'active' "
    "     WHEN now() - n.reported_at < interval '1 hour' THEN 'stale' "
    "     ELSE 'dead' END"
)


async def queue_state(db: AsyncSession) -> dict[str, Any]:
    """The queue, by the column that actually gates a worker's claim.

    Returns every `tagging_status` value present, plus the two roll-ups the
    workers act on, so a caller never has to know which values count as queued.
    """
    by_status = await tagging_queue.by_status(db)
    return {
        "by_status": by_status,
        "queued": by_status.get("pending", 0) + by_status.get("claimed", 0),
        "done": by_status.get("done", 0),
        "total_posts": sum(by_status.values()),
        "queued_means": (
            "posts a worker may claim now. NOT 'never tagged' — the sweep "
            "re-queues tagged posts so a new release re-tags the archive."
        ),
    }


async def sweep_coverage(db: AsyncSession, release_id: int) -> dict[str, Any]:
    """How much of the archive the CURRENT release has processed.

    The dashboard divided "posts with an audit row for this release" by "posts
    whose tagging_status is done RIGHT NOW" and called the result a completion
    percentage. Those are different populations: a post keeps its audit row when
    the sweep re-queues it, so it stays in the numerator and leaves the
    denominator. On 2026-09-05 that printed **1308.9%**, and the companion
    `max(0, done - processed)` printed **0 remaining** while 941,116 posts were
    queued — the clamp turned an inverted ratio into a claim that the work was
    finished.

    The denominator is the archive: of every post that exists, how many has this
    release touched. That is the question the label asks, and unlike the old one
    it cannot exceed 100%.
    """
    row = (await db.execute(text(
        "SELECT (SELECT count(DISTINCT post_id) FROM post_tag_audit WHERE release_id = :rid) AS processed, "
        "       (SELECT count(*) FROM post) AS total_posts"
    ), {"rid": release_id})).mappings().one()
    processed, total = int(row["processed"]), int(row["total_posts"])
    return {
        "release_id": release_id,
        "processed": processed,
        "total_posts": total,
        "remaining": max(0, total - processed),
        "pct": (100.0 * processed / total) if total else 0.0,
    }


async def worker_nodes(db: AsyncSession) -> dict[str, Any]:
    """Every worker the registry expects, and every worker that reports without
    being expected — both directions, because silence and surprise are both drift.

    `expected` comes from `tagger_expected_worker`, which exists because primary's
    tagger unit sat un-enabled for a month and nobody noticed: a worker that
    never reports is invisible unless something states it should have.

    `expected_provider` is carried because the question "is this host on its GPU"
    has a declared answer here, and the places a reader would otherwise look for
    it all mislead: the worker's own log prints `pixai chars loaded (…, CPU)`
    for a component pinned to CPU by design, and a fleet note written on
    2026-07-20 recorded nodec as degraded to CPU — still quoted as current on
    2026-09-05, by which time nodec was on CUDA and nodeb on ROCm. A declaration
    with a date beats a note without one.
    """
    expected = (await db.execute(text(
        "SELECT w.host, w.profile, w.expected_provider, w.opportunistic, w.description, "
        "       n.release_id, n.verified, n.detail, "
        f"       n.reported_at, {_LIVENESS_SQL} AS liveness "
        "FROM tagger_expected_worker w LEFT JOIN tagger_node_state n ON n.host = w.host "
        "ORDER BY (n.reported_at IS NULL), n.reported_at DESC"
    ))).mappings().all()
    unexpected = (await db.execute(text(
        "SELECT n.host, n.release_id, n.reported_at FROM tagger_node_state n "
        "WHERE NOT EXISTS (SELECT 1 FROM tagger_expected_worker w WHERE w.host = n.host)"
    ))).mappings().all()
    return {"expected": [dict(r) for r in expected],
            "reporting_but_unexpected": [dict(r) for r in unexpected]}


async def character_coverage(db: AsyncSession) -> dict[str, Any]:
    """Character tagging: how much is covered, who applied any in the last day,
    and which hosts are actually running the pixai component.

    These three together are the signal. Coverage alone rises so slowly that it
    looks static; `applied_24h` alone cannot distinguish "no work arrived" from
    "the component is not loaded"; `pixai_hosts` alone says the code is present,
    not that it produced anything. The interesting state is the contradiction —
    every host carrying pixai while the day's applications are zero — and it is
    only visible when the three are read side by side, which is why they are one
    function and one JSON object rather than three lookups a caller must join.
    """
    cat = await character_category_id(db)
    cov = (await db.execute(text(
        "SELECT (SELECT count(DISTINCT pt.post_id) FROM post_tag pt "
        "        JOIN tag t ON t.id = pt.tag_id AND t.category_id = :cat) AS covered, "
        "       (SELECT count(*) FROM post) AS total"
    ), {"cat": cat})).mappings().one()
    adds = (await db.execute(text(
        "SELECT a.host, count(*) AS n FROM post_tag_audit a "
        "WHERE a.source = 'auto_tag' AND a.created_at > now() - interval '24 hours' "
        "  AND EXISTS (SELECT 1 FROM unnest(a.added_tags) x "
        "              JOIN tag_name tn ON tn.name = x AND tn.ord = 0 "
        "              JOIN tag tg ON tg.id = tn.tag_id AND tg.category_id = :cat) "
        "GROUP BY a.host ORDER BY n DESC"
    ), {"cat": cat})).mappings().all()
    pixai = (await db.execute(text(
        "SELECT a.host, bool_or(a.actor_id LIKE '%pixai=%') AS has_pixai "
        "FROM post_tag_audit a WHERE a.source = 'auto_tag' "
        "  AND a.created_at > now() - interval '6 hours' GROUP BY a.host ORDER BY a.host"
    ))).mappings().all()
    covered, total = int(cov["covered"]), int(cov["total"])
    hosts = [dict(r) for r in pixai]
    return {
        "covered": covered,
        "total_posts": total,
        "pct": (100.0 * covered / total) if total else 0.0,
        "applied_24h": {r["host"]: int(r["n"]) for r in adds},
        "applied_24h_total": sum(int(r["n"]) for r in adds),
        "pixai_hosts": hosts,
        "pixai_hosts_loaded": sum(1 for r in hosts if r["has_pixai"]),
        "pixai_hosts_seen": len(hosts),
        "note": (
            "pixai is pinned to CPUExecutionProvider by design; a worker log line "
            "reading 'pixai chars loaded (…, CPU)' says nothing about the provider "
            "the worker's own model runs on."
        ),
    }


async def provenance_marker_counts(db: AsyncSession) -> dict[str, Any]:
    """`auto_tagme` — counted here ONLY so its meaning travels with its number.

    It reads like a queue and is not one. An importer attaches it to every post it
    brings in as provenance and nothing removes it, so its count is "posts that
    arrived through that importer", not "posts awaiting tags". It is included
    because it will be looked up regardless, and a number without its definition
    is the shape that produced a wrong answer on 2026-09-05.
    """
    n = (await db.execute(text(
        "SELECT count(*) AS n FROM post_tag pt JOIN tag_name tn ON tn.tag_id = pt.tag_id "
        "WHERE tn.name = 'auto_tagme'"
    ))).scalar_one()
    return {
        "auto_tagme_posts": int(n),
        "is_the_queue": False,
        "meaning": ("a provenance marker an importer attaches and never removes; "
                    "the queue is post_tagging_work.status"),
    }


async def situation(db: AsyncSession, release_id: int) -> dict[str, Any]:
    """Everything above, in one read."""
    from api.services.tagger_progress import progress
    return {
        "training_control": await progress(db),
        "release_id": release_id,
        "queue": await queue_state(db),
        "sweep": await sweep_coverage(db, release_id),
        "workers": await worker_nodes(db),
        "character": await character_coverage(db),
        "provenance_markers": await provenance_marker_counts(db),
    }
