"""Small request-local search sets preserve the relational search contract."""
import itertools
import subprocess
from uuid import uuid4
import pytest
import sqlalchemy as sa
from test_collector_next import postgres, run  # noqa:F401
from api.db.models import Base
from scripts.collector_next_backup import pg_environment
from api.routers import pools as P


@pytest.fixture
def search_database(postgres):
    # Public ORM tables must not leak into other collector tests sharing the
    # temporary PostgreSQL server (notably authentication's minimal user table).
    name = 'pool_search_' + uuid4().hex
    env = pg_environment(postgres)
    subprocess.run(['psql','-X','-v','ON_ERROR_STOP=1','-c',f'CREATE DATABASE {name}'],
                   env=env,check=True,capture_output=True)
    try:
        yield postgres.replace('/postgres?', f'/{name}?')
    finally:
        subprocess.run(['psql','-X','-v','ON_ERROR_STOP=1','-c',f'DROP DATABASE {name}'],
                       env=env,check=True,capture_output=True)


def test_search_scope_equivalence_and_bounded_fallback(search_database, monkeypatch):
    from api import extension_hooks
    monkeypatch.setattr(extension_hooks, 'installed', lambda: {})  # this schema is core's ORM only
    async def scenario(sessions):
        engine = sessions.kw['bind']
        metadata = sa.MetaData()
        for model in Base.metadata.tables.values():
            table = model.to_metadata(metadata)
            table.constraints = {c for c in table.constraints if not isinstance(c, sa.ForeignKeyConstraint)}
            table.foreign_keys.clear()
            for column in table.columns:
                column.foreign_keys.clear()
        async with engine.begin() as db:
            await db.run_sync(metadata.create_all)
            statements = [
                "INSERT INTO pool_category(id,version,name,color,\"default\") VALUES(1,1,'default','#fff',true)",
                "INSERT INTO tag_category(id,version,name,color,\"default\",\"order\") VALUES(1,1,'default','#fff',true,0)",
                "INSERT INTO tag(id,category_id,version,creation_time,post_count) VALUES(1,1,1,now(),2)",
                "INSERT INTO tag_name(tag_id,name,ord) VALUES(1,'artist',0),(1,'alias',1)",
                "INSERT INTO post(id,version,creation_time,safety,type,checksum,file_size,status,flags,\"mime-type\") SELECT i,1,now(),CASE WHEN i%2=1 THEN 'safe' ELSE 'unsafe' END,'image',i::text,i*100,CASE WHEN i=4 THEN 'pending' ELSE 'ok' END,'','image/png' FROM generate_series(1,4)i",
                "INSERT INTO post_tag(post_id,tag_id,locked) VALUES(3,1,false),(4,1,false)",
                "INSERT INTO pool(id,category_id,version,creation_time,last_edit_time,cover_post_id,verify_status,meta_tagging_status,meta_tagging_attempts) SELECT i,1,1,'2026-01-01','2026-01-02',CASE WHEN i=3 THEN NULL WHEN i=2 THEN 3 WHEN i=8 THEN 2 ELSE 1 END,CASE WHEN i=5 THEN 'pending' WHEN i=6 THEN 'excluded' END,'pending',0 FROM generate_series(1,8)i",
                "INSERT INTO pool_name(pool_name_id,pool_id,name,ord) SELECT i,i,CASE WHEN i=2 THEN 'unrelated' WHEN i=7 THEN 'other' WHEN i IN (4,5) THEN 'seed artist '||i ELSE 'artist '||i END,0 FROM generate_series(1,8)i",
                "INSERT INTO pool_name(pool_name_id,pool_id,name,ord) VALUES(20,1,'alternate artist',1)",
                "INSERT INTO pool_post(pool_id,post_id,ord) VALUES(1,1,0),(1,2,1),(2,3,0),(2,4,1),(4,1,0),(5,1,0),(6,1,0),(7,1,0),(8,2,0)",
            ]
            for statement in statements:
                await db.execute(sa.text(statement))
        async with sessions() as db:
            resolved = await P._resolve_pool_query_tags(db, ['ARTIST','alias','missing'])
            assert resolved == {'artist':[1], 'alias':[1], 'missing':[]}
            for query in ('artist','tag:alias','name:artist','artist name:unrelated','missing'):
                terms = P._parse_pool_query(query)
                for hide in (False,True):
                    legacy = P._apply_pool_filters(sa.select(P.Pool.pool_id),*terms,hide)
                    actual = P._apply_pool_filters(sa.select(P.Pool.pool_id),*terms,hide,resolved)
                    assert set((await db.execute(actual)).scalars()) == set((await db.execute(legacy)).scalars())
        optimized = P._pool_search_candidates
        async def relational(db, base):
            return None
        async with sessions() as db:
            for query, safe, hide, empty in itertools.product(
                    ('artist', 'tag:alias', 'name:artist', 'artist name:unrelated', 'missing', ''),
                    (False, True), (False, True), (False, True)):
                for sort, direction, offset in (('updated','desc',0), ('id','asc',1)):
                    args = dict(query=query,safe=safe,hide_verify=hide,include_empty=empty,
                                sort=sort,dir=direction,offset=offset,limit=2,db=db)
                    monkeypatch.setattr(P,'_pool_search_candidates',relational)
                    expected = await P.list_pools(**args)
                    monkeypatch.setattr(P,'_pool_search_candidates',optimized)
                    actual = await P.list_pools(**args)
                    assert actual.model_dump() == expected.model_dump(), args
            args = dict(query='tag:artist',safe=False,hide_verify=True,include_empty=False,
                        sort='id',dir='asc',offset=0,limit=40,db=db)
            actual = await P.list_pools(**args)
            assert actual.total == 1 and actual.totalBytes == 300
            assert [r.id for r in actual.results] == [2]
            # Boundary accepts the whole set, never a truncated total/page.
            base = sa.select(P.Pool)
            monkeypatch.setattr(P,'POOL_SEARCH_CANDIDATE_LIMIT',8)
            assert len(await optimized(db,base)) == 8
            monkeypatch.setattr(P,'POOL_SEARCH_CANDIDATE_LIMIT',7)
            assert await optimized(db,base) is None
            args['query']='artist'
            monkeypatch.setattr(P,'_pool_search_candidates',relational)
            expected = await P.list_pools(**args)
            monkeypatch.setattr(P,'_pool_search_candidates',optimized)
            monkeypatch.setattr(P,'POOL_SEARCH_CANDIDATE_LIMIT',1)
            assert (await P.list_pools(**args)).model_dump() == expected.model_dump()
    run(search_database,scenario)
