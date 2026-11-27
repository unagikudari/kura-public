"""Refill the dashboard with small, media-bound rechecks of prior human labels."""
import argparse
import asyncio
import json

from sqlalchemy import text

from api.errors import KuraError
from api.tagger_core.config import NON_TRAINABLE_TAGS
from api.tagger_core.post_exclusion import not_excluded, training_label
from api.tagger_core.review_censor_variants import review_candidate
from api.services.human_review import digest, rendition_hash, reviewable_post_ids
from api.services.review_adjudication import awaiting_resolution, disagreements, is_adjudication, media_is_current, request
from api.services.review_queues import parse_queues, read_legacy_lora_refs, review_lanes
from api.services.review_set_registry import available, import_sets, legacy_lora_refs, load
from api.tagger_core.paired_review import JOINT_REVIEW_PAIR, is_paired_review

TARGET_TAGS = 5
REVIEW_BATCH_SIZE = 12


CANDIDATES = """
WITH latest AS (
    SELECT DISTINCT ON (tag_id) id, tag_id, tag, status, rejected_reason, lifecycle_event_id
    FROM tagger_registration_request
    ORDER BY tag_id, requested_at DESC, id DESC
), last_requested AS (
    SELECT tag_id, max(created_at) AS requested_at
    FROM tagger_review_set
    WHERE purpose IN ('historical_label_recheck','training_support')
    GROUP BY tag_id
)
SELECT candidates.* FROM (
    SELECT 'registration' AS source, id, tag_id, tag, 0 AS priority, rejected_reason,
           CASE WHEN rejected_reason LIKE '%whole_group_split_unavailable%'
                THEN lifecycle_event_id END AS group_event
    FROM latest
    WHERE status = 'rejected' AND (
        rejected_reason LIKE 'serving_quality_failed:%'
        OR rejected_reason LIKE '%insufficient_human_split_support%'
        OR rejected_reason LIKE '%whole_group_split_unavailable%'
    )
    UNION ALL
    SELECT DISTINCT ON (n.tag_id) 'open_candidate', n.tag_id, n.tag_id, n.name, 1, NULL, NULL
    FROM tagger_head_candidate h
    JOIN tag_name n ON n.name=h.tag AND n.ord=0
    WHERE h.status='candidate'
      AND (SELECT count(DISTINCT g.verdict) FROM tagger_gt_label g
           WHERE g.tag_id=n.tag_id AND g.derived_from='human_correction'
             AND g.verdict IN ('positive','negative'))=2
    UNION ALL
    SELECT 'historical_labels', n.tag_id, n.tag_id, n.name, 2, NULL, NULL
    FROM tagger_gt_label g JOIN tag_name n ON n.tag_id=g.tag_id AND n.ord=0
    WHERE g.derived_from='human_correction' AND g.verdict IN ('positive','negative')
    GROUP BY n.tag_id,n.name
    HAVING count(DISTINCT g.verdict)=2
) AS candidates
LEFT JOIN last_requested USING (tag_id)
JOIN tag t ON t.id=candidates.tag_id
JOIN tag_category c ON c.id=t.category_id
ORDER BY CASE c.name WHEN 'general' THEN 0 WHEN 'character' THEN 2 ELSE 1 END,
         candidates.priority, last_requested.requested_at NULLS FIRST, candidates.id DESC
"""

SUPPORT_DEFICITS = f"""
WITH floors AS (
    SELECT sum(ceil(value::numeric)) FILTER (
              WHERE key IN ('min_gt_positive','min_val_pos_support'))::int AS positive,
           sum(ceil(value::numeric)) FILTER (
              WHERE key IN ('min_gt_negative','min_held_out_samples'))::int AS negative
    FROM tagger_config
    WHERE key IN ('min_gt_positive','min_val_pos_support','min_gt_negative','min_held_out_samples')
    HAVING count(*)=4 AND min(value::numeric)>0
), labels AS (
    SELECT count(*) FILTER (WHERE g.verdict='positive') AS positive,
           count(*) FILTER (WHERE g.verdict='negative') AS negative
    FROM tagger_gt_label g
    WHERE g.tag_id=:tag_id AND g.derived_from='human_correction' AND {training_label('g')}
)
SELECT greatest(0,floors.positive-labels.positive) AS positive,
       greatest(0,floors.negative-labels.negative) AS negative
FROM floors CROSS JOIN labels
"""


def support_trigger(reason):
    tokens = (reason or "").replace(":", " ").replace(",", " ").split()
    return ("insufficient_human_split_support" in tokens, "prediction_support" in tokens)


def support_order(samples, deficits):
    """Favor the requested hypothesis while preserving both classes if needed."""
    positive = [sample for sample in samples if sample["tagged"]]
    negative = [sample for sample in samples if not sample["tagged"]]
    if not deficits["positive"]:
        return negative
    if not deficits["negative"]:
        return positive
    first, second = (positive, negative) if deficits["positive"] >= deficits["negative"] else (negative, positive)
    ordered = []
    for i in range(max(len(first), len(second))):
        if i < len(first):
            ordered.append(first[i])
        if i < len(second):
            ordered.append(second[i])
    return ordered


def support_documents(tag, frame, observed, provenance):
    predecessor = {
        "schema": "human-review-set/1", "tag": tag, "purpose": "training_support",
        "frame_sha256": frame, "model_sha256": None,
        "post_ids": [post for post, _, _ in observed],
    }
    explicit = dict(
        predecessor, schema="human-review-set/2",
        media_sha256={str(post): sha for post, sha, _ in observed},
        source_provenance={
            "kind": "frozen_training_support", "artifact_sha256": provenance,
            "supersedes_queue_sha256": digest(predecessor),
        },
    )
    return predecessor, explicit


EXAMPLES = f"""
WITH neighbors AS (
    SELECT CASE WHEN a_tag_id=:tag_id THEN b_tag_id ELSE a_tag_id END AS tag_id
    FROM tag_relation WHERE (a_tag_id=:tag_id OR b_tag_id=:tag_id)
      AND kind IN ('related','exclusive')
    ORDER BY (kind='exclusive') DESC, tag_id LIMIT 8
), excluded AS MATERIALIZED (
    SELECT post_id FROM tagger_gt_label WHERE tag_id=:tag_id
    UNION
    SELECT post_id FROM post_tag_pin WHERE tag_id=:tag_id
    UNION
    SELECT m.post_id
    FROM tagger_review_set_member m JOIN tagger_review_set q USING (queue_sha256)
    WHERE q.tag_id=:tag_id
), tagged_posts AS MATERIALIZED (
    SELECT pt.post_id FROM post_tag pt JOIN post p ON p.id=pt.post_id
     WHERE pt.tag_id=:tag_id AND p.status='ok' AND p.type='image' AND {not_excluded('p.id')} AND {review_candidate('p.id', '(SELECT name FROM tag_name WHERE tag_id=:tag_id AND ord=0)')}
       AND pt.post_id NOT IN (SELECT post_id FROM excluded)
     ORDER BY pt.post_id DESC LIMIT 400
), neighbor_posts AS MATERIALIZED (
    SELECT DISTINCT sample.post_id FROM neighbors n
    CROSS JOIN LATERAL (
        SELECT pt.post_id FROM post_tag pt JOIN post p ON p.id=pt.post_id
        WHERE pt.tag_id=n.tag_id AND p.status='ok' AND p.type='image' AND {not_excluded('p.id')} AND {review_candidate('p.id', '(SELECT name FROM tag_name WHERE tag_id=:tag_id AND ord=0)')}
          AND pt.post_id NOT IN (SELECT post_id FROM excluded)
        ORDER BY pt.post_id DESC LIMIT 250
    ) sample
), same_series AS MATERIALIZED (
    SELECT pt.tag_id FROM tagged_posts seed
    JOIN post_tag pt ON pt.post_id=seed.post_id
    JOIN tag t ON t.id=pt.tag_id
    JOIN tag_category c ON c.id=t.category_id AND c.name='copyright'
    WHERE EXISTS (
        SELECT 1 FROM tag subject JOIN tag_category kind ON kind.id=subject.category_id
        WHERE subject.id=:tag_id AND kind.name='character'
    )
    AND NOT EXISTS (
        SELECT 1 FROM neighbor_posts n
        WHERE NOT EXISTS (
            SELECT 1 FROM post_tag pt WHERE pt.tag_id=:tag_id AND pt.post_id=n.post_id
        )
    )
    GROUP BY pt.tag_id
    ORDER BY count(*) DESC, pt.tag_id
    LIMIT 3
), pool AS MATERIALIZED (
    SELECT post_id FROM tagged_posts
    UNION
    SELECT post_id FROM neighbor_posts
    UNION
    SELECT sample.post_id FROM same_series series
    CROSS JOIN LATERAL (
        SELECT pt.post_id FROM post_tag pt JOIN post p ON p.id=pt.post_id
        WHERE pt.tag_id=series.tag_id AND p.status='ok' AND p.type='image' AND {not_excluded('p.id')} AND {review_candidate('p.id', '(SELECT name FROM tag_name WHERE tag_id=:tag_id AND ord=0)')}
          AND pt.post_id NOT IN (SELECT post_id FROM excluded)
          AND NOT EXISTS (
              SELECT 1 FROM post_tag known WHERE known.post_id=pt.post_id AND known.tag_id=:tag_id
          )
        ORDER BY pt.post_id DESC LIMIT :limit
    ) sample
), eligible AS (
    SELECT post_id,
           EXISTS (SELECT 1 FROM post_tag pt WHERE pt.tag_id=:tag_id AND pt.post_id=pool.post_id) AS tagged
    FROM pool
), ranked AS (
    SELECT post_id, tagged, row_number() OVER (PARTITION BY tagged ORDER BY post_id DESC) AS sample_rank
    FROM eligible
)
SELECT s.post_id,s.tagged,
       encode(CASE WHEN p.media_location='pg' AND octet_length(p.data)<=16777216
                   THEN sha256(p.data) END,'hex') AS inline_sha,
       o.sha256 AS rendition_sha,o.mime,o.byte_size
FROM ranked s JOIN post p ON p.id=s.post_id
LEFT JOIN post_rendition r ON r.post_id=s.post_id AND r.kind='view'
LEFT JOIN rendition_object o ON o.sha256=r.sha256
WHERE s.sample_rank<=:limit
ORDER BY s.sample_rank,s.tagged,s.post_id
"""


GROUP_EXAMPLES = f"""
WITH grouped AS MATERIALIZED (
    SELECT * FROM unnest(CAST(:posts AS bigint[]), CAST(:groups AS integer[])) AS g(post_id, number)
), reviewed AS MATERIALIZED (
    SELECT DISTINCT g.number FROM grouped g
    WHERE EXISTS (SELECT 1 FROM tagger_gt_label x WHERE x.tag_id=:tag_id AND x.post_id=g.post_id)
       OR EXISTS (SELECT 1 FROM post_tag_pin x WHERE x.tag_id=:tag_id AND x.post_id=g.post_id)
       OR EXISTS (
          SELECT 1 FROM tagger_review_set_member m JOIN tagger_review_set q USING (queue_sha256)
          WHERE q.tag_id=:tag_id AND m.post_id=g.post_id
      )
), candidates AS (
    SELECT DISTINCT ON (g.number) p.id AS post_id, g.number
    FROM grouped g
    JOIN post p ON p.id=g.post_id
    JOIN post_tag pt ON pt.tag_id=:tag_id AND pt.post_id=p.id
    WHERE p.status='ok' AND p.type='image' AND {not_excluded('p.id')} AND {review_candidate('p.id', '(SELECT name FROM tag_name WHERE tag_id=:tag_id AND ord=0)')}
      AND NOT EXISTS (SELECT 1 FROM reviewed r WHERE r.number=g.number)
    ORDER BY g.number,p.id DESC
    LIMIT :limit
)
SELECT s.post_id,true AS tagged,
       encode(CASE WHEN p.media_location='pg' AND octet_length(p.data)<=16777216
                   THEN sha256(p.data) END,'hex') AS inline_sha,
       o.sha256 AS rendition_sha,o.mime,o.byte_size
FROM candidates s JOIN post p ON p.id=s.post_id
LEFT JOIN post_rendition r ON r.post_id=s.post_id AND r.kind='view'
LEFT JOIN rendition_object o ON o.sha256=r.sha256
ORDER BY s.number
"""


async def group_examples(db, row):
    from api.tagger_core.learning_artifacts import ProtocolError, job_root, read_verified

    event = row["group_event"]
    path = job_root(row["tag"], event) / "review-groups.json"
    try:
        index = read_verified(path, "tagger-review-groups/1")
    except (OSError, ValueError, ProtocolError):
        return []
    if index["tag"] != row["tag"] or index["event"] != event:
        return []
    posts, groups = [], []
    for number, members in enumerate(index["groups"]):
        posts.extend(members)
        groups.extend([number] * len(members))
    result = await db.execute(text(GROUP_EXAMPLES), {
        "tag_id": row["tag_id"], "posts": posts, "groups": groups, "limit": REVIEW_BATCH_SIZE // 2,
    })
    return result.mappings().all()


async def prepare_joint_batch(db, open_tags):
    from api.tagger_core.exclusive_training import (
        exclusive_relation, grouped_support_requirement, joint_minima, paired_evidence,
        paired_review_batch, split_examples,
    )

    left, right = JOINT_REVIEW_PAIR
    if open_tags.intersection(JOINT_REVIEW_PAIR) or not await exclusive_relation(db, left, right):
        return {}
    _, groups = await paired_evidence(db, left, right)
    classes = (left, right, "neither")
    minima = await joint_minima(db)
    counts, split = split_examples(groups, classes, **minima)
    if split["state"] == "split_available":
        return {}
    deficits = grouped_support_requirement(counts, classes, **minima)[
        "minimum_extra_examples_before_split"
    ]
    return await paired_review_batch(db, left, right, deficits=deficits)



async def prepare(db, *, target_tags=None, ordinary_limit=None):
    if target_tags is None:
        target_tags = TARGET_TAGS
    if not await available(db):
        raise RuntimeError("review registry inactive")
    from api.services.review_reselection import retired_refs
    retired = await retired_refs(db) or set()
    queues = await load(db)
    legacy = await legacy_lora_refs(db)
    if legacy is None:
        legacy = await asyncio.to_thread(read_legacy_lora_refs)
    review_lanes(queues, legacy)
    open_tags = set()
    adjudication_tags = set()
    paired_pending = False
    for queue in queues:
        if queue["queue_sha256"] in retired or queue.get("superseded_by") or queue["schema"] not in (
                "human-review-set/2", "human-review-set/3"):
            continue
        if queue["review_lane"] == "lora_findings":
            continue
        if queue["purpose"] not in (
            "historical_label_recheck", "candidate_evaluation", "boundary_recheck", "training_support",
        ):
            continue
        third = is_adjudication(queue)
        ids = await reviewable_post_ids(db, queue)
        answered = (await db.execute(text("""
            SELECT count(DISTINCT post_id) FROM tagger_human_review_event
            WHERE queue_sha256=:ref AND post_id=ANY(:ids) AND (NOT :third OR decision <> 'uncertain')
        """), {"ref": queue["queue_sha256"], "third": third, "ids": ids})).scalar_one()
        if answered < len(ids) and (not third or await awaiting_resolution(db, queue)):
            (adjudication_tags if third else open_tags).add(queue["tag"])
            paired_pending = paired_pending or is_paired_review(queue)
    incoming = {}
    suggestions = []
    used_frames = {q["frame_sha256"] for q in queues if q["purpose"] == "historical_label_recheck"}
    if len(adjudication_tags) < TARGET_TAGS:
        for conflict in await disagreements(db):
            tag = conflict["tag"]
            document = request(tag, conflict["post_id"], conflict["display_sha256"])
            if tag in adjudication_tags or document["frame_sha256"] in used_frames:
                continue
            if not await media_is_current(db, document):
                continue
            ref = digest(document)
            incoming[ref + ".json"] = json.dumps(
                document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            suggestions.append({"tag": tag, "images": 1, "reference": ref, "adjudication": True})
            adjudication_tags.add(tag)
            if len(adjudication_tags) == TARGET_TAGS:
                break
    if len(open_tags) >= target_tags:
        return incoming, suggestions
    if ordinary_limit is None:
        pair = await prepare_joint_batch(db, open_tags)
        incoming.update(pair)
        for queue in parse_queues(pair, canonical=True):
            tag = queue["tag"]
            open_tags.add(tag)
            suggestions.append({"tag": tag, "images": len(queue["post_ids"]),
                                "reference": queue["queue_sha256"]})
        # The matched queues are atomic. The final free suggestion slot may
        # therefore temporarily result in one extra tag.
        if len(open_tags) >= target_tags:
            return incoming, suggestions
    considered = set(JOINT_REVIEW_PAIR) if paired_pending else set()
    ordinary_added = 0
    candidates = (await db.execute(text(CANDIDATES))).mappings().all()
    for row in candidates:
        if row["tag"] in NON_TRAINABLE_TAGS or row["tag"] in open_tags or row["tag"] in considered:
            continue
        considered.add(row["tag"])
        split_shortage, prediction_shortage = support_trigger(row["rejected_reason"])
        deficits = None
        if split_shortage or prediction_shortage:
            support = (await db.execute(text(SUPPORT_DEFICITS), {"tag_id": row["tag_id"]})).mappings().one_or_none()
            if support is not None and any(support.values()):
                deficits = support
            else:
                # A prediction shortfall alone belongs to the candidate's
                # independent evaluation. A resolved label shortage also
                # needs no further training-support review.
                continue
        preferred = await group_examples(db, row) if row["group_event"] is not None else []
        samples = (await db.execute(text(EXAMPLES), {
            "tag_id": row["tag_id"], "limit": REVIEW_BATCH_SIZE * 2,
        })).mappings().all()
        observed = []
        unique_samples = {image["post_id"]: image for image in (*preferred, *samples)}
        ordered = support_order(unique_samples.values(), deficits) if deficits else unique_samples.values()
        for image in ordered:
            if (image["byte_size"] or 0) > 16 * 1024 * 1024:
                continue
            try:
                sha = (await asyncio.wait_for(asyncio.to_thread(
                    rendition_hash, image["rendition_sha"], image["mime"]), 8)
                    if image["rendition_sha"] is not None else None)
            except (KuraError, TimeoutError):
                continue
            sha = sha or image["inline_sha"]
            if sha:
                observed.append((image["post_id"], sha, image["tagged"]))
                if len(observed) == REVIEW_BATCH_SIZE:
                    break
        if not observed:
            continue
        provenance = digest({"source": row["source"], "source_id": row["id"], "images": observed})
        frame = digest({"kind": "automatic_unjudged_review", "tag": row["tag"], "images": observed})
        document = {
            "schema": "human-review-set/2",
            "tag": row["tag"], "purpose": "historical_label_recheck",
            "frame_sha256": frame,
            "model_sha256": None,
            "post_ids": [i for i, _, _ in observed],
            "media_sha256": {str(i): sha for i, sha, _ in observed},
            "source_provenance": {"kind": "historical_human_training", "artifact_sha256": provenance},
        }
        documents = support_documents(row["tag"], frame, observed, provenance) if deficits else (document,)
        ref = digest(documents[-1])
        for item in documents:
            incoming[digest(item) + ".json"] = json.dumps(
                item, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        suggestion = {"tag": row["tag"], "images": len(observed), "reference": ref}
        if deficits:
            suggestion.update(purpose="training_support", deficits=dict(deficits))
        suggestions.append(suggestion)
        open_tags.add(row["tag"])
        ordinary_added += 1
        if len(open_tags) == target_tags or ordinary_added == ordinary_limit:
            break
    return incoming, suggestions


async def run(execute):
    from api.db.session import async_session

    async with async_session() as db:
        await db.execute(text("SET LOCAL statement_timeout='20s'"))
        await db.execute(text("SET LOCAL lock_timeout='2s'"))
        if execute:
            await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('tagger_review_set_registry',0))"))
        incoming, suggestions = await prepare(db)
        if incoming and execute:
            await import_sets(db, incoming)
            await db.commit()
        print(json.dumps({"suggestions": suggestions, "registered": bool(incoming and execute)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    asyncio.run(run(parser.parse_args().execute))


if __name__ == "__main__":
    main()
