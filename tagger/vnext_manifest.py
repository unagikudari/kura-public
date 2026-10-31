#!/usr/bin/env python3
# Description: split-manifest generation + machine verification for v-next tagger measurements
# Kind: library
# Effects: pure (no DB, no network, no filesystem)
# Runs-on: trainer (imported by tagger/vnext_arms.py; testable anywhere)
"""tagger/vnext_manifest.py — split manifest for every v-next measurement
(plan tagger-vnext-deterministic-runner Step 7 / INV-R10, adopted finding C1).

Why this exists: two REAL measurement bugs went into production judgment
untested (round 6/7) —

  (1) grouping degeneration: pool-free GT collapsed into one group `-1`,
      so grouped CV silently stopped grouping;
  (2) negative-sampler no-op: a per-group quota `max(1, round(n*quota))`
      kept every member of every singleton group, so the declared 1,500
      cap sampled 8,527 -> 8,527.

Both are exactly the class of failure a machine-checked manifest catches:
the manifest declares what the split SHOULD look like (groups don't span
folds, per-fold stratification floors, outer rows never seen by inner
threshold selection, totals within declared caps) and verify_manifest()
re-derives each property from the recorded membership. Any violation is a
raised ManifestError — measurement must not proceed (fail-closed, E0).

The manifest is part of the measurement artifact and is included in the
content hash (`artifact_sha256` は manifest を含む, plan Step 18), so a
same-seed re-run must reproduce the identical manifest_hash (self-check
C3(c), tests/test_vnext_manifest.py).

Everything here is pure: builders take plain dicts/lists, verification
reads only the manifest itself. DB access, torch, sampling etc. live in
tagger/vnext_arms.py (INV-R9: the ONE measurement implementation).
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterable, Mapping, Sequence

SCHEMA = "vnext-split-manifest/1"


class ManifestError(Exception):
    """A split manifest failed generation or machine verification.

    Raising (not returning False) is deliberate: INV-R10 says a failed check
    aborts the measurement, and an exception cannot be accidentally ignored
    the way a boolean can.
    """


def _canonical_json(obj) -> str:
    """Deterministic serialization — dict order and unicode must not leak
    into the hash (same-seed reproducibility, self-check C3(c))."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def dataset_hash(labels: Mapping[int, int], group_of: Mapping[int, int]) -> str:
    """Content hash of the *dataset* (ids + labels + group assignment),
    independent of seed/fold layout: two runs on the same data share this
    even when their fold shuffles differ. Sorted triples so insertion order
    cannot leak in."""
    triples = sorted((int(i), int(labels[i]), int(group_of[i])) for i in labels)
    return hashlib.sha256(_canonical_json(triples).encode("utf-8")).hexdigest()


def manifest_hash(manifest: Mapping) -> str:
    """Hash of the FULL manifest (dataset + seed + fold membership + caps).
    The reproducibility check compares this: same seed + same data => same
    folds => same hash."""
    doc = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    return hashlib.sha256(_canonical_json(doc).encode("utf-8")).hexdigest()


def build_manifest(
    *,
    tag: str,
    seed: int,
    labels: Mapping[int, int],
    group_of: Mapping[int, int],
    provenance: Mapping[int, str],
    outer_folds: Sequence[Sequence[int]],
    inner_folds_by_outer: Mapping[int, Sequence[Sequence[int]]] | None = None,
    sampling_caps: Mapping[str, int] | None = None,
    observed_totals: Mapping[str, int] | None = None,
    stratification_floors: Mapping[str, int] | None = None,
    extra: Mapping | None = None,
) -> dict:
    """Assemble the split manifest for one measurement.

    labels:     id -> 0/1 (the labels the CV actually uses — for a
                permutation run these are the SHUFFLED labels).
    group_of:   id -> leakage group (pool id, or -post_id singleton).
    provenance: id -> where the group came from ('pool', 'pool_free',
                'recovered_human_add', ...). Recorded so a grouping
                degeneration (bug 1) is visible in the artifact itself.
    outer_folds: list of id-lists (test membership of each outer fold).
    inner_folds_by_outer: outer fold index -> list of id-lists used for the
                inner (threshold-selection) folds of that outer training set.
    sampling_caps / observed_totals: declared ceilings vs what was actually
                used (bug 2 is caught by check 4 comparing these).
    stratification_floors: {'min_pos_per_fold': n, 'min_neg_per_fold': n}.

    Every id in a fold must be labelled and grouped — an unknown id is a
    generation error (raised here, not deferred to verification).
    """
    all_fold_ids = [i for f in outer_folds for i in f]
    unknown = [i for i in all_fold_ids if i not in labels or i not in group_of]
    if unknown:
        raise ManifestError(f"build_manifest: {len(unknown)} fold ids lack label/group "
                            f"(first: {unknown[:5]})")

    per_fold = []
    for f in outer_folds:
        per_fold.append({
            "n": len(f),
            "pos": sum(1 for i in f if labels[i] == 1),
            "neg": sum(1 for i in f if labels[i] == 0),
            "groups": len({group_of[i] for i in f}),
        })

    n_pos = sum(1 for i in labels if labels[i] == 1)
    n_neg = sum(1 for i in labels if labels[i] == 0)
    manifest = {
        "schema": SCHEMA,
        "tag": tag,
        "seed": int(seed),
        "dataset_hash": dataset_hash(labels, group_of),
        "n_total": len(labels),
        "n_pos": n_pos,
        "n_neg": n_neg,
        # str keys: JSON round-trip must not change the manifest hash.
        "labels": {str(i): int(labels[i]) for i in sorted(labels)},
        "group_provenance": {str(i): {"group": int(group_of[i]),
                                      "provenance": provenance.get(i, "unknown")}
                             for i in sorted(group_of)},
        "fold_membership": {
            "outer": [sorted(int(i) for i in f) for f in outer_folds],
            "inner": {str(k): [sorted(int(i) for i in f) for f in folds]
                      for k, folds in (inner_folds_by_outer or {}).items()},
        },
        "per_fold_counts": per_fold,
        "sampling_caps": dict(sampling_caps or {}),
        "observed_totals": dict(observed_totals or {}),
        "stratification_floors": dict(stratification_floors
                                      or {"min_pos_per_fold": 1, "min_neg_per_fold": 1}),
        "extra": dict(extra or {}),
    }
    manifest["manifest_hash"] = manifest_hash(manifest)
    return manifest


# ---------------------------------------------------------------- verification

def _check_groups_disjoint(manifest: Mapping) -> None:
    """Check 1: no leakage group appears in more than one outer fold, and,
    within each outer split, in more than one inner fold. A shared group is
    the definition of train/test leakage under grouped CV."""
    gp = manifest["group_provenance"]

    def group(i: int) -> int:
        return gp[str(i)]["group"]

    seen: dict[int, int] = {}
    for fi, fold in enumerate(manifest["fold_membership"]["outer"]):
        for i in fold:
            g = group(i)
            if g in seen and seen[g] != fi:
                raise ManifestError(
                    f"check1 fold-group leak: group {g} spans outer folds {seen[g]} and {fi}")
            seen[g] = fi
    for key, inner in manifest["fold_membership"]["inner"].items():
        iseen: dict[int, int] = {}
        for fi, fold in enumerate(inner):
            for i in fold:
                g = group(i)
                if g in iseen and iseen[g] != fi:
                    raise ManifestError(
                        f"check1 fold-group leak: group {g} spans inner folds {iseen[g]} "
                        f"and {fi} of outer {key}")
                iseen[g] = fi


def _check_stratification(manifest: Mapping) -> None:
    """Check 2: every outer fold holds at least the declared minimum of each
    class. A fold below floor makes its fold-level estimate meaningless (and
    is how a degenerate grouping first surfaced as auc=NaN)."""
    floors = manifest["stratification_floors"]
    min_pos = int(floors.get("min_pos_per_fold", 1))
    min_neg = int(floors.get("min_neg_per_fold", 1))
    for fi, c in enumerate(manifest["per_fold_counts"]):
        if c["pos"] < min_pos or c["neg"] < min_neg:
            raise ManifestError(
                f"check2 stratification floor: outer fold {fi} has pos={c['pos']}/"
                f"neg={c['neg']} < floor {min_pos}/{min_neg}")
    # per_fold_counts must agree with the recorded membership -- a manifest
    # whose summary contradicts its own membership is corrupt.
    labels = manifest["labels"]
    for fi, fold in enumerate(manifest["fold_membership"]["outer"]):
        pos = sum(1 for i in fold if labels[str(i)] == 1)
        neg = sum(1 for i in fold if labels[str(i)] == 0)
        c = manifest["per_fold_counts"][fi]
        if pos != c["pos"] or neg != c["neg"]:
            raise ManifestError(
                f"check2 count mismatch: outer fold {fi} membership says {pos}/{neg}, "
                f"per_fold_counts says {c['pos']}/{c['neg']}")


def _check_outer_unseen_by_inner(manifest: Mapping) -> None:
    """Check 3: rows of an outer test fold must not appear in the inner folds
    that select thresholds for that outer fold. Inner selection touching outer
    rows makes the nested estimate optimistic — the exact optimism nested CV
    exists to remove."""
    outer = manifest["fold_membership"]["outer"]
    for key, inner in manifest["fold_membership"]["inner"].items():
        oi = int(key)
        if oi < 0 or oi >= len(outer):
            raise ManifestError(f"check3 inner refers to nonexistent outer fold {key}")
        outer_ids = set(outer[oi])
        inner_ids = {i for fold in inner for i in fold}
        hit = outer_ids & inner_ids
        if hit:
            raise ManifestError(
                f"check3 outer rows in inner selection: outer fold {oi} shares "
                f"{len(hit)} ids with its inner folds (first: {sorted(hit)[:5]})")


def _check_totals_within_caps(manifest: Mapping) -> None:
    """Check 4: observed totals respect every declared sampling cap. This is
    the check that catches the sampler no-op (declared cap 1500, observed
    8527) mechanically instead of by eyeball."""
    caps = manifest["sampling_caps"]
    obs = manifest["observed_totals"]
    for k, cap in caps.items():
        if k not in obs:
            raise ManifestError(f"check4 declared cap {k!r} has no observed total")
        if int(obs[k]) > int(cap):
            raise ManifestError(
                f"check4 cap exceeded: {k} observed {obs[k]} > declared cap {cap}")


def _check_nested_records_inner(manifest: Mapping) -> None:
    """Check 3b (primary review of 1.110.0, LOW): a manifest that DECLARES itself
    nested must actually record inner folds for every outer fold.

    Check 3 only inspects the inner folds that exist — an empty `inner` map
    passes it vacuously. That is the same shape of hole as the sampler no-op:
    the guard is present, the property is unverified. A nested run whose inner
    membership went unrecorded cannot be attested at all, so it fails closed
    rather than reporting a check it never performed."""
    if not manifest.get("extra", {}).get("nested"):
        return
    inner = manifest["fold_membership"]["inner"]
    for fi in range(len(manifest["fold_membership"]["outer"])):
        folds = inner.get(str(fi))
        if not folds or not any(folds):
            raise ManifestError(
                f"check3b nested run recorded no inner folds for outer fold {fi} "
                f"-- nested threshold selection cannot be attested")


def verify_manifest(manifest: Mapping) -> None:
    """Run all four INV-R10 machine checks + hash integrity. Raises
    ManifestError on the first violation; returns None when the split is
    sound. Callers MUST call this before fitting anything (vnext_arms does)."""
    if manifest.get("schema") != SCHEMA:
        raise ManifestError(f"unknown manifest schema {manifest.get('schema')!r}")
    if manifest.get("manifest_hash") != manifest_hash(manifest):
        raise ManifestError("manifest_hash does not match content (tampered or truncated)")
    _check_groups_disjoint(manifest)
    _check_stratification(manifest)
    _check_outer_unseen_by_inner(manifest)
    _check_nested_records_inner(manifest)
    _check_totals_within_caps(manifest)
