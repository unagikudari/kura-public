"""repair_posts: which original is the post's, and what a post needs (kura-on-nodeb step 4)."""
import hashlib
import io

import pytest
from PIL import Image

from scripts import repair_posts as R


def _png(color):
    b = io.BytesIO(); Image.new("RGB", (8, 8), color).save(b, "PNG"); return b.getvalue()


def _gif(durations):
    frames = [Image.new("RGB", (8, 8), c) for c in ("red", "blue", "green")][: len(durations)]
    b = io.BytesIO(); frames[0].save(b, "GIF", save_all=True, append_images=frames[1:], duration=durations, loop=0)
    return b.getvalue()


def test_the_original_is_the_content_whose_sha1_is_the_posts_checksum(tmp_path, monkeypatch):
    png, jpg = _png("red"), _png("blue")
    (tmp_path / "1_a.png").write_bytes(png); (tmp_path / "1_a.jpg").write_bytes(jpg)
    monkeypatch.setattr(R.files, "names", lambda kind, pid, mime: [tmp_path / f"1_a.{'jpg' if mime == 'image/jpeg' else 'png'}"])
    data, others = R.original_bytes(1, "image/png", hashlib.sha1(png).hexdigest())
    assert data == png and others == ["1_a.jpg"]
    with pytest.raises(R.Refused, match="SHA1"):
        R.original_bytes(1, "image/png", "0" * 40)


def _post(**kw):
    return {"type": "image", "checksum": "", "checksum_md5": None, "sha256": None, "media_location": "pg", **kw}


def test_hashes_come_from_the_file():
    data = _png("red")
    changes, need, animated = R.decide(_post(), data, view=_png("red"))
    assert changes == {"sha256": hashlib.sha256(data).digest(), "checksum_md5": hashlib.md5(data).hexdigest()}
    assert not need and not animated


def test_a_post_without_a_view_or_location_gets_one():
    assert R.decide(_post(media_location=None), _png("red"), view=_png("red"))[1]
    assert R.decide(_post(), _png("red"), view=None)[1]


def test_animation_needs_a_moving_view_and_the_animation_type():
    from api.services.media_materialize import animated_webp_bytes
    gif = _gif([40, 80, 120])
    moving = animated_webp_bytes(Image.open(io.BytesIO(gif)))
    assert R.decide(_post(type="image"), gif, view=moving)[1:] == (True, True)        # frozen type
    assert R.decide(_post(type="animation"), gif, view=moving)[1:] == (False, True)   # keeps motion
    assert R.decide(_post(type="animation"), gif, view=_png("red"))[1:] == (True, True)  # still view
    assert R.decide(_post(type="animation"), _png("red"), view=_png("red"))[1:] == (True, False)  # still original


class _Cursor:
    def __init__(self, pg): self.pg, self.last = pg, None
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def execute(self, sql, params=None):
        self.pg.sql.append(sql); self.last = sql
    def fetchone(self):
        if "FOR UPDATE" in self.last or "FROM post WHERE id" in self.last: return dict(self.pg.post)
        return None                                         # no rendition, no sha256 collision


class _PG:
    """Records statements and whether the transaction committed or rolled back."""
    def __init__(self, post, journal_path=None):
        self.post, self.sql, self.outcome, self.journal_path, self.planned_before_commit = post, [], None, journal_path, None
    def __enter__(self): return self
    def __exit__(self, exc_type, *a):
        self.outcome = "rollback" if exc_type else "commit"
        if self.journal_path and not exc_type:
            self.planned_before_commit = '"stage": "planned"' in self.journal_path.read_text()
    def cursor(self, **kw): return _Cursor(self)
    def rollback(self): self.outcome = "rollback"


def _setup(tmp_path, monkeypatch, original):
    path = tmp_path / "1_a.gif"; path.write_bytes(original)
    monkeypatch.setattr(R.files, "names", lambda kind, pid, mime: [path])
    import contextlib
    monkeypatch.setattr(R.files, "content_lock", lambda pid: contextlib.nullcontext())
    stored = []
    def store(blob):
        stored.append(blob)
        return R.renditions.Stored(sha256=hashlib.sha256(blob).digest(), mime="image/webp", byte_size=len(blob),
                                   width=8, height=8, path=tmp_path / "x")
    monkeypatch.setattr(R.renditions, "store", store)
    return {"id": 1, "type": "image", "status": "ok", "checksum": hashlib.sha1(original).hexdigest(),
            "checksum_md5": None, "sha256": None, "media_location": None, "mime": "image/gif", "version": 3}, stored


def test_an_applied_repair_journals_its_plan_before_the_commit(tmp_path, monkeypatch):
    post, stored = _setup(tmp_path, monkeypatch, _gif([40, 80, 120]))
    journal = R.Journal(tmp_path / "j.jsonl")
    pg = _PG(post, journal.path)
    r = R.repair(pg, 1, apply=True, journal=journal)
    assert pg.outcome == "commit" and pg.planned_before_commit
    assert r["new"]["type"] == "animation" and r["new"]["version"] == 4 and r["new"]["view"]
    assert any(s.startswith("UPDATE post SET") for s in pg.sql)
    assert journal.path.read_text().splitlines()[-1] == '{"post": 1, "stage": "committed"}'
    with pytest.raises(FileExistsError):
        R.Journal(journal.path)                              # never reopened over another run


def test_a_generated_view_that_loses_the_motion_rolls_everything_back(tmp_path, monkeypatch):
    post, stored = _setup(tmp_path, monkeypatch, _gif([40, 80, 120]))
    still = io.BytesIO(); Image.new("RGB", (8, 8)).save(still, "WEBP")
    monkeypatch.setattr(R, "materialize_animation_bytes",
                        lambda data: {"data": still.getvalue(), "thumbnail": still.getvalue(), "mime": "image/webp"})
    pg = _PG(post)
    with pytest.raises(R.Refused, match="does not keep the motion"):
        R.repair(pg, 1, apply=True)
    assert pg.outcome == "rollback" and not stored and not any(s.startswith("UPDATE") for s in pg.sql)


def test_a_dry_run_writes_nothing_and_takes_no_row_lock(tmp_path, monkeypatch):
    post, stored = _setup(tmp_path, monkeypatch, _gif([40, 80, 120]))
    pg = _PG(post)
    r = R.repair(pg, 1, apply=False)
    assert r["view"] and not stored
    assert not any("FOR UPDATE" in s or s.startswith(("UPDATE", "INSERT")) for s in pg.sql)


def test_a_failure_after_the_plan_is_recorded_stops_the_run_instead_of_counting_a_refusal(tmp_path, monkeypatch):
    post, stored = _setup(tmp_path, monkeypatch, _gif([40, 80, 120]))
    journal = R.Journal(tmp_path / "j.jsonl")
    class LostCommit(_PG):
        def __exit__(self, exc_type, *a):
            super().__exit__(exc_type, *a)
            if not exc_type: raise ConnectionError("connection lost during commit")
    with pytest.raises(R.Stop, match="outcome unknown"):
        R.repair(LostCommit(post, journal.path), 1, apply=True, journal=journal)
    real_write = journal.write
    def fail_committed(value):
        if value.get("stage") == "committed": raise OSError("disk full")
        real_write(value)
    journal.write = fail_committed
    with pytest.raises(R.Stop, match="committed, but"):
        R.repair(_PG(post, journal.path), 1, apply=True, journal=journal)


def test_pids_replace_the_candidate_query(tmp_path, monkeypatch):
    ids = tmp_path / "ids"; ids.write_text("5\n3\n")
    seen = []
    class PG:
        def cursor(self, *a, **k): raise AssertionError("the candidate query must not run")
        def rollback(self): pass
    monkeypatch.setattr(R.psycopg2, "connect", lambda *a, **k: PG())
    monkeypatch.setattr(R, "repair", lambda pg, pid, apply, journal=None: seen.append(pid) or
                        {"post": pid, "hashes": {}, "view": False})
    monkeypatch.setattr(R, "LOG", tmp_path / "log")
    assert R.main(["--pids", str(ids)]) == 0
    assert seen == [3, 5]
