from tagger.label_identity import label_identity, labels_changed


def test_same_count_correction_changes_identity():
    before = [(1, "positive", "human_correction"), (2, "negative", "human_correction")]
    after = [(1, "negative", "human_correction"), (2, "positive", "human_correction")]
    baseline = label_identity(before)
    assert baseline == label_identity(reversed(before))
    assert not labels_changed(baseline, label_identity(before))
    assert labels_changed(baseline, label_identity(after))


def test_unknown_baseline_does_not_count_as_a_correction():
    current = label_identity([])
    assert not labels_changed(None, current)
    assert not labels_changed("0" * 63 + "z", current)


def test_recover_legacy_baseline_only_from_pinned_snapshot(tmp_path):
    import hashlib

    from tagger.dedicated_protocol import SCHEMA, write_once
    from tagger.label_identity import registration_label_identity

    key = hashlib.sha256(b"fixture@42").hexdigest()[:24]
    rows = [(1, "positive", "human_correction"), (2, "negative", "human_correction")]
    snapshot = write_once(tmp_path / key / "snapshot.json", {
        "schema": SCHEMA, "tag": "fixture", "status": "prepared",
        "gt": [dict(id=i, verdict=v, derived_from=d) for i, v, d in rows],
    })
    request = {
        "gt": {"positive": 1, "negative": 1}, "lifecycle_event_id": 42,
        "metrics": {"snapshot_sha256": snapshot["sha256"]},
    }
    baseline = registration_label_identity("fixture", request, root=tmp_path)
    assert baseline == label_identity(rows)
    swapped = [(1, "negative", "human_correction"), (2, "positive", "human_correction")]
    assert labels_changed(baseline, label_identity(swapped))
    assert registration_label_identity("fixture", request | {"lifecycle_event_id": 43}, root=tmp_path) is None
    assert registration_label_identity(
        "fixture", request | {"metrics": {"snapshot_sha256": "0" * 64}}, root=tmp_path,
    ) is None
    assert registration_label_identity(
        "fixture", request | {"gt": {"positive": 2, "negative": 1}}, root=tmp_path,
    ) is None
