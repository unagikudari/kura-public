"""scripts/szurubooru_migrate.py moves a szurubooru library into kura through the two HTTP APIs:
posts with tags, safety and source (artist tags declared), tag categories where kura has them,
pools in order. Both servers here are stand-ins that answer like the real ones.
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
import urllib.parse
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts/szurubooru_migrate.py"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 12


def _reply(h, status, body, content_type="application/json"):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    h.send_response(status)
    h.send_header("content-type", content_type)
    h.send_header("content-length", str(len(raw)))
    h.end_headers()
    h.wfile.write(raw)


class _Szuru(http.server.BaseHTTPRequestHandler):
    """A szurubooru: paged /api/posts/ and /api/pools/, files under /data/."""
    files = {
        "data/posts/1_a.jpg": JPEG + b"one", "data/posts/2_b.png": PNG + b"two",
        "data/posts/3_c.png": PNG + b"three", "data/posts/4_d.swf": b"FWS\x0a" + b"\0" * 20,
        "data/posts/5_e.png": PNG + b"two",          # the same bytes as post 2: a duplicate in kura
    }
    posts = [
        {"id": 1, "contentUrl": "data/posts/1_a.jpg", "safety": "safe", "source": "https://example.org/1",
         "mimeType": "image/jpeg", "tags": [{"names": ["studio_orbit"], "category": "artist", "usages": 2},
                                            {"names": ["night", "apps"], "category": "default", "usages": 2}]},
        {"id": 2, "contentUrl": "data/posts/2_b.png", "safety": "sketchy", "source": "",
         "mimeType": "image/png", "tags": [{"names": ["Unit_07"], "category": "character", "usages": 1},
                                           {"names": ["night", "apps"], "category": "default", "usages": 2}]},
        {"id": 3, "contentUrl": "data/posts/3_c.png", "safety": "unsafe", "source": None,
         "mimeType": "image/png", "tags": [{"names": ["studio_orbit"], "category": "artist", "usages": 2},
                                           {"names": ["rare_cat"], "category": "lore", "usages": 1}]},
        {"id": 4, "contentUrl": "data/posts/4_d.swf", "safety": "safe", "source": "",
         "mimeType": "application/x-shockwave-flash", "tags": []},
        {"id": 5, "contentUrl": "data/posts/5_e.png", "safety": "safe", "source": "",
         "mimeType": "image/png", "tags": []},
    ]
    pools = [{"id": 10, "names": ["night_series", "ns"], "description": "the series",
              "posts": [{"id": 2}, {"id": 1}, {"id": 3}, {"id": 4}]}]
    calls: list[tuple[str, str]] = []
    page = 2
    fail_once: set[str] = set()          # content paths that answer 500 the first time they are asked
    truncate: set[str] = set()           # content paths whose body stops short of Content-Length

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        self.calls.append((self.path, self.headers.get("authorization")))
        key = u.path.lstrip("/")
        if key in self.files:
            if key in self.fail_once:
                self.fail_once.discard(key)
                return _reply(self, 500, {"name": "Boom"})
            if key in self.truncate:
                self.send_response(200); self.send_header("content-type", "application/octet-stream")
                self.send_header("content-length", str(len(self.files[key]) + 100)); self.end_headers()
                self.wfile.write(self.files[key]); self.wfile.flush(); self.close_connection = True
                return
            return _reply(self, 200, self.files[key], "application/octet-stream")
        if not self.headers.get("authorization", "").startswith("Token "):
            return _reply(self, 403, {"name": "AuthError"})
        if u.path in ("/api/posts/", "/api/pools/"):
            items = self.posts if u.path == "/api/posts/" else self.pools
            if u.path == "/api/posts/" and q.get("query", [""])[0] != "-sort:id":   # ascending ids, as szurubooru spells it
                return _reply(self, 400, {"name": "SearchError", "description": "unknown sort"})
            off, lim = int(q.get("offset", [0])[0]), int(q.get("limit", [100])[0])
            lim = min(lim, self.page)          # a smaller page than asked, so paging is exercised
            fields = q.get("fields", [""])[0].split(",")
            res = [{k: v for k, v in it.items() if k in fields} for it in items[off:off + lim]]
            return _reply(self, 200, {"query": "", "offset": off, "limit": lim, "total": len(items), "results": res})
        return _reply(self, 404, {"name": "NotFound"})

    def log_message(self, *a):
        pass


class _Kura(http.server.BaseHTTPRequestHandler):
    uploads: list[dict] = []
    categories = [{"name": "default", "color": "#ccc", "default": True}, {"name": "artist", "color": "#a00", "default": False},
                  {"name": "character", "color": "#0a0", "default": False}]
    tags: dict[str, str] = {}        # name → category, as kura holds them
    pools: list[dict] = []
    calls: list[tuple[str, str]] = []

    def _body(self):
        return self.rfile.read(int(self.headers.get("content-length") or 0))

    def do_GET(self):
        self.calls.append(("GET", self.path))
        if self.path == "/api/tag-categories":
            return _reply(self, 200, self.categories)
        if self.path.startswith("/api/pools/"):
            key = urllib.parse.unquote(self.path[len("/api/pools/"):])
            found = next((p for p in self.pools if key in p["names"] or (key.isdigit() and p["id"] == int(key))), None)
            return _reply(self, 200, {**found, "version": 3}) if found else _reply(self, 404, {"detail": "Pool not found"})
        if self.path.startswith("/api/tag/"):
            name = urllib.parse.unquote(self.path[len("/api/tag/"):])
            if name not in self.tags:
                return _reply(self, 404, {"detail": "Tag not found"})
            return _reply(self, 200, {"names": [name], "category": self.tags[name], "version": 7})
        return _reply(self, 404, {"detail": "no"})

    def do_PATCH(self):
        self.calls.append(("PATCH", self.path))
        body = json.loads(self._body())
        name = urllib.parse.unquote(self.path[len("/api/tag/"):])
        if body.get("version") != 7:                  # kura's optimistic lock: the version just read
            return _reply(self, 422, {"detail": [{"type": "missing", "loc": ["body", "version"], "msg": "Field required"}]})
        self.tags[name] = body["category"]
        return _reply(self, 200, {"names": [name], "category": body["category"], "version": 8})

    put_fail_once = False               # the next pool PUT answers 503

    def do_PUT(self):
        body = json.loads(self._body())
        self.calls.append(("PUT", self.path))
        if _Kura.put_fail_once:
            _Kura.put_fail_once = False
            return _reply(self, 503, {"detail": "storage unavailable"})
        if body.get("version") not in (None, 3):            # kura's optimistic lock on pools
            return _reply(self, 409, {"detail": "version mismatch"})
        pid = int(self.path.rsplit("/", 1)[1])
        pool = next(p for p in self.pools if p["id"] == pid)
        pool["posts"] = body["posts"]
        return _reply(self, 200, pool)

    def do_POST(self):
        body = self._body()
        self.calls.append(("POST", self.path))
        if len(body) < int(self.headers.get("content-length") or 0):
            return _reply(self, 400, {"detail": "truncated body"})     # the sender stopped short, as kura drops it
        if self.path == "/api/auth/login":
            req = json.loads(body)
            if req.get("key") != "the-key":
                return _reply(self, 401, {"detail": "invalid_key"})
            return _reply(self, 200, {"username": "admin", "token": "tok-1", "email": req["email"]})
        if not self.headers.get("authorization"):
            return _reply(self, 401, {"detail": "missing token"})
        if self.path == "/api/auth/logout":
            return _reply(self, 200, {"status": "ok"})
        if self.path == "/api/pools":
            req = json.loads(body)
            if any(req["names"][0] in p["names"] for p in self.pools):
                return _reply(self, 409, {"detail": "name taken"})
            pool = {"id": 100 + len(self.pools), **req}
            self.pools.append(pool)
            return _reply(self, 200, pool)
        if self.path == "/api/upload":
            msg = BytesParser(policy=HTTP).parsebytes(
                b"content-type: " + self.headers["content-type"].encode() + b"\r\n\r\n" + body)
            fields = {p.get_param("name", header="content-disposition"): p for p in msg.iter_parts()}
            data = fields["file"].get_payload(decode=True)
            md5 = hashlib.md5(data).hexdigest()
            row = {k: v.get_payload(decode=True).decode() for k, v in fields.items() if k != "file"}
            row.update(name=fields["file"].get_filename(), md5=md5)
            for t in row.get("tags", "").split():
                self.tags.setdefault(t.lower(), "default")      # kura stores names lowercased
            earlier = next((u["id"] for u in self.uploads if u["md5"] == md5), None)
            row["id"] = earlier or 500 + len({u["id"] for u in self.uploads}) + 1
            self.uploads.append(row)
            # a duplicate answers with the first post's id and takes no new id, as kura does
            return _reply(self, 200, {"id": row["id"], "md5": md5, "duplicate": earlier is not None, "status": "active"})
        return _reply(self, 404, {"detail": "no such route"})

    def log_message(self, *a):
        pass


def _server(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def szuru():
    _Szuru.calls, _Szuru.fail_once, _Szuru.truncate = [], set(), set()
    srv = _server(_Szuru)
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
    tok = tmp_path / "szuru.token"; tok.write_text("szuru-token\n"); tok.chmod(0o600)
    return key, tok


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True,
                          env={**os.environ, "KURA_API_KEY": ""})


def _migrate(szuru, kura, creds, tmp_path, *extra):
    key, tok = creds
    return run("--from", szuru, "--from-user", "migrator", "--from-token-file", tok,
               "--url", kura, "--email", "me@example.org", "--key-file", key, "--record", tmp_path / "rec.jsonl", *extra)


def test_posts_tags_categories_and_pools_move_over(szuru, kura, creds, tmp_path):
    r = _migrate(szuru, kura, creds, tmp_path)
    assert r.returncode == 0, r.stderr
    counts = json.loads(r.stdout)
    assert counts == {"posts": 5, "added": 3, "duplicates": 1, "failed": 0, "unsupported": 1, "skipped": 0,
                      "tags_recategorized": 2, "pools_created": 1, "pools_completed": 0, "pools_existing": 0, "pools_failed": 0,
                      "seconds": pytest.approx(0, abs=60)}
    # posts in id order, with tags (first names), safety, source and the artist declaration
    ups = _Kura.uploads
    assert [u["name"] for u in ups] == ["1_a.jpg", "2_b.png", "3_c.png", "5_e.png"]
    assert ups[0]["tags"] == "studio_orbit night" and ups[0]["safety"] == "safe" and ups[0]["source"] == "https://example.org/1"
    assert ups[0]["artist_tags"] == "studio_orbit" and ups[1].get("artist_tags") is None and ups[1]["safety"] == "sketchy"
    assert ups[2]["tags"] == "studio_orbit rare_cat" and ups[2].get("source") is None and ups[2]["artist_tags"] == "studio_orbit"
    assert "post 4: passed over (application/x-shockwave-flash)" in r.stderr
    # szurubooru was asked for Token auth, in id order, page by page
    assert all(a == "Token " + base64.b64encode(b"migrator:szuru-token").decode() for _, a in _Szuru.calls)
    assert sum(1 for p, _ in _Szuru.calls if p.startswith("/api/posts/")) == 3     # 5 posts, 2 per page
    # categories: artist and character exist in kura → set; lore does not → left, and said so
    assert _Kura.tags == {"studio_orbit": "artist", "night": "default", "unit_07": "character", "rare_cat": "default"}
    assert "left at its default: lore" in r.stderr
    # the pool: szurubooru order 2,1,3,(4 unsupported → missing) mapped to kura ids
    assert _Kura.pools == [{"id": 100, "names": ["night_series", "ns"], "description": "the series", "posts": [502, 501, 503]}]
    # kura's own session: one sign-in, one sign-out
    assert [c for c in _Kura.calls if c[1].startswith("/api/auth")] == [("POST", "/api/auth/login"), ("POST", "/api/auth/logout")]
    rows = [json.loads(l) for l in (tmp_path / "rec.jsonl").read_text().splitlines()]
    assert rows[0]["migrate"] == {"source": "szurubooru", "from": szuru, "url": kura, "artist_category": "artist"}
    assert [r.get("post") for r in rows[1:6]] == [1, 2, 3, 4, 5] and rows[6]["pool"] == 10 and rows[6]["missing"] == 1


def test_a_second_run_moves_nothing_again(szuru, kura, creds, tmp_path):
    assert _migrate(szuru, kura, creds, tmp_path).returncode == 0
    n_up, n_pool = len(_Kura.uploads), len(_Kura.pools)
    r = _migrate(szuru, kura, creds, tmp_path)
    assert r.returncode == 0, r.stderr
    counts = json.loads(r.stdout)
    assert counts["skipped"] == 4 and counts["added"] == 0 and counts["unsupported"] == 1 and counts["pools_created"] == 0
    assert len(_Kura.uploads) == n_up and len(_Kura.pools) == n_pool
    assert "4 already moved" in r.stderr        # the unsupported one is tried again


def test_a_kura_tag_already_categorized_is_left_alone(szuru, kura, creds, tmp_path):
    _Kura.tags["studio_orbit"] = "character"      # the kura admin decided this before
    r = _migrate(szuru, kura, creds, tmp_path)
    assert r.returncode == 0, r.stderr
    assert _Kura.tags["studio_orbit"] == "character" and json.loads(r.stdout)["tags_recategorized"] == 1


def test_a_pool_kura_already_has_is_left_as_it_is(szuru, kura, creds, tmp_path):
    _Kura.pools.append({"id": 77, "names": ["night_series"], "description": "mine", "posts": [1, 2]})
    r = _migrate(szuru, kura, creds, tmp_path)
    assert r.returncode == 0, r.stderr
    counts = json.loads(r.stdout)
    assert counts["pools_existing"] == 1 and counts["pools_created"] == 0 and counts["pools_failed"] == 0
    assert _Kura.pools == [{"id": 77, "names": ["night_series"], "description": "mine", "posts": [1, 2]}]
    assert "kura already has it (#77), left as is" in r.stderr
    rows = [json.loads(l) for l in (tmp_path / "rec.jsonl").read_text().splitlines()]
    assert rows[-1] == {"pool": 10, "id": 77, "existing": True}


def test_a_failed_post_is_retried_and_its_pool_completed_next_run(szuru, kura, creds, tmp_path):
    """Run 1: post 3's download fails and post 2's is cut short, so the pool is created without
    them and said so; run 2 moves them, completes the pool, and the categories of tags seen
    only on those posts are copied too (category work comes from every source post)."""
    _Szuru.fail_once = {"data/posts/3_c.png"}
    _Szuru.truncate = {"data/posts/2_b.png"}
    r = _migrate(szuru, kura, creds, tmp_path)
    assert r.returncode == 1
    c = json.loads(r.stdout)
    assert c["added"] == 2 and c["failed"] == 2 and c["duplicates"] == 0 and c["pools_created"] == 1   # posts 1 and 5 in
    assert "post 2: no connection" in r.stderr and "19 of 119 bytes" in r.stderr    # short download, not sent
    assert "created without 3 posts that are not in kura yet" in r.stderr
    assert _Kura.pools[0]["posts"] == [501] and "unit_07" not in _Kura.tags
    _Szuru.truncate = set()
    # Between the runs the owner worked on that pool in kura: added a post of their own (9001),
    # put it first, and took the original first post (501) out. Completing must keep all that.
    _Kura.pools[0]["posts"] = [9001, 502]
    r = _migrate(szuru, kura, creds, tmp_path)
    assert r.returncode == 0, r.stderr
    c = json.loads(r.stdout)
    # post 3 is new; post 2 now duplicates post 5's bytes (kura 502); post 4 is passed over again
    assert c["added"] == 1 and c["duplicates"] == 1 and c["skipped"] == 2 and c["unsupported"] == 1
    assert c["pools_completed"] == 1 and c["pools_created"] == 0
    assert _Kura.pools[0]["posts"] == [9001, 502, 503]        # the owner's list, plus only the post that was missing (kura#159)
    assert _Kura.tags["unit_07"] == "character" and c["tags_recategorized"] == 1
    assert "pool 10 (night_series): 1 of its posts are not in kura yet" in r.stderr   # post 4, unsupported


def test_a_record_from_before_1_299_1_does_not_put_back_what_the_owner_took_out(szuru, kura, creds, tmp_path):
    """Pool rows written before 1.299.1 carry no "included" list. The record is in order and
    posts come before pools, so the members such a row put in are the posts recorded above
    it: completing appends only what came in later, never a post the owner has since removed."""
    _Szuru.fail_once = {"data/posts/3_c.png"}
    _Szuru.truncate = {"data/posts/2_b.png"}
    assert _migrate(szuru, kura, creds, tmp_path).returncode == 1
    assert _Kura.pools[0]["posts"] == [501]                     # post 1 only; 2 and 3 are missing
    _Szuru.truncate = set()
    rec = tmp_path / "rec.jsonl"
    rows = [json.loads(l) for l in rec.read_text().splitlines()]
    assert "included" in rows[-1] and "pool" in rows[-1]
    del rows[-1]["included"]                                     # as 1.298.0–1.299.0 wrote it
    rec.write_text("".join(json.dumps(x) + "\n" for x in rows))
    _Kura.pools[0]["posts"] = [9001]                             # the owner took post 1 (501) out and added their own
    r = _migrate(szuru, kura, creds, tmp_path)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["pools_completed"] == 1
    assert _Kura.pools[0]["posts"] == [9001, 502, 503]           # 501 stays out; only the posts moved after that row join
    assert [json.loads(l) for l in rec.read_text().splitlines()][-1]["included"] == [2, 1, 3]


def test_a_refused_pool_completion_fails_the_run_and_is_retried(szuru, kura, creds, tmp_path):
    """Run 1 leaves the pool short of post 3; run 2's completing PUT is refused, so the run
    exits 1 and records nothing for the pool; run 3 completes it."""
    _Szuru.fail_once = {"data/posts/3_c.png"}
    assert _migrate(szuru, kura, creds, tmp_path).returncode == 1
    assert _Kura.pools[0]["posts"] == [502, 501]                 # szurubooru order 2, 1 (3 failed)
    _Kura.put_fail_once = True
    r = _migrate(szuru, kura, creds, tmp_path)
    c = json.loads(r.stdout)
    assert r.returncode == 1 and c["pools_failed"] == 1 and c["pools_completed"] == 0 and c["failed"] == 0
    assert "pool 10 (night_series): not completed (503" in r.stderr
    assert _Kura.pools[0]["posts"] == [502, 501]                 # untouched
    rows = [json.loads(l) for l in (tmp_path / "rec.jsonl").read_text().splitlines()]
    assert [x for x in rows if "pool" in x][-1]["missing"] == 2    # the pending state from run 1 stands
    r = _migrate(szuru, kura, creds, tmp_path)
    c = json.loads(r.stdout)
    assert r.returncode == 0, r.stderr
    assert c["pools_completed"] == 1 and _Kura.pools[0]["posts"] == [502, 501, 503]
    assert [x for x in rows if "pool" in x][-1]["missing"] == 2 and \
        [json.loads(l) for l in (tmp_path / "rec.jsonl").read_text().splitlines()][-1]["missing"] == 1


def test_the_credential_never_goes_to_another_origin(szuru, kura, creds, tmp_path):
    other = _server(_Szuru)
    try:
        _Szuru.posts[0] = {**_Szuru.posts[0], "contentUrl": f"http://127.0.0.1:{other.server_port}/data/posts/1_a.jpg"}
        r = _migrate(szuru, kura, creds, tmp_path)
        assert r.returncode == 0, r.stderr
        fetched = [(p, a) for p, a in _Szuru.calls if p == "/data/posts/1_a.jpg"]
        assert fetched and all(a is None for _, a in fetched)      # the other origin saw no Authorization
        assert all(a for p, a in _Szuru.calls if p.startswith("/api/"))
    finally:
        _Szuru.posts[0] = {**_Szuru.posts[0], "contentUrl": "data/posts/1_a.jpg"}
        other.shutdown()


def test_dry_run_reads_both_sides_and_writes_nothing(szuru, kura, creds, tmp_path):
    key, tok = creds
    r = run("--from", szuru, "--from-user", "migrator", "--from-token-file", tok, "--url", kura,
            "--dry-run", "--record", tmp_path / "rec.jsonl")
    assert r.returncode == 0, r.stderr
    assert "5 posts (0 already moved), 1 pools" in r.stderr
    assert r.stdout.splitlines()[0].startswith("post 1  image/jpeg  safe  studio_orbit night")
    assert "pool 10  night_series  4 posts" in r.stdout
    assert not _Kura.calls and not (tmp_path / "rec.jsonl").exists()


def test_secrets_come_from_private_files_only(szuru, kura, creds, tmp_path):
    key, tok = creds
    tok.chmod(0o644)
    r = _migrate(szuru, kura, creds, tmp_path)
    assert r.returncode == 1 and "mode 0600" in r.stderr and not _Szuru.calls and not _Kura.calls
    r = run("--help")
    assert r.returncode == 0 and "--from-password " not in r.stdout and "--from-token " not in r.stdout
