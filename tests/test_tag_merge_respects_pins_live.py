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
from types import SimpleNamespace

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


def _migration_function():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "api" / "alembic" / "versions" / "0196_governed_merge_tag.py"
    spec = importlib.util.spec_from_file_location("m0196", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod.FUNCTION


MERGE_FUNCTION = _migration_function()


def test_the_merge_does_not_switch_role():
    """kura is never a member of kura_reconciler: a role switch in the request failed every merge on an installed
    database (2026-11-15)."""
    source = (Path(__file__).resolve().parent.parent / "api" / "routers" / "tags.py").read_text()
    assert "SET LOCAL ROLE" not in source and "RESET ROLE" not in source
    assert "tagger_governed_merge_tag(:sid, :tid)" in source


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
                             "tag_merge_identity, post_tag_actor_claim, tagger_registration_request, tagger_lifecycle_event, "
                             "tagger_candidate_event"))
        md.drop_all(conn)
        md.create_all(conn)
        import importlib.util
        spec = importlib.util.spec_from_file_location("m0198", Path(__file__).parents[1] / "api/alembic/versions/0198_tag_merge_identity.py")
        migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
        conn.execute(sa.text("CREATE ROLE replicator") if not conn.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname='replicator'")).scalar() else sa.text("SELECT 1"))
        conn.execute(sa.text("DROP FUNCTION IF EXISTS tag_merge_identity_immutable() CASCADE"))
        for statement in migration.STATEMENTS:
            conn.execute(sa.text(statement))
        conn.execute(sa.text("CREATE TABLE post_tag_pin (post_id int, tag_id int, state text, pinned_by text, "
                             "audit_id bigint, PRIMARY KEY (post_id, tag_id))"))
        conn.execute(sa.text("CREATE TABLE tagger_gt_label (post_id int, tag_id int)"))
        conn.execute(sa.text("CREATE TABLE tagger_head_candidate (id serial, tag_id int, tag text, source_pool_id int)"))
        conn.execute(sa.text("CREATE TABLE post_tag_actor_claim (post_id int, tag_id int, actor_id int)"))
        # what the merge reads of the tagger: the tables whose rows make a tag "live" for it, and its history
        for table, cols in (("tagger_patch", "tag_id int"), ("tagger_head_policy", "tag_id int"),
                            ("tagger_node_head", "tag_id int"), ("tagger_tag_rule", "tag_id int"),
                            ("tagger_tag_exclusion", "tag_a_id int, tag_b_id int"),
                            ("tagger_candidate_disposition", "tag_id int"), ("tag_quarantine", "tag_id int"),
                            ("tagger_registration_request", "id serial, tag_id int REFERENCES tag(id), tag text, status text"),
                            ("tagger_lifecycle_event", "id serial, tag_id int REFERENCES tag(id), tag text, to_stage text, "
                                                       "occurred_at timestamptz DEFAULT now()"),
                            ("tagger_candidate_event", "seq serial, tag_id int REFERENCES tag(id), tag text, kind text")):
            conn.execute(sa.text(f"DROP TABLE IF EXISTS {table} CASCADE"))
            conn.execute(sa.text(f"CREATE TABLE {table} ({cols})"))
        # the governed tagger rows move through the function of migration 0196, as written there (the router never
        # switches role: kura is not a member of kura_reconciler); the test session is its allowed executor
        conn.execute(sa.text("DROP TABLE IF EXISTS tagger_allowed_executor"))
        conn.execute(sa.text("CREATE TABLE tagger_allowed_executor (db_role text, scope text)"))
        conn.execute(sa.text("INSERT INTO tagger_allowed_executor VALUES (session_user, 'gt-sync')"))
        conn.execute(sa.text(MERGE_FUNCTION))

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
                return await TR.merge_tags("s_tag", "t_tag", db=s, auth_user=SimpleNamespace(user_id=42))
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


HISTORY = ("INSERT INTO tagger_lifecycle_event (tag_id, tag, to_stage) VALUES (1, 's_tag', 'candidate')",
           "INSERT INTO tagger_candidate_event (tag_id, tag, kind) VALUES (1, 's_tag', 'held')",
           "INSERT INTO tagger_registration_request (tag_id, tag, status) VALUES (1, 's_tag', 'rejected')")


def history(dburls):
    return (run(dburls, "SELECT id, tag_id, tag, to_stage FROM tagger_lifecycle_event ORDER BY id"),
            run(dburls, "SELECT seq, tag_id, tag, kind FROM tagger_candidate_event ORDER BY seq"),
            run(dburls, "SELECT id, tag_id, tag, status FROM tagger_registration_request ORDER BY id"))


def test_a_source_with_tagger_history_stays_as_a_nameless_retired_identity(db):
    """The append-only history names the identity it was recorded for: it is not rewritten, the source row stays for
    it without a name or posts, and everything a person sees of the tag is on the target (2026-11-15: merging
    dog_penis failed on these foreign keys)."""
    for sql in HISTORY + ("INSERT INTO post_tag VALUES (1, 1, false), (2, 2, false)",
                          "INSERT INTO post_tag_pin VALUES (1, 1, 'present', 'owner', NULL)"):
        run(db, sql)
    before = history(db)
    merge(db)
    assert history(db) == before
    assert run(db, "SELECT post_count FROM tag WHERE id = 1") == [(0,)]
    assert run(db, "SELECT count(*) FROM tag_name WHERE tag_id = 1") == [(0,)]
    assert sorted(r[0] for r in run(db, "SELECT name FROM tag_name WHERE tag_id = 2")) == ["s_tag", "t_tag"]
    assert run(db, "SELECT count(*) FROM post_tag WHERE tag_id = 1") == [(0,)]
    assert sorted(run(db, "SELECT post_id FROM post_tag WHERE tag_id = 2")) == [(1,), (2,)]
    assert run(db, "SELECT post_id, tag_id, state FROM post_tag_pin") == [(1, 2, "present")]


def test_a_source_without_tagger_history_is_deleted(db):
    run(db, "INSERT INTO post_tag VALUES (1, 1, false)")
    merge(db)
    assert run(db, "SELECT count(*) FROM tag WHERE id = 1") == [(0,)]


LIVE = {
    "lifecycle stage training": "INSERT INTO tagger_lifecycle_event (tag_id, tag, to_stage) VALUES (1, 's_tag', 'training')",
    "registration validated": "INSERT INTO tagger_registration_request (tag_id, tag, status) VALUES (1, 's_tag', 'validated')",
    "registration pending": "INSERT INTO tagger_registration_request (tag_id, tag, status) VALUES (1, 's_tag', 'pending')",
    "patch": "INSERT INTO tagger_patch VALUES (1)",
    "head policy": "INSERT INTO tagger_head_policy VALUES (1)",
    "node head": "INSERT INTO tagger_node_head VALUES (1)",
}


@pytest.mark.parametrize("case", sorted(LIVE))
def test_a_tag_the_tagger_is_working_on_is_not_merged(db, case):
    """A patch, a head policy, an open measurement or a lifecycle past candidate stays with its identity: the merge
    moves none of it, so it refuses by name and changes nothing. A retired source is therefore never in the live set
    (the training queue, the review of open measurements, the served head policies)."""
    for sql in HISTORY + (LIVE[case], "INSERT INTO post_tag VALUES (1, 1, false)"):
        run(db, sql)
    before = state(db), history(db)
    with pytest.raises(HTTPException) as e:
        merge(db)
    assert e.value.status_code == 409 and e.value.detail["error"] == "tag_merge_live_tagger_state"
    assert case in e.value.detail["state"]
    assert (state(db), history(db)) == before


def test_merge_records_identity_and_actor_without_rewriting_history(db):
    for sql in HISTORY:
        run(db, sql)
    before = history(db)
    merge(db)
    assert history(db) == before
    assert run(db, "SELECT source_id,target_id,source_name,target_name,actor_user_id FROM tag_merge_identity") == [(S,T,'s_tag','t_tag',42)]


@pytest.mark.parametrize("sql", ["UPDATE tag_merge_identity SET target_id=3", "DELETE FROM tag_merge_identity", "TRUNCATE tag_merge_identity"])
def test_merge_identity_is_immutable(db, sql):
    merge(db)
    with pytest.raises(sa.exc.DBAPIError, match='tag_merge_identity_immutable'):
        run(db, sql)


def test_refused_merge_records_no_identity_edge(db):
    for sql in CONFLICTS['source present, target absent']:
        run(db, sql)
    with pytest.raises(HTTPException):
        merge(db)
    assert run(db, "SELECT source_id FROM tag_merge_identity") == []


def test_failed_commit_rolls_back_the_edge_and_merge(db, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession
    before = state(db)
    async def refuse_commit(self):
        raise RuntimeError('synthetic_commit_failure')
    monkeypatch.setattr(AsyncSession, 'commit', refuse_commit)
    with pytest.raises(RuntimeError, match='synthetic_commit_failure'):
        merge(db)
    assert state(db) == before
    assert run(db, 'SELECT source_id FROM tag_merge_identity') == []


def test_later_merge_keeps_the_original_edge(db):
    merge(db)
    async def later():
        engine = create_async_engine(db[1], poolclass=sa.pool.NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                await TR.merge_tags('t_tag', 'other', db=session, auth_user=SimpleNamespace(user_id=43))
        finally:
            await engine.dispose()
    asyncio.run(later())
    assert run(db, 'SELECT source_id,target_id,actor_user_id FROM tag_merge_identity ORDER BY source_id') == [(S,T,42),(T,OTHER,43)]


def test_concurrent_merges_make_one_edge_and_refuse_stale_identity(db, monkeypatch):
    for sql in HISTORY:
        run(db, sql)
    async def go():
        engine = create_async_engine(db[1], poolclass=sa.pool.NullPool)
        ready = asyncio.Event()
        count = 0
        original = TR._find_tag
        async def find(session, name):
            nonlocal count
            found = await original(session, name)
            if name == 't_tag':
                count += 1
                if count == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), timeout=5)
            return found
        monkeypatch.setattr(TR, '_find_tag', find)
        async def one():
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                try:
                    return await TR.merge_tags('s_tag', 't_tag', db=session, auth_user=SimpleNamespace(user_id=42))
                except HTTPException as exc:
                    return exc
        try:
            return await asyncio.wait_for(asyncio.gather(one(), one()), timeout=10)
        finally:
            await engine.dispose()
    results = asyncio.run(go())
    assert sum(isinstance(x, dict) for x in results) == 1
    refused = [x for x in results if isinstance(x, HTTPException)]
    assert len(refused) == 1 and refused[0].status_code == 409
    assert run(db, 'SELECT source_id,target_id FROM tag_merge_identity') == [(S,T)]


def test_migration_as_api_role_and_replicator_has_no_access(db):
    import importlib.util
    spec = importlib.util.spec_from_file_location('m0198_role', Path(__file__).parents[1] / 'api/alembic/versions/0198_tag_merge_identity.py')
    migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    eng = sa.create_engine(db[0], poolclass=sa.pool.NullPool)
    try:
        with eng.begin() as conn:
            conn.execute(sa.text('DROP TABLE tag_merge_identity'))
            conn.execute(sa.text('DROP FUNCTION tag_merge_identity_immutable()'))
            if not conn.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname='kura'")).scalar():
                conn.execute(sa.text('CREATE ROLE kura'))
            conn.execute(sa.text('GRANT USAGE, CREATE ON SCHEMA public TO kura'))
            conn.execute(sa.text('SET LOCAL ROLE kura'))
            class Operations:
                def execute(self, statement):
                    conn.execute(sa.text(statement))
            original = migration.op
            migration.op = Operations()
            try:
                migration.upgrade()
            finally:
                migration.op = original
            conn.execute(sa.text("INSERT INTO tag_merge_identity(source_id,target_id,source_name,target_name,actor_user_id) VALUES(1,2,'a','b',42)"))
            assert conn.execute(sa.text('SELECT count(*) FROM tag_merge_identity')).scalar() == 1
            assert not conn.execute(sa.text("SELECT has_table_privilege('replicator','tag_merge_identity','SELECT')")).scalar()
        with pytest.raises(sa.exc.DBAPIError, match='permission denied'):
            with eng.begin() as conn:
                conn.execute(sa.text('SET LOCAL ROLE replicator'))
                conn.execute(sa.text('SELECT * FROM tag_merge_identity'))
    finally:
        eng.dispose()
