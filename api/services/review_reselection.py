"""Retire review requests and select replacements in a single transaction."""
from sqlalchemy import text

from api.errors import KuraError
from api.services.review_adjudication import is_adjudication
from api.services.review_set_registry import import_sets, load
from api.tagger_core.paired_review import is_paired_review


async def retired_refs(db):
    if not (await db.execute(text(
        "SELECT to_regclass('public.tagger_review_set_retirement') IS NOT NULL"
    ))).scalar_one():
        return None
    return set((await db.execute(text(
        "SELECT queue_sha256 FROM tagger_review_set_retirement"
    ))).scalars())


async def lock_queue(db, ref):
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                     {"key": "tagger-review-retirement:" + ref})


async def replace(db, refs, actor_id):
    from api.tagger_core.review_refill import TARGET_TAGS, prepare

    await db.execute(text("SET LOCAL statement_timeout='20s'"))
    await db.execute(text("SET LOCAL lock_timeout='2s'"))
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('tagger_review_set_registry',0))"))
    for ref in sorted(refs):
        await lock_queue(db, ref)
    retired = await retired_refs(db)
    if retired is None:
        raise KuraError(503, "review_reselection_unavailable")
    queues = {q["queue_sha256"]: q for q in await load(db)}
    if any(ref not in queues or ref in retired or
           queues[ref]["purpose"] != "historical_label_recheck" or is_adjudication(queues[ref])
           or is_paired_review(queues[ref])
           for ref in refs):
        raise KuraError(409, "review_reselection_stale")
    open_count = (await db.execute(text("""
        SELECT count(DISTINCT q.queue_sha256)
        FROM tagger_review_set q JOIN tagger_review_set_member m USING (queue_sha256)
        WHERE q.queue_sha256=ANY(:refs) AND NOT EXISTS (
            SELECT 1 FROM tagger_human_review_event e
            WHERE e.queue_sha256=m.queue_sha256 AND e.post_id=m.post_id
        )
    """), {"refs": refs})).scalar_one()
    if open_count != len(refs):
        raise KuraError(409, "review_reselection_stale")
    await db.execute(text("""
        INSERT INTO tagger_review_set_retirement(queue_sha256,actor_id)
        SELECT unnest(CAST(:refs AS text[])), :actor
    """), {"refs": refs, "actor": actor_id})
    incoming, suggestions = await prepare(
        db, target_tags=TARGET_TAGS + len(refs), ordinary_limit=len(refs)
    )
    if sum(not q.get('adjudication') for q in suggestions) != len(refs):
        raise KuraError(409, "review_reselection_no_replacements")
    await import_sets(db, incoming)
    return {"retired": len(refs), "suggestions": suggestions}
