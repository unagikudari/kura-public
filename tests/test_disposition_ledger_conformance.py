"""issue #55 P1: disposition ledger の共通契約 conformance test。

**なぜこのテストが要るか** (Phase B rev2, codex 反証②への回答):
P1 の disposition ledger は FK を保つために domain 別テーブルで実装する。その
代償は「契約面 (append-only / defer の期限必須 / actor 型 / fold view / governed
書き込み) を domain ごとに複製する」爆発 — それを防ぐのがこの parametrized test。
**同族テーブルを増やす = LEDGERS に 1 行足すだけ**で全契約の検証が付いてくる。
契約を満たさない同族テーブルは CI (DB のある環境) で落ちる。

DB に届かない環境では skip する (このテストの対象は information_schema の実物で
あり、ファイルの静的解析ではない — green は「検証した」を意味する必要がある)。
"""
from __future__ import annotations

import pytest

psycopg2 = pytest.importorskip("psycopg2")

# ── enrollment list — a new domain disposition table adds ONE row here ──
LEDGERS = [
    {
        "table": "tagger_candidate_disposition",
        "fold_view": "tagger_candidate_disposition_current",
        "defer_check": "ck_tcd_defer_needs_expiry",
        "cond_check": "ck_tcd_cond_versioned",
    },
]


@pytest.fixture(scope="module")
def db():
    try:
        conn = psycopg2.connect(host="127.0.0.1", dbname="kura", user="kura",
                                password="kura", connect_timeout=5)
    except Exception:  # noqa: BLE001
        pytest.skip("kura DB not reachable — conformance runs where the schema lives")
    cur = conn.cursor(); cur.execute("SELECT count(*) FROM pg_subscription")
    if cur.fetchone()[0]:
        # A replica carries the published TABLES; triggers and functions are
        # publisher-only (REPLICA_DDL = None, #60). The contract this test pins
        # lives on the canonical node — on a replica the assertion measures the
        # wrong object and was red on every replica (2026-09-05).
        conn.close()
        pytest.skip("replica DB (pg_subscription present) — triggers are publisher-only; conformance runs on canonical")
    yield conn
    conn.close()


def _q(db, sql: str, *args):
    cur = db.cursor()
    cur.execute(sql, args)
    return cur.fetchall()


@pytest.mark.parametrize("ledger", LEDGERS, ids=[x["table"] for x in LEDGERS])
def test_ledger_contract(db, ledger):
    t = ledger["table"]
    if not _q(db, "SELECT 1 FROM pg_tables WHERE tablename = %s", t):
        pytest.skip(f"{t} not migrated yet on this DB")

    # 1. append-only: an UPDATE/DELETE-blocking trigger exists
    trig = {r[0] for r in _q(db,
        "SELECT tgname FROM pg_trigger WHERE tgrelid = %s::regclass AND NOT tgisinternal", t)}
    assert any("append_only" in name for name in trig), \
        f"{t}: append-only trigger missing — a decision row must never be rewritten (P6/P7)"
    # 2. governed-only writes
    assert any("governed_only" in name for name in trig), \
        f"{t}: governed-only trigger missing — direct writes bypass the validators"

    checks = {r[0]: r[1] for r in _q(db,
        "SELECT conname, pg_get_expr(conbin, conrelid) FROM pg_constraint "
        "WHERE conrelid = %s::regclass AND contype = 'c'", t)}
    # 3. permanent defer is impossible by type
    assert ledger["defer_check"] in checks, \
        f"{t}: defer-needs-expiry CHECK missing — a defer without deadline/condition is 忘却装置"
    # 4. conditions carry a version (P2: unevaluable must be detectable, not guessed)
    assert ledger["cond_check"] in checks, \
        f"{t}: release_condition/cond_version mutual CHECK missing"
    # 5. closed vocabularies as CHECKs, not conventions
    assert any("disposition" in e for e in checks.values()), \
        f"{t}: disposition CHECK missing (5 値は型で閉じる)"
    assert any("actor_type" in e for e in checks.values()), \
        f"{t}: actor_type CHECK missing (human/policy/automation の型化 — P7 の土台)"

    # 6. fold view exists — current state is a query, never a second writable column
    assert _q(db, "SELECT 1 FROM pg_views WHERE viewname = %s", ledger["fold_view"]), \
        f"{t}: fold view {ledger['fold_view']} missing — 現在値の正本は fold (P6)"
