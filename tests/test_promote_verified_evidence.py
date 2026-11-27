"""Regression coverage for heads training evidence and exclusions."""
from datetime import datetime, timedelta, timezone

import pytest

from tagger.heads import promote_verified as promote
from tagger.heads.verified_ledger import is_proven

MACHINE = datetime(2026, 1, 1, tzinfo=timezone.utc)
HUMAN = MACHINE + timedelta(minutes=1)


def record(**changes):
    columns = dict(eligible=True, tags=[], status="ok", gt=None, aid=9,
                   kind="human", actor="reviewer", at=HUMAN, added=True,
                   removed=False, rid=None, uid=None, name=None, rt=None,
                   decision=None, media_ok=False)
    columns.update(changes)
    return tuple(columns.values())


@pytest.mark.parametrize("added,removed,label", [(True, False, "positive"), (False, True, "negative")])
def test_requires_tag_specific_audit(added, removed, label):
    evidence, reasons = promote.decide_evidence(record(added=added, removed=removed), MACHINE)
    assert not reasons
    assert evidence["label"] == label
    assert evidence["actor"] == "reviewer"
    assert is_proven(dict(schema=3, concept="fixture", post_id=4, **evidence))
    assert not is_proven(dict(schema=2, concept="fixture", post_id=4, **evidence))


@pytest.mark.parametrize("changes,reason", [
    (dict(eligible=False, tags=["loli"]), "youth_tag"),
    (dict(eligible=False, tags=["tagger-training-exclude"]), "training_opt_out"),
    (dict(gt="negative"), "canonical_gt_conflict"),
    (dict(at=MACHINE), "tag_specific_evidence_missing"),
    (dict(added=False), "tag_specific_evidence_missing"),
    (dict(kind="script"), "tag_specific_evidence_missing"),
    (dict(rid=8, rt=HUMAN, decision="uncertain"), "review_evidence_unverified"),
])
def test_refuses_unproven_or_ineligible_input(changes, reason):
    assert promote.decide_evidence(record(**changes), MACHINE) == (None, (reason,))


def test_explicit_review_records_actual_actor_and_event():
    evidence, reasons = promote.decide_evidence(
        record(rid=8, uid=17, name="other reviewer", rt=HUMAN, decision="present", media_ok=True), MACHINE)
    assert not reasons
    assert evidence["source"] == "human_review_event"
    assert evidence["actor_id"] == 17
    assert evidence["evidence_id"] == 8
    assert "'training_support','historical_label_recheck'" in promote.EVIDENCE
