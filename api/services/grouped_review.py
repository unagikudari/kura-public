"""Operator-configured grouped review, preserving each queue's judgment contract."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import text

from api.services import human_review as review
from api.services.review_adjudication import is_adjudication
from api.services.review_reselection import retired_refs
from api.services.review_set_registry import load_configured
from api.services.review_tag_contract_v1 import valid as valid_tag

REF = re.compile(r"[0-9a-f]{64}")


def read_config():
    path = Path(os.environ.get(
        "KURA_GROUPED_REVIEW_PATH", "~/.local/state/kura/grouped-review.json")).expanduser()
    try:
        with path.open("rb") as source:
            raw = source.read(65537)
    except FileNotFoundError:
        return None
    if len(raw) > 65536:
        review.refuse("grouped_review_config_invalid")
    doc = json.loads(raw)
    if doc.get("schema") != "tagger-grouped-review/1":
        review.refuse("grouped_review_config_invalid")
    return doc


def expand(group, selected):
    tags = group["tags"]
    chosen = set(selected)
    if (len(chosen) != len(selected) or chosen - set(tags)
            or group["mode"] not in ("single", "multiple")):
        review.refuse("grouped_review_selection_invalid")
    implications = group.get("implies", {})
    if set(implications) - set(tags) or any(set(targets) - set(tags) for targets in implications.values()):
        review.refuse("grouped_review_config_invalid")
    while True:
        following = chosen.union(*(implications.get(tag, []) for tag in chosen))
        if following == chosen:
            break
        chosen = following
    if (group["mode"] == "single" and len(chosen) > 1
            or any(len(chosen.intersection(pair)) > 1 for pair in group.get("exclusive", []))):
        review.refuse("grouped_review_selection_conflict")
    return chosen


async def linked_queues(db, ref):
    config = await asyncio.to_thread(read_config)
    if config is None:
        return None
    batches = [batch for batch in config["batches"] if ref in batch["queues"].values()]
    if not batches:
        return None
    if len(batches) != 1:
        review.refuse("grouped_review_config_invalid")
    batch = batches[0]
    group = config["groups"][batch["group"]]
    tags = group["tags"]
    refs = list(batch["queues"].values())
    if (not 2 <= len(tags) <= 16 or len(set(tags)) != len(tags)
            or any(not valid_tag(tag) for tag in tags)
            or not 2 <= len(refs) == len(set(refs))
            or any(not REF.fullmatch(ref) for ref in refs)
            or set(batch["queues"]) - set(tags)
            or any(set(pair) - set(tags) for pair in group.get("exclusive", []))):
        review.refuse("grouped_review_config_invalid")
    queues = {q["queue_sha256"]: q for q in await load_configured(db) or []}
    retired = await retired_refs(db) or set()
    targets = {}
    for tag, target in batch["queues"].items():
        q = queues.get(target)
        if (not q or q["tag"] != tag or target in retired or q.get("superseded_by")
                or is_adjudication(q)
                or (q["schema"], q["purpose"]) not in (
                    ("human-review-set/2", "historical_label_recheck"),
                    ("human-review-set/3", "base_comparison"))):
            review.refuse("grouped_review_queue_invalid")
        targets[tag] = q
    if len({q["schema"] for q in targets.values()}) != 1:
        review.refuse("grouped_review_mixed_purpose")
    return (batch["group"],group,targets)


async def contexts(db, linked, ref, pid, *, lock=False):
    key, group, all_targets = linked
    active = {tag: q for tag, q in all_targets.items() if pid in q["post_ids"]}
    anchor = next(q for q in all_targets.values() if q["queue_sha256"] == ref)
    if anchor["tag"] not in active:
        review.refuse("grouped_review_post_outside_queue")
    hashes = {q["media_sha256"][str(pid)] for q in active.values()}
    if len(hashes) != 1:
        review.refuse("grouped_review_media_mismatch")
    found = {}
    for tag, queue in sorted(active.items()):
        if pid not in await review.reviewable_post_ids(db, queue):
            review.refuse("grouped_review_post_unavailable")
        found[tag] = await review.context(db, queue, pid, lock=lock)
    token = review.digest({"key":key,"policy":group,"ref":ref,"post":pid,
                            "queues":{tag: q["queue_sha256"] for tag,q in active.items()},
                            "contexts":{tag: ctx["context_sha256"] for tag,ctx in found.items()}})
    return active, found, token


async def describe(db, linked, ref, pid):
    key, group, _ = linked
    active, found, token = await contexts(db, linked, ref, pid)
    definitions = dict((await db.execute(text("""
        SELECT n.name,t.description FROM tag_name n JOIN tag t ON t.id=n.tag_id
        WHERE n.ord=0 AND n.name=ANY(:tags)
    """), {"tags":group["tags"]})).all())
    if set(definitions)!=set(group["tags"]):
        review.refuse("grouped_review_tag_missing")
    return {"key":key,"mode":group["mode"],"exclusive":group.get("exclusive",[]),
            "context_sha256":token,
            "options":[{"tag":tag,"definition":(definitions[tag] or "")[:180],
                        "target":tag in active,
                        "answered":tag in found and found[tag]["previous_judgment"] is not None}
                       for tag in group["tags"]]}


def request_for(operation, tag):
    return str(uuid5(NAMESPACE_URL,f"kura-group-review/{operation}/{tag}"))


async def submit(db, linked, ref, pid, body, user):
    await review.bounded_transaction(db)
    _, group, all_targets = linked
    request = body["request_id"]
    identity = review.digest({"ref":ref,"post_id":pid,**body})
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                     {"key":f"group-review:{user.user_id}:{request}"})
    previous = (await db.execute(text("""
        SELECT id,decision,source_context->'group_review'->>'request_sha256' AS identity
        FROM tagger_human_review_event
        WHERE actor_id=:user AND request_id=ANY(CAST(:keys AS uuid[]))
    """), {"user":user.user_id, "keys":[request_for(request,tag) for tag in all_targets]})).mappings().all()
    if previous:
        if any(row["identity"] != identity for row in previous):
            review.refuse("grouped_review_request_reused")
        return {"outcome":"succeeded","recorded":len(previous),"replayed":True}
    selected = expand(group, body["selected"])
    if (body["answer"] == "selected") != bool(selected):
        review.refuse("grouped_review_selection_invalid")
    # Queue/tag locks precede the post lock, matching the ordinary answer path.
    from api.services.review_reselection import lock_queue
    from api.services.review_set_registry import lock_evaluation_answers
    for tag, q in sorted(all_targets.items()):
        if pid not in q["post_ids"]:
            continue
        if q["schema"] == "human-review-set/2":
            await lock_queue(db, q["queue_sha256"])
        else:
            await lock_evaluation_answers(db, tag)
    targets, found, token = await contexts(db, linked, ref, pid, lock=True)
    if token != body["expected_context_sha256"]:
        review.refuse("grouped_review_context_stale")
    written=0
    for tag, q in sorted(targets.items()):
        if found[tag]["previous_judgment"] is not None:
            continue
        decision = ("uncertain" if body["answer"] == "uncertain"
                    else "present" if tag in selected else "absent")
        # Earlier writes from this transaction can change the post revision.
        # All initial contexts were checked under locks before writing.
        current=await review.context(db,q,pid)
        await review.submit(db,q,pid,dict(request_id=request_for(request,tag),
                            expected_context_sha256=current["context_sha256"],decision=decision),
                            user,group_review={"request_sha256":identity})
        written+=1
    if not written:
        review.refuse("grouped_review_already_answered")
    if next(iter(targets.values()))["schema"] == "human-review-set/2":
        for tag in sorted(selected - set(targets)):
            tag_id = await db.scalar(text("SELECT tag_id FROM tag_name WHERE ord=0 AND name=:tag"),
                                     {"tag":tag})
            if tag_id is None:
                review.refuse("grouped_review_tag_missing")
            present = await db.scalar(text("SELECT EXISTS(SELECT 1 FROM post_tag WHERE post_id=:p AND tag_id=:t)"),
                                      {"p":pid,"t":tag_id})
            await review.apply_human_decision(db,dict(post_id=pid,tag_id=tag_id,tag=tag,tag_present=present),
                                              "present",user.name)
    return {"outcome":"succeeded","recorded":written,"replayed":False}


async def incomplete_posts(db, linked, ids):
    refs = [q["queue_sha256"] for q in linked[2].values()]
    rows = await db.execute(text("""
        SELECT DISTINCT m.post_id FROM tagger_review_set_member m
        WHERE m.queue_sha256=ANY(:refs) AND m.post_id=ANY(:ids)
          AND NOT EXISTS (
            SELECT 1 FROM tagger_human_review_event e
            WHERE e.queue_sha256=m.queue_sha256 AND e.post_id=m.post_id)
    """), {"refs":refs,"ids":list(ids)})
    return set(rows.scalars())
