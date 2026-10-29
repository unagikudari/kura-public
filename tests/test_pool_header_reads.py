"""Pool header loads metadata without touching member ORM rows or media bytes."""
import asyncio
from datetime import datetime

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from api.db.models import Base, Pool, PoolCategory, PoolName, PoolState, PoolUrl, PoolPost, PostTag, Tag, TagCategory, TagName, TagLocalization
from api.routers.pools import get_pool, _load_pool


class AsyncReads:
    def __init__(self, session):
        self.session = session
    async def execute(self, statement):
        return self.session.execute(statement)


def test_header_keeps_details_and_artists_without_loading_post_graph(monkeypatch):
    from api import extension_hooks
    monkeypatch.setattr(extension_hooks, 'installed', lambda: {})  # extensions' annotations are their own tests'
    engine = create_engine('sqlite:///:memory:')
    tables = [m.__table__ for m in (Pool, PoolCategory, PoolName, PoolState, PoolUrl, PoolPost, PostTag, Tag, TagCategory, TagName, TagLocalization)]
    Base.metadata.create_all(engine, tables=tables)
    with engine.begin() as db:
        # Deliberately no full Post table: selecting any unused post metadata fails.
        db.execute(text('CREATE TABLE post(id integer PRIMARY KEY,source text)'))
        db.execute(PoolCategory.__table__.insert(), {'id': 1, 'name': 'default', 'color': '#fff', 'default': True, 'ord': 0})
        db.execute(Pool.__table__.insert(), {'id': 7, 'category_id': 1, 'version': 2,
            'creation_time': datetime(2026, 1, 1), 'description': 'details', 'cover_post_id': 1})
        db.execute(PoolName.__table__.insert(), {'pool_name_id': 1, 'pool_id': 7, 'name': 'work', 'ord': 0})
        db.execute(text("INSERT INTO post VALUES(1,'https://example.test/source')"))
        db.execute(text('INSERT INTO pool_post VALUES(7,1,0)'))
    statements = []
    event.listen(engine, 'before_cursor_execute', lambda conn, cursor, sql, parameters, context, executemany: statements.append(sql))
    with Session(engine) as session:
        async def scenario():
            metadata = await get_pool('7', AsyncReads(session), localize=True, include_posts=False)
            assert metadata['name'] == 'work' and metadata['description'] == 'details'
            assert metadata['firstPostSource'] == 'https://example.test/source'
            assert metadata['artists'] == [] and metadata['focusTag'] is None
            assert 'posts' not in metadata and 'postCount' not in metadata
            loaded = await _load_pool('7', AsyncReads(session), include_posts=False)
            from sqlalchemy.exc import InvalidRequestError
            import pytest
            with pytest.raises(InvalidRequestError):
                _ = loaded._posts  # accidental future member traversal must fail loudly
        asyncio.run(scenario())
    assert all('post.data' not in sql and 'post.thumbnail' not in sql for sql in statements)
    assert not any('JOIN post AS' in sql or 'FROM pool_post LEFT OUTER JOIN post' in sql for sql in statements)
    engine.dispose()


def test_pool_list_omits_descriptions_and_parent_backref_reloads():
    from sqlalchemy import select, inspect
    from api.routers.pools import pool_list_options
    from sqlalchemy.exc import InvalidRequestError
    import pytest
    engine=create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine,tables=[m.__table__ for m in (Pool,PoolCategory,PoolName,PoolState,PoolUrl)])
    with engine.begin() as db:
        db.execute(PoolCategory.__table__.insert(),dict(id=1,name='default',color='#fff',default=True,order=0))
        db.execute(Pool.__table__.insert(),dict(id=7,category_id=1,version=1,creation_time=datetime(2026,1,1),description='unused'*10000))
        db.execute(PoolState.__table__.insert(),dict(pool_id=7,scope='collection',notes='unused'))
        db.execute(PoolUrl.__table__.insert(),dict(pool_id=7,url='https://example.test',is_primary=True,created_at=datetime(2026,1,1)))
    statements=[]
    event.listen(engine,'before_cursor_execute',lambda conn,cursor,sql,parameters,context,executemany:statements.append(sql))
    with Session(engine) as session:
        pool=session.execute(select(Pool).options(*pool_list_options())).scalar_one()
        assert pool.version==1 and pool.state.scope=='collection' and pool.primary_url.url=='https://example.test'
        assert 'description' in inspect(pool).unloaded
        with pytest.raises(InvalidRequestError): _=pool.description
        with pytest.raises(InvalidRequestError): _=pool.category
        with pytest.raises(InvalidRequestError): _=pool.state.pool
        with pytest.raises(InvalidRequestError): _=pool.primary_url.pool
    assert all('pool.description' not in sql and 'JOIN pool AS' not in sql for sql in statements)
    engine.dispose()
