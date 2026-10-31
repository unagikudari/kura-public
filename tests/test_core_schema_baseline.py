"""Core works on the schema baseline a fresh install loads — the full one here, and the
core one the public tree ships (release/core_schema, private) when it is present.

Pool creation, membership (the observed-count trigger updates pool_url), a pool_url
update and a post insert: the paths that broke when acquisition objects were dropped in
the wrong order (the triggers that read them have to go first).
"""
import asyncio
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from full_schema import ROOT, cluster, load
from api import extension_hooks
from api.db.session import get_db
from api.routers import pools
from api.claim_storage import service as claim_storage

BASELINES = [sorted((ROOT / "api/alembic/baseline").glob("*.sql"))[-1],
             *sorted((ROOT / "release/core_schema").glob("[0-9]*.sql"))[-1:]]


@pytest.fixture(scope="module")
def pg():
    with cluster() as c:
        yield c


@pytest.mark.parametrize("baseline", BASELINES, ids=lambda p: str(p.relative_to(ROOT)))
def test_core_pool_paths_work_on_the_baseline(pg, baseline, monkeypatch):
    monkeypatch.setattr(extension_hooks, "installed", lambda: {})  # core alone
    url = load(*pg, baseline)

    async def scenario():
        engine = create_async_engine(url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        app = FastAPI(); app.include_router(pools.router)

        async def db():
            async with sessions() as s:
                yield s
        app.dependency_overrides[get_db] = db
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                pool = (await c.post("/api/pools/ensure", json={"url": "https://example.test/set/1", "count": 3})).json()
                assert pool["created"] and pool["deltaNeeded"] == 3
            async with engine.begin() as c:
                post = (await c.execute(sa.text(
                    "INSERT INTO post (user_id,version,creation_time,safety,type,checksum,\"mime-type\",status) "
                    "VALUES (NULL,1,now(),'safe','image','x','image/png','pending') RETURNING id"))).scalar()
                await c.execute(sa.text("INSERT INTO pool_post (pool_id,post_id,ord) VALUES (:p,:i,0)"),
                                {"p": pool["id"], "i": post})
                observed = (await c.execute(sa.text(
                    "SELECT observed_count FROM pool_url WHERE pool_id=:p AND is_primary"), {"p": pool["id"]})).scalar()
                assert observed == 1
                await c.execute(sa.text("UPDATE pool_url SET expected_count = 4 WHERE pool_id=:p"), {"p": pool["id"]})
                await c.execute(sa.text("DELETE FROM pool_post WHERE pool_id=:p"), {"p": pool["id"]})
                assert (await c.execute(sa.text(
                    "SELECT observed_count FROM pool_url WHERE pool_id=:p AND is_primary"), {"p": pool["id"]})).scalar() == 0
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("baseline", BASELINES, ids=lambda p: str(p.relative_to(ROOT)))
def test_a_fresh_install_starts_with_the_rows_core_needs(pg, baseline):
    """Generator 1 loaded none of these, and the 1.291.34 public release could not create a
    pool (no default pool_category), complete an auto-tag (no claim_storage.migration row) or
    activate a tagger release (no tagger_release_active row) — 2026-10-29, on a clean VM.
    The pool test above used to insert the category itself, which is how that went unseen."""
    url = load(*pg, baseline)

    async def scenario():
        engine = create_async_engine(url)
        try:
            async with engine.begin() as c:
                one = lambda sql: c.execute(sa.text(sql))
                assert (await one('SELECT name FROM pool_category WHERE "default"')).scalars().all() == ["default"]
                assert (await one("SELECT id, release_id FROM tagger_release_active")).all() == [(1, None)]
                assert (await one("SELECT paused FROM tagger_reconciler_state")).scalars().all() == [False]
                assert (await one("SELECT version FROM collector_next.schema_version")).scalars().all() == [5]
                assert (await one("SELECT count(*) FROM tagger_producer_threshold")).scalar() > 0
                backend = await claim_storage.backend(c, "a-fresh-actor")
                assert backend.normalized
        finally:
            await engine.dispose()
    asyncio.run(scenario())
