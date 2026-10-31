"""scripts/hydrus_migrate.py moves a Hydrus Network library into kura through the Client
API: every current file with its tags (namespaces mapped to categories, creator → artist
declared), its first known URL as the source, one safety for all. The Hydrus here is a
stand-in answering both metadata shapes the API has had; the kura stand-in is the one the
szurubooru test uses.
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_szurubooru_migrate import _Kura, _reply, _server  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts/hydrus_migrate.py"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 12
H = {"a1": "a" * 64, "b2": "b" * 64, "c3": "c" * 64, "d4": "d" * 64}


class _Hydrus(http.server.BaseHTTPRequestHandler):
    files = {H["a1"]: JPEG + b"one", H["b2"]: PNG + b"two", H["c3"]: b"FWS\x0a" + b"\0" * 20, H["d4"]: PNG + b"two"}
    metadata = [
        # the current shape: tags keyed by service key, statuses as strings
        {"file_id": 7, "hash": H["a1"], "mime": "image/jpeg", "known_urls": ["https://example.org/a", "https://x/y"],
         "tags": {"svc1": {"storage_tags": {"0": ["creator:jane doe", "night"], "1": ["old tag"]},
                           "display_tags": {"0": ["creator:jane doe", "night", "series:orbital notes"]}}}},
        # the older shape
        {"file_id": 3, "hash": H["b2"], "mime": "image/png", "known_urls": [],
         "service_names_to_statuses_to_display_tags": {"my tags": {"0": ["character:unit 07", "night", "rating:5"]}}},
        {"file_id": 9, "hash": H["c3"], "mime": "application/x-shockwave-flash", "known_urls": [], "tags": {}},
        {"file_id": 11, "hash": H["d4"], "mime": "image/png", "known_urls": [], "tags": {}},
        {"file_id": 12, "hash": "e" * 64, "mime": "image/png", "is_trashed": True, "tags": {}},
    ]
    calls: list[tuple[str, str | None]] = []
    many = False                        # 101 plain files with ids 1000..1100, handed back unsorted

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        self.calls.append((self.path, self.headers.get("Hydrus-Client-API-Access-Key")))
        if self.headers.get("Hydrus-Client-API-Access-Key") != "hydrus-key":
            return _reply(self, 403, {"error": "bad key"})
        if u.path == "/get_files/search_files":
            assert json.loads(q["tags"][0]) == ["system:everything"]
            return _reply(self, 200, {"file_ids": list(range(1100, 999, -1)) if self.many else [11, 3, 9, 7, 12]})
        if u.path == "/get_files/file_metadata":
            ids = json.loads(q["file_ids"][0])
            assert len(ids) <= 100
            if self.many:
                return _reply(self, 200, {"metadata": [{"file_id": i, "hash": f"{i:064x}", "mime": "image/png", "tags": {}} for i in ids]})
            return _reply(self, 200, {"metadata": [m for m in self.metadata if m["file_id"] in ids]})
        if u.path == "/get_files/file":
            sha = q["hash"][0]
            if self.many:
                return _reply(self, 200, PNG + sha.encode()[-8:], "application/octet-stream")
            if sha in self.files:
                return _reply(self, 200, self.files[sha], "application/octet-stream")
        return _reply(self, 404, {"error": "no"})

    def log_message(self, *a):
        pass


@pytest.fixture
def hydrus():
    _Hydrus.calls = []
    srv = _server(_Hydrus)
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


@pytest.fixture
def creds(tmp_path):
    key = tmp_path / "admin.key"; key.write_text("the-key\n"); key.chmod(0o600)
    hk = tmp_path / "hydrus.key"; hk.write_text("hydrus-key\n"); hk.chmod(0o600)
    return key, hk


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True,
                          env={**os.environ, "KURA_API_KEY": ""})


def _migrate(hydrus, kura, creds, tmp_path, *extra):
    key, hk = creds
    return run("--from", hydrus, "--from-key-file", hk, "--url", kura, "--email", "me@example.org",
               "--key-file", key, "--record", tmp_path / "rec.jsonl", *extra)


def test_files_tags_and_categories_move_over(hydrus, kura, creds, tmp_path):
    r = _migrate(hydrus, kura, creds, tmp_path, "--safety", "sketchy")
    assert r.returncode == 0, r.stderr
    c = json.loads(r.stdout)
    assert c == {"posts": 4, "added": 2, "duplicates": 1, "failed": 0, "unsupported": 1, "skipped": 0,
                 "tags_recategorized": 2, "seconds": pytest.approx(0, abs=60)}
    ups = _Kura.uploads                                        # file id order 3, 7, (9 passed over), 11
    assert [u["name"] for u in ups] == [f"{H['b2']}.png", f"{H['a1']}.jpg", f"{H['d4']}.png"]
    assert ups[0]["tags"] == "unit_07 night rating:5" and ups[0].get("artist_tags") is None
    assert ups[1]["tags"] == "jane_doe night orbital_notes" and ups[1]["artist_tags"] == "jane_doe"
    assert ups[1]["source"] == "https://example.org/a" and {u["safety"] for u in ups} == {"sketchy"}
    assert "post 9: passed over (application/x-shockwave-flash)" in r.stderr
    # creator → artist and character → character exist in kura; series → copyright does not
    assert _Kura.tags == {"unit_07": "character", "night": "default", "rating:5": "default",
                          "jane_doe": "artist", "orbital_notes": "default"}
    assert "left at its default: copyright" in r.stderr
    assert all(k == "hydrus-key" for _, k in _Hydrus.calls)
    assert sum(1 for p, _ in _Hydrus.calls if p.startswith("/get_files/file?")) == 4   # one download each (the flash file too, before it is passed over), the trashed file never
    rows = [json.loads(l) for l in (tmp_path / "rec.jsonl").read_text().splitlines()]
    assert rows[0]["migrate"] == {"source": "hydrus", "from": hydrus, "url": kura, "safety": "sketchy",
                                  "namespaces": {"character": "character", "creator": "artist", "series": "copyright"}}
    assert [x.get("post") for x in rows[1:]] == [3, 7, 9, 11]


def test_a_second_run_skips_what_is_in(hydrus, kura, creds, tmp_path):
    assert _migrate(hydrus, kura, creds, tmp_path).returncode == 0
    n = len(_Kura.uploads)
    r = _migrate(hydrus, kura, creds, tmp_path)
    assert r.returncode == 0, r.stderr
    c = json.loads(r.stdout)
    assert c["skipped"] == 3 and c["added"] == 0 and c["unsupported"] == 1 and len(_Kura.uploads) == n


def test_namespace_mapping_is_configurable(hydrus, kura, creds, tmp_path):
    r = _migrate(hydrus, kura, creds, tmp_path, "--namespace", "creator=character,rating=artist")
    assert r.returncode == 0, r.stderr
    assert _Kura.uploads[1]["tags"] == "jane_doe night series:orbital_notes"      # series unmapped: kept whole
    assert _Kura.uploads[1].get("artist_tags") is None                          # creator is no longer the artist namespace
    assert _Kura.uploads[0]["tags"] == "character:unit_07 night 5" and _Kura.uploads[0]["artist_tags"] == "5"
    assert _Kura.tags["jane_doe"] == "character" and _Kura.tags["5"] == "artist"


def test_a_record_is_bound_to_safety_and_namespace_mapping(hydrus, kura, creds, tmp_path):
    """Other settings would give new files other tags or safety while the old ones keep theirs,
    so a record made with one setting refuses a run with another."""
    assert _migrate(hydrus, kura, creds, tmp_path).returncode == 0
    n = len(_Kura.uploads)
    for extra in (["--safety", "safe"], ["--namespace", "creator=character"]):
        r = _migrate(hydrus, kura, creds, tmp_path, *extra)
        assert r.returncode == 1 and "belongs to a different migration" in r.stderr, r.stderr
        assert len(_Kura.uploads) == n
    # the same mapping spelled in another order is the same import
    r = _migrate(hydrus, kura, creds, tmp_path, "--namespace", "series=copyright,character=character,creator=artist")
    assert r.returncode == 0 and json.loads(r.stdout)["skipped"] == 3


def test_metadata_is_fetched_in_chunks_and_files_go_in_id_order(hydrus, kura, creds, tmp_path):
    _Hydrus.many = True
    try:
        r = _migrate(hydrus, kura, creds, tmp_path)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["added"] == 101
        assert sum(1 for p, _ in _Hydrus.calls if p.startswith("/get_files/file_metadata")) == 2
        assert [u["name"] for u in _Kura.uploads] == [f"{i:064x}.png" for i in range(1000, 1101)]
    finally:
        _Hydrus.many = False


def test_dry_run_lists_and_writes_nothing(hydrus, kura, creds, tmp_path):
    key, hk = creds
    r = run("--from", hydrus, "--from-key-file", hk, "--url", kura, "--dry-run", "--record", tmp_path / "rec.jsonl")
    assert r.returncode == 0, r.stderr
    assert "4 files (0 already moved)" in r.stderr
    assert r.stdout.splitlines()[0] == "file 3  image/png  unit_07 night rating:5  "
    assert not _Kura.calls and not (tmp_path / "rec.jsonl").exists()


def test_secrets_come_from_private_files_only(hydrus, kura, creds, tmp_path):
    key, hk = creds
    hk.chmod(0o644)
    r = _migrate(hydrus, kura, creds, tmp_path)
    assert r.returncode == 1 and "mode 0600" in r.stderr and not _Hydrus.calls and not _Kura.calls
    r = run("--help")
    assert r.returncode == 0 and "--from-key " not in r.stdout and "--from-key-file" in r.stdout
