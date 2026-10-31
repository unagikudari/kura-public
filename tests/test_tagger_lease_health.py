"""Tests for the tagger lease release path + stale-lease health classification
(root-fix for the tagger-health/stale-lease CRIT incident, 2026-07-22).

Two layers:
  1. pure: classify_stale_leases -- an expired lock is reclaim-on-acquire self-
     healing, so it is genuinely-stuck (urgency=1) ONLY when the reconciler is
     halted (nothing will reclaim it); otherwise it is idle-reclaimable and
     must NOT page. This is the exact logic whose absence fired ~102 CRITs.
  2. live-DB (set KURA_TEST_DSN to a kura-role DSN to run; skipped otherwise):
     the migration-0108 acquire -> release round-trip actually removes the
     tagger_lifecycle_lock row, release is fencing-matched + idempotent, and a
     released scope leaves nothing for check_stale_leases to ever flag.
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tagger.health import classify_stale_leases  # noqa: E402


# ── 1. classify_stale_leases (pure) ──────────────────────────────────────────

_LOCK_A = {"scope": "lifecycle:foreskin", "holder_agent_id": "kura", "expires_at": "past"}
_LOCK_B = {"scope": "backlog-sweep", "holder_agent_id": "kura", "expires_at": "past"}
_SMOKE = {"scope": "lifecycle:__smoketest__", "holder_agent_id": "kura", "expires_at": "past"}


def test_no_stale_locks_is_empty_both_ways():
    for paused in (True, False):
        out = classify_stale_leases([], reconciler_paused=paused)
        assert out == {"genuinely_stuck": [], "idle_reclaimable": []}


def test_expired_locks_are_idle_reclaimable_when_reconciler_running():
    # The incident case: dozens of expired lifecycle:* + backlog-sweep +
    # smoketest leftovers while the reconciler is healthy. reclaim-on-acquire
    # (and now the 0108 release path) makes every one self-healing -> NONE may
    # be classified genuinely-stuck, so nothing pages.
    stale = [_LOCK_A, _LOCK_B, _SMOKE]
    out = classify_stale_leases(stale, reconciler_paused=False)
    assert out["genuinely_stuck"] == []
    assert out["idle_reclaimable"] == stale


def test_expired_locks_are_stuck_when_reconciler_halted():
    # The only genuinely-dangerous case: the reconciler is halted, so nothing
    # will re-acquire these scopes -> reclaim-on-acquire cannot fire and the
    # leases really are frozen. These must still be surfaced.
    stale = [_LOCK_A, _LOCK_B]
    out = classify_stale_leases(stale, reconciler_paused=True)
    assert out["genuinely_stuck"] == stale
    assert out["idle_reclaimable"] == []


def test_partition_is_total_and_disjoint():
    stale = [_LOCK_A, _LOCK_B, _SMOKE]
    for paused in (True, False):
        out = classify_stale_leases(stale, reconciler_paused=paused)
        assert len(out["genuinely_stuck"]) + len(out["idle_reclaimable"]) == len(stale)
        stuck_ids = {id(x) for x in out["genuinely_stuck"]}
        idle_ids = {id(x) for x in out["idle_reclaimable"]}
        assert stuck_ids.isdisjoint(idle_ids)


def test_classify_does_not_mutate_input():
    stale = [_LOCK_A, _LOCK_B]
    classify_stale_leases(stale, reconciler_paused=False)
    assert stale == [_LOCK_A, _LOCK_B]


# ── 2. live-DB acquire -> release round-trip (migration 0108) ────────────────

KURA_TEST_DSN = os.environ.get("KURA_TEST_DSN")
_SCOPE = "lifecycle:__pytest_release__"  # covered by the lifecycle:* allowlist


async def _round_trip(dsn: str) -> dict:
    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        # clean any leftover from a previous aborted run (best-effort; only
        # works if this same session holds the token, so ignore failures)
        row = await conn.fetchrow(
            "SELECT fencing_token, expires_at FROM tagger_acquire_lease($1, 60)", _SCOPE
        )
        token = row["fencing_token"]
        present_after_acquire = await conn.fetchval(
            "SELECT count(*) FROM tagger_lifecycle_lock WHERE scope = $1", _SCOPE
        )
        released = await conn.fetchval("SELECT tagger_release_lease($1, $2)", _SCOPE, token)
        present_after_release = await conn.fetchval(
            "SELECT count(*) FROM tagger_lifecycle_lock WHERE scope = $1", _SCOPE
        )
        # idempotent: releasing again (row already gone) returns false, no error
        released_again = await conn.fetchval("SELECT tagger_release_lease($1, $2)", _SCOPE, token)
        return {
            "token": token,
            "present_after_acquire": present_after_acquire,
            "released": released,
            "present_after_release": present_after_release,
            "released_again": released_again,
        }
    finally:
        await conn.close()


@pytest.mark.skipif(not KURA_TEST_DSN, reason="set KURA_TEST_DSN to run live-DB lease tests")
def test_release_lease_round_trip_removes_row_and_is_idempotent():
    r = asyncio.run(_round_trip(KURA_TEST_DSN))
    assert r["present_after_acquire"] == 1        # acquire inserted the lock row
    assert r["released"] is True                  # release removed OUR row
    assert r["present_after_release"] == 0        # ... so no idle leftover remains
    assert r["released_again"] is False           # double-release is a benign no-op


@pytest.mark.skipif(not KURA_TEST_DSN, reason="set KURA_TEST_DSN to run live-DB lease tests")
def test_release_with_wrong_token_is_noop_and_leaves_live_lease_intact():
    async def _wrong_token(dsn: str) -> dict:
        import asyncpg
        conn = await asyncpg.connect(dsn)
        try:
            row = await conn.fetchrow(
                "SELECT fencing_token FROM tagger_acquire_lease($1, 60)", _SCOPE
            )
            token = row["fencing_token"]
            # someone else's stale/forged token must never clobber a live lease
            wrong = await conn.fetchval(
                "SELECT tagger_release_lease($1, $2)", _SCOPE, token + 999999
            )
            still_present = await conn.fetchval(
                "SELECT count(*) FROM tagger_lifecycle_lock WHERE scope = $1", _SCOPE
            )
            # clean up our real lease
            await conn.fetchval("SELECT tagger_release_lease($1, $2)", _SCOPE, token)
            return {"wrong": wrong, "still_present": still_present}
        finally:
            await conn.close()

    r = asyncio.run(_wrong_token(KURA_TEST_DSN))
    assert r["wrong"] is False        # wrong token released nothing
    assert r["still_present"] == 1    # the real live lease is untouched
