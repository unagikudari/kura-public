"""Synthetic protocol contracts; no real images, human annotations or GPU runs."""
import hashlib
import json
from pathlib import Path

import pytest

from tagger import dedicated_protocol as d
from tagger import vnext_arms as arms
from tagger import feature_contract as fc


def test_connected_closure_uses_unlabelled_bridges_and_transitive_edges():
    groups = d.connected_groups([1, 2, 3, 4, 5], [(1, 3), (3, 4), (2, 4)])
    assert groups[1] == groups[2] == groups[3] == groups[4]
    assert groups[5] != groups[1]
    with pytest.raises(d.ProtocolError, match="absent endpoint"):
        d.connected_groups([1], [(1, 2)])


def test_fixed_split_keeps_every_human_negative_and_component():
    labels = {i: int(i < 30) for i in range(150)}
    groups = d.connected_groups(list(labels), [(0, 100), (1, 101), (100, 101)])
    split = d.fixed_split(labels, groups, positive=6, negative=20, seed=7)
    assert split == d.fixed_split(dict(reversed(list(labels.items()))), groups,
                                   positive=6, negative=20, seed=7)
    train, held = set(split["train"]), set(split["heldout"])
    assert train | held == set(labels)
    assert not train & held
    assert not {groups[i] for i in train} & {groups[i] for i in held}
    assert sum(labels[i] for i in held) == 6
    assert sum(not labels[i] for i in held) == 20
    assert sum(not labels[i] for i in train) == 100  # no neg cap


def test_impossible_grouping_fails_instead_of_breaking_component():
    with pytest.raises(d.ProtocolError, match="infeasible"):
        d.fixed_split({1: 1, 2: 0, 3: 1, 4: 0}, {i: 1 for i in range(1, 5)},
                      positive=1, negative=1)


def test_snapshots_create_only_and_detect_changed_content(tmp_path):
    path = tmp_path / "snapshot.json"
    doc = d.write_once(path, {"schema": "example", "ids": [1, 2]})
    assert d.read_verified(path, "example") == doc
    assert d.write_once(path, {"schema": "example", "ids": [1, 2]}) == doc
    with pytest.raises(d.ProtocolError, match="immutable"):
        d.write_once(path, {"ids": [1, 3]})
    doc["ids"].append(3)
    path.write_text(json.dumps(doc))
    with pytest.raises(d.ProtocolError, match="digest/schema"):
        d.read_verified(path)


def test_only_human_labels_enter_split_even_implicit_positive(tmp_path):
    labels = {i: int(i < 15) for i in range(60)}
    records = [{"id": i, "verdict": "positive" if v else "negative",
                "derived_from": "human_correction", "media_exclusion": None} for i, v in labels.items()]
    records += [{"id": 70, "verdict": "positive", "derived_from": "implicit_review", "media_exclusion": None},
                {"id": 71, "verdict": "negative", "derived_from": "human_correction", "media_exclusion": "missing_blob"}]
    doc = {"status": "prepared", "graph_nodes": list(range(72)), "graph_edges": [],
           "gt": records, "seed": 1, "parameters": {"positive": 3, "negative": 10, "tolerance": 0.1},
           "grouping_policy": d.POLICY}
    doc["split"] = d.fixed_split(labels, {i: i for i in range(72)}, positive=3, negative=10, seed=1)
    actual, _ = d.validate_snapshot(doc)
    assert actual == labels
    doc["split"]["train"].append(70)
    with pytest.raises(d.ProtocolError, match="split/policy"):
        d.validate_snapshot(doc)


def test_calibration_and_final_fit_never_see_heldout(monkeypatch):
    labels = {1: 1, 2: 0, 3: 1, 4: 0}
    calls = []

    def oof(getv, pos, neg, groups, **kw):
        calls.append(("calibrate", set(pos+neg)))
        return {1: 0.8, 2: 0.2}, {1: 1, 2: 0}, [], {"manifest_hash": "test"}

    def fit(getv, pos, neg, **kw):
        calls.append(("fit", set(pos+neg)))
        return {"synthetic": True}

    monkeypatch.setattr(arms, "grouped_oof", oof)
    monkeypatch.setattr(arms, "fit_deployable", fit)
    _, threshold, _ = d.fit_candidate(lambda i: i, labels, {i: i for i in labels},
               {"train": [1, 2], "heldout": [3, 4]}, tag="fixture", seed=1)
    assert calls == [("calibrate", {1, 2}), ("fit", {1, 2})]
    assert 0.2 < threshold <= 0.8
    with pytest.raises(d.ProtocolError, match="group overlap"):
        d.fit_candidate(lambda i: i, labels, {1: 9, 2: 2, 3: 9, 4: 4},
                        {"train": [1, 2], "heldout": [3, 4]}, tag="fixture", seed=1)


def test_incomplete_oof_blocks_final_fit(monkeypatch):
    monkeypatch.setattr(arms, "grouped_oof", lambda *a, **kw: ({1: 0.7}, {1: 1, 2: 0}, [], {}))
    monkeypatch.setattr(arms, "fit_deployable", lambda *a, **kw: pytest.fail("must not fit"))
    with pytest.raises(d.ProtocolError, match="incomplete training OOF"):
        d.fit_candidate(lambda i: i, {1: 1, 2: 0}, {1: 1, 2: 2},
                        {"train": [1, 2], "heldout": []}, tag="fixture", seed=1)


def test_changed_or_missing_media_aborts_before_feature_extraction(monkeypatch, tmp_path):
    monkeypatch.setattr(d, "_media_rows", lambda *a: [{"id": 1, "exclusion": "missing_blob", "blob_sha256": None}])
    monkeypatch.setattr(fc, "extract", lambda *a, **k: pytest.fail("must not extract"))
    with pytest.raises(d.ProtocolError, match='"id": 1'):
        d.features(None, [1], {"media": [{"id": 1, "blob_sha256": "old"}]}, tmp_path)


def test_decode_failure_never_silently_drops_a_label(monkeypatch, tmp_path):
    monkeypatch.setattr(d, "_media_rows", lambda *a: [{"id": 1, "exclusion": None, "blob_sha256": "same"}])
    monkeypatch.setattr(fc, "extract", lambda *a, **kw: ({}, {"input_sha256": {}}))
    with pytest.raises(d.ProtocolError, match="feature inputs differ"):
        d.features(None, [1], {"sha256": "test", "media": [{"id": 1, "blob_sha256": "same"}]}, tmp_path)


def review_fixture(tmp_path):
    sample = d.write_once(tmp_path / "sample.json", {
        "schema": "dedicated-precision-sample/1", "sample_ids": [1, 2, 3],
        "status": "awaiting_human_review", "estimand": "test-only", "scope_limit": "test-only",
        "sample_group_overlap": {"1": "train", "2": "unseen", "3": "unseen"}})
    reviews = {"label_source": "human", "sample_sha256": sample["sha256"], "human_reviews": [
        {"id": i, "label": "positive" if i < 3 else "negative", "reviewer": "synthetic-test-reviewer",
         "reviewed_at": "2026-09-13T06:31:00+09:00"} for i in (1, 2, 3)]}
    return reviews


def test_separate_precision_requires_real_review_not_predictions(tmp_path):
    reviews = review_fixture(tmp_path)
    path = tmp_path / "reviews.json"
    path.write_text(json.dumps(reviews))
    result = d.precision_evaluate(tmp_path / "sample.json", path, tmp_path / "result.json")
    assert result["precision"] == 2/3
    assert result["n"] == 3
    assert result["sample_group_overlap_counts"] == {"train": 1, "unseen": 2}
    assert result["precision_by_overlap_diagnostic"]["unseen"]["precision"] == 0.5
    assert result["wilson_95"][0] < 2/3 < result["wilson_95"][1]
    assert result["registration_eligible"] is False
    reviews["label_source"] = "model"
    path.write_text(json.dumps(reviews))
    with pytest.raises(d.ProtocolError, match="human review"):
        d.precision_evaluate(tmp_path / "sample.json", path, tmp_path / "wrong.json")


@pytest.mark.parametrize("change", ["null_label", "absent_reviewer", "missing_id", "duplicate_id", "wrong_hash", "bad_timestamp"])
def test_precision_refuses_partial_unattributed_or_replaced_sample(tmp_path, change):
    reviews = review_fixture(tmp_path)
    if change == "null_label": reviews["human_reviews"][0]["label"] = None
    if change == "absent_reviewer": reviews["human_reviews"][0]["reviewer"] = None
    if change == "missing_id": reviews["human_reviews"].pop()
    if change == "duplicate_id": reviews["human_reviews"][1]["id"] = 1
    if change == "wrong_hash": reviews["sample_sha256"] = "not-the-sample"
    if change == "bad_timestamp": reviews["human_reviews"][0]["reviewed_at"] = "sometime"
    path = tmp_path / "reviews.json"
    path.write_text(json.dumps(reviews))
    with pytest.raises(d.ProtocolError):
        d.precision_evaluate(tmp_path / "sample.json", path, tmp_path / "wrong.json")


def test_uniform_sample_uses_complete_predicted_positive_frame():
    frame = list(range(1000))
    scores = {i: 0.8 if i % 2 else 0.1 for i in frame}
    positives, sample = d.sample_predictions(frame, scores, threshold=0.5, n=300, seed=5)
    assert len(positives) == 500 and len(sample) == len(set(sample)) == 300
    assert set(sample) <= set(positives)
    assert max(sample) > 900  # not an early-ID LIMIT or ranked top 300
    assert (positives, sample) == d.sample_predictions(list(reversed(frame)), scores,
                                                       threshold=0.5, n=300, seed=5)
    with pytest.raises(d.ProtocolError, match="truncated frame"):
        d.sample_predictions(frame, {1: 0.8}, threshold=0.5, n=300, seed=5)
    scores[1] = float("nan")
    with pytest.raises(d.ProtocolError, match="nonfinite"):
        d.sample_predictions(frame, scores, threshold=0.5, n=300, seed=5)


def test_production_frame_retains_training_and_holdout_members():
    doc = {"production_frame": [1, 2, 3, 4], "split": {"train": [1], "heldout": [2]},
           "gt": [{"id": 1}, {"id": 2}],
           "media": [{"id": i, "exclusion": "missing_blob" if i == 4 else None} for i in range(1, 5)]}
    frame, exclusions = d.precision_frame(doc)
    assert frame == [1, 2, 3]  # GT membership never conditions the production sample
    assert exclusions == [{"id": 4, "reason": "missing_blob"}]


def test_independent_review_excludes_connected_known_labels():
    labels = {i: int(i <= 15) for i in range(1, 61)}
    doc = {
        "status": "prepared", "graph_nodes": list(range(1, 65)),
        "graph_edges": [[1, 61]], "seed": 1, "grouping_policy": d.POLICY,
        "parameters": {"positive": 3, "negative": 10, "tolerance": 0.1},
        "gt": [{"id": i, "verdict": "positive" if v else "negative",
                "derived_from": "human_correction", "media_exclusion": None}
               for i, v in labels.items()],
        "production_frame": [1, 61, 62, 63],
        "media": [{"id": i, "exclusion": "missing_blob" if i == 63 else None}
                  for i in (1, 61, 62, 63)],
    }
    groups = d.connected_groups(doc["graph_nodes"], doc["graph_edges"])
    doc["split"] = d.fixed_split(labels, groups, positive=3, negative=10, seed=1)
    ids, exclusions = d.independent_production_frame(doc)
    assert ids == [62]
    assert exclusions == [
        {"id": 1, "reason": "known_label_group"},
        {"id": 61, "reason": "known_label_group"},
        {"id": 63, "reason": "missing_blob"},
    ]


def test_catalog_sample_covers_untagged_posts_and_rejects_changed_membership(monkeypatch):
    doc = {"seed": 1, "graph_nodes": [1, 2, 3, 4], "gt": [{"id": 1}],
           "split": {"train": [1], "heldout": []}, "production_frame": [1, 2],
           "media": [{"id": i, "exclusion": None} for i in (1, 2, 3, 4)],
           "catalog_sample": {"ids": [2, 3, 4], "population_n": 3, "requested_n": 3}}
    monkeypatch.setattr(d, "validate_snapshot", lambda _: ({1: 1}, {i: i for i in range(1, 5)}))
    assert d.independent_production_frame(doc) == ([2, 3, 4], [])
    assert d.production_score_scope(doc) == "catalog_sample_outside_frozen_gt_groups"
    doc["catalog_sample"]["ids"] = [1, 2, 3]
    with pytest.raises(d.ProtocolError, match="catalog sample membership mismatch"):
        d.independent_production_frame(doc)


def test_a_snapshot_under_the_earlier_policy_still_validates(monkeypatch):
    # Earlier policies are known by the sha256 of their name only.
    earlier = "an-earlier-grouping-policy"
    monkeypatch.setattr(d, "_EARLIER_POLICIES", {hashlib.sha256(earlier.encode()).hexdigest()})
    labels = {i: int(i < 15) for i in range(60)}
    records = [{"id": i, "verdict": "positive" if v else "negative",
                "derived_from": "human_correction", "media_exclusion": None} for i, v in labels.items()]
    doc = {"status": "prepared", "graph_nodes": list(range(60)), "graph_edges": [], "gt": records, "seed": 1,
           "parameters": {"positive": 3, "negative": 10, "tolerance": 0.1}, "grouping_policy": earlier}
    doc["split"] = d.fixed_split(labels, {i: i for i in range(60)}, positive=3, negative=10, seed=1)
    assert d.validate_snapshot(doc)[0] == labels
    doc["grouping_policy"] = "some-other-policy"
    with pytest.raises(d.ProtocolError, match="split/policy"):
        d.validate_snapshot(doc)


@pytest.fixture(autouse=True)
def _fresh_installed_cache():
    from api import extension_hooks

    extension_hooks.installed.cache_clear()
    yield
    extension_hooks.installed.cache_clear()


def _runtime(tmp_path, monkeypatch, *, files, installed_commit="a" * 40, pinned_commit="a" * 40):
    """A composed runtime under tmp_path: the files an extension installed and, when
    pinned_commit is not None, the private pin file."""
    from api import extension_hooks

    recorded = {}
    for relative, text in files.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        recorded[relative] = hashlib.sha256(text.encode()).hexdigest()
    if installed_commit is not None:
        (tmp_path / ".kura-extensions.json").write_text(json.dumps({"schema": 1, "extensions": [
            {"id": "ext", "version": "1.0.0", "commit": installed_commit, "files": recorded}]}))
    if pinned_commit is not None:
        (tmp_path / "release").mkdir()
        (tmp_path / "release/extension_sources.json").write_text(json.dumps({"schema": 1, "extensions": {
            "ext": {"repository": "x", "commit": pinned_commit, "version": "1.0.0"}}}))
    monkeypatch.setattr(extension_hooks, "_ROOT", tmp_path)
    extension_hooks.installed.cache_clear()
    monkeypatch.setattr(d, "WORK_EDGES", tmp_path / "tagger/work_edges")


def test_work_edges_are_the_sql_files_the_pinned_extensions_installed(tmp_path, monkeypatch):
    _runtime(tmp_path, monkeypatch, files={"tagger/work_edges/b.sql": "SELECT 2, 1;\n",
                                                   "tagger/work_edges/a.sql": "SELECT 1, 1"})
    assert d.work_edge_sql() == {"a.sql": "SELECT 1, 1", "b.sql": "SELECT 2, 1"}


def test_a_core_only_install_has_no_work_edges(tmp_path, monkeypatch):
    _runtime(tmp_path, monkeypatch, files={}, installed_commit=None, pinned_commit=None)
    assert d.work_edge_sql() == {}


def test_a_checkout_pulled_but_not_recomposed_is_refused(tmp_path, monkeypatch):
    # the pin moved to a release whose extension adds the SQL; the node still runs the old one
    _runtime(tmp_path, monkeypatch, files={}, installed_commit="a" * 40, pinned_commit="b" * 40)
    with pytest.raises(RuntimeError, match="pinned set"):
        d.work_edge_sql()


def test_a_missing_declared_sql_file_is_refused(tmp_path, monkeypatch):
    _runtime(tmp_path, monkeypatch, files={"tagger/work_edges/a.sql": "SELECT 1, 1"})
    (tmp_path / "tagger/work_edges/a.sql").unlink()
    with pytest.raises(RuntimeError, match="a.sql"):
        d.work_edge_sql()


def test_an_sql_file_no_extension_declares_is_refused(tmp_path, monkeypatch):
    _runtime(tmp_path, monkeypatch, files={"tagger/work_edges/a.sql": "SELECT 1, 1"})
    (tmp_path / "tagger/work_edges/left_over.sql").write_text("SELECT 3, 4")
    with pytest.raises(d.ProtocolError, match="left_over.sql"):
        d.work_edge_sql()


def test_dedicated_extraction_binds_contract_not_legacy_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(d, "_media_rows", lambda *a: [{"id": 1, "exclusion": None, "blob_sha256": "same"}])
    evidence = {"input_sha256": {"1": "same"}, "identity": {"preprocessing_sha256": "explicit"}, "backbone_path": "frozen.pt"}
    monkeypatch.setattr(fc, "extract", lambda *a, **kw: ({1: "feature"}, evidence))
    monkeypatch.setattr(arms, "_extract_features", lambda *a, **kw: pytest.fail("legacy extraction forbidden"))
    doc = {"sha256": "snapshot", "media": [{"id": 1, "blob_sha256": "same"}]}
    assert d.features(None, [1], doc, tmp_path) == {1: "feature"}
    saved = d.read_verified(tmp_path / "backbone-v2.json", "dedicated-backbone/2")
    assert saved["identity"] == evidence["identity"]
    evidence["identity"] = {"preprocessing_sha256": "changed"}
    with pytest.raises(d.ProtocolError, match="immutable output"):
        d.features(None, [1], doc, tmp_path)


def test_uncertain_human_review_is_neither_negative_nor_silently_dropped(tmp_path):
    reviews = review_fixture(tmp_path)
    reviews['human_reviews'][1]['label'] = 'uncertain'
    path = tmp_path / 'reviews.json'; path.write_text(json.dumps(reviews))
    result = d.precision_evaluate(tmp_path / 'sample.json', path, tmp_path / 'result.json')
    assert result['schema'] == 'dedicated-human-precision/2'
    assert (result['n'], result['true_positive'], result['false_positive'], result['uncertain']) == (3, 1, 1, 1)
    assert result['precision'] is None and result['wilson_95'] is None
    assert result['precision_on_decidable'] == .5
    assert result['decidable_fraction'] == 2/3
    assert result['unresolved_precision_bounds'] == [1/3, 2/3]
    unseen = result['precision_by_overlap_diagnostic']['unseen']
    assert unseen['n'] == 2 and unseen['uncertain'] == 1 and unseen['precision'] is None
    assert result['registration_eligible'] is False


def test_all_uncertain_reviews_do_not_claim_perfect_precision(tmp_path):
    reviews = review_fixture(tmp_path)
    for row in reviews['human_reviews']: row['label'] = 'uncertain'
    path = tmp_path / 'reviews.json'; path.write_text(json.dumps(reviews))
    result = d.precision_evaluate(tmp_path / 'sample.json', path, tmp_path / 'result.json')
    assert result['precision_on_decidable'] is None and result['precision'] is None
    assert result['false_positive'] == 0 and result['true_positive'] == 0
    assert result['uncertain'] == 3 and result['decidable_fraction'] == 0
    assert result['unresolved_precision_bounds'] == [0, 1]


def test_feasible_holdout_reduces_only_proven_unreachable_positive_target():
    labels = {post: int(post < 24) for post in range(54)}
    groups = {post: 0 if post < 18 else post for post in labels}
    floor = {"min_val_pos_support": "5", "min_held_out_samples": "8",
             "min_gt_positive": "10", "min_gt_negative": "10"}
    settings = {"positive": 8, "negative": 10, "seed": 7, "tolerance": .1}
    target, split = d.feasible_holdout(labels, groups, **settings, floor=floor)
    assert target == split["heldout_positive"] == 6
    assert split["heldout_negative"] == 10
    assert set(range(18)) <= set(split["train"])
    assert not ({groups[post] for post in split["heldout"]} & {groups[post] for post in split["train"]})
    with pytest.raises(d.ProtocolError, match="existing support floors"):
        d.feasible_holdout(labels, groups, **settings, floor={**floor, "min_val_pos_support": "7"})
    target, split = d.feasible_holdout(labels, groups, **(settings | {"positive": 5}), floor=floor)
    assert target == split["heldout_positive"] == 5


def test_automatic_holdout_reserves_training_groups_for_all_calibration_folds():
    labels = {post: int(post < 100) for post in range(200)}
    groups = {post: 0 if post < 88 else post for post in labels}
    floor = {"min_val_pos_support": "6", "min_held_out_samples": "10",
             "min_gt_positive": "10", "min_gt_negative": "10"}
    settings = {"positive": 18, "negative": 30, "seed": 7, "tolerance": .1, "floor": floor}
    _, first = d.feasible_holdout(labels, groups, **settings)
    assert not d.calibration_has_both_classes(labels, groups, first["train"], 7)
    target, chosen = d.calibratable_holdout(labels, groups, **settings)
    assert target == 8
    assert chosen["heldout_positive"] == 8
    assert d.calibration_has_both_classes(labels, groups, chosen["train"], 7)
    assert not ({groups[i] for i in chosen["heldout"]} & {groups[i] for i in chosen["train"]})


def test_catalog_extension_keeps_old_population_and_training_groups_out(monkeypatch):
    from tagger.catalog_extension import plan

    parent = {
        "tag": "fixture", "sha256": "snapshot", "seed": 17,
        "graph_nodes": list(range(1, 8)), "gt": [{"id": 1}],
        "split": {"train": [1], "heldout": []},
        "media": [{"id": i, "exclusion": None} for i in range(1, 8)],
        "catalog_sample": {"ids": d.sample_catalog_ids(range(3, 8), size=2, seed=17),
                           "requested_n": 2, "population_n": 5},
    }
    monkeypatch.setattr(d, "validate_snapshot", lambda _: ({1: 1}, {1: 1, 2: 1, **{i: i for i in range(3, 8)}}))
    measurement = {"tag": "fixture", "sha256": "measurement", "snapshot_sha256": "snapshot",
                   "bundle_sha256": "bundle", "threshold": .75}
    scores = {"tag": "fixture", "sha256": "scores", "snapshot_sha256": "snapshot",
              "measurement_sha256": "measurement", "model_sha256": "bundle", "threshold": .75,
              "scope": d.production_score_scope(parent), "exclusions": [],
              "scores": [(i, .5) for i in parent["catalog_sample"]["ids"]]}

    extension = plan(parent, measurement, scores, 10)
    assert extension["post_ids"] == sorted(set(range(3, 8)) - set(parent["catalog_sample"]["ids"]))
    assert extension["model_sha256"] == "bundle"
    with pytest.raises(d.ProtocolError, match="binding mismatch"):
        plan(parent, measurement, dict(scores, threshold=.65), 10)


def test_catalog_extension_preserves_opt_out_and_prior_media_exclusions(monkeypatch):
    from tagger import catalog_extension, vnext_arms

    parent = {"gt": [{"id": 1}], "catalog_sample": {"ids": [2]},
              "split": {"train": [1], "heldout": []},
              "media": [{"id": 1, "blob_sha256": "old-training"},
                        {"id": 2, "blob_sha256": "old-score"}]}
    selected = {"tag": "fixture", "post_ids": [3, 4, 5]}
    monkeypatch.setattr(vnext_arms, "q", lambda *_: [(3,), (5,)])
    monkeypatch.setattr(d, "_media_rows", lambda _, ids: [
        {"id": i, "blob_sha256": {3: "old-score", 5: "new"}[i], "exclusion": None} for i in ids
    ])
    receipt = catalog_extension.freeze_media(None, parent, selected)
    assert receipt["plan_sha256"] == d.digest(selected)
    assert [(row["id"], row["exclusion"]) for row in receipt["media"]] == [
        (3, "previous_sample_or_training_bytes"), (4, "no_longer_eligible"), (5, None),
    ]


def test_catalog_extension_exports_only_bound_eligible_hashes():
    from tagger.catalog_extension import media_hashes

    selection = {"post_ids": [1, 2]}
    valid = {"plan_sha256": d.digest(selection), "media": [
        {"id": 1, "exclusion": None, "blob_sha256": "a" * 64},
        {"id": 2, "exclusion": "no_longer_eligible", "blob_sha256": None},
    ]}
    assert media_hashes(selection, valid) == {1: "a" * 64}
    with pytest.raises(d.ProtocolError, match="does not match selected posts"):
        media_hashes(selection, dict(valid, media=valid["media"][1:]))


def test_catalog_extension_scoring_rejects_changed_bytes(tmp_path, monkeypatch):
    from tagger import catalog_extension, dedicated_qualification

    media_file = tmp_path / "1.bin"
    media_file.write_bytes(b"fixture")
    sha256 = hashlib.sha256(media_file.read_bytes()).hexdigest()
    selection = {"tag": "fixture", "post_ids": [1], "model_sha256": "a" * 64, "seed": 17,
                 "measurement_sha256": "measurement", "previous_scores_sha256": "previous",
                 "threshold": .75}
    plan_sha = d.digest(selection)
    media = d.write_once(tmp_path / "media.json", {
        "schema": "tagger-catalog-extension-media/1", "plan_sha256": plan_sha,
        "media": [{"id": 1, "blob_sha256": sha256, "exclusion": None}],
    })
    d.write_once(tmp_path / "dataset.json", {
        "schema": "tagger-catalog-extension-dataset/1", "plan_sha256": plan_sha,
        "media_sha256": media["sha256"], "images": {"1": sha256},
    })

    def score(bundle, digest, reader, ids, *, device):
        assert digest == "a" * 64
        return {post_id: .8 for post_id, _ in reader(ids)}

    monkeypatch.setattr(dedicated_qualification, "score_images", score)
    result = catalog_extension.score_extension(selection, media, tmp_path, None, tmp_path / "scores.json")
    assert result["scores"] == [(1, .8)]
    assert result["threshold"] == .75
    dataset = d.read_verified(tmp_path / "dataset.json", "tagger-catalog-extension-dataset/1")
    frame = catalog_extension.review_frame(selection, media, dataset, result, 4)
    assert frame["sample"] == {"predicted_present": [1], "predicted_absent": []}
    review = catalog_extension.prepare_review(
        selection, media, dataset, result, per_stratum=4, minimum_predicted_positive=2,
    )
    assert review["state"] == "production_evaluation_insufficient_support"
    assert "request" not in review
    with pytest.raises(d.ProtocolError, match="evaluation inputs"):
        catalog_extension.review_frame(selection, media, dataset, dict(result, threshold=.65), 4)
    media_file.write_bytes(b"changed")
    with pytest.raises(d.ProtocolError, match="dataset bytes changed"):
        catalog_extension.score_extension(selection, media, tmp_path, None, tmp_path / "changed.json")


def test_catalog_extension_refuses_new_opt_out_before_export(monkeypatch):
    from tagger import catalog_extension

    monkeypatch.setattr(catalog_extension, "eligible_now", lambda conn, ids, tag: {1})
    with pytest.raises(d.ProtocolError, match="no longer eligible"):
        catalog_extension.require_current_media(None, [1, 2], "fixture")


def test_catalog_extension_review_binds_both_candidate_and_worker_inputs():
    from tagger.catalog_extension import prepare_review, registration_review

    selection = {"tag": "fixture", "post_ids": [1, 2], "model_sha256": "a" * 64,
                 "seed": 17, "previous_scores_sha256": "previous", "snapshot_sha256": "snapshot",
                 "measurement_sha256": "measurement", "threshold": .75}
    plan_sha = d.digest(selection)
    media = {"sha256": "media", "plan_sha256": plan_sha, "media": [
        {"id": i, "blob_sha256": f"{i:064x}", "exclusion": None} for i in (1, 2)
    ]}
    dataset = {"sha256": "dataset", "plan_sha256": plan_sha, "media_sha256": "media",
               "images": {str(row["id"]): row["blob_sha256"] for row in media["media"]}}
    scores = {key: selection[key] for key in (
        "tag", "model_sha256", "threshold", "measurement_sha256", "previous_scores_sha256",
    )}
    scores.update(sha256="scores", plan_sha256=plan_sha, media_sha256="media",
                  dataset_sha256="dataset", scores=[(1, .8), (2, .2)])
    proposal = prepare_review(selection, media, dataset, scores, per_stratum=4,
                              minimum_predicted_positive=1)
    assert proposal["state"] == "review_available"
    assert proposal["request"]["source_provenance"]["measurement_sha256"] == "measurement"
    assert proposal["worker_trial_inputs"]["artifact_sha256"] == selection["model_sha256"]
    assert {row["post_id"] for row in proposal["worker_trial_inputs"]["examples"]} == {1, 2}
    assert d.digest(proposal["worker_trial_inputs"]) == proposal["worker_trial_inputs_sha256"]
    payload = registration_review(proposal)
    assert payload["scope"] == "catalog_extension_outside_frozen_gt_groups"
    assert payload["frame_sha256"] == payload["request"]["frame_sha256"]
    assert payload["catalog_extension_plan_sha256"] == plan_sha


def test_catalog_sample_reconstructs_frozen_exclusions(monkeypatch):
    doc = {
        "seed": 1, "graph_nodes": [1, 2, 3, 4], "gt": [{"id": 1}],
        "split": {"train": [1], "heldout": []},
        "media": [{"id": 2, "exclusion": None}, {"id": 4, "exclusion": None}],
        "catalog_sample": {"ids": [2, 4], "population_n": 2, "requested_n": 2,
                           "excluded_ids": [3]},
    }
    monkeypatch.setattr(d, "validate_snapshot", lambda _: ({1: 1}, {i: i for i in range(1, 5)}))
    assert d.independent_production_frame(doc) == ([2, 4], [])
    doc["catalog_sample"]["excluded_ids"] = [1]
    with pytest.raises(d.ProtocolError, match="catalog sample exclusion inventory mismatch"):
        d.independent_production_frame(doc)
