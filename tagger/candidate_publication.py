"""Transaction checks for publishing a dedicated candidate."""

from sqlalchemy import text

from api.tagger_core.config import load_config
from api.tagger_core.quality_hold import held_names

from .canonical_evaluation import require_publication_review
from .head_policy import domain_gate_tags, proposed_policy
from .label_identity import label_identity, read_label_rows
from .stratified_evaluation import require


async def require_candidate_publication(db, registration_id, *, expected_release_id,
                                        expected_predecessor, answers_sha256):
    """Bind admission to the current release, head, labels and review answers."""
    release = (await db.execute(text("""
        SELECT release_id FROM tagger_release_active WHERE id = 1 FOR UPDATE
    """))).scalar_one_or_none()
    require(release is not None and release == expected_release_id,
            "publication_release_changed")
    row = (await db.execute(text("""
        SELECT r.id, r.tag, r.artifact_sha256, r.metrics, r.gt, r.tag_id,
               r.lifecycle_event_id, r.promoted_patch_id
        FROM tagger_registration_request r
        JOIN tag_name tn ON tn.tag_id = r.tag_id AND tn.ord = 0 AND tn.name = r.tag
        WHERE r.id = :id
        FOR UPDATE OF r
    """), {"id": registration_id})).mappings().one_or_none()
    require(row is not None and row["lifecycle_event_id"] is not None
            and row["promoted_patch_id"] is None, "publication_registration_unavailable")
    tag = row["tag"]
    latest = (await db.execute(text("""
        SELECT id FROM tagger_registration_request
        WHERE tag_id = :tag_id ORDER BY requested_at DESC, id DESC LIMIT 1
    """), {"tag_id": row["tag_id"]})).scalar_one()
    require(latest == registration_id, "publication_candidate_superseded")
    names = (await db.execute(text("SELECT name FROM tag_name WHERE tag_id = :id"),
                              {"id": row["tag_id"]})).scalars().all()
    require(not set(names).intersection(await held_names(db)), "publication_quality_hold")
    disposition = (await db.execute(text("""
        SELECT disposition FROM tagger_candidate_disposition_current WHERE tag = :tag
    """), {"tag": tag})).scalar_one_or_none()
    require(disposition not in ("defer", "dismiss", "block"), "publication_owner_hold")
    predecessor = (await db.execute(text("""
        SELECT content_sha256, patch_version, threshold, apply_mode, gate_tags
        FROM tagger_current_head_policy WHERE tag_id = :id
    """), {"id": row["tag_id"]})).mappings().one_or_none()
    require((dict(predecessor) if predecessor else None) == expected_predecessor,
            "publication_predecessor_changed")
    label_sha = (row["gt"] or {}).get("label_sha256")
    require(label_sha is not None and label_sha == label_identity(await read_label_rows(db, tag)),
            "publication_training_labels_stale")
    measurement = await require_publication_review(
        db, row, await load_config(db), answers_sha256=answers_sha256,
    )
    return {**row, "publication_measurement": measurement}


async def apply_admitted_candidate(db, row, *, lease, policy, publication_evidence=None):
    """Write an already admitted candidate. Caller owns admission and commit."""
    from datetime import date
    import json

    from api.tagger_core.transitions import record_lifecycle_event

    tag = row["tag"]
    require(lease.scope == f"lifecycle:{tag}", "publication_lease_scope_mismatch")
    require(policy["threshold"] == row["metrics"]["threshold"],
            "publication_threshold_mismatch")
    async with db.begin_nested():
        latest = (await db.execute(text("""
            SELECT id, to_stage FROM tagger_lifecycle_event
            WHERE tag_id = :tag_id ORDER BY occurred_at DESC, id DESC LIMIT 1
        """), {"tag_id": row["tag_id"]})).one_or_none()
        require(latest is not None and latest.id == row["lifecycle_event_id"]
                and latest.to_stage == "training", "publication_lifecycle_changed")
        artifact = (await db.execute(text("""
            SELECT EXISTS (SELECT FROM tagger_artifact
                WHERE content_sha256 = :sha AND kind = 'content_head_bundle'
                  AND byte_size > 0 AND kind_checked_at IS NOT NULL)
        """), {"sha": row["artifact_sha256"]})).scalar_one()
        require(artifact, "publication_artifact_unavailable")
        version = (await db.execute(text("""
            SELECT coalesce(max(version), 0) + 1 FROM tagger_patch WHERE tag_id = :id
        """), {"id": row["tag_id"]})).scalar_one()
        metrics = dict(row["metrics"])
        if publication_evidence is not None:
            metrics["publication_evidence"] = publication_evidence
        params = {
            "registration": row["id"], "tag": tag, "version": version,
            "sha": row["artifact_sha256"], "metrics": json.dumps(metrics),
            "gt": json.dumps(row["gt"]), "date": date.today(),
            "why": f"Autonomous candidate registration {row['id']}",
            "operating_point": policy["operating_point"], "threshold": policy["threshold"],
            "recall": policy["measured_recall"], "fpr": policy["measured_fpr"],
            "mode": policy["apply_mode"], "gates": policy["gate_tags"],
        }
        await db.execute(text("""
            SELECT tagger_governed_set_registration_status(
                :registration, 'validated', NULL, 'deployable')
        """), params)
        await db.execute(text("""
            SELECT tagger_governed_upsert_patch(
                :tag,:version,'approved',:sha,:why,NULL,CAST(:metrics AS jsonb),
                CAST(:gt AS jsonb),'content_head',NULL,NULL,NULL,:date,'tagger/lifecycle',NULL)
        """), params)
        patch_id = (await db.execute(text("""
            SELECT id FROM tagger_patch WHERE tag = :tag AND version = :version
        """), params)).scalar_one()
        await db.execute(text("""
            SELECT tagger_governed_upsert_head_policy(
                :tag,:version,:sha,:operating_point,:threshold,:recall,:fpr,
                :mode,CAST(:gates AS text[]),:registration)
        """), params)
        await record_lifecycle_event(db, tag=tag, from_stage="training", to_stage="approved",
                                     reason=params["why"], lease=lease, commit=False)
        await db.execute(text("""
            SELECT tagger_governed_promote_registration(:registration,:patch)
        """), {**params, "patch": patch_id})
    return patch_id


async def _publication_inventory_sha256(db, row, policy):
    from api.errors import KuraError
    from api.routers.tagger import read_head_policy_rows, render_head_policy_inventory
    from .dedicated_protocol import digest

    target = (await db.execute(text("""
        SELECT c.name AS category,
               (SELECT coalesce(max(version), 0) + 1 FROM tagger_patch WHERE tag_id = t.id) AS version
        FROM tag t JOIN tag_category c ON c.id = t.category_id
        WHERE t.id = :tag_id
    """), {"tag_id": row["tag_id"]})).mappings().one()
    candidate = proposed_policy(
        policy, tag=row["tag"], category=target["category"], version=target["version"],
        artifact_sha256=row["artifact_sha256"], threshold=row["metrics"]["threshold"],
    )
    try:
        current, _ = await render_head_policy_inventory(
            await read_head_policy_rows(db), require_complete=True)
    except KuraError:
        require(False, "publication_inventory_unavailable")
    policies = {p.tag: p.model_dump() for p in current}
    policies[row["tag"]] = candidate
    return digest([policies[tag] for tag in sorted(policies)])


async def publish_reviewed_candidate(db, registration_id, *, lease, policy,
                                     expected_release_id, expected_predecessor,
                                     answers_sha256, runtime_plan, approval_sha256,
                                     trusted_reviewer_ids):
    """Publish one reviewed candidate in the caller's transaction.

    Only a caller with an independently configured reviewer allowlist may
    invoke this. The evidence intake endpoint cannot grant that authority.
    """
    from api.services.qualification_evidence import (
        lock_runtime_evidence, registration_context, runtime_report_coverage,
    )
    from api.tagger_core.qualification_evidence import assemble, digest

    publication_evidence = {
        "expected_release_id": expected_release_id,
        "expected_predecessor": expected_predecessor,
        "answers_sha256": answers_sha256,
        "approval_sha256": approval_sha256,
        "runtime_plan_sha256": digest([
            {"host": host, "profile": profile, "runtime": pins}
            for (host, profile), pins in sorted((runtime_plan or {}).items())
        ]),
    }
    async with db.begin_nested():
        await lock_runtime_evidence(db, registration_id)
        active = (await db.execute(text("""
            SELECT release_id FROM tagger_release_active WHERE id = 1 FOR UPDATE
        """))).scalar_one_or_none()
        require(active is not None and active == expected_release_id, "publication_release_changed")
        existing = (await db.execute(text("""
            SELECT r.promoted_patch_id, r.artifact_sha256,
                   p.registration_id, p.content_sha256, p.threshold, p.apply_mode,
                   p.gate_tags, p.operating_point, p.measured_recall, p.measured_fpr,
                   patch.metrics->'publication_evidence' AS publication_evidence
            FROM tagger_registration_request r
            LEFT JOIN tagger_current_head_policy p ON p.tag_id = r.tag_id
            LEFT JOIN tagger_patch patch ON patch.id = r.promoted_patch_id
            WHERE r.id = :id
            FOR UPDATE OF r
        """), {"id": registration_id})).mappings().one_or_none()
        if existing is not None and existing["promoted_patch_id"] is not None:
            require(existing["registration_id"] == registration_id
                    and existing["content_sha256"] == existing["artifact_sha256"]
                    and existing["publication_evidence"] == publication_evidence
                    and all(existing[key] == policy[key] for key in (
                        "threshold", "apply_mode", "gate_tags", "operating_point",
                    ))
                    and all(key not in policy or existing[key] == policy[key]
                            for key in ("measured_recall", "measured_fpr")), "publication_existing_result_mismatch")
            return existing["promoted_patch_id"]
        row = await require_candidate_publication(
            db, registration_id, expected_release_id=expected_release_id,
            expected_predecessor=expected_predecessor, answers_sha256=answers_sha256,
        )
        domain = await domain_gate_tags(db, row["tag"])
        require(domain is not None, "publication_domain_undeclared")
        require(sorted(policy["gate_tags"]) == list(domain), "publication_domain_changed")
        measurement = row["publication_measurement"]
        require(all(key not in policy or policy[key] == value for key, value in measurement.items()),
                "publication_measurement_mismatch")
        policy = {**policy, **measurement}
        require(runtime_plan and trusted_reviewer_ids, "publication_authority_missing")
        trial_inputs = row["metrics"]["production_review"].get("worker_trial_inputs_sha256")
        require(isinstance(trial_inputs, str) and len(trial_inputs) == 64
                and all(pins.get("inputs_sha256") == trial_inputs for pins in runtime_plan.values()),
                "publication_runtime_inputs_changed")
        inventory_sha = await _publication_inventory_sha256(db, row, policy)
        require(all(pin.get("inventory_sha256") == inventory_sha for pin in runtime_plan.values()),
                "publication_runtime_inventory_changed")
        coverage = await runtime_report_coverage(db, registration_id, runtime_plan=runtime_plan)
        require(coverage["all_workers_report_pass"], "publication_runtime_trial_incomplete")
        context = await registration_context(db, registration_id)
        context_sha = digest(context)
        approval = (await db.execute(text("""
            SELECT body, submitted_by, receipt_sha256 FROM tagger_qualification_evidence
            WHERE registration_id = :id AND context_sha256 = :context
              AND submitted_by = ANY(:reviewers) AND body->>'kind' = 'independent_review'
            ORDER BY received_at DESC, receipt_sha256
            LIMIT 1
        """), {"id": registration_id, "context": context_sha,
               "reviewers": sorted(trusted_reviewer_ids)})).mappings().one_or_none()
        require(approval is not None and approval["receipt_sha256"] == approval_sha256,
                "publication_independent_review_required")
        report = approval["body"]["report"]
        envelope = assemble(context, context_sha, "independent_review", report)
        require(envelope["receipt_sha256"] == approval_sha256
                and report["verdict"] == "approve"
                and report.get("answers_sha256") == answers_sha256
                and set(report["evidence_sha256s"]) == {
                    worker["receipt_sha256"] for worker in coverage["workers"]
                }, "publication_independent_review_mismatch")
        overlap = (await db.execute(text("""
            SELECT EXISTS (
                SELECT FROM tagger_qualification_evidence
                WHERE submitted_by = :reviewer AND receipt_sha256 = ANY(:receipts)
            )
        """), {"reviewer": approval["submitted_by"],
               "receipts": report["evidence_sha256s"]})).scalar_one()
        require(not overlap, "publication_runtime_self_review")
        return await apply_admitted_candidate(
            db, row, lease=lease, policy=policy, publication_evidence=publication_evidence)


async def run_publication_plan(sessions, plan):
    """Process operator-pinned publication entries on a learning tick."""
    from api.tagger_core.transitions import acquire_lease, release_lease, TransitionError
    from .stratified_evaluation import EvaluationRefused

    require(isinstance(plan, dict) and plan.get("schema") == "tagger-publication-plan/1",
            "publication_plan_invalid")
    reviewers = plan.get("trusted_reviewer_ids")
    entries = plan.get("candidates")
    require(isinstance(reviewers, list) and reviewers
            and all(type(i) is int and i > 0 for i in reviewers)
            and isinstance(entries, list) and len(entries) <= 16,
            "publication_plan_invalid")
    results = []
    for entry in entries:
        registration = entry["registration_id"]
        async with sessions() as db:
            tag = (await db.execute(text("""
                SELECT tag FROM tagger_registration_request WHERE id = :id
            """), {"id": registration})).scalar_one_or_none()
            if tag is None:
                results.append({"registration_id": registration, "state": "missing"})
                continue
            try:
                lease = await acquire_lease(db, f"lifecycle:{tag}")
            except TransitionError:
                results.append({"registration_id": registration, "state": "lease_unavailable"})
                continue
            try:
                pins = {(worker["host"], worker["profile"]): worker["runtime"]
                        for worker in entry["workers"]}
                require(len(pins) == len(entry["workers"]), "publication_duplicate_plan_worker")
                patch = await publish_reviewed_candidate(
                    db, registration, lease=lease, policy=entry["policy"],
                    expected_release_id=entry["expected_release_id"],
                    expected_predecessor=entry["expected_predecessor"],
                    answers_sha256=entry["answers_sha256"], runtime_plan=pins,
                    approval_sha256=entry["approval_sha256"], trusted_reviewer_ids=set(reviewers),
                )
                await db.commit()
                results.append({"registration_id": registration, "state": "published", "patch_id": patch})
            except EvaluationRefused as exc:
                await db.rollback()
                results.append({"registration_id": registration, "state": "waiting", "reason": str(exc)})
            finally:
                await release_lease(db, lease)
    return results


async def generate_publication_plan(sessions, settings, *, trial_declaration):
    """Generate pinned entries from completed trials and configured reviewer authority."""
    from collections import defaultdict
    from api.services.qualification_evidence import registration_context, runtime_report_coverage
    from api.services.review_set_registry import evaluation_answers
    from .dedicated_protocol import digest, read_verified

    reviewers = settings.get("trusted_reviewer_ids")
    require(isinstance(reviewers, list) and reviewers
            and all(type(i) is int and i > 0 for i in reviewers),
            "publication_authority_missing")
    entries = defaultdict(list)
    for item in trial_declaration["trials"]:
        entries[item["registration_id"]].append(
            read_verified(item["plan_path"], "tagger-worker-runtime-plan/1"))
    result = {"schema": "tagger-publication-plan/1", "trusted_reviewer_ids": reviewers,
              "candidates": []}
    # The trial declaration records the exact policies its plans ran with;
    # publishing anything else would only stall at the inventory check.
    policies = (trial_declaration["candidate_policies"] if "candidate_policies" in trial_declaration
                else settings.get("policies", {}))
    fields = ("source_sha256", "inventory_sha256", "inputs_sha256", "device", "provider",
              "memory_limit_bytes", "threads", "deadline_s", "total")
    for registration, plans in entries.items():
        async with sessions() as db:
            ctx = await registration_context(db, registration)
            tag = ctx["tag"]
            if tag not in policies:
                continue
            workers = [{"host": p["host"], "profile": p["profile"],
                        "runtime": {key: p[key] for key in fields}} for p in plans]
            pins = {(w["host"], w["profile"]): w["runtime"] for w in workers}
            require(len(pins) == len(plans), "publication_duplicate_plan_worker")
            coverage = await runtime_report_coverage(db, registration, runtime_plan=pins)
            if not coverage["all_workers_report_pass"]:
                continue
            approval = (await db.execute(text("""
                SELECT receipt_sha256, body
                FROM tagger_qualification_evidence
                WHERE registration_id=:id AND submitted_by=ANY(:reviewers)
                  AND body->>'kind'='independent_review'
                ORDER BY received_at DESC, receipt_sha256
                LIMIT 1
            """), {"id": registration, "reviewers": reviewers})).mappings().one_or_none()
            if approval is None:
                continue
            assertions = approval["body"]["report"]
            if assertions["verdict"] != "approve" or set(assertions["evidence_sha256s"]) != {
                    w["receipt_sha256"] for w in coverage["workers"]}:
                continue
            request = ctx["metrics"]["production_review"]["request"]
            answers = await evaluation_answers(db, digest(request))
            if (answers is None or assertions.get("answers_sha256") != answers["sha256"]
                    or any(a["decision"] not in ("present", "absent") for a in answers["answers"])):
                continue
            policy = dict(policies[tag], threshold=ctx["metrics"]["threshold"])
            if any(p["apply_mode"] != policy["apply_mode"] or
                   (next((r for r in p["policies"] if r["tag"] == tag), None) or {}).get("gate_tags") != policy["gate_tags"]
                   for p in plans):
                continue
            if (len({p["release_id"] for p in plans}) != 1
                    or any(p["expected_predecessor"] != plans[0]["expected_predecessor"] for p in plans)):
                continue
            result["candidates"].append({
                "registration_id": registration, "expected_release_id": plans[0]["release_id"],
                "expected_predecessor": plans[0]["expected_predecessor"],
                "answers_sha256": answers["sha256"], "approval_sha256": approval["receipt_sha256"],
                "policy": policy, "workers": workers,
            })
    return result



VERIFIER_NAME = "kura-tagger-verifier/1"
VERIFIER_CHECKS = (
    "the registration is the latest unpromoted candidate for its tag",
    "no trusted reviewer's latest review of this context asks for revision",
    "every expected worker reported a passing runtime trial for the pinned plan",
    "no runtime receipt was submitted by this verifier",
    "all trial plans share one release, predecessor, input set and head inventory",
    "the publication admission check passes: release, predecessor, quality hold, "
    "owner disposition, training labels, review scope, answers and point floors",
    "the serving policy matches the curated domain declaration",
)
_VERIFIER_FIELDS = ("source_sha256", "inventory_sha256", "inputs_sha256", "device", "provider",
                    "memory_limit_bytes", "threads", "deadline_s", "total")


async def attest_qualified_candidates(sessions, settings, *, trial_declaration):
    """Deterministic independent review: approve only what every machine check proves.

    The verifier is a dedicated non-human account, distinct from the trial
    submitter, so the publication gate's self-review rule still holds. It runs
    the gate's own admission check without writing, never overrides a trusted
    reviewer's "revise", and never publishes: it records one
    ``independent_review`` receipt per evidence set and the ordinary
    publication plan then admits the candidate. A failure is contained to its
    registration so other candidates still proceed on the same tick.
    """
    from sqlalchemy.exc import SQLAlchemyError
    from .dedicated_protocol import read_verified

    verifier = settings.get("verifier_user_id")
    reviewers = settings.get("trusted_reviewer_ids") or []
    submitter = trial_declaration.get("submitter_id")
    if not (type(verifier) is int and verifier > 0 and verifier != submitter
            and verifier in reviewers):
        return [{"state": "verifier_misconfigured"}]
    # The trial declaration records the exact policies its plans ran with;
    # publishing anything else would only stall at the inventory check.
    policies = (trial_declaration["candidate_policies"] if "candidate_policies" in trial_declaration
                else settings.get("policies", {}))
    paths = {}
    for item in trial_declaration.get("trials", []):
        paths.setdefault(item["registration_id"], []).append(item["plan_path"])
    results = []
    for registration, plan_paths in sorted(paths.items()):
        async with sessions() as db:
            try:
                plans = [read_verified(path, "tagger-worker-runtime-plan/1") for path in plan_paths]
                state = await _verify_candidate(db, registration, plans, verifier=verifier,
                                                reviewers=reviewers, policies=policies)
                if state["state"] == "verified":
                    state = await _attest(db, registration, state, verifier=verifier)
                    await db.commit()
                else:
                    await db.rollback()
            except (ValueError, KeyError, TypeError, OSError, SQLAlchemyError) as exc:
                await db.rollback()
                state = {"state": "error", "error": type(exc).__name__, "reason": str(exc)[:200]}
            results.append({"registration_id": registration, **state})
    return results


async def _attest(db, registration, state, *, verifier):
    from datetime import datetime, timezone
    from api.services.qualification_evidence import attach
    from api.tagger_core.qualification_evidence import digest as evidence_digest

    context = state["context"]
    now = datetime.now(timezone.utc).isoformat()
    report = {
        "artifact_sha256": context["artifact_sha256"], "threshold": context["metrics"]["threshold"],
        "observed_at": now, "evidence_sha256s": state["evidence_sha256s"],
        "answers_sha256": state["answers_sha256"], "reviewer": VERIFIER_NAME,
        "reviewed_at": now, "verdict": "approve", "findings": list(VERIFIER_CHECKS),
    }
    received = await attach(db, registration, expected_context_sha256=evidence_digest(context),
                            kind="independent_review", report=report, user_id=verifier)
    return {"state": "attested", "receipt_sha256": received["receipt_sha256"]}


async def _verify_candidate(db, registration, plans, *, verifier, reviewers, policies):
    from api.services.qualification_evidence import registration_context, runtime_report_coverage
    from api.tagger_core.qualification_evidence import digest as evidence_digest
    from .canonical_evaluation import publication_evaluation_summary

    context = await registration_context(db, registration)
    tag = context["tag"]
    context_sha = evidence_digest(context)
    current = (await db.execute(text("""
        SELECT r.status, r.promoted_patch_id,
               (SELECT newer.id FROM tagger_registration_request newer
                WHERE newer.tag_id = r.tag_id ORDER BY requested_at DESC, id DESC LIMIT 1) AS latest
        FROM tagger_registration_request r WHERE r.id = :id
    """), {"id": registration})).mappings().one()
    if (current["status"] != "pending" or current["promoted_patch_id"] is not None
            or current["latest"] != registration):
        return {"state": "not_current"}
    others = (await db.execute(text("""
        SELECT DISTINCT ON (submitted_by) body->'report'->>'verdict'
        FROM tagger_qualification_evidence
        WHERE registration_id = :id AND context_sha256 = :context
          AND body->>'kind' = 'independent_review'
          AND submitted_by = ANY(:reviewers) AND submitted_by <> :verifier
        ORDER BY submitted_by, received_at DESC, receipt_sha256
    """), {"id": registration, "context": context_sha, "reviewers": list(reviewers),
           "verifier": verifier})).scalars().all()
    if "revise" in others:
        return {"state": "held", "reason": "trusted_reviewer_revise"}
    if tag not in policies:
        return {"state": "ineligible", "reason": "no_candidate_policy"}
    pins = {(p["host"], p["profile"]): {key: p[key] for key in _VERIFIER_FIELDS} for p in plans}
    if len(pins) != len(plans):
        return {"state": "ineligible", "reason": "duplicate_plan_worker"}
    if (len({p["release_id"] for p in plans}) != 1
            or any(p["expected_predecessor"] != plans[0]["expected_predecessor"] for p in plans)
            or len({p["inventory_sha256"] for p in plans}) != 1):
        return {"state": "ineligible", "reason": "trial_plans_inconsistent"}
    trial_inputs = context["metrics"].get("production_review", {}).get("worker_trial_inputs_sha256")
    if any(p["inputs_sha256"] != trial_inputs for p in plans):
        return {"state": "ineligible", "reason": "trial_inputs_changed"}
    try:
        coverage = await runtime_report_coverage(db, registration, runtime_plan=pins)
    except ValueError as exc:
        return {"state": "ineligible", "reason": str(exc)}
    if not coverage["all_workers_report_pass"]:
        return {"state": "waiting", "reason": "runtime_trial_incomplete",
                "workers": coverage["workers"]}
    receipts = sorted(worker["receipt_sha256"] for worker in coverage["workers"])
    own = (await db.execute(text("""
        SELECT EXISTS (SELECT FROM tagger_qualification_evidence
                       WHERE submitted_by = :verifier AND receipt_sha256 = ANY(:receipts))
    """), {"verifier": verifier, "receipts": receipts})).scalar_one()
    if own:
        return {"state": "ineligible", "reason": "verifier_submitted_runtime_evidence"}
    summary = await publication_evaluation_summary(
        db, dict(context, id=registration), await load_config(db))
    if summary["state"] != "awaiting_production_scope_and_fleet_qualification":
        return {"state": "waiting", "reason": summary["state"]}
    answers = summary["answers_sha256"]
    try:
        # The gate's own admission check (read-only; the caller rolls back or
        # commits only the receipt), so the approval never claims less than it checked.
        row = await require_candidate_publication(
            db, registration, expected_release_id=plans[0]["release_id"],
            expected_predecessor=plans[0]["expected_predecessor"], answers_sha256=answers)
    except ValueError as exc:
        return {"state": "waiting", "reason": str(exc)}
    policy = dict(policies[tag], threshold=context["metrics"]["threshold"])
    domain = await domain_gate_tags(db, tag)
    if domain is None or sorted(policy["gate_tags"]) != list(domain):
        return {"state": "ineligible", "reason": "publication_domain_mismatch"}
    if any(p["apply_mode"] != policy["apply_mode"]
           or (next((r for r in p["policies"] if r["tag"] == tag), None) or {}).get("gate_tags") != policy["gate_tags"]
           for p in plans):
        return {"state": "ineligible", "reason": "trial_policy_mismatch"}
    if plans[0]["inventory_sha256"] != await _publication_inventory_sha256(db, row, policy):
        return {"state": "waiting", "reason": "publication_runtime_inventory_changed"}
    previous = (await db.execute(text("""
        SELECT body FROM tagger_qualification_evidence
        WHERE registration_id = :id AND context_sha256 = :context AND submitted_by = :verifier
          AND body->>'kind' = 'independent_review'
        ORDER BY received_at DESC, receipt_sha256 LIMIT 1
    """), {"id": registration, "context": context_sha, "verifier": verifier})).scalar_one_or_none()
    if previous is not None:
        report = previous["report"]
        if (report["verdict"] == "approve" and report.get("answers_sha256") == answers
                and sorted(report["evidence_sha256s"]) == receipts):
            return {"state": "already_attested"}
    return {"state": "verified", "context": context, "evidence_sha256s": receipts,
            "answers_sha256": answers}
