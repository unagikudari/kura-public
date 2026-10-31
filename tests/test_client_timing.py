"""Paint timings from the viewer's browser become one journal line each, with only the
declared fields, bounded; an unknown kind is refused. No database."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.auth import get_current_user
    from api.routers.client_timing import router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(name="person")
    return TestClient(app)


def test_a_timing_is_one_bounded_journal_line(caplog):
    caplog.set_level(logging.WARNING, logger="kura.client_timing")
    r = _client().post("/api/client-timing", json={
        "kind": "back", "filled_ms": 1234.56, "first_frame_ms": 16, "visible": 40, "unfilled": 0,
        "path": "/?q=" + "x" * 1000, "nav_type": "back_forward", "extra": "dropped", "dom_nodes": True,
        "longtask_ms": -5})
    assert r.status_code == 204
    [record] = [r for r in caplog.records if r.name == "kura.client_timing"]
    line = json.loads(record.getMessage().removeprefix("client-timing "))
    assert line["kind"] == "back" and line["user"] == "person" and line["filled_ms"] == 1234.6
    assert len(line["path"]) == 300 and "extra" not in line and "dom_nodes" not in line and "longtask_ms" not in line


def test_an_unknown_kind_is_refused():
    assert _client().post("/api/client-timing", json={"kind": "anything"}).status_code == 422


def test_a_large_body_and_a_flood_are_refused(monkeypatch):
    import api.routers.client_timing as C
    monkeypatch.setattr(C, "_recent", __import__("collections").defaultdict(__import__("collections").deque))
    client = _client()
    assert client.post("/api/client-timing", json={"kind": "back", "path": "x" * 5000}).status_code == 413
    codes = [client.post("/api/client-timing", json={"kind": "scroll"}).status_code for _ in range(C.PER_MINUTE + 3)]
    assert codes.count(204) == C.PER_MINUTE and set(codes[C.PER_MINUTE:]) == {429}


def test_a_chunked_body_is_bounded_by_what_arrives(monkeypatch):
    import api.routers.client_timing as C
    monkeypatch.setattr(C, "_recent", __import__("collections").defaultdict(__import__("collections").deque))
    big = ('{"kind":"back","path":"' + "x" * 5000 + '"}').encode()
    def chunks():
        for i in range(0, len(big), 1000):
            yield big[i:i + 1000]
    r = _client().post("/api/client-timing", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert _client().post("/api/client-timing", content=b"[1]").status_code == 422
