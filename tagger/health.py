#!/usr/bin/env python3
# Description: tagger lifecycle health check — reports anomalies to the broker via report_signal, re-flags regressed tags as head_candidate
# Kind: tool
# Effects: mostly read-only (kura DB) + reports to broker; one governed write path for regression re-flagging
# Runs-on: primary
"""tagger/health.py — continuous health check for the tagger lifecycle
(plan:kura-tagger-lifecycle-redesign Step 18).

Read-only against kura's own DB (0078's tagger_lifecycle/tagger_fleet
views + tagger_reconciler_state/tagger_lifecycle_lock) -- never mutates
anything. Reports anomalies to the broker via report_signal_cli.py
(fire-and-forget, matches that script's own non-fatal-on-broker-down
contract) so they show up wherever the fleet already watches signals,
instead of requiring someone to know to poll kura specifically.

Checks implemented this pass:
  - lifecycle consistency violations (tagger_lifecycle.consistency IS NOT
    NULL -- e.g. "patch approved but no lifecycle_event recorded")
  - fleet degradation (tagger_fleet.liveness != 'active' OR
    degraded_reason IS NOT NULL -- missing heartbeat, profile mismatch,
    provider regression). A host marked opportunistic (no uptime SLA,
    e.g. a laptop -- tagger_expected_worker.opportunistic, migration
    0126) that is merely stale with no degraded_reason does not page
    (see classify_fleet); it stays in fleet_degraded_opportunistic_quiet
    for observability, same treatment as idle-reclaimable leases below.
  - reconciler paused (tagger_reconciler_state.paused = true) -- surfaced
    as a signal so a human notices a halt instead of it sitting silently
  - stale/uncleaned leases (tagger_lifecycle_lock rows expired more than
    STALE_LEASE_GRACE_SECONDS ago). An expired lock is reclaim-on-acquire
    self-healing and, since migration 0108, normal operations release their
    own lock -- so a merely-expired lock is NOT a safety issue and is no
    longer reported as a signal (it was the ~102-repeat false-positive CRIT
    of incident 2026-07-22). Only a lock that genuinely cannot self-heal --
    the reconciler is halted, so nothing will reclaim it -- is escalated to
    urgency=1. See classify_stale_leases for the full reasoning.
  - post-merge regression (user directive 2026-07-20: "マージの結果スコ
    アが悪化し要観察状態になったタグがあればhead候補に入れてほしい" --
    if a merge, or accumulated real-world tagging drift, made an already-
    approved tag's accuracy against its own tagger_gt_label evidence drop
    below the tagger_config regression_accuracy_threshold, re-register it as a
    tagger_head_candidate so the reconciler picks it back up. This is the
    ONE mutating check in this module (via the governed function, not a
    direct write) -- everything else here is read-only.

Known gaps (documented, not silently skipped -- see plan Step 18's full
wishlist): sweep-ceiling-reached detection depends on Step 14-15 (staged-
publish) machinery that does not exist yet; unregistered-executor
rejection logging would require log scanning, not a DB query.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from api.db.session import async_session
from api.tagger_core.config import load_config

# STALE_LEASE_GRACE_SECONDS stays a plain constant deliberately -- it's an
# ops/lease-timing knob, not an ML quality judgment, so it's out of scope
# for the tagger_config dashboard-editable set (user directive 2026-07-20
# was specifically about magic numbers used to JUDGE tagger quality).
STALE_LEASE_GRACE_SECONDS = 3600
# regression_accuracy_threshold/regression_min_gt_labels: dashboard-
# editable via tagger_config (migration 0098, api/tagger_core/config.py) -- were
# hardcoded constants here, moved per the same directive.
REPORT_SIGNAL_CLI = os.environ.get(
    "KURA_REPORT_SIGNAL_CLI",
    os.path.expanduser("~/broker/scripts/report_signal_cli.py"),
)


def _report(key: str, urgency: int, detail: str) -> None:
    """Fire-and-forget, matches report_signal_cli.py's own non-fatal
    contract -- a health-check script must never crash the timer unit
    because the broker happened to be unreachable."""
    try:
        subprocess.run(
            [sys.executable, REPORT_SIGNAL_CLI, "tagger-health", key, str(urgency), detail],
            timeout=15, check=False,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[health] report_signal failed (non-fatal): {e}", file=sys.stderr)


async def check_consistency(db) -> list[dict]:
    rows = (await db.execute(text(
        "SELECT tag, lifecycle_stage, consistency FROM tagger_lifecycle "
        "WHERE consistency IS NOT NULL"
    ))).mappings().all()
    return [dict(r) for r in rows]


async def check_reap_silent_nodes(db) -> list[dict]:
    """**タグ付けしているのに一度も reap を提案していないノード。**

    reap は「サーバが認可する」と「worker が提案する」の積で動く。提案は
    worker のコードが出すので、**古いコードを走らせているノードは黙って
    掃除に参加しない**。DB からは「タグ付けは進んでいる」ようにしか見えない。

    2026-08-05 実測: primary だけが提案しており、nodec / desk / nodeb は直近 1 時間で
    1,620 件タグ付けして削除 0 件だった (3 ノードは `tagger_node_state.detail` も
    空で、現行 worker とは別のコードベースを走らせていた — migration 0068 が
    2026-07-20 に「3 つのコピーがあり同期を強制するものが無い」と記録し、
    cut-over されないまま残っていたもの)。**コーパスの 95% で reap が
    走っていなかった。**

    「削除が 0 件」自体は正常でありうる (消すべきものが無い) ので、**他ノードが
    同期間に削除している**ことを条件に入れる — fleet 内で挙動が割れているときだけ
    鳴らす。
    """
    rows = (await db.execute(text(
        "WITH act AS ("
        "  SELECT host, count(*) AS tagged,"
        "         count(*) FILTER (WHERE removed_tags <> '{}') AS removed"
        "  FROM post_tag_audit"
        "  WHERE created_at > now() - interval '2 hours' AND host IS NOT NULL"
        "  GROUP BY host)"
        "SELECT host, tagged, removed FROM act"
        " WHERE removed = 0 AND tagged >= 100"
        "   AND EXISTS (SELECT 1 FROM act o WHERE o.removed > 0)"
        " ORDER BY tagged DESC"
    ))).mappings().all()
    return [dict(r) for r in rows]


async def check_fleet(db) -> list[dict]:
    rows = (await db.execute(text(
        "SELECT host, liveness, degraded_reason, opportunistic FROM tagger_fleet "
        "WHERE liveness != 'active' OR degraded_reason IS NOT NULL"
    ))).mappings().all()
    return [dict(r) for r in rows]


def classify_fleet(fleet: list[dict]) -> dict:
    """Split fleet-degraded rows into paged vs quiet, same shape as
    classify_stale_leases: a host marked opportunistic (no uptime SLA --
    e.g. a laptop, see tagger_expected_worker.opportunistic / migration
    0126) that is merely stale, with no degraded_reason, is expected to
    go quiet for hours at a time -- reporting it at urgency=2 every
    10-minute health tick regardless is the exact ~1500-repeat pattern
    already fixed for stale leases and tag-regression. Anything else
    (missing heartbeat, off-release, or a genuine degraded_reason such as
    a provider mismatch) still pages."""
    paged, quiet = [], []
    for f in fleet:
        if f["opportunistic"] and f["liveness"] == "stale" and not f["degraded_reason"]:
            quiet.append(f)
        else:
            paged.append(f)
    return {"paged": paged, "quiet": quiet}


async def check_reconciler_paused(db) -> dict | None:
    row = (await db.execute(text(
        "SELECT paused, halt_reason, updated_at FROM tagger_reconciler_state WHERE id = true"
    ))).one_or_none()
    if row is None or not row.paused:
        return None
    return {"halt_reason": row.halt_reason, "updated_at": str(row.updated_at)}


async def check_stale_leases(db) -> list[dict]:
    rows = (await db.execute(text(
        "SELECT scope, holder_agent_id, expires_at FROM tagger_lifecycle_lock "
        "WHERE expires_at < now() - make_interval(secs => :grace)"
    ), {"grace": STALE_LEASE_GRACE_SECONDS})).mappings().all()
    return [dict(r) for r in rows]


def classify_stale_leases(stale: list[dict], reconciler_paused: bool) -> dict:
    """Split expired-beyond-grace locks into genuinely-stuck vs idle-
    reclaimable -- the fix for the incident-2026-07-22 false-positive CRIT.

    tagger_lifecycle_lock is a per-scope singleton whose acquire path
    (tagger_acquire_lease) is reclaim-on-acquire: an expired row is not a
    resource leak and blocks nothing -- the very next acquire of that scope
    (`ON CONFLICT (scope) DO UPDATE ... WHERE expires_at < now()`) reclaims it.
    Since migration 0108 also added an explicit release path, a normally-
    completing operation now removes its own row, so in steady state there are
    no idle rows at all. Either way, a merely-expired lock is self-healing and
    NOT a safety issue (health.py's own module docstring already conceded
    this) -- reporting it at urgency=1 was the bug that fired ~102 CRITs.

    A stale lock is genuinely-stuck ONLY when it cannot self-heal, i.e. when
    nothing will re-acquire its scope: that is exactly the case where the
    reconciler is halted. While the reconciler is running, every lifecycle:*
    and backlog-sweep scope is revisited on its timer and any orphaned lock is
    reclaimed (or was already released) -- so an expired lock is idle-
    reclaimable, not stuck. A lock for a scope that is simply no longer active
    (a finished/dismissed tag, a one-shot smoketest) lingers harmlessly as a
    tombstone and is likewise idle-reclaimable, never a CRIT.

    Returns {"genuinely_stuck": [...], "idle_reclaimable": [...]}.
    """
    if reconciler_paused:
        # Reclaim-on-acquire can't fire while the reconciler is halted, so
        # these leases really are frozen -- surface them.
        return {"genuinely_stuck": list(stale), "idle_reclaimable": []}
    return {"genuinely_stuck": [], "idle_reclaimable": list(stale)}


async def check_regressions(db) -> dict:
    """For every tag with an approved patch AND enough tagger_gt_label
    evidence, compare the CURRENT post_tag state against each label's
    verdict. If accuracy has dropped below the regression_accuracy_
    threshold (tagger_config), re-register the tag as a
    tagger_head_candidate (governed, dedups against an already-open
    candidate for the same tag automatically -- see
    tagger_governed_add_head_candidate's UNIQUE(tag, source_pool_id)
    semantics; source_pool_id is NULL here since this isn't pool-scoped,
    so re-detection across multiple health-check runs will only add one
    row while a NULL-pool candidate for that tag stays open).

    Real regressions this can genuinely catch: a later release's patch
    for a DIFFERENT tag shifted the shared base model's weights enough to
    move this tag's boundary (rare but real for logit-patch heads sharing
    a backbone); a sweep re-tagged posts with a buggy release; or simply
    that the original approval's GT sample was too small and more
    evidence (via gt_sync.py's implicit_review path) revealed the patch
    was never as good as its own reported metrics claimed.

    Split into newly_flagged vs already_tracked (same shape as
    classify_stale_leases): a tag stays below threshold until the
    reconciler actually dispatches and approves a retrain, which itself
    waits on a GT-growth floor (tagger/reconciler.py's pos_grew/neg_grew
    gate) that can take a long time to clear -- reporting it at urgency=2
    EVERY health-check tick for as long as that takes is the exact
    ~102-repeat false-positive pattern already fixed for stale leases
    (incident 2026-07-22, see classify_stale_leases), just for a
    different check. Found live 2026-08-03: half-erect's regression
    candidate (id=77, added 2026-07-21) re-fired this signal every 10min
    for 315h+ while sitting untouched -- the candidate was already open
    and the reconciler was correctly holding it on no_growth (gt_pos=4/
    gt_neg=13 vs required +20/+50 over a zero baseline), so every one of
    those re-reports carried no new information. Only a newly-detected
    regression (no open candidate yet) is worth paging on; an
    already-tracked one stays in the summary for observability, same as
    idle-reclaimable leases.
    """
    config = await load_config(db)
    regression_min_gt_labels = config["regression_min_gt_labels"]
    regression_accuracy_threshold = config["regression_accuracy_threshold"]
    rows = (await db.execute(text(
        "SELECT g.tag, "
        "count(*) FILTER (WHERE (pt.tag_id IS NOT NULL) = (g.verdict = 'positive')) AS matches, "
        "count(*) AS total "
        "FROM tagger_gt_label g "
        "JOIN tag_name tn ON tn.name = g.tag "
        "LEFT JOIN post_tag pt ON pt.post_id = g.post_id AND pt.tag_id = tn.tag_id "
        "WHERE g.tag IN (SELECT tag FROM tagger_patch WHERE status = 'approved') "
        "GROUP BY g.tag HAVING count(*) >= :min_labels"
    ), {"min_labels": regression_min_gt_labels})).mappings().all()

    newly_flagged, already_tracked = [], []
    for r in rows:
        accuracy = r["matches"] / r["total"]
        if accuracy >= regression_accuracy_threshold:
            continue
        entry = {"tag": r["tag"], "accuracy": accuracy, "matches": r["matches"], "total": r["total"]}

        # tagger_governed_add_head_candidate's own dedup check is a plain
        # `source_pool_id = p_source_pool_id` comparison -- NULL-unsafe
        # (NULL = NULL is never true in SQL), so passing NULL here would
        # let a duplicate regression candidate get inserted on every
        # health-check run instead of being deduped. Do the NULL-safe
        # existence check here instead, before calling the governed
        # function at all.
        already_open = (await db.execute(text(
            "SELECT 1 FROM tagger_head_candidate WHERE tag = :tag "
            "AND source_pool_id IS NULL AND status = 'candidate'"
        ), {"tag": r["tag"]})).one_or_none()
        if already_open is not None:
            already_tracked.append(entry)
            continue

        try:
            await db.execute(text(
                "SELECT tagger_governed_add_head_candidate(:tag, NULL, :reason, 'auto:health-regression')"
            ), {"tag": r["tag"],
                "reason": f"tagger-health regression check: accuracy={accuracy:.0%} "
                          f"({r['matches']}/{r['total']}) against tagger_gt_label, "
                          f"below {regression_accuracy_threshold:.0%} threshold"})
            await db.commit()
            newly_flagged.append(entry)
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            print(f"[health] regression re-flag failed for {r['tag']}: {e}", file=sys.stderr)
    return {"newly_flagged": newly_flagged, "already_tracked": already_tracked}


async def run_health_check() -> dict:
    async with async_session() as db:
        consistency = await check_consistency(db)
        reap_silent = await check_reap_silent_nodes(db)
        fleet = await check_fleet(db)
        paused = await check_reconciler_paused(db)
        stale_leases = await check_stale_leases(db)
        regressions = await check_regressions(db)

    if consistency:
        _report("lifecycle-consistency", 2,
                 f"{len(consistency)} tag(s) with lifecycle consistency violations: "
                 + ", ".join(f"{c['tag']}={c['consistency']}" for c in consistency[:5]))
    if reap_silent:
        # **fleet 内で挙動が割れている。** 片方だけが掃除しているのは、
        # 多くの場合コードベースの分裂 (worker が古い)。
        _report("reap-silent-node", 2,
                 f"{len(reap_silent)} node(s) tagging but never reaping while others do: "
                 + ", ".join(f"{r['host']}={r['tagged']}件/削除0" for r in reap_silent[:5]))
    fleet_classes = classify_fleet(fleet)
    fleet_paged = fleet_classes["paged"]
    fleet_quiet = fleet_classes["quiet"]
    if fleet_paged:
        _report("fleet-degraded", 2,
                 f"{len(fleet_paged)} node(s) degraded: "
                 + ", ".join(f"{f['host']}={f['degraded_reason'] or f['liveness']}" for f in fleet_paged))
    if paused:
        _report("reconciler-paused", 3, f"reconciler paused: {paused['halt_reason']}")
    # A merely-expired lock is reclaim-on-acquire self-healing (see
    # classify_stale_leases) -- only escalate the ones that genuinely cannot
    # self-heal because the reconciler is halted. Idle-reclaimable expired
    # locks are NOT reported as a signal at all (they were the ~102-repeat
    # false-positive CRIT of incident 2026-07-22); they stay visible in the
    # returned summary for observability without paging anyone.
    lease_classes = classify_stale_leases(stale_leases, reconciler_paused=paused is not None)
    stuck = lease_classes["genuinely_stuck"]
    idle_reclaimable = lease_classes["idle_reclaimable"]
    if stuck:
        _report("stale-lease", 1,
                 f"{len(stuck)} lease(s) expired >{STALE_LEASE_GRACE_SECONDS}s ago and genuinely stuck "
                 f"(reconciler halted -> reclaim-on-acquire cannot fire): "
                 + ", ".join(l["scope"] for l in stuck[:5]))
    newly_flagged = regressions["newly_flagged"]
    already_tracked = regressions["already_tracked"]
    if newly_flagged:
        _report("tag-regression", 2,
                 f"{len(newly_flagged)} tag(s) newly re-flagged as head_candidate for accuracy regression: "
                 + ", ".join(f"{r['tag']}={r['accuracy']:.0%}" for r in newly_flagged[:5]))

    return {
        "consistency_violations": len(consistency),
        "fleet_degraded": len(fleet),
        "fleet_degraded_paged": len(fleet_paged),
        "fleet_degraded_opportunistic_quiet": len(fleet_quiet),
        "reconciler_paused": paused is not None,
        "stale_leases": len(stale_leases),
        "stale_leases_stuck": len(stuck),
        "stale_leases_idle_reclaimable": len(idle_reclaimable),
        "regressions_newly_flagged": len(newly_flagged),
        "regressions_already_tracked": len(already_tracked),
    }


def main() -> int:
    import asyncio
    summary = asyncio.run(run_health_check())
    print(f"[health] {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
