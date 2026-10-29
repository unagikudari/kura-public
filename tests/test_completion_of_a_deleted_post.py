"""A post deleted after its claim: the completion is a 404 `tagging_post_gone`, and the workers drop the item.

Before (to 1.263.2) the service raised a bare ValueError, the route let it through as a 500, and
both workers then reported the item failed, which met the same 500: two tracebacks per deleted post
(16 on 2026-10-24 in primary's kura-api journal). The work row went with the post (ON DELETE CASCADE),
so there is nothing to settle; the only correct outcome is to drop the item.

**DB を必要としない。** The service side is tested on PostgreSQL in tests/test_claim_storage.py.
"""
from __future__ import annotations

import asyncio
import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from api.errors import KuraError
from api.routers import tagging as routes
from api.schemas.tagging import CompleteRequest
from api.services import governed_window
from api.services.tagging import PostGone


def _route_error(monkeypatch, status):
    async def gone(db, attempt, function, *, route, started=None):
        raise PostGone(41)

    monkeypatch.setattr(governed_window, "run", gone)

    async def scenario():
        with pytest.raises(KuraError) as raised:
            await routes.complete(CompleteRequest(id=41, kind="auto_tag", status=status, error="x"),
                                  db=None, user=None)
        return raised.value

    return asyncio.run(scenario())


@pytest.mark.parametrize("status", ["ok", "failed"])
def test_the_route_answers_404_post_gone_with_the_next_action(monkeypatch, status):
    error = _route_error(monkeypatch, status)
    assert error.status == 404
    body = error.body()
    assert body["error"] == "tagging_post_gone" and body["context"] == {"post_id": 41}
    assert "drop this item" in body["fix"]


def test_both_workers_recognise_the_routes_slug(monkeypatch):
    import re
    from pathlib import Path

    slug = _route_error(monkeypatch, "ok").error
    # Read from source: tagger/loop.py cannot be imported without PyAV.
    for worker in ("tagger/loop.py", "tagger/nodec_autotag_worker.py"):
        text = (Path(__file__).resolve().parents[1] / worker).read_text()
        assert re.search(r'^POST_GONE = "([a-z_]+)"$', text, re.M).group(1) == slug, worker


def _response(status, body):
    return SimpleNamespace(status_code=status, json=lambda: body,
                           raise_for_status=lambda: (_ for _ in ()).throw(RuntimeError(f"HTTP {status}")))


def test_the_wd14_worker_drops_the_item_without_reporting_it_failed(monkeypatch):
    from tagger import nodec_autotag_worker as w

    sent = []

    def post(url, headers, json, timeout):
        sent.append(json["status"])
        return _response(404, {"detail": {"error": "tagging_post_gone", "context": {"post_id": 7}}})

    monkeypatch.setattr(w.requests, "post", post)
    monkeypatch.setattr(w, "ensemble_source_id", lambda release_id, heads: "test-source")
    with pytest.raises(w.PostGone):
        w.complete({}, 7, "ok", tags=[])

    # process_item: the ok completion meets the 404, and no failed completion follows.
    sent.clear()
    monkeypatch.setattr(w, "fetch_image", lambda H, url: object())
    monkeypatch.setattr(w, "apply_pixai_chars", lambda img: {})
    monkeypatch.setattr(w, "PIXAI_CHARS", {"chars": {}})
    monkeypatch.setattr(w, "infer", lambda *a, **k: ([], "general", {}))
    monkeypatch.setattr(w, "apply_content_heads", lambda c, img, heads: c)
    monkeypatch.setattr(w, "apply_skin_model", lambda c, img, model: c)
    monkeypatch.setattr(w, "build_tags", lambda *a: [])
    assert w.process_item({}, None, [], [], 448, {"id": 7}) == (False, 0, None)
    assert sent == ["ok"]


def test_the_wd14_worker_still_raises_other_404s(monkeypatch):
    from tagger import nodec_autotag_worker as w

    monkeypatch.setattr(w.requests, "post", lambda *a, **k: _response(404, {"detail": "Not Found"}))
    monkeypatch.setattr(w, "ensemble_source_id", lambda release_id, heads: "test-source")
    with pytest.raises(RuntimeError, match="HTTP 404"):
        w.complete({}, 7, "ok", tags=[])


def _http_error(code, body):
    return urllib.error.HTTPError("http://x/api/internal/tagging/complete", code, "Not Found", {},
                                  io.BytesIO(json.dumps(body).encode()))


def test_the_full_worker_recognises_only_the_post_gone_404():
    pytest.importorskip("av", reason="tagger/loop.py imports tagger.infer, which needs PyAV (the tagger venv has it)")
    from tagger import loop

    assert loop._post_gone(_http_error(404, {"detail": {"error": "tagging_post_gone"}}))
    assert not loop._post_gone(_http_error(404, {"detail": "Not Found"}))
    assert not loop._post_gone(_http_error(500, {"detail": {"error": "tagging_post_gone"}}))
