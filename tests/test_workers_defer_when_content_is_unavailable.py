"""A tagger worker does not spend a post's attempts on an infrastructure outage
(tagger/content_fetch.py; docs/notes/kura-on-nodeb-plan.md, rev 8). 503 original_unavailable,
a timeout, a refused connection or a short body: no completion, the batch ends, the leases
expire uncounted. Any other failure is still the post's and is reported failed. No DB."""
from __future__ import annotations

import http.server
import json
import re
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from tagger import content_fetch

REPO = Path(__file__).resolve().parents[1]


class _H(http.server.BaseHTTPRequestHandler):
    mode = "503"

    def do_GET(self):
        if self.mode == "503":
            body = json.dumps({"detail": {"error": "original_unavailable"}}).encode()
            self.send_response(503)
        elif self.mode == "other503":
            body = b'{"detail": "maintenance"}'
            self.send_response(503)
        elif self.mode == "short":
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"x" * 10)
            return
        else:
            body = b"ok"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/data/posts/1_0123456789abcdef.png"
    srv.shutdown()


@pytest.mark.parametrize("mode,deferred", [("503", True), ("short", True), ("other503", False), ("404", False)])
def test_urllib_failures_are_classified(server, mode, deferred):
    _H.mode = mode
    try:
        with urllib.request.urlopen(server, timeout=5) as r:
            r.read()
        raise AssertionError("expected a failure")
    except Exception as exc:  # noqa: BLE001
        assert (content_fetch.classify_urllib_error(exc) is not None) is deferred


def test_a_refused_connection_defers():
    try:
        urllib.request.urlopen("http://127.0.0.1:9/x", timeout=2)
    except Exception as exc:  # noqa: BLE001
        assert content_fetch.classify_urllib_error(exc) is not None


def _resp(status, body=b"", headers=None):
    def rfs():
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")
    return SimpleNamespace(status_code=status, content=body, headers=headers or {}, raise_for_status=rfs)


def test_the_wd14_worker_defers_without_a_completion(monkeypatch):
    from tagger import nodec_autotag_worker as w
    sent = []
    monkeypatch.setattr(w, "complete", lambda *a, **k: sent.append(a))
    for response in (_resp(503, json.dumps({"detail": {"error": "original_unavailable"}}).encode()),
                     _resp(200, b"x" * 10, {"Content-Length": "100"})):
        monkeypatch.setattr(w.requests, "get", lambda *a, _r=response, **k: _r)
        with pytest.raises(content_fetch.ContentUnavailable):
            w.process_item({}, None, [], [], 448, {"id": 7, "content_url": "/data/posts/7_x.png"})
    def refused(*a, **k):
        raise w.requests.ConnectionError("refused")
    monkeypatch.setattr(w.requests, "get", refused)
    with pytest.raises(content_fetch.ContentUnavailable):
        w.process_item({}, None, [], [], 448, {"id": 7, "content_url": "/data/posts/7_x.png"})
    assert sent == []
    monkeypatch.setattr(w.requests, "get", lambda *a, **k: _resp(404))
    assert w.process_item({}, None, [], [], 448, {"id": 7, "content_url": "/x"})[0] is False
    assert sent and sent[-1][2] == "failed"          # the post's own failure is still reported


def test_the_full_worker_loop_defers_before_reporting_failures():
    """tagger/loop.py needs PyAV to import; its wiring is read from source."""
    src = (REPO / "tagger" / "loop.py").read_text()
    fetch = src[src.index("def _fetch_bytes"):src.index("def claim(")]
    assert "classify_urllib_error(exc)" in fetch and "raise deferred from exc" in fetch
    loop = src[src.index("for item in items:"):]
    handlers = re.findall(r"except (\w+)", loop)
    assert handlers.index("ContentUnavailable") < handlers.index("Exception")
    block = loop[loop.index("except ContentUnavailable"):loop.index("except RequiredCharacterComponentError")]
    assert "complete(" not in block and "break" in block


def test_wd14_defers_on_a_real_mid_body_disconnect_and_completes_nothing(server, monkeypatch):
    """Codex 1.269.0: requests raises ChunkedEncodingError on a real short body; the mock in
    the test above never took that path. The rest of the batch gets no completion either."""
    from tagger import nodec_autotag_worker as w
    _H.mode = "short"
    sent = []
    monkeypatch.setattr(w, "complete", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(w, "claim", lambda H, limit, lease, state: [
        {"id": 7, "content_url": server}, {"id": 8, "content_url": server}])
    monkeypatch.setattr(w, "SLEEP_POST", 0)
    state = SimpleNamespace(model=None, names=[], cats=[], img_size=448, release_id=1,
                            veto_heads=None, reap_gates=None, release_reap=())
    with pytest.raises(content_fetch.ContentUnavailable):
        w.process_batch({}, state, 2, 60)
    assert sent == []
