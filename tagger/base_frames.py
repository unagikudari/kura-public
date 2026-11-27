"""Turn base scores into per-tag review frames (docs/notes/tagger-base-switch-plan.md
step 3; offline, read-only).

Input is the write-once output of tagger/base_scoring.py. For each tag, both
models' scores are cut at their thresholds and tagger/base_comparison.joint_frame
draws a reproducible sample from each of the four agreement cells.

Some posts are removed from the population of every tag before sampling,
symmetrically for both models (so the comparison is not biased), and recorded by
reason; nothing in kura is changed:

* ``youth``: posts carrying a youth tag -- the owner's age-check ruling, rule 5
  (~/.local/state/review/owner-rulings/age-check.md), plus kura tags whose names
  state the same thing (``onee-shota``, ``toddlercon``, ...; stricter than the
  literal list, never looser).
* ``training_opt_out``: posts tagged ``tagger-training-exclude``.
* ``no_display_image``: posts without an eligible display image in the frozen
  media snapshot (excluded there, or not an image).

Every sampled post's scored bytes (``<id>.bin`` in the scoring dataset) must hash
to its display blob sha256 in the snapshot, so a review set can prove the image a
human judges is the one both models scored.
"""
import argparse
import json
from pathlib import Path

from .base_comparison import DEFAULT_PER_CELL, joint_frame
from .dedicated_protocol import digest, read_verified, write_once
from .stratified_evaluation import require

SCHEMA = "tagger-base-comparison-frames/1"
SCORES_SCHEMA = "tagger-base-scores/2"
DEFAULT_THRESHOLD = 0.35  # wd14 production general-tag threshold
# One definition with the training/GT exclusion (age-check ruling, rule 5).
from api.tagger_core.post_exclusion import OPT_OUT_TAG, YOUTH_NAMED_TAGS, YOUTH_TAGS  # noqa: E402


def _posts_with_tags(conn, post_ids, names):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT pt.post_id FROM post_tag pt
            WHERE pt.post_id = ANY(%s)
              AND pt.tag_id IN (SELECT tag_id FROM tag_name WHERE lower(name) = ANY(%s))
        """, (sorted(post_ids), [n.lower() for n in names]))
        return {row[0] for row in cur.fetchall()}


def youth_posts(conn, post_ids):
    """Post ids among ``post_ids`` that carry any youth tag under any of its names."""
    return _posts_with_tags(conn, post_ids, YOUTH_TAGS + YOUTH_NAMED_TAGS)


def opt_out_posts(conn, post_ids):
    return _posts_with_tags(conn, post_ids, (OPT_OUT_TAG,))


def display_hashes(media):
    """post id -> display blob sha256 for decoded image entries of a media snapshot."""
    return {entry["id"]: entry["blob_sha256"] for entry in media["media"]
            if entry.get("status") == "ok" and entry.get("type") == "image"
            and entry.get("exclusion") is None and entry.get("blob_sha256")}


def _blob_sha256(path):
    import hashlib

    digest_ = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest_.update(chunk)
    return digest_.hexdigest()


def verify_scored_bytes(dataset, document):
    """Every sampled post's scored file must be the snapshot's display blob."""
    for post_id, expected in document["display_hashes"].items():
        require(_blob_sha256(Path(dataset) / f"{post_id}.bin") == expected, "scored_bytes_mismatch")


def sampled_display_hashes(document):
    """``display_hashes`` keyed by int post id, as review-set documents expect
    (JSON stores them as strings; an int lookup on the raw dict always misses)."""
    return {int(post_id): sha for post_id, sha in document["display_hashes"].items()}


def _model_scores(side, tag):
    return {int(post_id): value for post_id, value in side["scores"][tag].items()}


def population(scores):
    require(scores.get("schema") == SCORES_SCHEMA and scores.get("tags"), "scores_schema_invalid")
    return {int(i) for i in scores["models"]["incumbent"]["scores"][scores["tags"][0]]}


def build(scores, media, exclusions, *, thresholds, per_cell=DEFAULT_PER_CELL, seed=None):
    """``exclusions`` maps a reason to post ids; posts without an eligible display
    image are added as ``no_display_image``."""
    hashes = display_hashes(media)
    exclusions = {reason: sorted(set(ids)) for reason, ids in exclusions.items()}
    exclusions["no_display_image"] = sorted(population(scores) - set(hashes))
    seed = str(seed if seed is not None else scores["sha256"])
    from api.tagger_core.review_censor_variants import CENSOR_REVIEW_TAGS
    excluded = {i for reason, ids in exclusions.items() if reason != "censor_variant" for i in ids}
    frames = {}
    for tag in scores["tags"]:
        tag_excluded = excluded if tag in CENSOR_REVIEW_TAGS else excluded | set(exclusions.get("censor_variant", []))
        sides = {}
        for role in ("incumbent", "candidate"):
            side = scores["models"][role]
            values = {i: v for i, v in _model_scores(side, tag).items() if i not in tag_excluded}
            sides[role] = {"model_sha256": side["model_sha256"], "threshold": thresholds[role],
                           "scores": values}
        frame = joint_frame(tag, sides["incumbent"], sides["candidate"], per_cell=per_cell, seed=seed)
        sampled = [i for cell in frame["cells"].values() for i in cell["sample"]]
        require(all(i in hashes for i in sampled), "evaluation_display_hash_missing")
        frames[tag] = frame
    sampled = sorted({i for frame in frames.values() for cell in frame["cells"].values()
                      for i in cell["sample"]})
    return {
        "schema": SCHEMA,
        "scores_sha256": scores["sha256"],
        "snapshot_sha256": media["sha256"],
        "youth_tags": list(YOUTH_TAGS + YOUTH_NAMED_TAGS),
        "excluded_post_ids": exclusions,
        "thresholds": dict(thresholds),
        "per_cell": per_cell,
        "seed": seed,
        "frames": frames,
        "display_hashes": {str(i): hashes[i] for i in sampled},
    }


def summary(document):
    rows = {}
    for tag, frame in document["frames"].items():
        rows[tag] = {name: [cell["population"], len(cell["sample"])] for name, cell in frame["cells"].items()}
    return {"tags": len(rows), "excluded": {k: len(v) for k, v in document["excluded_post_ids"].items()},
            "sampled": len(document["display_hashes"]), "cells": rows,
            "frames_sha256": digest(document["frames"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scores", required=True, help="tagger-base-scores/2 from base_scoring")
    parser.add_argument("--media", required=True, help="media.json of the frozen snapshot")
    parser.add_argument("--dataset", required=True, help="the scored <id>.bin files")
    parser.add_argument("--incumbent-threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--candidate-threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--per-cell", type=int, default=DEFAULT_PER_CELL)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if Path(args.out).exists():
        raise SystemExit(f"{args.out} exists; frames are written once")
    scores = read_verified(args.scores, SCORES_SCHEMA)
    media = read_verified(args.media)
    ids = population(scores)
    from .kura_db import connect_canonical
    from api.tagger_core.review_censor_variants import excluded_with_cursor
    conn = connect_canonical(connect_timeout=5)
    conn.set_session(readonly=True)
    try:
        exclusions = {"youth": youth_posts(conn, ids), "training_opt_out": opt_out_posts(conn, ids)}
        with conn.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout='10s'")
            exclusions["censor_variant"] = excluded_with_cursor(cursor,ids)
    finally:
        conn.close()
    document = build(scores, media, exclusions, per_cell=args.per_cell,
                     thresholds={"incumbent": args.incumbent_threshold, "candidate": args.candidate_threshold})
    verify_scored_bytes(args.dataset, document)
    write_once(args.out, document)
    print(json.dumps(summary(document), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
