"""Reading originals on a node without the archive disk (docs/notes/kura-on-nodeb-plan.md,
"Originals on another host", rev 8): classify by link text, fetch from kura-archive-objects
into a verified spool with bounded slots and a deadline, never answer before verification,
and never write or delete through an archive link. No database; no real archive."""
from __future__ import annotations

import hashlib
import http.server
import os
import threading
import time
from pathlib import Path

import pytest

from api import config
from api.services import files, originals

BODY = b"original-bytes-" * 1000
SHA = hashlib.sha256(BODY).hexdigest()
MD5 = hashlib.md5(BODY).hexdigest()


class _Archive(http.server.BaseHTTPRequestHandler):
    archive_id = "archive-1"
    body = BODY
    delay = 0.0

    def do_GET(self):
        if self.headers.get("X-Archive-Key") != "k":
            self.send_response(401); self.end_headers(); return
        time.sleep(self.delay)
        self.send_response(200)
        self.send_header("X-Archive-Id", self.archive_id)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        pass


@pytest.fixture
def archive(monkeypatch, tmp_path):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Archive)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(config, "ARCHIVE_OBJECTS_URL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(config, "ARCHIVE_READ_KEY", "k")
    monkeypatch.setattr(config, "ARCHIVE_ID", "archive-1")
    monkeypatch.setattr(config, "ORIGINAL_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "LEGACY_ORIGINAL_ROOT", Path("/mnt/archive-disk/kura-media/posts"))
    _Archive.archive_id, _Archive.body, _Archive.delay = "archive-1", BODY, 0.0
    yield srv
    srv.shutdown()


def _links(tmp_path):
    posts = tmp_path / "posts"
    posts.mkdir()
    cas = posts / "7_0123456789abcdef.png"
    os.symlink(f"/mnt/archive-disk/kura-original-archive/objects/sha256/{SHA[:2]}/{SHA}", cas)
    legacy = posts / "8_0123456789abcdef.jpg"
    os.symlink("/mnt/archive-disk/kura-media/posts/8_fedcba9876543210.jpg", legacy)
    local = posts / "9_0123456789abcdef.png"
    local.write_bytes(BODY)
    other = posts / "10_0123456789abcdef.png"
    os.symlink("/elsewhere/x.png", other)
    return cas, legacy, local, other


def test_classify_reads_only_the_link_text(archive, tmp_path):
    cas, legacy, local, other = _links(tmp_path)
    assert originals.classify(cas).kind == "cas" and originals.classify(cas).sha == SHA
    loc = originals.classify(legacy)
    assert loc.kind == "legacy" and loc.legacy_name == "8_fedcba9876543210.jpg"
    assert originals.classify(local).kind == "local"
    assert originals.classify(other) is None


def test_a_verified_fetch_and_a_cache_keyed_by_identity(archive, tmp_path):
    cas, *_ = _links(tmp_path)
    loc = originals.classify(cas)
    assert originals.original_bytes(loc, originals.Identity(SHA, None, len(BODY))) == BODY
    assert originals.original_bytes(loc, originals.Identity(None, MD5, len(BODY))) == BODY
    keys = {p.name.split("-", 1)[0] for p in (tmp_path / "cache").glob("*/*")}
    assert keys == {SHA, MD5}


@pytest.mark.parametrize("change", ["wrong_bytes", "wrong_archive", "too_long"])
def test_wrong_answers_are_unavailable_and_leave_no_spool(archive, tmp_path, change):
    cas, *_ = _links(tmp_path)
    if change == "wrong_bytes":
        _Archive.body = b"x" * len(BODY)
    elif change == "wrong_archive":
        _Archive.archive_id = "another-disk"
    else:
        _Archive.body = BODY + b"extra"
    with pytest.raises(originals.OriginalUnavailable):
        originals.original_bytes(originals.classify(cas), originals.Identity(SHA, None, len(BODY)))
    assert not [p for p in (tmp_path / "cache").glob("*/*") if not p.name.startswith(".")]


def test_a_slow_archive_ends_within_the_deadline(archive, tmp_path, monkeypatch):
    cas, *_ = _links(tmp_path)
    monkeypatch.setattr(originals, "FETCH_DEADLINE", 0.5)
    _Archive.delay = 3.0
    start = time.monotonic()
    with pytest.raises(originals.OriginalUnavailable):
        originals.original_bytes(originals.classify(cas), originals.Identity(SHA, None, len(BODY)))
    assert time.monotonic() - start < 2.5


def test_the_wait_queue_is_bounded(archive, tmp_path, monkeypatch):
    cas, *_ = _links(tmp_path)
    monkeypatch.setattr(originals, "_admitted", threading.BoundedSemaphore(1))
    assert originals._admitted.acquire(blocking=False)
    with pytest.raises(originals.OriginalUnavailable, match="too many"):
        originals.original_bytes(originals.classify(cas), originals.Identity(SHA, None, len(BODY)))


class _Trickle(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("X-Archive-Id", "archive-1")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        for i in range(0, len(BODY), 100):          # 100 bytes every 0.2 s: far past any deadline
            try:
                self.wfile.write(BODY[i:i + 100]); self.wfile.flush()
            except OSError:
                return
            time.sleep(0.2)

    def log_message(self, *a):
        pass


def test_a_trickling_archive_is_cut_at_the_deadline_and_the_slot_held_until_then(archive, tmp_path, monkeypatch):
    """Codex 1.269.0: per-read timeouts let a trickle run past the deadline, and the caller's
    timeout released the admission slot while the fetch thread was still downloading."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Trickle)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(config, "ARCHIVE_OBJECTS_URL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(originals, "FETCH_DEADLINE", 1.0)
    monkeypatch.setattr(originals, "_admitted", threading.BoundedSemaphore(1))
    cas, *_ = _links(tmp_path)
    start = time.monotonic()
    with pytest.raises(originals.OriginalUnavailable):
        originals.original_bytes(originals.classify(cas), originals.Identity(SHA, None, len(BODY)))
    assert time.monotonic() - start < 3
    time.sleep(0.5)                                  # the fetch thread has ended by now
    assert originals._admitted.acquire(blocking=False)
    originals._admitted.release()
    srv.shutdown()


def test_a_cache_hit_is_verified_again(archive, tmp_path):
    cas, *_ = _links(tmp_path)
    loc, ident = originals.classify(cas), originals.Identity(SHA, None, len(BODY))
    assert originals.original_bytes(loc, ident) == BODY
    entry = originals._cache_path(loc, ident)
    entry.write_bytes(b"z" * len(BODY))              # same size, wrong bytes
    assert originals.original_bytes(loc, ident) == BODY
    assert entry.read_bytes() == BODY                 # replaced by a verified download


def test_eviction_skips_leased_entries_and_downloads_in_progress(archive, tmp_path, monkeypatch):
    cas, *_ = _links(tmp_path)
    loc, ident = originals.classify(cas), originals.Identity(SHA, None, len(BODY))
    lease = originals.acquire(loc, ident)
    part = lease.path.with_name(".x.part")
    part.write_bytes(b"p")
    originals._evict(0)
    assert lease.path.exists() and part.exists()
    lease.release()
    originals._evict(0)
    assert not lease.path.exists() and part.exists()


def test_archive_links_are_never_written_or_deleted_through(archive, tmp_path):
    cas, legacy, *_ = _links(tmp_path)
    for link in (cas, legacy):
        assert files.managed_archive_target(link) is not None
    files.delete_all_names([legacy])
    assert not os.path.lexists(legacy)          # the link is gone; nothing under /mnt was touched


class _Session:
    def __init__(self, row):
        self.row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *a, **k):
        row = self.row

        class R:
            def mappings(self):
                return self

            def first(self):
                return row
        return R()


def _client(monkeypatch, tmp_path, row):
    from fastapi.staticfiles import StaticFiles
    from fastapi.testclient import TestClient
    import api.main as main
    import api.db.session as session
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "_data_static", StaticFiles(directory=str(tmp_path), follow_symlink=True))
    monkeypatch.setattr(session, "async_session", lambda: _Session(row))
    return TestClient(main.app)


def test_route_serves_local_files_statically_and_archived_ones_only_verified(archive, tmp_path, monkeypatch):
    cas, legacy, local, other = _links(tmp_path)
    row = {"sha256": bytes.fromhex(SHA), "checksum_md5": None, "checksum": None,
           "file_size": len(BODY), "mime": "image/webp"}
    client = _client(monkeypatch, tmp_path, row)
    r = client.get(f"/data/posts/{local.name}")
    assert r.status_code == 200 and r.content == BODY
    r = client.get(f"/data/posts/{cas.name}")
    assert r.status_code == 200 and r.content == BODY and r.headers["content-type"] == "image/png"
    r = client.get(f"/data/posts/{cas.name}", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == BODY[:10]
    assert client.get(f"/data/posts/{other.name}").status_code == 404
    _Archive.body = b"y" * len(BODY)
    r = client.get(f"/data/posts/{legacy.name}")
    assert r.status_code == 503 and r.json()["detail"]["error"] == "original_unavailable"


def test_archive_server_serves_only_regular_files_under_its_roots(tmp_path, monkeypatch):
    import importlib.util
    from starlette.testclient import TestClient
    spec = importlib.util.spec_from_file_location(
        "archive_objects_server", Path(__file__).resolve().parents[1] / "scripts" / "archive_objects_server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    root = tmp_path / "mnt" / "kura-original-archive"
    obj = root / "objects" / "sha256" / SHA[:2]
    obj.mkdir(parents=True)
    (obj / SHA).write_bytes(BODY)
    legacy = tmp_path / "mnt" / "kura-media" / "posts"
    legacy.mkdir(parents=True)
    (legacy / "8_fedcba9876543210.jpg").write_bytes(BODY)
    os.symlink("/etc/passwd", legacy / "9_fedcba9876543210.jpg")
    outside = tmp_path / "outside" / "ab"
    outside.mkdir(parents=True)
    (outside / ("ab" + "0" * 62)).write_bytes(b"secret")
    os.symlink(outside, root / "objects" / "sha256" / "ab")
    monkeypatch.setenv("KURA_ARCHIVE_MOUNT", str(tmp_path / "mnt"))
    monkeypatch.setenv("KURA_ORIGINAL_ARCHIVE_ROOT", str(root))
    monkeypatch.setenv("KURA_LEGACY_ORIGINAL_ROOT", str(legacy))
    monkeypatch.setenv("KURA_ARCHIVE_READ_KEY", "k")
    client = TestClient(srv.app)
    key = {"X-Archive-Key": "k"}
    monkeypatch.setattr(srv, "_archive_id", lambda cfg: None)       # disk not mounted
    assert client.get(f"/objects/sha256/{SHA[:2]}/{SHA}", headers=key).status_code == 503
    monkeypatch.setattr(srv, "_archive_id", lambda cfg: "archive-1")
    assert client.get(f"/objects/sha256/{SHA[:2]}/{SHA}").status_code == 401
    r = client.get(f"/objects/sha256/{SHA[:2]}/{SHA}", headers=key)
    assert r.status_code == 200 and r.content == BODY and r.headers["x-archive-id"] == "archive-1"
    assert client.get("/legacy/posts/8_fedcba9876543210.jpg", headers=key).content == BODY
    assert client.get("/legacy/posts/9_fedcba9876543210.jpg", headers=key).status_code == 404
    assert client.get("/legacy/posts/..%2Fx", headers=key).status_code in (400, 404)
    assert client.get(f"/objects/sha256/00/{SHA}", headers=key).status_code == 400
    # an intermediate directory that is a symlink out of the root (codex 1.269.0)
    assert client.get("/objects/sha256/ab/ab" + "0" * 62, headers=key).status_code == 404
    monkeypatch.setenv("KURA_ARCHIVE_ALLOWED_CLIENTS", "10.0.0.9")
    assert client.get(f"/objects/sha256/{SHA[:2]}/{SHA}", headers=key).status_code == 403


def test_without_a_remote_archive_the_route_is_exactly_the_static_mount(tmp_path, monkeypatch):
    """Codex 1.269.0: on primary (no KURA_ARCHIVE_OBJECTS_URL, no KURA_LEGACY_ORIGINAL_ROOT) a
    legacy link was unclassifiable and became 404 where the static mount answered 200."""
    monkeypatch.setattr(config, "ARCHIVE_OBJECTS_URL", "")
    monkeypatch.setattr(config, "LEGACY_ORIGINAL_ROOT", None)
    disk = tmp_path / "hdd"
    disk.mkdir()
    (disk / "8_fedcba9876543210.jpg").write_bytes(BODY)
    posts = tmp_path / "posts"
    posts.mkdir()
    os.symlink(disk / "8_fedcba9876543210.jpg", posts / "8_0123456789abcdef.jpg")
    client = _client(monkeypatch, tmp_path, None)
    r = client.get("/data/posts/8_0123456789abcdef.jpg")
    assert r.status_code == 200 and r.content == BODY
    assert client.get("/data/posts/nope.jpg").status_code == 404


def test_export_reads_an_md5_only_original(archive, tmp_path):
    """Codex 1.269.0: the export dropped originals that have an MD5 and no SHA-256."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "export_censor_gt", Path(__file__).resolve().parents[1] / "scripts" / "export_censor_gt.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cas, *_ = _links(tmp_path)
    mod.content_path = lambda post_id, mime: cas
    assert mod.read_image_bytes(7, "image/png", None, False, None,
                                originals.Identity(None, MD5, len(BODY))) == BODY


def test_serving_follows_the_leased_inode_not_the_name(archive, tmp_path):
    """Codex 1.269.1: a concurrent publish replaced the name and eviction removed it while an
    older lease was still being served by path."""
    cas, *_ = _links(tmp_path)
    loc, ident = originals.classify(cas), originals.Identity(SHA, None, len(BODY))
    lease = originals.acquire(loc, ident)
    os.replace(lease.path, lease.path.with_suffix(".gone"))     # the name moves away
    lease.path.with_suffix(".gone").unlink()                     # and is removed
    with open(lease.serve_path, "rb") as f:
        assert f.read() == BODY
    lease.release()
    lease.release()                                              # idempotent


def test_a_bad_range_and_a_cancelled_wait_release_the_lease(archive, tmp_path, monkeypatch):
    import asyncio
    cas, *_ = _links(tmp_path)
    loc, ident = originals.classify(cas), originals.Identity(SHA, None, len(BODY))
    released = []
    lease = originals.acquire(loc, ident)
    real = lease.release
    lease.release = lambda: (released.append(1), real())
    response = originals.releasing_file_response(lease, "image/png")
    from starlette.testclient import TestClient
    from starlette.applications import Starlette
    from starlette.routing import Route
    app = Starlette(routes=[Route("/x", lambda request: response)])
    r = TestClient(app).get("/x", headers={"Range": f"bytes={len(BODY) + 10}-"})
    assert r.status_code == 416 and released

    _Archive.delay = 1.0
    async def scenario():
        task = asyncio.ensure_future(originals.acquire_async(
            loc, originals.Identity(SHA, None, len(BODY) + 0)))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    entry = originals._cache_path(loc, ident)
    entry.unlink()                                       # force a real fetch
    asyncio.run(scenario())
    time.sleep(1.5)                                      # the fetch finishes after the cancel
    import fcntl
    fd = os.open(entry, os.O_RDONLY)                     # published by the late fetch …
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # … and nobody holds its lease
    finally:
        os.close(fd)


class _SlowHeaders(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.wfile.write(b"HTTP/1.1 200 OK\r\n")
        for i in range(40):                              # a header line every 0.05 s for 2 s
            try:
                self.wfile.write(f"X-Pad-{i}: x\r\n".encode()); self.wfile.flush()
            except OSError:
                return
            time.sleep(0.05)

    def log_message(self, *a):
        pass


def test_trickled_headers_are_cut_at_the_absolute_deadline(archive, tmp_path, monkeypatch):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowHeaders)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(config, "ARCHIVE_OBJECTS_URL", f"http://127.0.0.1:{srv.server_port}")
    cas, *_ = _links(tmp_path)
    start = time.monotonic()
    with pytest.raises(originals.OriginalUnavailable):
        originals._download(originals.classify(cas), originals.Identity(SHA, None, len(BODY)),
                            time.monotonic() + 0.3, open(os.devnull, "wb"))
    assert time.monotonic() - start < 0.9
    srv.shutdown()


def test_the_archive_url_must_be_a_numeric_address(archive):
    """Codex 1.269.2: DNS resolution cannot be cut by the deadline timer."""
    assert config._numeric_host_url("http://10.32.1.44:8470") == "http://10.32.1.44:8470"
    assert config._numeric_host_url("http://[fd7a::1]:8470")
    for bad in ("http://primary:8470", "http://primary.example.ts.net:8470", "ftp://10.0.0.1/"):
        with pytest.raises(ValueError):
            config._numeric_host_url(bad)
