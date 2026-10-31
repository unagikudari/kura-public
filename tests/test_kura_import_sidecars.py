"""scripts/kura_import.py --sidecars takes the tags, rating and source that other tools left
next to the images: a booru post's JSON, XMP keywords, and .txt tag lists (one per
line as Hydrus writes them, or comma-separated as training captions are). Namespaced tags
and booru tag groups get their kura category. The kura here is the stand-in the szurubooru
test uses (it knows tag categories).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_szurubooru_migrate import _Kura, _server  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts/kura_import.py"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 12
XMP = """<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:subject><rdf:Bag>
<rdf:li>sunset</rdf:li><rdf:li>beach trip</rdf:li></rdf:Bag></dc:subject></rdf:Description></rdf:RDF></x:xmpmeta>"""


@pytest.fixture
def kura():
    _Kura.uploads, _Kura.tags, _Kura.pools, _Kura.calls, _Kura.put_fail_once = [], {}, [], [], False
    srv = _server(_Kura)
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    (root / "a.jpg").write_bytes(JPEG + b"a")
    (root / "a.jpg.json").write_text(json.dumps({
        "tag_string": "1girl solo jane_doe", "tag_string_artist": "jane_doe",
        "tag_string_character": "unit_07", "tag_string_copyright": "orbital_notes",
        "rating": "g", "source": "https://example.org/p/1"}))
    (root / "b.png").write_bytes(PNG + b"b")
    (root / "b.png.txt").write_text("creator:John Smith\nNight sky\nrating:Explicit\ncharacter:Unit 07\n")
    (root / "c.png").write_bytes(PNG + b"c")
    (root / "c.txt").write_text("1girl, smile, blue eyes\n")            # a caption beside c.png
    (root / "d.jpg").write_bytes(JPEG + b"d")
    (root / "d.xmp").write_text(XMP)
    (root / "e.png").write_bytes(PNG + b"e")                          # no sidecar at all
    (root / "f.png").write_bytes(PNG + b"f")
    (root / "f.png.json").write_text("{not json")                     # unreadable: no tags, still sent
    return root


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True,
                          env={**os.environ, "KURA_API_KEY": ""})


def _key(tmp_path):
    k = tmp_path / "admin.key"
    k.write_text("the-key\n")
    k.chmod(0o600)
    return k


def _import(folder, kura, tmp_path, *extra, record="rec.jsonl"):
    return run(folder, "--url", kura, "--email", "me@example.org", "--key-file", _key(tmp_path),
               "--record", tmp_path / record, *extra)


def test_sidecars_give_tags_categories_rating_and_source(kura, folder, tmp_path):
    r = _import(folder, kura, tmp_path, "--sidecars", "--tags", "library")
    assert r.returncode == 0, r.stderr
    up = {u["name"]: u for u in _Kura.uploads}
    assert set(up) == {"a.jpg", "b.png", "c.png", "d.jpg", "e.png", "f.png"}
    assert up["a.jpg"]["tags"] == "library jane_doe unit_07 orbital_notes 1girl solo"
    assert up["a.jpg"]["artist_tags"] == "jane_doe"
    assert up["a.jpg"]["safety"] == "safe" and up["a.jpg"]["source"] == "https://example.org/p/1"
    assert up["b.png"]["tags"] == "library john_smith night_sky unit_07"
    assert up["b.png"]["artist_tags"] == "john_smith" and up["b.png"]["safety"] == "unsafe"
    assert up["c.png"]["tags"] == "library 1girl smile blue_eyes" and "artist_tags" not in up["c.png"]
    assert up["d.jpg"]["tags"] == "library sunset beach_trip"
    assert up["e.png"]["tags"] == "library" and up["e.png"]["safety"] == "unsafe"   # no sidecar: the default
    assert up["f.png"]["tags"] == "library"
    # the categories kura has are given; copyright is not one of the stand-in's
    assert _Kura.tags["unit_07"] == "character" and _Kura.tags["jane_doe"] == "artist"
    assert _Kura.tags["john_smith"] == "artist" and _Kura.tags["orbital_notes"] == "default"
    assert "left at its default: copyright" in r.stderr
    counts = json.loads(r.stdout)
    assert counts["added"] == 6 and counts["tags_recategorized"] == 3
    # the sidecar files themselves are not images: passed over, never sent
    assert counts["not_media"] == 5


def test_the_command_line_wins_over_a_sidecar(kura, folder, tmp_path):
    r = _import(folder, kura, tmp_path, "--sidecars", "--safety", "sketchy", "--source", "https://mine.example/")
    assert r.returncode == 0, r.stderr
    assert {u["safety"] for u in _Kura.uploads} == {"sketchy"}
    assert {u["source"] for u in _Kura.uploads} == {"https://mine.example/"}


def test_without_the_flag_sidecars_are_ignored_and_old_records_stay_valid(kura, folder, tmp_path):
    r = _import(folder, kura, tmp_path)
    assert r.returncode == 0, r.stderr
    assert {u.get("tags", "") for u in _Kura.uploads} == {""} and not any("artist_tags" in u for u in _Kura.uploads)
    header = json.loads((tmp_path / "rec.jsonl").read_text().splitlines()[0])["import"]
    assert "sidecars" not in header and header["safety"] == "unsafe"       # what records before 1.301 hold
    # --sidecars is another import: its own record, not this one
    r = _import(folder, kura, tmp_path, "--sidecars")
    assert r.returncode == 1 and "belongs to a different import" in r.stderr


def test_dry_run_shows_what_each_file_would_get(folder, tmp_path):
    r = run(folder, "--url", "http://127.0.0.1:9", "--dry-run", "--sidecars", "--record", tmp_path / "rec.jsonl")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert "a.jpg  [safe] jane_doe unit_07 orbital_notes 1girl solo" in lines
    assert "b.png  [unsafe] john_smith night_sky unit_07" in lines


def test_an_oversized_or_odd_sidecar_is_not_read(kura, tmp_path):
    root = tmp_path / "odd"
    root.mkdir()
    (root / "g.png").write_bytes(PNG + b"g")
    (root / "g.png.txt").write_bytes(b"x" * ((1 << 20) + 1))
    (root / "h.png").write_bytes(PNG + b"h")
    (root / "h.png.xmp").write_text("<not xml")
    r = _import(root, kura, tmp_path, "--sidecars")
    assert r.returncode == 0, r.stderr
    assert {u["name"]: u.get("tags", "") for u in _Kura.uploads} == {"g.png": "", "h.png": ""}


def test_an_omitted_safety_and_safety_unsafe_are_different_imports(kura, folder, tmp_path):
    """With sidecars, no --safety means each sidecar's rating, --safety unsafe means unsafe for
    all: a record made one way must not let the other skip what it sent."""
    assert _import(folder, kura, tmp_path, "--sidecars").returncode == 0
    r = _import(folder, kura, tmp_path, "--sidecars", "--safety", "unsafe")
    assert r.returncode == 1 and "belongs to a different import" in r.stderr


def test_a_sidecar_added_or_edited_later_sends_its_image_again(kura, folder, tmp_path):
    assert _import(folder, kura, tmp_path, "--sidecars").returncode == 0
    n = len(_Kura.uploads)
    (folder / "e.png.txt").write_text("late tag\n")                 # e.png had none
    (folder / "c.txt").write_text("1girl, smile, blue eyes, hat\n")   # c.png's caption grew
    r = _import(folder, kura, tmp_path, "--sidecars")
    assert r.returncode == 0, r.stderr
    again = {u["name"]: u["tags"] for u in _Kura.uploads[n:]}
    assert again == {"c.png": "1girl smile blue_eyes hat", "e.png": "late_tag"}
    assert json.loads(r.stdout)["duplicates"] == 2                  # the same posts, now with the new tags
    r = _import(folder, kura, tmp_path, "--sidecars")
    assert json.loads(r.stdout)["skipped"] == 6 and len(_Kura.uploads) == n + 2


class _RefusingKura(_Kura):
    patch_fail = 0                      # how many tag PATCHes to answer 503

    def do_PATCH(self):
        if _RefusingKura.patch_fail:
            _RefusingKura.patch_fail -= 1
            self._body()
            self.calls.append(("PATCH", self.path))
            from test_szurubooru_migrate import _reply
            return _reply(self, 503, {"detail": "busy"})
        return super().do_PATCH()


def test_category_work_left_undone_is_taken_up_by_the_next_run(tmp_path, folder):
    _Kura.uploads, _Kura.tags, _Kura.pools, _Kura.calls = [], {}, [], []
    _RefusingKura.patch_fail = 1
    srv = _server(_RefusingKura)
    try:
        url = f"http://127.0.0.1:{srv.server_port}"
        r = _import(folder, url, tmp_path, "--sidecars")
        assert r.returncode == 0, r.stderr
        assert "category not set (503" in r.stderr
        not_set = [n for n in ("jane_doe", "john_smith", "unit_07") if _Kura.tags[n] == "default"]
        assert len(not_set) == 1                                    # the first PATCH was refused
        r = _import(folder, url, tmp_path, "--sidecars")            # nothing new to send
        assert r.returncode == 0, r.stderr
        c = json.loads(r.stdout)
        assert c["skipped"] == 6 and c["tags_recategorized"] == 1
        assert all(_Kura.tags[n] != "default" for n in ("jane_doe", "john_smith", "unit_07"))
        patches = sum(1 for m, _ in _Kura.calls if m == "PATCH")
        assert json.loads(_import(folder, url, tmp_path, "--sidecars").stdout).get("tags_recategorized", 0) == 0
        assert sum(1 for m, _ in _Kura.calls if m == "PATCH") == patches   # settled: not asked again
    finally:
        srv.shutdown()


def test_a_sidecar_is_read_once_and_held_to_the_limit(tmp_path):
    sys.path.insert(0, str(REPO / "scripts"))
    import kura_import
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (kura_import.SIDECAR_LIMIT + 1))
    assert kura_import._read_small(big) is None
    ok = tmp_path / "ok.txt"
    ok.write_bytes(b"a, b")
    assert kura_import._read_small(ok) == b"a, b"
    fifo = tmp_path / "pipe.txt"
    os.mkfifo(fifo)                                                 # nothing will ever write to it
    assert kura_import._read_small(fifo) is None                    # refused at once, not waited on


def test_a_record_from_1_301_0_with_mixed_case_categories_settles(kura, folder, tmp_path):
    """1.301.0 kept a sidecar tag's case in the record's categories while kura and the
    "categorized" rows use the lowercase name; reading such a record must not keep that work
    pending for ever."""
    assert _import(folder, kura, tmp_path, "--sidecars").returncode == 0
    rec = tmp_path / "rec.jsonl"
    rows = [json.loads(l) for l in rec.read_text().splitlines()]
    for r in rows:
        if r.get("categories"):
            r["categories"] = {n.title(): c for n, c in r["categories"].items()}   # as 1.301.0 could write
    rec.write_text("".join(json.dumps(r) + "\n" for r in rows))
    sys.path.insert(0, str(REPO / "scripts"))
    import kura_import
    pending: dict = {}
    kura_import.open_record(rec, rows[0]["import"], categories=pending)
    assert pending == {}                                            # every one was settled by run 1
