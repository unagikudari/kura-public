"""Recognize a paired review by its immutable frame identity."""
from api.services.human_review import digest


JOINT_REVIEW_PAIR = ("double_penetration", "triple_penetration")


def paired_review_frame(tags, ids, hashes):
    return digest({"kind": "exclusive_pair_review", "tags": sorted(tags),
                   "posts": ids, "media_sha256": hashes})


def is_paired_review(queue):
    return (
        queue.get("tag") in JOINT_REVIEW_PAIR
        and queue.get("purpose") == "historical_label_recheck"
        and queue.get("schema") == "human-review-set/2"
        and queue["frame_sha256"] == paired_review_frame(
            JOINT_REVIEW_PAIR, queue["post_ids"], queue["media_sha256"])
    )
