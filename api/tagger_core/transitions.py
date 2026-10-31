"""api/tagger_core/transitions.py — guarded entry points to the governed lifecycle functions.

Plan kura-tagger-lifecycle-redesign (fact 2fe6daf6, revision 5, approved),
Steps 8-9. This module is the ONLY python-side caller of the SECURITY
DEFINER functions from migrations 0079/0080 — reconciler.py, the claim
guard in api/routers/tagging.py, and sweep.py all go through here, never
raw SQL against a governed table. Every guard here fails closed: missing or
ambiguous input blocks the transition, it never defaults to "proceed".

This module deliberately does NOT re-derive the allowed-edge/rank check
that 0077's trg_tagger_lifecycle_event_edge_check trigger already enforces
in the DB — duplicating that logic here would let the two definitions
drift apart. A caller that wants to pre-check "is this even a legal edge"
before doing expensive work (e.g. before kicking off a retrain) should
query tagger_stage_transition directly (read-only, ungoverned, no lease
needed for a read).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Fail-closed metrics floors -- used to be hardcoded constants here
# (MIN_HELD_OUT_SAMPLES/MIN_PRECISION/MIN_RECALL/MIN_VAL_POS_SUPPORT/
# MIN_PRED_POS_SUPPORT), moved to the governed table tagger_config
# (migration 0098, api/tagger_core/config.py) so they're dashboard-editable
# instead of magic numbers requiring a code deploy to change (user
# directive 2026-07-20: マジックナンバーにせず、変数としてダッシュボード
# からコントロールできるようにしてくれ / その他の値についてもだ).
# validate_metrics_floor/validate_registration_metrics_floor below now
# take a `config: dict` (from api.tagger_core.config.load_config(db)) instead of
# reading module constants. See api/tagger_core/config.py's DEFAULTS for the
# current values and api/tagger_core/config.py's docstring for the reasoning
# behind each floor.


class TransitionError(Exception):
    """A guard or governed function refused a transition. Always fail-closed:
    callers must treat this as 'no state changed', never partial success."""


@dataclass(frozen=True)
class Lease:
    scope: str
    fencing_token: int
    expires_at: object  # datetime, kept opaque — callers never compare it themselves


async def acquire_lease(db: AsyncSession, scope: str, lease_seconds: int = 60) -> Lease:
    """Acquire the scope-exclusive lease via tagger_acquire_lease (0079).

    Raises TransitionError if another live holder has it, or if session_user
    is not on the allowlist for this scope. Never retries internally — a
    caller that wants retry-with-backoff does so explicitly, so failure is
    always visible one level up.

    IMPORTANT — reuse, don't re-acquire: every governed caller connects as
    the same DB role (session_user is always 'kura'), so holder_agent_id
    never distinguishes "me, renewing" from "someone else, stealing" — the
    governed function only allows a takeover once expires_at has passed
    (confirmed live: re-acquiring the same scope from the same process
    before expiry raises "held by another agent", identical to a real
    steal attempt). A caller that needs to perform several writes to the
    same scope must acquire ONE lease with a lease_seconds covering the
    whole unit of work, then pass that same Lease to every call — not
    acquire per-call.
    """
    try:
        row = (await db.execute(
            text("SELECT * FROM tagger_acquire_lease(:scope, :secs)"),
            {"scope": scope, "secs": lease_seconds},
        )).one()
    except Exception as e:
        await db.rollback()
        raise TransitionError(f"acquire_lease({scope}) failed: {e}") from e
    await db.commit()
    return Lease(scope=scope, fencing_token=row.fencing_token, expires_at=row.expires_at)


async def release_lease(db: AsyncSession, lease: Lease) -> bool:
    """Release a lease acquired via acquire_lease, by DELETEing its row from
    tagger_lifecycle_lock through the governed tagger_release_lease function
    (migration 0108).

    Fencing-safe and idempotent by construction: the governed function only
    deletes the row when (scope, holder_agent_id=session_user, fencing_token)
    all still match — the fencing token IS the capability, so a caller can
    only ever release the exact lease it holds, never steal or clobber a lease
    someone else has since reclaimed. Returns True if this call actually
    removed our row, False if it was already gone (double-release) or had been
    reclaimed by another holder (token no longer matches) — either way the
    post-condition "we no longer hold this scope" is satisfied, so a False is
    not an error.

    Deliberately does NOT go through tagger_check_fencing (which requires the
    lease to still be un-expired): a unit of work can legitimately outlive its
    lease_seconds, and releasing an already-expired-but-not-yet-reclaimed lease
    is exactly the cleanup we want — matching on the token alone still proves
    we were the last legitimate holder. Best-effort: this is called in a
    finally/cleanup path, so it swallows nothing silently but also never
    raises TransitionError for the benign already-released/already-reclaimed
    cases (those return False rather than error).
    """
    try:
        row = (await db.execute(
            text("SELECT tagger_release_lease(:scope, :token) AS released"),
            {"scope": lease.scope, "token": lease.fencing_token},
        )).one()
    except Exception as e:
        await db.rollback()
        raise TransitionError(f"release_lease({lease.scope}): {e}") from e
    await db.commit()
    return bool(row.released)


async def record_lifecycle_event(
    db: AsyncSession, *, tag: str, from_stage: str | None, to_stage: str,
    reason: str, lease: Lease,
) -> int:
    """Append one row to tagger_lifecycle_event via the governed function.

    Requires a lease already held for scope 'lifecycle:<tag>' (acquire it
    first) so two callers can never race writing two different stages for
    the same tag concurrently.
    """
    expected_scope = f"lifecycle:{tag}"
    if lease.scope != expected_scope:
        raise TransitionError(
            f"record_lifecycle_event: lease scope {lease.scope!r} does not match "
            f"required scope {expected_scope!r} — acquire the right lease first"
        )
    try:
        row = (await db.execute(
            text(
                "SELECT tagger_record_lifecycle_event(:tag, :from_stage, :to_stage, "
                ":reason, :token) AS id"
            ),
            {"tag": tag, "from_stage": from_stage, "to_stage": to_stage,
             "reason": reason, "token": lease.fencing_token},
        )).one()
    except Exception as e:
        await db.rollback()
        raise TransitionError(f"record_lifecycle_event({tag}, {from_stage}->{to_stage}): {e}") from e
    await db.commit()
    return row.id


async def upsert_heartbeat(
    db: AsyncSession, *, worker_id: str, host: str, profile: str, generation: int,
    release_id: int | None, provider: str, model_sha256: str | None,
) -> None:
    """Worker self-report. No lease — each worker owns only its own
    worker_id-keyed row, there is no cross-worker exclusivity to protect
    here (unlike lifecycle-event/reconciler-control, which are shared
    singleton/per-tag state)."""
    try:
        await db.execute(
            text(
                "SELECT tagger_upsert_heartbeat(:wid, :host, :profile, :gen, "
                ":rel, :provider, :sha)"
            ),
            {"wid": worker_id, "host": host, "profile": profile, "gen": generation,
             "rel": release_id, "provider": provider, "sha": model_sha256},
        )
    except Exception as e:
        await db.rollback()
        raise TransitionError(f"upsert_heartbeat({worker_id}): {e}") from e
    await db.commit()


async def set_reconciler_pause(
    db: AsyncSession, *, paused: bool, halt_reason: str | None, lease: Lease,
) -> None:
    if lease.scope != "reconciler-control":
        raise TransitionError(
            f"set_reconciler_pause: lease scope {lease.scope!r} != reconciler-control"
        )
    try:
        await db.execute(
            text("SELECT tagger_set_reconciler_pause(:paused, :reason, :token)"),
            {"paused": paused, "reason": halt_reason, "token": lease.fencing_token},
        )
    except Exception as e:
        await db.rollback()
        raise TransitionError(f"set_reconciler_pause: {e}") from e
    await db.commit()


async def is_reconciler_paused(db: AsyncSession) -> tuple[bool, str | None]:
    """Fail-closed: a missing singleton row is itself a breach, not 'not paused'."""
    row = (await db.execute(
        text("SELECT paused, halt_reason FROM tagger_reconciler_state WHERE id = true")
    )).one_or_none()
    if row is None:
        raise TransitionError("tagger_reconciler_state has no row — treating as paused/broken")
    return bool(row.paused), row.halt_reason


async def record_operation(
    db: AsyncSession, *, scope: str, target_id: str, operation_type: str,
    lease: Lease, result: str | None = None,
) -> bool:
    """Idempotency check-and-record via tagger_record_operation (0079).

    Returns True if this call newly recorded the operation (caller should
    proceed with the underlying side effect), False if it was already
    recorded (caller must treat this as a no-op, never retry the side
    effect a second time for the same scope+target_id+operation_type).
    """
    if lease.scope != scope:
        raise TransitionError(f"record_operation: lease scope {lease.scope!r} != {scope!r}")
    try:
        row = (await db.execute(
            text(
                "SELECT tagger_record_operation(:scope, :target, :optype, :token, :result) AS is_new"
            ),
            {"scope": scope, "target": target_id, "optype": operation_type,
             "token": lease.fencing_token, "result": result},
        )).one()
    except Exception as e:
        await db.rollback()
        raise TransitionError(f"record_operation({scope}, {target_id}, {operation_type}): {e}") from e
    await db.commit()
    return bool(row.is_new)


def validate_metrics_floor(metrics: dict | None, config: dict) -> None:
    """Fail-closed metrics gate for a FUTURE automatic-approval transition
    (Step 14) -- see api/tagger_core/config.py for why nothing calls this today
    (human review is the real gate for the current git-file patch
    workflow; this exists for whenever an automatic-approval path is
    actually built).

    `config` is a dict from api.tagger_core.config.load_config(db) (or DEFAULTS,
    for a caller with no db handy) -- min_held_out_samples/min_precision/
    min_recall keys.

    Missing metrics, a missing n_val, or a held-out count below the floor
    all raise — there is no code path where absence of evidence is
    treated as passing evidence. Field name matches the real patch JSON
    schema (tagger/patches/<tag>/vN.json's "metrics.n_val"), not an
    invented name.
    """
    if not metrics:
        raise TransitionError("validate_metrics_floor: no metrics provided")
    min_held_out = config["min_held_out_samples"]
    n = metrics.get("n_val")
    if n is None or n < min_held_out:
        raise TransitionError(
            f"validate_metrics_floor: n_val={n} below floor {min_held_out}"
        )
    min_precision, min_recall = config["min_precision"], config["min_recall"]
    precision = metrics.get("precision")
    recall = metrics.get("recall")
    if precision is None or recall is None:
        raise TransitionError("validate_metrics_floor: precision/recall missing")
    if precision < min_precision or recall < min_recall:
        raise TransitionError(
            f"validate_metrics_floor: precision={precision} recall={recall} "
            f"below floor ({min_precision}/{min_recall})"
        )


def validate_registration_metrics_floor(metrics: dict | None, config: dict) -> None:
    """Fail-closed denominator + accuracy gate for tagger_registration_
    request metrics (trainer's train_gt_heads.py output shape). Used as a
    reviewer aid before deciding validated vs rejected via POST
    /registration-request/{id}/status -- that endpoint is itself
    deliberately unenforced (see its docstring), so this does not block
    anything by itself; a caller must actually check the raised error and
    act on it.

    `config` is a dict from api.tagger_core.config.load_config(db) (or DEFAULTS,
    for a caller with no db handy) -- min_val_pos_support/
    min_pred_pos_support/min_precision/min_recall keys.

    Checks the DENOMINATOR each ratio (P, R) was computed over, not just
    the ratio or the overall n_val -- a tiny positive-class support can
    produce a perfect-looking P/R by chance (see api/tagger_core/config.py's
    DEFAULTS docstring for the real example that prompted this).
    """
    if not metrics:
        raise TransitionError("validate_registration_metrics_floor: no metrics provided")
    val = metrics.get("val")
    if not val:
        raise TransitionError("validate_registration_metrics_floor: no val breakdown provided")
    tp, fp, fn = val.get("tp"), val.get("fp"), val.get("fn")
    if tp is None or fp is None or fn is None:
        raise TransitionError("validate_registration_metrics_floor: val breakdown missing tp/fp/fn")
    min_val_pos, min_pred_pos = config["min_val_pos_support"], config["min_pred_pos_support"]
    recall_support = tp + fn      # == val_pos
    precision_support = tp + fp   # predicted-positive count
    if recall_support < min_val_pos:
        raise TransitionError(
            f"validate_registration_metrics_floor: recall denominator (tp+fn={recall_support}) "
            f"below floor {min_val_pos} -- too few actual positives in held-out to trust R"
        )
    if precision_support < min_pred_pos:
        raise TransitionError(
            f"validate_registration_metrics_floor: precision denominator (tp+fp={precision_support}) "
            f"below floor {min_pred_pos} -- too few predicted positives in held-out to trust P"
        )
    min_precision, min_recall = config["min_precision"], config["min_recall"]
    precision, recall = metrics.get("P"), metrics.get("R")
    if precision is None or recall is None:
        raise TransitionError("validate_registration_metrics_floor: P/R missing")
    if precision < min_precision or recall < min_recall:
        raise TransitionError(
            f"validate_registration_metrics_floor: P={precision} R={recall} below floor "
            f"({min_precision}/{min_recall})"
        )
    # Frame-corrected deploy precision (round8 CRIT registration-gate-passable-by-
    # harmful-head, plan rev2 Floor/INV-E). val P/R is measured on the flagged pool;
    # P_deploy = πR/(πR+(1−π)FPR) is the precision at the tag's corpus prevalence.
    # A head can clear P/R yet deploy harmfully at low P_deploy when π is tiny
    # (prevalence ceiling). FAIL-CLOSED: a missing P_deploy is rejected exactly like
    # missing P/R -- the fail-closed rule (not the number) is what shuts the CRIT.
    min_p_deploy = config["min_p_deploy"]
    p_deploy = metrics.get("P_deploy")
    if p_deploy is None:
        raise TransitionError(
            "validate_registration_metrics_floor: P_deploy (frame-corrected deploy "
            "precision at corpus prevalence) missing -- fail-closed. Submit metrics with "
            "P_deploy so a harmful head cannot pass on flagged-pool P/R alone."
        )
    if p_deploy < min_p_deploy:
        raise TransitionError(
            f"validate_registration_metrics_floor: P_deploy={p_deploy} below floor "
            f"{min_p_deploy} -- deploys at low precision despite val P={precision}/R={recall} "
            f"(prevalence ceiling / frame failure)"
        )


# ---------------------------------------------------------------------------
# Registration-rejection DISPOSITION (round8 §held, plan kura-tagger-self-
# improvement-loop rev2 §(C)).
#
# The floor above ENFORCES (raise/don't-raise). The reconciler needs a second,
# different answer about the SAME rejection: "should this candidate be HELD, and
# under which reason_class?" That answer used to be derived by substring-matching
# 'P_deploy' against the stored rejected_reason PROSE (reconciler._last_
# registration_ceiling). Measured on primary 2026-07-27 (trainer round6, task
# 9d0c9190): 0 of 27 rejected registration_requests matched, so the round-8
# auto-hold had never fired once since it went live -- because the P_deploy
# checks are the LAST two in the ordered floor, every candidate that fails an
# EARLIER check (P/R below floor, denominator too small, no val breakdown) is
# rejected with prose that never contains the word. The predicate was
# simultaneously too narrow (missed every real respawn regular) and too coarse
# (any match would have been recorded as prevalence_ceiling regardless of cause).
#
# The fix is to classify from the STRUCTURED evidence the validator itself reads
# -- the submitted metrics dict + the same tagger_config floors -- never from the
# rendered message. Prose is a rendering; the metrics are the fact.
# ---------------------------------------------------------------------------

# hold_reason_class values the DB accepts (migration 0110's CHECK constraint).
# Mirrored here so a caller can assert membership BEFORE issuing the governed
# call; the CHECK remains the enforcement.
DB_HOLD_REASON_CLASSES = frozenset({
    "prevalence_ceiling",
    "label_definition_conflict",
    "inert_serving_path",
    "vocab_absent_routing",
    "insufficient_gt",
    "migration_quarantine",
})

# NOT a hold class -- the sentinel for "rejected, but the remedy is a corrected
# RESUBMISSION rather than more GT" (the submitter omitted a required field).
# Holding on these would strand a candidate on a protocol gap that no amount of
# labelling fixes. Deliberately outside DB_HOLD_REASON_CLASSES so it is
# structurally impossible to write into hold_reason_class.
RESUBMIT = "resubmit_metrics_incomplete"

# Which hold classes a GT-growth event may AUTO-release (plan rev2 F3: "release
# はクラスから導出(OR手書きしない)"). Growth is new signal only where the block
# is a function of how much labelled data exists:
#   - insufficient_gt      : literally waiting for more labels
#   - prevalence_ceiling   : more positives can move R (and so P_deploy = piR /
#                            (piR + (1-pi)FPR)) off the ceiling
# The INV-D/label classes are NOT releasable by GT: a routing gap
# (vocab_absent_routing -- cum_in_cup), an unreachable serving path
# (inert_serving_path), a contradictory label definition or a migration
# grandfathering decision are not resolved by adding rows to tagger_gt_label,
# so auto-releasing them would just restart the respawn loop the hold exists to
# stop. Those release through the human governed path only (migration 0110's
# asymmetry: a mis-hold recovers on +GT, a mis-dismiss is permanent).
GT_GROWTH_RELEASABLE = frozenset({"prevalence_ceiling", "insufficient_gt"})


def classify_registration_floor_failure(metrics: dict | None, config: dict) -> str | None:
    """Why a registration's metrics fail the floor, as a reason CLASS.

    Returns None when the metrics CLEAR the floor (validate_registration_
    metrics_floor would not raise), RESUBMIT when the submission is missing a
    required field, else a member of DB_HOLD_REASON_CLASSES.

    Check order mirrors validate_registration_metrics_floor exactly -- the two
    read the same keys off the same dict with the same config floors, so the
    class always describes the check that actually rejected the row rather than
    a later one it never reached. tests/test_registration_hold_class.py asserts
    the agreement (raises <=> non-None) over both hand-written and generated
    cases; that test, not this comment, is what keeps them from drifting.

    Why 'P/R below floor with adequate denominators' maps to insufficient_gt
    rather than a class of its own: the reconciler only ever consults this when
    GT has NOT grown since the rejected attempt, and the question it is really
    asking is "would re-dispatching produce a different number?". With the same
    training set the answer is no, and the thing that would change it is more
    labelled data -- which is exactly insufficient_gt's release condition. It is
    the closest honest fit inside migration 0110's plan-defined 6-class
    vocabulary; a distinct 'metrics_floor_unmet' class would describe it better
    but adding one is a plan-design change (INV-7), not an implementation
    detail, so it is raised to the plan owner instead of invented here.
    """
    if not metrics:
        return RESUBMIT
    val = metrics.get("val")
    if not val:
        return RESUBMIT
    tp, fp, fn = val.get("tp"), val.get("fp"), val.get("fn")
    if tp is None or fp is None or fn is None:
        return RESUBMIT
    if (tp + fn) < config["min_val_pos_support"]:
        return "insufficient_gt"
    if (tp + fp) < config["min_pred_pos_support"]:
        return "insufficient_gt"
    precision, recall = metrics.get("P"), metrics.get("R")
    if precision is None or recall is None:
        return RESUBMIT
    if precision < config["min_precision"] or recall < config["min_recall"]:
        return "insufficient_gt"
    p_deploy = metrics.get("P_deploy")
    if p_deploy is None:
        return RESUBMIT
    if p_deploy < config["min_p_deploy"]:
        return "prevalence_ceiling"
    return None
