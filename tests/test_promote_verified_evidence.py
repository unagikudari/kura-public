"""promote_verified only writes a curated row when independent human evidence
backs it (kura#64). The evidence rule is a pure function; fix its boundaries here."""
from tagger.heads import promote_verified as P
from tagger.heads import verified_ledger as L


def test_present_and_human_edited_is_positive_pool_review():
    assert P.classify(present=True, human_edited_post=True, human_removed_tag=False) \
        == ("human_pool_review", "positive")


def test_absent_and_human_removed_is_negative_removed():
    assert P.classify(present=False, human_edited_post=True, human_removed_tag=True) \
        == ("human_removed", "negative")


def test_present_but_no_human_touch_is_skipped():
    # a head applied it and the tag is still there, but no human ever looked:
    # unproven, so NOT written.
    assert P.classify(present=True, human_edited_post=False, human_removed_tag=False) is None


def test_absent_without_a_human_removal_is_skipped():
    # the tag is gone, but not by a recorded human hand (could be another head):
    # not evidence of a human negative.
    assert P.classify(present=False, human_edited_post=True, human_removed_tag=False) is None


def test_every_decision_produces_a_curated_row():
    # whatever classify returns, the row it implies must pass is_curated — the
    # writer cannot emit something the loader would refuse.
    for present, edited, removed in [(True, True, False), (False, True, True)]:
        source, label = P.classify(present=present, human_edited_post=edited,
                                    human_removed_tag=removed)
        row = L.make_row(concept="phimosis", post_id=1, label=label, source=source,
                         actor="owner", at="2026-09-05T00:00:00", pool=9)
        assert L.is_curated(row) is True
