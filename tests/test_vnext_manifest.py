"""Split-manifest machine verification (plan tagger-vnext-deterministic-runner
Step 7/9, INV-R10 adopted finding C1 + self-checks C3).

Why this file exists: two REAL measurement bugs (grouping degeneration,
negative-sampler no-op) reached production judgment untested in the lt_round
script lineage. The manifest checks are the mechanism that catches that class
of failure, so each of the four checks is exercised here BOTH ways (sound
split passes, violating split raises), plus the three C3 self-verifications:

  C1-1  fold-group disjointness      test_check1_*
  C1-2  per-fold stratification floor test_check2_*
  C1-3  outer rows unseen by inner    test_check3_*
  C1-4  totals within declared caps   test_check4_*
  C3-a  leak fixture must FAIL        test_c3a_leak_fixture_always_fails
  C3-b  shuffled labels => chance AUC test_c3b_shuffled_labels_chance_auc
  C3-c  same seed => same hash        test_c3c_same_seed_same_manifest_hash

All pure -- no DB, no network, no torch (grouped_oof takes an injected fit_fn;
the production default fit_torch is exercised by the separate GPU smoke, not
here)."""
import math
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tagger.vnext_manifest import (  # noqa: E402
    ManifestError,
    build_manifest,
    dataset_hash,
    manifest_hash,
    verify_manifest,
)
from tagger.vnext_arms import grouped_oof, rankauc  # noqa: E402


# ---------------------------------------------------------------- fixtures

def _dataset(n_groups=8, per_group=3, pos_groups=4):
    """Grouped dataset: ids 0.., groups g0.., first pos_groups groups are
    positive. Returns (ids, labels, group_of, provenance)."""
    ids, labels, group_of, prov = [], {}, {}, {}
    nid = 0
    for g in range(n_groups):
        for _ in range(per_group):
            ids.append(nid)
            labels[nid] = 1 if g < pos_groups else 0
            group_of[nid] = g
            prov[nid] = "pool"
            nid += 1
    return ids, labels, group_of, prov


def _sound_folds(ids, group_of, k=2):
    """Groups round-robin'd into k folds -- never spans folds."""
    gs = sorted({group_of[i] for i in ids})
    slices = [set(gs[i::k]) for i in range(k)]
    return [[i for i in ids if group_of[i] in s] for s in slices]


def _manifest(**over):
    ids, labels, group_of, prov = _dataset()
    outer = _sound_folds(ids, group_of)
    inner = {fi: _sound_folds([i for i in ids if i not in set(f)], group_of)
             for fi, f in enumerate(outer)}
    kw = dict(tag="t", seed=0, labels=labels, group_of=group_of, provenance=prov,
              outer_folds=outer, inner_folds_by_outer=inner,
              sampling_caps={"neg": 100}, observed_totals={"neg": 12})
    kw.update(over)
    return build_manifest(**kw)


def _retamper(man, mutate):
    """Apply a mutation and restore hash consistency, so verification failures
    come from the CHECKS, not the tamper-detection."""
    mutate(man)
    man["manifest_hash"] = manifest_hash(man)
    return man


# ---------------------------------------------------------------- C1 checks

def test_sound_manifest_passes():
    verify_manifest(_manifest())  # no raise


def test_tampered_hash_detected():
    man = _manifest()
    man["seed"] = 999  # content changed without rehash
    with pytest.raises(ManifestError, match="manifest_hash"):
        verify_manifest(man)


def test_check1_group_spanning_outer_folds_fails():
    man = _manifest()

    def leak(m):
        # move one member of fold-0's first group into fold 1
        moved = m["fold_membership"]["outer"][0].pop(0)
        m["fold_membership"]["outer"][1].append(moved)
        m["fold_membership"]["outer"][1].sort()

    with pytest.raises(ManifestError, match="check1"):
        verify_manifest(_retamper(man, leak))


def test_check1_group_spanning_inner_folds_fails():
    man = _manifest()

    def leak(m):
        inner = m["fold_membership"]["inner"]["0"]
        moved = inner[0].pop(0)
        inner[1].append(moved)
        inner[1].sort()

    with pytest.raises(ManifestError, match="check1"):
        verify_manifest(_retamper(man, leak))


def test_check2_stratification_floor_fails():
    # every positive group in fold 0 -> fold 1 has zero positives
    ids, labels, group_of, prov = _dataset(n_groups=8, pos_groups=4)
    pos_fold = [i for i in ids if labels[i] == 1]
    neg_fold = [i for i in ids if labels[i] == 0]
    man = build_manifest(tag="t", seed=0, labels=labels, group_of=group_of,
                         provenance=prov, outer_folds=[pos_fold, neg_fold])
    with pytest.raises(ManifestError, match="check2"):
        verify_manifest(man)


def test_check2_count_summary_must_match_membership():
    man = _manifest()

    def corrupt(m):
        m["per_fold_counts"][0]["pos"] += 1

    with pytest.raises(ManifestError, match="check2"):
        verify_manifest(_retamper(man, corrupt))


def test_check3_outer_rows_in_inner_selection_fails():
    man = _manifest()

    def leak(m):
        outer0 = m["fold_membership"]["outer"][0]
        m["fold_membership"]["inner"]["0"][0].append(outer0[0])
        m["fold_membership"]["inner"]["0"][0].sort()

    with pytest.raises(ManifestError, match="check3"):
        verify_manifest(_retamper(man, leak))


def test_check3b_nested_without_inner_folds_fails():
    """primary review of 1.110.0 (LOW): check 3 passes vacuously on an empty inner
    map, so a nested run that recorded no inner membership must fail closed."""
    man = _manifest(inner_folds_by_outer={}, extra={"nested": True})
    with pytest.raises(ManifestError, match="check3b"):
        verify_manifest(man)


def test_check3b_partial_inner_coverage_fails():
    man = _manifest(extra={"nested": True})

    def drop(m):
        m["fold_membership"]["inner"].pop("1")

    with pytest.raises(ManifestError, match="check3b"):
        verify_manifest(_retamper(man, drop))


def test_check3b_ignores_non_nested_runs():
    # a permutation/flat run legitimately has no inner folds
    verify_manifest(_manifest(inner_folds_by_outer={}, extra={"nested": False}))


def test_check4_total_over_declared_cap_fails():
    man = _manifest(sampling_caps={"neg": 100}, observed_totals={"neg": 101})
    with pytest.raises(ManifestError, match="check4"):
        verify_manifest(man)


def test_check4_declared_cap_without_observation_fails():
    # a cap nobody measured against is exactly the sampler-no-op blind spot
    man = _manifest(sampling_caps={"neg": 100}, observed_totals={})
    with pytest.raises(ManifestError, match="check4"):
        verify_manifest(man)


def test_build_rejects_unlabelled_fold_ids():
    ids, labels, group_of, prov = _dataset()
    outer = _sound_folds(ids, group_of)
    outer[0].append(99999)  # id with no label/group
    with pytest.raises(ManifestError, match="lack label/group"):
        build_manifest(tag="t", seed=0, labels=labels, group_of=group_of,
                       provenance=prov, outer_folds=outer)


# ------------------------------------------------- C3 self-verifications

def _features(labels, group_of, seed=5, sep=2.0):
    """Separable 4-d features (pure python): positives shifted by `sep`,
    plus a per-GROUP offset so leakage (same group in train and test) is
    actually exploitable by the learner -- which is what makes the leak
    fixture meaningful."""
    import random
    rnd = random.Random(seed)
    goff = {g: rnd.uniform(-1, 1) for g in set(group_of.values())}
    feats = {}
    for i, lab in labels.items():
        base = sep * lab + goff[group_of[i]]
        feats[i] = [base + rnd.gauss(0, 0.5) for _ in range(4)]
    return feats


def centroid_fit(getv, pos, neg, seed=0, extra_pos=(), extra_neg=()):
    """Pure-python injectable learner (nearest-centroid logistic): a REAL
    learner (it exploits any leakage present) with no torch dependency."""
    P = list(pos) + list(extra_pos)
    N = list(neg) + list(extra_neg)
    dim = len(getv(P[0]))
    mp = [sum(getv(i)[d] for i in P) / len(P) for d in range(dim)]
    mn = [sum(getv(i)[d] for i in N) / len(N) for d in range(dim)]
    w = [a - b for a, b in zip(mp, mn)]

    def sc(ids):
        out = []
        for i in ids:
            z = sum(wd * xd for wd, xd in zip(w, getv(i)))
            out.append(1.0 / (1.0 + math.exp(-z)))
        return out
    return sc


def _big_dataset():
    return _dataset(n_groups=40, per_group=5, pos_groups=20)


def test_c3a_leak_fixture_always_fails():
    """(a) a split with one group deliberately planted across folds must FAIL
    manifest verification -- every time, whichever fold pair leaks."""
    ids, labels, group_of, prov = _big_dataset()
    outer = _sound_folds(ids, group_of, k=5)
    # plant every member of one multi-member group across two folds
    victims = [i for i in ids if group_of[i] == 0]
    for f in outer:
        for v in victims:
            if v in f:
                f.remove(v)
    outer[0].extend(victims[:2])
    outer[1].extend(victims[2:])
    for f in outer:
        f.sort()
    man = build_manifest(tag="t", seed=0, labels=labels, group_of=group_of,
                         provenance=prov, outer_folds=outer)
    with pytest.raises(ManifestError, match="check1"):
        verify_manifest(man)


def test_c3b_shuffled_labels_chance_auc():
    """(b) negative control: with labels shuffled, the manifest-verified
    grouped OOF pipeline must score in the chance band. Sanity guard: the
    SAME pipeline on true labels is far above chance (else the control is
    vacuous)."""
    ids, labels, group_of, prov = _big_dataset()
    feats = _features(labels, group_of)
    getv = feats.__getitem__
    pos = [i for i in ids if labels[i] == 1]
    neg = [i for i in ids if labels[i] == 0]

    sc, y, _, _ = grouped_oof(getv, pos, neg, group_of, tag="t", seed=3,
                              fit_fn=centroid_fit, provenance=prov)
    auc_true = rankauc([sc[i] for i in sc if y[i] == 1],
                       [sc[i] for i in sc if y[i] == 0])
    assert auc_true > 0.9, f"sanity: separable data should score high, got {auc_true}"

    aucs = []
    for s in range(3):
        ps, py, _, _ = grouped_oof(getv, pos, neg, group_of, tag="t", seed=s,
                                   shuffle=True, fit_fn=centroid_fit, provenance=prov)
        aucs.append(rankauc([ps[i] for i in ps if py[i] == 1],
                            [ps[i] for i in ps if py[i] == 0]))
    mean_auc = sum(aucs) / len(aucs)
    assert 0.35 < mean_auc < 0.65, f"shuffled-label OOF AUC not chance: {aucs}"


def test_c3c_same_seed_same_manifest_hash():
    """(c) reproducibility: same data + same seed => identical manifest hash;
    a different seed must change the fold layout (and therefore the hash)."""
    ids, labels, group_of, prov = _big_dataset()
    feats = _features(labels, group_of)
    getv = feats.__getitem__
    pos = [i for i in ids if labels[i] == 1]
    neg = [i for i in ids if labels[i] == 0]

    def run(seed):
        _, _, _, man = grouped_oof(getv, pos, neg, group_of, tag="t", seed=seed,
                                   nested=True, fit_fn=centroid_fit, provenance=prov)
        return man

    m1, m2, m3 = run(7), run(7), run(8)
    assert m1["manifest_hash"] == m2["manifest_hash"]
    assert m1["manifest_hash"] != m3["manifest_hash"]
    # dataset identity is seed-independent
    assert m1["dataset_hash"] == m3["dataset_hash"]


def test_dataset_hash_order_independent():
    ids, labels, group_of, _ = _dataset()
    rev_labels = dict(reversed(list(labels.items())))
    rev_groups = dict(reversed(list(group_of.items())))
    assert dataset_hash(labels, group_of) == dataset_hash(rev_labels, rev_groups)
