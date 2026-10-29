"""Tagger reads display bytes through one loader: legacy column first, then the media authority."""
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tagger import display_bytes

IMAGES = {1: b"legacy-one", 2: None, 3: None, 4: None}   # post.data per id (None = moved out)
SERVED = {2: b"file-two", 3: b"file-three"}               # what the authority's content route has
NOT_IMAGES = {5}                                          # media_location != 'pg'


def query(sql, args):
    ids = args[0]
    if "select id, data" in sql:
        return [(i, IMAGES[i]) for i in ids if i in IMAGES]
    if "select id from post" in sql:
        return [(i,) for i in ids if i in IMAGES]
    raise AssertionError(sql)


@pytest.fixture
def authority(monkeypatch):
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            pid = int(self.path.split("/")[3])
            hits.append((pid, self.headers.get("If-None-Match")))
            body = SERVED.get(pid)
            if body is None:
                self.send_response(404); self.end_headers(); return
            etag = f'"{hashlib.sha256(body).hexdigest()}"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304); self.end_headers(); return
            self.send_response(200)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("KURA_MEDIA_AUTHORITY", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.delenv("KURA_DISPLAY_CACHE", raising=False)
    yield hits
    server.shutdown()


def test_legacy_first_then_authority_in_request_order(authority):
    got = display_bytes.load(query, [3, 1, 5, 2, 4, 3])
    assert got == [(3, b"file-three"), (1, b"legacy-one"), (2, b"file-two")]
    # legacy bytes are never fetched; non-images are never asked for; 404 is "no image"
    assert sorted(pid for pid, _ in authority) == [2, 3, 4]


def test_cache_revalidates_with_the_content_address(authority, monkeypatch, tmp_path):
    monkeypatch.setenv("KURA_DISPLAY_CACHE", str(tmp_path))
    assert display_bytes.load(query, [2]) == [(2, b"file-two")]
    assert display_bytes.load(query, [2]) == [(2, b"file-two")]
    assert authority[0] == (2, None)
    assert authority[1] == (2, f'"{hashlib.sha256(b"file-two").hexdigest()}"')  # answered 304


def test_without_a_cache_nothing_is_written(authority, tmp_path, monkeypatch):
    monkeypatch.setattr(display_bytes.Path, "home", lambda: tmp_path)
    display_bytes.load(query, [2])
    assert list(tmp_path.iterdir()) == []


def test_private_authority_uses_service_token_without_redirecting_it(monkeypatch):
    from tagger.credentials import token_headers
    monkeypatch.setenv("KURA_USER", "test-service")
    monkeypatch.setenv("KURA_TOKEN", "test-token")
    authorization = token_headers("test-service", "test-token")["Authorization"]
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            if self.path.endswith("/2/content"):
                if self.headers.get("Authorization") != authorization:
                    self.send_response(401); self.end_headers(); return
                self.send_response(200); self.end_headers(); self.wfile.write(b"authorized-image")
            elif self.path.endswith("/3/content"):
                self.send_response(302)
                self.send_header("Location", "/redirected")
                self.end_headers()
            else:
                self.send_response(200); self.end_headers(); self.wfile.write(b"redirected-image")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        origin = f"http://127.0.0.1:{server.server_port}"
        assert display_bytes._http(2, None, origin) == b"authorized-image"
        assert display_bytes._http(3, None, origin) == b"redirected-image"
        assert seen == [("/api/post/2/content", authorization),
                        ("/api/post/3/content", authorization), ("/redirected", None)]
    finally:
        server.shutdown()
        server.server_close()
