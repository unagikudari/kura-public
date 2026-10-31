"""A pool keeps its folder's (or archive's) order even when a file in it cannot go in at first:
kura_import.py holds the files behind a failed one until it is in, in a single run and in
--watch, while a file kura refused for good or a damaged page does not hold anything up.
"""
from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_szurubooru_migrate import _Kura, _reply, _server  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts/kura_import.py"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 8


class _Flaky(_Kura):
    fail_once: set[str] = set()         # filenames answered 503 the first time
    fail_always: set[str] = set()       # filenames answered 503 every time
    refuse: set[str] = set()            # filenames answered 415 every time

    def do_POST(self):
        if self.path == "/api/upload" and (self.fail_once or self.refuse or self.fail_always):
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            names = self.fail_once | self.refuse | self.fail_always
            name = next((n for n in names if f'filename="{n}"'.encode() in body), None)
            if name:
                self.calls.append(("POST", self.path))
                if name in self.refuse:
                    return _reply(self, 415, {"detail": "unsupported_media"})
                if name in self.fail_always:
                    return _reply(self, 503, {"detail": "busy"})
                _Flaky.fail_once.discard(name)
                return _reply(self, 503, {"detail": "busy"})
            self.rfile = io.BytesIO(body)
        return super().do_POST()


@pytest.fixture
def kura():
    _Kura.uploads, _Kura.tags, _Kura.pools, _Kura.calls = [], {}, [], []
    _Flaky.fail_once, _Flaky.refuse, _Flaky.fail_always = set(), set(), set()
    srv = _server(_Flaky)
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()


def _old(path: Path) -> None:
    t = time.time() - 120
    os.utime(path, (t, t))


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "lib"
    (root / "Book").mkdir(parents=True)
    for n in (1, 2, 3):
        (root / "Book" / f"p{n}.png").write_bytes(PNG + bytes([n]))
        _old(root / "Book" / f"p{n}.png")
    with zipfile.ZipFile(root / "Comic.cbz", "w") as z:
        for n in (1, 2, 3):
            z.writestr(f"c{n}.png", PNG + bytes([10 + n]))
    _old(root / "Comic.cbz")
    return root


def _key(tmp_path):
    k = tmp_path / "admin.key"
    k.write_text("the-key\n")
    k.chmod(0o600)
    return k


def _args(folder, kura, tmp_path, *extra):
    return [sys.executable, str(SCRIPT), str(folder), "--url", kura, "--email", "me@example.org",
            "--key-file", str(_key(tmp_path)), "--record", str(tmp_path / "rec.jsonl"),
            "--pool-per-folder", "--archives", *map(str, extra)]


def _run(*a):
    return subprocess.run(list(a), capture_output=True, text=True, env={**os.environ, "KURA_API_KEY": ""})


def _pool(name):
    return [u["name"] for u in _Kura.uploads if u.get("pool") == name]


def test_a_failed_file_holds_the_rest_of_its_pool_until_it_is_in(kura, folder, tmp_path):
    _Flaky.fail_once = {"p2.png", "c2.png"}
    r = _run(*_args(folder, kura, tmp_path))
    assert r.returncode == 1
    assert _pool("Book") == ["p1.png"] and _pool("Comic") == ["c1.png"]       # p3, c3 held
    assert json.loads(r.stdout)["held"] == 2
    r = _run(*_args(folder, kura, tmp_path))
    assert r.returncode == 0, r.stderr
    assert _pool("Book") == ["p1.png", "p2.png", "p3.png"]
    assert _pool("Comic") == ["c1.png", "c2.png", "c3.png"]


def test_a_file_refused_for_good_holds_nothing(kura, folder, tmp_path):
    _Flaky.refuse = {"p2.png"}
    r = _run(*_args(folder, kura, tmp_path))
    assert r.returncode == 1                                                  # the refusal is a failure
    assert _pool("Book") == ["p1.png", "p3.png"]
    r = _run(*_args(folder, kura, tmp_path))                                   # unchanged: not tried again
    assert r.returncode == 0 and json.loads(r.stdout)["refused_before"] == 1


def test_the_watch_keeps_a_pool_in_order_across_passes(kura, folder, tmp_path):
    _Flaky.fail_once = {"p2.png"}
    proc = subprocess.Popen(_args(folder, kura, tmp_path, "--watch", "1"), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env={**os.environ, "KURA_API_KEY": ""})
    try:
        end = time.monotonic() + 20
        while time.monotonic() < end and len(_pool("Book")) < 3:
            time.sleep(0.1)
        assert _pool("Book") == ["p1.png", "p2.png", "p3.png"]
        assert _pool("Comic") == ["c1.png", "c2.png", "c3.png"]
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=20)


def test_pools_that_keep_failing_do_not_starve_one_that_waited(kura, tmp_path):
    """Ten pools whose first page keeps failing, each with a second page that failed long ago,
    must not keep an eleventh pool, whose page failed once in between, from its turn: a pool's
    turn goes by its latest attempt, not by an old row of a page it is holding back."""
    root = tmp_path / "many"
    for i in range(10):
        d = root / f"A{i:02}"
        d.mkdir(parents=True)
        for n in (1, 2):
            (d / f"p{n}.png").write_bytes(PNG + bytes([i, n]))
            _old(d / f"p{n}.png")
    (root / "Z").mkdir()
    (root / "Z" / "z1.png").write_bytes(PNG + b"z")
    _old(root / "Z" / "z1.png")
    _Flaky.fail_always = {"p2.png", "z1.png"}                       # phase 1: every p2 and z1 fail
    rec = tmp_path / "rec.jsonl"
    proc = subprocess.Popen(_args(root, kura, tmp_path, "--watch", "1"), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env={**os.environ, "KURA_API_KEY": ""})
    try:
        end = time.monotonic() + 20
        while time.monotonic() < end and not (rec.exists() and '"Z/z1.png"' in rec.read_text()):
            time.sleep(0.1)
        assert '"Z/z1.png"' in rec.read_text()
        # phase 2: every first page changes and now fails; z1 would go in if it got its turn
        _Flaky.fail_always = {"p1.png", "p2.png"}
        for i in range(10):
            f = root / f"A{i:02}" / "p1.png"
            f.write_bytes(PNG + bytes([i, 1, 1]))
            _old(f)
        end = time.monotonic() + 25
        while time.monotonic() < end and not _pool("Z"):
            time.sleep(0.1)
        assert _pool("Z") == ["z1.png"]
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=20)
