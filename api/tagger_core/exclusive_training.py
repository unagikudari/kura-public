"""Inspect paired human-label support for a declared exclusive tag relation."""
import argparse
import asyncio
import json
from collections import Counter, defaultdict
from itertools import zip_longest

from sqlalchemy import text

from api.errors import KuraError
from api.services.human_review import digest, rendition_hash
from api.tagger_core.paired_review import JOINT_REVIEW_PAIR, is_paired_review, paired_review_frame
from api.services.review_queues import parse_queues
from api.tagger_core.post_exclusion import not_excluded, training_label


PAIR_CONTEXT_TAGS = ("anal", "vaginal")


async def exclusive_relation(db, left, right):
    return (await db.execute(text("""
        SELECT EXISTS (
            SELECT 1 FROM tag_relation r
            JOIN tag_name a ON a.tag_id=r.a_tag_id AND a.ord=0
            JOIN tag_name b ON b.tag_id=r.b_tag_id AND b.ord=0
            WHERE r.kind='exclusive' AND
                  ((a.name=:left AND b.name=:right) OR (a.name=:right AND b.name=:left))
        )
    """), {"left": left, "right": right})).scalar_one()


async def paired_evidence(db, left, right):
    if not await exclusive_relation(db, left, right):
        raise ValueError("exclusive relation is not declared")

    rows = (await db.execute(text(f"""
        WITH pairs AS (
            SELECT post_id, max(verdict) FILTER (WHERE tag=:left) AS left_label,
                            max(verdict) FILTER (WHERE tag=:right) AS right_label
            FROM tagger_gt_label g
            WHERE tag IN (:left,:right) AND derived_from='human_correction' AND {training_label('g')}
            GROUP BY post_id
        )
        SELECT p.post_id, p.left_label, p.right_label, a.artist_ids
        FROM pairs p JOIN post image ON image.id=p.post_id
        LEFT JOIN LATERAL (
            SELECT array_agg(DISTINCT t.id ORDER BY t.id) AS artist_ids
            FROM post_tag pt JOIN tag t ON t.id=pt.tag_id
            JOIN tag_category c ON c.id=t.category_id AND c.name='artist'
            WHERE pt.post_id=p.post_id
        ) a ON true
        WHERE image.status='ok' AND image.type='image' AND {not_excluded('image.id')}
        ORDER BY p.post_id
    """), {"left": left, "right": right})).all()
    counts = Counter()
    artists = defaultdict(set)
    missing_artist = Counter()
    components = []
    observations = []
    for post_id, a, b, artist_ids in rows:
        if a == b == "positive":
            label = "conflicting"
        elif a == "positive":
            label = left
        elif b == "positive":
            label = right
        elif a == b == "negative":
            label = "neither"
        else:
            label = "unresolved"
        counts[label] += 1
        if artist_ids:
            group = set(artist_ids)
            artists[label].update(group)
            observations.append((post_id, label, frozenset(group)))
            overlapping = [component for component in components if component & group]
            for component in overlapping:
                group.update(component)
                components.remove(component)
            components.append(group)
        else:
            missing_artist[label] += 1
    outcomes = (left, right, "neither", "unresolved", "conflicting")
    support = {
        label: {
            "images": counts[label],
            "artist_groups": sum(bool(group & artists[label]) for group in components),
            "artist_unknown": missing_artist[label],
        }
        for label in outcomes
    }

    classes = {left, right, "neither"}
    grouped = [
        [(post_id, label) for post_id, label, ids in observations
         if label in classes and ids & group]
        for group in components
    ]
    return support, sorted(group for group in grouped if group)


def split_examples(groups, classes, *, train_minimum, holdout_minimum):
    counts = [dict(Counter(label for _, label in group)) for group in groups]
    split = grouped_partition(counts, classes, train_minimum=train_minimum,
                              holdout_minimum=holdout_minimum)
    if split["state"] == "split_available":
        holdout = set(split["holdout_group_indexes"])
        split["train_examples"] = sorted(example for i, group in enumerate(groups)
                                         if i not in holdout for example in group)
        split["holdout_examples"] = sorted(example for i, group in enumerate(groups)
                                           if i in holdout for example in group)
    return counts, split


async def missing_counterpart(db, left, right):
    """Find images where a negative judgment leaves the paired class unknown."""
    rows = (await db.execute(text(f"""
        SELECT g.tag, g.post_id
        FROM tagger_gt_label g JOIN post p ON p.id=g.post_id
        WHERE g.tag IN (:left,:right) AND g.verdict='negative'
          AND g.derived_from='human_correction' AND p.status='ok' AND p.type='image'
          AND {training_label('g')}
          AND NOT EXISTS (
              SELECT 1 FROM tagger_gt_label other
              WHERE other.post_id=g.post_id AND other.tag IN (:left,:right)
                AND other.tag<>g.tag AND other.derived_from='human_correction'
                AND other.verdict IN ('positive','negative')
          )
        ORDER BY g.tag,g.post_id
    """), {"left": left, "right": right})).all()
    missing = {left: [], right: []}
    for judged_tag, post_id in rows:
        missing[right if judged_tag == left else left].append(post_id)
    return missing


async def review_candidates(db, left, right, *, limit=12):
    """Find fresh review images from distinct artists for each sampling source."""
    context_tags = list(PAIR_CONTEXT_TAGS) if {left, right} == set(JOINT_REVIEW_PAIR) else []
    rows = (await db.execute(text(f"""
        WITH targets AS (
            SELECT tag_id, name FROM tag_name WHERE ord=0 AND name IN (:left,:right)
        ), related AS (
            SELECT DISTINCT CASE WHEN r.a_tag_id=t.tag_id THEN r.b_tag_id ELSE r.a_tag_id END AS tag_id
            FROM targets t JOIN tag_relation r ON t.tag_id IN (r.a_tag_id,r.b_tag_id)
            WHERE r.kind='related'
        ), sources AS (
            SELECT tag_id,name FROM targets
            UNION ALL
            SELECT tag_id,'related' FROM related WHERE tag_id NOT IN (SELECT tag_id FROM targets)
            UNION ALL
            SELECT tag_id,'context' FROM tag_name WHERE ord=0 AND name=ANY(:context_tags)
              AND tag_id NOT IN (SELECT tag_id FROM targets UNION SELECT tag_id FROM related)
        ), seen AS MATERIALIZED (
            SELECT post_id FROM tagger_gt_label WHERE tag IN (:left,:right)
            UNION
            SELECT m.post_id FROM tagger_review_set_member m JOIN tagger_review_set q USING (queue_sha256)
            WHERE q.tag_id IN (SELECT tag_id FROM targets)
            UNION
            SELECT post_id FROM post_tag_pin WHERE tag_id IN (SELECT tag_id FROM targets)
        ), fresh AS (
            SELECT DISTINCT source.name, sample.post_id
            FROM sources source
            CROSS JOIN LATERAL (
                SELECT pt.post_id FROM post_tag pt JOIN post p ON p.id=pt.post_id
                WHERE pt.tag_id=source.tag_id AND p.status='ok' AND p.type='image'
                  AND {not_excluded('p.id')} AND pt.post_id NOT IN (SELECT post_id FROM seen)
                  AND (source.name NOT IN ('related','context') OR NOT EXISTS (
                      SELECT 1 FROM post_tag paired
                      WHERE paired.post_id=pt.post_id AND paired.tag_id IN (SELECT tag_id FROM targets)
                  ))
                ORDER BY pt.post_id DESC LIMIT 400
            ) sample
        )
        SELECT f.name, f.post_id, artists.ids
        FROM fresh f CROSS JOIN LATERAL (
            SELECT array_agg(DISTINCT pt.tag_id ORDER BY pt.tag_id) AS ids
            FROM post_tag pt JOIN tag t ON t.id=pt.tag_id
            JOIN tag_category c ON c.id=t.category_id AND c.name='artist'
            WHERE pt.post_id=f.post_id
        ) artists
        WHERE artists.ids IS NOT NULL
        ORDER BY f.name, f.post_id DESC
    """), {"left": left, "right": right, "context_tags": context_tags})).all()
    selected = {source: [] for source in (left, right, "related", "context")}
    used_artists = {source: set() for source in selected}
    for source, post_id, artist_ids in rows:
        if len(selected[source]) == limit or used_artists[source].intersection(artist_ids):
            continue
        selected[source].append({"post_id": post_id, "artist_ids": artist_ids})
        used_artists[source].update(artist_ids)
    return selected


async def paired_review_batch(db, left, right, *, limit=12, deficits=None):
    """Prepare two ordinary review requests over the same immutable displays."""
    candidates = await review_candidates(db, left, right)
    def interleave(sources):
        return [row["post_id"] for batch in zip_longest(*(candidates[key] for key in sources))
                for row in batch if row is not None]

    ranked = []
    if deficits:
        preferred = [source for source, label in (
            ("context", "neither"), (left, left), (right, right),
        ) if deficits[label] > 0]
        ranked = interleave(preferred)
    pool = list(dict.fromkeys(ranked + interleave(candidates)))
    if not pool:
        return {}
    rows = (await db.execute(text("""
        SELECT p.id,
               encode(CASE WHEN p.media_location='pg' AND octet_length(p.data)<=16777216
                           THEN sha256(p.data) END, 'hex') AS inline_sha,
               o.sha256 AS rendition_sha, o.mime, o.byte_size
        FROM post p
        LEFT JOIN post_rendition r ON r.post_id=p.id AND r.kind='view'
        LEFT JOIN rendition_object o ON o.sha256=r.sha256
        WHERE p.id=ANY(:ids)
    """), {"ids": pool})).mappings().all()
    images = {row["id"]: row for row in rows}
    hashes = {}
    for post_id in pool:
        image = images[post_id]
        if (image["byte_size"] or 0) > 16 * 1024 * 1024:
            continue
        try:
            verified = (await asyncio.wait_for(asyncio.to_thread(
                rendition_hash, image["rendition_sha"], image["mime"]), 8)
                if image["rendition_sha"] is not None else None)
        except (KuraError, TimeoutError):
            continue
        verified = verified or image["inline_sha"]
        if verified:
            hashes[str(post_id)] = verified
        if len(hashes) == limit:
            break
    if not hashes:
        return {}
    ids = list(map(int, hashes))
    frame = paired_review_frame((left, right), ids, hashes)
    incoming = {}
    for tag in (left, right):
        document = {"schema": "human-review-set/2", "tag": tag,
                    "purpose": "historical_label_recheck", "frame_sha256": frame,
                    "model_sha256": None, "post_ids": ids, "media_sha256": hashes,
                    "source_provenance": {"kind": "historical_human_training",
                                          "artifact_sha256": frame}}
        incoming[digest(document) + ".json"] = json.dumps(
            document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return incoming


def grouped_support_requirement(groups, classes, *, train_minimum, holdout_minimum):
    """Report deficits against current policy before attempting grouped training."""
    counts = Counter()
    for group in groups:
        counts.update(group)
    required = train_minimum + holdout_minimum
    deficits = {label: max(0, required - counts[label]) for label in classes}
    return {"per_class_train_minimum": train_minimum, "per_class_holdout_minimum": holdout_minimum,
            "minimum_extra_examples_before_split": deficits}


def grouped_partition(groups, classes, *, train_minimum, holdout_minimum):
    """Find a feasible three-class split of whole artist components."""
    vectors = [tuple(group.get(label, 0) for label in classes) for group in groups]
    totals = tuple(map(sum, zip(*vectors))) if vectors else (0,) * len(classes)
    if any(n < train_minimum + holdout_minimum for n in totals):
        return {"state": "insufficient_class_support"}
    upper = tuple(n - train_minimum for n in totals)
    states = {(0,) * len(classes): ()}
    for index, vector in enumerate(vectors):
        for counts, held in list(states.items()):
            candidate = tuple(a + b for a, b in zip(counts, vector))
            if candidate in states or any(value > limit for value, limit in zip(candidate, upper)):
                continue
            selected = (*held, index)
            if all(value >= holdout_minimum for value in candidate):
                return {
                    "state": "split_available",
                    "holdout_group_indexes": selected,
                    "holdout": dict(zip(classes, candidate)),
                    "train": dict(zip(classes, (n - held_count for n, held_count in zip(totals, candidate)))),
                }
            states[candidate] = selected
        if len(states) > 100_000:
            return {"state": "split_search_budget_exhausted"}
    return {"state": "artist_group_split_unavailable"}


async def joint_minima(db):
    floors = dict((await db.execute(text("""
        SELECT key,ceil(value::numeric)::int FROM tagger_config
        WHERE key IN ('min_gt_positive','min_val_pos_support')
    """))).all())
    return {"train_minimum": floors["min_gt_positive"],
            "holdout_minimum": floors["min_val_pos_support"]}


async def run(left, right, *, show_examples=False, suggest=False, prepare_review=False):
    from api.db.session import async_session

    async with async_session() as db:
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        await db.execute(text("SET LOCAL statement_timeout='10s'"))
        support, groups = await paired_evidence(db, left, right)
        missing = await missing_counterpart(db, left, right)
        classes = (left, right, "neither")
        minima = await joint_minima(db)
        counts, split = split_examples(groups, classes, **minima)
        requirement = grouped_support_requirement(counts, classes, **minima)
        if not show_examples:
            split.pop("train_examples", None)
            split.pop("holdout_examples", None)
        result = {"support": support, "artist_group_class_counts": counts,
                  "review_post_ids": missing, "grouped_requirement": requirement,
                  "artist_group_split": split}
        if suggest and split["state"] != "split_available":
            result["suggested_review_candidates"] = await review_candidates(db, left, right)
        if prepare_review and split["state"] != "split_available":
            incoming = await paired_review_batch(db, left, right)
            result["prepared_review_sets"] = [
                {"tag": row["tag"], "queue_sha256": row["queue_sha256"], "images": len(row["post_ids"])}
                for row in parse_queues(incoming, canonical=True)
            ]
        print(json.dumps(result, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left")
    parser.add_argument("right")
    parser.add_argument("--show-examples", action="store_true")
    parser.add_argument("--suggest", action="store_true")
    parser.add_argument("--prepare-review", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.left, args.right, show_examples=args.show_examples, suggest=args.suggest,
                    prepare_review=args.prepare_review))


if __name__ == "__main__":
    main()
