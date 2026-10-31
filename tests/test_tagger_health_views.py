"""Regression tests for migration 0119 — the tagger-health false-positive
root fix (broker signal lifecycle-consistency ×1478, 2026-07-31).

The logic under test lives entirely in SQL view definitions, and the views
read governed tables a kura-role test session cannot insert fixtures into
(0079). So these are live-DB invariant tests (set KURA_TEST_DSN to run,
same convention as test_tagger_lease_health.py): each asserts a property
the 0119 view definitions make impossible to violate REGARDLESS of table
contents, so they are deterministic, not data-dependent.

  1. tagger_fleet can never surface a smoketest heartbeat -- the join
     itself excludes worker_id LIKE '%smoketest%' (the six 2026-07-20
     fixture corpses that inflated fleet-degraded).
  2. tagger_lifecycle can never flag the living-cycle shape as a
     consistency violation: approved patch + recorded approved event +
     latest event newer than the approved patch (= regression
     re-candidacy per user directive 2026-07-20) must have consistency
     IS NULL.
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

KURA_TEST_DSN = os.environ.get("KURA_TEST_DSN")


async def _fetchval(dsn: str, sql: str):
    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchval(sql)
    finally:
        await conn.close()


@pytest.mark.skipif(not KURA_TEST_DSN, reason="set KURA_TEST_DSN to run live-DB view tests")
def test_fleet_view_never_surfaces_smoketest_workers():
    n = asyncio.run(_fetchval(
        KURA_TEST_DSN,
        "SELECT count(*) FROM tagger_fleet WHERE worker_id LIKE '%smoketest%'",
    ))
    assert n == 0


@pytest.mark.skipif(not KURA_TEST_DSN, reason="set KURA_TEST_DSN to run live-DB view tests")
def test_fleet_viewdef_keeps_smoketest_exclusion():
    # Guard against a future view rewrite silently dropping the 0119 filter
    # (the views are recreated wholesale by migrations, so a rewrite that
    # starts from the 0078 text would reintroduce the corpses).
    viewdef = asyncio.run(_fetchval(
        KURA_TEST_DSN, "SELECT pg_get_viewdef('tagger_fleet'::regclass)"
    ))
    assert "smoketest" in viewdef


@pytest.mark.skipif(not KURA_TEST_DSN, reason="set KURA_TEST_DSN to run live-DB view tests")
def test_lifecycle_living_cycle_is_never_a_consistency_violation():
    # The exact false-positive shape of the ×1478 signal: an approved tag
    # legitimately back in candidate/training for a new cycle. Under the
    # 0119 definition such a row can never carry a non-NULL consistency.
    n = asyncio.run(_fetchval(KURA_TEST_DSN, """
        WITH latest_event AS (
            SELECT DISTINCT ON (tag) tag, to_stage, occurred_at
            FROM tagger_lifecycle_event
            ORDER BY tag, occurred_at DESC, id DESC
        ),
        latest_patch AS (
            SELECT DISTINCT ON (tag) tag, status, created_at
            FROM tagger_patch
            ORDER BY tag, version DESC
        )
        SELECT count(*)
        FROM tagger_lifecycle v
        JOIN latest_event le ON le.tag = v.tag
        JOIN latest_patch lp ON lp.tag = v.tag
        WHERE v.consistency IS NOT NULL
          AND lp.status = 'approved'
          AND le.occurred_at >= lp.created_at
          AND EXISTS (SELECT 1 FROM tagger_lifecycle_event e
                      WHERE e.tag = v.tag AND e.to_stage = 'approved')
    """))
    assert n == 0


@pytest.mark.skipif(not KURA_TEST_DSN, reason="set KURA_TEST_DSN to run live-DB view tests")
def test_lifecycle_view_still_flags_unrecorded_approval():
    # The true-bypass detection must survive the false-positive fix: any
    # approved patch whose tag has NO approved event ever recorded must
    # still be flagged. (Set-difference formulation: rows matching the
    # bypass shape but carrying consistency IS NULL must not exist.)
    n = asyncio.run(_fetchval(KURA_TEST_DSN, """
        WITH latest_patch AS (
            SELECT DISTINCT ON (tag) tag, status
            FROM tagger_patch
            ORDER BY tag, version DESC
        )
        SELECT count(*)
        FROM tagger_lifecycle v
        JOIN latest_patch lp ON lp.tag = v.tag
        WHERE lp.status = 'approved'
          AND NOT EXISTS (SELECT 1 FROM tagger_lifecycle_event e
                          WHERE e.tag = v.tag AND e.to_stage = 'approved')
          AND v.consistency IS NULL
    """))
    assert n == 0


# ── migration 0120: provider 判定を per-host expected_provider に置き換え ──
#
# 旧判定 `provider ILIKE '%cpu%' AND profile='full'` は両方向に誤っていた:
# primary (CPU が定常構成) を恒久 degraded にし、かつ profile='wd14' である
# nodec/desk -- メッセージが名指しする 2026-07-20 の退行当事者 -- を検出でき
# なかった。以下は「偽陽性が戻らない」と「本物の退行は依然検出する」を両方
# 固定する。前者だけを書くと、判定を丸ごと消しても通ってしまう。

@pytest.mark.skipif(not KURA_TEST_DSN, reason="set KURA_TEST_DSN to run live-DB view tests")
def test_fleet_viewdef_uses_expected_provider_not_profile_inference():
    """view が profile から GPU 有無を推測していないこと (旧ヒューリスティックの再来防止)。"""
    vd = asyncio.run(_fetchval(
        KURA_TEST_DSN, "SELECT pg_get_viewdef('tagger_fleet'::regclass)"
    ))
    assert "expected_provider" in vd, "degraded 判定が expected_provider を参照していない"
    assert "full-ensemble profile" not in vd, "旧 profile ベースのヒューリスティックが残っている"


@pytest.mark.skipif(not KURA_TEST_DSN, reason="set KURA_TEST_DSN to run live-DB view tests")
def test_provider_mismatch_is_always_degraded():
    """**本物の退行の検知を守る不変条件**: expected が設定されたホストが別の
    provider を報告していたら、必ず degraded_reason が付く。expected=CUDA の
    nodec/desk が CPU に落ちた場合 (2026-07-20 と同型) がまさにこれに当たる。
    データ内容に依らず view の CASE により 0 が保証される。"""
    n = asyncio.run(_fetchval(KURA_TEST_DSN, """
        SELECT count(*) FROM tagger_fleet
        WHERE worker_id IS NOT NULL
          AND expected_provider IS NOT NULL
          AND reported_provider IS DISTINCT FROM expected_provider
          AND degraded_reason IS NULL
    """))
    assert n == 0, f"{n} 行が provider 不一致なのに degraded になっていない"


@pytest.mark.skipif(not KURA_TEST_DSN, reason="set KURA_TEST_DSN to run live-DB view tests")
def test_provider_match_is_never_degraded():
    """偽陽性の再来防止: 期待どおりの provider を報告しているホストは degraded に
    しない (primary の CPU 定常運用 = user 容認 2026-07-31)。"""
    n = asyncio.run(_fetchval(KURA_TEST_DSN, """
        SELECT count(*) FROM tagger_fleet
        WHERE expected_provider IS NOT NULL
          AND reported_provider = expected_provider
          AND degraded_reason IS NOT NULL
    """))
    assert n == 0, f"{n} 行が期待どおりなのに degraded になっている"


@pytest.mark.skipif(not KURA_TEST_DSN, reason="set KURA_TEST_DSN to run live-DB view tests")
def test_unset_expectation_never_degrades():
    """expected_provider IS NULL = 「provider について意見を持たない」。
    nodeb がこの状態 (user 判断待ち、migration 0120 docstring 参照)。
    NULL の間は provider 退行を検出しないという fail-open を明示的に固定する。"""
    n = asyncio.run(_fetchval(KURA_TEST_DSN, """
        SELECT count(*) FROM tagger_fleet
        WHERE worker_id IS NOT NULL
          AND expected_provider IS NULL
          AND degraded_reason IS NOT NULL
    """))
    assert n == 0, f"{n} 行が期待未設定なのに degraded になっている"


# ── migration 0126: opportunistic host が単なる stale で毎tick再発火しない ──
#
# 実地 (2026-08-03): tagger-health/fleet-degraded が 333h13m/count 1520 で
# 発火し続けていた。原因は desk (opportunistic worker, CLAUDE.md node-role
# table) が単に stale (heartbeat 1h超過、degraded_reason なし) なだけなのに
# health.py が毎 10分 tick 無条件で page していたこと -- stale-lease
# (2026-07-22) / tag-regression (2026-08-03 v1.120.1) と同型の再発。
# classify_fleet() のロジックを直接 import してテストする (view 側は列を
# 持つだけで判定ロジックは Python 側にあるため)。

def test_opportunistic_stale_is_quiet_not_paged():
    from tagger.health import classify_fleet
    fleet = [
        {"host": "desk", "liveness": "stale", "degraded_reason": None, "opportunistic": True},
    ]
    result = classify_fleet(fleet)
    assert result["paged"] == []
    assert result["quiet"] == fleet


def test_opportunistic_but_missing_still_pages():
    from tagger.health import classify_fleet
    fleet = [
        {"host": "desk", "liveness": "missing", "degraded_reason": None, "opportunistic": True},
    ]
    result = classify_fleet(fleet)
    assert result["paged"] == fleet
    assert result["quiet"] == []


def test_opportunistic_with_degraded_reason_still_pages():
    """接続中の本物の退行 (provider mismatch 等) は opportunistic でも黙らせない。"""
    from tagger.health import classify_fleet
    fleet = [
        {"host": "desk", "liveness": "active", "degraded_reason": "provider mismatch: expected CUDAExecutionProvider but reporting CPUExecutionProvider -- accelerator regression or an out-of-date expectation", "opportunistic": True},
    ]
    result = classify_fleet(fleet)
    assert result["paged"] == fleet
    assert result["quiet"] == []


def test_non_opportunistic_stale_still_pages():
    """常駐ホスト (primary/nodeb/nodec) は従来どおり stale で page する。"""
    from tagger.health import classify_fleet
    fleet = [
        {"host": "nodec", "liveness": "stale", "degraded_reason": None, "opportunistic": False},
    ]
    result = classify_fleet(fleet)
    assert result["paged"] == fleet
    assert result["quiet"] == []


@pytest.mark.skipif(not KURA_TEST_DSN, reason="set KURA_TEST_DSN to run live-DB view tests")
def test_fleet_view_exposes_opportunistic_column():
    n = asyncio.run(_fetchval(KURA_TEST_DSN, "SELECT count(*) FROM tagger_fleet WHERE opportunistic IS NULL"))
    assert n == 0, "opportunistic は NOT NULL DEFAULT false のはず"
