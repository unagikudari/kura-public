"""Resolve independent review votes through a separate, media-bound request."""
import hashlib
import json

from sqlalchemy import text


def _digest(document):
    return hashlib.sha256(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def frame(tag, post_id, media_sha):
    return _digest({"kind": "third_reviewer", "tag": tag, "post_id": post_id, "media": media_sha})


def is_adjudication(queue):
    ids = queue["post_ids"]
    return (queue["purpose"] == "historical_label_recheck" and len(ids) == 1
            and queue["frame_sha256"] == frame(queue["tag"], ids[0], queue["media_sha256"][str(ids[0])]))


LATEST_VOTES = """
SELECT DISTINCT ON (q.tag_id, e.post_id, e.actor_id, m.display_sha256)
       q.tag_id, n.name AS tag, e.post_id, e.actor_id, e.decision, m.display_sha256
FROM tagger_human_review_event e
JOIN tagger_review_set_member m USING (queue_sha256, post_id)
JOIN tagger_review_set q USING (queue_sha256)
JOIN tag_name n ON n.tag_id=q.tag_id AND n.ord=0
WHERE q.purpose='candidate_evaluation' AND m.display_sha256 IS NOT NULL
ORDER BY q.tag_id,e.post_id,e.actor_id,m.display_sha256,e.id DESC
"""


CURRENT_MEMBERS = """
SELECT current.tag_id, m.post_id, m.display_sha256
FROM (
    SELECT DISTINCT ON (q.tag_id) q.tag_id,q.queue_sha256
    FROM tagger_review_set q
    WHERE q.purpose='candidate_evaluation'
      AND convert_from(q.document,'UTF8')::jsonb->>'schema'='human-review-set/3'
    ORDER BY q.tag_id,q.created_at DESC,q.queue_sha256 DESC
) current
JOIN tagger_review_set_member m USING (queue_sha256)
"""


async def disagreements(db):
    return (await db.execute(text(f"""
        WITH votes AS ({LATEST_VOTES})
        SELECT tag,post_id,display_sha256
        FROM votes JOIN ({CURRENT_MEMBERS}) current USING (tag_id,post_id,display_sha256)
        GROUP BY tag,post_id,display_sha256
        HAVING bool_or(decision='present') AND bool_or(decision='absent')
        ORDER BY tag,post_id,display_sha256
    """))).mappings().all()


async def check_reviewer(db, queue, post_id, actor_id):
    return (await db.execute(text(f"""
        SELECT count(*) FILTER (WHERE actor_id=:actor) > 0 AS original,
               bool_or(decision='present') AND bool_or(decision='absent') AS disputed
        FROM ({LATEST_VOTES}) votes
        WHERE tag=:tag AND post_id=:post AND display_sha256=:media
    """), {"tag": queue["tag"], "post": post_id,
           "media": queue["media_sha256"][str(post_id)], "actor": actor_id})).one()


async def has_adjudicated(db, queue, post_id, actor_id):
    if queue["schema"] != "human-review-set/3":
        from api.services.review_set_registry import available
        if not await available(db):
            return False
    identity = frame(queue["tag"], post_id, queue["media_sha256"][str(post_id)])
    return (await db.execute(text("""
        SELECT EXISTS (
            SELECT 1 FROM tagger_human_review_event e
            JOIN tagger_review_set q USING (queue_sha256)
            WHERE q.frame_sha256=:frame AND q.purpose='historical_label_recheck'
              AND e.post_id=:post AND e.actor_id=:actor AND e.decision IN ('present','absent')
        )
    """), {"frame": identity, "post": post_id, "actor": actor_id})).scalar_one()


async def awaiting_resolution(db, queue):
    pid = queue["post_ids"][0]
    votes = await check_reviewer(db, queue, pid, None)
    if not votes.disputed:
        return False
    current = (await db.execute(text(f"""
        SELECT EXISTS (
            SELECT 1 FROM ({CURRENT_MEMBERS}) m
            JOIN tag_name n ON n.tag_id=m.tag_id AND n.ord=0
            WHERE n.name=:tag AND m.post_id=:post AND m.display_sha256=:media
        )
    """), {"tag": queue["tag"], "post": pid,
           "media": queue["media_sha256"][str(pid)]})).scalar_one()
    return current and await media_is_current(db, queue)


async def media_is_current(db, queue):
    from api.errors import KuraError
    from api.services.human_review import context

    # Use the same rendition and fallback checks as the judgment endpoint.
    bound = queue if "queue_sha256" in queue else dict(queue, queue_sha256=_digest(queue))
    try:
        await context(db, bound, queue["post_ids"][0])
    except KuraError:
        return False
    return True


def request(tag, post_id, media):
    identity = frame(tag, post_id, media)
    return {
        "schema": "human-review-set/2",
        "tag": tag, "purpose": "historical_label_recheck", "frame_sha256": identity,
        "model_sha256": None, "post_ids": [post_id], "media_sha256": {str(post_id): media},
        "source_provenance": {"kind": "historical_human_training", "artifact_sha256": identity},
    }


async def resolution(db, queue_ref, tag, answers):
    rows = (await db.execute(text(f"""
        WITH votes AS ({LATEST_VOTES})
        SELECT v.post_id, v.display_sha256
        FROM votes v JOIN tagger_review_set_member m
          ON m.post_id=v.post_id AND m.display_sha256=v.display_sha256
        WHERE m.queue_sha256=:ref AND v.tag=:tag
        GROUP BY v.post_id,v.display_sha256
        HAVING bool_or(v.decision='present') AND bool_or(v.decision='absent')
    """), {"ref": queue_ref, "tag": tag})).mappings().all()
    by_post = {a["post_id"]: a for a in answers}
    for row in rows:
        answer = by_post[row["post_id"]]
        if answer["event_id"] is None:
            continue
        decisive = (await db.execute(text("""
            SELECT e.id,e.decision FROM tagger_human_review_event e
            JOIN tagger_review_set q USING (queue_sha256)
            WHERE q.frame_sha256=:frame AND q.purpose='historical_label_recheck'
              AND e.post_id=:post AND e.decision IN ('present','absent')
            ORDER BY e.id DESC LIMIT 1
        """), {"frame": frame(tag, row["post_id"], row["display_sha256"]),
               "post": row["post_id"]})).one_or_none()
        answer["decision"] = decisive.decision if decisive is not None else None
        answer["adjudication_event_id"] = decisive.id if decisive is not None else None
