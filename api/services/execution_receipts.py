"""Worker-reported execution facts; consistency, not hardware attestation.

No commit: caller owns the heartbeat/completion transaction. Receipt storage is
canonical-only; replicated audit references may be unresolved on subscribers.
"""
from sqlalchemy import text
from api.tagger_core import execution_receipt as contract


async def remember(db, packet, *, host, profile, release_id):
    checked = contract.verify(packet, host=host, profile=profile, release_id=release_id)
    contract.require(checked['body']['qualification_ref'] is None,
                     'execution_receipt_qualification_unregistered')
    wire = contract.canonical(checked['body']).decode('ascii')
    await db.execute(text("INSERT INTO tagger_execution_receipt (receipt_sha256, canonical_body, body) "
                          "VALUES (:ref, CAST(:wire AS text), CAST(CAST(:wire AS text) AS jsonb)) ON CONFLICT DO NOTHING"),
                     {'ref': checked['receipt_sha256'], 'wire': wire})
    stored = (await db.execute(text("SELECT canonical_body FROM tagger_execution_receipt WHERE receipt_sha256=:ref"),
                               {'ref': checked['receipt_sha256']})).scalar_one()
    contract.require(stored == wire, 'execution_receipt_collision')
    return checked['receipt_sha256']


async def for_completion(db, *, receipt_sha256, worker_id, host, release_id):
    if receipt_sha256 is None:
        return None                    # legacy unknown, never interpreted as CPU
    contract.require(isinstance(worker_id, str) and bool(worker_id), 'execution_receipt_worker_required')
    row = (await db.execute(text("SELECT host, profile, release_id, execution_receipt_sha256 "
                                "FROM tagger_worker_heartbeat WHERE worker_id=:worker FOR SHARE"),
                            {'worker': worker_id})).mappings().one_or_none()
    contract.require(row is not None and row['host'] == host and row['release_id'] == release_id
                     and row['execution_receipt_sha256'] == receipt_sha256,
                     'execution_receipt_heartbeat_mismatch')
    body = (await db.execute(text("SELECT body FROM tagger_execution_receipt WHERE receipt_sha256=:ref"),
                             {'ref': receipt_sha256})).scalar_one_or_none()
    contract.bind_completion({'receipt_sha256': receipt_sha256, 'body': body},
                             host=host, profile=row['profile'], release_id=release_id,
                             heartbeat_receipt=row['execution_receipt_sha256'])
    return receipt_sha256


async def content_head_coverage(db, *, release_id, tag, artifact_sha256, threshold,
                                apply_mode, published_at, policy_epoch=None):
    """Check completed inference on the current receipt for every required host."""
    rows = (await db.execute(text("""
        SELECT e.host, e.profile, EXISTS (
            SELECT 1
            FROM tagger_worker_heartbeat h
            JOIN tagger_content_head_execution x
              ON x.worker_id = h.worker_id
             AND x.receipt_sha256 = h.execution_receipt_sha256
            WHERE h.host = e.host AND h.profile = e.profile
              AND h.release_id = :release
              AND h.reported_at >= clock_timestamp() - interval '5 minutes'
              AND (e.expected_provider IS NULL OR h.provider = e.expected_provider)
              AND x.tag = :tag AND x.artifact_sha256 = :artifact
              AND x.threshold = :threshold AND x.apply_mode = :mode
              AND x.completed_at >= :published
              AND (CAST(:epoch AS timestamptz) IS NULL OR x.policy_epoch = :epoch)
        ) AS confirmed
        FROM tagger_expected_worker e
        WHERE NOT e.opportunistic
        ORDER BY e.host
    """), {"release": release_id, "tag": tag, "artifact": artifact_sha256,
           "threshold": threshold, "mode": apply_mode, "published": published_at, "epoch": policy_epoch})).mappings().all()
    workers = [dict(row) for row in rows]
    return {"complete": bool(workers) and all(w["confirmed"] for w in workers),
            "workers": workers}


async def published_candidate_coverage(db):
    """Return coverage only for current policies backed by a candidate review."""
    exists = (await db.execute(text(
        "SELECT to_regclass('public.tagger_content_head_execution') IS NOT NULL"
    ))).scalar_one()
    if not exists:
        return {}
    rows = (await db.execute(text("""
        SELECT p.tag, p.content_sha256, p.threshold, p.apply_mode,
               greatest(p.created_at, active.updated_at) AS published_at,
               active.release_id
        FROM tagger_current_head_policy p
        JOIN tagger_registration_request r ON r.id = p.registration_id
        CROSS JOIN tagger_release_active active
        WHERE p.artifact_kind = 'content_head_bundle'
          AND r.metrics ? 'production_review'
          AND r.artifact_sha256 = p.content_sha256
    """))).mappings().all()
    return {
        row["tag"]: await content_head_coverage(
            db, release_id=row["release_id"], tag=row["tag"],
            artifact_sha256=row["content_sha256"], threshold=row["threshold"],
            apply_mode=row["apply_mode"], published_at=row["published_at"], policy_epoch=row["published_at"],
        )
        for row in rows
    }
