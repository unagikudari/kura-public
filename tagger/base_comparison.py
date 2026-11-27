"""Compare a candidate base model with the incumbent on fair human review
(docs/notes/tagger-base-switch-plan.md steps 3-4; owner rulings 2026-11-23).

Fairness: review images are drawn per tag from the four cells of
(incumbent predicts, candidate predicts), so neither model's own errors decide
what humans see; each cell is re-weighted to its population. The default budget
is 60 images per tag (15 per cell).

Verdict (ruling 3): the switch is admissible only when every priority tag is
fully answered with no statistically significant regression in precision or
recall (95 % stratified bootstrap), and the mean F1 change over the compared
tags is positive.

A tag the incumbent never predicts (both of its "present" cells are empty) has
no incumbent precision, so no difference can be bounded. Owner ruling
2026-11-24: such a tag is decided by the candidate alone -- it shows no
regression when the candidate's measured precision reaches the governed
``min_precision`` floor (recall cannot fall below an incumbent that finds
nothing). The candidate's own "present" cell must be answered in full
(``per_cell`` images), so one or three lucky images cannot decide a priority
tag; without that, or without a floor, the tag stays undecidable (fail
closed). Such a tag below the floor earns no credit in the mean F1 change. Everything here is pure; nothing changes models or labels.
Registering the frames as human review sets needs their own provenance kind so
they never displace a publication review for the same tag (next step).
"""
import math
from random import Random

from .dedicated_protocol import digest
from .stratified_evaluation import require

CELLS = ("both_present", "incumbent_only", "candidate_only", "both_absent")
DEFAULT_PER_CELL = 15
BOOTSTRAP_ROUNDS = 2000


def _cell(incumbent_present, candidate_present):
    if incumbent_present and candidate_present:
        return "both_present"
    if incumbent_present:
        return "incumbent_only"
    if candidate_present:
        return "candidate_only"
    return "both_absent"


def _model(side):
    require(isinstance(side, dict) and set(side) == {"model_sha256", "threshold", "scores"},
            "comparison_model_invalid")
    require(isinstance(side["model_sha256"], str) and len(side["model_sha256"]) == 64,
            "comparison_model_invalid")
    threshold = side["threshold"]
    require(type(threshold) in (int, float) and 0 < threshold < 1, "threshold_invalid")
    scores = side["scores"]
    require(isinstance(scores, dict) and scores
            and all(type(i) is int and i > 0 and type(v) in (int, float)
                    and math.isfinite(v) and 0 <= v <= 1 for i, v in scores.items()),
            "scores_invalid")
    return side


def joint_frame(tag, incumbent, candidate, *, per_cell=DEFAULT_PER_CELL, seed):
    """Freeze the scored population and a reproducible per-cell sample."""
    incumbent, candidate = _model(incumbent), _model(candidate)
    require(set(incumbent["scores"]) == set(candidate["scores"]), "comparison_population_mismatch")
    require(type(per_cell) is int and 1 <= per_cell <= 250, "review_sample_budget_invalid")
    members = {name: [] for name in CELLS}
    for post_id in sorted(incumbent["scores"]):
        members[_cell(incumbent["scores"][post_id] >= incumbent["threshold"],
                      candidate["scores"][post_id] >= candidate["threshold"])].append(post_id)
    rng = Random(f"{seed}:{tag}")
    cells = {name: {"population": len(ids), "sample": sorted(rng.sample(ids, min(len(ids), per_cell)))}
             for name, ids in members.items()}
    return {
        "schema": "tagger-base-comparison-frame/1",
        "tag": tag,
        "incumbent": {"model_sha256": incumbent["model_sha256"], "threshold": incumbent["threshold"]},
        "candidate": {"model_sha256": candidate["model_sha256"], "threshold": candidate["threshold"]},
        "population_sha256": digest(sorted(incumbent["scores"])),
        "scores_sha256": digest([sorted(incumbent["scores"].items()), sorted(candidate["scores"].items())]),
        "seed": str(seed),
        "per_cell": per_cell,
        "cells": cells,
    }


def _rates(rows, weights):
    """Weighted precision/recall/F1 for one model from (predicted, truth, cell) rows."""
    counts = dict(tp=0.0, fp=0.0, fn=0.0)
    for predicted, truth, cell in rows:
        weight = weights[cell]
        if predicted and truth:
            counts["tp"] += weight
        elif predicted:
            counts["fp"] += weight
        elif truth:
            counts["fn"] += weight
    precision = counts["tp"] / (counts["tp"] + counts["fp"]) if counts["tp"] + counts["fp"] else None
    recall = counts["tp"] / (counts["tp"] + counts["fn"]) if counts["tp"] + counts["fn"] else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision is not None and recall is not None and precision + recall else
          (0.0 if precision is not None or recall is not None else None))
    return {"precision": precision, "recall": recall, "f1": f1}


def _estimate(frame, judged):
    weights = {}
    rows = {"incumbent": [], "candidate": []}
    for name in CELLS:
        ids = judged.get(name, [])
        if not ids:
            continue
        weights[name] = frame["cells"][name]["population"] / len(ids)
        incumbent_present = name in ("both_present", "incumbent_only")
        candidate_present = name in ("both_present", "candidate_only")
        for truth in ids:
            rows["incumbent"].append((incumbent_present, truth, name))
            rows["candidate"].append((candidate_present, truth, name))
    return {side: _rates(rows[side], weights) for side in rows}


def _difference(estimate, metric):
    a, b = estimate["incumbent"][metric], estimate["candidate"][metric]
    return None if a is None or b is None else b - a


def compare(frame, judgments, *, rounds=BOOTSTRAP_ROUNDS, seed=0, precision_floor=None):
    """Per-tag comparison from present/absent answers keyed by post id.

    ``precision_floor`` (tagger_config ``min_precision``) decides tags the
    incumbent never predicts; see the module docstring."""
    require(precision_floor is None or (type(precision_floor) in (int, float) and 0 < precision_floor < 1),
            "precision_floor_invalid")
    require(frame.get("schema") == "tagger-base-comparison-frame/1", "comparison_frame_invalid")
    require(type(rounds) is int and rounds >= 1000, "bootstrap_rounds_too_few")
    sampled = {i for cell in frame["cells"].values() for i in cell["sample"]}
    require(isinstance(judgments, dict) and set(judgments) <= sampled, "judgment_ids_invalid")
    require(all(v in ("present", "absent", None) for v in judgments.values()), "judgment_value_invalid")
    judged = {name: [judgments[i] == "present" for i in cell["sample"] if judgments.get(i) is not None]
              for name, cell in frame["cells"].items()}
    complete = all(len(judged[name]) == len(frame["cells"][name]["sample"]) for name in CELLS)
    point = _estimate(frame, judged)
    rng = Random(seed)
    draws = {metric: [] for metric in ("precision", "recall", "f1")}
    for _ in range(rounds):
        resampled = {name: [rng.choice(values) for _ in values] for name, values in judged.items() if values}
        estimate = _estimate(frame, resampled)
        for metric in draws:
            value = _difference(estimate, metric)
            if value is not None:
                draws[metric].append(value)
    intervals = {}
    for metric, values in draws.items():
        values.sort()
        intervals[metric] = ([values[int(0.025 * (len(values) - 1))], values[int(0.975 * (len(values) - 1))]]
                             if len(values) >= rounds // 2 else None)
    regression = any(intervals[m] is not None and intervals[m][1] < 0 for m in ("precision", "recall"))
    # Fail closed: a tag whose precision or recall difference cannot be bounded
    # (e.g. one model never predicts it) is not evidence of "no regression".
    decidable = all(intervals[m] is not None for m in ("precision", "recall"))
    incumbent_silent = all(frame["cells"][name]["population"] == 0
                           for name in ("both_present", "incumbent_only"))
    below_floor = False
    if incumbent_silent:
        precision = point["candidate"]["precision"]
        enough = len(judged.get("candidate_only", [])) >= frame["per_cell"]
        decidable = precision_floor is not None and precision is not None and enough
        below_floor = decidable and precision < precision_floor
    return {
        "tag": frame["tag"],
        "frame_sha256": digest(frame),
        "complete": complete,
        "answered": sum(len(v) for v in judged.values()),
        "incumbent": point["incumbent"],
        "candidate": point["candidate"],
        "difference": {m: _difference(point, m) for m in ("precision", "recall", "f1")},
        "interval95": intervals,
        "significant_regression": regression,
        "decidable": decidable,
        "incumbent_silent": incumbent_silent,
        "candidate_below_precision_floor": below_floor,
        "precision_floor": precision_floor if incumbent_silent else None,
        "models": [frame["incumbent"]["model_sha256"], frame["candidate"]["model_sha256"]],
    }


def verdict(results, *, priority_tags, compared_tags):
    """Ruling 3: no significant regression on priority tags, then overall improvement.

    ``compared_tags`` is the full set the comparison covers; a missing result
    counts against the switch instead of silently shrinking the mean.
    """
    tags = [result["tag"] for result in results]
    require(len(tags) == len(set(tags)), "comparison_duplicate_tag")
    require({tuple(result["models"]) for result in results} <= {tuple(results[0]["models"])}
            if results else True, "comparison_models_mixed")
    require(set(priority_tags) <= set(compared_tags), "priority_tags_not_compared")
    by_tag = {result["tag"]: result for result in results}
    unanswered = sorted(t for t in compared_tags if not (t in by_tag and by_tag[t]["complete"]))
    missing_priority = sorted(set(priority_tags) & set(unanswered))
    undecidable = sorted(t for t in priority_tags if t in by_tag and by_tag[t]["complete"]
                         and not by_tag[t]["decidable"])
    regressed = sorted(t for t in priority_tags if t in by_tag and by_tag[t]["significant_regression"])
    below_floor = sorted(t for t in priority_tags if t in by_tag and by_tag[t]["complete"]
                         and by_tag[t].get("candidate_below_precision_floor"))
    floors = {r.get("precision_floor") for r in results if r.get("incumbent_silent")}
    require(len(floors) <= 1, "comparison_precision_floor_mixed")
    # A tag the incumbent never predicts gains its whole F1 by default; below the
    # floor (or undecided) it earns no credit.
    changes = [(0.0 if by_tag[t].get("incumbent_silent")
                and (by_tag[t].get("candidate_below_precision_floor") or not by_tag[t]["decidable"])
                else by_tag[t]["difference"]["f1"])
               for t in compared_tags
               if t in by_tag and by_tag[t]["complete"] and by_tag[t]["difference"]["f1"] is not None]
    mean_f1_change = sum(changes) / len(changes) if changes else None
    reasons = []
    if missing_priority:
        reasons.append("priority_tags_unanswered")
    if undecidable:
        reasons.append("priority_tag_undecidable")
    if regressed:
        reasons.append("priority_tag_regression")
    if below_floor:
        reasons.append("priority_tag_below_precision_floor")
    if mean_f1_change is None or mean_f1_change <= 0:
        reasons.append("no_overall_improvement")
    return {
        "schema": "tagger-base-comparison-verdict/1",
        "admissible": not reasons,
        "reasons": reasons,
        "unanswered_priority_tags": missing_priority,
        "undecidable_priority_tags": undecidable,
        "regressed_priority_tags": regressed,
        "below_precision_floor_priority_tags": below_floor,
        "unanswered_tags": unanswered,
        "mean_f1_change": mean_f1_change,
        "compared_tags": len(changes),
        "models": list(results[0]["models"]) if results else None,
        "precision_floor": next(iter(floors), None),
        "results_sha256": digest(sorted((r["tag"], r["frame_sha256"], r.get("precision_floor"))
                                        for r in results)),
    }


def comparison_review_document(frame, *, display_hashes, snapshot_sha256):
    """One tag's frame as a direct review request with its own purpose and provenance
    kind (migration 0212), so it never displaces a dedicated candidate's publication
    review in the per-tag registry and review refill does not count it."""
    post_ids = sorted(i for cell in frame["cells"].values() for i in cell["sample"])
    require(post_ids and all(i in display_hashes for i in post_ids), "evaluation_display_hash_missing")
    return {
        "schema": "human-review-set/3",
        "tag": frame["tag"],
        "purpose": "base_comparison",
        "frame_sha256": digest(frame),
        "model_sha256": frame["candidate"]["model_sha256"],
        "post_ids": post_ids,
        "media_sha256": {str(i): display_hashes[i] for i in post_ids},
        "source_provenance": {
            "kind": "base_comparison",
            "snapshot_sha256": snapshot_sha256,
            "measurement_sha256": frame["scores_sha256"],
        },
    }


async def request_comparison_review(db, frame, *, display_hashes, snapshot_sha256):
    from api.services.review_set_registry import register_candidate_set

    return await register_candidate_set(db, comparison_review_document(
        frame, display_hashes=display_hashes, snapshot_sha256=snapshot_sha256))
