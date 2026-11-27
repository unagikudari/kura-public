import pytest
from tagger.dedicated_qualification import compare_predictions,quality_floor
from tagger.dedicated_protocol import ProtocolError


def test_parity_requires_all_ids_finite_values_and_same_decisions():
    assert compare_predictions({1:.2,2:.8},{1:.200001,2:.800001},.5)['passed']
    assert not compare_predictions({1:.49999},{1:.50001},.5)['passed']
    assert not compare_predictions({1:.2},{1:.21},.5)['passed']
    assert not compare_predictions({}, {}, .5)['passed']
    for actual in ({}, {1:float('nan')}, {1:float('inf')}, {1:1.1}, {1:.2,2:.3}):
        with pytest.raises(ProtocolError):compare_predictions({1:.2},actual,.5)


def test_configured_floor_fails_closed_no_implicit_defaults():
    floor={'min_precision':'.85','min_recall':'.7','min_val_pos_support':'100','min_pred_pos_support':'100'}
    m={'tp':180,'fp':20,'fn':20,'tn':780,'precision':.9,'recall':.9}
    assert quality_floor(m,floor)['passed']
    assert quality_floor(dict(m,tp=168,fp=32,fn=32,precision=.84,recall=.84),floor)['reasons']==['precision']
    assert quality_floor(dict(m,tp=138,fp=12,fn=62,precision=.92,recall=.69),floor)['reasons']==['recall']
    assert not quality_floor(dict(m,tp=3,precision=3/23,recall=3/23),floor)['passed']
    for f in ({}, dict(floor,min_precision='nan'), dict(floor,min_recall='2')):
        with pytest.raises(ProtocolError):quality_floor(m,f)


@pytest.mark.parametrize('changes', [
    {'precision':float('nan')}, {'recall':float('inf')},
    {'precision':1}, {'tp':-1}, {'fp':True}, {'fn':2.5},
])
def test_invalid_quality_evidence_cannot_pass(changes):
    m={'tp':180,'fp':20,'fn':20,'tn':780,'precision':.9,'recall':.9}
    floor={'min_precision':.85,'min_recall':.7,'min_val_pos_support':100,'min_pred_pos_support':100}
    with pytest.raises(ProtocolError):quality_floor(dict(m,**changes),floor)


def test_full_worker_probe_needs_actual_execution_and_same_decision(monkeypatch):
    from tagger import dedicated_qualification as q, infer

    identity = dict(tag="fixture", artifact_sha256="a" * 64, threshold=.5, apply_mode="replace")
    examples = [(1, b"image-a", "image/png", .49999), (2, b"image-b", "image/png", .8)]

    def fake(image, mime, models, notes):
        notes["executed_content_heads"] = [identity] if image == b"image-a" else []
        notes["executed_content_head_scores"] = {"fixture": .50001}

    monkeypatch.setattr(infer, "tag_image", fake)
    result = q.probe_full_worker({}, examples, **identity)
    assert (result["completed"], result["total"], result["parity_passed"]) == (1, 2, False)
    result = q.probe_full_worker({}, examples[:1], **identity)
    assert not result["parity_passed"] and result["parity"]["decision_differences"] == 1


def test_worker_trial_inputs_keep_both_sides_nearest_threshold():
    from tagger.dedicated_qualification import worker_trial_inputs

    scores = {8: .1, 9: .49, 10: .51, 11: .8}
    doc = {"media": [{"id": i, "blob_sha256": f"{i:064x}"} for i in scores]}
    rows = worker_trial_inputs(doc, scores, threshold=.5, per_side=1)
    assert rows == [
        {"post_id": i, "display_sha256": f"{i:064x}", "reference_score": scores[i]}
        for i in (9, 10)
    ]


def test_frozen_trial_refuses_changed_image_before_running_probe(tmp_path, monkeypatch):
    import hashlib
    from unittest.mock import Mock
    from tagger import dedicated_qualification as q, dedicated_protocol as p

    records = []
    for post_id, score in ((1, .4), (2, .6)):
        data = bytes([post_id])
        (tmp_path / f"{post_id}.bin").write_bytes(data)
        records.append(dict(post_id=post_id, reference_score=score,
                            display_sha256=hashlib.sha256(data).hexdigest()))
    manifest = p.write_once(tmp_path / "trial.json", {
        "schema": "tagger-worker-trial-inputs/1", "artifact_sha256": "a" * 64,
        "threshold": .5, "examples": records,
    })
    probe = Mock(return_value={"completed": 2})
    monkeypatch.setattr(q, "probe_wd14_worker", probe)
    args = dict(inputs_sha256=manifest["sha256"], artifact_sha256="a" * 64,
                threshold=.5, tag="fixture", apply_mode="replace")
    assert q.run_frozen_worker_trial("wd14", {}, tmp_path / "trial.json", tmp_path,
                                     **args)["completed"] == 2
    probe.reset_mock()
    (tmp_path / "2.bin").write_bytes(b"different")
    with pytest.raises(p.ProtocolError, match="image digest mismatch"):
        q.run_frozen_worker_trial("wd14", {}, tmp_path / "trial.json", tmp_path, **args)
    probe.assert_not_called()


def test_trial_staging_is_idempotent_and_checks_display_identity(tmp_path):
    import hashlib
    from tagger import dedicated_qualification as q, dedicated_protocol as p

    data = b"frozen"
    manifest = {"examples": [{"post_id": 1, "display_sha256": hashlib.sha256(data).hexdigest()}]}
    reader = lambda ids: [(ids[0], data)]
    assert q.stage_worker_trial_images(reader, manifest, tmp_path) == len(data)
    assert q.stage_worker_trial_images(reader, manifest, tmp_path) == len(data)
    assert (tmp_path / "1.bin").read_bytes() == data
    with pytest.raises(p.ProtocolError, match="image digest mismatch"):
        q.stage_worker_trial_images(lambda ids: [(1, b"changed")], manifest, tmp_path)


def test_trial_loader_requires_exact_release_and_uses_trial_inventory(monkeypatch):
    from unittest.mock import Mock
    from tagger import character_policy, dedicated_qualification as q, dedicated_protocol as p, infer

    monkeypatch.setattr(character_policy, "fetch",
                        lambda *a: {"release_id": 14, "threshold": None})
    policies = [{"tag": "fixture"}]
    load = Mock(return_value={"wd14_fixed": {"fallback": False, "release_id": 14}})
    monkeypatch.setattr(infer, "load_all_models", load)
    assert q.load_full_trial_inventory(release_id=14, policies=policies,
                                       api_url="https://fixture.invalid", headers={}) == load.return_value
    assert load.call_args.kwargs["trial_head_policies"] is policies
    load.return_value["wd14_fixed"]["fallback"] = True
    with pytest.raises(p.ProtocolError, match="trial release unavailable"):
        q.load_full_trial_inventory(release_id=14, policies=policies,
                                    api_url="https://fixture.invalid", headers={})
