"""scripts/kura_import.py --archives takes the pages of .zip and .cbz archives: each archive is a
pool of its pages in natural order, folders, hidden and macOS entries and non-images inside are
passed over, a page that would unpack to far more than it looks is refused, a ComicInfo.xml
gives tags with --sidecars, and a page is known by its own size and CRC, so adding a page to
an archive sends only that page. The kura here is the stand-in the szurubooru test uses.
"""
from __future__ import annotations

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
from test_szurubooru_migrate import _Kura, _server  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts/kura_import.py"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 12
COMIC = """<?xml version="1.0"?>
<ComicInfo><Title>Night Ferry</Title><Series>Orbital Notes</Series><Writer>Jane Doe</Writer>
<Penciller>Jane Doe, John Smith</Penciller><Characters>Unit 07</Characters><Genre>Science Fiction</Genre>
<Tags>space, ferry</Tags><AgeRating>Teen</AgeRating><Web>https://example.org/book/1</Web></ComicInfo>"""


@pytest.fixture
def kura():
    _Kura.uploads, _Kura.tags, _Kura.pools, _Kura.calls, _Kura.put_fail_once = [], {}, [], [], False
    srv = _server(_Kura)
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()


def _zip(path: Path, members: dict[str, bytes], compression=zipfile.ZIP_STORED) -> None:
    with zipfile.ZipFile(path, "w", compression) as z:
        for name, data in members.items():
            z.writestr(name, data)


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "comics"
    root.mkdir()
    (root / "cover.png").write_bytes(PNG + b"cover")
    _zip(root / "Night Ferry.cbz", {
        "page10.png": PNG + b"10", "page2.png": PNG + b"2", "page1.jpg": JPEG + b"1",
        "extras/sketch.png": PNG + b"sketch",
        "__MACOSX/._page1.jpg": PNG + b"resource fork", ".thumbs/t.png": PNG + b"thumb",
        "notes.txt": b"not a page", "ComicInfo.xml": COMIC.encode(),
    })
    _zip(root / "broken.zip", {"a.png": PNG + b"a"})
    (root / "broken.zip").write_bytes((root / "broken.zip").read_bytes()[:30])   # cut short
    return root


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True,
                          env={**os.environ, "KURA_API_KEY": ""})


def _key(tmp_path):
    k = tmp_path / "admin.key"
    k.write_text("the-key\n")
    k.chmod(0o600)
    return k


def _import(folder, kura, tmp_path, *extra):
    return run(folder, "--url", kura, "--email", "me@example.org", "--key-file", _key(tmp_path),
               "--record", tmp_path / "rec.jsonl", *extra)


def test_an_archive_is_a_pool_of_its_pages_in_order(kura, folder, tmp_path):
    r = _import(folder, kura, tmp_path, "--archives")
    assert r.returncode == 0, r.stderr
    sent = [(u["name"], u.get("pool", "")) for u in _Kura.uploads]
    assert sent == [("cover.png", ""), ("page1.jpg", "Night Ferry"), ("page2.png", "Night Ferry"),
                    ("page10.png", "Night Ferry"), ("sketch.png", "Night Ferry")]
    c = json.loads(r.stdout)
    assert c["added"] == 5 and c["failed"] == 0
    assert c["not_media"] == 2                         # notes.txt inside, and the archive that is cut short
    assert all("tags" not in u for u in _Kura.uploads)  # no --sidecars: ComicInfo is not read
    rows = [json.loads(l) for l in (tmp_path / "rec.jsonl").read_text().splitlines()]
    assert rows[0]["import"]["archives"] is True
    assert rows[2]["path"] == "Night Ferry.cbz/page1.jpg" and isinstance(rows[2]["crc"], int)


def test_comic_info_gives_tags_rating_and_source(kura, folder, tmp_path):
    r = _import(folder, kura, tmp_path, "--archives", "--sidecars")
    assert r.returncode == 0, r.stderr
    page = next(u for u in _Kura.uploads if u["name"] == "page1.jpg")
    assert page["tags"] == "jane_doe john_smith unit_07 orbital_notes science_fiction space ferry"
    assert page["artist_tags"] == "jane_doe john_smith"
    assert page["safety"] == "sketchy" and page["source"] == "https://example.org/book/1"
    cover = next(u for u in _Kura.uploads if u["name"] == "cover.png")
    assert "tags" not in cover                          # ComicInfo belongs to the archive's pages only
    assert _Kura.tags["unit_07"] == "character" and _Kura.tags["jane_doe"] == "artist"


def test_without_the_flag_an_archive_is_just_another_file(kura, folder, tmp_path):
    r = _import(folder, kura, tmp_path)
    assert r.returncode == 0, r.stderr
    assert [u["name"] for u in _Kura.uploads] == ["cover.png"]
    header = json.loads((tmp_path / "rec.jsonl").read_text().splitlines()[0])["import"]
    assert "archives" not in header                    # records made before --archives stay valid


def test_a_page_added_to_an_archive_sends_only_that_page(kura, folder, tmp_path):
    assert _import(folder, kura, tmp_path, "--archives").returncode == 0
    n = len(_Kura.uploads)
    book = folder / "Night Ferry.cbz"
    with zipfile.ZipFile(book, "a") as z:
        z.writestr("page11.png", PNG + b"11")
    r = _import(folder, kura, tmp_path, "--archives")
    assert r.returncode == 0, r.stderr
    assert [u["name"] for u in _Kura.uploads[n:]] == ["page11.png"]
    assert json.loads(r.stdout)["skipped"] == 5


def test_a_page_that_would_unpack_to_far_more_is_refused(kura, tmp_path):
    root = tmp_path / "odd"
    root.mkdir()
    _zip(root / "bomb.zip", {"ok.png": PNG + os.urandom(64), "huge.png": PNG + b"\0" * (4 << 20)},
         compression=zipfile.ZIP_DEFLATED)
    r = _import(root, kura, tmp_path, "--archives")
    assert r.returncode == 0, r.stderr
    assert [u["name"] for u in _Kura.uploads] == ["ok.png"]


def test_an_archive_that_changes_before_its_page_goes_fails_that_page(kura, folder, tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("kura_import_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    items, _, _ = mod.plan(folder, archives=True)
    page = next(i for i in items if i.member == "page2.png")
    _zip(folder / "Night Ferry.cbz", {"page2.png": PNG + b"rewritten"})     # same name, other bytes
    api = mod.Api(kura, None)
    api.authorization = "Token x"
    with pytest.raises(mod.FileChanged):
        api.upload(page, {"tags": ""})
    assert not _Kura.uploads


def test_the_watch_takes_an_archive_once_it_has_settled(kura, tmp_path):
    root = tmp_path / "inbox"
    root.mkdir()
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), str(root), "--url", kura, "--email", "me@example.org",
         "--key-file", str(_key(tmp_path)), "--record", str(tmp_path / "rec.jsonl"),
         "--watch", "1", "--settle", "60", "--archives"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**os.environ, "KURA_API_KEY": ""})
    try:
        book = root / "Book.cbz"
        _zip(book, {"p1.png": PNG + b"1", "p2.png": PNG + b"2"})
        time.sleep(2.5)
        assert not _Kura.uploads                         # just written: waits
        t = time.time() - 120
        os.utime(book, (t, t))
        end = time.monotonic() + 20
        while time.monotonic() < end and len(_Kura.uploads) < 2:
            time.sleep(0.1)
        assert [(u["name"], u.get("pool")) for u in _Kura.uploads] == [("p1.png", "Book"), ("p2.png", "Book")]
        time.sleep(2.5)
        assert len(_Kura.uploads) == 2                   # not again on later passes
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=20)


def test_a_damaged_page_is_passed_over_until_its_archive_changes(kura, tmp_path):
    root = tmp_path / "bad"
    root.mkdir()
    marker = b"MARKER-0123456789"
    body = PNG + os.urandom(8000) + marker + os.urandom(8000)       # random: stored as is inside deflate
    _zip(root / "book.zip", {"a.png": body, "b.png": PNG + os.urandom(500)}, compression=zipfile.ZIP_DEFLATED)
    raw = bytearray((root / "book.zip").read_bytes())
    at = raw.index(marker)
    raw[at] ^= 0xFF                                                  # one byte off: the CRC no longer matches
    (root / "book.zip").write_bytes(bytes(raw))
    # and one page past the first read (1 MiB), broken further in: found while it streams
    big = PNG + os.urandom(1_300_000) + marker + os.urandom(1000)
    _zip(root / "book2.zip", {"c.png": big, "d.png": PNG + os.urandom(500)}, compression=zipfile.ZIP_DEFLATED)
    raw = bytearray((root / "book2.zip").read_bytes())
    raw[raw.index(marker)] ^= 0xFF
    (root / "book2.zip").write_bytes(bytes(raw))
    r = _import(root, kura, tmp_path, "--archives")
    assert r.returncode == 0, r.stderr                               # damaged pages are recorded, not a crash
    assert "Traceback" not in r.stderr and r.stderr.count("damaged, passed over") == 2
    assert {u["name"] for u in _Kura.uploads} >= {"b.png", "d.png"}
    assert not {"a.png", "c.png"} & {u["name"] for u in _Kura.uploads}
    c = json.loads(r.stdout)
    assert c["failed"] == 0 and c["damaged"] == 2
    rows = [json.loads(l) for l in (tmp_path / "rec.jsonl").read_text().splitlines()[1:]]
    assert sorted(x["path"] for x in rows if x.get("damaged")) == ["book.zip/a.png", "book2.zip/c.png"]
    r = _import(root, kura, tmp_path, "--archives")                  # unchanged archives: not tried again
    assert r.returncode == 0 and "damaged, passed over" not in r.stderr
    c = json.loads(r.stdout)
    assert c.get("damaged", 0) == 0 and c["refused_before"] == 2


def test_two_entries_of_one_name_are_passed_over(kura, tmp_path):
    root = tmp_path / "dup"
    root.mkdir()
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(root / "book.zip", "w") as z:
            z.writestr("p.png", PNG + b"first")
            z.writestr("p.png", PNG + b"second")
            z.writestr("q.png", PNG + b"q")
    for _ in range(2):                                               # and again: nothing keeps failing
        r = _import(root, kura, tmp_path, "--archives")
        assert r.returncode == 0, r.stderr
    assert [u["name"] for u in _Kura.uploads] == ["q.png"]


def test_an_edited_comic_info_sends_the_pages_again_an_added_page_does_not(kura, folder, tmp_path):
    assert _import(folder, kura, tmp_path, "--archives", "--sidecars").returncode == 0
    n = len(_Kura.uploads)
    book = folder / "Night Ferry.cbz"
    with zipfile.ZipFile(book, "a") as z:
        z.writestr("page11.png", PNG + b"11")
    r = _import(folder, kura, tmp_path, "--archives", "--sidecars")
    assert [u["name"] for u in _Kura.uploads[n:]] == ["page11.png"]
    n = len(_Kura.uploads)
    members = {}
    with zipfile.ZipFile(book) as z:
        for i in z.infolist():
            members[i.filename] = z.read(i)
    members["ComicInfo.xml"] = COMIC.replace("<Tags>space, ferry</Tags>", "<Tags>space, ferry, night</Tags>").encode()
    _zip(book, members)
    r = _import(folder, kura, tmp_path, "--archives", "--sidecars")
    assert r.returncode == 0, r.stderr
    again = [u for u in _Kura.uploads[n:]]
    assert len(again) == 5 and all(u["tags"].endswith("space ferry night") for u in again)


def test_archives_of_one_name_in_two_folders_are_two_pools(kura, tmp_path):
    root = tmp_path / "series"
    (root / "Alpha").mkdir(parents=True)
    (root / "Beta").mkdir()
    _zip(root / "Alpha" / "Vol 1.cbz", {"1.png": PNG + b"a1"})
    _zip(root / "Beta" / "Vol 1.cbz", {"1.png": PNG + b"b1"})
    r = _import(root, kura, tmp_path, "--archives")
    assert r.returncode == 0, r.stderr
    assert [u["pool"] for u in _Kura.uploads] == ["Alpha / Vol 1", "Beta / Vol 1"]


def test_a_repaired_archive_sends_the_page_that_was_damaged(kura, tmp_path):
    """A repair restores the packed bytes; the page's declared size and CRC were right all
    along. The damaged mark belongs to the broken copy of the archive, not to the page."""
    root = tmp_path / "fix"
    root.mkdir()
    marker = b"MARKER-0123456789"
    body = PNG + os.urandom(4000) + marker + os.urandom(4000)
    _zip(root / "book.zip", {"a.png": body, "b.png": PNG + os.urandom(300)}, compression=zipfile.ZIP_DEFLATED)
    good = (root / "book.zip").read_bytes()
    raw = bytearray(good)
    raw[raw.index(marker)] ^= 0xFF
    (root / "book.zip").write_bytes(bytes(raw))
    t = time.time() - 300
    os.utime(root / "book.zip", (t, t))
    r = _import(root, kura, tmp_path, "--archives")
    assert r.returncode == 0 and "a.png: damaged" in r.stderr
    (root / "book.zip").write_bytes(good)                           # repaired: same entries, sound bytes
    r = _import(root, kura, tmp_path, "--archives")
    assert r.returncode == 0, r.stderr
    assert [u["name"] for u in _Kura.uploads] == ["b.png", "a.png"]


def test_a_repair_that_lands_while_the_damaged_copy_is_read_is_not_marked(kura, tmp_path):
    """The damaged mark carries the mtime of the copy that was read, from its open descriptor:
    a repaired archive moved into place meanwhile is not marked by the old copy's failure."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("kura_import_r", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    root = tmp_path / "race"
    root.mkdir()
    marker = b"MARKER-0123456789"
    _zip(root / "book.zip", {"a.png": PNG + os.urandom(1_300_000) + marker + os.urandom(1000)},
         compression=zipfile.ZIP_DEFLATED)
    good = (root / "book.zip").read_bytes()
    raw = bytearray(good)
    raw[raw.index(marker)] ^= 0xFF
    (root / "book.zip").write_bytes(bytes(raw))
    t = time.time() - 300
    os.utime(root / "book.zip", (t, t))
    old_mtime = (root / "book.zip").stat().st_mtime_ns
    items, _, _ = mod.plan(root, archives=True)
    (root / "repaired.zip").write_bytes(good)

    class Repairing(mod.Api):
        def call(self, method, path, body=None, headers=None, timeout=60.0):
            os.replace(root / "repaired.zip", root / "book.zip")    # the repair lands mid-stream
            return super().call(method, path, body, headers, timeout)

    api = Repairing(kura, None)
    api.authorization = "Token x"
    with pytest.raises(mod.Damaged) as caught:
        api.upload(items[0], {"tags": ""})
    assert caught.value.archive_mtime == old_mtime
    assert (root / "book.zip").stat().st_mtime_ns != old_mtime
