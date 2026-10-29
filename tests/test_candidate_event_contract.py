"""0155 tagger_candidate_event — 遷移台帳の契約と reason 追記廃止の固定。

**なぜこのテストが要るか** (census #55 §1-1): 遷移履歴が head_candidate.reason に
無制限追記され (155/238 行、最大 3660 文字、同一遷移の重複追記あり)、正規の
event 台帳が無かった。0155 で governed fn 5 本から追記を除去し event 台帳へ —
このテストは (a) 台帳の契約 (append-only / governed / CHECK)、(b) **fn 本文に
reason 連結が戻っていないこと** (再発ゲート) を固定する。
"""
from __future__ import annotations

import pytest

psycopg2 = pytest.importorskip("psycopg2")

APPENDING_FNS = (
    "tagger_governed_dismiss_head_candidate",
    "tagger_governed_release_head_candidate",
    "tagger_governed_reopen_headless_candidate",
    "tagger_governed_reopen_overdetected_candidate",
)


@pytest.fixture(scope="module")
def db():
    try:
        conn = psycopg2.connect(host="127.0.0.1", dbname="kura", user="kura",
                                password="kura", connect_timeout=5)
    except Exception:  # noqa: BLE001
        pytest.skip("kura DB not reachable")
    yield conn
    conn.close()


def _q(db, sql, *args):
    cur = db.cursor()
    cur.execute(sql, args)
    return cur.fetchall()


def test_event_table_contract(db):
    if not _q(db, "SELECT 1 FROM pg_tables WHERE tablename='tagger_candidate_event'"):
        pytest.skip("0155 not migrated yet on this DB")
    trig = {r[0] for r in _q(db,
        "SELECT tgname FROM pg_trigger WHERE tgrelid='tagger_candidate_event'::regclass AND NOT tgisinternal")}
    assert any("append_only" in t for t in trig), "遷移 event は不変 (T の定義)"
    assert any("governed_only" in t for t in trig)
    checks = {r[0] for r in _q(db,
        "SELECT conname FROM pg_constraint WHERE conrelid='tagger_candidate_event'::regclass AND contype='c'")}
    assert "ck_tce_held_has_class" in checks, "held は必ず理由クラスを持つ (相互 CHECK)"


def test_no_governed_fn_appends_to_reason_anymore(db):
    """再発ゲート: fn 本文の `reason = reason ||` が 0 件であること。

    0155 以前は 4 本がこれをやって 3660 文字の text-event-log を作った。"""
    if not _q(db, "SELECT 1 FROM pg_tables WHERE tablename='tagger_candidate_event'"):
        pytest.skip("0155 not migrated yet on this DB")
    offenders = []
    for fn in APPENDING_FNS:
        src = _q(db, "SELECT prosrc FROM pg_proc WHERE proname=%s", fn)
        assert src, f"{fn} が存在しない"
        for (body,) in src:
            if "reason = reason ||" in body or "reason || '" in body:
                offenders.append(fn)
    assert not offenders, \
        f"reason への遷移追記が復活している: {offenders} — 遷移は tagger_candidate_event へ"
