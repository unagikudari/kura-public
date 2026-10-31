"""structural_context() の replica schema drift 検査 (tagger/vnext_runner.py)。

この検査は 2026-08-02 に「alembic 帳簿はノード間で不一致 (実 schema とも乖離)」
という理由で **version 一致を諦め「両方が読める」だけ** に弱められていた。
実測するとその不一致は **間違った表を見ていたから** だった:

    素の alembic_version        trainer 5b5c940b4e78 / primary 0078   (szurubooru の遺物)
    kura_alembic_version        trainer 0126        / primary 0126    (kura の本物)

alembic.ini と api/alembic/env.py は version_table=kura_alembic_version を明示して
いる。壊れていたのは帳簿ではなく参照先で、そのせいで drift 検査が実質無効に
なっていた。

ここで固定するのは「直したあと本当に drift を捕まえるか」。版が食い違えば False、
一致すれば True、そもそも検査が走らなければ **key を立てない** (走らなかったことを
「一致していた」と読ませない)。
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tagger import vnext_runner as vr  # noqa: E402


@pytest.fixture()
def stub(monkeypatch):
    """kura_read / kura_read_primary を差し替えて DB なしで検査ロジックを回す。"""
    state = {"local": "0126", "primary": "0126", "constraint": True,
             "local_raises": False, "primary_raises": False}

    def fake_read(sql, args=()):
        if state["local_raises"]:
            raise OSError("replica unreachable")
        # 帳簿名を問わず版を返す。旧実装 (素の alembic_version を読む) にも
        # ちゃんと応答させないと、「stub が空を返したから False になった」だけの
        # テストになり、drift を検出しているのか判別できない。
        if "version_num" in sql:
            return [(state["local"],)] if state["local"] else []
        if "pg_constraint" in sql:
            return [(1,)] if state["constraint"] else []
        if "tag_name" in sql:
            return [(1,)]
        if "tagger_release_active" in sql:
            return [(1,)]
        return []

    def fake_read_primary(sql, args=()):
        if state["primary_raises"]:
            raise OSError("primary unreachable")
        return [(state["primary"],)] if state["primary"] else []

    monkeypatch.setattr(vr, "kura_read", fake_read)
    monkeypatch.setattr(vr, "kura_read_primary", fake_read_primary)
    monkeypatch.setattr(vr, "log", lambda *a, **k: None)
    return state


def test_matching_versions_pass(stub):
    ctx = vr.structural_context("smegma")
    assert ctx["replica_schema_matches"] is True


def test_version_drift_is_detected(stub):
    """本題: 版が食い違えば False。旧実装は「両方が読める」だけを見ていたので
    ここが True のまま通っていた。"""
    stub["primary"] = "0127"
    ctx = vr.structural_context("smegma")
    assert ctx["replica_schema_matches"] is False


def test_drift_is_logged_with_both_versions(stub, monkeypatch):
    seen = []
    monkeypatch.setattr(vr, "log", lambda ev, **kw: seen.append((ev, kw)))
    stub["primary"] = "0130"
    vr.structural_context("smegma")
    drift = [kw for ev, kw in seen if ev == "structural.schema_version_drift"]
    # 完全一致で固定しない — 情報を**足す**変更まで失敗になる。
    # 2026-08-08 に向き (verdict) を足したときここで落ちた。守りたいのは
    # 「どちらがどの版かが出ること」で、payload の形そのものではない。
    assert len(drift) == 1, "drift が 1 件記録されること"
    assert drift[0]["local"] == "0126" and drift[0]["primary"] == "0130", \
        "どちらがどの版かを出さないと、届いた人が調べ直すことになる"
    assert drift[0].get("verdict") == "canonical_ahead", \
        "向きを出さないと、危険な向きか推奨される順序かが分からない"


def test_missing_constraint_still_fails_even_when_versions_agree(stub):
    """版一致は schema 一致を意味しない。published table への ADD COLUMN が
    replica に届いていない形は版が同じでも起こる。"""
    stub["constraint"] = False
    ctx = vr.structural_context("smegma")
    assert ctx["replica_schema_matches"] is False


def test_unreadable_primary_leaves_the_key_absent(stub):
    """検査が走らなかったことを「一致していた」と読ませない。key を立てなければ
    vnext_escalate 側は「観測できていない前提」として扱う。"""
    stub["primary_raises"] = True
    ctx = vr.structural_context("smegma")
    assert "replica_schema_matches" not in ctx


def test_unreadable_local_leaves_the_key_absent(stub):
    stub["local_raises"] = True
    ctx = vr.structural_context("smegma")
    assert "replica_schema_matches" not in ctx


def test_empty_ledger_is_not_a_pass(stub):
    """帳簿が空 (行が無い) を一致と読まない。"""
    stub["primary"] = ""
    ctx = vr.structural_context("smegma")
    assert ctx["replica_schema_matches"] is False


def test_reads_the_kura_ledger_not_the_szurubooru_one(monkeypatch):
    """参照先そのものを固定する。素の alembic_version に戻したら落ちる。"""
    seen = []

    def fake_read(sql, args=()):
        seen.append(sql)
        if "kura_alembic_version" in sql:
            return [("0126",)]
        if "pg_constraint" in sql:
            return [(1,)]
        return [(1,)]

    monkeypatch.setattr(vr, "kura_read", fake_read)
    monkeypatch.setattr(vr, "kura_read_primary", lambda sql, args=(): seen.append(sql) or [("0126",)])
    monkeypatch.setattr(vr, "log", lambda *a, **k: None)
    vr.structural_context("smegma")

    ledger_reads = [s for s in seen if "version_num" in s]
    assert ledger_reads, "帳簿を一度も読んでいない"
    for s in ledger_reads:
        assert "kura_alembic_version" in s, f"kura の帳簿ではない表を読んでいる: {s}"
