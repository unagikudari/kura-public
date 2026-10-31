"""GET /api/post/{id}/index gives a post's position in exactly the listing the grid shows
(#148): the same set (status ok, safe mode, every query filter, advertisements hidden unless
searched for) and the same total order, ties included (pool ord ties broken by post id,
equal update times by post id). A post outside the listing is said to be outside it.

Runs on a private, throwaway PostgreSQL cluster (or KURA_INDEX_TEST_DATABASE_URL pointing at
a database named kura_index_test); never the application database.
"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sa = pytest.importorskip("sqlalchemy")
pytest.importorskip("asyncpg")
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from api.db.models import PoolPost, Post, PostTag, TagImplication, TagName  # noqa: E402
from api.routers import posts as P  # noqa: E402


@pytest.fixture(scope="module")
def database_url():
    configured = os.environ.get("KURA_INDEX_TEST_DATABASE_URL")
    if configured:
        assert sa.engine.make_url(configured).database == "kura_index_test", "test DB must be named kura_index_test"
        yield configured
        return
    bindir = Path(subprocess.check_output(["pg_config", "--bindir"], text=True).strip())
    if not (bindir / "initdb").exists():
        pytest.skip("needs PostgreSQL's initdb, or KURA_INDEX_TEST_DATABASE_URL")
    with tempfile.TemporaryDirectory(prefix="kura148-") as root:
        cluster = Path(root) / "db"
        subprocess.run([str(bindir / "initdb"), "-D", str(cluster), "-A", "trust", "-U", "tester"],
                       check=True, capture_output=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        ctl = [str(bindir / "pg_ctl"), "-D", str(cluster)]
        subprocess.run([*ctl, "-l", str(Path(root) / "postgres.log"), "-o",
                        f"-h 127.0.0.1 -p {port} -k {root} -F", "-w", "start"], check=True, capture_output=True)
        try:
            yield f"postgresql+asyncpg://tester@127.0.0.1:{port}/postgres"
        finally:
            subprocess.run([*ctl, "-m", "immediate", "-w", "stop"], check=True, capture_output=True)


T0 = datetime(2026, 1, 1)
POOL, OTHER_POOL = 7, 8
# post id → (status, safety, pool ord or None, updated offset in minutes, tags)
FIXTURE = {}
for i in range(1, 42):                      # 41 pool members, all ord 0 (the issue's case)
    FIXTURE[i] = ("ok", "safe", 0, 0, set())
FIXTURE.update({
    50: ("ok", "safe", 2, 5, {"night"}), 51: ("ok", "unsafe", 2, 5, {"night"}),
    52: ("ok", "safe", 1, 5, {"night", "advertisement"}), 53: ("near_dup", "safe", 1, 1, {"night"}),
    54: ("ok", "safe", 3, 2, set()), 55: ("ok", "sketchy", 1, 2, {"night"}),
    60: ("ok", "safe", None, 9, set()), 61: ("ok", "safe", None, 9, {"advertisement"}),
})


@pytest.fixture(scope="module")
def sessions(database_url):
    async def setup():
        engine = create_async_engine(database_url, poolclass=sa.pool.NullPool)   # each asyncio.run is its own loop
        metadata = sa.MetaData()
        for model in (Post, PoolPost, PostTag, TagName, TagImplication):
            table = model.__table__.to_metadata(metadata)
            table.constraints = {c for c in table.constraints if not isinstance(c, sa.ForeignKeyConstraint)}
            table.foreign_keys.clear()
            for column in table.columns:
                column.foreign_keys.clear()
        async with engine.begin() as conn:
            await conn.run_sync(metadata.drop_all)
            await conn.run_sync(metadata.create_all)
            tags = sorted({t for *_, ts in FIXTURE.values() for t in ts})
            tag_id = {t: n for n, t in enumerate(tags, 1)}
            for t, n in tag_id.items():
                await conn.execute(sa.text("INSERT INTO tag_name (tag_name_id, tag_id, name, ord) VALUES (:n, :n, :t, 0)"),
                                   {"n": n, "t": t})
            post_cols = {c.name for c in metadata.tables["post"].columns}
            for pid, (status, safety, ord_, minutes, ts) in FIXTURE.items():
                row = {"id": pid, "status": status, "safety": safety, "type": "image",
                       "creation_time": T0, "last_edit_time": T0 + timedelta(minutes=minutes)}
                required = {c.name: c for c in metadata.tables["post"].columns
                            if not c.nullable and c.server_default is None and c.name not in row and not c.primary_key}
                for name, col in required.items():
                    row[name] = 0 if isinstance(col.type, (sa.Integer, sa.BigInteger)) else (
                        False if isinstance(col.type, sa.Boolean) else T0 if isinstance(col.type, sa.DateTime) else "x")
                await conn.execute(metadata.tables["post"].insert(), {c: v for c, v in row.items() if c in post_cols})
                if ord_ is not None:
                    await conn.execute(sa.text("INSERT INTO pool_post (pool_id, post_id, ord) VALUES (:p, :i, :o)"),
                                       {"p": POOL, "i": pid, "o": ord_})
                for t in ts:
                    await conn.execute(sa.text("INSERT INTO post_tag (post_id, tag_id) VALUES (:i, :t)"),
                                       {"i": pid, "t": tag_id[t]})
            await conn.execute(sa.text("INSERT INTO pool_post (pool_id, post_id, ord) VALUES (:p, 60, 0)"), {"p": OTHER_POOL})
        return engine
    engine = asyncio.run(setup())
    yield async_sessionmaker(engine, expire_on_commit=False)
    asyncio.run(engine.dispose())


def expected(query: str, sort: str, dir: str, safe: bool) -> list[int]:
    """The listing worked out by hand from FIXTURE: the set and the order the grid shows."""
    words = query.split()
    rows = []
    for pid, (status, safety, ord_, minutes, ts) in FIXTURE.items():
        if status != "ok" or (safe and safety != "safe"):
            continue
        if "advertisement" not in query and "advertisement" in ts:
            continue
        if f"pool:{POOL}" in words and ord_ is None:
            continue
        if f"pool:{OTHER_POOL}" in words and pid != 60:
            continue
        if any(w not in ts for w in words if ":" not in w and not w.startswith("-")):
            continue
        if any(w[1:] in ts for w in words if w.startswith("-") and ":" not in w):
            continue
        rows.append(pid)
    pool = f"pool:{POOL}" in words
    if sort == "id":
        return sorted(rows, reverse=dir == "desc")
    if sort == "updated":
        return sorted(rows, key=lambda p: (FIXTURE[p][3], p), reverse=dir == "desc")
    if pool and sort == "page":
        return sorted(rows, key=lambda p: (FIXTURE[p][2], p), reverse=dir == "desc")
    if pool:
        return sorted(rows, key=lambda p: (FIXTURE[p][2], p))
    return sorted(rows, reverse=True)


def index(sessions, pid, query="", sort="", dir="desc", safe=False):
    async def go():
        async with sessions() as db:
            return await P.get_post_index(pid, query=query, sort=sort, dir=dir, db=db, safe=safe)
    return asyncio.run(go())


def listed(sessions, query="", sort="", dir="desc", safe=False):
    async def go():
        async with sessions() as db:
            base, pool = await P._listing_set(db, query, safe)
            stmt, keys, asc = P._listing_order(base, pool, sort, dir)
            stmt = stmt.with_only_columns(Post.post_id).order_by(*(k.asc() if asc else k.desc() for k in keys))
            return list((await db.execute(stmt)).scalars())
    return asyncio.run(go())


def test_the_issue_case_41_members_of_one_ord_the_largest_id_is_40(sessions):
    q = f"pool:{POOL}"
    order = expected(q, "", "desc", False)
    assert order[:41] == list(range(1, 42))
    assert index(sessions, 41, q) == {"idx": 40, "member": True}


CASES = [(q, s, d, safe)
         for q in ("", f"pool:{POOL}", f"pool:{POOL} night", f"pool:{POOL} -night", "night", "advertisement",
                   f"pool:{POOL} advertisement", f"pool:{OTHER_POOL}")
         for s in ("", "id", "updated", "page") for d in ("asc", "desc") for safe in (False, True)]


@pytest.mark.parametrize("query,sort,dir,safe", CASES)
def test_every_post_s_index_is_its_place_in_the_listing(sessions, query, sort, dir, safe):
    order = expected(query, sort, dir, safe)
    assert listed(sessions, query, sort, dir, safe) == order        # the listing itself is what the grid shows
    for place, pid in enumerate(order):
        assert index(sessions, pid, query, sort, dir, safe) == {"idx": place, "member": True}, (pid, place)
    for pid in set(FIXTURE) - set(order):
        assert index(sessions, pid, query, sort, dir, safe) == {"idx": 0, "member": False}, pid


def test_list_posts_and_the_index_share_one_definition():
    import inspect
    src = inspect.getsource(P.list_posts), inspect.getsource(P.get_post_index)
    for s in src:
        assert "_listing_set(" in s and "_listing_order(" in s
