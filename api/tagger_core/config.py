#!/usr/bin/env python3
# Description: loads/writes tagger_config -- dashboard-editable ML judgment thresholds
# Kind: library
# Effects: read (always) + write (governed, via tagger_governed_set_config)
# Runs-on: primary
"""api/tagger_core/config.py -- ML judgment thresholds that used to be hardcoded
Python constants in transitions.py/reconciler.py/health.py (MIN_HELD_OUT_
SAMPLES, MIN_PRECISION, MIN_RECALL, MIN_VAL_POS_SUPPORT,
MIN_PRED_POS_SUPPORT, MIN_GT_LABELS, REGRESSION_ACCURACY_THRESHOLD,
REGRESSION_MIN_GT_LABELS), now rows in the governed table tagger_config
(migration 0098). User directive 2026-07-20: "それらはマジックナンバー
にせず、変数としてダッシュボードからコントロールできるようにしてくれ"
followed by "その他の値についてもだ" -- every ML-judgment threshold, not
just the two denominator ones first asked about.

STALE_LEASE_GRACE_SECONDS (health.py) is deliberately NOT here -- that's
an ops/lease-timing knob, not an ML quality judgment, a different
category of "value" than what was asked for.

DEFAULTS below are a fallback ONLY (missing row, or DB unreachable at
import time in some ad-hoc script) -- they exist so a caller never
crashes outright on a missing key, not as the source of truth. The
seeded tagger_config rows (same values, migration 0098) are the real
source of truth once applied; the dashboard writes there, not here.
"""
from __future__ import annotations

# NOTE: sqlalchemy is imported lazily inside load_config/set_config, NOT at module
# level. This module is shared by two runtimes with different dependency sets: the
# kura-api venv (has sqlalchemy) and the kura-tagger venv (does not -- it talks to
# PG through asyncpg). The tagger worker only ever wants the QUARANTINE_TAGS
# constant, but a module-level `from sqlalchemy import text` made that import drag
# in a DB library the worker never uses, and kura-tagger@*.service crash-looped on
# ModuleNotFoundError from 2026-07-20 (commit 653ec0a38e) until 2026-07-27 -- with
# Restart=always masking it as "running". Keep DB imports inside the DB functions
# so importing a constant stays free.

# Calibrated release-row operating point shared by full and WD14 workers.
# Keep this import free of full-pipeline inference dependencies (e.g. PyAV).
WD14_FIXED_THRESH = 0.5

DEFAULTS: dict[str, float] = {
    "min_held_out_samples": 20,
    "min_precision": 0.85,
    "min_recall": 0.70,
    # reconciler の学習 dispatch は per-tick 有界にする (sweep が 5000件/15分で
    # 有界なのと同じ流儀)。2026-09-04 の character trainable 化 + プール GT 種付け
    # で open candidate が一度に約 90 増え、無制限だと 1 tick で全て trainer へ
    # 流れる — 溢れず毎 tick この数だけ進み、残りは candidate のまま次 tick へ。
    "max_training_dispatch_per_cycle": 6,
    # Frame-corrected deploy precision floor (round8, plan rev2 Floor/INV-E). The
    # registration floor's val P/R is measured on the flagged pool; P_deploy =
    # πR/(πR+(1−π)FPR) is the operating precision at the tag's corpus prevalence π.
    # A head can clear val P/R yet deploy at low P_deploy when π is tiny (prevalence
    # ceiling) -- signal tagger/registration-gate-passable-by-harmful-head. Config-
    # driven (do NOT hardcode in the validator) so it migrates to the Phase-3
    # cancellation/McNemar floor without a code change.
    "min_p_deploy": 0.85,
    # Baseline-relative counterpart of min_p_deploy (user 2026-08-02, plan
    # tagger-vnext-deterministic-runner Step 8b follow-up): P_deploy / pi --
    # how many times more precise a head is than what the base tagger already
    # ships. An ABSOLUTE precision floor is unreachable for an over-detected tag
    # whose prevalence pi is tiny (licking_testicle pi=0.042: min_p_deploy=0.85
    # demands FPR<=0.54%, the best honest operating point is 2.0%), even when the
    # head suppresses 98% of the false positives. Only evaluated where min_recall
    # and the support floors still hold -- lift rises as recall falls, so without
    # that guard "improve the tag" degenerates into "stop detecting it". Read by
    # tagger/vnext_arms.py; governed row seeded by migration 0125. Listing it here
    # is also what lets set_config accept it (that function refuses unknown keys).
    "min_p_deploy_lift": 10.0,
    # **規模の floor** (user 2026-08-05「バンドエイドはしない。決定論的に修正が
    # 回るようにせよ」)。min_p_deploy_lift は **率** しか見ないので、規模の違う
    # 効果を区別できない。実測 2026-08-05:
    #   smegma         lift 5.76x / 差し引き **163,583 件** 減 → 配備可のはず
    #   chastity_cage  lift 8.07x / 差し引き      358 件 減 → lift は高いが効果は小
    # lift だけで判定すると前者が落ちて後者が残りかける。net_fp_removed_deploy は
    # 増える誤検出も勘定に入れた**差し引き**なので、これを floor に据えれば
    # 「率は届かないが規模で明らかに得」を機械が決められる。
    #
    # 判定は **率 OR 規模** — どちらか一方を満たせば絶対 floor 通過とする。
    # ただし規模経路にも品質の下限 (min_p_deploy_lift_negligible、既定 1.25 倍) は
    # 効く。「ほぼ改善しないが母集団が巨大なので net だけ大きい」を通さないため。
    #
    # 既定値は観測 3 件の間 (358 と 43,658) に置いた。**これは policy なので
    # config 行で調整する** — コードを書き換えずに owner が動かせる。
    "min_net_fp_removed": 10000.0,
    "min_val_pos_support": 100,
    "min_pred_pos_support": 100,
    "min_gt_labels": 10,
    "min_gt_positive": 20,
    "min_gt_negative": 50,
    "regression_accuracy_threshold": 0.80,
    "regression_min_gt_labels": 10,
    "eval_split_mode": 1,  # 1=pooled out-of-fold, 0=legacy 20% holdout (migration 0106)
}

# Tags whose WD14 output is DISCARDED in production (infer.py pops them from the
# score dict; nodec_autotag_worker excludes them from build_tags) because the
# frozen tagger is unreliable for them: mosaic_censoring nearly all-wrong
# (daioo/mic3/yourojisan), bar_censor 30/30 false positives on new (pool
# 6335/6336), condom_on_penis grouped with the same censor over-detection.
# Canonical single source (was duplicated in infer.py + nodec_autotag_worker.py);
# lives here in the light config module so the reconciler can import it WITHOUT
# pulling infer.py's onnxruntime. A precision/veto head for a quarantined tag is
# structurally INERT (it cannot change discarded output), so the reconciler must
# not dispatch training for these until the quarantine is lifted (remove the tag
# here + rebuild a reliable head first).
#
# SoT の裁定 (issue #55 §5-6, 2026-09-04): DB 側 `tag_quarantine` (0068) は
# 「これを SoT にする」と宣言したまま cut-over されず、active を誰も読まない
# dead state だった。完全 cut-over は worker 側 (infer.py / nodec — DB 直読不可)
# への配信機構が要るため保留し、**この定数が実効 SoT のまま**とする。代わりに
# tests/test_quarantine_sot_sync.py が DB の active 集合とこの定数の一致を強制
# する — 片方だけ変えると CI が落ちる (黙った分裂 SoT を作らない)。DB 行は
# reason/evidence_pools の provenance とダッシュボード表示を担う。
QUARANTINE_TAGS: frozenset[str] = frozenset({"condom_on_penis", "mosaic_censoring", "bar_censor"})

# ---------------------------------------------------------------------------
# Non-trainable tags (plan tagger-vnext-deterministic-runner Step 5 / E-M2).
#
# A tagger head is a VISUAL binary classifier; a tag whose truth is not a
# property of the pixels cannot be trained and only burns the single training
# node to a HOLD registration. Two exclusion axes, category-primary with a
# literal auxiliary (meta exists as tag_category id 41, but workflow markers
# like 'tagger-exclude' sit in 'general'):
#
#   - NON_TRAINABLE_TAG_CATEGORIES: identity/provenance categories. artist /
#     copyright name WHO made -- knowable only from provenance, not from one
#     image (the candidate list mixed in 'Sakura🔞'). meta is workflow
#     bookkeeping by definition.
#
#     **character は 2026-09-04 に外した** (user 裁定「開いて」)。当初は
#     「WHO appears も provenance 由来」として artist と同じ籠に入れていたが、
#     過剰一般化だった: キャラは絵に写っている視覚概念そのもので、pixai の
#     char head が画像だけから ellen_joe を score 1.0 で当てるのがその証明。
#     copyright は据え置き (作品帰属はキャラ・絵柄からの推論であって単独の
#     視覚概念ではない — キャラ head が育てば implication で導ける)。
#   - NON_TRAINABLE_TAGS: literal workflow markers that live in a trainable
#     category ('tagger-exclude' is 'general' with corpus 32).
#
# is_non_trainable() is the ONE predicate all candidate entry points share
# (pools.py::set_pool_verify_status, gt_sync._detect_global_candidates,
# reconciler's candidate loop) plus the one-shot triage script
# (tagger/vnext_triage.py). Keep it pure (no DB) -- callers look up the
# category themselves.
NON_TRAINABLE_TAGS: frozenset[str] = frozenset({"tagger-exclude"})
NON_TRAINABLE_TAG_CATEGORIES: frozenset[str] = frozenset({"meta", "artist", "copyright"})


def is_non_trainable(tag: str, category: str | None) -> bool:
    """True if this tag must never become / stay a training candidate.

    `category` is the tag's tag_category NAME (or None when the tag has no
    tag row yet / lookup failed): an unknown category deliberately does NOT
    exclude on its own -- only the literal list can, so a lookup failure
    degrades to the old behaviour instead of silently dropping candidates.
    """
    if tag in NON_TRAINABLE_TAGS:
        return True
    return category is not None and category in NON_TRAINABLE_TAG_CATEGORIES


async def load_config(db) -> dict[str, float]:
    """Returns a dict of every known key -> current value. Any key missing
    from the table (shouldn't happen post-migration, but a fresh checkout
    that hasn't migrated yet is a real case) falls back to DEFAULTS rather
    than raising -- these are quality thresholds, not authorization gates,
    so a stale/missing config value degrading to a hardcoded default is
    the safe failure mode, not a security hole."""
    from sqlalchemy import text
    rows = (await db.execute(text("SELECT key, value FROM tagger_config"))).all()
    live = {r.key: float(r.value) for r in rows}
    return {**DEFAULTS, **live}


async def set_config(db, key: str, value: float, updated_by: str) -> None:
    """Governed write -- tagger_governed_set_config only UPDATEs an
    existing key, it refuses to create new ones (see migration 0098's
    function body), so a typo'd key fails loudly instead of silently
    adding a dead row nothing reads."""
    if key not in DEFAULTS:
        raise ValueError(f"set_config: unknown key {key!r} -- not one of {sorted(DEFAULTS)}")
    from sqlalchemy import text
    await db.execute(
        text("SELECT tagger_governed_set_config(:key, :value, :updated_by)"),
        {"key": key, "value": value, "updated_by": updated_by},
    )
    await db.commit()
