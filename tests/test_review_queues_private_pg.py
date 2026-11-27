"""Actual counts SQL and asyncpg binds, on a fresh socket-only PG18 fixture."""
import asyncio
from types import SimpleNamespace
import pytest
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from tests.test_cl2_publish import database
from tests.test_review_queues import item, write
from api.routers.pools import review_queues
from api.errors import KuraError


def test_real_readonly_counts_and_unresolved_tag(database, tmp_path, monkeypatch):
    # Fixture creates a private cluster; no live DSN or credentials are read.
    _, c = database
    with c.cursor() as q:
        q.execute("CREATE TABLE tag_name(tag_id int,name text,ord int);"
                  "INSERT INTO tag_name VALUES(41,'some_tag',0),(41,'alias',1);"
                  "INSERT INTO post_tag_pin VALUES(3,41,'present'),(8,41,'absent'),(12,42,'present');"
                  "GRANT SELECT ON tag_name,post_tag_pin TO kura")
    write(tmp_path,item()); monkeypatch.setenv('KURA_REVIEW_QUEUE_DIR',str(tmp_path))
    from api.services.review_queues import load_queues
    from tests.test_review_comparison import frozen
    score_root = tmp_path.parent / (tmp_path.name + '-scores'); score_root.mkdir()
    queue, = load_queues(tmp_path); write(score_root, frozen(queue))
    monkeypatch.setenv('KURA_REVIEW_SCORE_DIR', str(score_root))
    async def exercise():
        engine = create_async_engine(URL.create('postgresql+asyncpg',username='kura',database='postgres',
            query={'host':c.info.host,'port':str(c.info.port)}))
        try:
            async with AsyncSession(engine) as db:
                from sqlalchemy import text
                await db.execute(text('SET TRANSACTION READ ONLY'))
                result = await review_queues(db,SimpleNamespace(name='reader'))
                queue, = result['queues']
                assert queue['judged'] == {'present':1,'absent':1}
                assert queue['remaining'] == 1
                assert queue['comparison']['counts'] == {'tp':1,'tn':0,'fp':1,'fn':0}
                assert queue['comparison']['compared'] == 2
                await db.rollback()
                for path in tmp_path.iterdir(): path.unlink()
                write(tmp_path,dict(item(),tag='unknown'))
                with pytest.raises(KuraError) as exc:
                    await review_queues(db,SimpleNamespace(name='reader'))
                assert exc.value.status == 503
                assert 'tag_unresolved' in str(exc.value.body())
        finally:
            await engine.dispose()
    asyncio.run(exercise())
