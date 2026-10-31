"""A tag merge never leaves the merged tag on a post whose surviving human pin says absent,
and never silently picks one of two opposite pins (#147): such a merge is refused with 409
naming the posts, and nothing changes. Merges without a contradiction still go through.

Runs on a private, throwaway PostgreSQL cluster; never the application database.
"""
from __future__ import annotations

import asyncio
import socket
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sa = pytest.importorskip("sqlalchemy")
pytest.importorskip("asyncpg")
from fastapi import HTTPException  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from api.db.models import Base  # noqa: E402
from api.routers import tags as TR  # noqa: E402

S, T, OTHER = 1, 2, 3            # tag ids: source "s_tag", target "t_tag"


@pytest.fixture(scope="module")
def url():
    bindir = Path(subprocess.check_output(["pg_config", "--bindir"], text=True).strip())
    if not (bindir / "initdb").exists():
        pytest.skip("needs PostgreSQL's initdb")
    with tempfile.TemporaryDirectory(prefix="kura147-") as root:
        subprocess.run([str(bindir / "initdb"), "-D", f"{root}/db", "-A", "trust", "-U", "tester"],
                       check=True, capture_output=True)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        ctl = [str(bindir / "pg_ctl"), "-D", f"{root}/db"]
        subprocess.run([*ctl, "-l", f"{root}/log", "-o", f"-h 127.0.0.1 -p {port} -k {root} -F", "-w", "start"],
                       check=True, capture_output=True)
        try:
            yield f"tester@127.0.0.1:{port}/postgres"
        finally:
            subprocess.run([*ctl, "-m", "immediate", "-w", "stop"], check=True, capture_output=True)


@pytest.fixture
def db(url):
    md = sa.MetaData()
    for t in Base.metadata.sorted_tables:
        c = t.to_metadata(md)
        c.constraints = {x for x in c.constraints if not isinstance(x, sa.ForeignKeyConstraint)}
        c.foreign_keys.clear()
        for col in c.columns:
            col.foreign_keys.clear()
    eng = sa.create_engine(f"postgresql+psycopg2://{url}", poolclass=sa.pool.NullPool)
    with eng.begin() as conn:
        conn.execute(sa.text("DROP TABLE IF EXISTS post_tag_pin, tagger_gt_label, tagger_head_candidate, "
                             "post_tag_actor_claim"))
        md.drop_all(conn)
        md.create_all(conn)
        conn.execute(sa.text("CREATE TABLE post_tag_pin (post_id int, tag_id int, state text, pinned_by text, "
                             "audit_id bigint, PRIMARY KEY (post_id, tag_id))"))
        conn.execute(sa.text("CREATE TABLE tagger_gt_label (post_id int, tag_id int)"))
        conn.execute(sa.text("CREATE TABLE tagger_head_candidate (id serial, tag_id int, tag text, source_pool_id int)"))
        conn.execute(sa.text("CREATE TABLE post_tag_actor_claim (post_id int, tag_id int, actor_id int)"))
        conn.execute(sa.text("DO $$ BEGIN CREATE ROLE kura_reconciler; EXCEPTION WHEN duplicate_object THEN NULL; END $$"))
        conn.execute(sa.text("GRANT ALL ON tagger_gt_label, tagger_head_candidate, tag_name TO kura_reconciler"))
        conn.execute(sa.text("GRANT ALL ON SEQUENCE tagger_head_candidate_id_seq TO kura_reconciler"))

        def fill(table, row):
            for col in md.tables[table].columns:
                if col.name in row or col.nullable or col.server_default is not None or col.primary_key:
                    continue
                ty = col.type
                row[col.name] = (0 if isinstance(ty, (sa.Integer, sa.BigInteger)) else False if isinstance(ty, sa.Boolean)
                                 else datetime(2026, 1, 1) if isinstance(ty, sa.DateTime) else "x")
            conn.execute(md.tables[table].insert(), row)
        fill("tag_category", {"id": 1, "name": "general", "default": True})
        for tid, name in ((S, "s_tag"), (T, "t_tag"), (OTHER, "other")):
            fill("tag", {"id": tid, "category_id": 1, "version": 1, "creation_time": datetime(2026, 1, 1)})
            fill("tag_name", {"tag_name_id": tid, "tag_id": tid, "name": name, "ord": 0})
        for pid in range(1, 6):
            fill("post", {"id": pid, "status": "ok", "safety": "safe", "type": "image", "version": 1,
                          "creation_time": datetime(2026, 1, 1)})
    eng.dispose()
    yield f"postgresql+psycopg2://{url}", f"postgresql+asyncpg://{url}"


def run(dburls, sql, params=None):
    eng = sa.create_engine(dburls[0], poolclass=sa.pool.NullPool)
    with eng.begin() as c:
        r = c.execute(sa.text(sql), params or {})
        out = r.fetchall() if r.returns_rows else None
    eng.dispose()
    return out


def merge(dburls):
    async def go():
        engine = create_async_engine(dburls[1], poolclass=sa.pool.NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await TR.merge_tags("s_tag", "t_tag", db=s, auth_user=None)
        finally:
            await engine.dispose()
    return asyncio.run(go())


def state(dburls):
    return (sorted(run(dburls, "SELECT post_id, tag_id FROM post_tag")),
            sorted(run(dburls, "SELECT post_id, tag_id, state FROM post_tag_pin")),
            sorted(r[0] for r in run(dburls, "SELECT id FROM tag")))


CONFLICTS = {
    # the issue's case: the source pinned present, the target removed by a person (absent pin)
    "source present, target absent": ("INSERT INTO post_tag VALUES (1, 1, false)",
                                      "INSERT INTO post_tag_pin VALUES (1, 1, 'present', 'owner', NULL), (1, 2, 'absent', 'owner', NULL)"),
    "source absent, target present": ("INSERT INTO post_tag VALUES (1, 2, false)",
                                      "INSERT INTO post_tag_pin VALUES (1, 1, 'absent', 'owner', NULL), (1, 2, 'present', 'owner', NULL)"),
    "source absent, target on the post unpinned": ("INSERT INTO post_tag VALUES (1, 2, false)",
                                                   "INSERT INTO post_tag_pin VALUES (1, 1, 'absent', 'owner', NULL)"),
    "target absent, source on the post unpinned": ("INSERT INTO post_tag VALUES (1, 1, false)",
                                                   "INSERT INTO post_tag_pin VALUES (1, 2, 'absent', 'owner', NULL)"),
}


@pytest.mark.parametrize("case", sorted(CONFLICTS))
def test_a_merge_that_would_contradict_a_pin_is_refused_and_changes_nothing(db, case):
    for sql in CONFLICTS[case]:
        run(db, sql)
    run(db, "INSERT INTO post_tag VALUES (2, 1, false)")        # an unrelated post that would merge fine
    before = state(db)
    with pytest.raises(HTTPException) as e:
        merge(db)
    assert e.value.status_code == 409
    assert e.value.detail["error"] == "tag_merge_conflict" and e.value.detail["posts"][0]["post"] == 1
    assert state(db) == before


NO_CONFLICT = {
    "no pins": ("INSERT INTO post_tag VALUES (1, 1, false), (2, 2, false)",),
    "one side present": ("INSERT INTO post_tag VALUES (1, 1, false)",
                         "INSERT INTO post_tag_pin VALUES (1, 1, 'present', 'owner', NULL)"),
    "both present": ("INSERT INTO post_tag VALUES (1, 1, false), (1, 2, false)",
                     "INSERT INTO post_tag_pin VALUES (1, 1, 'present', 'owner', NULL), (1, 2, 'present', 'owner', NULL)"),
    "both absent, tag on neither": ("INSERT INTO post_tag_pin VALUES (1, 1, 'absent', 'owner', NULL), (1, 2, 'absent', 'owner', NULL)",),
    "source absent, nothing on the post": ("INSERT INTO post_tag_pin VALUES (1, 1, 'absent', 'owner', NULL)",),
}


@pytest.mark.parametrize("case", sorted(NO_CONFLICT))
def test_a_merge_without_a_contradiction_goes_through(db, case):
    for sql in NO_CONFLICT[case]:
        run(db, sql)
    merge(db)
    posts, pins, tags = state(db)
    assert S not in tags and all(t == T for _, t in posts) and all(t == T for _, t, _ in pins)
    # nowhere does the tag sit on a post whose pin says absent
    assert not run(db, """SELECT 1 FROM post_tag_pin p JOIN post_tag t USING (post_id, tag_id)
                          WHERE p.state = 'absent'""")
