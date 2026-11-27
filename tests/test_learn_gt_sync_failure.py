"""#161: after a failed GT sync, nothing trains, reviews, promotes or publishes
against the GT it left behind -- in the learning pass and in the 2-minute
qualification pass that does not run the sync itself."""
import asyncio
import contextlib
import json
import types

import pytest

from tagger import learn


@pytest.fixture
def calls(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(learn, "STATE", tmp_path / "kura-tagger")
    seen = []

    async def record(name, value=None):
        seen.append(name)
        return value

    from api.tagger_core import gt_sync
    from tagger import auto_review, learning_execution, reconciler
    monkeypatch.setattr(reconciler, "reconcile_once", lambda: record("reconcile", {}))
    monkeypatch.setattr(reconciler, "settle_judged_training", lambda db: record("settle_judged", []))
    monkeypatch.setattr(learning_execution, "settle_consumed", lambda args: record("settle_consumed"))
    monkeypatch.setattr(learn, "measurement_pass", lambda args: record("measurement", 0))
    monkeypatch.setattr(learn, "refresh_evaluations", lambda: record("refresh_evaluations", False))
    monkeypatch.setattr(learn, "run_candidate_qualification", lambda: record("publication", False))
    monkeypatch.setattr(auto_review, "run", lambda **kw: record(
        "auto_review(review_only)" if kw.get("review_only") else "auto_review(promote)", {}))

    @contextlib.asynccontextmanager
    async def session():
        yield None
    monkeypatch.setattr(learn, "async_session", session)

    def set_sync(summary):
        monkeypatch.setattr(gt_sync, "run_sync", lambda: record("gt_sync", summary))
    return seen, set_sync, tmp_path / "kura-tagger" / "gt-sync-status.json"


REMOTE = types.SimpleNamespace(remote_config="worker.json", undeclared_domain="always")
LOCAL = types.SimpleNamespace(remote_config=None, undeclared_domain="always")
FAILED = {"_global": {"error": "deadlock detected"}}
CLEAN = {"_global": {"negatives_synced": 1, "positives_synced": 0, "candidates_flagged": []}}


@pytest.mark.parametrize("args", [REMOTE, LOCAL])
def test_a_failed_sync_stops_the_whole_pass(calls, args):
    seen, set_sync, status = calls
    set_sync(FAILED)
    assert asyncio.run(learn.run_pass(args)) == 1
    assert seen == ["gt_sync"]  # no training, review, promotion or publication
    assert json.loads(status.read_text())["failed"] == ["_global"]


def test_a_clean_sync_runs_the_whole_pass(calls):
    seen, set_sync, _ = calls
    set_sync(CLEAN)
    assert asyncio.run(learn.run_pass(LOCAL)) == 0
    assert seen == ["gt_sync", "reconcile", "settle_consumed", "measurement",
                    "auto_review(promote)", "publication", "settle_judged"]
    seen.clear()
    assert asyncio.run(learn.run_pass(REMOTE)) == 0
    assert seen == ["gt_sync", "reconcile", "settle_consumed", "measurement",
                    "refresh_evaluations", "auto_review(review_only)", "publication", "settle_judged"]


def test_the_qualification_pass_waits_for_a_successful_sync(calls):
    from api.tagger_core import gt_sync

    seen, _, status = calls
    gt_sync.record_status(FAILED, status)
    assert asyncio.run(learn.qualification_pass()) == 1
    assert seen == []
    gt_sync.record_status(CLEAN, status)
    assert asyncio.run(learn.qualification_pass()) == 0
    assert seen == ["refresh_evaluations", "auto_review(review_only)", "publication"]


def test_no_recorded_sync_yet_does_not_block_but_a_broken_or_stale_one_does(calls):
    import time
    from api.tagger_core import gt_sync

    seen, _, status = calls
    assert not status.exists()
    assert asyncio.run(learn.qualification_pass()) == 0
    status.write_text("{broken")
    assert gt_sync.last_status(status)["ok"] is False
    status.write_text(json.dumps({"ok": True, "failed": [], "at": time.time() - gt_sync.STATUS_FRESH_SECONDS - 1}))
    assert gt_sync.last_status(status)["failed"] == ["_status_stale"]
    seen.clear()
    assert asyncio.run(learn.qualification_pass()) == 1 and seen == []


def test_a_sync_that_raises_does_not_leave_an_older_ok(calls, monkeypatch):
    from api.tagger_core import gt_sync

    seen, _, status = calls
    gt_sync.record_status(CLEAN, status)

    async def dies():
        raise ConnectionError("server closed the connection")
    monkeypatch.setattr(gt_sync, "run_sync", dies)
    with pytest.raises(ConnectionError):
        asyncio.run(learn.run_pass(REMOTE))
    assert gt_sync.last_status(status)["ok"] is False
    assert asyncio.run(learn.qualification_pass()) == 1
