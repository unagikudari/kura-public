"""make_group_folds() stratification fix (2026-08-09, broker task 334582d2,
trainer-cc-primary, peeing r13 live measurement).

Bug: the original round-robin (`gs[i::k]` after a seeded shuffle) assigns
GROUPS to folds with no regard for class balance. When negatives cluster into
few groups (verify-pool-sourced, unlike organic corpus-wide singletons), the
odds that ALL k=5 outer folds land at least one negative-bearing group is a
coupon-collector surjection probability -- 3.8% at G=5 negative-bearing
groups. vnext_manifest.py's check2 (stratification floor) then fails on seed
luck, not genuine GT insufficiency, and growing GT can never fix that
deterministically.

Fix: an optional y= (id -> 0/1 label) switches to a StratifiedGroupKFold-style
greedy assignment (same algorithm as train_base_finetune.py's group_folds):
largest groups first, each to whichever fold is currently thinnest across
both classes. Guarantees every fold gets >=1 of each class whenever that's
even possible (each class has >=k label-bearing groups) -- deterministically,
not by luck.

All pure -- no DB, no network, no torch.
"""
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tagger.vnext_arms import make_group_folds  # noqa: E402


def _grouped_dataset(group_sizes: dict[int, int], pos_groups: set[int]):
    """group_sizes: {group_id: member_count}. Members of a group NOT in
    pos_groups are all-negative; members of a group IN pos_groups are
    all-positive (mirrors the real shape: a leakage group is single-source,
    so its members skew heavily toward one class in practice, and here are
    made uniformly one class for a clean adversarial construction)."""
    ids, group_of, y = [], {}, {}
    nid = 0
    for g, n in group_sizes.items():
        for _ in range(n):
            group_of[nid] = g
            y[nid] = 1 if g in pos_groups else 0
            ids.append(nid)
            nid += 1
    return ids, group_of, y


class TestMakeGroupFoldsBackwardCompat:
    def test_y_none_matches_the_original_round_robin_behaviour(self):
        ids, group_of, _y = _grouped_dataset({0: 2, 1: 2, 2: 2, 3: 2, 4: 2}, pos_groups={0, 1})
        got = make_group_folds(ids, group_of, seed=0, k=5)

        gs = sorted({group_of[i] for i in ids})
        rnd = random.Random(0)
        rnd.shuffle(gs)
        slices = [set(gs[i::5]) for i in range(5)]
        want = [[i for i in ids if group_of[i] in s] for s in slices]
        assert got == want


class TestMakeGroupFoldsGroupIntegrity:
    def test_a_group_never_spans_folds_stratified(self):
        ids, group_of, y = _grouped_dataset(
            {0: 3, 1: 5, 2: 2, 3: 7, 4: 4, 5: 1, 6: 6}, pos_groups={0, 2, 4, 6})
        folds = make_group_folds(ids, group_of, seed=3, k=5, y=y)
        seen: dict[int, int] = {}
        for fi, fold in enumerate(folds):
            for i in fold:
                g = group_of[i]
                assert seen.setdefault(g, fi) == fi, f"group {g} spans folds"

    def test_every_id_appears_exactly_once(self):
        ids, group_of, y = _grouped_dataset({0: 3, 1: 5, 2: 2, 3: 7}, pos_groups={1, 3})
        folds = make_group_folds(ids, group_of, seed=1, k=4, y=y)
        flat = sorted(i for f in folds for i in f)
        assert flat == sorted(ids)

    def test_deterministic_for_same_seed(self):
        ids, group_of, y = _grouped_dataset({0: 3, 1: 5, 2: 2, 3: 7, 4: 4}, pos_groups={0, 3})
        a = make_group_folds(ids, group_of, seed=7, k=5, y=y)
        b = make_group_folds(ids, group_of, seed=7, k=5, y=y)
        assert a == b


class TestMakeGroupFoldsStratification:
    def test_reproduces_the_peeing_shaped_failure_without_y(self):
        """peeing r13 live shape: 3 negative-bearing groups, outer_k=5 -- a
        fold with zero negatives is POSSIBLE (not guaranteed) under plain
        round-robin. Find a seed where it actually happens, to prove the
        failure mode is real before proving the fix removes it."""
        group_sizes = {0: 17, 1: 30, 2: 1,               # 3 negative-bearing groups (peeing shape)
                       10: 6, 11: 6, 12: 6, 13: 6, 14: 6, 15: 6, 16: 6, 17: 6}  # 8 positive-bearing groups
        pos_groups = {10, 11, 12, 13, 14, 15, 16, 17}
        ids, group_of, y = _grouped_dataset(group_sizes, pos_groups)

        found_a_zero_neg_fold = False
        for seed in range(50):
            folds = make_group_folds(ids, group_of, seed=seed, k=5)
            neg_counts = [sum(1 for i in f if y[i] == 0) for f in folds]
            if min(neg_counts) == 0:
                found_a_zero_neg_fold = True
                break
        assert found_a_zero_neg_fold, (
            "expected at least one seed in range(50) to produce a fold with "
            "zero negatives under unstratified round-robin -- if this now "
            "fails, the adversarial fixture stopped being adversarial"
        )

    def test_the_same_shape_never_zero_folds_a_class_when_stratified(self):
        """Same adversarial shape as above, but with y= passed -- every fold
        must get >=1 negative and >=1 positive, for EVERY seed, because 3
        negative-bearing groups < 5... wait: 3 < 5 means it is NOT always
        possible to give every fold a negative. Use a shape where it IS
        possible (negative-bearing groups >= k) to test the guarantee, and
        a separate test below for the honestly-impossible case."""
        group_sizes = {0: 10, 1: 10, 2: 10, 3: 10, 4: 10,   # 5 negative-bearing groups == k
                       10: 6, 11: 6, 12: 6, 13: 6, 14: 6, 15: 6}  # 6 positive-bearing groups
        pos_groups = {10, 11, 12, 13, 14, 15}
        ids, group_of, y = _grouped_dataset(group_sizes, pos_groups)

        for seed in range(30):
            folds = make_group_folds(ids, group_of, seed=seed, k=5, y=y)
            for fi, f in enumerate(folds):
                pos = sum(1 for i in f if y[i] == 1)
                neg = sum(1 for i in f if y[i] == 0)
                assert pos >= 1, f"seed={seed} fold={fi} has zero positives"
                assert neg >= 1, f"seed={seed} fold={fi} has zero negatives"

    def test_honestly_impossible_case_is_not_silently_hidden(self):
        """3 negative-bearing groups < outer_k=5 (the ACTUAL peeing r13 shape)
        -- no assignment, stratified or not, can give all 5 folds a negative.
        The fix must not pretend otherwise: it should still produce its best
        (most-balanced) effort, not crash, and the shortfall is what
        vnext_manifest.py's check2 is SUPPOSED to catch (genuine GT
        insufficiency) rather than pretend to solve algorithmically."""
        group_sizes = {0: 17, 1: 30, 2: 1,
                       10: 6, 11: 6, 12: 6, 13: 6, 14: 6, 15: 6, 16: 6, 17: 6}
        pos_groups = {10, 11, 12, 13, 14, 15, 16, 17}
        ids, group_of, y = _grouped_dataset(group_sizes, pos_groups)

        folds = make_group_folds(ids, group_of, seed=0, k=5, y=y)
        neg_counts = [sum(1 for i in f if y[i] == 0) for f in folds]
        # at most 3 folds can hold a negative-bearing group; at least 2 folds
        # are structurally starved of negatives no matter the algorithm.
        assert sum(1 for c in neg_counts if c > 0) <= 3
        assert sum(1 for c in neg_counts if c == 0) >= 2
        # but it must still be a complete, valid partition -- no crash, no
        # dropped ids, groups still intact (covered by the integrity tests
        # above; re-asserted here as the end-to-end shape of this scenario).
        assert sorted(i for f in folds for i in f) == sorted(ids)
