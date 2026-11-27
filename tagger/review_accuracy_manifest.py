"""Assemble paired review prediction evidence without invoking production workers."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .dedicated_protocol import read_verified, write_once
from .review_accuracy import SCHEMA, SIDES, positive, read_reviews

SIDE_SCHEMA = "tagger-review-release-scores/1"


def assemble(before, after, reviews):
    sides = dict(before=before, after=after)
    if any(set(side["sets"]) != set(reviews) for side in sides.values()):
        raise ValueError("release score inventories refer to different review sets")
    result = {}
    for ref, review in reviews.items():
        sets = {name: sides[name]["sets"][ref] for name in SIDES}
        if any((item["tag"], item["frame_sha256"]) != (review["tag"], review["frame_sha256"])
               for item in sets.values()):
            raise ValueError("score frame mismatch")
        rows = {str(row["post_id"]): row for row in review["answers"]}
        thresholds = {name: sets[name]["threshold"] for name in SIDES}
        observed = {name: sets[name]["predictions"] for name in SIDES}
        if any(set(index) - set(rows) for index in observed.values()):
            raise ValueError("score outside the review set")
        scores = {}
        for post_id, row in rows.items():
            display = row["display_sha256"]
            entry = {"display_sha256": display}
            for name in SIDES:
                prediction = observed[name].get(post_id)
                if prediction is not None and prediction["display_sha256"] != display:
                    raise ValueError("score image mismatch")
                entry[name] = prediction["score"] if prediction is not None else None
                positive(entry[name], thresholds[name])
            scores[post_id] = entry
        result[ref] = dict(tag=review["tag"], frame_sha256=review["frame_sha256"],
                           thresholds=thresholds, scores=scores, excluded=review.get("excluded", {}))
    return dict(schema=SCHEMA, releases={name: doc["release"] for name, doc in sides.items()},
                side_evidence_sha256={name: doc["sha256"] for name, doc in sides.items()}, sets=result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    before, after = [read_verified(path, SIDE_SCHEMA) for path in (args.before, args.after)]
    reviews = asyncio.run(read_reviews(before["sets"]))
    write_once(args.out, assemble(before, after, reviews))


if __name__ == "__main__":
    main()
