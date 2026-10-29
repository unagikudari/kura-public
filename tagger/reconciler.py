#!/usr/bin/env python3
# Description: auto-dispatches training work for open tagger_head_candidate rows to trainer
# Kind: tool
# Effects: mutating (kura DB lifecycle_event rows) + dispatches broker tasks (external side effect)
# Runs-on: primary
"""tagger/reconciler.py — closes the "detected weak point -> triggers
training" gap (plan:kura-tagger-lifecycle-redesign, user directive
2026-07-20: この plan の原案の目標そのもの、人手を一切介さずタガーが
進歩する仕組み).

Problem this fixes: tagger_head_candidate already auto-detects tags the
tagger is weak on (pool verify-status flip -> repeated human correction
-> automatic candidate row, api/routers/pools.py). But nothing ever acted
on those rows -- 15 sat open, untouched, for hours, because closing the
loop (get someone/something to actually train a fix) was never wired up.
The tagger was structurally incapable of improving itself even though the
"this needs improvement" signal was already flowing correctly.

What this script does, each run:
  1. Reads every OPEN head candidate (status='candidate'), deduped by tag
     (the same tag can get flagged from multiple pools -- one training
     request per tag, not one per pool).
  2. For each tag whose tagger_lifecycle_event history shows it has never
     been dispatched for training (latest stage is NULL or 'candidate'),
     dispatches a broker task to trainer (the fleet's one training-
     capable, CUDA node) asking it to train/improve that tag, with the
     aggregated reasons/pools as context.
  3. Records a lifecycle transition (candidate -> training, seeding
     NULL -> candidate first if the tag has no prior ledger history at
     all) so the SAME candidate is never dispatched twice.

What this does NOT do (deliberately, same reasoning as Step 13/14-15):
  - It does not train anything itself -- there is no training code in
    this repo; trainer's own training pipeline (outside this repo)
    does the actual work in response to the dispatched task.
  - It does not auto-approve or auto-publish whatever trainer produces.
    The registration-request/validate path (Step 13, migration 0086)
    already exists for trainer to submit a trained artifact back, and a
    human/agent still reviews before it becomes a real patch -- that
    review step is fast (one status-flip call), not a redesign of the
    whole approval philosophy that the earlier metrics-floor survey
    showed shouldn't be a rigid automatic gate.

This closes the trigger gap without pretending the training-quality
judgment gap is solved -- those are two different problems, and only the
first one was actually a "structurally nothing happens" bug.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from api.db.session import async_session
from api.tagger_core.transitions import (
    DB_HOLD_REASON_CLASSES,
    GT_GROWTH_RELEASABLE,
    classify_registration_floor_failure,
)
from api.tagger_core.config import QUARANTINE_TAGS, is_non_trainable, load_config
from api.tagger_core.transitions import TransitionError, acquire_lease, record_lifecycle_event, release_lease

BROKER_DISPATCH_CLI = str(Path.home() / "broker" / "scripts" / "broker_dispatch_task.py")
# Training must go to the SPECIFIC trainer agent that has full env access (kura
# DB + filesystem writes + network to POST registration). Dispatching to the bare
# host let the broker route to whichever trainer agent was 'ready' -- and the
# 2026-07-23 8-tag burst all landed on trainer-CODEX-primary, a read-only/network-
# restricted sandbox that cannot connect to the DB, write an artifact, or POST a
# registration, so it correctly refused all 8 (rather than fabricate). Those tags
# then sat stuck in stage=training (see _reap_stale_training). trainer-cc-primary
# is the training-capable agent. Pinning it (target_agent_id) also means the
# no-allow-stale preflight fails CLOSED if it isn't live -- retried next tick --
# instead of misrouting to a sandboxed sibling.
# Phase 1 cutover (2026-08-03、plan tagger-vnext-deterministic-runner rev6 の
# E-H1): 訓練 dispatch の宛先を LLM 窓から決定論 runner へ切り替える。切替を
# Phase 3 に置くと「shadow が始まらない」(dispatch は完全一致なので runner に
# 1 件も届かない) ため、Phase 1 の開始条件がこの切替そのもの。runner 側は
# --no-registration で走り、判定は出すが登録はしない = 本番経路の shadow。
# LLM fallback は Phase 1 では常時有効のまま (Phase 3 で stale 時限定に絞る)。
TRAINING_TARGET_AGENT = "trainer-taggerrunner-primary"
# Host/executor derived from the agent id ({host}-{executor}-{n} convention) so a
# target switch (plan tagger-vnext-deterministic-runner Phase 1: -> taggerrunner)
# updates all three in one edit. Passing BOTH to the dispatch CLI is plan Step 4:
# --host alone still lets find_ready_agent fall through to trainer's sandboxed
# codex/gemini workers when the executor filter is NULL (dispatch.py:300-303);
# --host + --executor makes the pinned agent the only candidate, so the broker's
# dead-target reroute takes its reroute-skipped path instead of spraying the
# task cross-host (target_host NULL fail-open, plan explicit-target-reroute-
# fails-open).
TRAINING_TARGET_HOST, TRAINING_TARGET_EXECUTOR = TRAINING_TARGET_AGENT.split("-", 2)[:2]
# A tag dispatched to training but never advanced (no approved registration) is
# stuck: auto_review.py only moves training->approved on an APPROVED registration,
# so a refused/failed/held/rejected attempt leaves the tag in 'training' forever
# (measured 2026-07-23: ~40 tags stuck, oldest 60h). _reap_stale_training rolls a
# stale one back to 'candidate' (a legal rollback edge, migration 0077) so it can
# re-dispatch to the right agent. Bounded per-tag so a tag trainer legitimately
# HOLDS (e.g. a rename artifact) can't loop forever -- after the cap it is left for
# the dismissal flow (primary-cc-primary's domain). ops/timing knobs, NOT ML-quality
# thresholds, so they stay module constants (same call as STALE_LEASE_GRACE, see
# api/tagger_core/config.py docstring), not tagger_config rows.
STALE_TRAINING_MINUTES = 60      # real cc-primary training round ~23min; 60 = >2.5x margin
# 2026-08-03: 2 -> 8。この上限は **LLM 時代の throttle** だった (1 回 23 分の
# 対話セッションで訓練窓を溢れさせない)。Phase 1 で消費者が決定論 runner に
# 変わり、実測で「GPU 0% / 16 タグが 24h 窓の空きを待って停止」という、資源を
# 遊ばせるだけの状態になった (user 指摘 2026-08-03「時間だけでなく使用率も見ろ /
# 待っている間、一番大切な時間という予算を失っている」)。無限ループ防止は
# 既に (a) GT 成長ゲート (GT が動いていなければ再 dispatch しない) (b) runner の
# attempt ledger と dead-letter (c) MAX_REOPENS_PER_RUN の per-tick 平滑化 が
# 担っており、この定数はその 4 重目。runner の 1 回のコストは可観測かつ小さい
# (測定 1 タグ ~30 分・GPU gate 経由) ので、窓を広げる側に倒す。
MAX_TRAINING_REOPENS = 8         # per tag per 24h -- caps held/untrainable tag looping
MAX_REOPENS_PER_RUN = 8          # smooth the backlog drain across ticks, don't flood one agent
# Per-tick snapshot of the reconciler's decision for EVERY open head_candidate
# tag (dispatch vs hold + why), written atomically each run. The dashboard reads
# it to show the true automation disposition instead of re-deriving the gate (no
# drift) -- same single-source-of-truth pattern as tagger/reap_authorized.json.
DISPOSITION_PATH = Path(__file__).resolve().parent / "reconciler_disposition.json"
# min_gt_labels: below this (positive+negative) total, don't dispatch at
# all -- matches trainer's own refusal reasoning for chastity_cage (task
# c53b31bc): a training request with too little labeled data is exactly
# the kind of thing a fail-closed gate should refuse before it ever
# reaches the training node, not rely on the training node to notice and
# decline. Dashboard-editable via tagger_config (migration 0098, tagger/
# config.py) -- was a hardcoded constant here, moved per user directive
# 2026-07-20 (マジックナンバーにせず...).


async def _open_candidates_by_tag(db) -> dict[str, list[dict]]:
    rows = (await db.execute(text(
        "SELECT tag, source_pool_id, reason, added_at FROM tagger_head_candidate "
        "WHERE status = 'candidate' ORDER BY tag, added_at"
    ))).mappings().all()
    by_tag: dict[str, list[dict]] = {}
    for r in rows:
        by_tag.setdefault(r["tag"], []).append(dict(r))
    return by_tag


async def _tag_category(db, tag: str) -> str | None:
    """The tag's tag_category NAME, or None when the tag has no tag row.
    Input to the shared non-trainable predicate (config.is_non_trainable);
    None deliberately does not exclude on its own."""
    return (await db.execute(text(
        "SELECT tc.name FROM tag_name tn "
        "JOIN tag tg ON tg.id = tn.tag_id "
        "JOIN tag_category tc ON tc.id = tg.category_id "
        "WHERE tn.name = :tag ORDER BY tn.ord LIMIT 1"
    ), {"tag": tag})).scalar_one_or_none()


async def _latest_stage(db, tag: str) -> str | None:
    row = (await db.execute(text(
        "SELECT to_stage FROM tagger_lifecycle_event WHERE tag = :tag "
        "ORDER BY occurred_at DESC, id DESC LIMIT 1"
    ), {"tag": tag})).one_or_none()
    return row.to_stage if row else None


async def _has_approved_patch(db, tag: str) -> bool:
    """True if tag already has an approved tagger_patch row.

    Found live by trainer-cc-primary (2026-07-20, task c53b31bc): 7 of
    this session's first 11 real dispatches were for tags that were
    ALREADY approved, strong patches (e.g. chastity_cage v2, P=0.94/
    R=0.90, LOO AUC 0.988) -- the reconciler was misreading "no
    lifecycle_event for this tag" as "never trained", when the real
    reason was that these patches predate migration 0077's ledger and
    were approved through the old git-file workflow, which never wrote
    to tagger_lifecycle_event at all. That is a ledger PROVENANCE gap
    (the same thing tagger-health's "patch approved but no
    lifecycle_event recorded" signal already flags), not a "this tag
    needs retraining" signal.

    NOTE (2026-07-21, user correction): this used to be an unconditional
    "already approved -> never touch again" skip, deferred to a human
    forever regardless of how much new GT piled up. That contradicted the
    whole point of setting thresholds in the first place ("閾値を設定して
    いるんだからかけてよ") -- anilingus went through 6 rounds of manual
    v-next dispatch by hand (GT growing 44->67->100 positive across
    verify-pool rounds) because the automation refused to ever revisit an
    approved tag on its own. Callers now still call this to decide "is
    there a strong baseline to beat", but gate the ACTUAL skip on GT
    growth since the last registration attempt (see _last_registration_
    positive) rather than skipping unconditionally -- an approved patch
    with real new GT is exactly as dispatch-worthy as a fresh candidate,
    just measured against a different baseline (the last attempt, not
    zero).
    """
    row = (await db.execute(text(
        "SELECT 1 FROM tagger_patch WHERE tag = :tag AND status = 'approved' LIMIT 1"
    ), {"tag": tag})).one_or_none()
    return row is not None


async def _last_registration_positive(db, tag: str) -> int | None:
    """Positive-GT count recorded on the tag's most recent registration_request
    (any status -- approved, rejected, whatever), or None if it has never had
    one. Used as the baseline a v-next retry's fresh GT must meaningfully beat
    (see _has_approved_patch's growth-gate) -- without this, a tag that already
    cleared the floor once would silently never be worth revisiting even after
    real new correction data piles up, and a tag rejected for insufficient GT
    would get redispatched every single cycle it stays candidate, spamming the
    training node with identical retries."""
    row = (await db.execute(text(
        "SELECT gt->>'positive' AS pos FROM tagger_registration_request "
        "WHERE tag = :tag ORDER BY requested_at DESC LIMIT 1"
    ), {"tag": tag})).one_or_none()
    if row is None or row.pos is None:
        return None
    return int(row.pos)


async def _last_registration_negative(db, tag: str) -> int | None:
    """Negative-GT count recorded on the tag's most recent registration_request,
    or None if it has never had one (or the row predates negative bookkeeping in
    the gt JSON). The baseline a PRECISION v-next measures new FP corrections
    against -- an already-approved tag that keeps over-detecting accumulates
    negative GT (human deletions -> tagger_gt_label negatives via gt_sync's
    _sync_global_negatives) without any new positives, so the positive-growth
    gate alone would never revisit it (user 2026-07-23: '誤検知を10000件報告しても
    改善される仕組みがない'). This is the negative-side analogue of
    _last_registration_positive."""
    row = (await db.execute(text(
        "SELECT gt->>'negative' AS neg FROM tagger_registration_request "
        "WHERE tag = :tag ORDER BY requested_at DESC LIMIT 1"
    ), {"tag": tag})).one_or_none()
    if row is None or row.neg is None:
        return None
    return int(row.neg)


async def _last_registration_hold(db, tag: str, config: dict) -> tuple[str, dict] | None:
    """(hold_reason_class, row) if the tag's most recent registration_request was
    REJECTED and the rejection is one a hold should absorb, else None.

    Such a tag would otherwise cycle training<->candidate forever, so the
    reconciler HOLDS it (round8, plan rev2 §held) instead of re-dispatching --
    unless GT has grown since, which _release_held detects and re-admits.

    The class comes from classify_registration_floor_failure reading the STORED
    METRICS, not from substring-matching the rendered rejected_reason. The old
    predicate here tested `"P_deploy" in rejected_reason` and, measured on primary
    2026-07-27 (trainer round6, task 9d0c9190), matched 0 of 27 rejected rows:
    the P_deploy checks are LAST in the ordered floor, so every candidate that
    trips an earlier one (P/R below floor, denominator too small, no val
    breakdown -- i.e. every real respawn regular) carries prose without the word.
    The auto-hold had therefore never fired once since going live.

    Returns None -- i.e. re-dispatch rather than hold -- when the rejection was a
    RESUBMIT (a required metrics field was omitted): that is a submitter protocol
    gap whose remedy is a corrected registration, and holding on it would strand
    the candidate on something no amount of labelling fixes.
    """
    row = (await db.execute(text(
        "SELECT status, rejected_reason, review_class, metrics, gt "
        "FROM tagger_registration_request "
        "WHERE tag = :tag ORDER BY requested_at DESC, id DESC LIMIT 1"
    ), {"tag": tag})).mappings().one_or_none()
    if not row or row["status"] != "rejected":
        return None
    reason_class = _hold_class_from_registration(
        row["review_class"], row["metrics"], config)
    # None => not a hold (not_needed / owner_decision / floor cleared / RESUBMIT).
    # Membership test (not `!= RESUBMIT`) so an unmappable value can never
    # reach migration 0110's hold_reason_class CHECK.
    if reason_class not in DB_HOLD_REASON_CLASSES:
        return None
    return reason_class, dict(row)


def _hold_class_from_registration(review_class: str | None, metrics, config):
    """rejected な直近 registration から hold class を導く (#55: 0154 の
    review_class が第一ソース)。

    旧実装は metrics["val"] 前提の classify_registration_floor_failure だけで、
    新形式 (disposition/classification) の行 1007/1010 に RESUBMIT を返し、
    **auto-hold (respawn ダンパー) が一度も発火しない dead 状態**だった
    (census #55 §2 実測 2026-09-04)。review_class は auto_review が書く型付き
    判定なので prose にも旧 metrics 形式にも依存しない。

      unmeasurable   -> insufficient_gt (runner の hold = GT が CV に足りない —
                        auto_review._hold_reason の insufficient_gt_for_cv と同義)
      not_needed /
      owner_decision -> None (hold ではない: 前者は baseline 済みで成長ゲートが
                        受け持つ、後者は人間の決裁待ち)
      floor_rejected /
      NULL (旧世代)  -> 従来どおり structured metrics の floor 分類へ
    """
    if review_class == "unmeasurable":
        return "insufficient_gt"
    if review_class in ("not_needed", "owner_decision"):
        return None
    return classify_registration_floor_failure(metrics, config)


async def _release_held(db, min_pos: int, min_neg: int) -> list[str]:
    """Auto-release held candidates whose GT has GROWN since the hold, for the
    hold classes where growth is actually new signal (plan rev2 §held release
    trigger (a) + F3 "release はクラスから導出(OR手書きしない)" -- the releasable
    set is the declared GT_GROWTH_RELEASABLE, not a hand-written class literal).

    This used to read `hold_reason_class='prevalence_ceiling'` only. That was
    wrong in both directions once the hold predicate above started firing:
    insufficient_gt holds (now the common case) would have been stranded forever
    even though +GT is precisely their release condition, while the dashboard
    promised every held row "GT が伸びれば自動 release". The INV-D classes
    (vocab_absent_routing, inert_serving_path) stay human-release-only, which is
    the correct behaviour for cum_in_cup -- a routing gap is not solved by labels.

    Runs BEFORE the main loop so a released tag is re-evaluated this same tick
    (and re-held if still capped). Growth only -- swaps/retractions don't release
    (they leave the fingerprint's set size). Auto re-evaluation on +GT is allowed
    (trainer Q4); a human release/dismiss is a separate governed path. Returns
    released tags."""
    rows = (await db.execute(text(
        "SELECT tag, hold_evidence FROM tagger_head_candidate "
        "WHERE status='held' AND hold_reason_class = ANY(:classes)"
    ), {"classes": sorted(GT_GROWTH_RELEASABLE)})).mappings().all()
    released, release_noop = [], []
    for r in rows:
        hist = ((r["hold_evidence"] or {}).get("history") or [])
        if not hist:
            continue
        base = hist[-1]  # most recent hold measurement
        base_pos, base_neg = int(base.get("gt_pos") or 0), int(base.get("gt_neg") or 0)
        gt_pos, gt_neg = await _gt_counts(db, r["tag"])
        if _gt_grew(base_pos, base_neg, gt_pos, gt_neg, min_pos, min_neg):
            # Both governed fns RETURN the UPDATE's row count (0110/0111,
            # GET DIAGNOSTICS ... ROW_COUNT). Discarding it lets a no-op report as
            # success: each fn's UPDATE is scoped by status, so a row that moved
            # underneath us (concurrent tick, or a governed call from another node)
            # matches nothing and the tag is still announced as released. Check it --
            # a claim in the run summary should mean rows actually changed.
            n = (await db.execute(text(
                "SELECT tagger_governed_release_head_candidate(:tag, :reason)"
            ), {"tag": r["tag"], "reason": f"GT grew (pos {base_pos}->{gt_pos} / neg {base_neg}->{gt_neg}) -- re-evaluate"})).scalar()
            await db.commit()
            (released if n else release_noop).append(r["tag"])
    return released, release_noop


def _gt_grew(base_pos: int, base_neg: int, now_pos: int, now_neg: int,
             min_pos: int, min_neg: int) -> bool:
    """Is there enough NEW labelled signal since the baseline to spend a training
    run on? Two conditions, both required:

      1. the correction SET got bigger (not merely relabelled), and
      2. one class grew by at least the batch the dispatch gate already demands
         (min_gt_positive for a recall retrain, min_gt_negative for a precision
         one).

    (2) exists because release used to fire on a SINGLE new label. The
    established-tag dispatch gate further down this module has always required
    `gt_pos >= last_pos + min_gt_positive` (or the negative analogue) before
    spending a training run -- but the hold/release path sits at the TOP of the
    predicate, deliberately outside that block (round8, trainer N1), and so
    bypassed the magnitude entirely. One label was enough to release a held tag,
    re-dispatch it, have it rejected on a training set that had barely moved, and
    re-hold it. Two rules for the same question, and the weaker one governed the
    loop the hold exists to damp.

    Raised by trainer (task ca47c3f6 reply) as "a wrongly-releasable class has
    no self-stopping side": a genuinely capped tag comes back to re-measure its
    ceiling every time GT twitches. That is real, but the cause is the magnitude,
    not the class -- the floor result cannot distinguish "capped" from
    "under-trained" (both surface as P/R below floor with adequate denominators),
    so encoding never-releasable there would trade churn for permanent stalls
    that only a human can clear, which is the failure migration 0110's asymmetry
    is built to avoid. Requiring a real batch bounds the waste without guessing:
    a capped tag retries once per +20 positives / +50 negatives, not once per
    label. Where per-tag analysis DOES establish the cause, the governed hold
    path with the accurate class is the instrument (cum_in_cup ->
    vocab_absent_routing, human-release-only).

    (1) is the older contract -- _release_held has always read "Growth only --
    swaps/retractions don't release (they leave the fingerprint's set size)" --
    which the implementation did not honour: it compared the two class counts
    independently, and a flip satisfies that (relabel a positive as negative and
    gt_neg rises with no new label in existence). Observed live 2026-07-27 the
    moment auto-release started covering insufficient_gt: smegma held at 104/406
    (510 labels), released at 102/408 -- still 510, two flipped.
    """
    if not _set_grew(base_pos, base_neg, now_pos, now_neg):
        return False
    return (now_pos >= base_pos + min_pos) or (now_neg >= base_neg + min_neg)


def _set_grew(base_pos: int, base_neg: int, now_pos: int, now_neg: int) -> bool:
    """Did the correction SET get bigger since the baseline?

    Plan rev2's release trigger (a) is "a bigger correction set is new signal a
    fresh arm may clear the ceiling with", and _release_held's contract has always
    read "Growth only -- swaps/retractions don't release (they leave the
    fingerprint's set size)". The implementation, however, compared the two class
    counts independently (pos > base_pos or neg > base_neg), which a pure FLIP
    satisfies: relabel one positive as negative and gt_neg rises without a single
    new label existing.

    Observed live 2026-07-27 the moment auto-release started covering
    insufficient_gt: smegma held at 104/406 (510 labels), released at 102/408 --
    still 510 labels, two of them flipped. That is a re-dispatch to the fleet's
    single training node for a training set that did not grow, i.e. the respawn
    the hold exists to stop, re-entered through the release door.

    Comparing totals makes the code do what its contract says. A flip is not
    nothing -- it is a corrected label -- but it is not a bigger set, and the
    place to act on relabelling is the correction pipeline, not a retrain trigger.
    """
    return (now_pos + now_neg) > (base_pos + base_neg)


async def _gt_counts(db, tag: str) -> tuple[int, int]:
    """Returns (positive, negative) counts from tagger_gt_label -- the
    structured labels api/tagger_core/gt_sync.py derives from post_tag_audit
    corrections + verify-pool review (see that module's docstring). This
    is what lets a dispatch carry real evidence instead of forcing the
    training node to independently discover "there's no GT for this"
    (trainer had to do exactly that for chastity_cage, task c53b31bc)."""
    row = (await db.execute(text(
        "SELECT count(*) FILTER (WHERE verdict='positive') AS pos, "
        "count(*) FILTER (WHERE verdict='negative') AS neg "
        "FROM tagger_gt_label WHERE tag = :tag"
    ), {"tag": tag})).one()
    return row.pos, row.neg


async def _corpus_positive_count(db, tag: str) -> int:
    """Posts that CURRENTLY hold this tag (post_tag present) -- the tag's live
    positive population across the whole corpus, not just the verify pool.

    User 2026-07-23: 検知済み正例は私の学習プールではなく、そのタグを持つ全体に
    したほうがいい. For a tag ALREADY in the main tagger (an approved patch), the
    posts that still carry it -- everything except the ones a human DELETED (those
    are gone from post_tag and recorded as negative GT) -- are a valid, large
    positive set. This is what lets an over-detected tag whose humans only ever
    DELETE (few/zero verify-pool positives, thousands of FP negatives) still train
    a PRECISION retrain: positives come from the live corpus, hard negatives from
    the human deletions. Deliberately NOT used to relax the floor for a FRESH
    (not-yet-approved) candidate -- there, present!=trustworthy until a verified
    pool draws the review boundary (the 1.96.11 principle), and a tag with no
    approved patch has no reliably-applied live population to trust yet."""
    return (await db.execute(text(
        "SELECT count(*) FROM post_tag pt "
        "JOIN tag_name tn ON tn.tag_id = pt.tag_id AND tn.ord = 0 "
        "WHERE tn.name = :tag"
    ), {"tag": tag})).scalar_one()


# kura#62: default ceiling on the training runner's unclaimed queue. The runner
# processes ~2-3 tags/h (one --limit 1 tick includes feature extraction, 20+ min);
# a frequent reconcile timer dispatched up to 6/tick and outran it 4-6x, so the
# queue and the task-pending-never-claimed signal grew without bound. This is the
# depth past which we stop adding — an env override lets ops tune it without a DB
# change, and load_config's "dispatch_backlog_max" overrides that in turn.
_DEFAULT_BACKLOG_MAX = int(os.environ.get("TAGGER_DISPATCH_BACKLOG_MAX", "10"))


def _saturated(backlog: int | None, dispatched_this_tick: int, backlog_max: int) -> bool:
    """Would adding one more dispatch push the runner's unclaimed queue to or past
    the ceiling? The depth we would create is what the runner has not claimed
    (`backlog`) plus what we already sent this tick. None backlog (unreadable) is
    NOT saturated — backpressure fails open. Pure so its boundary is tested."""
    if backlog is None:
        return False
    return backlog + dispatched_this_tick >= backlog_max


def _downstream_backlog(agent_id: str) -> int | None:
    """How many tasks are queued to `agent_id` and not yet claimed.

    This is an OBSERVATION the reconciler reads before adding more — "is the
    runner already behind?" — not a decision about any one tag (#55's disposition
    ledger holds decisions; saturation is not one). Returns None when the broker
    cannot be read: backpressure then fails OPEN (dispatch as before), because a
    transient read failure must not silently halt all training. The cost of
    failing open is at worst the pre-#62 behaviour for one tick.
    """
    try:
        sys.path.insert(0, str(Path.home() / "broker" / "scripts"))
        from broker_api_client import BrokerApiClient
        client = BrokerApiClient.from_env()
        rows = client.list_tasks(status="pending", target_agent_id=agent_id)
        return len(rows) if rows is not None else None
    except Exception as e:  # noqa: BLE001
        print(f"[reconciler] backlog read failed ({e}) — backpressure fails open this tick",
              file=sys.stderr)
        return None


def _dispatch_training_request(
    tag: str, candidates: list[dict], gt_pos: int, gt_neg: int, min_gt_labels: float,
    trigger: str = "new-candidate", corpus_pos: int | None = None,
) -> bool:
    """Fire-and-forget-ish: returns True if the broker CLI reported success.
    A failed dispatch here must NOT record a training transition (the
    lifecycle ledger would then claim work is in flight that never
    actually got requested) -- caller only records the transition when
    this returns True.

    Deliberately does NOT pass --allow-stale (bug found live 2026-07-21,
    nodec-cc-primary task 2ef1f836): --allow-stale skips broker_dispatch_
    task.py's client-side live-agent preflight and lets the server's
    resolve_routing() cross-host fallback pick ANY ready cc agent when
    trainer's isn't in the freshness window at that exact tick -- it
    misrouted 16 dispatches to nodec-cc-primary, which has no GPU/kura
    repo/tagger_gt_label access and correctly bounced all of them. Without
    --allow-stale, a missed preflight just fails closed (rc!=0, no
    transition recorded) and the tag stays a candidate for the next
    15-minute reconciler tick -- self-healing instead of misrouting."""
    pools = sorted({str(c["source_pool_id"]) for c in candidates if c["source_pool_id"] is not None})
    # Broker code-edit tasks are hard-capped at 4,000 chars (task-body-oversize,
    # rc=3) — an unbounded reasons list breaks the WHOLE tick: the dispatch
    # fails, the reconciler exits 1 and auto_review never runs (smegma with 9+
    # pools, measured 2026-08-02: content_chars 4970 → unit FAILURE every tick).
    # Cap the per-pool reasons; the full candidate rows stay queryable in
    # tagger_head_candidate, so the tail carries no information the trainer
    # can't already reach with one SELECT.
    _MAX_REASONS = 6
    reasons = "\n".join(f"- pool #{c['source_pool_id']}: {c['reason']}"
                        for c in candidates[:_MAX_REASONS])
    if len(candidates) > _MAX_REASONS:
        reasons += (f"\n- (他 {len(candidates) - _MAX_REASONS} pool — 全件は "
                    f"SELECT * FROM tagger_head_candidate WHERE tag='{tag}' で参照)")
    # POSITIVE SOURCE for a v-next: gt_label positives are few for an over-
    # detected tag (humans only ever DELETE it), so a precision retrain draws
    # its positive class from the tag's LIVE CORPUS (posts still holding the
    # tag) and its hard negatives from the human deletions (negative GT). User
    # 2026-07-23: 検知済み正例は学習プールではなくそのタグを持つ全体にしたほうがいい.
    corpus_note = (
        f"\n正例源: このタグを現在保持する corpus 全体 = {corpus_pos} 件"
        f"(SELECT pt.post_id FROM post_tag pt JOIN tag_name tn ON tn.tag_id=pt.tag_id "
        f"AND tn.ord=0 WHERE tn.name='{tag}')。gt_label 正例({gt_pos}件)は検証プール由来で"
        f"少数のため、正例は corpus-present から sample し、negative GT({gt_neg}件=人手削除の"
        f"FP補正)を hard negative に使うこと。" if corpus_pos is not None else "")
    if trigger == "new-candidate":
        trigger_note = "トリガ=new-candidate: 未学習(または未 approved)の新規候補。"
    else:
        base = ("既存 approved patch の再学習(v-next)" if trigger.endswith("v-next")
                else "base WD14 タグの過検出に対する新規の抑制/精度ヘッド(approved patch 無し)")
        aspect = ("recall+precision(取りこぼし FN と過検出 FP の両方を是正)"
                  if trigger.startswith("recall+precision")
                  else "recall(取りこぼし false negative を減らす)" if trigger.startswith("recall")
                  else "precision(過検出 false positive を抑制、決定境界を negative 側に締める)")
        trigger_note = f"トリガ={trigger}: {base} — {aspect} を依頼する。"
    trigger_note += corpus_note
    content = (
        f"PLANS_CHECKED: kura-tagger-lifecycle-redesign (completed, fact 2fe6daf6) — "
        f"auto-dispatched by tagger/reconciler.py, no human triggered this specific request.\n\n"
        f"tag={tag} が head_candidate として自動検出されました({len(candidates)}件、"
        f"pool: {', '.join(pools) if pools else '(なし)'})。学習/改善の検討・実施をお願いします。\n\n"
        f"{trigger_note}\n\n"
        f"検出理由:\n{reasons}\n\n"
        f"GTラベル(tagger_gt_label、post_tag_audit訂正+検証済みpoolから自動導出): "
        f"positive={gt_pos} / negative={gt_neg}(合計{gt_pos + gt_neg}件、閾値{min_gt_labels:.0f}件以上のみdispatch)。\n"
        f"SELECT * FROM tagger_gt_label WHERE tag='{tag}' で全件参照可能。\n\n"
        f"完了したら POST /api/tagger/registration-request で登録してください "
        f"(tag/source_host/artifact_ref/artifact_sha256/metrics/gt)。"
    )
    # 最終安全弁: broker の code-edit 上限 4,000 chars (rc=3 task-body-oversize)。
    # _MAX_REASONS で通常は届かないが、単一 reason が異常長でも tick を殺さない。
    if len(content) > 3900:
        content = content[:3800] + "\n…(truncated — 全文は tagger_head_candidate を SELECT)"
    try:
        result = subprocess.run(
            [sys.executable, BROKER_DISPATCH_CLI, "--content", content, "--kind", "code-edit",
             # target the SPECIFIC training-capable agent, not the bare host --
             # a host dispatch can land on trainer's sandboxed codex/gemini
             # agents which cannot train (2026-07-23 incident, see the constant).
             "--agent", TRAINING_TARGET_AGENT,
             # Plan tagger-vnext-deterministic-runner Step 4: without --host the
             # task row's target_host stays NULL (code-edit has no KIND_DEFAULTS
             # host) and the dead-target reroute fails OPEN to an all-host
             # search; --executor is required with it or find_ready_agent falls
             # through to this host's sandboxed codex/gemini workers.
             "--host", TRAINING_TARGET_HOST, "--executor", TRAINING_TARGET_EXECUTOR,
             # **未処理の同一依頼を二度作らない。** tasks.dedup_key の UNIQUE は
             # open な status だけに掛かる部分索引なので、「まだ誰も着手して
             # いない依頼が 1 つある間は作らない、完了したらまた作れる」になる。
             #
             # 2026-08-07 実測: male_pubic_hair は tagger_head_policy 行を 1 つも
             # 持たないので `deployable=False` になり、**成長ゲートを素通りして
             # 毎 tick dispatch されていた**。同一内容の code-edit が 20 件
             # trainer の inbox に積み上がった。成長ゲートは「配備できている
             # タグ」の再測定を抑えるためのもので、配備できていないタグには
             # 掛からない — その経路に回数制限が無かった。
             #
             # 時刻でスコープしない。日バケットは「持続する状態に応答側が
             # close で再武装をかける」signal の incident 側で要るもので、
             # ここは完了した依頼を作り直してよい別の話 (broker_signal_consumer
             # と揃えるべきではない)。
             "--dedup-key", f"tagger-train:{tag}",
             "--importance", "medium",
             # Found live 2026-07-21: every timer-triggered dispatch this
             # session was silently failing (rc=2, "cannot identify the
             # calling executor") because broker_dispatch_task.py's identity
             # detection falls back to CLAUDECODE/BROKER_EXECUTOR env vars,
             # neither of which kura-tagger-auto-cycle.service's systemd
             # environment sets -- it's a plain oneshot unit, not a Claude
             # Code session. Passing --source-executor explicitly makes this
             # call self-contained instead of depending on ambient session
             # state that can silently disappear (e.g. across a systemd user
             # manager restart).
             "--source-executor", "cc",
             # 2026-08-08: この dispatch は primary-cc-primary の identity で行う
             # しかない (bootstrap の {host}-cc-* prefix gate が非対話 identity
             # の新規 mint を許さない — security_exposure_audit.py の
             # audit_agent_id と同じ制約)。そのままだと、この完全自動ループの
             # 完了 1 件ごとに、その identity を握っている対話セッション (人間
             # が今使っている pane) へ割り込み通知が飛ぶ。reconciler の完了は
             # 人間の判断を要さないので、要求元 push だけを明示的に止める
             # (get_briefing の unacked completions には引き続き残る)。
             "--tags", "notify:silent"],
            capture_output=True, text=True, timeout=30,
        )
        print(f"[reconciler] dispatch({tag}): rc={result.returncode} {result.stdout.strip()[:200]}")
        return result.returncode == 0
    except Exception as e:  # noqa: BLE001
        print(f"[reconciler] dispatch({tag}) failed: {e}", file=sys.stderr)
        return False


async def _reap_stale_training(db) -> list[str]:
    """Roll stale 'training' tags back to 'candidate' so they can re-dispatch.

    A tag advances OFF 'training' only when auto_review.py approves a registration
    (training->approved). A dispatch that was refused (sandboxed agent, see the
    routing fix), failed, held, or rejected produces NO approved registration, so
    the tag sits in 'training' forever -- measured 2026-07-23: ~40 tags stuck,
    oldest 60h. This GC finds a stale one (training transition older than
    STALE_TRAINING_MINUTES) that still has an open head_candidate and rolls it
    back to 'candidate' (a legal rollback edge, migration 0077), letting the main
    loop re-evaluate it: it either re-dispatches (now to the training-capable
    agent) or, if a rejected registration already advanced its GT baseline, the
    growth gate HOLDS it -- so a just-rejected tag does not respawn.

    Bounded so a tag trainer legitimately HOLDS (e.g. the paruru rename artifact)
    cannot loop forever: a tag reopened >= MAX_TRAINING_REOPENS times in the last
    24h is left in 'training' for the dismissal flow (primary-cc-primary's domain) to
    resolve, and at most MAX_REOPENS_PER_RUN are reopened per tick to drain the
    backlog smoothly instead of flooding the single training node."""
    rows = (await db.execute(text(
        "WITH latest_ev AS ("
        "  SELECT DISTINCT ON (tag) tag, to_stage, occurred_at "
        "  FROM tagger_lifecycle_event ORDER BY tag, occurred_at DESC, id DESC), "
        "reopens AS ("
        "  SELECT tag, count(*) AS n FROM tagger_lifecycle_event "
        "  WHERE from_stage='training' AND to_stage='candidate' "
        "    AND occurred_at > now() - interval '24 hours' GROUP BY tag) "
        "SELECT e.tag FROM latest_ev e LEFT JOIN reopens r ON r.tag = e.tag "
        "WHERE e.to_stage = 'training' "
        "  AND e.occurred_at < now() - make_interval(mins => :stale_min) "
        "  AND COALESCE(r.n, 0) < :max_reopens "
        "  AND EXISTS (SELECT 1 FROM tagger_head_candidate hc "
        "              WHERE hc.tag = e.tag AND hc.status = 'candidate') "
        "ORDER BY e.occurred_at ASC LIMIT :cap"
    ), {"stale_min": STALE_TRAINING_MINUTES, "max_reopens": MAX_TRAINING_REOPENS,
        "cap": MAX_REOPENS_PER_RUN})).all()
    reopened: list[str] = []
    for r in rows:
        lease = None
        try:
            lease = await acquire_lease(db, f"lifecycle:{r.tag}", lease_seconds=30)
            await record_lifecycle_event(
                db, tag=r.tag, from_stage="training", to_stage="candidate",
                reason=f"stale-training GC: {STALE_TRAINING_MINUTES}分経過・approved 登録なし "
                       f"→ candidate に戻し再ディスパッチ可能化(training 前進は approved 登録のみ、"
                       f"refused/failed/held/rejected は放置されるため)",
                lease=lease,
            )
            reopened.append(r.tag)
        except TransitionError as e:
            print(f"[reconciler] stale-training reopen failed for {r.tag}: {e}", file=sys.stderr)
        finally:
            if lease is not None:
                try:
                    await release_lease(db, lease)
                except TransitionError:
                    pass
    if reopened:
        print(f"[reconciler] stale-training GC reopened {len(reopened)}: {reopened}")
    return reopened


async def _reap_approved_without_head(db) -> list[str]:
    """**`approved` は「patch 行がある」ではなく「配備できる head がある」**。

    2026-08-05 実測: monochrome / greyscale は 19:05 に `approved` へ到達したが、
    その patch は **bytes を 1 byte も持たない**。測定は「head を作ればこれだけ
    良くなる」を計算しながら head 本体を保存していなかったため、登録の
    artifact_sha256 は測定 JSON の hash で、patch は load できるものを指していない。

    そして `approved` は lifecycle の終端なので、reconciler は二度と
    re-dispatch しない — runner は 5 分ごとに `no-work` と言い続ける。結果:

        patch は approved / head は存在しない / 二度と測り直されない
        → **corpus 601,069 件の monochrome (in-gate 精度 π=0.031 = 96.9% が
          誤検出) を、タガーは毎周回そのまま再生産し続ける**

    「終端に着いたが何も起きていない」を検出できる状態にしておかないと、
    **静かに効かない**まま残る。`_reap_stale_training` が training について
    やっているのと同じことを approved についてやる。

    判定は `tagger_deployable_head` view (migration 0140)。policy 行があるかでは
    なく **load できるバイトを指しているか**。policy が
    無い = 適用に必要な値 (bytes/閾値/適用モード) が揃っていない、と
    スキーマがそのまま言っている。

    再 dispatch の暴走は既存の 3 重で止まる: GT 成長ゲート /
    MAX_TRAINING_REOPENS (24h 上限) / MAX_REOPENS_PER_RUN (per-tick 平滑化)。
    """
    rows = (await db.execute(text(
        "WITH latest_ev AS ("
        "  SELECT DISTINCT ON (tag) tag, to_stage, occurred_at "
        "  FROM tagger_lifecycle_event ORDER BY tag, occurred_at DESC, id DESC), "
        # **自分が戻した回数だけ**を数える。`to_stage='candidate'` を全部
        # 数えると、通常の candidate⇄training の往復 (1 時間に 1 往復ある) が
        # そのまま上限に当たり、**この GC は永久に発火しない**。
        # 実測 2026-08-05: monochrome / greyscale はその日のうちに candidate
        # 遷移が 9 回あり、上限 8 を超えていて 1 件も戻らなかった。
        # `_reap_stale_training` が `from_stage='training'` に限定しているのと
        # 同じ理由 — 上限は「この GC の暴走」を止めるためのもので、他の遷移を
        # 数える意味は無い。
        "reopens AS ("
        "  SELECT tag, count(*) AS n FROM tagger_lifecycle_event "
        "  WHERE from_stage='approved' AND to_stage='candidate' "
        "    AND occurred_at > now() - interval '24 hours' GROUP BY tag) "
        # **不変条件で選ぶ (lifecycle の位置で選ばない)。**
        #
        # 初版は `e.to_stage='approved'` を条件にしていた。すると自分が
        # lifecycle を candidate へ戻した瞬間に条件から外れ、**head_candidate を
        # 開き直す前に対象でなくなる**。実測 2026-08-05: 20:21 に 5 件の
        # lifecycle を戻したが head_candidate は promoted のまま残り、その後
        # GC はもう拾わない — 私自身が作った新しい行き止まり。
        #
        # 守りたいのは「**approved な patch があって配備できる head が無いなら、
        # 開いた candidate 行があるはず**」という不変条件。lifecycle がどこに
        # あるかは関係ない。壊れている行を、壊れている限り拾う。
        "SELECT e.tag, e.to_stage FROM latest_ev e LEFT JOIN reopens r ON r.tag = e.tag "
        "WHERE COALESCE(r.n, 0) < :max_reopens "
        "  AND EXISTS (SELECT 1 FROM tagger_patch p "
        "              WHERE p.tag = e.tag AND p.status = 'approved') "
        # **定義は tagger_deployable_head view が唯一持つ** (migration 0140)。
        # ここに条件を書き下すと、governed 関数側の不変条件と 2 箇所に別々の
        # 定義ができ、片方だけ直したときに静かにずれる — 実際 0139 でずれた
        # (関数は「load できるバイトか」に直したのに、ここは「policy 行が
        # あるか」のままで、壊れた head を持つ 2 タグが永久に選ばれなかった)。
        "  AND NOT EXISTS (SELECT 1 FROM tagger_deployable_head d "
        "                  WHERE d.tag_id = tagger_tag_id_of(e.tag)) "
        # 開いた candidate 行が既にあるなら何もしなくてよい (二重に動かさない)。
        "  AND NOT EXISTS (SELECT 1 FROM tagger_head_candidate hc "
        "                  WHERE hc.tag = e.tag AND hc.status = 'candidate') "
        "ORDER BY e.occurred_at ASC LIMIT :cap"
    ), {"max_reopens": MAX_TRAINING_REOPENS, "cap": MAX_REOPENS_PER_RUN})).all()
    reopened: list[str] = []
    for r in rows:
        lease = None
        try:
            # lifecycle は approved のときだけ戻す。既に candidate なら
            # 動かす必要が無い (0077 に candidate→candidate の辺は無い)。
            if r.to_stage == "approved":
                lease = await acquire_lease(db, f"lifecycle:{r.tag}", lease_seconds=30)
                await record_lifecycle_event(
                    db, tag=r.tag, from_stage="approved", to_stage="candidate",
                    reason="approved だが配備できる head が無い (tagger_deployable_head に不在) "
                           "→ candidate に戻す。patch 行があることと配備できることは別で、"
                           "終端に置いたままだと二度と測り直されない",
                    lease=lease,
                )
            # **lifecycle だけ戻しても dispatch されない** (2026-08-05 20:21 実測)。
            # 主ループが読むのは `tagger_head_candidate` の open 行で、昇格時に
            # `promoted` になったままだと 1 件も拾われない。5 件戻して 0 件
            # dispatch という形でそれが出た。両方戻して初めて測り直しに入る。
            n = (await db.execute(text(
                "SELECT tagger_governed_reopen_headless_candidate(:tag, :why)"
            ), {"tag": r.tag,
                "why": "approved-without-head GC"})).scalar()
            if not n:
                # policy があるか、promoted 行が無い。**黙って成功にしない** —
                # lifecycle だけ動いた状態は「戻したのに動かない」に見える。
                print(f"[reconciler] {r.tag}: lifecycle は戻したが head_candidate を "
                      f"開き直せなかった (promoted 行なし / policy あり)", file=sys.stderr)
            await db.commit()
            reopened.append(r.tag)
        except TransitionError as e:
            print(f"[reconciler] approved-without-head reopen failed for {r.tag}: {e}",
                  file=sys.stderr)
        finally:
            if lease is not None:
                try:
                    await release_lease(db, lease)
                except TransitionError:
                    pass
    if reopened:
        print(f"[reconciler] approved-without-head GC reopened {len(reopened)}: {reopened}")
    return reopened


async def _current_dispositions(db) -> dict[str, dict]:
    """issue #55 P1: 現在の裁定 (fold = tag ごと最新行) を全タグ分読む。

    dispatch gate (loop 先頭) が読む唯一の consumer 入口。view が fold の
    正本なので、ここで ORDER BY を再発明しない。"""
    rows = (await db.execute(text(
        "SELECT tag, seq, disposition, reason_class, decided_by, actor_type, "
        "valid_until FROM tagger_candidate_disposition_current"
    ))).mappings().all()
    return {r["tag"]: dict(r) for r in rows}


async def _expire_due_dispositions(db) -> tuple[list[str], list[str]]:
    """満了した defer に expiry 行を書く (満了 ≠ supersede — P7 の唯一の自動経路)。

    評価は governed fn (= 書き込み時 validator と同じ側) が行う。fn は
    「未成立/対象なし」を NULL で返し、**評価不能のみ** RAISE する (P2 hard
    error) — 呼び出し側がエラーメッセージの散文で分岐しない (P3)。
    returns (expired_tags, expiry_error_tags)."""
    rows = (await db.execute(text(
        "SELECT tag FROM tagger_candidate_disposition_current "
        "WHERE disposition = 'defer' "
        "AND (valid_until <= now() OR release_condition IS NOT NULL)"
    ))).all()
    expired: list[str] = []
    errors: list[str] = []
    for (tag,) in rows:
        try:
            async with db.begin_nested():
                seq = (await db.execute(text(
                    "SELECT tagger_governed_expire_candidate_disposition(:tag, :by)"
                ), {"tag": tag, "by": "reconciler:expiry"})).scalar()
            if seq is not None:
                expired.append(tag)
        except Exception as e:  # unevaluable condition — durable diagnostic, loud
            errors.append(tag)
            print(f"[reconciler] WARNING: disposition expiry unevaluable for {tag}: {e}",
                  file=sys.stderr)
    return expired, errors


async def run_reconcile() -> dict:
    dispatched, skipped, failed, already_approved, gt_insufficient = [], [], [], [], []
    quarantined: list[str] = []  # output-discarded tags -- training would be inert
    non_trainable: list[str] = []  # identity/meta tags -- not a visual concept (Step 5)
    held: list[str] = []  # auto-held this tick (plan rev2 §held)
    decision_skipped: list[str] = []  # standing disposition (issue #55 P1) gated these
    # Governed hold calls that matched 0 rows. Never empty-and-ignored: a hold that
    # changed nothing must not be reported as a hold (see the call site).
    hold_noop: list[tuple[str, str]] = []
    disp: dict[str, dict] = {}  # per-tag decision snapshot -> reconciler_disposition.json
    async with async_session() as db:
        # Kill switch (issue #55 census §5-6: `paused` は set 関数だけあって
        # **reconciler 自身が読んでいなかった** dead state — その配線)。行欠落は
        # transitions.py の規約どおり breach = paused 扱い (fail-closed)。
        _rs = (await db.execute(text(
            "SELECT paused, halt_reason FROM tagger_reconciler_state WHERE id"
        ))).one_or_none()
        if _rs is None or _rs.paused:
            reason = ("tagger_reconciler_state singleton row missing — 行欠落は "
                      "breach = paused 扱い (transitions.py の規約)" if _rs is None
                      else (_rs.halt_reason or "paused (halt_reason 未記入)"))
            print(f"[reconciler] PAUSED — tick skipped: {reason}", file=sys.stderr)
            return {"paused": True, "halt_reason": reason,
                    "dispatched": [], "failed": []}
        config = await load_config(db)
        min_gt_labels = config["min_gt_labels"]
        min_gt_positive = config["min_gt_positive"]
        min_gt_negative = config["min_gt_negative"]
        # kura#62: read the runner's queue depth ONCE per tick. If it is already at
        # or past the ceiling, this tick adds nothing (dispatch would only deepen a
        # queue it cannot drain). Held tags are not consumed — they dispatch on a
        # later tick once the runner has caught up.
        backlog_max = int(config.get("dispatch_backlog_max", _DEFAULT_BACKLOG_MAX))
        downstream_backlog = _downstream_backlog(TRAINING_TARGET_AGENT)
        backpressure_held: list[str] = []
        # Recover tags stuck in 'training' (dispatched but never approved) BEFORE
        # reading candidates, so the main loop sees them as 'candidate' and can
        # re-dispatch/hold them this same tick.
        reopened_stale = await _reap_stale_training(db)
        # 終端 (approved) に着いたのに配備できるものが無いタグも戻す。
        # training の stuck と同じ害で、しかも**終端なので誰も見に来ない**。
        reopened_stale += await _reap_approved_without_head(db)
        # Re-admit ceiling-held candidates whose GT has grown (release trigger (a)),
        # BEFORE reading candidates, so a released tag is re-evaluated this tick.
        released_held, release_noop = await _release_held(
            db, int(min_gt_positive), int(min_gt_negative))
        # issue #55 P1: expire due defers FIRST (満了 event を書く), then read the
        # fold once — a defer that just expired is dispatchable this same tick
        # (受入条件 3「満了後に出る」).
        expired_decisions, expiry_errors = await _expire_due_dispositions(db)
        decisions = await _current_dispositions(db)
        by_tag = await _open_candidates_by_tag(db)
        for tag, candidates in by_tag.items():
            # Decision gate (issue #55 P1) — a standing human/machine decision
            # outranks every machine gate below. Vocabulary is deliberately
            # distinct from the json 'hold'/'held' words (census: 語彙衝突).
            dec = decisions.get(tag)
            if dec is not None and dec["disposition"] in ("defer", "dismiss", "block"):
                disp[tag] = {"disposition": f"decision_{dec['disposition']}",
                             "reason": dec["reason_class"],
                             "decided_by": dec["decided_by"],
                             "actor_type": dec["actor_type"],
                             "decision_seq": dec["seq"]}
                decision_skipped.append(tag)
                continue
            # Non-trainable exclusion (plan tagger-vnext-deterministic-runner
            # Step 5 / E-M2), FIRST in the loop: identity/meta tags (artist/
            # copyright/character/meta categories, or the literal
            # 'tagger-exclude') are not visual concepts -- a dispatched
            # training run can only end in a HOLD registration. Same shared
            # predicate as the two detection entry points (pools.py verify-
            # status, gt_sync._detect_global_candidates); this loop-head check
            # is the backstop for candidate rows that predate the entry-point
            # filters (the 112-row backlog is triaged one-shot by
            # tagger/vnext_triage.py, run on primary -- not silently here).
            category = await _tag_category(db, tag)
            if is_non_trainable(tag, category):
                disp[tag] = {"disposition": "hold", "reason": "non_trainable",
                             "category": category, "gt": None}
                non_trainable.append(tag)
                continue
            if tag in QUARANTINE_TAGS:
                # This tag's WD14 output is DISCARDED in production (config.py
                # QUARANTINE_TAGS): infer.py pops it, nodec excludes it. A trained
                # precision/veto head for it is structurally INERT -- it cannot
                # change discarded output -- so dispatching training only burns a
                # trainer turn to HOLD (measured 2026-07-23: condom_on_penis /
                # mosaic_censoring HELD, bar_censor would fire at neg>=min_gt_negative).
                # These slip the establish/floor gate via has_global_candidate +
                # corpus_pos rescue; this is the one guard that was missing.
                # Lifting a quarantine (removing the tag in config.py once a
                # reliable head exists) re-admits it to the loop automatically.
                disp[tag] = {"disposition": "hold", "reason": "quarantined_output_discarded",
                             "gt": None}
                quarantined.append(tag)
                continue
            stage = await _latest_stage(db, tag)
            if stage == "training":
                # A dispatch is already in flight for this tag -- don't
                # double-fire. (None/'candidate'/'approved' all fall through
                # to the threshold checks below; 'approved' used to be an
                # unconditional skip here, see _has_approved_patch's docstring
                # for why that was wrong.)
                skipped.append((tag, stage))
                disp[tag] = {"disposition": "in_flight", "stage": stage}
                continue

            gt_pos, gt_neg = await _gt_counts(db, tag)
            # Rejected-registration auto-hold (round8 CRIT respawn, plan rev2 §held).
            # If the tag's most recent registration was REJECTED for a reason a hold
            # absorbs, it would otherwise return to 'candidate' via the stale-training
            # GC and re-dispatch forever (anal_object_insertion: 9 candidate->training
            # / 8 back since 2026-07-23, 0 approved). HOLD it -- UNLESS GT grew past
            # that registration's baseline (new signal, release condition (a) -> fall
            # through and re-dispatch). The reason_class is derived structurally from
            # the stored metrics (see _last_registration_hold); it was previously a
            # substring test for 'P_deploy' on the rejection prose, which matched 0 of
            # 27 rejected rows and so had never once fired. This eligibility check
            # sits at the TOP of the dispatch predicate, OUTSIDE the `if established:`
            # block below whose growth gate has the 3 fail-open paths (trainer N1).
            # held rows are status='held' so _open_candidates_by_tag (status=
            # 'candidate' only) excludes them from the next tick.
            hold = await _last_registration_hold(db, tag, config)
            if hold is not None:
                hold_class, ceil = hold
                _cgt = ceil["gt"] or {}
                # "absent" is NOT "zero". A rejected registration whose gt omits the
                # baseline keys cannot demonstrate growth, and reading the missing key
                # as 0 makes any gt_pos>0 look like growth -- a silent fail-open
                # straight back into the respawn loop this gate exists to stop.
                # Measured live (trainer, task ca47c3f6): round-8's
                # anal_object_insertion registration (id=33) wrote 'pos'/'neg' instead
                # of 'positive'/'negative', so the baseline read 0/0, "GT grew" was
                # true against an unchanged label set, and round 9 was dispatched only
                # to reproduce round 8 exactly. Fail CLOSED -- an unreadable baseline
                # holds. Safe because a wrong hold self-heals on +GT for every class
                # the reconciler can emit (see GT_GROWTH_RELEASABLE), which is the
                # asymmetry migration 0110 is built on: a mis-hold recovers, a
                # mis-dispatch burns the single training node for nothing.
                _readable = ("positive" in _cgt) or ("negative" in _cgt)
                _lp, _ln = int(_cgt.get("positive") or 0), int(_cgt.get("negative") or 0)
                # Same rule as the release side -- one helper so hold and release
                # cannot answer "is there enough new signal?" differently and bounce
                # a tag between them, and the same magnitude the established-tag
                # dispatch gate below demands before spending a training run.
                if not (_readable and _gt_grew(_lp, _ln, gt_pos, gt_neg,
                                               int(min_gt_positive), int(min_gt_negative))):
                    ev = {"reason": hold_class, "rejected_reason": ceil["rejected_reason"],
                          "metrics": ceil["metrics"], "gt_pos": gt_pos, "gt_neg": gt_neg,
                          "baseline_pos": _lp, "baseline_neg": _ln,
                          "baseline_readable": _readable,
                          "gt_growth_releasable": hold_class in GT_GROWTH_RELEASABLE}
                    # The governed fn's UPDATE is `WHERE tag=:tag AND status=
                    # 'candidate'` (0110) and it RETURNS the row count. Discarding
                    # that count makes a no-op indistinguishable from a hold: the
                    # tag gets announced as held while nothing changed. Today the
                    # only caller path is candidate->held so 0 means a concurrent
                    # tick moved the row -- but trainer (task ca47c3f6) hit the
                    # same clause from the other side: an ALREADY-held row cannot be
                    # re-classified by this fn alone, it silently matches nothing,
                    # and a re-classifier has to release+hold inside ONE transaction
                    # (splitting them opens a status='candidate' window a tick can
                    # claim). Checking the count is what makes that failure loud
                    # instead of a false success line in the run summary.
                    n = (await db.execute(text(
                        "SELECT tagger_governed_hold_head_candidate(:tag, :rc, CAST(:ev AS jsonb))"
                    ), {"tag": tag, "rc": hold_class, "ev": json.dumps(ev)})).scalar()
                    await db.commit()
                    if n:
                        held.append(tag)
                        disp[tag] = {"disposition": "held", "reason": hold_class,
                                     "gt_pos": gt_pos, "gt_neg": gt_neg, "rows": n,
                                     "rejected_reason": ceil["rejected_reason"]}
                    else:
                        # Nothing matched: do NOT claim a hold. The tag stays in
                        # whatever state it is actually in and is re-evaluated next
                        # tick; the summary shows the no-op so it can be chased.
                        hold_noop.append((tag, hold_class))
                        disp[tag] = {"disposition": "hold_noop", "reason": hold_class,
                                     "gt_pos": gt_pos, "gt_neg": gt_neg}
                    continue
                # GT grew since the ceiling registration -> release, re-dispatch below.
            # Two DIFFERENT questions: min_gt_labels asks "is there enough
            # total signal", min_gt_positive asks "is there ANY positive
            # signal at all" -- a tag can clear the former while being 0
            # for the latter (post_tag_audit corrections that were all
            # removals, or a verify-pool where every untouched post
            # legitimately lacks the tag). Found live 2026-07-21 by
            # trainer-cc-primary (task 2826bccc, PIPI-3: pos=0/neg=20,
            # total=20 easily clears min_gt_labels=10 but a binary
            # classifier cannot be trained with zero positive examples).
            # Trainer correctly refused to fabricate a registration
            # rather than silently failing.
            if gt_pos + gt_neg < min_gt_labels:
                gt_insufficient.append((tag, gt_pos, gt_neg))
                disp[tag] = {"disposition": "hold", "reason": "insufficient_labels",
                             "gt_pos": gt_pos, "gt_neg": gt_neg}
                continue

            is_v_next = await _has_approved_patch(db, tag)
            # An ESTABLISHED tag = one already producing tags in the main tagger:
            # either an approved patch (v-next) OR a global-deletion hotspot (a
            # NULL-source head_candidate, flagged by _detect_global_candidates
            # only after >= min_gt_labels human deletions across the corpus -- so
            # it is an actively-curated base WD14 tag like horse_penis, not a
            # speculative new tag (quarantined output-discarded tags are already
            # filtered out above, before this gate). A pool-only candidate (every
            # candidate row has
            # a source_pool_id) is NOT established -- it may be a brand-new tag
            # under first review, where present!=correct until a verified pool
            # draws the boundary (1.96.11).
            has_global_candidate = any(c["source_pool_id"] is None for c in candidates)
            established = is_v_next or has_global_candidate
            trigger = "new-candidate"
            # Positive-availability floor: a classifier needs enough positive
            # examples to train. For a non-established (fresh pool) candidate the
            # only trustworthy positives are verify-pool-derived gt_label rows.
            # For an ESTABLISHED tag (already IN the main tagger, user: すでに
            # ポジティブ例を持っている), the tag's LIVE corpus population is a valid
            # positive source too, so an over-detected tag whose humans only
            # DELETE (gt_pos ~0, thousands of FP negatives) still clears the floor
            # and can train a suppression/precision head against corpus positives
            # + the human deletions as hard negatives (user 2026-07-23: 検知済み
            # 正例は学習プールではなくそのタグを持つ全体にしたほうがいい). Before this,
            # those tags died here at gt_pos<min_gt_positive and no amount of FP
            # reporting could improve them -- the exact "誤検知10000件報告しても
            # 改善されない" gap.
            corpus_pos = await _corpus_positive_count(db, tag) if established else None
            pos_available = gt_pos if not established else max(gt_pos, corpus_pos or 0)
            if pos_available < min_gt_positive:
                gt_insufficient.append((tag, gt_pos, gt_neg))
                # established=False here => genuinely needs verify-pool positives
                # ("＋正例 あとN" is honest). established=True should be impossible
                # here (corpus rescues the floor) but record faithfully anyway.
                disp[tag] = {"disposition": "hold", "reason": "need_positives",
                             "gt_pos": gt_pos, "gt_neg": gt_neg, "corpus_pos": corpus_pos,
                             "established": established, "is_v_next": is_v_next,
                             "need_positives": int(min_gt_positive) - gt_pos}
                continue
            if established:
                # An established tag (approved patch OR global over-detection
                # hotspot) is only worth (re)training when a MEANINGFUL BATCH of
                # new correction signal has accumulated -- otherwise it re-fires
                # every cycle it still has an open head_candidate and spams the
                # single training node. It's worth a dispatch once EITHER class of
                # GT has grown past its last-registration baseline:
                #   - positives (+min_gt_positive): new true examples the tagger
                #     is missing -> a RECALL retrain.
                #   - negatives (+min_gt_negative): new human FP-deletions ->
                #     a PRECISION (over-detection suppression) retrain.
                # The negative bar is what makes reporting FPs on a tag ALREADY in
                # the tagger actually improve it -- before this the gate counted
                # positives only, so a tag accumulating nothing but FP corrections
                # (thousands of negatives, zero new positives) never re-dispatched
                # and could never be made more precise (user 2026-07-23: 誤検知を
                # 10000件報告しても改善される仕組みがない). It also correctly HOLDS a
                # tag with only a handful of FPs (< min_gt_negative) instead of
                # burning a training run on too little signal (anal_beads 0/12).
                last_pos = await _last_registration_positive(db, tag)
                last_neg = await _last_registration_negative(db, tag)
                # A MISSING baseline counts as 0, not "never dispatch". Almost
                # every current approved patch predates the registration ledger
                # (see _has_approved_patch: old git-file workflow, no recorded gt
                # -- phimosis/erection/smegma all have last_pos AND last_neg NULL),
                # and a global-hotspot base tag has none by definition. Requiring
                # a non-null baseline would make the precision trigger a DEAD
                # BRANCH for exactly the over-detectors it exists to help (phimosis
                # 5693 / erection 4401 FP-deletions, baseline NULL). Zero is the
                # same growth-from-zero semantics the positive gate always used for
                # last_pos is None. Safe because the floor above guaranteed
                # pos_available >= min_gt_positive (gt_pos OR the live corpus) so a
                # trainable positive set always exists, and the 'training' stage
                # guard prevents a dispatched tag from re-firing every tick.
                pos_grew = gt_pos >= (last_pos or 0) + min_gt_positive
                neg_grew = gt_neg >= (last_neg or 0) + min_gt_negative
                # **成長ゲートの前提は「前回の試行が何かを産んだ」こと。**
                # 配備できる head が無いタグにはその前提が無い — 比べる baseline が
                # そもそも存在しないので、GT が動いていないことを理由に止めると
                # **永久に何も起きない**。
                #
                # 2026-08-05 実測: monochrome / greyscale は approved に到達したが
                # patch は bytes を持たず、GT も動かないので、戻しても
                # `no_growth` で即 hold され二度と測り直されない。corpus 601,069 件の
                # 誤検出 (π=0.031) がそのまま再生産され続ける形。
                #
                # **定義は tagger_deployable_head view が唯一持つ** (migration 0140)。
                #
                # 2026-08-07 実測: ここだけ「policy 行があるか」の古い定義のまま
                # だった。GC は新しい定義 (**load できる head が無い**) で reopen
                # するのに、この gate は policy 行を見て「配備済み・成長なし」と
                # hold する。**両者が食い違い、anilingus と dark_labia_majora が
                # 28 時間 candidate のまま 1 度も測り直されなかった** — 壊れた
                # head を自動で測り直す仕掛けを入れた当日に、その入口を塞いでいた。
                deployable = (await db.execute(text(
                    "SELECT 1 FROM tagger_deployable_head "
                    "WHERE tag_id = tagger_tag_id_of(:tag) LIMIT 1"
                ), {"tag": tag})).one_or_none() is not None
                if not (pos_grew or neg_grew) and deployable:
                    already_approved.append(tag)
                    # established but neither class grew enough since last attempt:
                    # waiting for a bigger correction batch (e.g. FPs < min_gt_negative).
                    disp[tag] = {"disposition": "hold", "reason": "no_growth",
                                 "gt_pos": gt_pos, "gt_neg": gt_neg, "corpus_pos": corpus_pos,
                                 "established": established, "is_v_next": is_v_next,
                                 "last_pos": last_pos, "last_neg": last_neg,
                                 "need_neg": max(0, int((last_neg or 0) + min_gt_negative) - gt_neg),
                                 "need_pos": max(0, int((last_pos or 0) + min_gt_positive) - gt_pos)}
                    continue
                # v-next = retrain an existing approved patch; new-head = a base
                # WD14 tag with no patch yet needs a fresh suppression/precision
                # head (gt_pos may be ~0, corpus-rescued -> precision off neg_grew).
                suffix = "v-next" if is_v_next else "new-head"
                trigger = (f"recall+precision-{suffix}" if pos_grew and neg_grew
                           else f"recall-{suffix}" if pos_grew else f"precision-{suffix}")
            # else: a non-established (pool-only, possibly brand-new) candidate --
            # no growth gate, dispatches once as 'new-candidate' the first time it
            # clears the strict gt_label floor (then becomes 'training').

            # per-tick 有界 dispatch (config: max_training_dispatch_per_cycle)。
            # sweep が 5000件/15分で有界なのと同じ流儀 — character trainable 化
            # (2026-09-04) の プール GT 種付けで open candidate が一度に ~90 増え、
            # 無制限だと 1 tick で全て trainer へ流れる。cap を超えた分は
            # candidate のまま残り、次 tick 以降に自然に順番が来る。
            _max_disp = int(config.get("max_training_dispatch_per_cycle", 6))
            if len(dispatched) >= _max_disp:
                disp[tag] = {"disposition": "hold", "reason": "dispatch_cap",
                             "gt_pos": gt_pos, "gt_neg": gt_neg,
                             "established": established, "is_v_next": is_v_next}
                continue

            # kura#62 backpressure: the queue depth we would create this tick is
            # what the runner has NOT claimed plus what we have already dispatched
            # here. Past the ceiling, hold — the tag is not consumed, so it goes
            # out on a later tick once the runner catches up. Fails open: a None
            # backlog (broker unreadable) does not block, matching pre-#62.
            if _saturated(downstream_backlog, len(dispatched), backlog_max):
                disp[tag] = {"disposition": "hold", "reason": "downstream_saturated",
                             "backlog": downstream_backlog, "backlog_max": backlog_max,
                             "dispatched_this_tick": len(dispatched),
                             "gt_pos": gt_pos, "gt_neg": gt_neg}
                backpressure_held.append(tag)
                continue

            ok = _dispatch_training_request(tag, candidates, gt_pos, gt_neg, min_gt_labels,
                                            trigger, corpus_pos)
            disp[tag] = {"disposition": "dispatch" if ok else "dispatch_failed",
                         "trigger": trigger, "gt_pos": gt_pos, "gt_neg": gt_neg,
                         "corpus_pos": corpus_pos, "established": established,
                         "is_v_next": is_v_next}
            if not ok:
                failed.append(tag)
                continue

            lease = None
            try:
                # ONE lease, reused for both writes -- re-acquiring the same
                # scope mid-flight fails (a caller can't distinguish "renewing
                # my own lease" from "stealing someone else's", see
                # transitions.py::acquire_lease's own docstring). Confirmed
                # live: the two-acquire version left 11 tags stuck at
                # 'candidate' with the second write silently failing.
                lease = await acquire_lease(db, f"lifecycle:{tag}", lease_seconds=30)
                if stage is None:
                    await record_lifecycle_event(
                        db, tag=tag, from_stage=None, to_stage="candidate",
                        reason="head_candidate auto-detected, entering lifecycle ledger", lease=lease,
                    )
                    stage = "candidate"
                elif stage != "candidate":
                    # v-next retry on an already-approved tag: reopen as
                    # 'candidate' first (rollback edge -- same semantics
                    # tagger/health.py's regression re-flag already uses for
                    # "this approved tag needs another look"), then advance
                    # to 'training' normally. approved->training directly
                    # isn't a legal edge (rank-decreasing for an 'advance'
                    # kind, per trg_tagger_lifecycle_event_edge_check).
                    await record_lifecycle_event(
                        db, tag=tag, from_stage=stage, to_stage="candidate",
                        reason=f"{trigger}: GT grew to positive={gt_pos}/negative={gt_neg} "
                                f"(thresholds +{min_gt_positive:.0f} pos / +{min_gt_negative:.0f} neg "
                                f"past last registration attempt)",
                        lease=lease,
                    )
                    stage = "candidate"
                await record_lifecycle_event(
                    db, tag=tag, from_stage="candidate", to_stage="training",
                    reason=f"auto-dispatched training request to {TRAINING_TARGET_AGENT} "
                            f"({len(candidates)} candidate row(s))",
                    lease=lease,
                )
            except TransitionError as e:
                # Dispatch already happened -- do not silently lose that fact,
                # but do surface the ledger-write failure loudly.
                print(f"[reconciler] WARNING: dispatched {tag} but ledger write failed: {e}", file=sys.stderr)
            finally:
                # Release the lifecycle:<tag> lease as soon as this tag's unit
                # of work is done, instead of leaving the row to expire and
                # later trip tagger/health's stale-lease check (incident
                # 2026-07-22). Best-effort: release_lease is fencing-matched
                # and idempotent, so a failure here never corrupts another
                # holder's lease -- but a broker/DB hiccup shouldn't abort the
                # whole reconcile loop either.
                if lease is not None:
                    try:
                        await release_lease(db, lease)
                    except TransitionError as e:
                        print(f"[reconciler] WARNING: lease release failed for {tag} "
                              f"(reclaim-on-acquire will still self-heal): {e}", file=sys.stderr)
            dispatched.append(tag)

    # Snapshot every candidate tag's decision for the dashboard (single source of
    # truth -- the dashboard renders this, never re-derives the gate). Atomic
    # replace so a mid-write read never sees a truncated file.
    try:
        doc = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "min_gt_positive": int(min_gt_positive),
            "min_gt_negative": int(min_gt_negative),
            "min_gt_labels": int(min_gt_labels),
            "tags": disp,
        }
        tmp = DISPOSITION_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
        tmp.replace(DISPOSITION_PATH)
    except OSError as e:
        print(f"[reconciler] WARNING: could not write {DISPOSITION_PATH.name}: {e}", file=sys.stderr)

    return {"dispatched": dispatched, "skipped": skipped, "failed": failed,
             "reopened_stale_training": reopened_stale, "quarantined": quarantined,
             "non_trainable": non_trainable,
             "held_prevalence_ceiling": held, "released_held": released_held,
             "hold_noop": hold_noop, "release_noop": release_noop,
             "already_approved_not_dispatched": already_approved,
             "gt_insufficient_not_dispatched": gt_insufficient,
             "decision_skipped": decision_skipped,
             "expired_decisions": expired_decisions,
             "decision_expiry_errors": expiry_errors,
             "backpressure_held": backpressure_held,
             "downstream_backlog": downstream_backlog}


def main() -> int:
    import asyncio
    summary = asyncio.run(run_reconcile())
    print(f"[reconciler] {summary}")
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
