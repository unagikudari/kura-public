import json

from api.tagger_core import review_refill as refill


def test_shortfall_reasons_distinguish_training_and_prediction():
    assert refill.support_trigger("serving_quality_failed:prediction_support") == (False, True)
    assert refill.support_trigger("hold: insufficient_human_split_support") == (True, False)
    assert refill.support_trigger("serving_quality_failed:precision") == (False, False)


def test_support_candidates_follow_required_polarities():
    rows = [{"post_id": i, "tagged": yes} for i, yes in enumerate((False, True, False, True))]
    def picked(positive, negative):
        return [r["post_id"] for r in refill.support_order(rows, dict(positive=positive, negative=negative))]
    assert picked(3, 0) == [1, 3]
    assert picked(0, 3) == [0, 2]
    assert picked(3, 1) == [1, 0, 3, 2]
    assert picked(1, 3) == [0, 1, 2, 3]


def test_support_documents_bind_images_and_retire_legacy_predecessor():
    documents = refill.support_documents("no_humans", "f" * 64, [(4, "a" * 64, True)], "e" * 64)
    blobs = {
        refill.digest(item) + ".json": json.dumps(item, sort_keys=True, separators=(",", ":")).encode()
        for item in documents
    }
    old, current = sorted(refill.parse_queues(blobs, canonical=True), key=lambda q: q["schema"])
    assert old["superseded_by"] == current["queue_sha256"]
    assert current["purpose"] == "training_support"
    assert current["media_sha256"] == {"4": "a" * 64}
    refill.review_lanes([current], set())
    assert current["review_lane"] == "lifecycle"
