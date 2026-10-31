"""scripts/kura_import.py adds a folder to kura without anything but the API: it signs in with
the sign-in key, sends the media files in natural order, notes each one so an interrupted run
continues, and signs out. The server here is a stand-in that answers like /api/auth/login,
/api/upload and /api/auth/logout.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import subprocess
import sys
import threading
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts/kura_import.py"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 12


class _Kura(http.server.BaseHTTPRequestHandler):
    """Answers as kura does; `uploads` keeps every file received, `calls` every request."""
    uploads: list[dict] = []
    calls: list[tuple[str, str, str | None]] = []
    known_md5: set[str] = set()
    fail_paths: set[str] = set()     # filenames the server refuses with 500
    redirect_to: str | None = None   # when set, every request is answered with a 302 there
    logout_status = 200

    def _reply(self, status: int, body: dict, **headers) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length)
        auth = self.headers.get("authorization")
        self.calls.append(("POST", self.path, auth))
        if len(body) < length:                  # the client stopped short: no multipart to parse
            return self._reply(400, {"detail": "truncated body"})
        if self.redirect_to:
            return self._reply(302, {}, location=self.redirect_to + self.path)
        if self.path == "/api/auth/login":
            req = json.loads(body)
            if req.get("key") != "the-key" or req.get("email") != "me@example.org":
                return self._reply(401, {"detail": "invalid_key"})
            return self._reply(200, {"username": "admin", "token": "tok-1", "email": req["email"]})
        if auth is None:
            return self._reply(401, {"detail": "missing token"})
        if self.path == "/api/auth/logout":
            if self.logout_status in ("truncated", "truncated503"):   # declares 200 bytes, sends 5, hangs up
                self.send_response(200 if self.logout_status == "truncated" else 503)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", "200")
                self.end_headers()
                self.wfile.write(b'{"sta')
                self.wfile.flush()
                self.close_connection = True
                return
            return self._reply(self.logout_status, {"status": "ok"} if self.logout_status == 200 else {"detail": "down"})
        if self.path == "/api/upload":
            msg = BytesParser(policy=HTTP).parsebytes(
                b"content-type: " + self.headers["content-type"].encode() + b"\r\n\r\n" + body)
            fields = {p.get_param("name", header="content-disposition"): p for p in msg.iter_parts()}
            data = fields["file"].get_payload(decode=True)
            name = fields["file"].get_filename()
            if name in self.fail_paths:
                return self._reply(500, {"detail": "boom"})
            md5 = hashlib.md5(data).hexdigest()
            dup = md5 in self.known_md5
            self.known_md5.add(md5)
            row = {k: v.get_payload(decode=True).decode() for k, v in fields.items() if k != "file"}
            row.update(name=name, md5=md5, size=len(data), content_type=fields["file"].get_content_type(), auth=auth)
            self.uploads.append(row)
            return self._reply(200, {"id": len(self.uploads), "md5": md5, "duplicate": dup, "status": "active"})
        return self._reply(404, {"detail": "no such route"})

    def log_message(self, *a):
        pass


def _server(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def kura():
    _Kura.uploads, _Kura.calls, _Kura.known_md5, _Kura.fail_paths = [], [], set(), set()
    _Kura.redirect_to, _Kura.logout_status = None, 200
    srv = _server(_Kura)
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "scans"
    (root / "vol1").mkdir(parents=True)
    (root / "vol2" / "extras").mkdir(parents=True)
    (root / "vol1" / "page_10.png").write_bytes(PNG + b"10")
    (root / "vol1" / "page_2.png").write_bytes(PNG + b"2")
    (root / "vol1" / "page_1.png").write_bytes(PNG + b"1")
    (root / "vol1" / "notes.txt").write_bytes(b"not an image at all")
    (root / "vol1" / ".hidden.png").write_bytes(PNG + b"hidden")
    (root / "vol2" / "cover.jpg").write_bytes(JPEG + b"cover")
    (root / "vol2" / "extras" / "sketch.jpg").write_bytes(JPEG + b"sketch")
    (root / "vol2" / "empty.png").write_bytes(b"\0" * 32)
    return root


def _key_file(tmp_path, text="the-key\n", mode=0o600, name="admin.key"):
    p = tmp_path / name
    p.write_text(text)
    p.chmod(mode)
    return p


def run(*args, env=None):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True,
                          env={**os.environ, "KURA_API_KEY": "", **(env or {})})


def _signed_in(tmp_path, folder, url, *extra):
    return run(folder, "--url", url, "--email", "me@example.org", "--key-file", _key_file(tmp_path),
               "--record", tmp_path / "rec.jsonl", *extra)


TOKEN = "Token " + base64.b64encode(b"admin:tok-1").decode()


def test_a_folder_goes_in_natural_order_with_one_sign_in(kura, folder, tmp_path):
    r = _signed_in(tmp_path, folder, kura, "--tags", "scan 2024", "--safety", "safe", "--pool-per-folder")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"added": 5, "duplicates": 0, "failed": 0, "skipped": 0, "not_media": 2,
                                    "seconds": pytest.approx(0, abs=30)}
    assert [u["name"] for u in _Kura.uploads] == ["page_1.png", "page_2.png", "page_10.png", "cover.jpg", "sketch.jpg"]
    assert [u["pool"] for u in _Kura.uploads] == ["vol1", "vol1", "vol1", "vol2", "vol2 / extras"]
    assert {u["tags"] for u in _Kura.uploads} == {"scan 2024"}
    assert {u["safety"] for u in _Kura.uploads} == {"safe"}
    assert {u["content_type"] for u in _Kura.uploads} == {"image/png", "image/jpeg"}
    assert [u["size"] for u in _Kura.uploads] == [17, 17, 18, 21, 22]
    assert [c[1] for c in _Kura.calls] == ["/api/auth/login"] + ["/api/upload"] * 5 + ["/api/auth/logout"]
    assert {c[2] for c in _Kura.calls[1:]} == {TOKEN}
    assert "5/5 sent" in r.stderr


def test_an_interrupted_run_continues_and_a_changed_file_is_sent_again(kura, folder, tmp_path):
    rec = tmp_path / "rec.jsonl"
    _Kura.fail_paths = {"page_10.png"}
    r = _signed_in(tmp_path, folder, kura)
    assert r.returncode == 1 and json.loads(r.stdout)["failed"] == 1 and json.loads(r.stdout)["added"] == 4
    assert "page_10.png: 500 boom" in r.stderr
    _Kura.fail_paths = set()
    (folder / "vol2" / "cover.jpg").write_bytes(JPEG + b"cover v2")      # edited since: sent again
    os.utime(folder / "vol2" / "cover.jpg", ns=(1, 1))
    _Kura.uploads.clear()
    r = _signed_in(tmp_path, folder, kura)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"added": 2, "duplicates": 0, "failed": 0, "skipped": 3, "not_media": 2,
                                    "seconds": pytest.approx(0, abs=30)}
    assert sorted(u["name"] for u in _Kura.uploads) == ["cover.jpg", "page_10.png"]
    rows = [json.loads(line) for line in rec.read_text().splitlines()]
    assert rows[0]["import"]["url"] == kura and rows[0]["import"]["folder"] == str(folder)
    assert len(rows) == 8 and {"path", "size", "mtime_ns", "id"} <= set(rows[1])


def test_a_record_belongs_to_one_folder_one_kura_and_one_set_of_options(kura, folder, tmp_path):
    """Importing into kura A must not make an import into kura B skip everything; nor may a
    run with other tags reuse the record of a run with these."""
    other = _server(_Kura)
    try:
        r = _signed_in(tmp_path, folder, kura)
        assert r.returncode == 0 and len(_Kura.uploads) == 5
        r = _signed_in(tmp_path, folder, f"http://127.0.0.1:{other.server_port}")
        assert r.returncode == 1 and "belongs to a different import" in r.stderr and len(_Kura.uploads) == 5
        # Without --record, the default record is per import, so kura B simply gets its own.
        state = tmp_path / "state"
        r = run(folder, "--url", f"http://127.0.0.1:{other.server_port}", "--email", "me@example.org",
                "--key-file", _key_file(tmp_path), env={"XDG_STATE_HOME": str(state)})
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["added"] + json.loads(r.stdout)["duplicates"] == 5 and len(_Kura.uploads) == 10
        r = run(folder, "--url", kura, "--email", "me@example.org", "--key-file", _key_file(tmp_path),
                "--tags", "other", env={"XDG_STATE_HOME": str(state)})
        assert r.returncode == 0 and len(_Kura.uploads) == 15
        assert len(list((state / "kura/import").glob("*.jsonl"))) == 2
    finally:
        other.shutdown()


def test_what_kura_already_holds_counts_as_a_duplicate(kura, folder, tmp_path):
    (folder / "vol2" / "again.jpg").write_bytes(JPEG + b"cover")
    r = _signed_in(tmp_path, folder, kura)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["duplicates"] == 1 and json.loads(r.stdout)["added"] == 5


def test_a_token_file_skips_the_sign_in(kura, folder, tmp_path):
    tok = tmp_path / "token.env"
    tok.write_text("KURA_USER=worker\nKURA_TOKEN=tok-9\n")
    tok.chmod(0o600)
    r = run(folder, "--url", kura, "--token-file", tok, "--record", tmp_path / "rec.jsonl", "--pool", "Scans")
    assert r.returncode == 0, r.stderr
    assert [c[1] for c in _Kura.calls] == ["/api/upload"] * 5
    assert _Kura.uploads[0]["auth"] == "Token " + base64.b64encode(b"worker:tok-9").decode()
    assert {u["pool"] for u in _Kura.uploads} == {"Scans"}


def test_a_readable_key_file_and_a_wrong_key_are_refused(kura, folder, tmp_path):
    r = run(folder, "--url", kura, "--email", "me@example.org", "--key-file", _key_file(tmp_path, mode=0o644),
            "--record", tmp_path / "rec.jsonl")
    assert r.returncode == 1 and "mode 0600" in r.stderr and not _Kura.calls
    r = run(folder, "--url", kura, "--email", "me@example.org",
            "--key-file", _key_file(tmp_path, "wrong", name="wrong.key"), "--record", tmp_path / "rec.jsonl")
    assert r.returncode == 1 and "sign-in refused (401): invalid_key" in r.stderr
    assert [c[1] for c in _Kura.calls] == ["/api/auth/login"]


def test_a_redirect_is_refused_so_the_token_never_follows_it(kura, folder, tmp_path):
    """A 302 would carry the Authorization header to wherever it points."""
    elsewhere = _server(_Kura)
    try:
        _Kura.redirect_to = f"http://127.0.0.1:{elsewhere.server_port}"
        r = _signed_in(tmp_path, folder, kura)
        assert r.returncode == 1 and "sign-in refused (302): redirected to" in r.stderr and "final address" in r.stderr
        assert [c[1] for c in _Kura.calls] == ["/api/auth/login"]       # nothing reached `elsewhere`
        # Already signed in (token file): the first upload's redirect is a failure, not a hop.
        _Kura.calls.clear()
        tok = tmp_path / "token.env"
        tok.write_text("KURA_USER=worker\nKURA_TOKEN=tok-9\n")
        tok.chmod(0o600)
        r = run(folder, "--url", kura, "--token-file", tok, "--record", tmp_path / "rec2.jsonl")
        assert r.returncode == 1 and json.loads(r.stdout)["failed"] == 5
        assert all(c[1] == "/api/upload" for c in _Kura.calls) and len(_Kura.calls) == 5
    finally:
        elsewhere.shutdown()


def test_sign_out_happens_even_when_the_run_fails_and_its_failure_is_reported(kura, folder, tmp_path):
    _Kura.fail_paths = {"page_1.png", "page_2.png", "page_10.png", "cover.jpg", "sketch.jpg"}
    for i in range(8):
        (folder / f"more_{i}.png").write_bytes(PNG + bytes([i]))
        _Kura.fail_paths.add(f"more_{i}.png")
    _Kura.logout_status = 503
    r = _signed_in(tmp_path, folder, kura)
    assert r.returncode == 1 and "10 sends in a row failed" in r.stderr
    assert json.loads(r.stdout)["failed"] == 10 and _Kura.calls[-1][1] == "/api/auth/logout"
    assert "could not sign out (503: down)" in r.stderr


@pytest.mark.parametrize("how", ["truncated", "truncated503"])
def test_a_truncated_sign_out_reply_is_reported_and_does_not_mask_the_run(kura, folder, tmp_path, how):
    """A reply cut off mid-body is an http.client error, not a URLError, whether the status
    was 200 or 503 (the error body is read too); it must still end as 'could not sign out',
    with the run's own outcome (here: success) intact."""
    _Kura.logout_status = how
    r = _signed_in(tmp_path, folder, kura)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["added"] == 5
    assert "could not sign out (no connection: IncompleteRead" in r.stderr
    assert "Traceback" not in r.stderr


def test_dry_run_reads_the_record_but_never_writes_it(kura, folder, tmp_path):
    rec = tmp_path / "rec.jsonl"
    _Kura.fail_paths = {"page_10.png"}
    _signed_in(tmp_path, folder, kura)
    before = rec.read_bytes()
    r = run(folder, "--url", kura, "--dry-run", "--record", rec)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["vol1/page_10.png"]          # the one that failed: still to send
    assert "4 already sent" in r.stderr and rec.read_bytes() == before
    r = run(folder, "--url", kura, "--dry-run", "--record", rec, "--tags", "other")
    assert r.returncode == 1 and "belongs to a different import" in r.stderr


def test_a_file_that_shrinks_while_it_is_sent_is_a_failure_not_a_post(kura, folder, tmp_path):
    """The multipart Content-Length is taken from the opened file; a file that then yields
    fewer bytes is refused by the sender rather than padded or recorded as sent."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("kura_import", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    big = folder / "vol1" / "big.png"
    big.write_bytes(PNG + b"x" * 1000)

    class Shrinking(mod.Api):
        def call(self, method, path, body=None, headers=None, timeout=60.0):
            big.write_bytes(PNG + b"x" * 10)        # replaced after it was opened and measured
            return super().call(method, path, body, headers, timeout)

    api = Shrinking(kura, None)
    api.authorization = TOKEN
    with pytest.raises(mod.FileChanged):
        api.upload(big, {"tags": ""})
    assert not _Kura.uploads


def test_dry_run_lists_without_credentials_or_a_server(folder, tmp_path):
    r = run(folder, "--url", "http://127.0.0.1:9", "--dry-run", "--pool-per-folder", "--record", tmp_path / "rec.jsonl")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["vol1/page_1.png  → pool vol1", "vol1/page_2.png  → pool vol1",
                                     "vol1/page_10.png  → pool vol1", "vol2/cover.jpg  → pool vol2",
                                     "vol2/extras/sketch.jpg  → pool vol2 / extras"]
    assert "5 media files, 2 other files passed over" in r.stderr
    assert not (tmp_path / "rec.jsonl").exists()


def test_the_help_does_not_take_a_secret_on_the_command_line():
    r = run("--help")
    assert r.returncode == 0
    assert "--key " not in r.stdout and "--token " not in r.stdout and "--key-file" in r.stdout
