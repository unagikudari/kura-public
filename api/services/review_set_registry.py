"""Canonical request bytes and normalized membership, not a second label store."""
import hashlib
import json
import os

from sqlalchemy import text
from api.services.review_queues import parse_queues, ReviewQueueRefused


async def available(db):
    exists=(await db.execute(text(
        "SELECT to_regclass('public.tagger_review_registry_activation') IS NOT NULL"
    ))).scalar_one()
    return exists and (await db.execute(text(
        "SELECT EXISTS(SELECT FROM tagger_review_registry_activation)"
    ))).scalar_one()


async def blobs(db):
    """Read the bounded legacy inventory, including supersession predecessors."""
    rows = (await db.execute(text("""
        SELECT queue_sha256, document FROM tagger_review_set
        WHERE convert_from(document, 'UTF8')::jsonb->>'schema' <> 'human-review-set/3'
        ORDER BY queue_sha256
    """))).all()
    return {row[0] + ".json": bytes(row[1]) for row in rows}



async def catalog_available(db):
    return await db.scalar(text("SELECT to_regclass('public.tagger_review_current_set') IS NOT NULL"))


async def catalog_sets(db, ref=None):
    """Reconstruct the request from typed metadata and normalized membership."""
    from urllib.parse import urlencode

    direct = "" if ref is not None else """
        WHERE q.schema_version<>3 OR q.queue_sha256 IN (
            SELECT DISTINCT ON (tag_id,purpose) queue_sha256 FROM tagger_review_set
            WHERE schema_version=3 ORDER BY tag_id,purpose,created_at DESC,queue_sha256)
    """
    selected = "WHERE q.queue_sha256=:ref" if ref is not None else direct
    rows = (await db.execute(text(f"""
        SELECT q.queue_sha256,q.frozen_tag_name AS tag,q.purpose,q.schema_version,q.frame_sha256,
               q.model_sha256,q.declared_origin,q.provenance_kind,q.artifact_sha256,q.snapshot_sha256,
               q.measurement_sha256,q.supersedes_queue_sha256,
               (SELECT r.queue_sha256 FROM tagger_review_set r
                 WHERE r.supersedes_queue_sha256=q.queue_sha256) AS superseded_by,
               ARRAY(SELECT m.post_id FROM tagger_review_set_member m WHERE m.queue_sha256=q.queue_sha256
                     ORDER BY m.ordinal) AS post_ids,
               (SELECT jsonb_object_agg(m.post_id,m.display_sha256) FROM tagger_review_set_member m
                  WHERE m.queue_sha256=q.queue_sha256 AND m.display_sha256 IS NOT NULL) AS media_sha256
          FROM tagger_review_set q {selected}
          ORDER BY q.queue_sha256
    """), {"ref": ref} if ref is not None else {})).mappings().all()
    queues = []
    for row in rows:
        q = {key: row[key] for key in ("queue_sha256","tag","purpose","frame_sha256","model_sha256","post_ids")}
        q["schema"] = f"human-review-set/{row['schema_version']}"
        if row["superseded_by"]:
            q["superseded_by"] = row["superseded_by"]
        if row["schema_version"] > 1:
            q["source_provenance"] = {"kind": row["provenance_kind"]}
            for key in ("artifact_sha256","snapshot_sha256","measurement_sha256","supersedes_queue_sha256"):
                if row[key] is not None:
                    q["source_provenance"][key] = row[key]
            if row["declared_origin"] is not None:
                q["source_provenance"]["origin"] = row["declared_origin"]
            q["media_sha256"] = row["media_sha256"] or {}
            q["browse_url"] = q["unjudged_url"] = "/tagger-review/" + row["queue_sha256"]
        else:
            query = "ids:" + ",".join(map(str, row["post_ids"]))
            def href(search):
                return "/?" + urlencode({"q": search, "bulk": "1", "bulkTag": row["tag"]})
            q["browse_url"] = href(query)
            q["unjudged_url"] = href(query + f" -locked:{row['tag']} -removed:{row['tag']}")
        queues.append(q)
    return queues


async def load(db):
    """Show legacy queues and the latest direct request per tag and purpose: a base
    comparison set never hides a dedicated candidate's publication review."""
    if await catalog_available(db):
        return await catalog_sets(db)
    legacy = await blobs(db)
    rows = (await db.execute(text("""
        SELECT DISTINCT ON (tag_id, purpose) queue_sha256, document
        FROM tagger_review_set
        WHERE convert_from(document, 'UTF8')::jsonb->>'schema' = 'human-review-set/3'
        ORDER BY tag_id, purpose, created_at DESC, queue_sha256
    """))).all()
    current = {row[0] + ".json": bytes(row[1]) for row in rows}
    return parse_queues(legacy | current, canonical=True)


async def by_ref(db, ref):
    """Resolve archived direct candidates without loading other candidates."""
    if await catalog_available(db):
        return await catalog_sets(db, ref)
    raw = (await db.execute(text("""
        SELECT document FROM tagger_review_set
        WHERE queue_sha256 = :ref
          AND convert_from(document, 'UTF8')::jsonb->>'schema' = 'human-review-set/3'
    """), {"ref": ref})).scalar_one_or_none()
    if raw is not None:
        return parse_queues({ref + ".json": bytes(raw)})
    return parse_queues(await blobs(db), canonical=True)


async def require_review_variant_eligibility(db, queue):
    from api.tagger_core.review_censor_variants import review_candidate
    from api.services.review_adjudication import is_adjudication

    if is_adjudication(queue):
        return
    blocked = await db.scalar(text(
        "SELECT EXISTS(SELECT 1 FROM unnest(CAST(:ids AS integer[])) AS v(post_id) WHERE NOT "
        + review_candidate("v.post_id", ":tag") + ")"
    ), {"ids":queue["post_ids"], "tag":queue["tag"]})
    if blocked:
        raise ReviewQueueRefused("review_prefer_uncensored_variant")


async def import_sets(db, incoming):
    """Import a complete validated set atomically; callers own commit/rollback.

    Existing references and response bindings remain byte-identical. Importing
    requests does not write answers, pins, GT, lifecycle events or policies.
    """
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('tagger_review_set_registry',0))"))
    existing = await blobs(db)
    for name, raw in incoming.items():
        if name in existing and existing[name] != raw:
            raise ReviewQueueRefused("registry_identity_changed")
    combined = existing | incoming
    queues = parse_queues(combined, canonical=True)
    # v1 predecessors must precede their v2 bindings in the same transaction.
    for q in sorted(queues,key=lambda q:q["schema"]):
        if q["queue_sha256"]+".json" in existing:
            continue
        await require_review_variant_eligibility(db,q)
        await db.execute(text("SELECT tagger_register_review_set(:raw)"),
                         {"raw":combined[q["queue_sha256"]+".json"]})
    await db.execute(text("SELECT tagger_activate_review_registry()"))
    return {"state":"registered","sets":len(queues),"inserted":len(combined)-len(existing)}


async def load_configured(db):
    """Bridge until an import commits activation; afterward never consult queue files."""
    if await available(db):
        return await load(db)
    if await catalog_available(db) or os.environ.get("KURA_DB_ROLE", "canonical") == "replica":
        return None
    import asyncio
    from api.services.review_queues import load_queues
    return await asyncio.to_thread(load_queues)


async def register_candidate_set(db, document):
    """Register one direct candidate request after the legacy import cutover."""
    if not await available(db):
        raise ReviewQueueRefused("review_registry_inactive")
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"),
                     allow_nan=False).encode("utf-8")
    ref = hashlib.sha256(raw).hexdigest()
    queue, = parse_queues({ref + ".json": raw})
    if queue["schema"] != "human-review-set/3":
        raise ReviewQueueRefused("direct_candidate_required")
    already_registered = await db.scalar(text(
        "SELECT EXISTS(SELECT 1 FROM tagger_review_set WHERE queue_sha256=:ref)"
    ), {"ref":ref})
    if not already_registered:
        await require_review_variant_eligibility(db,queue)
    stored = (await db.execute(text("SELECT tagger_register_review_set(:raw)"),
                               {"raw": raw})).scalar_one()
    if stored != ref:
        raise ReviewQueueRefused("registry_identity_changed")
    return ref


async def evaluation_answers(db, queue_sha256):
    """Read the latest explicit answers and their immutable revision identity."""
    from api.services.human_review import digest

    if not await available(db):
        return None
    rows = (await db.execute(text("""
        SELECT q.model_sha256, q.frame_sha256, q.purpose, n.name AS tag, m.post_id, m.display_sha256,
               a.id AS event_id, a.decision, a.display_sha256 AS judged_display
        FROM tagger_review_set q
        JOIN tagger_review_set_member m USING (queue_sha256)
        JOIN tag_name n ON n.tag_id=q.tag_id AND n.ord=0
        LEFT JOIN LATERAL (
            SELECT id, decision, source_context->'observed'->>'display_sha256' AS display_sha256
            FROM tagger_human_review_event
            WHERE queue_sha256 = m.queue_sha256 AND post_id = m.post_id
            ORDER BY id DESC LIMIT 1
        ) a ON true
        WHERE q.queue_sha256 = :ref
          AND q.purpose IN ('candidate_evaluation', 'production_precision', 'base_comparison')
          AND q.model_sha256 IS NOT NULL
        ORDER BY m.ordinal
    """), {"ref": queue_sha256})).mappings().all()
    if not rows:
        return None
    answers = []
    for row in rows:
        if row["display_sha256"] is None:
            raise ReviewQueueRefused("evaluation_member_missing_display_hash")
        if row["event_id"] is not None and row["judged_display"] != row["display_sha256"]:
            raise ReviewQueueRefused("evaluation_answer_display_mismatch")
        answers.append({
            "post_id": row["post_id"], "display_sha256": row["display_sha256"],
            "event_id": row["event_id"], "decision": row["decision"],
        })
    from api.services.review_adjudication import resolution
    if rows[0]["purpose"] == "candidate_evaluation":
        await resolution(db, queue_sha256, rows[0]["tag"], answers)
    snapshot = {
        "schema": "tagger-review-answers/1", "queue_sha256": queue_sha256,
        "model_sha256": rows[0]["model_sha256"], "frame_sha256": rows[0]["frame_sha256"],
        "answers": answers,
    }
    return {**snapshot, "sha256": digest(snapshot)}


async def lock_evaluation_answers(db, tag):
    """Serialize a tag's evaluation and adjudication writes with publication."""
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                     {"key": "tagger-evaluation-tag:" + tag})


async def require_current_evaluation_answers(db, queue_sha256, answers_sha256):
    """Call inside the publication transaction before writing the active policy."""
    isolation = (await db.execute(text("SHOW transaction_isolation"))).scalar_one()
    if isolation != "read committed":
        raise ReviewQueueRefused("evaluation_transaction_isolation_unsupported")
    tag = (await db.execute(text(
        "SELECT n.name FROM tagger_review_set q JOIN tag_name n ON n.tag_id=q.tag_id "
        "AND n.ord=0 WHERE q.queue_sha256=:ref"
    ), {"ref": queue_sha256})).scalar_one_or_none()
    if tag is None:
        raise ReviewQueueRefused("evaluation_answers_stale")
    await lock_evaluation_answers(db, tag)
    answers = await evaluation_answers(db, queue_sha256)
    if answers is None or answers["sha256"] != answers_sha256:
        raise ReviewQueueRefused("evaluation_answers_stale")
    if any(row["decision"] not in ("present", "absent") for row in answers["answers"]):
        raise ReviewQueueRefused("evaluation_answers_unresolved")
    return answers


async def candidates_for_release_review(db, queue_refs):
    """Expose the next action only for a current, measured candidate."""
    if not queue_refs or not (await db.execute(text(
        "SELECT to_regclass('public.tagger_candidate_evaluation') IS NOT NULL"
    ))).scalar_one():
        return []
    from api.services.qualification_evidence import runtime_report_coverage
    from api.tagger_core.config import load_config
    from api.tagger_core.registration_review import review_current_registration

    candidates = (await db.execute(text("""
        SELECT r.id, r.tag, r.metrics, r.gt, r.artifact_sha256
        FROM tagger_registration_request r
        WHERE r.status = 'pending' AND EXISTS (
            SELECT 1 FROM tagger_candidate_evaluation e
            WHERE e.registration_id = r.id AND e.queue_sha256 = ANY(:queues)
        ) AND r.id = (
            SELECT newest.id FROM tagger_registration_request newest
            WHERE newest.tag_id = r.tag_id
            ORDER BY newest.requested_at DESC, newest.id DESC LIMIT 1
        )
        ORDER BY r.id
    """), {"queues": queue_refs})).mappings().all()
    if not candidates:
        return []
    config = await load_config(db)
    ready = []
    for candidate in candidates:
        decision = await review_current_registration(db, candidate, config)
        if decision.reason != 'awaiting_production_scope_and_fleet_qualification':
            continue
        coverage = await runtime_report_coverage(db, candidate["id"])
        if coverage["all_workers_report_pass"]:
            ready.append({"registration_id": candidate["id"], "tag": candidate["tag"]})
    return ready


async def legacy_lora_refs(db):
    """Read the canonical origin annotations when the capacity migration is present."""
    if await catalog_available(db):
        return set((await db.execute(text(
            "SELECT queue_sha256 FROM tagger_review_set WHERE origin='lora_findings'"
        ))).scalars())
    relation = (await db.execute(text(
        "SELECT to_regclass('public.tagger_review_lora_origin') IS NOT NULL"
    ))).scalar_one()
    if not relation:
        return None
    rows = await db.execute(text("SELECT queue_sha256 FROM tagger_review_lora_origin"))
    return set(rows.scalars())
