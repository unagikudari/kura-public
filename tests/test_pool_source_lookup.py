"""Core pool lookup preserves joined aliases for canonical and provider matches.

The optional extension retains its adapter tests; this contract belongs to core's
extension_api and must also run from a checkout without installed extensions.
"""
import asyncio
import sys
from pathlib import Path

import pytest
import sqlalchemy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.mark.parametrize("lookup", ["canonical", "sibling"])
def test_upload_reuses_a_pool_with_joined_names(lookup, monkeypatch):
    """Exercise ORM Results, not a scalar-returning mock. Pool.names is joined
    eagerly, so scalar_one() needs unique() even for a single matching pool.
    SQLite holds only these pool tables; no production connection is used.
    """
    from datetime import datetime
    from sqlalchemy.orm import Session
    from api.db.models import Base, Pool, PoolCategory, PoolName, PoolState, PoolUrl
    from api.extension_api import pool_by_source_url as _get_or_create_pool_by_source_url
    from api import pool_identity, site_hooks

    engine = sqlalchemy.create_engine("sqlite://")
    sqlalchemy.event.listen(
        engine, "connect", lambda conn, _: conn.create_function(
            "now", 0, lambda: "2026-10-09 00:00:00"))
    Base.metadata.create_all(engine, tables=[
        PoolCategory.__table__, Pool.__table__, PoolName.__table__,
        PoolState.__table__, PoolUrl.__table__,
    ])
    monkeypatch.setattr(pool_identity, "CANONICAL_POOL_LOOKUP_SQL",
                        "SELECT 9181" if lookup == "canonical" else "SELECT NULL")

    async def sibling(db, url):
        return 9181

    monkeypatch.setattr(site_hooks, "find_sibling_source_pool", sibling)
    old_url, new_url = "provider:old/archive.zip", "provider:new/archive.zip"
    try:
        with Session(engine) as session:
            session.add(PoolCategory(id=1, name="default", default=True))
            session.add(Pool(pool_id=9181, category_id=1,
                             creation_time=datetime(2026, 10, 9)))
            session.flush()
            session.add_all([
                PoolName(pool_id=9181, name="June archive", order=0),
                PoolName(pool_id=9181, name="Archive alias", order=1),
                PoolUrl(pool_id=9181, url=old_url, is_primary=True,
                        created_at=datetime(2026, 10, 9)),
            ])
            session.commit()

            class DB:
                async def execute(self, *args, **kwargs):
                    return session.execute(*args, **kwargs)

                def add(self, obj):
                    session.add(obj)

                async def flush(self):
                    session.flush()

            pool = asyncio.run(_get_or_create_pool_by_source_url(DB(), new_url))
            assert pool.pool_id == 9181
            assert [n.name for n in pool.names] == ["June archive", "Archive alias"]
            assert session.query(Pool).count() == 1
            if lookup == "sibling":
                alias = session.query(PoolUrl).filter_by(url=new_url).one()
                assert alias.pool_id == 9181 and not alias.is_primary
            assert session.query(PoolUrl).filter_by(url=old_url).one().is_primary
    finally:
        engine.dispose()
