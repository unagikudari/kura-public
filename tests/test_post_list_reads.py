"""List memberships must not reread posts or pool operational state."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session, load_only

from api.db.models import (Base, Post, Pool, PoolCategory, PoolName, PoolPost,
    PostTag, PostFavorite, User, Tag, TagName, TagCategory, TagLocalization)
from api.routers.posts import post_list_options


def test_membership_graph_is_bounded_and_keeps_pool_links():
    engine = create_engine('sqlite:///:memory:')
    # No pool_state table, and only two Post columns: unwanted joins fail.
    Base.metadata.create_all(engine, tables=[m.__table__ for m in (
        Pool, PoolCategory, PoolName, PoolPost, PostTag, PostFavorite, User,
        Tag, TagName, TagCategory, TagLocalization)])
    with engine.begin() as db:
        db.execute(text('CREATE TABLE post(id integer PRIMARY KEY,user_id integer)'))
        db.execute(text('INSERT INTO post VALUES(1,NULL)'))
        db.execute(PoolCategory.__table__.insert(),dict(id=1,name='default',color='#fff',default=True,ord=0))
        db.execute(Pool.__table__.insert(),dict(id=7,category_id=1,version=1,creation_time=datetime(2026,1,1),description='keep description'))
        db.execute(PoolName.__table__.insert(),dict(pool_name_id=1,pool_id=7,name='work',ord=0))
        db.execute(text('INSERT INTO pool_post VALUES(7,1,0)'))
    sql=[]
    event.listen(engine,'before_cursor_execute',lambda conn,cursor,statement,parameters,context,many:sql.append(statement))
    with Session(engine) as db:
        post=db.execute(select(Post).options(load_only(Post.post_id,Post.user_id),*post_list_options(True))).scalar_one()
        membership=post._pools[0]
        assert membership.pool.names[0].name=='work'
        assert membership.pool.description=='keep description'
        assert membership.pool.category.name=='default'
        with pytest.raises(InvalidRequestError): _=membership.post
        with pytest.raises(InvalidRequestError): _=membership.pool.state
    assert all('JOIN post AS' not in s and 'pool_state' not in s for s in sql)
    engine.dispose()
