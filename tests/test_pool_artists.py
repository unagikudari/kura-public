"""Pool artist aggregation against an isolated in-memory SQL database.

No HTTP client, live session factory, or archive writes are used. A small async
adapter executes the real SQLAlchemy statement (including eager relationships)
on SQLite; membership/category/alias behavior is not mocked.
"""
import asyncio
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.db.models import Base, PoolPost, PostTag, Tag, TagCategory, TagLocalization, TagName
from api.routers.pools import _pool_artists


class AsyncReads:
    def __init__(self, session):
        self.session = session

    async def execute(self, statement):
        assert self.session.get_bind().dialect.name == 'sqlite'
        return self.session.execute(statement)


@pytest.fixture
def artists_db():
    engine = create_engine('sqlite:///:memory:')
    tables = [TagCategory.__table__, Tag.__table__, TagName.__table__,
              TagLocalization.__table__, PostTag.__table__, PoolPost.__table__]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        session.execute(TagCategory.__table__.insert(), [
            dict(id=1, name='artist', color='#fff', default=False, order=0),
            dict(id=2, name='general', color='#fff', default=True, order=1),
        ])
        session.execute(Tag.__table__.insert(), [
            dict(id=i, category_id=2 if i == 30 else 1,
                 creation_time=datetime(2026, 1, 1)) for i in (10, 20, 30, 40, 50)
        ])
        # Physical insertion order deliberately differs from canonical-name order.
        session.execute(TagName.__table__.insert(), [
            dict(tag_name_id=1, tag_id=10, name='creator_alias', ord=1),
            dict(tag_name_id=2, tag_id=10, name='canonical_creator', ord=0),
            dict(tag_name_id=3, tag_id=20, name='late_member_creator', ord=0),
            dict(tag_name_id=4, tag_id=30, name='artist_sounding_general_tag', ord=0),
            dict(tag_name_id=5, tag_id=40, name='other_pool_creator', ord=0),
            dict(tag_name_id=6, tag_id=50, name='tie_creator', ord=0),
        ])
        session.execute(TagLocalization.__table__.insert(), [
            dict(tag_id=10, locale='ja', name='作者日本語名'),
            dict(tag_id=10, locale='en', name='Creator English Name'),
            dict(tag_id=20, locale='ja', name='後半の作者'),
        ])
        session.execute(PoolPost.__table__.insert(), [
            *[dict(pool_id=7, post_id=i, ord=i-1) for i in range(1, 82)],
            dict(pool_id=8, post_id=900, ord=0),
            dict(pool_id=9, post_id=901, ord=0),
        ])
        session.execute(PostTag.__table__.insert(), [
            *[dict(post_id=i, tag_id=10) for i in (1, 2, 50)],
            dict(post_id=81, tag_id=20),
            dict(post_id=81, tag_id=50),
            dict(post_id=1, tag_id=30),
            dict(post_id=901, tag_id=30),
            dict(post_id=900, tag_id=40),
        ])
        session.commit()
        yield AsyncReads(session)
    engine.dispose()


def test_all_members_multiple_artists_canonical_identity_and_category(artists_db):
    rows = asyncio.run(_pool_artists(artists_db, 7))
    assert rows == [
        dict(id=10, names=['canonical_creator', 'creator_alias'], category='artist', postCount=3),
        dict(id=20, names=['late_member_creator'], category='artist', postCount=1),
        dict(id=50, names=['tie_creator'], category='artist', postCount=1),
    ]
    # Creator #20 appears only beyond page 40 / the cover. Alias #10 is one
    # identity/count, and general tags plus other-pool artists are absent.


def test_localization_is_opt_in_and_preserves_canonical_names(artists_db):
    canonical = asyncio.run(_pool_artists(artists_db, 7))
    localized = asyncio.run(_pool_artists(artists_db, 7, localize=True))
    assert all('localizedNames' not in row for row in canonical)
    assert [row['localizedNames'] for row in localized] == [
        {'ja': '作者日本語名', 'en': 'Creator English Name'}, {'ja': '後半の作者'}, {},
    ]
    assert [{k: v for k, v in row.items() if k != 'localizedNames'} for row in localized] == canonical


def test_empty_and_no_artist_members_return_empty(artists_db):
    assert asyncio.run(_pool_artists(artists_db, 404)) == []
    assert asyncio.run(_pool_artists(artists_db, 9, localize=True)) == []
    assert asyncio.run(_pool_artists(artists_db, 8))[0]['id'] == 40
