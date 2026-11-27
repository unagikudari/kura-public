from random import Random

import pytest

from tagger import base_comparison as bc

A, B = "a" * 64, "b" * 64


def population(n=2000, seed=1, candidate_noise=0.05, incumbent_noise=0.20):
    """Synthetic truth; each model scores the truth with its own error rate."""
    rng = Random(seed)
    truth = {i: rng.random() < 0.3 for i in range(1, n + 1)}
    def scores(noise):
        out = {}
        for i, t in truth.items():
            correct = rng.random() >= noise
            out[i] = 0.9 if (t == correct) else 0.1
        return out
    return truth, scores(incumbent_noise), scores(candidate_noise)


def answers(frame, truth):
    return {i: ("present" if truth[i] else "absent")
            for cell in frame["cells"].values() for i in cell["sample"]}


def frame_for(tag, inc, cand, **kw):
    return bc.joint_frame(tag, {"model_sha256": A, "threshold": 0.5, "scores": inc},
                          {"model_sha256": B, "threshold": 0.5, "scores": cand}, seed=7, **kw)


def test_frame_is_reproducible_and_stratified_over_both_models():
    truth, inc, cand = population()
    frame = frame_for("t", inc, cand)
    assert frame == frame_for("t", inc, cand)
    assert (frame["seed"], frame["per_cell"]) == ("7", 15) and len(frame["scores_sha256"]) == 64
    assert set(frame["cells"]) == set(bc.CELLS)
    assert all(len(c["sample"]) == min(15, c["population"]) for c in frame["cells"].values())
    assert sum(c["population"] for c in frame["cells"].values()) == len(truth)
    with pytest.raises(ValueError, match="population_mismatch"):
        bc.joint_frame("t", {"model_sha256": A, "threshold": .5, "scores": inc},
                       {"model_sha256": B, "threshold": .5, "scores": {1: .5}}, seed=1)


def test_a_better_candidate_is_admissible_and_a_regression_is_not():
    truth, inc, cand = population()
    better = bc.compare(frame_for("better", inc, cand, per_cell=60), answers(frame_for("better", inc, cand, per_cell=60), truth))
    assert better["complete"] and better["difference"]["f1"] > 0
    assert not better["significant_regression"]
    worse_frame = frame_for("worse", cand, inc, per_cell=60)  # roles swapped: candidate is the noisy one
    worse = bc.compare(worse_frame, answers(worse_frame, truth))
    assert worse["significant_regression"] and worse["difference"]["f1"] < 0
    ok = bc.verdict([better], priority_tags={"better"}, compared_tags={"better"})
    assert ok["admissible"] and ok["reasons"] == []
    worse["models"] = better["models"]  # same pair of models, opposite outcome on this tag
    blocked = bc.verdict([better, worse], priority_tags={"better", "worse"},
                         compared_tags={"better", "worse"})
    assert not blocked["admissible"] and blocked["regressed_priority_tags"] == ["worse"]
    # A compared tag without a result counts against the switch.
    assert bc.verdict([better], priority_tags={"better"}, compared_tags={"better", "gone"})[
        "unanswered_tags"] == ["gone"]
    with pytest.raises(ValueError, match="duplicate_tag"):
        bc.verdict([better, better], priority_tags=set(), compared_tags={"better"})
    mixed = dict(better, tag="other", models=["c" * 64, B])
    with pytest.raises(ValueError, match="models_mixed"):
        bc.verdict([better, mixed], priority_tags=set(), compared_tags={"better", "other"})
    with pytest.raises(ValueError, match="rounds_too_few"):
        bc.compare(frame_for("better", inc, cand), {}, rounds=10)


def test_a_priority_tag_the_candidate_never_predicts_is_undecidable():
    truth, inc, _ = population()
    silent = {i: 0.0 for i in inc}
    frame = frame_for("silent", inc, silent)
    result = bc.compare(frame, answers(frame, truth))
    assert result["complete"] and not result["decidable"]
    decision = bc.verdict([result], priority_tags={"silent"}, compared_tags={"silent"})
    assert "priority_tag_undecidable" in decision["reasons"] and not decision["admissible"]


def test_unanswered_priority_tags_block_the_switch():
    truth, inc, cand = population()
    frame = frame_for("t", inc, cand)
    partial = answers(frame, truth)
    partial[next(iter(partial))] = None
    result = bc.compare(frame, partial)
    assert not result["complete"]
    decision = bc.verdict([result], priority_tags={"t", "other"}, compared_tags={"t", "other"})
    assert not decision["admissible"]
    assert decision["unanswered_priority_tags"] == ["other", "t"]


def _incumbent_silent(candidate_noise):
    truth, _, cand = population(candidate_noise=candidate_noise)
    frame = frame_for("rare", {i: 0.0 for i in cand}, cand)
    return frame, answers(frame, truth)


def test_a_tag_the_incumbent_never_predicts_is_decided_by_the_candidate_floor():
    frame, judged = _incumbent_silent(0.0)  # a perfect candidate
    assert frame["cells"]["both_present"]["population"] == frame["cells"]["incumbent_only"]["population"] == 0
    # Without the governed floor it stays undecidable (fail closed).
    closed = bc.compare(frame, judged)
    assert closed["incumbent_silent"] and not closed["decidable"]
    good = bc.compare(frame, judged, precision_floor=0.85)
    assert good["decidable"] and not good["candidate_below_precision_floor"] and good["precision_floor"] == 0.85
    assert bc.verdict([good], priority_tags={"rare"}, compared_tags={"rare"})["admissible"]


def test_a_candidate_below_the_floor_on_such_a_tag_blocks_the_switch():
    frame, judged = _incumbent_silent(0.45)  # mostly wrong where it fires
    bad = bc.compare(frame, judged, precision_floor=0.85)
    assert bad["decidable"] and bad["candidate_below_precision_floor"]
    decision = bc.verdict([bad], priority_tags={"rare"}, compared_tags={"rare"})
    assert not decision["admissible"] and "priority_tag_below_precision_floor" in decision["reasons"]
    assert decision["below_precision_floor_priority_tags"] == ["rare"]
    with pytest.raises(ValueError, match="precision_floor_invalid"):
        bc.compare(frame, judged, precision_floor=1.5)


def test_the_floor_does_not_change_tags_both_models_predict():
    truth, inc, cand = population()
    frame = frame_for("t", inc, cand)
    plain, floored = bc.compare(frame, answers(frame, truth)), bc.compare(frame, answers(frame, truth), precision_floor=0.99)
    assert not plain["incumbent_silent"]
    assert {k: v for k, v in plain.items() if k != "precision_floor"} == \
        {k: v for k, v in floored.items() if k != "precision_floor"}


def test_too_few_candidate_images_cannot_decide_a_silent_tag():
    truth, _, cand = population(n=400)
    few = {i: (0.9 if i == next(k for k, v in truth.items() if v) else 0.0) for i in cand}  # one lucky image
    frame = frame_for("tiny", {i: 0.0 for i in cand}, few)
    assert frame["cells"]["candidate_only"]["population"] == 1
    result = bc.compare(frame, answers(frame, truth), precision_floor=0.85)
    assert result["incumbent_silent"] and not result["decidable"]
    assert not bc.verdict([result], priority_tags={"tiny"}, compared_tags={"tiny"})["admissible"]


def test_both_models_silent_stays_undecidable_even_with_a_floor():
    truth, inc, _ = population()
    frame = frame_for("none", {i: 0.0 for i in inc}, {i: 0.0 for i in inc})
    assert not bc.compare(frame, answers(frame, truth), precision_floor=0.85)["decidable"]


def test_a_non_priority_silent_tag_below_the_floor_earns_no_f1_credit():
    frame, judged = _incumbent_silent(0.45)
    bad = bc.compare(frame, judged, precision_floor=0.85)
    assert bad["difference"]["f1"] > 0  # raw: the incumbent's F1 is 0
    decision = bc.verdict([bad], priority_tags=set(), compared_tags={"rare"})
    assert decision["mean_f1_change"] == 0.0 and "no_overall_improvement" in decision["reasons"]
    assert decision["precision_floor"] == 0.85
    good_frame, good_judged = _incumbent_silent(0.0)
    other = dict(bc.compare(good_frame, good_judged, precision_floor=0.9), tag="other")
    with pytest.raises(ValueError, match="precision_floor_mixed"):
        bc.verdict([bad, other], priority_tags=set(), compared_tags={"rare", "other"})
