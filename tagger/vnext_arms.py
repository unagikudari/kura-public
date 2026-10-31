#!/usr/bin/env python3
# Description: THE v-next tagger measurement (arms A/B/C/E, grouped nested CV, permutation null) -- tag-parameterized
# Kind: library + tool
# Effects: reads kura DB (primary canonical for floor/GT, replica for images); writes only local caches/result JSON. NEVER writes the DB (INV-R1).
# Runs-on: trainer (RTX 5070 Ti; CPU fallback supported)
"""tagger/vnext_arms.py — the ONE measurement implementation (INV-R9),
plan tagger-vnext-deterministic-runner Step 8.

Lineage: ~/tagger-heads/lt_round{6,7,8}.py measured licking_testicle
as one-off scripts, copied per round. This module replaces that lineage:
tag-parameterized, importable, and REVIEWED — a new round is a new invocation,
never a new copy.

Two measurement bugs from the script lineage are fixed here, and the split
manifest (tagger/vnext_manifest.py, INV-R10) verifies both mechanically before
anything is fitted:

  bug 1 (grouping degeneration): pool-free GT collapsed to a single group -1,
      degenerating grouped CV. Correct grouping (round8 flavour, kept):
      pool-derived rows group by source_pool_id; pool-free rows and recovered
      human-adds are per-post singletons (-post_id) — independent human
      judgments carry no pool leakage.
  bug 2 (negative-sampler no-op): the per-group quota
      `_k = max(1, round(len(_ids) * _quota))` keeps >= 1 of EVERY group, so
      under singleton-group domination the declared cap did nothing (measured:
      "in-gate neg sampled: 8527 -> 8527"). Replaced by sample_capped():
      seeded group-round-robin that ALWAYS honours the total cap while
      preserving group diversity.

Measurement floor comes from the LIVE DB (tagger_config on primary — the
canonical writer); it is deliberately not baked into this file (plan Step 16),
and a missing floor aborts (fail-closed) instead of falling back to code
constants.

Caches (= per-item checkpoints, plan Step 6): kura-space embeddings and WD14
logits are keyed by post id and TAG-INDEPENDENT (shared across all tags);
PixAI scores are per-tag files (the score extracted is the tag's own column).

Heavy deps (torch/timm/psycopg2/onnxruntime/numpy) import lazily inside the
functions that need them, so the pure logic (grouping, sampler, folds,
thresholds, manifest integration) is testable in any venv.
"""
from __future__ import annotations

import io
import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tagger.vnext_manifest import ManifestError, build_manifest, verify_manifest
from tagger import display_bytes

DEFAULT_SEED = 802081  # round6-8 lineage seed; a run may override
NEG_CAP_DEFAULT = 1500       # in-gate negative cap for CV (embed+PixAI cost)
OOG_NEG_CAP_DEFAULT = 1000   # out-of-gate (implicit_review) negative cap
RAND_SAMPLE_DEFAULT = 600    # random-archive sample for fire-rate readouts
CORPUS_LIMIT_DEFAULT = 2500  # corpus-positive arm-C augmentation cap
N_PERM_DEFAULT = 12          # permutation-null repeats (Step 15 may raise)
INPUT_SIZE = 448

CACHE_DIR = Path(os.environ.get("VNEXT_CACHE_DIR",
                                str(Path.home() / "tagger-heads" / "vnext_cache")))
EMB_CACHE = CACHE_DIR / "emb_kuraspace.pt"    # post-keyed, tag-independent
LOGIT_CACHE = CACHE_DIR / "wd14_logits.pt"    # post-keyed, tag-independent
PATCHES_DIR = Path(__file__).resolve().parent / "patches"
MANIFEST_PATH = PATCHES_DIR / "MANIFEST.json"
PIXAI_DIR = os.path.expanduser(
    "~/.cache/huggingface/hub/models--deepghs--pixai-tagger-v0.9-onnx/snapshots")

FLOOR_KEYS = ("min_precision", "min_recall", "min_gt_labels", "min_gt_positive",
              "min_gt_negative", "min_val_pos_support", "min_pred_pos_support",
              "min_held_out_samples", "min_p_deploy", "min_p_deploy_lift",
              # **規模の floor** (0133)。ここに載せないと config に行があっても
              # load_floor が捨てるので、classify まで届かず規模経路が働かない。
              # 「config に入れた」と「実際に読まれる」は別 — 2026-08-05 に
              # まさにこれを踏んだ (unit test は通るのに本番では効かない)。
              "min_net_fp_removed",
              "eval_split_mode")

THRS = [x / 100 for x in range(5, 100, 5)] + [0.99]


def _save_shared_pixai(path: Path, shared: dict) -> None:
    """post キー PixAI ベクトルキャッシュの atomic 保存。

    tmp + os.replace にするのは、将来ノードを跨いで並列に埋めるときに
    「片方の書き込みが丸ごと消える / 壊れたファイルが残る」を避けるため
    (2026-08-02 に replica 直書きで学んだ形の別版)。
    """
    try:
        import os as _os
        import torch as _t
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".pt.tmp")
        _t.save(shared, tmp)
        _os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        print(f"  pixai shared-cache save failed ({e})", flush=True)


def pixai_cache_path(tag: str) -> Path:
    return CACHE_DIR / f"pixai_scores_{tag}.json"


# ---------------------------------------------------------------- DB plumbing

def connect_primary():
    """Canonical (writer) node — floor/GT reads only. INV-R1: this module
    NEVER writes; both connections are used for SELECTs exclusively."""
    from tagger.kura_db import connect_canonical   # KURA_CANONICAL_DSN, default primary
    conn = connect_canonical()
    conn.autocommit = True
    return conn


def connect_local():
    """Read-only replica (127.0.0.1) — bulk image bytes stay off the WAN."""
    from tagger.kura_db import connect_read        # KURA_READ_DSN, default the local replica
    conn = connect_read()
    conn.autocommit = True
    return conn


def q(conn, sql: str, args=None) -> list[tuple]:
    c = conn.cursor()
    c.execute(sql, args)
    rows = c.fetchall()
    c.close()
    return rows


def load_floor(query: Callable[[str], list[tuple]] | None = None) -> dict[str, float]:
    """Measurement floor from the LIVE tagger_config (primary canonical).

    Fail-closed on an unreachable/empty table: the floor is a governed,
    dashboard-edited judgment input (plan Step 16 「floor をコードに焼かない」),
    and silently measuring against baked constants is exactly the drift this
    plan removes. `query` is injectable for tests; default queries primary."""
    if query is None:
        conn = connect_primary()
        try:
            rows = q(conn, "SELECT key, value FROM tagger_config")
        finally:
            conn.close()
    else:
        rows = query("SELECT key, value FROM tagger_config")
    floor = {k: float(v) for k, v in rows if k in FLOOR_KEYS}
    if "min_precision" not in floor or "min_recall" not in floor:
        raise RuntimeError(
            "load_floor: tagger_config returned no usable floor "
            f"(got keys {sorted(floor)}) — refusing to fall back to baked defaults")
    return floor


# ------------------------------------------------------------- pure: grouping

def build_grouping(gt_rows: Sequence[tuple], recovered_ids: Sequence[int]
                   ) -> tuple[dict[int, int], dict[int, str]]:
    """Leakage groups for grouped CV (bug 1 fixed — round8 flavour is
    canonical): pool-derived GT groups by source_pool_id; pool-free GT and
    recovered human-adds are per-post singletons (-post_id). Collapsing
    pool-free rows into one shared group is the degeneration that produced
    auc=NaN in round 7's first run.

    gt_rows: (post_id, verdict, derived_from, source_pool_id) tuples.
    Returns (group_of, provenance)."""
    group_of: dict[int, int] = {}
    provenance: dict[int, str] = {}
    for post_id, _verdict, _derived, pool_id in gt_rows:
        if pool_id is not None:
            group_of[post_id] = pool_id
            provenance[post_id] = "pool"
        else:
            group_of[post_id] = -post_id
            provenance[post_id] = "pool_free"
    for i in recovered_ids:
        group_of[i] = -i
        provenance[i] = "recovered_human_add"
    return group_of, provenance


# -------------------------------------------------------------- pure: sampler

def sample_capped(ids: Sequence[int], group_of: Mapping[int, int], cap: int,
                  seed: int) -> list[int]:
    """Deterministic negative sampler that ALWAYS honours the total cap
    (bug 2 fix). Group-round-robin: groups and their members are shuffled with
    a seeded RNG, then one id is drawn from each group in turn until the cap
    is reached — group diversity is preserved (every group is represented
    before any group contributes twice), and the output can never exceed
    `cap`, regardless of how many singleton groups exist.

    The replaced implementation (`_k = max(1, round(len(_ids) * _quota))`)
    kept >= 1 member of EVERY group; with thousands of singleton groups it was
    a no-op — measured "in-gate neg sampled: 8527 -> 8527"."""
    ids = sorted(ids)  # canonical order in => deterministic out
    if cap < 0:
        raise ValueError(f"sample_capped: negative cap {cap}")
    if len(ids) <= cap:
        return ids
    rnd = random.Random(seed)
    by_g: dict[int, list[int]] = {}
    for i in ids:
        by_g.setdefault(group_of[i], []).append(i)
    groups = sorted(by_g)
    rnd.shuffle(groups)
    for g in groups:
        rnd.shuffle(by_g[g])
    out: list[int] = []
    while len(out) < cap:
        drew = False
        for g in groups:
            if by_g[g]:
                out.append(by_g[g].pop())
                drew = True
                if len(out) >= cap:
                    break
        if not drew:
            break
    return sorted(out)


# ---------------------------------------------------------------- pure: folds

def make_group_folds(ids: Sequence[int], group_of: Mapping[int, int], seed: int,
                     k: int, y: Mapping[int, int] | None = None) -> list[list[int]]:
    """Split ids into k folds BY GROUP (a group never spans folds). Deterministic
    for (ids, groups, seed, k[, y]).

    y (optional): id -> 0/1 label. When given, fold assignment is class-balanced
    (StratifiedGroupKFold-style greedy, same algorithm as train_base_finetune.py's
    group_folds): groups are assigned largest-first, each to whichever fold is
    currently thinnest across BOTH classes combined. Ties (equal group size)
    break by the seeded shuffle order below, so the seed still varies the split
    when there's a tie to vary.

    Found live 2026-08-09 (trainer, peeing r13, broker task 334582d2): the
    plain unstratified round-robin (y=None path) assigns groups to folds with
    no regard for class balance. When negatives cluster into few groups
    (verify-pool-sourced negatives, unlike organic corpus-wide singleton
    deletions), the odds that all k=5 folds land at least one negative-bearing
    group is a coupon-collector surjection probability -- 3.8% at G=5
    negative-bearing groups, 21.5% at G=7. vnext_manifest.py's check2
    (stratification floor) then fails not on genuine GT insufficiency but on
    seed luck, and growing GT can never fix that deterministically -- the
    dispatcher's own diagnosis was "GT が育っても測定可否がコイントスのまま".
    y=None (the original behaviour) stays available for callers with no
    labels at hand.
    """
    gs = sorted({group_of[i] for i in ids})
    rnd = random.Random(seed)
    rnd.shuffle(gs)
    if y is None:
        slices = [set(gs[i::k]) for i in range(k)]
        return [[i for i in ids if group_of[i] in s] for s in slices]

    by_g: dict[int, list[int]] = {}
    for i in ids:
        by_g.setdefault(group_of[i], []).append(i)
    # stable sort: ties (equal group size) keep the seeded-shuffle order above
    order = sorted(gs, key=lambda g: -len(by_g[g]))

    fold_of_group: dict[int, int] = {}
    cnt = [[0, 0] for _ in range(k)]  # per fold: [n_neg, n_pos]
    for g in order:
        members = by_g[g]
        gp = sum(1 for i in members if y.get(i))
        gn = len(members) - gp

        def cost(t: int) -> int:
            cnt[t][0] += gn; cnt[t][1] += gp
            c = sum(max(x[c_] for x in cnt) - min(x[c_] for x in cnt) for c_ in (0, 1))
            cnt[t][0] -= gn; cnt[t][1] -= gp
            return c

        j = min(range(k), key=lambda t: (cost(t), sum(cnt[t])))
        cnt[j][0] += gn; cnt[j][1] += gp
        fold_of_group[g] = j

    slices = [{g for g, fj in fold_of_group.items() if fj == t} for t in range(k)]
    return [[i for i in ids if group_of[i] in s] for s in slices]


# ----------------------------------------------------- pure: metrics/threshold

def rankauc(p: Sequence[float], n: Sequence[float]) -> float:
    import bisect
    if not p or not n:
        return float("nan")
    ns = sorted(n)
    tot = 0.0
    for x in p:
        lo, hi = bisect.bisect_left(ns, x), bisect.bisect_right(ns, x)
        tot += lo + 0.5 * (hi - lo)
    return tot / (len(p) * len(ns))


def fire(scores: Sequence[float], t: float) -> int:
    return sum(1 for x in scores if x >= t)


def _median(xs: Sequence[float]) -> float | None:
    """外側 fold ごとの閾値から配備用の代表値を 1 つ取る。

    平均ではなく中央値。1 つの fold が極端な閾値を選んでも代表値が動かない
    (閾値は「どこで切るか」なので、外れ値に引きずられると配備点がずれる)。
    """
    s = sorted(x for x in xs if x is not None)
    if not s:
        return None
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def thr_rules(sc: Mapping[int, float], y: Mapping[int, int]) -> dict[str, float]:
    """Pick an operating threshold per rule from (inner-fold) scores only."""
    p = [sc[i] for i in sc if y[i] == 1]
    n = [sc[i] for i in sc if y[i] == 0]
    picks = {}
    for rule in ("max_f1", "P>=0.85", "P>=0.90", "P>=0.95"):
        best, bt = -1.0, 1.0
        for t in THRS:
            tp, fp = fire(p, t), fire(n, t)
            if tp + fp == 0:
                continue
            P = tp / (tp + fp)
            R = tp / max(1, len(p))
            if rule == "max_f1":
                v = 2 * P * R / (P + R) if P + R else 0
                if v > best:
                    best, bt = v, t
            else:
                if P >= float(rule.split(">=")[1]) and R > best:
                    best, bt = R, t
        picks[rule] = bt
    return picks


def p_deploy(recall: float, fpr: float, pi: float) -> float:
    """Operating precision at the DEPLOYMENT prevalence pi:

        P_deploy = pi*R / (pi*R + (1-pi)*FPR)

    Why this and not the measured P (plan Step 14, floor key `min_p_deploy`):
    the measured precision depends on the class balance of the EVALUATION set,
    which the negative sampling cap changes; R and FPR are class-conditional
    rates and so are unaffected by sub-sampling the negatives. Recombining them
    at the true deployment prevalence recovers the precision the head would
    actually operate at.

    Measured live (Step 8b, licking_testicle): the in-gate negative cap
    1500 moved the evaluation prevalence from 1:23.4 to 1:3.8, which lifted the
    measured P from 0.85 to 0.90 at the `P>=0.90` rule and flipped the verdict
    HOLD -> PASS -- while the AUC moved by 0.0013. P_deploy for that same
    operating point is 0.60. Reporting P without P_deploy is how a
    sampling-budget knob silently became a verdict knob.

    Degenerate cases: pi<=0 (nothing to find) -> 0.0; a head that fires on
    nothing (R=FPR=0) -> 0.0 rather than 0/0.
    """
    if pi <= 0.0:
        return 0.0
    num = pi * recall
    den = num + (1.0 - pi) * fpr
    return (num / den) if den > 0 else 0.0


# Baseline-relative improvement floor (user directive 2026-08-02: 「b、また今まで
# 判別できなかったタグを判別できるのは価値だ」). FALLBACK ONLY -- read from
# tagger_config key `min_p_deploy_lift` when the row exists, exactly like every
# other threshold (config.py's DEFAULTS philosophy, plan Step 16 「floor をコード
# に焼かない」). No migration is required for the fallback to work; adding the
# governed row later changes the bar with no code change.
#
# Why 10x: on the tag that motivated this (licking_testicle) the absolute floor
# min_p_deploy=0.85 is equivalent to a 20.2x lift at pi=0.042, which no arm
# reaches at R>=0.7; the best honest operating point is 14.8x. 10x is "at least
# an order of magnitude better than the base tagger while still keeping the
# governed recall floor" -- a real bar (it rejects the 8.8x fire-on-everything
# point) that a genuinely useful veto head can clear. It is a PLACEHOLDER
# pending the owner's number; the plan revision that adopts lift as the gate
# should set the governed row.
MIN_P_DEPLOY_LIFT_FALLBACK = 10.0


def deploy_lift(pd: float | None, pi: float) -> float | None:
    """Baseline-relative improvement: how many times more precise the head's
    output is than what the base tagger already ships.

    The baseline for a veto/precision head is the base tagger's own deploy
    precision, which IS the prevalence pi (of everything it flags, a pi
    fraction is right). So lift = P_deploy / pi.

    Why this exists alongside the absolute min_p_deploy floor: for a heavily
    over-fired tag pi is tiny (licking_testicle: 0.042 -- 172,858 of 180,439
    flagged posts are wrong), and an absolute 0.85 floor rejects a head that
    removes 98% of those false positives and lifts precision 15x, because 0.85
    at pi=0.042 demands FPR<=0.54%. An absolute bar measures distance from
    perfection; lift measures the improvement actually delivered.

    MUST be paired with the recall floor. Lift rises monotonically as the
    threshold rises (fire on less, be righter about it): measured 8.8x at
    R=0.90 up to 17.2x at R=0.60. Without the R>=min_recall guard, "improve
    the tag" degenerates into "stop firing".
    """
    if pd is None or pi <= 0.0:
        return None
    return pd / pi


def deployed_patch_version(tag: str, manifest_path: Path = MANIFEST_PATH) -> int | None:
    """The version actually in the current release, per MANIFEST.json's
    'approved' allowlist (the same source build_release.py reads -- the
    allowlist IS the deployed-row ledger, not a hardcoded 'v1'). None if the
    tag has no approved entry (no deployed row to score against, e.g. a tag
    that was retired or never had a patch)."""
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    for row in manifest.get("approved", []):
        if row.get("tag") == tag:
            return row.get("version")
    return None


def gt_payload(n_pos: int, n_neg: int) -> dict[str, int]:
    """The gt dict a registration submits. Keys are the validator's vocabulary
    'positive'/'negative' with int values — round-8's registration id=33 wrote
    'pos'/'neg' and the reconciler's growth baseline silently read 0/0 (the
    fail-open the plan's Evidence documents). Shape is asserted here once,
    at the source."""
    payload = {"positive": int(n_pos), "negative": int(n_neg)}
    assert all(isinstance(v, int) for v in payload.values())
    return payload


# ------------------------------------------------------------------ torch fit

def fit_torch(getv, pos, neg, seed=0, iters=600, wd=1e-3, lr=0.05,
              extra_pos=(), extra_neg=()):
    """Logistic head on frozen features (lt_round8 `fit`, ported verbatim).
    Returns a scorer ids -> [sigmoid]. Device: CPU tensors — the vectors are
    small; only embedding extraction uses the GPU."""
    import torch
    torch.manual_seed(seed)
    P = list(pos) + list(extra_pos)
    N = list(neg) + list(extra_neg)
    Xp = torch.stack([getv(i) for i in P])
    Xn = torch.stack([getv(i) for i in N])
    A_ = torch.cat([Xp, Xn])
    mu, sd = A_.mean(0), A_.std(0) + 1e-6
    X = torch.cat([(Xp - mu) / sd, (Xn - mu) / sd])
    y = torch.cat([torch.ones(len(Xp)), torch.zeros(len(Xn))])
    w = torch.zeros(X.shape[1], requires_grad=True)
    b = torch.zeros(1, requires_grad=True)
    opt = torch.optim.Adam([w, b], lr=lr)
    lf = torch.nn.BCEWithLogitsLoss(pos_weight=torch.tensor(len(Xn) / max(1, len(Xp))))
    for _ in range(iters):
        opt.zero_grad()
        (lf(X @ w + b, y) + wd * w.norm() ** 2).backward()
        opt.step()

    def sc(ids):
        Z = (torch.stack([getv(i) for i in ids]) - mu) / sd
        with torch.no_grad():
            return torch.sigmoid(Z @ w + b).flatten().tolist()

    # **当てはめた係数を捨てない。**
    #
    # ここまでこの関数は scorer の closure だけを返しており、w/b/mu/sd は
    # 呼び出し側から**到達できなかった**。その結果、測定は「head を作れば
    # これだけ良くなる」を計算するのに **head そのものは 1 度も保存されず**、
    # 登録の `artifact_sha256` には測定 JSON の hash が入っていた
    # (2026-08-05 実測: monochrome の artifact_ref は
    #  `.../monochrome.measurement.json`)。
    #
    # つまり配備側から見ると **artifact という名前の列に model ではなく
    # 報告書が入っている**。patch 行は作れるが load できるものが無いので、
    # 「配備可」と判定されたタグが永久に適用されない。
    #
    # 係数を出しておけば、同じ当てはめから配備できる形を作れる
    # (下の fit_deployable)。
    sc.params = {"w": w.detach().clone(), "b": b.detach().clone(),
                 "mu": mu.clone(), "sd": sd.clone()}
    return sc


def fit_deployable(getv, pos, neg, *, seed: int = 0, **kw) -> dict:
    """配備できる形の head を 1 つ作る (**全 GT で最終当てはめ**)。

    CV の各 fold の当てはめは「この手法がどれだけ効くか」を測るためのもので、
    配備に使うのは**全データでの 1 本**。ここを省くと測っただけで終わる。

    返す形は推論側 (`tagger/patches/<tag>/vN.safetensors`、arm A が読む形) の
    `{w, b}` で、**標準化を線形層へ畳んである**:

        p = sigmoid( ((f - mu)/sd) @ w + b )
          = sigmoid( f @ (w/sd) + (b - Σ(w·mu/sd)) )

    畳まないと推論側が mu/sd を知らないまま生特徴を入れることになり、
    確率が静かにずれる (同じ形の事故を `distribute_heads.py` が別経路で
    記録している)。
    """
    import torch
    sc = fit_torch(getv, pos, neg, seed=seed, **kw)
    p = sc.params
    w_folded = p["w"] / p["sd"]
    b_folded = p["b"] - (p["w"] * p["mu"] / p["sd"]).sum()
    return {"w": w_folded.detach().contiguous(),
            "b": b_folded.detach().reshape(1).contiguous()}


# ------------------------------------------------- grouped OOF CV (+ manifest)

def grouped_oof(getv, pos, neg, group_of, *, tag: str, seed: int = 0,
                shuffle: bool = False, nested: bool = False,
                aug_pos=(), aug_neg=(), fit_fn=None,
                provenance: Mapping[int, str] | None = None,
                sampling_caps: Mapping[str, int] | None = None,
                observed_totals: Mapping[str, int] | None = None,
                outer_k: int = 5, inner_k: int = 3,
                stratification_floors: Mapping[str, int] | None = None,
                arm: str | None = None, deploy_prevalence: float | None = None):
    """Grouped out-of-fold CV with nested threshold selection — the lt_round8
    measurement core, with the split manifest generated and machine-verified
    BEFORE any fitting (INV-R10: no verified manifest, no measurement).

    Returns (sc_out, y, picks, manifest). Raises ManifestError fail-closed
    when the constructed split violates any of the four checks.
    """
    fit_fn = fit_fn or fit_torch
    ids = list(pos) + list(neg)
    pset = set(pos)
    y = {i: (1 if i in pset else 0) for i in ids}
    if shuffle:
        lab = [y[i] for i in ids]
        rr = random.Random(seed)
        rr.shuffle(lab)
        y = dict(zip(ids, lab))

    outer = make_group_folds(ids, group_of, seed, outer_k, y=y)
    inner_by_outer: dict[int, list[list[int]]] = {}
    if nested:
        for fi, fold in enumerate(outer):
            fold_set = set(fold)
            tr = [i for i in ids if i not in fold_set]
            inner_by_outer[fi] = make_group_folds(tr, group_of, seed + 1, inner_k,
                                                  y={i: y[i] for i in tr})

    prov = dict(provenance or {})
    for i in ids:
        prov.setdefault(i, "pool" if group_of[i] >= 0 else "singleton")
    manifest = build_manifest(
        tag=tag, seed=seed, labels=y, group_of={i: group_of[i] for i in ids},
        provenance=prov, outer_folds=outer, inner_folds_by_outer=inner_by_outer,
        sampling_caps=sampling_caps, observed_totals=observed_totals,
        stratification_floors=stratification_floors,
        extra={"shuffle": shuffle, "nested": nested, "outer_k": outer_k,
               "inner_k": inner_k, "n_aug_pos": len(list(aug_pos)),
               "n_aug_neg": len(list(aug_neg)),
               # ARM identity: two arms over the same ids/groups/seed produce
               # the SAME split, so without this the manifest hash cannot tell
               # feat1024 from logit and an artifact hash is not arm-unique
               # (found in the Step 8b reference run: B_feat1024 and B_logit
               # shared one hash).
               "arm": arm,
               # Evaluation vs deployment prevalence, recorded side by side so
               # the gap the sampling cap opens is visible IN the attestation
               # rather than only in the analysis (Step 8b finding).
               "eval_prevalence": (len(pos) / len(ids)) if ids else 0.0,
               "deploy_prevalence_pi": deploy_prevalence},
    )
    verify_manifest(manifest)  # fail-closed: raises before any fit

    sc_out: dict[int, float] = {}
    picks: list[tuple[dict, list[int]]] = []
    for fi, te in enumerate(outer):
        te_set = set(te)
        tr = [i for i in ids if i not in te_set]
        trp = [i for i in tr if y[i] == 1]
        trn = [i for i in tr if y[i] == 0]
        if not trp or not trn or not te:
            continue
        s = fit_fn(getv, trp, trn, seed=seed, extra_pos=aug_pos, extra_neg=aug_neg)
        for i, v in zip(te, s(te)):
            sc_out[i] = v
        if nested:
            isc: dict[int, float] = {}
            iy: dict[int, int] = {}
            for ite in inner_by_outer[fi]:
                ite_set = set(ite)
                itr = [i for i in tr if i not in ite_set]
                ip = [i for i in itr if y[i] == 1]
                inn = [i for i in itr if y[i] == 0]
                if not ip or not inn or not ite:
                    continue
                s2 = fit_fn(getv, ip, inn, seed=seed, extra_pos=aug_pos, extra_neg=aug_neg)
                for i, v in zip(ite, s2(ite)):
                    isc[i] = v
                    iy[i] = y[i]
            if isc:
                picks.append((thr_rules(isc, iy), te))
    return sc_out, y, picks, manifest


def summarize(sc, y, label, picks=None, echo=True, pi: float | None = None) -> dict:
    """`pi` = the deployment prevalence (probability that an item the head is
    applied to is truly positive). When given, every nested operating point
    also carries FPR and P_deploy -- the precision at pi rather than at the
    evaluation set's class balance (plan Step 14, floor `min_p_deploy`)."""
    p = [sc[i] for i in sc if y[i] == 1]
    n = [sc[i] for i in sc if y[i] == 0]
    d = {"auc": rankauc(p, n), "n_pos": len(p), "n_neg": len(n), "sweep": {},
         "eval_prevalence": (len(p) / (len(p) + len(n))) if (p or n) else 0.0,
         "deploy_prevalence_pi": pi}
    for t in THRS:
        tp, fp = fire(p, t), fire(n, t)
        if tp + fp:
            d["sweep"][f"{t:.2f}"] = {"P": tp / (tp + fp), "R": tp / max(1, len(p)),
                                      "tp": tp, "fp": fp}
    d["oracle_max_P"] = max([v["P"] for v in d["sweep"].values()] or [0.0])
    d["oracle_best_P_at_R>=0.7"] = max(
        [v["P"] for v in d["sweep"].values() if v["R"] >= 0.7] or [0.0])
    if picks:
        d["nested"] = {}
        for rule in ("max_f1", "P>=0.85", "P>=0.90", "P>=0.95"):
            tp = fp = 0
            for pick, te in picks:
                t = pick[rule]
                tp += sum(1 for i in te if y[i] == 1 and sc.get(i, 0) >= t)
                fp += sum(1 for i in te if y[i] == 0 and sc.get(i, 0) >= t)
            R_ = tp / max(1, len(p))
            fpr = fp / max(1, len(n))
            pd_ = None if pi is None else p_deploy(R_, fpr, pi)
            d["nested"][rule] = {"P": tp / max(1, tp + fp), "R": R_,
                                 "tp": tp, "fp": fp, "FPR": fpr,
                                 # None (not 0.0) when pi is unknown -- a missing
                                 # P_deploy must fail the floor closed, never
                                 # read as a real low value or a passing one.
                                 "P_deploy": pd_,
                                 # Baseline-relative view (user 2026-08-02):
                                 # improvement over what the base tagger ships.
                                 "P_deploy_lift": (None if pi is None
                                                   else deploy_lift(pd_, pi)),
                                 # Operational reading of the same operating
                                 # point, so a reviewer sees the trade directly.
                                 "fp_suppressed_frac": 1.0 - fpr,
                                 "tp_retained_frac": R_,
                                 # **動作点はそれ自身の閾値を持つ** (2026-08-05)。
                                 # これが無いと「どの点で配備するか」を測定から
                                 # 復元できず、配備側が自前の定数を持つことになる
                                 # (実際 infer.py の GREYOWL_ADD_THRESH=0.5 や
                                 # PIXAI_LOW_THRESH がそれ)。**測定が選んだ点を
                                 # そのまま配備できる**ようにするための値。
                                 #
                                 # 外側 fold ごとに picks が違いうるので単一値では
                                 # ない。配備に使えるのは「全 fold を通した 1 つの
                                 # 閾値」なので、fold 間の中央値を代表に取る
                                 # (平均は外れ fold に引きずられる)。fold ごとの
                                 # 値も残す — 散らばりが大きい点は配備に向かない
                                 # ことが読み手に分かる。
                                 "thr": _median([pick[rule] for pick, _ in picks]),
                                 "thr_by_fold": [pick[rule] for pick, _ in picks]}
    if echo:
        print(f"\n=== {label} ===\n" + json.dumps(d, indent=1, default=float), flush=True)
    return d


def permutation_null(getv, pos, neg, group_of, *, tag: str, n_perm: int,
                     fit_fn=None, **oof_kw) -> dict:
    """Label-shuffle null distribution of the OOF AUC. Each permutation runs
    the SAME manifest-verified pipeline; a shuffled split that fails a
    stratification check is recorded (not silently dropped) and skipped."""
    perm: list[float] = []
    invalid = 0
    for s in range(n_perm):
        try:
            ps, py, _, _ = grouped_oof(getv, pos, neg, group_of, tag=tag, seed=s,
                                       shuffle=True, fit_fn=fit_fn, **oof_kw)
        except ManifestError:
            invalid += 1
            continue
        pp = [ps[i] for i in ps if py[i] == 1]
        pn = [ps[i] for i in ps if py[i] == 0]
        if pp and pn:
            perm.append(rankauc(pp, pn))
    perm.sort()
    if not perm:
        return {"n": 0, "invalid_split_skipped": invalid}
    return {"n": len(perm), "invalid_split_skipped": invalid,
            "min": perm[0], "median": perm[len(perm) // 2], "max": perm[-1]}


# -------------------------------------------------------- population assembly

def load_populations(tag: str, primary_conn, loc_conn, *, seed: int,
                     neg_cap: int = NEG_CAP_DEFAULT,
                     oog_neg_cap: int = OOG_NEG_CAP_DEFAULT,
                     rand_sample: int = RAND_SAMPLE_DEFAULT,
                     corpus_limit: int = CORPUS_LIMIT_DEFAULT) -> dict:
    """In-gate population construction (lt_round8 lineage): GT from primary
    (canonical), image presence from the replica. Returns a dict of id lists,
    grouping, provenance, caps and observed totals — everything the manifest
    needs to attest the sampling."""
    IMG = f"and {display_bytes.HAS_IMAGE} and p.type='image'"
    gt_rows = q(primary_conn, "select g.post_id, g.verdict, g.derived_from, g.source_pool_id "
                          "from tagger_gt_label g where g.tag=%s", (tag,))
    gt_ids = [r[0] for r in gt_rows]
    have = set(i for (i,) in q(loc_conn,
               f"select id from post p where p.id=any(%s) {IMG}", (gt_ids,))) if gt_ids else set()
    posset = {r[0]: r for r in gt_rows if r[1] == "positive"}
    negset = {r[0]: r for r in gt_rows if r[1] == "negative"}

    hadd = [i for (i,) in q(primary_conn, "select distinct a.post_id from post_tag_audit a "
                                      "where a.actor_type='human' and %s=any(a.added_tags)", (tag,))]
    rec = [i for i in hadd if i not in posset and i not in negset]
    if rec:
        have |= set(i for (i,) in q(loc_conn,
                    f"select id from post p where p.id=any(%s) {IMG}", (rec,)))

    group_of, provenance = build_grouping(gt_rows, rec)

    ig_pos = [i for i in sorted(posset) + sorted(rec) if i in have]
    ig_neg_full = sorted(i for i, r in negset.items()
                         if r[2] == "human_correction" and i in have)
    oog_neg_full = sorted(i for i, r in negset.items()
                          if r[2] == "implicit_review" and i in have)

    # bug 2 fix: total cap ENFORCED (see sample_capped)
    ig_neg = sample_capped(ig_neg_full, group_of, neg_cap, seed=seed + 7)
    oog_neg = (sorted(random.Random(seed + 8).sample(oog_neg_full, oog_neg_cap))
               if len(oog_neg_full) > oog_neg_cap else oog_neg_full)

    randpool = [i for (i,) in q(loc_conn,
                "select p.id from post p where p.type='image' and p.media_location = 'pg' "
                "and p.id not in (select post_id from tagger_gt_label where tag=%s) "
                "order by p.id limit 100000", (tag,))]
    rand = sorted(random.Random(seed).sample(randpool, min(rand_sample, len(randpool))))

    gtids = set(ig_pos) | set(ig_neg) | set(oog_neg)
    corp_pre = [i for (i,) in q(loc_conn,
                "select pt.post_id from post_tag pt join tag_name tn "
                "on tn.tag_id=pt.tag_id and tn.ord=0 and tn.name=%s "
                "join post p on p.id=pt.post_id where p.type='image' and p.media_location = 'pg' "
                "and not exists (select 1 from post_tag_audit a where a.post_id=pt.post_id "
                "  and a.actor_id like 'nodec-autotag-r%%' and %s=any(a.added_tags)) "
                "order by pt.post_id limit 3000", (tag, tag)) if i not in gtids]

    # Non-GT populations (random archive / corpus augmentation) still need
    # groups for the manifest: they are not pool-reviewed, so per-post
    # singletons — independent draws, no shared leakage structure.
    for i in rand + corp_pre:
        group_of.setdefault(i, -i)
        provenance.setdefault(i, "archive_random" if i in set(rand) else "corpus_present")

    # DEPLOYMENT PREVALENCE pi (plan Step 14 / floor min_p_deploy). A veto or
    # precision head is applied to the posts the base tagger already flagged --
    # the tag's live corpus population -- so pi is the rate at which a FLAGGED
    # post is truly positive, estimated from the UNCAPPED in-gate GT. It is
    # therefore independent of the negative sampling cap, which is exactly why
    # P_deploy is the cap-proof floor term (the measured P is not).
    ig_prevalence = (len(ig_pos) / (len(ig_pos) + len(ig_neg_full))
                     if (ig_pos or ig_neg_full) else 0.0)
    corpus_flagged = q(loc_conn,
                       "select count(*) from post_tag pt join tag_name tn "
                       "on tn.tag_id=pt.tag_id and tn.ord=0 where tn.name=%s", (tag,))[0][0]

    return {
        "gt_rows": gt_rows,
        "ig_pos": ig_pos, "ig_neg": ig_neg, "oog_neg": oog_neg,
        "ig_neg_full_n": len(ig_neg_full), "oog_neg_full_n": len(oog_neg_full),
        # full tagger_gt_label store size (not eval-set-capped, not
        # image-presence-filtered) -- same semantics as reconciler._gt_counts,
        # since gt_rows/posset/negset are built from the UNFILTERED query
        # above. This is what a dispatch-growth baseline must compare against
        # (plan Evidence: the eval-set-capped count degrades the auto-hold to
        # fail-open once true GT store size exceeds the cap).
        "gt_full_pos_n": len(posset), "gt_full_neg_n": len(negset),
        "deploy_prevalence_pi": ig_prevalence,
        "corpus_flagged_n": corpus_flagged,
        "rand": rand, "corp_pre": corp_pre[:corpus_limit],
        "recovered": [i for i in rec if i in have],
        "group_of": group_of, "provenance": provenance,
        "sampling_caps": {"in_gate_neg": neg_cap, "out_of_gate_neg": oog_neg_cap,
                          "random_archive": rand_sample, "corpus_aug": corpus_limit},
        "observed_totals": {"in_gate_neg": len(ig_neg), "out_of_gate_neg": len(oog_neg),
                            "random_archive": len(rand),
                            "corpus_aug": len(corp_pre[:corpus_limit])},
    }


# ------------------------------------------------------------ heavy: features

def _image_transform():
    from torchvision import transforms
    return transforms.Compose([transforms.Resize((INPUT_SIZE, INPUT_SIZE)),
                               transforms.ToTensor()])  # infer.py verbatim


def _device() -> str:
    import torch
    return os.environ.get("VNEXT_DEV") or ("cuda" if torch.cuda.is_available() else "cpu")


def _save_feature_cache(cache: dict, cache_path: Path) -> None:
    """Publish a complete checkpoint without overwriting the last valid one.

    A killed writer may leave a temporary file; readers only use cache_path.
    The caller must serialize writers to a given cache (as for extraction).
    """
    import torch
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=cache_path.parent,
                                         prefix=cache_path.name + ".", suffix=".tmp",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            torch.save(cache, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, cache_path)
        directory_fd = os.open(cache_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _extract_features(loc_conn, ids: list[int], cache_path: Path, *,
                      classifier_reset: bool, label: str,
                      model_identity_path: Path | None = None) -> dict:
    """Shared extractor for kura-space embeddings (classifier_reset=True) and
    WD14 logits (False). Cache is post-keyed and tag-independent. Atomically
    checkpoint after batches at least 30 seconds apart, then flush on success.
    Interruptions lose work since the last checkpoint, never that checkpoint.
    """
    import torch
    if model_identity_path is not None and cache_path.exists() and not model_identity_path.exists():
        raise RuntimeError("dedicated cache has no frozen backbone identity; restore evidence or use a new run")
    cache = torch.load(cache_path) if cache_path.exists() else {}
    missing = [i for i in ids if i not in cache]
    print(f"[vnext_arms] {label}: {len(missing)} to extract / {len(ids)} wanted "
          f"({len(cache)} cached)", flush=True)
    if not missing:
        return cache
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    import timm
    dev = _device()
    batch_n = int(os.environ.get("VNEXT_EMB_BATCH", "8"))  # GPU shared with ComfyUI
    tf = _image_transform()
    from PIL import Image
    bb = timm.create_model("hf-hub:SmilingWolf/wd-swinv2-tagger-v3", pretrained=True)
    if classifier_reset:
        bb.reset_classifier(0)
    if model_identity_path is not None:
        import hashlib
        from tagger.dedicated_protocol import write_once
        h = hashlib.sha256()
        for name, tensor in sorted(bb.state_dict().items()):
            h.update(name.encode())
            h.update(str(tensor.dtype).encode())
            h.update(str(tuple(tensor.shape)).encode())
            h.update(tensor.detach().cpu().contiguous().numpy().tobytes())
        write_once(model_identity_path, {
            "schema": "dedicated-backbone/1", "weights_sha256": h.hexdigest(),
            "model": "hf-hub:SmilingWolf/wd-swinv2-tagger-v3", "classifier_reset": classifier_reset,
            "transform": repr(tf), "timm_version": timm.__version__, "torch_version": torch.__version__})
    bb.eval().to(dev)
    last_checkpoint = time.monotonic()
    dirty = False
    try:
        for i in range(0, len(missing), batch_n):
            chunk = missing[i:i + batch_n]
            batch = display_bytes.load(lambda sql, a: q(loc_conn, sql, a), chunk)
            imgs, keep = [], []
            for pid, data in batch:
                try:
                    imgs.append(tf(Image.open(io.BytesIO(bytes(data))).convert("RGB")))
                    keep.append(pid)
                except Exception:
                    continue
            if not imgs:
                continue
            with torch.no_grad():
                f = bb(torch.stack(imgs).to(dev)).float().cpu()
            for pid, v in zip(keep, f):
                cache[pid] = v
            dirty = True
            print(f"  {label} {i + len(chunk)}/{len(missing)}", flush=True)
            if time.monotonic() - last_checkpoint >= 30:
                _save_feature_cache(cache, cache_path)
                last_checkpoint = time.monotonic()
                dirty = False
        if dirty:
            _save_feature_cache(cache, cache_path)
    finally:
        del bb
        if dev == "cuda":
            torch.cuda.empty_cache()
    return cache


def _pixai_scores(loc_conn, tag: str, ids: list[int]) -> dict[str, float] | None:
    """Stock native PixAI scores for this tag's column; per-tag cache file.
    Returns None when the tag is not in the PixAI vocabulary (arm E does not
    apply) — recorded by the caller, never silently dropped."""
    import csv
    import numpy as np
    import onnxruntime as rt
    from PIL import Image
    if not os.path.isdir(PIXAI_DIR) or not os.listdir(PIXAI_DIR):
        return None
    snap = os.path.join(PIXAI_DIR, os.listdir(PIXAI_DIR)[0])
    with open(os.path.join(snap, "selected_tags.csv"), newline="", encoding="utf-8") as f:
        names = [r["name"] for r in csv.DictReader(f)]
    hits = [i for i, name in enumerate(names) if name == tag]
    if len(hits) != 1:
        return None
    tidx = hits[0]

    # ── post 単位の共有キャッシュ (2026-08-03) ────────────────────────────
    # モデルは 1 回の推論で **全 10,861 タグの予測**を出しているのに、旧実装は
    # 対象タグ 1 個のスカラーだけ per-tag ファイルに保存して残りを捨てていた。
    # 16 タグを測れば同じ画像を 16 回スコアリングすることになり、実測で 1 タグ
    # あたり ~27 分 (1,250 posts) のこの腕が丸ごと重複していた
    # (user 指摘 2026-08-03「待っている間、一番大切な時間という予算を失っている」)。
    # 全ベクトルを post キーで float16 保存する (10,861 × 2B ≒ 21KB/post) と、
    # 2 タグ目以降はディスクからスライスするだけで済む。per-tag JSON は
    # 既存資産の読み出し互換のために残す (書き込みは共有側が正)。
    shared_path = CACHE_DIR / "pixai_logits_by_post.pt"
    shared: dict[int, "object"] = {}
    try:
        import torch as _t
        if shared_path.exists():
            shared = _t.load(shared_path)
    except Exception as e:  # noqa: BLE001
        print(f"  pixai shared-cache load failed ({e}) — per-tag cache のみ使う", flush=True)

    cache_path = pixai_cache_path(tag)
    pxs: dict[str, float] = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    # 共有キャッシュにベクトルがある post は推論不要 — 対象タグの位置を切るだけ。
    for i in ids:
        if str(i) not in pxs and i in shared:
            pxs[str(i)] = float(shared[i][tidx])
    need = [i for i in ids if str(i) not in pxs]
    if need:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        sess = rt.InferenceSession(os.path.join(snap, "model.onnx"),
                                   providers=["CPUExecutionProvider"])
        in_name = sess.get_inputs()[0].name
        pred_i = [o.name for o in sess.get_outputs()].index("prediction")

        def preprocess(b: bytes):
            img = Image.open(io.BytesIO(b)).convert("RGBA")
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg.convert("RGB").resize((INPUT_SIZE, INPUT_SIZE), Image.BILINEAR)
            a = (np.array(img, dtype=np.float32) / 255.0 - 0.5) / 0.5
            return np.expand_dims(a.transpose(2, 0, 1), axis=0)

        t0 = time.time()
        for n, pid in enumerate(need, 1):
            row = display_bytes.load(lambda sql, a: q(loc_conn, sql, a), [pid])
            if not row:
                continue
            try:
                s = sess.run(None, {in_name: preprocess(row[0][1])})[pred_i][0]
            except Exception as e:  # noqa: BLE001
                print(f"  pixai skip {pid} {e}", flush=True)
                continue
            pxs[str(pid)] = float(s[tidx])
            try:
                import torch as _t
                shared[pid] = _t.from_numpy(np.asarray(s, dtype=np.float32)).half()
            except Exception:  # noqa: BLE001 — 共有キャッシュは最適化であって必須ではない
                pass
            if n % 50 == 0:
                print(f"  pixai {n}/{len(need)} {(time.time() - t0) / 60:.1f}min", flush=True)
                cache_path.write_text(json.dumps(pxs))
                _save_shared_pixai(shared_path, shared)
        cache_path.write_text(json.dumps(pxs))
        _save_shared_pixai(shared_path, shared)
    return pxs


# -------------------------------------------------------------------- measure

def legitimate_operating_points(pool_arms, *, min_recall: float,
                                min_val_pos: float, min_pred_pos: float) -> list[dict]:
    """**正当な動作点** = recall floor と支持数 floor を保つ (arm, rule) の一覧。

    **lift floor を混ぜてはいけない** (nodec-cc-primary のレビュー、2026-08-03)。
    `clears_lift` は `lift >= min_p_deploy_lift` (既定 10 倍) を含むが、あれは
    配備可否の policy dial。正当性に混ぜると **smegma (lift 5.95 倍) の動作点が
    0 件になり hold に落ちて逆転が復活する** — 4 クラス判定が存在する理由その
    ものが消える。

    inline ではなくここに切り出してあるのは**検査するため**。fixture には抽出
    済みの結果が焼き込まれるので、抽出を inline に置いたままだと lift 条件を
    混ぜても**テストが 1 件も落ちない** (2026-08-04 に実際に改変して確認した)。
    """
    return [
        {"arm": k, "rule": rule, "R": v["R"], "FPR": v.get("FPR"),
         "P_deploy_lift": v.get("P_deploy_lift"), "P_deploy": v.get("P_deploy")}
        for k, d in pool_arms for rule, v in (d.get("nested") or {}).items()
        if (v["R"] >= min_recall and v["tp"] >= min_val_pos
            and (v["tp"] + v["fp"]) >= min_pred_pos and v.get("FPR") is not None)
    ]


from tagger.vnext_class import DEFAULT_NEGLIGIBLE_LIFT as NEGLIGIBLE_LIFT_FALLBACK

# 「既存の base tagger そのもの」の腕。ここが通っても自前 head の成果ではない。
_BASELINE_ARMS = {"E_stock_pixai"}


def _classify(out: dict):
    """測定結果を 4 クラスへ。**verdict の解釈はここで完結させる** (vnext_class は
    平らな数値しか受け取らない — 辞書を掘らせると意味の誤読が起きる)。"""
    from tagger.vnext_class import OperatingPoint, classify

    v = out.get("verdict") or {}
    pop = out.get("deploy_prevalence") or {}
    # **無いキーを「偽」と読まない** (2026-08-03 の実装事故の核心)。
    # any_arm_above_chance が欠けたまま False と読むと「どの腕も偶然を超えて
    # いない」= 定義を見直せ、という**正反対の助言**を自信満々に出す。
    # 欠けている = 安全ではなく未確認。ここは落とす (fail-closed)。
    missing = [k for k in ("any_arm_above_chance", "legitimate_operating_points")
               if k not in v]
    if missing:
        raise ValueError(
            f"verdict に {', '.join(missing)} が無い — 測定スキーマが古い "
            f"(schema={out.get('schema')!r}、4 クラス判定は vnext-measurement/2 以降)。"
            "再測定するか、腕データから同じ定義で再構成すること")
    points = [OperatingPoint(arm=p["arm"], rule=p["rule"], recall=p["R"],
                             fpr=p["FPR"], lift=p.get("P_deploy_lift"),
                             p_deploy=p.get("P_deploy"))
              for p in (v.get("legitimate_operating_points") or [])]
    # 届かなかった理由: 最も惜しい腕の binding 項をそのまま渡す (裸の HOLD は
    # 「何をすれば届くのか」が分からず dead-end になる)。
    binding: list[str] = []
    for _arm, info in (v.get("binding_lift_terms_by_arm") or {}).items():
        if info.get("binding_terms"):
            binding = list(info["binding_terms"])
            break
    # baseline (stock) 腕だけが floor を通過した場合を見分ける。
    # 「自前 head の開発失敗」を「配備可」と呼ばないため (レビュー指摘 3)。
    passing_arms = set((v.get("arms_clearing_full_floor") or {}).keys())
    baseline_only = bool(passing_arms) and passing_arms <= _BASELINE_ARMS
    floor = v.get("floor") or {}
    return classify(
        absolute_pass_is_baseline_only=baseline_only,
        any_arm_above_chance=bool(v.get("any_arm_above_chance")),
        absolute_pass=bool(v.get("any_arm_meets_floor")),
        legit_points=points, pi=pop.get("pi"),
        n_flagged=pop.get("corpus_flagged_n"), binding_terms=binding,
        negligible_lift=float(floor.get("min_p_deploy_lift_negligible",
                                        NEGLIGIBLE_LIFT_FALLBACK)),
        # 規模 floor。**config に無ければ渡さない** — 既定値をコード側で勝手に
        # 決めると、config を見た人が実際の判定を再現できなくなる (fail-closed
        # ではなく「policy はデータ側にある」の徹底)。
        min_net_fp_removed=(float(floor["min_net_fp_removed"])
                            if floor.get("min_net_fp_removed") is not None else None))


def _save_deployable_head(out: dict, tag: str, spaces: dict, pos, neg, seed: int,
                          aug_pos=(), aug_neg=()) -> None:
    """判定が選んだ腕で全 GT に最終当てはめし、**bytes を content-addressed に置く**。

    `out["artifact_sha256"]` / `out["artifact_ref"]` をここで確定させる。

    **identity は中身の hash**。ファイル名や版番号を identity にすると、同じ
    bytes が別の名前で増えたり (実測: smegma v2..v7 のうち 3 版ずつが同じ
    patch_sha256)、名前は同じで中身が違うものが同一視される。

    保存できないとき (対象の腕が feat1024 空間でない / torch が無い等) は
    **artifact_sha256 を設定しない**。設定しなければ下流は「配備できるものが
    無い」と分かる — ここで測定 JSON の hash を入れてしまうと、報告書が
    model の顔をして patch まで通る (それが 2026-08-05 まで起きていたこと)。
    """
    klass = out.get("classification") or {}
    if klass.get("verdict") != "deployable":
        return
    at = klass.get("at")
    if not isinstance(at, str) or "@" not in at:
        return
    arm = at.split("@", 1)[0]
    # **腕は「特徴空間」ではなく「作り方」。** corpus_aug は feat1024 と同じ
    # 空間で、学習時に corpus 由来の正例と無作為負例を足しただけ (arm C の
    # grouped_oof は spaces["feat1024"] を渡している)。名前で spaces を引くと
    # 引けず、**配備できるのに落とす** — 2026-08-06 実測: greyscale の最良腕が
    # corpus_aug で、head が 1 度も保存されなかった。
    #
    # **測った腕と同じ作り方で当てはめる。** augmentation を外すと、記録されて
    # いる性能は配備されるものの性能ではなくなる。
    fit_kw: dict = {}
    if arm == "corpus_aug":
        getv = spaces.get("feat1024")
        fit_kw = {"extra_pos": list(aug_pos), "extra_neg": list(aug_neg)}
    else:
        getv = spaces.get(arm)
    if getv is None:
        # feat1024 以外の腕 (logit / corpus_aug) は推論側の読む形が違う。
        # **黙って別の腕の head を保存しない** — 測った腕と配備する腕が
        # ずれると、記録されている性能は配備されるものの性能ではない。
        print(f"[vnext] {tag}: 判定の腕 {arm} は配備できる形を持たない "
              f"(推論側が読むのは feat1024 空間の w/b) — head は保存しない",
              flush=True)
        return
    try:
        import torch  # noqa: F401
        from api.tagger_core.artifact_store import default_store, ingest_file
        from tagger.content_head_model import WD_SWINV2
        from tagger.distribute_heads import bundle_from_folded
        head = fit_deployable(getv, pos, neg, seed=seed, **fit_kw)
        # **推論側が読む形で出す。** `content_head_model.load` は
        # `{'model': backbone+head の state_dict}` を `torch.load` で読む。
        # 生の `{w, b}` を出すと全ノードで `invalid load key` になる (2026-08-05 実測)。
        model = bundle_from_folded(head["w"].reshape(1, -1), head["b"])
        store = default_store()
        tmp = store.root / f".fit-{tag}.pt"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        torch.save({"model": model.state_dict(), "meta": {"backbone": WD_SWINV2}}, tmp)
        sha = ingest_file(store, tmp, move=True)
        out["artifact_sha256"] = sha
        out["artifact_ref"] = str(store.path_for(sha))
        print(f"[vnext] {tag}: 配備できる head を保存 {sha[:12]} (腕 {arm})",
              flush=True)
    except Exception as e:  # noqa: BLE001
        # **失敗を artifact_sha256 の欠落として表す。** 例外で測定ごと落とすと
        # 判定まで捨てることになるが、成功したふりをすると報告書が model として
        # 流れる。欠落は下流が fail-closed に扱う。
        print(f"[vnext] {tag}: head を保存できない — {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)


def measure_tag(tag: str, *, seed: int = DEFAULT_SEED, out_path: str | None = None,
                n_perm: int = N_PERM_DEFAULT, neg_cap: int = NEG_CAP_DEFAULT,
                floor: Mapping[str, float] | None = None,
                dedicated_snapshot: str | None = None) -> dict:
    """Full v-next measurement for one tag: arms A (deployed row), B (veto
    ceiling: feat1024 / logit spaces), C (dispatch recipe: corpus-augmented),
    E (stock PixAI), each grouped-nested-CV'd behind a verified split manifest,
    plus permutation null and a floor verdict. The ONLY measurement entry
    point (INV-R9)."""
    if dedicated_snapshot is not None:
        from tagger.dedicated_protocol import measure
        return measure(tag, dedicated_snapshot, out_path)

    import torch

    # /2 で verdict に any_arm_above_chance と legitimate_operating_points が
    # 増えた (4 クラス判定の入力)。旧ファイルはこのキーを持たないので、
    # 判定側は版で区別して fail-closed に倒せる。
    out: dict = {"schema": "vnext-measurement/2", "tag": tag, "seed": seed}
    floor = dict(floor if floor is not None else load_floor())
    out["floor_live_primary"] = floor

    primary_conn, loc_conn = connect_primary(), connect_local()
    try:
        out["ran_at"] = str(q(loc_conn, "select now()")[0][0])
        pop = load_populations(tag, primary_conn, loc_conn, seed=seed, neg_cap=neg_cap)
    finally:
        primary_conn.close()

    try:
        group_of, provenance = pop["group_of"], pop["provenance"]
        ig_pos, ig_neg, oog_neg = pop["ig_pos"], pop["ig_neg"], pop["oog_neg"]
        rand, corp_pre = pop["rand"], pop["corp_pre"]
        out["counts"] = {
            "in_gate_pos": len(ig_pos), "in_gate_neg": len(ig_neg),
            "in_gate_neg_before_cap": pop["ig_neg_full_n"],
            "out_of_gate_neg": len(oog_neg),
            "out_of_gate_neg_before_cap": pop["oog_neg_full_n"],
            "recovered_human_adds": len(pop["recovered"]),
            "random_archive_sample": len(rand),
            "corpus_pre_noncircular": len(corp_pre),
        }
        # full tagger_gt_label store size, NOT the eval-set (capped at
        # neg_cap). The reconciler's growth baseline (_gt_counts) reads the
        # full store; a capped gt here understates negative growth forever
        # once the store exceeds the cap, which degrades the rejected-
        # registration auto-hold to fail-open (see load_populations).
        out["gt"] = gt_payload(pop["gt_full_pos_n"], pop["gt_full_neg_n"])
        print(json.dumps(out["counts"], indent=1), flush=True)

        # features (post-keyed, tag-independent caches)
        all_ids = list(dict.fromkeys(ig_pos + ig_neg + oog_neg + rand + corp_pre))
        emb = _extract_features(loc_conn, all_ids, EMB_CACHE,
                                classifier_reset=True, label="kura-space emb")
        gtall = ig_pos + ig_neg + oog_neg
        lg = _extract_features(loc_conn, gtall, LOGIT_CACHE,
                               classifier_reset=False, label="wd14 logits")

        present = lambda L: [i for i in L if i in emb]  # noqa: E731
        ig_pos, ig_neg, oog_neg, rand = (present(ig_pos), present(ig_neg),
                                         present(oog_neg), present(rand))
        corp_pre = [i for i in corp_pre if i in emb]
        lgp = lambda L: [i for i in L if i in lg]  # noqa: E731

        spaces = {"feat1024": lambda i: emb[i], "logit": lambda i: lg[i]}
        pi = pop["deploy_prevalence_pi"]
        out["deploy_prevalence"] = {
            "pi": pi,
            "basis": ("uncapped in-gate GT: positives / (positives + ALL in-gate "
                      "negatives) -- the rate at which a post the base tagger FLAGGED "
                      "is truly positive; cap-independent by construction"),
            "in_gate_pos": len(ig_pos), "in_gate_neg_uncapped": pop["ig_neg_full_n"],
            "corpus_flagged_n": pop["corpus_flagged_n"],
            "eval_prevalence_after_cap": (len(ig_pos) / (len(ig_pos) + len(ig_neg))
                                          if (ig_pos or ig_neg) else 0.0),
        }
        oof_kw = dict(provenance=provenance, sampling_caps=pop["sampling_caps"],
                      observed_totals=pop["observed_totals"],
                      deploy_prevalence=pi)
        manifests: dict[str, str] = {}

        # ---- arm A: deployed row (score-only, no split -> no manifest).
        # Key stays "A_deployed" (was "A_deployed_v1") -- vnext_runner._arms_of
        # reads this group by a literal name, so the key itself must not
        # depend on which version is live; the actual version is carried in
        # the "version" field for traceability.
        deployed_v = deployed_patch_version(tag)
        patch = PATCHES_DIR / tag / f"v{deployed_v}.safetensors" if deployed_v else None
        if patch is not None and patch.exists():
            from safetensors.torch import load_file
            v1 = load_file(str(patch))
            w1, b1 = v1["w"].float().flatten(), v1["b"].float().flatten()

            def rowscore(ids):
                with torch.no_grad():
                    return torch.sigmoid(
                        torch.stack([emb[i] for i in ids]) @ w1 + b1).flatten().tolist()

            sp, sn, sr = rowscore(ig_pos), rowscore(ig_neg), rowscore(rand)
            A = {"version": deployed_v, "auc_in_gate_keep_vs_remove": rankauc(sp, sn), "sweep": {}}
            for t in [0.3, 0.35, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99]:
                tp, fp = fire(sp, t), fire(sn, t)
                A["sweep"][f"{t:.2f}"] = {
                    "P": tp / max(1, tp + fp), "R": tp / max(1, len(sp)),
                    "tp": tp, "fp": fp,
                    "archive_fire_rate": fire(sr, t) / max(1, len(sr))}
            out["A_deployed"] = A
        else:
            out["A_deployed"] = {
                "skipped": (f"no deployed row at {patch}" if patch is not None
                           else f"no MANIFEST.json approved entry for {tag}")}

        # ---- arm B: veto ceiling per space, nested + permutation null
        out["B_veto_ceiling"] = {}
        for space, getv in spaces.items():
            P_, N_ = ((lgp(ig_pos), lgp(ig_neg)) if space == "logit"
                      else (ig_pos, ig_neg))
            sc, y, picks, man = grouped_oof(getv, P_, N_, group_of, tag=tag,
                                            seed=seed, nested=True,
                                            arm=f"B_{space}", **oof_kw)
            manifests[f"B_{space}"] = man["manifest_hash"]
            d = summarize(sc, y, f"B veto ceiling [{space}]", picks, pi=pi)
            d["permutation_null"] = permutation_null(getv, P_, N_, group_of, tag=tag,
                                                     n_perm=n_perm,
                                                     arm=f"B_{space}_perm", **oof_kw)
            d["above_chance"] = (d["auc"] > d["permutation_null"].get("max", float("inf"))
                                 if d["permutation_null"]["n"] else False)
            d["manifest"] = man
            out["B_veto_ceiling"][space] = d

        # ---- arm C: dispatch recipe (corpus positives as augmentation)
        out["C_dispatch_recipe"] = {}
        if len(corp_pre) >= 50:
            sc, y, picks, man = grouped_oof(
                spaces["feat1024"], ig_pos, ig_neg, group_of, tag=tag, seed=seed,
                nested=True, aug_pos=corp_pre, aug_neg=rand[:400],
                arm="C_corpus_aug", **oof_kw)
            manifests["C_corpus"] = man["manifest_hash"]
            d = summarize(sc, y, "C dispatch recipe [corpus aug] in-gate", picks, pi=pi)
            d["n_corpus_positives_used"] = len(corp_pre)
            d["manifest"] = man
            out["C_dispatch_recipe"]["corpus_aug"] = d

        # ---- arm E: stock native PixAI (threshold-only, still nested + manifest)
        pxs = _pixai_scores(loc_conn, tag, ig_pos + ig_neg + rand)
        if pxs is None:
            out["E_stock_pixai"] = {"skipped": "tag not in PixAI vocabulary "
                                               "or model dir missing"}
        else:
            pxof = {i: pxs[str(i)] for i in ig_pos + ig_neg if str(i) in pxs}
            pxy = {i: (1 if i in set(ig_pos) else 0) for i in pxof}
            px_ids = sorted(pxof)
            px_pos = [i for i in px_ids if pxy[i] == 1]
            px_neg = [i for i in px_ids if pxy[i] == 0]
            folds = make_group_folds(px_ids, group_of, seed, 5, y=pxy)
            inner = {}  # threshold-only: outer folds pick thresholds from the rest
            px_picks = []
            for fi, te in enumerate(folds):
                te_set = set(te)
                tr = {i: pxof[i] for i in px_ids if i not in te_set}
                if not te or not tr:
                    continue
                px_picks.append((thr_rules(tr, {i: pxy[i] for i in tr}), te))
                inner[fi] = [sorted(tr)]
            man = build_manifest(
                tag=tag, seed=seed, labels=pxy,
                group_of={i: group_of[i] for i in px_ids},
                provenance={i: provenance.get(i, "unknown") for i in px_ids},
                outer_folds=folds, inner_folds_by_outer=inner,
                sampling_caps=pop["sampling_caps"],
                observed_totals=pop["observed_totals"],
                extra={"arm": "E_stock_pixai", "threshold_only": True,
                       "eval_prevalence": (len(px_pos) / len(px_ids)) if px_ids else 0.0,
                       "deploy_prevalence_pi": pi})
            verify_manifest(man)
            manifests["E_pixai"] = man["manifest_hash"]
            E = summarize(pxof, pxy, "E stock native PixAI", px_picks, pi=pi)
            E["archive_fire_rate_at_0.4"] = (
                fire([pxs[str(i)] for i in rand if str(i) in pxs], 0.4)
                / max(1, len([i for i in rand if str(i) in pxs])))
            E["manifest"] = man
            out["E_stock_pixai"] = E
    finally:
        loc_conn.close()

    out["manifest_hashes"] = manifests

    # ---- verdict against the LIVE floor
    minP = float(floor["min_precision"])
    minR = float(floor["min_recall"])
    pool_arms = (list(out["B_veto_ceiling"].items())
                 + list(out["C_dispatch_recipe"].items())
                 + ([("E_stock_pixai", out["E_stock_pixai"])]
                    if "skipped" not in out["E_stock_pixai"] else []))
    best_key, best = None, None
    for k, d in pool_arms:
        nb = (d.get("nested") or {}).get("max_f1") or {"P": 0, "R": 0}
        if best is None or (nb["P"], nb["R"]) > (
                (best.get("nested") or {}).get("max_f1", {"P": 0, "R": 0})["P"],
                (best.get("nested") or {}).get("max_f1", {"P": 0, "R": 0})["R"]):
            best_key, best = k, d
    # min_p_deploy is part of the SAME ordered floor the registration validator
    # applies (transitions.validate_registration_metrics_floor). Leaving it out
    # of the measurement's own verdict made the two disagree: the Step 8b
    # reference read PASS on val P/R while its P_deploy was 0.60 against a 0.85
    # floor. A missing P_deploy FAILS CLOSED here, exactly as the validator
    # treats it -- never "absent therefore fine".
    minPD = float(floor.get("min_p_deploy", 0.85))
    minValPos = float(floor.get("min_val_pos_support", 20))
    minPredPos = float(floor.get("min_pred_pos_support", 20))
    # Governed if the row exists, documented fallback otherwise (see the
    # constant). Same "DB wins, code never bakes it" rule as every other floor.
    minLift = float(floor.get("min_p_deploy_lift", MIN_P_DEPLOY_LIFT_FALLBACK))

    def clears_lift(v: dict) -> bool:
        """Baseline-relative criterion (user 2026-08-02, option (b)): the head
        must beat what the base tagger already ships by minLift, WHILE holding
        the same governed recall and support floors. Precision-vs-baseline
        replaces the absolute precision terms; recall/support are unchanged --
        that guard is what stops 'improve the tag' collapsing into 'fire on
        almost nothing', since lift rises as recall falls."""
        lift = v.get("P_deploy_lift")
        return (v["R"] >= minR
                and v["tp"] >= minValPos and (v["tp"] + v["fp"]) >= minPredPos
                and lift is not None and lift >= minLift)

    def binding_lift(v: dict) -> list[str]:
        terms = []
        if v["R"] < minR:
            terms.append(f"min_recall (R={v['R']:.4f} < {minR})")
        if v["tp"] < minValPos:
            terms.append(f"min_val_pos_support (tp={v['tp']} < {minValPos:.0f})")
        if (v["tp"] + v["fp"]) < minPredPos:
            terms.append(f"min_pred_pos_support (tp+fp={v['tp'] + v['fp']} < {minPredPos:.0f})")
        lift = v.get("P_deploy_lift")
        if lift is None:
            terms.append("min_p_deploy_lift (lift MISSING -- fail-closed)")
        elif lift < minLift:
            terms.append(f"min_p_deploy_lift (lift={lift:.1f}x < {minLift:.1f}x)")
        return terms

    def clears(v: dict) -> bool:
        pd = v.get("P_deploy")
        return (v["P"] >= minP and v["R"] >= minR
                and v["tp"] >= minValPos and (v["tp"] + v["fp"]) >= minPredPos
                and pd is not None and pd >= minPD)

    def binding(v: dict) -> list[str]:
        """Which floor terms block this operating point (validator order)."""
        out_terms = []
        if v["P"] < minP:
            out_terms.append(f"min_precision (P={v['P']:.4f} < {minP})")
        if v["R"] < minR:
            out_terms.append(f"min_recall (R={v['R']:.4f} < {minR})")
        if v["tp"] < minValPos:
            out_terms.append(f"min_val_pos_support (tp={v['tp']} < {minValPos:.0f})")
        if (v["tp"] + v["fp"]) < minPredPos:
            out_terms.append(f"min_pred_pos_support (tp+fp={v['tp'] + v['fp']} "
                             f"< {minPredPos:.0f})")
        pd = v.get("P_deploy")
        if pd is None:
            out_terms.append("min_p_deploy (P_deploy MISSING -- fail-closed)")
        elif pd < minPD:
            out_terms.append(f"min_p_deploy (P_deploy={pd:.4f} < {minPD})")
        return out_terms

    passing = {k: [rule for rule, v in (d.get("nested") or {}).items() if clears(v)]
               for k, d in pool_arms}
    passing = {k: v for k, v in passing.items() if v}
    # "which floor term is binding" for every arm's closest operating point
    binding_by_arm = {}
    for k, d in pool_arms:
        cand = [(len(binding(v)), rule, binding(v))
                for rule, v in (d.get("nested") or {}).items()]
        if cand:
            cand.sort()
            binding_by_arm[k] = {"closest_rule": cand[0][1], "binding_terms": cand[0][2]}
    # ---- baseline-relative (lift) axis
    passing_lift = {k: [rule for rule, v in (d.get("nested") or {}).items()
                        if clears_lift(v)] for k, d in pool_arms}
    passing_lift = {k: v for k, v in passing_lift.items() if v}
    binding_lift_by_arm = {}
    best_lift = 0.0
    for k, d in pool_arms:
        cand = [(len(binding_lift(v)), rule, binding_lift(v), v.get("P_deploy_lift"))
                for rule, v in (d.get("nested") or {}).items()]
        if cand:
            cand.sort(key=lambda c: (c[0], -(c[3] or 0)))
            binding_lift_by_arm[k] = {"closest_rule": cand[0][1],
                                      "binding_terms": cand[0][2],
                                      "P_deploy_lift": cand[0][3]}
        for _, _r, _t, lf in cand:
            if lf and not _t:
                best_lift = max(best_lift, lf)

    # NEW CAPABILITY (user 2026-08-02: 「今まで判別できなかったタグを判別できるのは
    # 価値だ」). Being able to separate a tag the fleet could not separate before
    # is a result in its own right, independent of whether any operating point
    # clears a deployment floor -- and it must not be reported as a bare HOLD,
    # which reads as "nothing happened". Conditions: some arm is genuinely above
    # the permutation null (not chance), AND it delivers a real baseline-relative
    # improvement at a legitimate operating point (recall/support floors held).
    any_above = any(d.get("above_chance") for _, d in pool_arms)
    out["verdict"] = {
        "floor": {"min_precision": minP, "min_recall": minR, "min_p_deploy": minPD,
                  "min_val_pos_support": minValPos, "min_pred_pos_support": minPredPos,
                  "min_p_deploy_lift": minLift,
                  "min_p_deploy_lift_source": ("tagger_config" if "min_p_deploy_lift" in floor
                                               else "code fallback (owner decision pending)"),
                  # **規模の floor** (0133)。verdict["floor"] は明示列挙なので、
                  # ここに載せないと _classify に届かない — load_floor が返していても
                  # 意味が無い (2026-08-05 実測: smegma が lift 7.61x / net 170,720 で
                  # 規模 floor を満たすのに cleanup-capable のままだった)。
                  # **DB に行が無ければキー自体を出さない** — コードに既定値を
                  # 焼かないので、classify 側は None を受けて規模判定をしない。
                  **({"min_net_fp_removed": float(floor["min_net_fp_removed"])}
                     if floor.get("min_net_fp_removed") is not None else {}),
                  # 「改善が無いに等しい」境界 (4 クラス判定の「不要」側)。
                  # DB に行があれば DB が勝つ — 他の floor と同じ「コードに焼かない」規則。
                  "min_p_deploy_lift_negligible": float(
                      floor.get("min_p_deploy_lift_negligible", NEGLIGIBLE_LIFT_FALLBACK)),
                  "min_p_deploy_lift_negligible_source": (
                      "tagger_config" if "min_p_deploy_lift_negligible" in floor
                      else "code fallback (owner decision pending)")},
        "deploy_prevalence_pi": out.get("deploy_prevalence", {}).get("pi"),
        "best_arm_by_nested_maxf1_P": best_key,
        "best_nested": (best or {}).get("nested"),
        "arms_clearing_full_floor": passing,
        "binding_floor_terms_by_arm": binding_by_arm,
        "any_arm_meets_floor": bool(passing),
        # -- baseline-relative axis
        "arms_clearing_lift_floor": passing_lift,
        "binding_lift_terms_by_arm": binding_lift_by_arm,
        "any_arm_meets_lift_floor": bool(passing_lift),
        "best_lift_at_legitimate_operating_point": best_lift or None,
        "capability_gain": bool(any_above and passing_lift),
        "verdict_absolute": "PASS" if passing else "HOLD",
        "verdict_lift": "PASS" if passing_lift else "HOLD",
        # kept for continuity with the round6-8 lineage: the val-only view that
        # ignores P_deploy. Reported, never used as the verdict.
        # ── 4 クラス判定に必要な「正規化済みの事実」 (plan rev8) ────────
        # 判定 module に verdict を掘らせない: 初版は capability_gain を
        # 「偶然を超えた」と誤読し (実際は「10 倍 floor を満たす」)、lift 5.9 倍の
        # smegma に「判別できない」と正反対の助言を出した。**意味の分かっている
        # ここで数値を確定させ、外へは平らな形で渡す**。
        "any_arm_above_chance": bool(any_above),
        # 正当な動作点 = recall/支持数 floor を保つ点。lift floor は**含めない**
        # (配備可否の policy dial であって、点が意味を持つかとは別軸)。
        "legitimate_operating_points": legitimate_operating_points(
            pool_arms, min_recall=minR, min_val_pos=minValPos,
            min_pred_pos=minPredPos),
        "any_arm_meets_val_pr_only": any(
            v["P"] >= minP and v["R"] >= minR
            for _, d in pool_arms for v in (d.get("nested") or {}).values()),
    }

    # ── 4 クラス判定 (不要/配備可/掃除可/保留) — plan rev8 ──────────────────
    # PASS/HOLD の 2 値では判定が逆転する (dark_anus は floor 通過だが改善
    # 1.05 倍 = 無価値、smegma は floor 未達だが 5.9 倍の分離能)。判定は
    # **配備母集団のスケール**で行う (INV-R12)。
    out["classification"] = _classify(out).as_dict()
    print("\n=== verdict ===\n" + json.dumps(out["verdict"], indent=1, default=float),
          flush=True)
    print(f"=== classification: {out['classification']['verdict']} — "
          f"{out['classification']['why']}", flush=True)

    # ── 配備できる head を実際に作る ────────────────────────────────────
    #
    # **測っただけで終わらせない。** ここまでの工程は「head を作ればこれだけ
    # 良くなる」を計算するが、2026-08-05 まで **head 本体は 1 度も保存されて
    # いなかった**。登録の artifact_sha256 には測定 JSON の hash が入り、
    # 昇格した patch 行は load できるものを指していない (arm A が毎回
    # 「no deployed row at .../<tag>/v1.safetensors」と言っていたのがそれ)。
    #
    # 実害: monochrome は corpus 601,069 件に付いていて in-gate 精度
    # π=0.031 (= 96.9% が誤検出)。lift 31 倍の head が「配備可」と判定されて
    # いたのに、**適用できる形が存在しないので誤検出が再生産され続けていた**。
    #
    # 配備するのは判定が選んだ動作点の腕。feat1024 空間の腕だけがここで
    # 保存できる (推論側 arm A / veto head が読む形が生特徴 @ w + b のため)。
    _save_deployable_head(out, tag, spaces, ig_pos, ig_neg, seed,
                          aug_pos=corp_pre, aug_neg=rand[:400])

    if out_path:
        Path(out_path).write_text(json.dumps(out, indent=1, default=float))
        print(f"wrote {out_path}", flush=True)
    return out


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="v-next tagger measurement (INV-R9 single implementation)")
    ap.add_argument("tag")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--out", default=None)
    ap.add_argument("--n-perm", type=int, default=N_PERM_DEFAULT)
    ap.add_argument("--dedicated-snapshot", help="immutable human-only snapshot to measure")
    ap.add_argument("--prepare-dedicated", help="capture canonical snapshot here; CPU/read-only")
    ap.add_argument("--heldout-positive", type=int, default=300)
    ap.add_argument("--heldout-negative", type=int, default=1000)
    ap.add_argument("--precision-n", type=int, default=300)
    ap.add_argument("--precision-from", help="dedicated measurement whose frozen head to sample")
    ap.add_argument("--evaluate-precision", help="immutable production precision sample")
    ap.add_argument("--human-reviews", help="explicit human reviews JSON bound to sample hash")
    args = ap.parse_args()
    from tagger import dedicated_protocol as dedicated
    stages = sum(bool(x) for x in (args.prepare_dedicated, args.precision_from, args.evaluate_precision))
    if stages > 1:
        ap.error("choose one dedicated stage")
    if args.prepare_dedicated:
        if args.dedicated_snapshot or args.human_reviews:
            ap.error("prepare cannot consume another snapshot/reviews")
        result = dedicated.prepare(args.tag, args.prepare_dedicated, seed=args.seed,
                                   positive=args.heldout_positive, negative=args.heldout_negative,
                                   precision_n=args.precision_n)
        print(json.dumps({k: result[k] for k in ("sha256", "status", "counts_by_provenance")}))
        return 0
    if args.precision_from:
        if not args.dedicated_snapshot or not args.out:
            ap.error("--precision-from requires --dedicated-snapshot and --out")
        result = dedicated.precision_sample(args.tag, args.dedicated_snapshot, args.precision_from, args.out)
        print(json.dumps({"sha256": result["sha256"], "status": result["status"]}))
        return 0 if result["status"] == "awaiting_human_review" else 75
    if args.evaluate_precision:
        if not args.human_reviews or not args.out:
            ap.error("--evaluate-precision requires --human-reviews and --out")
        dedicated.precision_evaluate(args.evaluate_precision, args.human_reviews, args.out)
        return 0
    if args.human_reviews:
        ap.error("--human-reviews requires --evaluate-precision")
    if args.dedicated_snapshot:
        if not args.out:
            ap.error("dedicated measurement requires an explicit new --out")
        measure_tag(args.tag, out_path=args.out, dedicated_snapshot=args.dedicated_snapshot)
        return 0
    out = args.out or str(CACHE_DIR / f"measure_{args.tag}_{args.seed}.json")
    measure_tag(args.tag, seed=args.seed, out_path=out, n_perm=args.n_perm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
