"""sweep の監査記録が失敗しても後続を巻き添えにしないことを固定する。

2026-08-04 22:27 の自動 sweep で実際に起きた形:

    [sweep] WARNING: tagger_sweep_run への記録に失敗: ... 関数が存在しません
    [sweep] WARNING: lease release failed: ... 現在のトランザクションがアボートしました

記録用の governed 関数が primary に未適用だったため INSERT が失敗し、例外を握るだけで
**rollback しなかった**ので session が InFailedSQLTransactionError のまま残り、
`finally` の release_lease() まで巻き添えで失敗した。lease が解放されないのは
stale-lease CRIT (2026-07-22 のインシデント) の形。

**fail-open にするなら、後続が使える状態まで戻すのが fail-open。** 例外を握って
先へ進めるだけでは、壊れた状態を次の処理に押し付けているだけになる。
"""
import asyncio
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


class _FakeDB:
    """execute が失敗する DB。rollback が呼ばれたかを記録する。"""

    def __init__(self, fail: bool):
        self.fail = fail
        self.rolled_back = False
        self.committed = False

    async def execute(self, *_a, **_k):
        if self.fail:
            raise RuntimeError("関数 tagger_governed_record_sweep_run は存在しません")
        return None

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


SUMMARY = {"active_release_id": 12, "active_generation": 3, "ceiling": 5000,
           "flipped": 5000, "dry_run": False, "status": "completed"}


def _record_run():
    # sweep.py は import 時に api.db.session を要求するので、モジュール全体では
    # なく関数だけを取り出して検証する (DB 接続を張らずに済む)。
    import importlib.util
    src = (REPO / "tagger" / "sweep.py").read_text()
    start = src.index("async def _record_run(")
    end = src.index("async def run_sweep(")
    ns = {"text": lambda q: q, "sys": sys}
    exec(compile(src[start:end], "sweep_record", "exec"), ns)  # noqa: S102
    return ns["_record_run"]


def test_records_on_success():
    db = _FakeDB(fail=False)
    asyncio.run(_record_run()(db, SUMMARY, None))
    assert db.committed
    assert not db.rolled_back


def test_failure_rolls_back_so_the_session_stays_usable():
    """**本題**: 記録が失敗したら rollback する。しないと session が
    InFailedSQLTransactionError のまま残り、finally の release_lease() が
    巻き添えで失敗して stale lease になる。"""
    db = _FakeDB(fail=True)
    asyncio.run(_record_run()(db, SUMMARY, None))   # 例外を外に出さない (fail-open)
    assert db.rolled_back, "記録失敗時に rollback していない — 後続が巻き添えになる"


def test_failure_does_not_raise():
    """監査は sweep の目的ではないので、記録失敗で sweep を落とさない。"""
    db = _FakeDB(fail=True)
    asyncio.run(_record_run()(db, SUMMARY, None))  # 例外が出れば pytest が落とす


def test_rollback_failure_is_also_swallowed():
    """rollback 自体が失敗しても sweep は続く (二重障害でも落とさない)。"""
    class _Worse(_FakeDB):
        async def rollback(self):
            raise RuntimeError("rollback すら失敗")

    asyncio.run(_record_run()(_Worse(fail=True), SUMMARY, None))


# ── status の写像 ────────────────────────────────────────────────────────
# tagger_sweep_run の CHECK は running/completed/aborted_toctou/failed のみ。
# "dry_run" と "no_candidates" は status ではなく **別の次元** で、表は既に
# dry_run boolean と flipped_count を持っている。初版はそれらを status に
# 押し込んで CheckViolationError で弾かれていた (2026-08-04 実測) — 制約を
# 広げるのではなく、2 つの次元を混ぜていた側を直す。

_ALLOWED = {"running", "completed", "aborted_toctou", "failed"}


def _recorded_status(summary_status: str) -> str:
    captured = {}

    class _Cap(_FakeDB):
        async def execute(self, _q, params=None):
            captured.update(params or {})

    s = dict(SUMMARY, status=summary_status)
    asyncio.run(_record_run()(_Cap(fail=False), s, None))
    return captured["status"]


@pytest.mark.parametrize("given,expected", [
    ("completed", "completed"),
    ("dry_run", "completed"),
    ("no_candidates", "completed"),
    ("aborted_toctou", "aborted_toctou"),
])
def test_status_is_mapped_into_the_tables_vocabulary(given, expected):
    assert _recorded_status(given) == expected


@pytest.mark.parametrize("given", ["completed", "dry_run", "no_candidates",
                                   "aborted_toctou", "なにか未知の値"])
def test_recorded_status_always_satisfies_the_check_constraint(given):
    """未知の status が来ても CHECK を破らない — 破ると記録ごと落ちる。"""
    assert _recorded_status(given) in _ALLOWED


def test_unknown_status_becomes_failed():
    """黙って completed にしない。分からないものを成功として記録しない。"""
    assert _recorded_status("なにか未知の値") == "failed"
