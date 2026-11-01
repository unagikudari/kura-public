"""/api/activity groups new posts by pool and passes on what extensions report; it never
reaches further back than its window, and refuses a time without an offset."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


class _Result:
    def __init__(self, rows): self._rows = rows
    def all(self): return self._rows


class _Db:
    def __init__(self, posts, names):
        self.posts, self.names, self.bounds = posts, names, None
    async def execute(self, statement, params):
        if "FROM post p" in str(statement):
            self.bounds = params
            return _Result(self.posts)
        return _Result([(i, self.names[i]) for i in params["ids"] if i in self.names])


def _client(db, events=()):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from api import extension_hooks
    from api.auth import get_current_user
    from api.db.session import get_db
    from api.errors import KuraError
    from api.routers.activity import router
    app = FastAPI()
    app.add_exception_handler(KuraError, lambda request, e: JSONResponse(e.body(), status_code=e.status))
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(name="person")
    app.dependency_overrides[get_db] = lambda: db

    async def activity(db, since, now): return list(events)
    mp = pytest.MonkeyPatch(); mp.setattr(extension_hooks, "activity", activity)
    return TestClient(app), mp


def test_the_first_call_only_fixes_the_start():
    client, mp = _client(_Db([(1, datetime(2026, 11, 1), 5)], {5: "pool"}))
    try:
        body = client.get("/api/activity").json()
    finally:
        mp.undo()
    assert body["events"] == [] and datetime.fromisoformat(body["now"]).tzinfo


def test_new_posts_are_one_event_per_pool_with_their_ids():
    t = datetime(2026, 11, 1)
    db = _Db([(10, t, 5), (11, t, None), (12, t, 5)], {5: "Some pool"})
    client, mp = _client(db, [{"key": "ext:x:failed", "kind": "extension", "level": "error", "text": "failed"}])
    try:
        since = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        events = client.get("/api/activity", params={"since": since}).json()["events"]
    finally:
        mp.undo()
    pool, loose, ext = events
    assert pool == {"key": "posts:5:12", "kind": "posts_added", "level": "info", "pool_id": 5,
                    "pool_name": "Some pool", "count": 2, "post_ids": [10, 12]}
    assert loose["pool_id"] is None and loose["post_ids"] == [11]
    assert ext["key"] == "ext:x:failed"
    # creation_time is naive UTC, so the bounds are too
    assert db.bounds["since"].tzinfo is None and db.bounds["now"] - db.bounds["since"] < timedelta(minutes=2)


def test_a_long_sleep_is_not_replayed_and_a_naive_time_is_refused():
    db = _Db([], {})
    client, mp = _client(db)
    try:
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        assert client.get("/api/activity", params={"since": old}).status_code == 200
        assert db.bounds["now"] - db.bounds["since"] <= timedelta(minutes=15, seconds=1)
        r = client.get("/api/activity", params={"since": "2026-11-01T00:00:00"})
    finally:
        mp.undo()
    assert r.status_code == 422 and r.json()["error"] == "since_without_timezone"


def test_an_extension_event_is_checked_and_namespaced(monkeypatch):
    import asyncio
    from api import extension_hooks
    module = SimpleNamespace()
    monkeypatch.setattr(extension_hooks, "installed", lambda: {"ext": {}})
    monkeypatch.setattr(extension_hooks, "load", lambda name: module)

    async def good(db, since, now): return [{"key": "k", "level": "error", "text": "t", "kind": "posts_added"}]
    module.activity = good
    [event] = asyncio.run(extension_hooks.activity(None, None, None))
    assert event == {"key": "ext:k", "level": "error", "text": "t", "kind": "extension", "extension": "ext", "href": None}

    async def bad(db, since, now): return [{"key": "k", "level": "loud", "text": "t"}]
    module.activity = bad
    with pytest.raises(RuntimeError):
        asyncio.run(extension_hooks.activity(None, None, None))


def test_a_full_answer_is_continued_from_its_last_post_even_within_one_instant(monkeypatch):
    from api.routers import activity
    monkeypatch.setattr(activity, "MAX_POSTS", 2)
    last = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=30)
    db = _Db([(1, last, None), (2, last, None)], {})
    client, mp = _client(db)
    try:
        since = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        body = client.get("/api/activity", params={"since": since}).json()
        assert body["more"] is True and body["after_id"] == 2
        assert datetime.fromisoformat(body["now"]) == last.replace(tzinfo=timezone.utc)
        assert body["events"][0]["post_ids"] == [1, 2]
        client.get("/api/activity", params={"since": body["now"], "after_id": body["after_id"]})
    finally:
        mp.undo()
    assert db.bounds["after_id"] == 2 and db.bounds["since"] == last
