import pytest

from tagger import base_comparison as bc
from tagger import base_verdict as bv
from tagger.dedicated_protocol import digest

from tests.test_base_comparison import answers as truth_answers, frame_for, population


def _registry(frame, truth, *, uncertain=()):
    rows = [{"post_id": i, "display_sha256": "x", "event_id": 1,
             "decision": ("uncertain" if i in uncertain else ("present" if truth[i] else "absent"))}
            for cell in frame["cells"].values() for i in cell["sample"]]
    return {"frame_sha256": digest(frame), "answers": rows, "sha256": "a" * 64}


def test_full_answers_give_the_ruling_3_verdict():
    truth, inc, cand = population()
    frame = frame_for("t", inc, cand)
    frames = {"frames": {"t": frame}}
    document = bv.decide(frames, {"t": _registry(frame, truth)}, precision_floor=0.85)
    expected = bc.compare(frame, truth_answers(frame, truth), precision_floor=0.85)
    assert document["results"] == [expected]
    assert document["verdict"]["admissible"] == bc.verdict([expected], priority_tags={"t"}, compared_tags={"t"})["admissible"]
    assert document["schema"] == bv.SCHEMA


def test_uncertain_answers_leave_the_tag_unanswered():
    truth, inc, cand = population()
    frame = frame_for("t", inc, cand)
    one = frame["cells"]["both_present"]["sample"][0]
    document = bv.decide({"frames": {"t": frame}}, {"t": _registry(frame, truth, uncertain={one})},
                         precision_floor=0.85)
    assert not document["results"][0]["complete"]
    assert not document["verdict"]["admissible"]
    assert "priority_tags_unanswered" in document["verdict"]["reasons"]


def test_answers_for_another_frame_are_refused():
    truth, inc, cand = population()
    frame = frame_for("t", inc, cand)
    wrong = dict(_registry(frame, truth), frame_sha256="f" * 64)
    with pytest.raises(ValueError, match="answers_frame_mismatch"):
        bv.decide({"frames": {"t": frame}}, {"t": wrong}, precision_floor=0.85)
    with pytest.raises(ValueError, match="answers_missing"):
        bv.decide({"frames": {"t": frame}}, {}, precision_floor=0.85)
    stray = _registry(frame, truth)
    stray["answers"].append({"post_id": 10**9, "decision": "present"})
    with pytest.raises(ValueError, match="answer_outside_frame"):
        bv.decide({"frames": {"t": frame}}, {"t": stray}, precision_floor=0.85)
