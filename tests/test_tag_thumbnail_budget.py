"""A tag with no color images has a bounded candidate budget and a fallback."""
import sqlalchemy as sa
from test_collector_next import postgres, run  # noqa:F401
from test_pool_search_candidates import search_database  # noqa:F401
from api.services.tag_thumbnails import representatives, CANDIDATE_SCAN_LIMIT


def test_monochrome_budget_distinct_choices_empty_and_shared_fallback(search_database):
    async def scenario(sessions):
        async with sessions.begin() as db:
            # Membership lives in post_tag only; tag 90 is `monochrome`.
            await db.execute(sa.text('CREATE TABLE post(id int PRIMARY KEY)'))
            await db.execute(sa.text('CREATE TABLE post_tag(tag_id int,post_id int,PRIMARY KEY(tag_id,post_id))'))
            await db.execute(sa.text('CREATE TABLE tag_name(tag_id int,name text,ord int)'))
            await db.execute(sa.text("INSERT INTO tag_name VALUES(90,'monochrome',0),(91,'color',0)"))
            await db.execute(sa.text("INSERT INTO post SELECT i FROM generate_series(1,20000)i"))
            await db.execute(sa.text("INSERT INTO post VALUES(30001),(30002)"))
            await db.execute(sa.text('INSERT INTO post_tag SELECT 90,i FROM generate_series(1,20000)i'))
            await db.execute(sa.text('INSERT INTO post_tag VALUES(91,30001),(91,30002)'))
            await db.execute(sa.text('INSERT INTO post_tag SELECT 1,id FROM post'))
            await db.execute(sa.text('INSERT INTO post_tag VALUES(2,30001),(2,30002),(3,30001),(3,30002),(4,30001)'))
        async with sessions() as db:
            # The old search walked all 20k monochrome posts to find late colors.
            assert await representatives(db,[1]) == {1:1}
            assert CANDIDATE_SCAN_LIMIT < 20000
            assert await representatives(db,[2,3,4,999]) == {2:30001,3:30002,4:30001}
            assert await representatives(db,[3,2]) == {3:30001,2:30002}
            assert await representatives(db,[]) == {}
    run(search_database,scenario)
