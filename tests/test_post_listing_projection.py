"""Small display rows remain exact while unrelated tag edits leave them untouched."""
import asyncio
from sqlalchemy import text
from test_collector_next import postgres,run  # noqa:F401
from test_pool_search_candidates import search_database  # noqa:F401
from api.read_models.post_listing import SCHEMA,totals,rebuild


async def setup(sessions):
    async with sessions.begin() as db:
        for q in ('CREATE TABLE post(id int PRIMARY KEY,status text,safety varchar(32),file_size bigint,tags text[])',
                  'CREATE TABLE tag_name(tag_id int,name text)',
                  'CREATE TABLE post_tag(post_id int,tag_id int)',
                  "INSERT INTO post VALUES(1,'ok','safe',10,'{}'),(2,'ok','unsafe',20,'{}'),(3,'pending','safe',30,'{}'),(4,'ok',NULL,NULL,'{}')",
                  "INSERT INTO tag_name VALUES(1,'advertisement'),(2,'alias')",
                  'INSERT INTO post_tag VALUES(2,1),(2,1)'):
            await db.execute(text(q))
        for q in SCHEMA.read_text().split('-- statement-break'):
            if q.strip():await db.execute(text(q))


async def equal(db):
    for safe in (False,True):
        q="SELECT count(*),coalesce(sum(p.file_size),0) FROM post p WHERE p.status='ok' AND NOT EXISTS (SELECT 1 FROM post_tag pt JOIN tag_name n ON n.tag_id=pt.tag_id WHERE pt.post_id=p.id AND n.name='advertisement')"+(" AND p.safety='safe'" if safe else '')
        expected=(await db.execute(text(q))).one()
        assert await totals(db,safe)==tuple(map(int,expected))
    diff=(await db.execute(text("""WITH expected AS (SELECT id,safety,file_size FROM post WHERE status='ok'),
      missing AS (SELECT * FROM expected EXCEPT ALL SELECT * FROM post_listing),
      extra AS (SELECT * FROM post_listing EXCEPT ALL SELECT * FROM expected)
      SELECT (SELECT count(*) FROM missing)+(SELECT count(*) FROM extra)"""))).scalar_one()
    assert diff==0


def test_projection_seed_changes_aliases_deletes_and_rebuild(search_database):
    async def scenario(sessions):
        await setup(sessions)
        async with sessions.begin() as db:
            await equal(db)
            before=(await db.execute(text('SELECT ctid::text FROM post_listing WHERE post_id=1'))).scalar_one()
            await db.execute(text("UPDATE post SET tags=ARRAY['new'] WHERE id=1"))
            assert (await db.execute(text('SELECT ctid::text FROM post_listing WHERE post_id=1'))).scalar_one()==before
            for q in ("UPDATE post SET file_size=15 WHERE id=1", "UPDATE post SET safety='safe' WHERE id=2",
                      "UPDATE post SET status='ok' WHERE id=3", "UPDATE post SET status='lost' WHERE id=1",
                      "UPDATE tag_name SET name='different' WHERE tag_id=1", "UPDATE tag_name SET name='advertisement' WHERE tag_id=2",
                      "UPDATE post SET id=5 WHERE id=3", "DELETE FROM post WHERE id=4",
                      "INSERT INTO post VALUES(6,'ok','safe',60,'{}')"):
                await db.execute(text(q));await equal(db)
            await db.execute(text('DELETE FROM post_listing WHERE post_id=6'))
            assert (await rebuild(db))['rows']==3
            await equal(db)
    run(search_database,scenario)


def test_independent_posts_do_not_share_an_aggregate_writer_lock(search_database):
    async def scenario(sessions):
        await setup(sessions)
        async with sessions.begin() as first:
            await first.execute(text('UPDATE post SET file_size=11 WHERE id=1'))
            async with sessions.begin() as second:
                await second.execute(text("SET LOCAL lock_timeout='250ms'"))
                await second.execute(text('UPDATE post SET file_size=22 WHERE id=2'))
        async with sessions.begin() as db:await equal(db)
    run(search_database,scenario)
