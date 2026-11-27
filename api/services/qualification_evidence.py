"""Receipt intake, not an admission writer. Caller owns commit/rollback."""
from sqlalchemy import text
from api.tagger_core import qualification_evidence as contract


async def registration_context(db, registration_id, *, lock=False):
    if type(registration_id) is not int or not 1 <= registration_id <= 2**63-1:
        raise contract.QualificationEvidenceRefused('qualification_registration_id_invalid')
    row = (await db.execute(text(
        'SELECT id, tag, tag_id, lifecycle_event_id, artifact_sha256, metrics, gt '
        'FROM tagger_registration_request WHERE id=:id' + (' FOR SHARE' if lock else '')),
        {'id': registration_id})).mappings().one_or_none()
    if row is None:
        raise contract.QualificationEvidenceRefused('qualification_registration_missing')
    return contract.context(row)


async def lock_runtime_evidence(db, registration_id):
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                     {"key": f"tagger-runtime-evidence:{registration_id}"})


async def attach(db, registration_id, *, expected_context_sha256, kind, report, user_id):
    if type(user_id) is not int or user_id <= 0:
        raise contract.QualificationEvidenceRefused('qualification_submitter_invalid')
    await db.execute(text("SET LOCAL lock_timeout='2s'"))
    await db.execute(text("SET LOCAL statement_timeout='10s'"))
    if kind in ("runtime", "independent_review"):
        await lock_runtime_evidence(db, registration_id)
    context = await registration_context(db, registration_id, lock=True)
    envelope = contract.assemble(context, expected_context_sha256, kind, report)
    if kind == 'independent_review':
        refs = report['evidence_sha256s']
        rows = (await db.execute(text(
            'SELECT receipt_sha256 FROM tagger_qualification_evidence '
            'WHERE receipt_sha256=ANY(:refs) AND registration_id=:id AND context_sha256=:context'),
            {'refs': refs, 'id': registration_id, 'context': envelope['context_sha256']})).scalars().all()
        if set(rows) != set(refs):
            raise contract.QualificationEvidenceRefused('qualification_review_evidence_mismatch')
    # Repeated placeholders are explicitly text before the jsonb cast: asyncpg
    # must not infer the same parameter once as text and once as jsonb.
    await db.execute(text(
        'INSERT INTO tagger_qualification_evidence '
        '(receipt_sha256,registration_id,context_sha256,context_canonical,context,canonical_body,body,submitted_by) '
        'VALUES (:ref,:id,:ctx,CAST(:context AS text),CAST(CAST(:context AS text) AS jsonb),'
        'CAST(:wire AS text),CAST(CAST(:wire AS text) AS jsonb),:uid) ON CONFLICT DO NOTHING'),
        {'ref': envelope['receipt_sha256'], 'id': registration_id, 'ctx': envelope['context_sha256'],
         'context': envelope['context_canonical'], 'wire': envelope['canonical_body'], 'uid': user_id})
    row = (await db.execute(text(
        'SELECT canonical_body,context_canonical,submitted_by,received_at '
        'FROM tagger_qualification_evidence WHERE receipt_sha256=:ref'),
        {'ref': envelope['receipt_sha256']})).mappings().one()
    if row['canonical_body'] != envelope['canonical_body'] or row['context_canonical'] != envelope['context_canonical']:
        raise contract.QualificationEvidenceRefused('qualification_receipt_collision')
    return dict(outcome='succeeded', state='evidence_received_unverified',
                reason='caller_assertion_bound_to_original_measurement',
                next='review_provenance_population_and_execution_scope_before_admission',
                receipt_sha256=envelope['receipt_sha256'], registration_id=registration_id,
                context_sha256=envelope['context_sha256'], first_submitted_by=row['submitted_by'],
                first_received_at=row['received_at'], **contract.admission_status())


PLAN_IDENTITY = ("source_sha256", "inventory_sha256", "inputs_sha256")


async def runtime_report_coverage(db, registration_id, *, runtime_plan=None):
    """Summarize candidate assertions against an optional frozen rollout plan."""
    plan_fields = {
        "source_sha256", "inventory_sha256", "inputs_sha256", "device", "provider",
        "memory_limit_bytes", "threads", "deadline_s", "total",
    }
    context = await registration_context(db, registration_id)
    context_sha = contract.digest(context)
    rows = (await db.execute(text("""
        SELECT e.host, e.profile, e.expected_provider, r.body, r.receipt_sha256
        FROM tagger_expected_worker e
        LEFT JOIN LATERAL (
            SELECT q.body, q.receipt_sha256
            FROM tagger_qualification_evidence q
            WHERE q.registration_id = :id AND q.context_sha256 = :context
              AND q.body->>'kind' = 'runtime'
              AND q.body->'report'->>'host' = e.host
              AND q.body->'report'->>'profile' = e.profile
            ORDER BY q.received_at DESC, q.receipt_sha256
            LIMIT 1
        ) r ON true
        WHERE NOT e.opportunistic
        ORDER BY e.host
    """), {"id": registration_id, "context": context_sha})).mappings().all()
    if runtime_plan is not None:
        contract.require(
            set(runtime_plan) == {(r["host"], r["profile"]) for r in rows}
            and all(isinstance(pin, dict) and set(pin) == plan_fields
                    for pin in runtime_plan.values()),
            "runtime_plan_inventory_mismatch",
        )
        # Judge each worker by its newest receipt for the same plan identity
        # (source tree, head inventory and inputs). Receipts for another plan
        # say nothing about this one. Within the same identity the newest
        # receipt counts, so a later failure or runtime drift (provider,
        # device, threads, memory) still shows as failure or mismatch below.
        # Without such a receipt, the newest of any plan is reported as before.
        import json
        rows = [dict(row) for row in rows]
        for row in rows:
            match = (await db.execute(text("""
                SELECT q.body, q.receipt_sha256
                FROM tagger_qualification_evidence q
                WHERE q.registration_id = :id AND q.context_sha256 = :context
                  AND q.body->>'kind' = 'runtime'
                  AND q.body->'report'->>'host' = :host
                  AND q.body->'report'->>'profile' = :profile
                  AND q.body->'report' @> CAST(:pins AS jsonb)
                ORDER BY q.received_at DESC, q.receipt_sha256
                LIMIT 1
            """), {"id": registration_id, "context": context_sha, "host": row["host"],
                   "profile": row["profile"],
                   "pins": json.dumps({key: runtime_plan[(row["host"], row["profile"])][key]
                                       for key in PLAN_IDENTITY})})).mappings().one_or_none()
            if match is not None:
                row.update(body=match["body"], receipt_sha256=match["receipt_sha256"])
    workers = []
    for row in rows:
        state = "missing"
        if row["body"] is not None:
            report = row["body"]["report"]
            envelope = contract.assemble(context, context_sha, "runtime", report)
            contract.require(envelope["receipt_sha256"] == row["receipt_sha256"],
                             "runtime_report_digest_mismatch")
            state = "reported_pass" if (
                report["parity_passed"] is True
                and report["completed"] == report["total"]
                and report["oom_events"] == 0
                and 0 < report["peak_bytes"] <= report["memory_limit_bytes"]
                and report["elapsed_s"] <= report["deadline_s"]
                and (row["expected_provider"] is None or report["provider"] == row["expected_provider"])
            ) else "reported_failure"
            if runtime_plan is not None:
                pin = runtime_plan[(row["host"], row["profile"])]
                if any(report[field] != expected for field, expected in pin.items()):
                    state = "reported_configuration_mismatch"
        workers.append({"host": row["host"], "profile": row["profile"],
                        "state": state, "receipt_sha256": row["receipt_sha256"]})
    return {
        "workers": workers,
        "all_workers_report_pass": bool(workers) and all(
            row["state"] == "reported_pass" for row in workers),
        "provenance": "caller_assertion",
        "publication_eligible": False,
    }
