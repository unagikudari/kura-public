"""Turn human answers on base_comparison review sets into the switch verdict
(docs/notes/tagger-base-switch-plan.md step 4; read-only on the database).

Inputs are the frames document written by tagger/base_frames.py and the
review-set references returned when the frames were registered. For each tag,
the latest answers come from the review registry; ``present``/``absent`` are
judgments and anything else (``uncertain``, unanswered) stays unanswered, so
an incomplete tag can never count as evidence for the switch. The governed
``min_precision`` decides tags the incumbent never predicts (owner ruling
2026-11-24). Every compared tag is a priority tag (owner rulings: the 20 tags
were chosen as the priority set).
"""
import argparse
import asyncio
import json
from pathlib import Path

from .base_comparison import compare, verdict, comparison_review_document
from .base_frames import SCHEMA as FRAMES_SCHEMA, sampled_display_hashes
from .dedicated_protocol import digest, read_verified, write_once
from .stratified_evaluation import require

SCHEMA = "tagger-base-switch-verdict/1"


def judgments(answers, frame):
    """post id -> 'present'/'absent'/None for the frame's sample, from registry answers."""
    sampled = {i for cell in frame["cells"].values() for i in cell["sample"]}
    out = {i: None for i in sampled}
    for row in answers:
        require(row["post_id"] in sampled, "answer_outside_frame")
        out[row["post_id"]] = row["decision"] if row["decision"] in ("present", "absent") else None
    return out


def decide(frames, answers_by_tag, *, precision_floor):
    """Pure: per-tag comparisons plus the ruling-3 verdict."""
    results = []
    for tag, frame in sorted(frames["frames"].items()):
        answers = answers_by_tag.get(tag)
        require(answers is not None, "answers_missing")
        require(answers["frame_sha256"] == digest(frame), "answers_frame_mismatch")
        results.append(compare(frame, judgments(answers["answers"], frame), precision_floor=precision_floor))
    tags = set(frames["frames"])
    return {"schema": SCHEMA, "frames_sha256": digest(frames["frames"]),
            "answers_sha256": {tag: a["sha256"] for tag, a in sorted(answers_by_tag.items())},
            "results": results,
            "verdict": verdict(results, priority_tags=tags, compared_tags=tags)}


async def read_answers(frames, references):
    from api.db.session import async_session
    from api.services.review_set_registry import evaluation_answers
    from api.tagger_core.config import load_config

    hashes = sampled_display_hashes(frames)
    out = {}
    async with async_session() as db:
        config = await load_config(db)
        for tag, frame in sorted(frames["frames"].items()):
            document = comparison_review_document(frame, display_hashes=hashes,
                                                  snapshot_sha256=frames["snapshot_sha256"])
            raw = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            import hashlib
            require(hashlib.sha256(raw).hexdigest() == references[tag], "registration_reference_mismatch")
            answers = await evaluation_answers(db, references[tag])
            require(answers is not None, "review_registry_inactive")
            out[tag] = answers
        await db.rollback()
    return out, config["min_precision"]


def summary(document):
    v = document["verdict"]
    rows = {r["tag"]: {"answered": r["answered"], "complete": r["complete"],
                       "f1_change": r["difference"]["f1"], "regression": r["significant_regression"],
                       "decidable": r["decidable"]} for r in document["results"]}
    return {"admissible": v["admissible"], "reasons": v["reasons"],
            "mean_f1_change": v["mean_f1_change"], "tags": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--frames", required=True)
    parser.add_argument("--references", required=True, help="JSON {tag: queue_sha256} from registration")
    parser.add_argument("--out", help="write the verdict once (omit to only print the summary)")
    args = parser.parse_args()
    frames = read_verified(args.frames, FRAMES_SCHEMA)
    references = json.loads(Path(args.references).read_text())
    require(set(references) == set(frames["frames"]), "registration_tags_mismatch")
    answers, floor = asyncio.run(read_answers(frames, references))
    document = decide(frames, answers, precision_floor=floor)
    if args.out:
        write_once(args.out, document)
    print(json.dumps(summary(document), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
