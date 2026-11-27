"""Read-only before/after accuracy report for fixed human review sets."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import re

from .dedicated_protocol import digest, read_verified

SCHEMA = "tagger-review-accuracy-input/1"
PRIORITY_TAGS = (
    "scat", "feces", "peeing", "anilingus", "licking_testicle",
    "testicle_sucking", "face_in_ass", "no_humans",
)
PAIRS = (
    ("scat", "peeing"), ("feces", "peeing"),
    ("anilingus", "licking_testicle"), ("anilingus", "testicle_sucking"),
)
SIDES = ("before", "after")


def rate(numerator, denominator):
    return numerator / denominator if denominator else None


def metrics(counts):
    counts = {key: counts[key] for key in ("tp", "fp", "fn", "tn")}
    predicted = counts["tp"] + counts["fp"]
    actual = counts["tp"] + counts["fn"]
    return counts | {
        "predicted_present": predicted,
        "human_present": actual,
        "false_tag_rate": rate(counts["fp"], predicted),
        "missed_tag_rate": rate(counts["fn"], actual),
    }


def positive(score, threshold):
    if score is None:
        return None
    if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError("invalid prediction score")
    return score >= threshold


def training_overlap(rows, releases, inventories):
    """Fail closed unless both release-bound training inventories are supplied."""
    if inventories is None:
        return "unverified"
    for side in SIDES:
        inventory = inventories[side]
        release = releases[side]
        if (inventory["release_sha256"] != release["release_sha256"]
                or inventory["sha256"] != release.get("training_inventory_sha256")):
            raise ValueError("training inventory release mismatch")
        ids = set(inventory["post_ids"])
        hashes = set(inventory["display_sha256"])
        if any(row["post_id"] in ids or row["display_sha256"] in hashes for row in rows):
            return "detected"
    return "clear"


def build_report(manifest, reviews, inventories=None):
    """Count only rows for which both releases scored the same reviewed bytes."""
    releases = manifest["releases"]
    for side in SIDES:
        release = releases[side]
        for key in ("release_sha256", "base_sha256", "patch_sha256"):
            if not isinstance(release[key], str) or not re.fullmatch("[a-f0-9]{64}", release[key]):
                raise ValueError(f"invalid {side} {key}")
    results = []
    paired_rows = defaultdict(dict)
    for reference, spec in sorted(manifest["sets"].items()):
        review = reviews[reference]
        if spec["tag"] != review["tag"] or spec["frame_sha256"] != review["frame_sha256"]:
            raise ValueError("review frame mismatch")
        thresholds = spec["thresholds"]
        if any(type(thresholds[s]) not in (int, float)
               or not 0 < thresholds[s] < 1 for s in SIDES):
            raise ValueError("invalid threshold")
        rows = review["answers"]
        if set(spec["scores"]) != {str(row["post_id"]) for row in rows}:
            raise ValueError("score inventory differs from review set")
        counts = {side: Counter() for side in SIDES}
        changed = Counter()
        exclusions = spec.get("excluded", {})
        if any(row["decision"] is not None or exclusions[str(row["post_id"])] != {
            "reason": "hidden_pool_only", "display_sha256": row["display_sha256"],
        } for row in rows if str(row["post_id"]) in exclusions):
            raise ValueError("invalid review exclusion evidence")
        if set(exclusions) - set(spec["scores"]) or (exclusions and review["schema"] != "human-review-set/3"):
            raise ValueError("review exclusions outside blind inventory")
        included_rows = [row for row in rows if str(row["post_id"]) not in exclusions]
        coverage = Counter(total=len(included_rows))
        track = review["origin"] == "lora_findings"
        blind = review["schema"] == "human-review-set/3"
        if not (track or blind):
            raise ValueError("review must be a LoRA finding or a blind set")
        for row in rows:
            pid = row["post_id"]
            score = spec["scores"][str(pid)]
            if score["display_sha256"] != row["display_sha256"]:
                raise ValueError("prediction image mismatch")
            if str(pid) in exclusions:
                continue
            if row["decision"] not in ("present", "absent"):
                coverage["uncertain" if row["decision"] == "uncertain" else "unanswered"] += 1
                continue
            truth = row["decision"] == "present"
            decisions = {side: positive(score[side], thresholds[side]) for side in SIDES}
            if None in decisions.values():
                coverage["missing_scores"] += 1
                continue
            coverage["compared"] += 1
            for side, predicted in decisions.items():
                counts[side][("tp" if truth else "fp") if predicted else ("fn" if truth else "tn")] += 1
            changed["corrected"] += decisions["before"] != truth and decisions["after"] == truth
            changed["regressed"] += decisions["before"] == truth and decisions["after"] != truth
            bucket = "lora_followup" if track else "blind_evaluation"
            identity = (bucket, pid, row["display_sha256"])
            value = (truth, decisions["before"], decisions["after"])
            previous = paired_rows[identity].get(review["tag"], value)
            paired_rows[identity][review["tag"]] = value if previous == value else None
        overlap = training_overlap(included_rows, releases, inventories) if blind and not track else "not_assessed"
        results.append({
            "queue_sha256": reference, "frame_sha256": review["frame_sha256"], "tag": review["tag"],
            "purpose": review["purpose"], "origin": review["origin"],
            "use": "correction_tracking_only" if track else "blind_evaluation",
            # A blind review purpose prevents these answers entering GT. It
            # does not establish that either release was trained without the images.
            "eligible_for_improvement_claim": (
                overlap == "clear" and coverage["total"] > 0 and coverage["compared"] == coverage["total"]
            ),
            "training_overlap": overlap,
            "thresholds": thresholds, "answers_sha256": digest(rows),
            "coverage": {key: coverage[key] for key in
                         ("total", "compared", "unanswered", "uncertain", "missing_scores")},
            "excluded": exclusions, "change": dict(changed), **{side: metrics(counts[side]) for side in SIDES},
        })
    matrices = []
    for left, right in PAIRS:
        for group in ("lora_followup", "blind_evaluation"):
            matrix = {side: Counter() for side in SIDES}
            for identity, row in paired_rows.items():
                if identity[0] != group or row.get(left) is None or row.get(right) is None:
                    continue
                first, second = row[left], row[right]
                actual = f"{int(first[0])}{int(second[0])}"
                for index, side in enumerate(SIDES, 1):
                    predicted = f"{int(first[index])}{int(second[index])}"
                    matrix[side][f"{actual}->{predicted}"] += 1
            matrices.append({"tags": [left, right], "group": group,
                             "matched": sum(matrix["before"].values()),
                             **{side: dict(matrix[side]) for side in SIDES}})
    return {"schema": "tagger-review-accuracy-report/1", "releases": releases,
            "sets": results, "confusion_pairs": matrices}


async def read_reviews(references):
    from sqlalchemy import text
    from api.db.session import async_session
    from api.services import review_set_registry as registry
    from api.services.pool_visibility import post_visible_sql

    async with async_session() as db:
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        legacy = await registry.legacy_lora_refs(db) or set()
        out = {}
        for reference in references:
            rows = (await db.execute(text(f"""
                SELECT q.document, m.post_id, m.display_sha256, a.id AS event_id,
                       a.decision, a.display_sha256 AS answered_display_sha256,
                       NOT ({post_visible_sql("m.post_id")}) AS hidden
                FROM tagger_review_set q
                JOIN tagger_review_set_member m USING(queue_sha256)
                LEFT JOIN LATERAL (
                  SELECT id, decision, source_context->'observed'->>'display_sha256' AS display_sha256
                  FROM tagger_human_review_event e
                  WHERE e.queue_sha256=m.queue_sha256 AND e.post_id=m.post_id
                  ORDER BY e.id DESC LIMIT 1
                ) a ON true
                WHERE q.queue_sha256=:ref
                ORDER BY m.ordinal
            """), {"ref": reference})).mappings().all()
            if not rows:
                raise ValueError("review set missing")
            doc = json.loads(bytes(rows[0]["document"]))
            answer_rows = [{key: row[key] for key in
                            ("post_id", "display_sha256", "event_id", "decision")} for row in rows]
            for row in rows:
                if not row["display_sha256"] or (
                    row["event_id"] is not None and row["answered_display_sha256"] != row["display_sha256"]
                ):
                    raise ValueError("review answer image mismatch")
            out[reference] = {key: doc[key] for key in ("schema", "tag", "purpose", "frame_sha256")}
            findings = reference in legacy or doc.get("source_provenance", {}).get("origin") == "lora_findings"
            exclusions = {
                str(row["post_id"]): {"reason": "hidden_pool_only", "display_sha256": row["display_sha256"]}
                for row in rows if doc["schema"] == "human-review-set/3"
                and row["hidden"] and row["event_id"] is None
            }
            out[reference].update(origin="lora_findings" if findings else "lifecycle",
                                  answers=answer_rows, excluded=exclusions)
        return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scores", type=Path, required=True,
                        help="digest-bound paired prediction manifest for frozen review images")
    parser.add_argument("--training-before", type=Path)
    parser.add_argument("--training-after", type=Path)
    args = parser.parse_args()
    if bool(args.training_before) != bool(args.training_after):
        parser.error("both release-bound training inventories are required")
    inventories = ({
        side: read_verified(getattr(args, f"training_{side}"), "tagger-training-inventory/1")
        for side in SIDES
    } if args.training_before else None)
    manifest = read_verified(args.scores, SCHEMA)
    reviews = asyncio.run(read_reviews(manifest["sets"]))
    print(json.dumps(build_report(manifest, reviews, inventories), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
