"""scripts/kura_import.py --watch keeps a folder in kura: every pass sends what is new or
changed once it has settled, signs in only when there is something to send (and out after),
leaves a file kura refused for good alone until it changes, rides out kura being away, and
stops cleanly on SIGTERM. The kura here is the stand-in the import test uses.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_kura_import import JPEG, PNG, SCRIPT, _Kura, _key_file, _server  # noqa: E402


class _WatchKura(_Kura):
    refuse_paths: set[str] = set()      # filenames answered 415 (the file itself is the problem)
    login_status = 200                  # 503 while "restarting"
    slow_paths: set[str] = set()        # filenames whose upload takes 2 s to answer
    slow_started: list[str] = []        # noted as such an upload arrives, before the wait

    def do_POST(self):
        if self.path == "/api/auth/login" and self.login_status != 200:
            self.rfile.read(int(self.headers.get("content-length") or 0))
            self.calls.append(("POST", self.path, None))
            return self._reply(self.login_status, {"detail": "starting up"})
        if self.path == "/api/upload" and self.refuse_paths:
            # peek at the filename without consuming the body twice
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length)
            if any(f'filename="{n}"'.encode() in body for n in self.refuse_paths):
                self.calls.append(("POST", self.path, self.headers.get("authorization")))
                return self._reply(415, {"detail": "unsupported_media"})
            self.rfile = __import__("io").BytesIO(body)
        if self.path == "/api/upload" and self.slow_paths:
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length)
            if any(f'filename="{n}"'.encode() in body for n in self.slow_paths):
                self.slow_started.append(self.path)
                time.sleep(2)
            self.rfile = __import__("io").BytesIO(body)
        return super().do_POST()


@pytest.fixture
def kura():
    _WatchKura.uploads, _WatchKura.calls, _WatchKura.known_md5, _WatchKura.fail_paths = [], [], set(), set()
    _WatchKura.redirect_to, _WatchKura.logout_status = None, 200
    _WatchKura.refuse_paths, _WatchKura.login_status, _WatchKura.slow_paths = set(), 200, set()
    _WatchKura.slow_started = []
    srv = _server(_WatchKura)
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()


def _old(path: Path, seconds: float = 120) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


@pytest.fixture
def box(tmp_path):
    root = tmp_path / "inbox"
    root.mkdir()
    for name, data in (("a.png", PNG + b"a"), ("b.jpg", JPEG + b"b")):
        (root / name).write_bytes(data)
        _old(root / name)
    return root


def _start(tmp_path, folder, url, *extra):
    return subprocess.Popen(
        [sys.executable, str(SCRIPT), str(folder), "--url", url, "--email", "me@example.org",
         "--key-file", str(_key_file(tmp_path)), "--record", str(tmp_path / "rec.jsonl"),
         "--watch", "1", *map(str, extra)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "KURA_API_KEY": ""})


def _until(cond, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def _stop(proc):
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=20)
    return proc.returncode, out, err


def _logins():
    return sum(1 for m, p, _ in _WatchKura.calls if p == "/api/auth/login")


def _logouts():
    return sum(1 for m, p, _ in _WatchKura.calls if p == "/api/auth/logout")


def test_new_files_are_sent_as_they_arrive_and_idle_passes_do_not_sign_in(kura, box, tmp_path):
    proc = _start(tmp_path, box, kura)
    try:
        assert _until(lambda: len(_WatchKura.uploads) == 2)
        assert _until(lambda: _logouts() == 1)
        time.sleep(2.5)                                  # a few idle passes
        assert _logins() == 1 and _logouts() == 1
        (box / "sub").mkdir()
        (box / "sub" / "c.png").write_bytes(PNG + b"c")
        _old(box / "sub" / "c.png")
        assert _until(lambda: len(_WatchKura.uploads) == 3)
        assert _until(lambda: _logouts() == 2)
        assert _logins() == 2
    finally:
        rc, out, err = _stop(proc)
    assert rc == 0, err
    assert json.loads(out.strip().splitlines()[-1]) == {"added": 3, "duplicates": 0, "failed": 0}
    assert [u["name"] for u in _WatchKura.uploads] == ["a.png", "b.jpg", "c.png"]


def test_a_file_still_changing_waits_until_it_has_settled(kura, box, tmp_path):
    (box / "copying.png").write_bytes(PNG + b"half")     # modified just now
    proc = _start(tmp_path, box, kura, "--settle", "60")
    try:
        assert _until(lambda: len(_WatchKura.uploads) == 2)
        time.sleep(2.5)
        assert [u["name"] for u in _WatchKura.uploads] == ["a.png", "b.jpg"]
        (box / "copying.png").write_bytes(PNG + b"whole")
        _old(box / "copying.png")                         # the copy is done
        assert _until(lambda: len(_WatchKura.uploads) == 3)
        assert _WatchKura.uploads[-1]["name"] == "copying.png"
    finally:
        _stop(proc)


def test_a_refused_file_waits_until_it_changes_a_failed_one_is_tried_again(kura, box, tmp_path):
    (box / "odd.png").write_bytes(PNG + b"odd"); _old(box / "odd.png")
    (box / "flaky.png").write_bytes(PNG + b"flaky"); _old(box / "flaky.png")
    _WatchKura.refuse_paths = {"odd.png"}
    _WatchKura.fail_paths = {"flaky.png"}
    proc = _start(tmp_path, box, kura)
    try:
        assert _until(lambda: len(_WatchKura.uploads) == 2)
        refused = lambda: sum(1 for m, p, _ in _WatchKura.calls if p == "/api/upload") - len(_WatchKura.uploads)
        assert _until(lambda: refused() >= 4)            # odd once + flaky at least three times
        time.sleep(2.5)
        rows = [json.loads(l) for l in (tmp_path / "rec.jsonl").read_text().splitlines()[1:]]
        assert sum(1 for r in rows if r["path"] == "odd.png") == 1          # not tried again
        assert sum(1 for r in rows if r["path"] == "flaky.png") >= 3        # 500: tried every pass
        _WatchKura.refuse_paths, _WatchKura.fail_paths = set(), set()
        (box / "odd.png").write_bytes(PNG + b"odd, fixed"); _old(box / "odd.png", 60)
        assert _until(lambda: {u["name"] for u in _WatchKura.uploads} >= {"odd.png", "flaky.png"})
    finally:
        _stop(proc)


def test_kura_restarting_is_waited_out_a_wrong_key_stops_the_watch(kura, box, tmp_path):
    _WatchKura.login_status = 503
    proc = _start(tmp_path, box, kura)
    try:
        assert _until(lambda: _logins() >= 2)            # tried, and tried again
        assert proc.poll() is None and not _WatchKura.uploads
        _WatchKura.login_status = 200
        assert _until(lambda: len(_WatchKura.uploads) == 2)
    finally:
        rc, out, err = _stop(proc)
    assert rc == 0 and "starting up" in err
    p = subprocess.run(
        [sys.executable, str(SCRIPT), str(box), "--url", kura, "--email", "me@example.org",
         "--key-file", str(_key_file(tmp_path, "wrong\n", name="bad.key")), "--record", str(tmp_path / "rec2.jsonl"),
         "--watch", "1"], capture_output=True, text=True, timeout=30, env={**os.environ, "KURA_API_KEY": ""})
    assert p.returncode == 1 and "sign-in refused (401)" in p.stderr


def test_watch_takes_no_dry_run_and_at_least_a_second(box, tmp_path):
    for extra in (["--watch", "1", "--dry-run"], ["--watch", "0.5"]):
        p = subprocess.run([sys.executable, str(SCRIPT), str(box), "--url", "http://127.0.0.1:9", *extra],
                           capture_output=True, text=True, timeout=30)
        assert p.returncode == 2


def test_a_file_that_changes_while_earlier_ones_are_sent_waits(kura, box, tmp_path):
    """The pass found b.jpg settled, but it was rewritten while a.png was still going out:
    it is looked at again just before it is sent, and waits."""
    _WatchKura.slow_paths = {"a.png"}
    proc = _start(tmp_path, box, kura, "--settle", "60")
    try:
        assert _until(lambda: _WatchKura.slow_started)              # a.png is on its way
        (box / "b.jpg").write_bytes(JPEG + b"b, rewritten")      # mtime now, mid-pass
        assert _until(lambda: len(_WatchKura.uploads) == 1)
        time.sleep(2.5)
        assert [u["name"] for u in _WatchKura.uploads] == ["a.png"]
        _old(box / "b.jpg")
        assert _until(lambda: len(_WatchKura.uploads) == 2)
        assert _WatchKura.uploads[1]["size"] == len(JPEG + b"b, rewritten")
    finally:
        _stop(proc)


def test_failures_at_the_front_do_not_starve_the_files_behind_them(kura, tmp_path):
    root = tmp_path / "many"
    root.mkdir()
    for i in range(1, 12):
        (root / f"f{i:02}.png").write_bytes(PNG + bytes([i]))
        _old(root / f"f{i:02}.png")
    _WatchKura.fail_paths = {f"f{i:02}.png" for i in range(1, 11)}   # ten in a row: the pass gives up
    proc = _start(tmp_path, root, kura)
    try:
        assert _until(lambda: [u["name"] for u in _WatchKura.uploads] == ["f11.png"])
    finally:
        _stop(proc)


def test_a_sign_out_that_did_not_get_through_is_retried_even_when_idle(kura, box, tmp_path):
    _WatchKura.logout_status = 503
    proc = _start(tmp_path, box, kura)
    try:
        assert _until(lambda: len(_WatchKura.uploads) == 2 and _logouts() >= 1)
        failed = _logouts()
        _WatchKura.logout_status = 200
        assert _until(lambda: _logouts() > failed)                 # no new files: the idle pass retries
        settled = _logouts()
        time.sleep(2.5)
        assert _logouts() == settled and _logins() == 1             # revoked once, not again and again
    finally:
        rc, out, err = _stop(proc)
    assert "could not sign out (503" in err and "could not be revoked" not in err


def test_a_file_that_recovers_behind_failing_ones_is_tried_again(kura, tmp_path):
    """All eleven fail at first; then only the last one could go. Retries are taken in the
    order they were last tried, so the ten that keep failing cannot keep it out."""
    root = tmp_path / "many"
    root.mkdir()
    for i in range(1, 12):
        (root / f"f{i:02}.png").write_bytes(PNG + bytes([i]))
        _old(root / f"f{i:02}.png")
    _WatchKura.fail_paths = {f"f{i:02}.png" for i in range(1, 12)}
    proc = _start(tmp_path, root, kura)
    try:
        rec = tmp_path / "rec.jsonl"
        tried = lambda name: rec.exists() and sum(1 for l in rec.read_text().splitlines()
                                                  if f'"path": "{name}"' in l)
        assert _until(lambda: tried("f11.png") >= 1)              # every one has failed once
        _WatchKura.fail_paths = {f"f{i:02}.png" for i in range(1, 11)}
        assert _until(lambda: [u["name"] for u in _WatchKura.uploads] == ["f11.png"])
    finally:
        _stop(proc)


def test_with_sidecars_an_image_waits_for_its_sidecar_to_settle(kura, tmp_path):
    root = tmp_path / "dl"
    root.mkdir()
    (root / "p.png").write_bytes(PNG + b"p")
    _old(root / "p.png")
    (root / "p.png.json").write_text('{"tag_string": "half"}')        # still being written
    proc = _start(tmp_path, root, kura, "--settle", "60", "--sidecars")
    try:
        time.sleep(3)
        assert not _WatchKura.uploads
        (root / "p.png.json").write_text('{"tag_string": "night sky_view"}')
        _old(root / "p.png.json")
        assert _until(lambda: len(_WatchKura.uploads) == 1)
        assert _WatchKura.uploads[0]["tags"] == "night sky_view"
    finally:
        _stop(proc)
