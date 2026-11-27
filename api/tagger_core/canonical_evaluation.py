"""Evaluate a frozen scored frame against canonical checker answers."""

import hashlib
import json
import math
import os
from pathlib import Path
from random import Random

from .learning_artifacts import digest, read_verified, write_once

from .human_evaluation_action import assess
from .stratified_evaluation import require


def freeze_scored_frame(model_sha256, scores, *, threshold, per_stratum, seed):
    """Select reproducible uniform samples from both prediction strata."""
    require(isinstance(model_sha256, str) and len(model_sha256) == 64
            and all(c in "0123456789abcdef" for c in model_sha256),
            "frame_model_invalid")
    require(type(threshold) in (int, float) and 0 < threshold < 1,
            "threshold_invalid")
    require(type(per_stratum) is int and 1 <= per_stratum <= 250,
            "review_sample_budget_invalid")
    require(isinstance(scores, dict) and scores
            and all(type(i) is int and i > 0 and type(v) in (int, float)
                    and math.isfinite(v) and 0 <= v <= 1
                    for i, v in scores.items()), "scores_invalid")
    ordered = sorted(scores.items())
    rng = Random(seed)
    sample = {}
    for name, positive in (("predicted_present", True), ("predicted_absent", False)):
        members = [i for i, score in ordered if (score >= threshold) == positive]
        sample[name] = sorted(rng.sample(members, min(len(members), per_stratum)))
    return {
        "schema": "tagger-scored-review-frame/1",
        "model_sha256": model_sha256,
        "threshold": threshold,
        "scores": [list(item) for item in ordered],
        "sample": sample,
    }


def candidate_review_document(*, tag, frame, display_hashes,
                                   snapshot_sha256, measurement_sha256):
    """Register the exact selected population and its frozen display hashes."""
    post_ids = sorted(i for ids in frame["sample"].values() for i in ids)
    require(all(i in display_hashes for i in post_ids), "evaluation_display_hash_missing")
    return {
        "schema": "human-review-set/3",
        "tag": tag,
        "purpose": "candidate_evaluation",
        "frame_sha256": digest(frame),
        "model_sha256": frame["model_sha256"],
        "post_ids": post_ids,
        "media_sha256": {str(i): display_hashes[i] for i in post_ids},
        "source_provenance": {
            "kind": "dedicated_candidate",
            "snapshot_sha256": snapshot_sha256,
            "measurement_sha256": measurement_sha256,
        },
    }


async def request_candidate_review(db, **inputs):
    from api.services.review_set_registry import register_candidate_set

    return await register_candidate_set(db, candidate_review_document(**inputs))


async def evaluate_queue(db, queue_sha256, frame, *, policy):
    """Return descriptive evidence bound to both the frame and answer revision.

    The scored frame carries the artifact, threshold, population scores and
    selected strata. The registry must already bind its exact digest.
    """
    from api.services.review_set_registry import evaluation_answers

    answers = await evaluation_answers(db, queue_sha256)
    if answers is None:
        return None
    require(set(frame) == {"schema", "model_sha256", "threshold", "scores", "sample"}
            and frame["schema"] == "tagger-scored-review-frame/1",
            "scored_review_frame_invalid")
    require(answers["frame_sha256"] == digest(frame)
            and answers["model_sha256"] == frame["model_sha256"],
            "scored_review_frame_binding_mismatch")
    requested = [post_id for ids in frame["sample"].values() for post_id in ids]
    judgments = {row["post_id"]: row["decision"] for row in answers["answers"]}
    require(sorted(requested) == sorted(judgments), "evaluation_membership_mismatch")
    report = assess([{"scores": dict(frame["scores"]), "sample": frame["sample"]}],
                    judgments, threshold=frame["threshold"], policy=policy)
    receipt = {
        "schema": "tagger-canonical-evaluation/1",
        "queue_sha256": queue_sha256,
        "answers_sha256": answers["sha256"],
        "frame_sha256": answers["frame_sha256"],
        "model_sha256": answers["model_sha256"],
        "report": report,
    }
    return {**receipt, "sha256": digest(receipt)}


POLICY_KEYS = ("min_precision", "min_recall", "min_val_pos_support", "min_pred_pos_support")


async def _review_evidence(db, row, config):
    """Classify current descriptive evidence without deriving admission."""
    from sqlalchemy import text
    from api.services.review_set_registry import evaluation_answers

    try:
        request = row["metrics"]["production_review"]["request"]
        ref = digest(request)
        answers = await evaluation_answers(db, ref)
        if answers is None:
            return "production_evaluation_queue_unavailable", None, None
        if any(item["decision"] not in ("present", "absent") for item in answers["answers"]):
            return "awaiting_production_checker_answers", None, None
        rows = (await db.execute(text("""
            SELECT receipt_sha256, document FROM tagger_candidate_evaluation
            WHERE registration_id = :id AND queue_sha256 = :queue AND answers_sha256 = :answers
            ORDER BY recorded_at DESC
        """), {"id": row["id"], "queue": ref, "answers": answers["sha256"]})).all()
        for sha, raw in rows:
            record = json.loads(raw)
            report = record["report"]
            require(digest(record) == sha and record["registration_id"] == row["id"]
                    and record["queue_sha256"] == ref
                    and record["answers_sha256"] == answers["sha256"]
                    and record["frame_sha256"] == request["frame_sha256"]
                    and record["model_sha256"] == row["artifact_sha256"]
                    and report["threshold"] == row["metrics"]["threshold"],
                    "production_evaluation_binding_invalid")
            if report["policy"] != {key: config[key] for key in POLICY_KEYS}:
                continue
            return {
                "measured_floor_not_met": "production_evaluation_floor_not_met",
                "insufficient_observed_support": "production_evaluation_insufficient_support",
                "point_floors_met_review_required": "awaiting_production_scope_and_fleet_qualification",
            }.get(report["state"], "production_evaluation_unresolved"), report, answers["sha256"]
        return "awaiting_current_production_evaluation", None, None
    except (ValueError, KeyError, TypeError):
        return "production_evaluation_binding_invalid", None, None


async def publication_evaluation_summary(db, row, config):
    """Project the current, verified checker evidence for the review page."""
    state, report, review_answers_sha256 = await _review_evidence(db, row, config)
    summary = {"state": state}
    if report is not None:
        summary.update(
            estimates=(report.get("measurement") or {}).get("weighted_estimate"),
            responses=report["responses"],
            checks=report["point_floor_checks"],
            answers_sha256=review_answers_sha256,
        )
    return summary


async def review_evidence_state(db, row, config):
    state, _, _ = await _review_evidence(db, row, config)
    return state


async def require_publication_review(db, row, config, *, answers_sha256):
    """Lock the complete current answers and require matching evaluation evidence.

    The caller owns the transaction and must separately enforce fleet and
    publication policy before changing the active head.
    """
    from api.services.review_set_registry import require_current_evaluation_answers

    metrics = row["metrics"]
    review = metrics["production_review"]
    request = review["request"]
    require(review.get("scope") == "catalog_sample_outside_frozen_gt_groups",
            "publication_review_scope_ineligible")
    require(request.get("purpose") == "candidate_evaluation", "publication_review_purpose_invalid")
    require(request["model_sha256"] == row["artifact_sha256"]
            and request["frame_sha256"] == review["frame_sha256"]
            and request["source_provenance"]["snapshot_sha256"] == metrics["snapshot_sha256"]
            and request["source_provenance"]["measurement_sha256"] == review["measurement_sha256"],
            "publication_review_candidate_mismatch")
    await require_current_evaluation_answers(db, digest(request), answers_sha256)
    state, report, review_answers_sha256 = await _review_evidence(db, row, config)
    require(state == "awaiting_production_scope_and_fleet_qualification", state)
    require(review_answers_sha256 == answers_sha256, "evaluation_answers_stale")
    estimate = report["measurement"]["weighted_estimate"]
    counts = estimate["counts"]
    negatives = counts["fp"] + counts["tn"]
    return {"measured_recall": estimate["recall"],
            "measured_fpr": counts["fp"] / negatives if negatives else None}


async def refresh_registered_evaluations(db, *, root=None):
    """Recompute descriptive quality whenever canonical answers change."""
    from sqlalchemy import text

    from api.tagger_core.config import load_config

    root = Path(root) if root is not None else (
        Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        / "kura-tagger" / "learning-jobs"
    )
    candidates = (await db.execute(text("""
        SELECT * FROM (
            SELECT DISTINCT ON (tag_id) id, tag, lifecycle_event_id,
                   artifact_sha256, metrics
            FROM tagger_registration_request
            ORDER BY tag_id, requested_at DESC, id DESC
        ) latest
        WHERE lifecycle_event_id IS NOT NULL AND metrics ? 'production_review'
        ORDER BY id
    """))).mappings().all()
    if not candidates:
        return {"evaluated": 0, "unavailable": []}
    config = await load_config(db)
    policy = {key: config[key] for key in POLICY_KEYS}
    evaluated = 0
    unavailable = []
    for row in candidates:
        event = row["lifecycle_event_id"]
        key = hashlib.sha256(f'{row["tag"]}@{event}'.encode()).hexdigest()[:24]
        received = root / key / "received"
        try:
            review = row["metrics"]["production_review"]
            request = review["request"]
            if "frame" in review:
                frame = review["frame"]
            else:
                frame = read_verified(received / "review-frame.json", "tagger-scored-review-frame/1")
                frame.pop("sha256")
            require(digest(frame) == request["frame_sha256"] == review["frame_sha256"]
                    and frame["model_sha256"] == row["artifact_sha256"],
                    "registered_frame_binding_mismatch")
            receipt = await evaluate_queue(db, digest(request), frame, policy=policy)
            require(receipt is not None, "registered_review_queue_unavailable")
            path = received / "evaluations" / (receipt["sha256"] + ".json")
            body = {k: v for k, v in receipt.items() if k != "sha256"}
            record = {**body, "registration_id": row["id"]}
            raw = json.dumps(record, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()
            await db.execute(text("""
                INSERT INTO tagger_candidate_evaluation
                    (receipt_sha256, registration_id, queue_sha256, answers_sha256, document)
                VALUES (:sha, :registration, :queue, :answers, :document)
                ON CONFLICT (receipt_sha256) DO NOTHING
            """), {"sha": digest(record), "registration": row["id"],
                   "queue": receipt["queue_sha256"], "answers": receipt["answers_sha256"],
                   "document": raw})
            write_once(path, body)
            evaluated += 1
        except (OSError, ValueError, KeyError, TypeError) as exc:
            unavailable.append({"registration_id": row["id"], "reason": type(exc).__name__})
    return {"evaluated": evaluated, "unavailable": unavailable}
