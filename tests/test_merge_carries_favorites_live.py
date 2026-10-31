"""A duplicate merge carries each person's favorite to the representative (#157), and a favorite
toggled while a merge holds the pair goes to the representative, not to the post the merge hid.

Runs on a private, throwaway PostgreSQL cluster (or KURA_MERGE_TEST_DSN, a psycopg2 DSN to a
database named kura_merge_test); never the application database.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

sa = pytest.importorskip("sqlalchemy")
psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("asyncpg")
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from api.db.models import Base  # noqa: E402
from api.routers import posts as P  # noqa: E402
from scripts import merge_duplicate_posts as M  # noqa: E402

DATA = b"one image, two posts"
SHA1 = hashlib.sha1(DATA).hexdigest()
T_DUP, T_REP = datetime(2026, 3, 1, 9, 0), datetime(2026, 5, 1, 9, 0)


@pytest.fixture(scope="module")
def cluster():
    configured = os.environ.get("KURA_MERGE_TEST_DSN")
    if configured:
        assert "dbname=kura_merge_test" in configured, "test DB must be named kura_merge_test"
        yield configured
        return
    bindir = Path(subprocess.check_output(["pg_config", "--bindir"], text=True).strip())
    if not (bindir / "initdb").exists():
        pytest.skip("needs PostgreSQL's initdb, or KURA_MERGE_TEST_DSN")
    with tempfile.TemporaryDirectory(prefix="kura157-") as root:
        subprocess.run([str(bindir / "initdb"), "-D", f"{root}/db", "-A", "trust", "-U", "tester"],
                       check=True, capture_output=True)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        ctl = [str(bindir / "pg_ctl"), "-D", f"{root}/db"]
        subprocess.run([*ctl, "-l", f"{root}/log", "-o", f"-h 127.0.0.1 -p {port} -k {root} -F", "-w", "start"],
                       check=True, capture_output=True)
        try:
            yield f"host=127.0.0.1 port={port} user=tester dbname=postgres"
        finally:
            subprocess.run([*ctl, "-m", "immediate", "-w", "stop"], check=True, capture_output=True)


def _async_url(dsn: str) -> str:
    kv = dict(x.split("=", 1) for x in dsn.split())
    return f"postgresql+asyncpg://{kv['user']}@{kv['host']}:{kv['port']}/{kv['dbname']}"


@pytest.fixture
def db(cluster, monkeypatch):
    """A fresh schema (every model table without foreign keys, plus the raw tables the merge
    writes), one user, and posts 1 (the duplicate) and 2 (the representative) of one image."""
    md = sa.MetaData()
    for t in Base.metadata.sorted_tables:
        c = t.to_metadata(md)
        c.constraints = {x for x in c.constraints if not isinstance(x, sa.ForeignKeyConstraint)}
        c.foreign_keys.clear()
        for col in c.columns:
            col.foreign_keys.clear()
    kv = dict(x.split("=", 1) for x in cluster.split())
    eng = sa.create_engine(f"postgresql+psycopg2://{kv['user']}@{kv['host']}:{kv['port']}/{kv['dbname']}",
                           poolclass=sa.pool.NullPool)
    with eng.begin() as conn:
        conn.execute(sa.text("DROP TABLE IF EXISTS post_tag_pin, tagger_gt_label, post_tag_audit"))
        md.drop_all(conn)
        md.create_all(conn)
        conn.execute(sa.text("CREATE TABLE post_tag_pin (post_id int, tag_id int, state text, pinned_by text, audit_id bigint)"))
        conn.execute(sa.text("DROP FUNCTION IF EXISTS tagger_governed_retract_pin_gt(text, integer)"))
        # with its real foreign key to post: the race of #158 goes through the FK's row lock
        conn.execute(sa.text("CREATE TABLE tagger_gt_label (tag text, tag_id int, post_id int REFERENCES post(id), "
                             "verdict text, source_pool_id int, derived_from text, verified_at timestamptz)"))
        # the production body (0122), less its executor check: the test session is the owner
        conn.execute(sa.text("""
            CREATE FUNCTION tagger_governed_retract_pin_gt(p_tag text, p_post_id integer) RETURNS integer
            LANGUAGE plpgsql AS $$ DECLARE v_count integer; BEGIN
                DELETE FROM tagger_gt_label WHERE tag = p_tag AND post_id = p_post_id
                   AND source_pool_id IS NULL AND derived_from = 'human_correction';
                GET DIAGNOSTICS v_count = ROW_COUNT; RETURN v_count; END $$"""))
        conn.execute(sa.text("CREATE TABLE post_tag_audit (id bigserial PRIMARY KEY, post_id int, actor_type text, "
                             "actor_id text, source text, added_tags text[], removed_tags text[], "
                             "created_at timestamptz, host text)"))

        def fill(table, row):
            for col in md.tables[table].columns:
                if col.name in row or col.nullable or col.server_default is not None or col.primary_key:
                    continue
                t = col.type
                row[col.name] = (0 if isinstance(t, (sa.Integer, sa.BigInteger)) else False if isinstance(t, sa.Boolean)
                                 else datetime(2026, 1, 1) if isinstance(t, sa.DateTime) else "x")
            conn.execute(md.tables[table].insert(), row)
        for uid in (1, 2):
            fill("user", {"id": uid, "name": f"u{uid}"})
        for pid in (1, 2):
            fill("post", {"id": pid, "status": "ok", "safety": "safe", "type": "image", "checksum": SHA1,
                          "mime-type": "image/png", "version": 1, "creation_time": datetime(2026, 1, 1)})
    eng.dispose()
    monkeypatch.setattr(M, "original_bytes", lambda pid, mime, checksum: (DATA, []))
    monkeypatch.setattr(M, "contents", lambda pid, mime: ({SHA1: DATA}, {SHA1: [f"{pid}.png"]}))
    monkeypatch.setattr(M.files, "content_lock", lambda pid: contextlib.nullcontext())
    yield cluster


def q(dsn, sql, params=()):
    with contextlib.closing(psycopg2.connect(dsn)) as c, c.cursor() as cur:
        cur.execute(sql, params)
        c.commit()
        return cur.fetchall() if cur.description else None


def favs(dsn):
    return sorted(q(dsn, "SELECT post_id, user_id, time FROM post_favorite"))


def merge(dsn, **kw):
    with contextlib.closing(psycopg2.connect(dsn)) as pg:
        return M.merge(pg, 1, 2, True, None, None, kw.get("carry_pins", False))


def test_a_favorite_of_the_duplicate_goes_to_the_representative(db):
    q(db, "INSERT INTO post_favorite VALUES (1, 1, %s)", (T_DUP,))
    rec = merge(db)
    assert favs(db) == [(2, 1, T_DUP)]                          # the person's choice, with its time
    assert rec["applied"]["favorites_added"] == [1]
    assert rec["favorites"] == [{"user": 1, "time": T_DUP.isoformat(), "rep_already": False}]


def test_a_person_who_chose_both_keeps_one_favorite_with_the_representative_s_time(db):
    q(db, "INSERT INTO post_favorite VALUES (1, 1, %s), (2, 1, %s)", (T_DUP, T_REP))
    rec = merge(db)
    assert favs(db) == [(2, 1, T_REP)]
    assert rec["applied"]["favorites_added"] == [] and rec["favorites"][0]["rep_already"] is True


def test_two_people_on_two_sides_both_keep_their_choice(db):
    q(db, "INSERT INTO post_favorite VALUES (1, 1, %s), (2, 2, %s)", (T_DUP, T_REP))
    merge(db)
    assert favs(db) == [(2, 1, T_DUP), (2, 2, T_REP)]


def test_no_favorite_makes_none(db):
    merge(db)
    assert favs(db) == []


def test_a_merge_that_fails_later_leaves_the_favorites_as_they_were(db):
    q(db, "INSERT INTO post_favorite VALUES (1, 1, %s)", (T_DUP,))
    # a present pin for a tag on neither post: refused after the favorites moved, so all rolls back
    q(db, "INSERT INTO post_tag_pin VALUES (1, 77, 'present', 'owner', NULL)")
    with pytest.raises(M.Refused):
        merge(db, carry_pins=True)
    assert favs(db) == [(1, 1, T_DUP)]
    assert q(db, "SELECT status FROM post WHERE id = 1") == [("ok",)]


def test_a_favorite_toggled_while_the_merge_holds_the_pair_goes_to_the_representative(db):
    engine = create_async_engine(_async_url(db), poolclass=sa.pool.NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    holder = psycopg2.connect(db)
    cur = holder.cursor()
    cur.execute(M.LOCK, ([1, 2],))                              # the merge's own lock, held
    out: list = []

    def toggle():
        async def go():
            async with sessions() as s:
                return await P.toggle_favorite(1, db=s, auth_user=None)
        out.append(asyncio.run(go()))
    t = threading.Thread(target=toggle)
    t.start()
    time.sleep(1.0)
    assert t.is_alive(), "the toggle must wait for the merge"
    cur.execute("UPDATE post SET status = 'exact_dup', duplicate_of = 2 WHERE id = 1")
    holder.commit()                                             # the merge commits
    t.join(20)
    holder.close()
    asyncio.run(engine.dispose())
    assert not t.is_alive() and out
    assert [(p, u) for p, u, _ in favs(db)] == [(2, 1)]         # on the representative, not the hidden post
    assert out[0].id == 2


def _toggle(dsn, post_id):
    async def go():
        engine = create_async_engine(_async_url(dsn), poolclass=sa.pool.NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await P.toggle_favorite(post_id, db=s, auth_user=None)
        finally:
            await engine.dispose()
    return asyncio.run(go())


def _add_posts(dsn, ids):
    for pid in ids:
        q(dsn, """INSERT INTO post (id, status, safety, type, checksum, "mime-type", version, creation_time,
                  file_size, image_width, image_height, flags)
                  SELECT %s, status, safety, type, checksum, "mime-type", version, creation_time,
                         file_size, image_width, image_height, flags FROM post WHERE id = 2""", (pid,))


def test_a_favorite_follows_a_merged_chain_to_its_end(db):
    _add_posts(db, [3])
    q(db, "UPDATE post SET status = 'exact_dup', duplicate_of = 2 WHERE id = 1")
    q(db, "UPDATE post SET status = 'exact_dup', duplicate_of = 3 WHERE id = 2")
    assert _toggle(db, 1).id == 3
    assert [(p, u) for p, u, _ in favs(db)] == [(3, 1)]


def test_a_chain_that_loops_or_runs_on_is_refused_and_nothing_is_written(db):
    q(db, "UPDATE post SET status = 'exact_dup', duplicate_of = 2 WHERE id = 1")
    q(db, "UPDATE post SET status = 'exact_dup', duplicate_of = 1 WHERE id = 2")
    with pytest.raises(P.KuraError) as e:
        _toggle(db, 1)
    assert e.value.status == 409 and e.value.error == "duplicate_chain_unresolved" and favs(db) == []
    ids = list(range(3, 3 + P.MAX_DUPLICATE_HOPS + 1))
    _add_posts(db, ids)
    chain = [1] + ids
    for a, b in zip(chain, chain[1:]):
        q(db, "UPDATE post SET status = 'exact_dup', duplicate_of = %s WHERE id = %s", (b, a))
    q(db, "UPDATE post SET status = 'ok', duplicate_of = NULL WHERE id = %s", (chain[-1],))
    with pytest.raises(P.KuraError):
        _toggle(db, 1)                                          # the end is past the limit
    assert favs(db) == []



def gt(dsn):
    return sorted(q(dsn, "SELECT post_id, tag, verdict, derived_from FROM tagger_gt_label"))


def test_the_duplicate_s_pin_label_is_retracted_so_the_image_counts_once(db):
    """#158: the duplicate's absent pin and the negative it gave go; the representative gets
    the pin (and from it, on the next gt_sync, the one negative)."""
    q(db, "INSERT INTO tag_name (tag_name_id, tag_id, name, ord) VALUES (7, 7, 'phimosis', 0)")
    q(db, "INSERT INTO post_tag_pin VALUES (1, 7, 'absent', 'owner', NULL)")
    q(db, "INSERT INTO tagger_gt_label VALUES ('phimosis', 7, 1, 'negative', NULL, 'human_correction', now())")
    rec = merge(db, carry_pins=True)
    assert gt(db) == []
    # the pin is carried; the duplicate keeps its own as history (the merge's rule)
    assert q(db, "SELECT post_id, tag_id, state FROM post_tag_pin ORDER BY post_id") == [(1, 7, "absent"), (2, 7, "absent")]
    assert rec["gt_retracted"] == [{"tag": "phimosis", "verdict": "negative"}]


def test_a_label_both_posts_hold_is_counted_once_after_the_merge(db):
    q(db, "INSERT INTO tag_name (tag_name_id, tag_id, name, ord) VALUES (7, 7, 'phimosis', 0)")
    for pid in (1, 2):
        q(db, "INSERT INTO post_tag_pin VALUES (%s, 7, 'absent', 'owner', NULL)", (pid,))
        q(db, "INSERT INTO tagger_gt_label VALUES ('phimosis', 7, %s, 'negative', NULL, 'human_correction', now())", (pid,))
    merge(db)
    assert gt(db) == [(2, "phimosis", "negative", "human_correction")]


def test_a_label_without_a_governed_per_post_retraction_refuses_the_pair(db):
    q(db, "INSERT INTO tagger_gt_label VALUES ('night', 9, 1, 'positive', 44, 'pool_seed', now())")
    q(db, "INSERT INTO tagger_gt_label VALUES ('night', 9, 2, 'positive', 44, 'pool_seed', now())")
    q(db, "INSERT INTO post_favorite VALUES (1, 1, %s)", (T_DUP,))
    before = (gt(db), favs(db))
    with pytest.raises(M.Refused, match="count twice"):
        merge(db)
    assert (gt(db), favs(db)) == before
    assert q(db, "SELECT status FROM post WHERE id = 1") == [("ok",)]



def test_a_gt_sync_running_during_a_merge_does_not_land_a_label_on_the_hidden_post(db, monkeypatch):
    """The reviewer's race (#158): a sync reads the duplicate as ok, waits on the merge's row
    lock through the label's foreign key, and inserts after the merge hid the post. Both take
    the GT write lock first, so the sync runs after the merge and labels the representative."""
    from api.tagger_core import gt_lock
    q(db, "INSERT INTO tag_name (tag_name_id, tag_id, name, ord) VALUES (7, 7, 'phimosis', 0)")
    q(db, "INSERT INTO post_tag_pin VALUES (1, 7, 'absent', 'owner', NULL)")   # no label yet
    paused, go_on = threading.Event(), threading.Event()
    real = M.original_bytes

    def slow(pid, mime, checksum):                 # called inside the merge's transaction
        paused.set()
        go_on.wait(10)
        return real(pid, mime, checksum)
    monkeypatch.setattr(M, "original_bytes", slow)
    merged = threading.Thread(target=lambda: merge(db, carry_pins=True))
    merged.start()
    assert paused.wait(10)

    def sync():                                    # the shape of tagger_governed_sync_neg_gt_from_pins
        with contextlib.closing(psycopg2.connect(db)) as c, c.cursor() as cur:
            cur.execute(gt_lock.SQL)
            cur.execute("""INSERT INTO tagger_gt_label (tag, tag_id, post_id, verdict, source_pool_id, derived_from)
                           SELECT 'phimosis', p.tag_id, p.post_id, 'negative', NULL, 'human_correction'
                             FROM post_tag_pin p JOIN post ON post.id = p.post_id
                            WHERE p.state = 'absent' AND post.status = 'ok'""")
            c.commit()
    writer = threading.Thread(target=sync)
    writer.start()
    time.sleep(1.0)
    go_on.set()
    merged.join(20)
    writer.join(20)
    assert not merged.is_alive() and not writer.is_alive()
    assert [(p, t) for p, t, *_ in gt(db)] == [(2, "phimosis")]     # one label, on the representative
