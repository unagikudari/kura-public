"""tagger/regression_check.py -- pure-function tests for the two bugs found
2026-08-09 in the ad-hoc script it replaces (see the module docstring):
in-sample eval and thin-N noise both being able to block a real improvement.

No DB/torch needed: wilson_lower_bound / two_proportion_z / compare_tag take
plain tp/fp/fn/tn dicts, same shape eval_gt() returns.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tagger"))

from regression_check import (  # noqa: E402
    compare_tag,
    pick_teacher_release_id,
    two_proportion_z,
    wilson_lower_bound,
)


def _c(tp, fp, fn, tn):
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn}


class TestTwoProportionZ:
    def test_identical_proportions_is_zero(self):
        assert two_proportion_z(10, 100, 10, 100) == 0.0

    def test_empty_sample_is_none(self):
        assert two_proportion_z(0, 0, 5, 10) is None
        assert two_proportion_z(5, 10, 0, 0) is None

    def test_large_clear_drop_is_significant(self):
        # 90% -> 50% on n=100 each -- an unmistakable real drop.
        z = two_proportion_z(90, 100, 50, 100)
        assert z is not None and z < -1.96

    def test_tiny_sample_swing_is_not_significant(self):
        # flaccid-shaped: n_pos=15 total, tp 2->2 fp 1->2 (the actual Phase 0 numbers).
        # Precision 0.6667 -> 0.5 "looks like" a big relative drop but n is tiny.
        z = two_proportion_z(2, 3, 2, 4)
        assert z is not None and z > -1.96, "a 2/3 vs 2/4 swing must not read as significant"


class TestWilsonLowerBound:
    def test_zero_n_is_zero(self):
        assert wilson_lower_bound(0, 0, 1.96) == 0.0

    def test_more_data_narrows_the_bound_upward(self):
        # Same point estimate (0.5), more data -> lower bound should rise (less pessimistic).
        small = wilson_lower_bound(5, 10, 1.96)
        large = wilson_lower_bound(500, 1000, 1.96)
        assert large > small


class TestPickTeacherReleaseId:
    """2026-08-09, found live on trainer: the first version of this script always
    loaded the CURRENT active release as teacher. A checkpoint built on release 6
    (n_out=10863) hard-failed against active release 14 (n_out=10862 -- one column
    narrower after licking_testicle's own row was removed post-round7). Comparing
    against a LATER release isn't just misaligned, it's impossible once a trained
    tag's own column no longer exists in that vocab -- the archived release the
    checkpoint actually trained from is the only honest teacher."""

    def test_defaults_to_the_checkpoints_own_base_release(self):
        assert pick_teacher_release_id(None, 6) == 6

    def test_explicit_override_wins_even_with_a_recorded_base_release(self):
        assert pick_teacher_release_id(14, 6) == 14

    def test_no_override_no_recorded_base_falls_back_to_none(self):
        # None means "caller uses the active release" -- the only option for
        # checkpoints old enough to predate the base_release_id field.
        assert pick_teacher_release_id(None, None) is None


class TestCompareTag:
    def test_in_sample_style_precision_drop_on_target_tag_is_reported_but_needs_significance(self):
        """This is the anilingus shape from phase0_regression_ft-l3-full.json:
        before P=0.5312 R=0.977 (tp=85,fp=75,fn=2,tn=64) -> after P=0.7339 R=0.9195
        (tp=80,fp=29,fn=7,tn=110), n=226 -- plenty of N, real signal, should NOT be
        blocked as unreliable, and the precision move is a big IMPROVEMENT (not a
        regression) despite the recall dip being what the old ad-hoc script flagged."""
        teacher = _c(85, 75, 2, 64)
        cand = _c(80, 29, 7, 110)
        r = compare_tag("anilingus", teacher, cand, min_labels=10, z_crit=1.96)
        assert r["reliable"] is True
        assert r["dP_pt"] > 0  # precision genuinely improved
        # recall dipped a little (2 more false negatives on 87 positives) -- assert
        # the mechanism CAN flag it if significant, without asserting which way it lands
        assert r["verdict"] in ("PASS", "FAIL")

    def test_thin_n_precision_drop_is_reported_insufficient_not_failed(self):
        """flaccid-shaped: n_pos=15, n_val total well under the min_labels=10 floor
        for the NEGATIVE side too once combined with tn -- use the actual tiny counts
        (tp=2,fp=1,fn=13,tn=55 before / tp=2,fp=2,fn=13,tn=54 after, n=71) and confirm
        the floor check operates on n_val (pos+neg), not silently pass a small-N drop."""
        teacher = _c(2, 1, 13, 55)
        cand = _c(2, 2, 13, 54)
        r = compare_tag("flaccid", teacher, cand, min_labels=10, z_crit=1.96)
        # n_val = 71 clears a floor of 10 -- reliable, but the actual counts (2 fp ->
        # still just 1 more fp) must not trip "significant" on a two-proportion test.
        assert r["reliable"] is True
        assert r["significant_precision_drop"] is False, (
            "a 1-example fp swing on n=71 must not register as a statistically "
            "significant regression"
        )
        assert r["verdict"] == "PASS"

    def test_below_floor_tag_is_insufficient_n_never_fails(self):
        """A tag with almost no val-fold labels (n_val < regression_min_gt_labels)
        must be reported for visibility but can NEVER produce a FAIL verdict on its
        own, matching tagger/health.py's existing regression_min_gt_labels floor."""
        teacher = _c(0, 0, 3, 2)   # n_val = 5
        cand = _c(0, 3, 0, 2)      # precision collapsed to 0 -- looks catastrophic
        r = compare_tag("newhalf", teacher, cand, min_labels=10, z_crit=1.96)
        assert r["reliable"] is False
        assert r["verdict"] == "insufficient_n"

    def test_zero_positive_tag_recall_is_null_not_zero(self):
        """n_pos=0 (no ground-truth positives in val) must report R=None, not a
        misleading R=0.0 that looks like a measured, failing recall."""
        teacher = _c(0, 4, 0, 8)
        cand = _c(0, 7, 0, 5)
        r = compare_tag("pussy", teacher, cand, min_labels=10, z_crit=1.96)
        assert r["before"]["R"] is None
        assert r["after"]["R"] is None

    def test_a_real_significant_drop_on_ample_data_still_fails(self):
        """The mechanism must still catch a GENUINE regression -- noise-safety must
        not become a rubber stamp. Large n, clear drop, both sides of P and R."""
        teacher = _c(180, 20, 20, 780)   # P=0.9, R=0.9
        cand = _c(90, 20, 110, 780)      # R collapses to 0.45, same n
        r = compare_tag("some_tag", teacher, cand, min_labels=10, z_crit=1.96)
        assert r["reliable"] is True
        assert r["significant_recall_drop"] is True
        assert r["verdict"] == "FAIL"
