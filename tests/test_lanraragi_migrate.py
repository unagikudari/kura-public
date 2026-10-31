"""scripts/lanraragi_migrate.py moves a LANraragi library into kura: each zip/cbz archive a
pool of its pages in order with the archive's tags (namespaces mapped to categories, source:
as the source, date_added: left out), a rar or 7z named and passed over without being fetched,
and an interrupted run continued from its record. The LANraragi here is a stand-in for its
API; the kura stand-in is the one the szurubooru test uses.
"""
from __future__ import annotations

import base64
import http.server
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_szurubooru_migrate import _Kura, _reply, _server  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts/lanraragi_migrate.py"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 8


def _zipbytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


A1 = "a" * 40
B2 = "b" * 40
C3 = "c" * 40


class _Lrr(http.server.BaseHTTPRequestHandler):
    key: str | None = None              # when set, every call needs Authorization: Bearer base64(key)
    archives = [
        {"arcid": B2, "title": "Night Ferry", "extension": "cbz", "pagecount": 3, "isnew": "false",
         "tags": "artist:jane doe, group:orbit studio, parody:orbital notes, character:unit 07, "
                 "female:glasses, date_added:1730000000, source:example.org/g/1, harbor"},
        {"arcid": A1, "title": "Afternoon Ferry", "extension": "zip", "pagecount": 1, "isnew": False, "tags": ""},
        {"arcid": C3, "title": "Packed With Rar", "extension": "rar", "pagecount": 5, "isnew": None, "tags": "artist:x"},
    ]
    files = {
        B2: _zipbytes({"p10.png": PNG + b"10", "p2.png": PNG + b"2", "p1.png": PNG + b"1",
                       "__MACOSX/._p1.png": PNG + b"fork", "info.txt": b"text"}),
        A1: _zipbytes({"only.png": PNG + b"only"}),
        C3: b"Rar!\x1a\x07\x00 not fetched",
    }
    calls: list[str] = []
    fail_once: set[str] = set()

    def do_GET(self):
        self.calls.append(self.path)
        if self.key is not None:
            want = "Bearer " + base64.b64encode(self.key.encode()).decode()
            if self.headers.get("authorization") != want:
                return _reply(self, 401, {"error": "This API is protected and requires login or an API Key."})
        if self.path == "/api/archives":
            return _reply(self, 200, self.archives)
        if self.path.startswith("/api/archives/") and self.path.endswith("/download"):
            arcid = self.path.split("/")[3]
            if arcid in self.fail_once:
                self.fail_once.discard(arcid)
                return _reply(self, 500, {"error": "busy"})
            return _reply(self, 200, self.files[arcid], "application/octet-stream")
        return _reply(self, 404, {"error": "no"})

    def log_message(self, *a):
        pass


@pytest.fixture
def lrr():
    _Lrr.calls, _Lrr.key, _Lrr.fail_once = [], None, set()
    srv = _server(_Lrr)
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()


@pytest.fixture
def kura():
    _Kura.uploads, _Kura.tags, _Kura.pools, _Kura.calls, _Kura.put_fail_once = [], {}, [], [], False
    srv = _server(_Kura)
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()


def _key(tmp_path, name="admin.key", text="the-key\n"):
    k = tmp_path / name
    k.write_text(text)
    k.chmod(0o600)
    return k


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True,
                          env={**os.environ, "KURA_API_KEY": ""})


def _migrate(lrr, kura, tmp_path, *extra):
    return run("--from", lrr, "--url", kura, "--email", "me@example.org", "--key-file", _key(tmp_path),
               "--record", tmp_path / "rec.jsonl", *extra)


def test_each_archive_is_a_pool_of_its_pages_with_its_tags(lrr, kura, tmp_path):
    r = _migrate(lrr, kura, tmp_path)
    assert r.returncode == 0, r.stderr
    sent = [(u["name"], u["pool"]) for u in _Kura.uploads]
    # titles in natural order: Afternoon Ferry, Night Ferry (the rar is not fetched)
    assert sent == [("only.png", "Afternoon Ferry"), ("p1.png", "Night Ferry"), ("p2.png", "Night Ferry"),
                    ("p10.png", "Night Ferry")]
    page = _Kura.uploads[1]
    assert page["tags"] == "jane_doe orbit_studio orbital_notes unit_07 female:glasses harbor"
    assert page["artist_tags"] == "jane_doe orbit_studio" and page["source"] == "https://example.org/g/1"
    assert page["safety"] == "unsafe"
    assert _Kura.tags["unit_07"] == "character" and _Kura.tags["jane_doe"] == "artist"
    assert not any(p.startswith(f"/api/archives/{C3}") for p in _Lrr.calls)
    assert "Packed With Rar: a .rar archive" in r.stderr
    c = json.loads(r.stdout)
    assert c["archives_moved"] == 2 and c["archives_unreadable"] == 1 and c["added"] == 4


def test_a_second_run_fetches_nothing_again(lrr, kura, tmp_path):
    assert _migrate(lrr, kura, tmp_path).returncode == 0
    _Lrr.calls = []
    r = _migrate(lrr, kura, tmp_path)
    assert r.returncode == 0, r.stderr
    assert not any(p.endswith("/download") for p in _Lrr.calls)
    assert json.loads(r.stdout)["archives_skipped"] == 2


def test_a_failed_download_is_taken_up_next_run(lrr, kura, tmp_path):
    _Lrr.fail_once = {B2}
    r = _migrate(lrr, kura, tmp_path)
    assert r.returncode == 1 and json.loads(r.stdout)["archives_failed"] == 1
    assert [u["name"] for u in _Kura.uploads] == ["only.png"]
    r = _migrate(lrr, kura, tmp_path)
    assert r.returncode == 0, r.stderr
    assert [u["name"] for u in _Kura.uploads] == ["only.png", "p1.png", "p2.png", "p10.png"]


def test_the_api_key_goes_as_lanraragi_wants_it_and_only_there(lrr, kura, tmp_path):
    _Lrr.key = "lrr-secret"
    r = _migrate(lrr, kura, tmp_path)
    assert r.returncode == 1 and "refused (401)" in r.stderr and "--from-key-file" in r.stderr
    assert "Traceback" not in r.stderr and not _Kura.calls          # No-Fun Mode without the key: said, nothing sent
    r = run("--from", lrr, "--from-key-file", _key(tmp_path, "lrr.key", "lrr-secret\n"), "--url", kura,
            "--email", "me@example.org", "--key-file", _key(tmp_path), "--record", tmp_path / "rec2.jsonl")
    assert r.returncode == 0, r.stderr
    assert len(_Kura.uploads) == 4


def test_dry_run_lists_archives_and_sends_nothing(lrr, kura, tmp_path):
    r = run("--from", lrr, "--url", kura, "--dry-run", "--record", tmp_path / "rec.jsonl")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[1].startswith("Night Ferry  [cbz, 3 pages]  jane_doe orbit_studio")
    assert not _Kura.calls and not (tmp_path / "rec.jsonl").exists()


def test_a_record_is_bound_to_its_migration(lrr, kura, tmp_path):
    assert _migrate(lrr, kura, tmp_path).returncode == 0
    r = _migrate(lrr, kura, tmp_path, "--safety", "safe")
    assert r.returncode == 1 and "belongs to a different migration" in r.stderr


class _FlakyKura(_Kura):
    fail_names: set[str] = set()        # uploads of these filenames answer 503 once each

    def do_POST(self):
        if self.path == "/api/upload" and self.fail_names:
            n = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(n)
            hit = next((x for x in self.fail_names if f'filename="{x}"'.encode() in body), None)
            if hit:
                _FlakyKura.fail_names.discard(hit)
                self.calls.append(("POST", self.path))
                return _reply(self, 503, {"detail": "busy"})
            self.rfile = io.BytesIO(body)
        return super().do_POST()


def test_a_page_kura_did_not_take_stops_its_archive_so_the_pool_keeps_its_order(lrr, tmp_path):
    _Kura.uploads, _Kura.tags, _Kura.pools, _Kura.calls = [], {}, [], []
    _FlakyKura.fail_names = {"p2.png"}
    srv = _server(_FlakyKura)
    try:
        url = f"http://127.0.0.1:{srv.server_port}"
        r = _migrate(lrr, url, tmp_path)
        assert r.returncode == 1 and "the rest of this archive waits" in r.stderr
        assert [u["name"] for u in _Kura.uploads] == ["only.png", "p1.png"]      # p10 not sent after p2 failed
        r = _migrate(lrr, url, tmp_path)
        assert r.returncode == 0, r.stderr
        night = [u["name"] for u in _Kura.uploads if u["pool"] == "Night Ferry"]
        assert night == ["p1.png", "p2.png", "p10.png"]
    finally:
        srv.shutdown()


def test_a_damaged_page_is_passed_over_and_the_rest_go_in(lrr, kura, tmp_path):
    marker = b"MARKER-0123456789"
    body = PNG + os.urandom(4000) + marker + os.urandom(4000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("p1.png", body)
        z.writestr("p2.png", PNG + os.urandom(300))
        z.writestr("p3.png", PNG + os.urandom(300))
    raw = bytearray(buf.getvalue())
    raw[raw.index(marker)] ^= 0xFF
    saved = _Lrr.files[B2]
    _Lrr.files[B2] = bytes(raw)
    try:
        r = _migrate(lrr, kura, tmp_path)
        assert r.returncode == 0, r.stderr
        assert "p1.png: damaged, passed over" in r.stderr and "Traceback" not in r.stderr
        assert [u["name"] for u in _Kura.uploads if u["pool"] == "Night Ferry"] == ["p2.png", "p3.png"]
        c = json.loads(r.stdout)
        assert c["pages_damaged"] == 1 and c["archives_moved"] == 2
        _Lrr.calls = []
        assert _migrate(lrr, kura, tmp_path).returncode == 0
        assert not any(p.endswith("/download") for p in _Lrr.calls)          # done: not fetched again
    finally:
        _Lrr.files[B2] = saved


def test_archives_with_one_title_are_two_pools(lrr, kura, tmp_path):
    saved = list(_Lrr.archives)
    _Lrr.archives = [dict(saved[1]), dict(saved[1], arcid="d" * 40)]
    _Lrr.files["d" * 40] = _zipbytes({"other.png": PNG + b"other"})
    try:
        r = _migrate(lrr, kura, tmp_path)
        assert r.returncode == 0, r.stderr
        assert sorted(u["pool"] for u in _Kura.uploads) == ["Afternoon Ferry [aaaaaaaa]", "Afternoon Ferry [dddddddd]"]
    finally:
        _Lrr.archives = saved


def test_a_category_from_any_occurrence_of_a_tag_wins(lrr, kura, tmp_path):
    saved = list(_Lrr.archives)
    _Lrr.archives = [dict(saved[1], tags="jane, artist:jane, harbor")]
    try:
        r = _migrate(lrr, kura, tmp_path)
        assert r.returncode == 0, r.stderr
        assert _Kura.uploads[0]["tags"] == "jane harbor" and _Kura.uploads[0]["artist_tags"] == "jane"
    finally:
        _Lrr.archives = saved


def test_a_pool_name_once_chosen_stays_and_names_never_collide(lrr, tmp_path):
    _Kura.uploads, _Kura.tags, _Kura.pools, _Kura.calls = [], {}, [], []
    _FlakyKura.fail_names = {"p2.png"}
    srv = _server(_FlakyKura)
    saved = list(_Lrr.archives)
    try:
        url = f"http://127.0.0.1:{srv.server_port}"
        assert _migrate(lrr, url, tmp_path).returncode == 1         # Night Ferry stops after p1
        # a second "Night Ferry" and a literal "Night Ferry [eeeeeeee]" turn up before the next run
        E, F = "e" * 40, "f" * 40
        _Lrr.archives = saved + [dict(saved[0], arcid=E), dict(saved[0], arcid=F, title="Night Ferry [eeeeeeee]")]
        _Lrr.files[E] = _zipbytes({"e.png": PNG + b"e"})
        _Lrr.files[F] = _zipbytes({"f.png": PNG + b"f"})
        r = _migrate(lrr, url, tmp_path)
        assert r.returncode == 0, r.stderr
        pools = {u["name"]: u["pool"] for u in _Kura.uploads}
        assert [pools[n] for n in ("p1.png", "p2.png", "p10.png")] == ["Night Ferry"] * 3   # the book stays whole
        assert pools["f.png"] == "Night Ferry [eeeeeeee]"
        assert pools["e.png"] not in {"Night Ferry", "Night Ferry [eeeeeeee]"}
        assert pools["e.png"].startswith("Night Ferry [eeeeeeee")
    finally:
        _Lrr.archives = saved
        srv.shutdown()


def test_a_damaged_page_is_not_unpacked_again_on_a_later_run(lrr, tmp_path):
    _Kura.uploads, _Kura.tags, _Kura.pools, _Kura.calls = [], {}, [], []
    marker = b"MARKER-0123456789"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("p1.png", PNG + os.urandom(4000) + marker + os.urandom(4000))
        z.writestr("p2.png", PNG + os.urandom(300))
        z.writestr("p3.png", PNG + os.urandom(300))
    raw = bytearray(buf.getvalue())
    raw[raw.index(marker)] ^= 0xFF
    saved = _Lrr.files[B2]
    _Lrr.files[B2] = bytes(raw)
    _FlakyKura.fail_names = {"p3.png"}
    srv = _server(_FlakyKura)
    try:
        url = f"http://127.0.0.1:{srv.server_port}"
        r = _migrate(lrr, url, tmp_path)
        assert r.returncode == 1 and "p1.png: damaged" in r.stderr
        r = _migrate(lrr, url, tmp_path)
        assert r.returncode == 0, r.stderr
        assert "p1.png" not in r.stderr                              # remembered, not looked at again
        assert [u["name"] for u in _Kura.uploads if u["pool"] == "Night Ferry"] == ["p2.png", "p3.png"]
    finally:
        _Lrr.files[B2] = saved
        srv.shutdown()


def test_a_pool_name_is_free_even_when_every_candidate_is_taken(tmp_path):
    sys.path.insert(0, str(REPO / "scripts"))
    import lanraragi_migrate as m
    rec = m.Record(tmp_path / "r.jsonl", {"x": 1})
    a = {"arcid": A1, "title": "Book"}
    others = [{"arcid": "1" * 40, "title": "Book"}, {"arcid": "2" * 40, "title": f"Book [{A1[:8]}]"},
              {"arcid": "3" * 40, "title": f"Book [{A1[:16]}]"}]
    rec.pools["4" * 40] = f"Book [{A1}]"
    name = m.pool_name(rec, [a] + others, a)
    assert name == f"Book [{A1}] (2)"
    assert len(set(rec.pools.values())) == len(rec.pools)
