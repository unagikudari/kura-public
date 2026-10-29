"""One image is one post; the contexts it appears in are pools (owner, 2026-10-25)."""
import contextlib
import hashlib

import pytest

from scripts import merge_duplicate_posts as M

DATA = b"same bytes"


class _Cur:
    def __init__(self, pg): self.pg = pg
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def execute(self, sql, params=None):
        self.pg.sql.append((" ".join(sql.split()), params)); self.last = sql
    def fetchone(self):
        if "duplicate_of FROM post WHERE id" in self.last: return None   # ok_root: not merged away
        if "RETURNING id" in self.last: return {"id": 99}                # the audit row
        return {"n": 1}                                       # the cover ended on the representative
    def fetchall(self):
        s = self.last
        if "RETURNING tag_id" in s: return [{"tag_id": 7}]
        if "post_tag_pin" in s: return list(self.pg.pins)
        if "tagger_gt_label" in s: return [{"tag_id": t} for t in self.pg.gt]
        if "FROM post" in s and "ORDER BY id" in s: return list(self.pg.posts.values())
        if "SELECT pool_id, ord FROM pool_post" in s: return [{"pool_id": 913, "ord": 0}, {"pool_id": 7756, "ord": 25}]
        if "SELECT pool_id FROM pool_post" in s: return [{"pool_id": 912}, {"pool_id": 7756}]
        if "cover_post_id" in s: return [{"id": 913}]
        return []


class _PG:
    def __init__(self, posts, pins=(), gt=()): self.posts, self.pins, self.gt, self.sql, self.outcome = posts, pins, gt, [], None
    def __enter__(self): return self
    def __exit__(self, exc_type, *a): self.outcome = "rollback" if exc_type else "commit"
    def cursor(self, **kw): return _Cur(self)
    def rollback(self): self.outcome = "rollback"


def _posts(rep_status="ok", rep_sha=hashlib.sha256(DATA).digest(), rep_checksum="x"):
    return {1: {"id": 1, "status": "ok", "sha256": None, "checksum": hashlib.sha1(DATA).hexdigest(), "mime": "image/png", "version": 2, "duplicate_of": None},
            2: {"id": 2, "status": rep_status, "sha256": rep_sha, "checksum": rep_checksum, "mime": "image/png", "version": 5, "duplicate_of": None}}


@pytest.fixture(autouse=True)
def _files(monkeypatch):
    monkeypatch.setattr(M, "original_bytes", lambda pid, mime, checksum: (b"other" if checksum == "differs" else DATA, []))
    monkeypatch.setattr(M.files, "content_lock", lambda pid: contextlib.nullcontext())
    monkeypatch.setattr(M, "contents", lambda pid, mime: DUP_FILES.get(pid, ({hashlib.sha1(DATA).hexdigest(): DATA}, {hashlib.sha1(DATA).hexdigest(): ["1_a.png"]})))


DUP_FILES = {}


def test_the_representative_takes_the_duplicates_place_and_the_duplicate_leaves_browse(tmp_path):
    pg = _PG(_posts()); journal = M.Journal(tmp_path / "j.jsonl")
    r = M.merge(pg, 1, 2, apply=True, journal=journal)
    sql = [s for s, _ in pg.sql]
    assert pg.outcome == "commit"
    assert any(s.startswith("UPDATE pool_post SET post_id") for s in sql)                 # 913: moved
    assert any(s.startswith("DELETE FROM pool_post") for s in sql)                        # 7756: rep already there
    assert any(s.startswith("UPDATE pool SET cover_post_id") and "id = ANY" in s for s in sql)
    assert any(s.startswith("INSERT INTO post_tag_audit") for s in sql) and r["applied"]["tags_added"] == [7]
    assert any("status = 'exact_dup', duplicate_of" in s for s in sql)
    assert [p["rep_already_member"] for p in r["pools"]] == [False, True]
    lines = journal.path.read_text().splitlines()
    assert '"stage": "planned"' in lines[0] and lines[-1] == '{"dup": 1, "rep": 2, "requested_rep": 2, "stage": "committed"}'


@pytest.mark.parametrize("posts,match", [
    (_posts(rep_status="exact_dup"), "representative is exact_dup"),
    (_posts(rep_checksum="differs"), "originals differ"),
])
def test_a_pair_that_is_not_an_exact_duplicate_of_an_ok_post_is_refused(posts, match):
    pg = _PG(posts)
    with pytest.raises(M.Refused, match=match):
        M.merge(pg, 1, 2, apply=True)
    assert pg.outcome == "rollback" and not any(s.startswith(("UPDATE", "DELETE", "INSERT")) for s, _ in pg.sql)


def test_a_dry_run_locks_and_writes_nothing():
    pg = _PG(_posts())
    M.merge(pg, 1, 2, apply=False)
    assert not any("FOR UPDATE" in s or s.startswith(("UPDATE", "DELETE", "INSERT")) for s, _ in pg.sql)


def test_a_pair_needing_human_pins_or_gt_moved_is_refused():
    pg = _PG(_posts(), pins=[{"tag_id": 3, "state": "absent", "pinned_by": "owner"}])
    with pytest.raises(M.Refused, match="governed path"):
        M.merge(pg, 1, 2, apply=True)
    assert pg.outcome == "rollback"


def test_a_post_is_not_merged_into_itself():
    with pytest.raises(M.Refused, match="itself"):
        M.merge(_PG(_posts()), 2, 2, apply=True)


def test_a_representative_merged_away_is_followed_to_its_ok_root():
    class Cur:
        def __init__(self): self.rows = {7: ("exact_dup", 2), 2: ("ok", None)}
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, sql, params): self.pid = params[0]
        def fetchone(self): return self.rows.get(self.pid)
    class PG:
        def cursor(self, **kw): return Cur()
    assert M.ok_root(PG(), 7) == 2 and M.ok_root(PG(), 2) == 2


def test_a_followed_root_is_what_planned_and_committed_both_record(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "ok_root", lambda pg, rep: 2)        # asked for 7, which was merged into 2
    pg = _PG(_posts()); journal = M.Journal(tmp_path / "j.jsonl")
    M.merge(pg, 1, 7, apply=True, journal=journal)
    planned, committed = [__import__("json").loads(l) for l in journal.path.read_text().splitlines()]
    assert planned["rep"] == committed["rep"] == 2 and planned["requested_rep"] == committed["requested_rep"] == 7


def test_a_lost_original_merges_only_for_a_checked_pair_still_as_checked(tmp_path, monkeypatch):
    """Codex 1.280.1: one remaining content does not prove it is the lost original."""
    sha1 = hashlib.sha1(DATA).hexdigest()
    monkeypatch.setattr(M, "served_view_sha256", lambda db, pid: "v1")
    posts = _posts(); posts[1]["checksum"] = "gone"
    with pytest.raises(M.Refused, match="no file under the post's names"):
        M.merge(_PG(dict(posts)), 1, 2, apply=False)                         # not checked
    checked = {(1, 2): {"files_sha1": sha1, "view_sha256": "v1", "checked": "by hand"}}
    r = M.merge(_PG(dict(posts)), 1, 2, apply=True, journal=M.Journal(tmp_path / "j.jsonl"), lost_ok=checked)
    assert r["dup_original_lost"]["files"] == ["1_a.png"] and r["dup_original_lost"]["view_sha256"] == "v1"
    monkeypatch.setattr(M, "served_view_sha256", lambda db, pid: "v2")
    with pytest.raises(M.Refused, match="changed since it was checked"):
        M.merge(_PG(_posts() | {1: posts[1]}), 1, 2, apply=False, lost_ok=checked)


def test_a_duplicate_with_two_contents_and_no_recorded_original_is_refused(monkeypatch):
    posts = _posts(); posts[1]["checksum"] = "gone"
    monkeypatch.setitem(DUP_FILES, 1, ({"a": DATA, "b": b"other"}, {"a": ["1_a.png"], "b": ["1_b.jpg"]}))
    with pytest.raises(M.Refused, match="no file under the post's names"):
        M.merge(_PG(posts), 1, 2, apply=False, lost_ok={(1, 2): {"files_sha1": "a", "view_sha256": "v", "checked": ""}})


def test_the_view_digest_is_of_the_bytes_read_not_the_recorded_address(tmp_path, monkeypatch):
    """Codex 1.280.1: a rendition file changed under its recorded address must not pass."""
    from scripts import repair_posts as R
    f = tmp_path / "view.webp"; f.write_bytes(b"changed bytes")
    monkeypatch.setattr(R.renditions, "path_for", lambda sha, mime: f)
    class Db:
        def execute(self, sql, params): pass
        def fetchone(self): return {"sha256": b"\x00" * 32, "mime": "image/webp"}
    assert M.served_view_sha256(Db(), 1) == hashlib.sha256(b"changed bytes").hexdigest()


def test_a_view_in_the_legacy_column_is_digested_from_the_column(monkeypatch):
    monkeypatch.setattr(M, "current_rendition", lambda db, pid, kind: (None, None))
    class Db:
        def execute(self, sql, params): pass
        def fetchone(self): return {"data": memoryview(b"legacy")}
    assert M.served_view_sha256(Db(), 1) == hashlib.sha256(b"legacy").hexdigest()


def test_carried_pins_go_to_the_representative_as_the_persons_decisions(tmp_path):
    """Owner 2026-10-25: 「手順で移していい」 — the pin moves; gt_sync derives its GT."""
    pg = _PG(_posts(), pins=[{"tag_id": 3, "state": "absent", "pinned_by": "owner"}], gt=[3])
    r = M.merge(pg, 1, 2, apply=True, journal=M.Journal(tmp_path / "j.jsonl"), carry_pins=True)
    sql = [q for q, _ in pg.sql]
    assert any(q.startswith("DELETE FROM post_tag WHERE post_id") for q in sql)        # the machine tag goes
    audit = next(p for q, p in pg.sql if "'manual'" in q)
    assert audit[1] == "merge_duplicate_posts:dup=1 carries owner's absent pin"
    pin = next(p for q, p in pg.sql if q.startswith("INSERT INTO post_tag_pin"))
    assert pin == (2, 3, "absent", "owner", 99)
    assert r["applied"]["pins_carried"] == [{"tag": 3, "state": "absent", "pinned_by": "owner",
                                             "audit_id": 99, "rep_tag_removed": True}]
    assert pg.outcome == "commit"


def test_gt_no_carried_pin_explains_is_still_refused():
    pg = _PG(_posts(), pins=[{"tag_id": 3, "state": "absent", "pinned_by": "owner"}], gt=[3, 4])
    with pytest.raises(M.Refused, match=r"GT labels \[4\]"):
        M.merge(pg, 1, 2, apply=True, carry_pins=True)
    assert pg.outcome == "rollback"
