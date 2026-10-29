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
            async with engine.begin() as c:
                if not (await c.execute(sa.text("SELECT count(*) FROM pool_category"))).scalar():
                    await c.execute(sa.text("INSERT INTO pool_category (version,name,color,\"default\") "
                                            "VALUES (1,'default','default',true)"))
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
