"""Real tag query semantics without images, useless DISTINCT, or autocomplete counts."""
import asyncio
from datetime import datetime

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from api.db.models import Base, Tag, TagName, TagCategory, TagLocalization
from api.routers.tags import search_tags

class AsyncReads:
    def __init__(self, session): self.session = session
    async def execute(self, statement): return self.session.execute(statement)


def test_text_catalog_keeps_alias_locale_category_order_and_exact_total():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine, tables=[m.__table__ for m in (Tag, TagName, TagCategory, TagLocalization)])
    with engine.begin() as db:
        db.execute(TagCategory.__table__.insert(), [
            dict(id=1,name='general',color='#aaa',default=True,order=0),
            dict(id=2,name='artist',color='#fff',default=False,order=1)])
        db.execute(Tag.__table__.insert(), [dict(id=i, category_id=1 if i<3 else 2,
            creation_time=datetime(2026,1,1), post_count=100-i, description='retained') for i in (1,2,3)])
        db.execute(TagName.__table__.insert(), [dict(tag_name_id=i,tag_id=t,name=n,ord=o) for i,t,n,o in
            [(1,1,'first',0),(2,1,'first_alias',1),(3,2,'second',0),(4,3,'creator',0)]])
        db.execute(TagLocalization.__table__.insert(), [dict(tag_id=i,locale='ja',name='共通名') for i in (1,2)])
    statements=[]
    event.listen(engine,'before_cursor_execute',lambda conn,cursor,sql,parameters,context,executemany:statements.append(sql))
    with Session(engine) as session:
        async def scenario():
            db=AsyncReads(session)
            common=dict(offset=0,sort='usage',localize=True,db=db,thumbnails=False)
            page=await search_tags(query='共通名',category='',limit=60,**common)
            assert page.total==2 and [r['id'] for r in page.results]==[1,2]
            assert page.results[0]['names']==['first','first_alias']
            assert all(r['thumbnailUrl'] is None and r['description']=='retained' for r in page.results)
            page=await search_tags(query='',category='artist',limit=40,**common)
            assert page.total==1 and page.results[0]['id']==3
            page=await search_tags(query='',category='',limit=60,**common)
            assert page.total==3 and [r['id'] for r in page.results]==[1,2,3]
            statements.clear()
            choices=await search_tags(query='first',category='',limit=10,offset=0,sort='usage',localize=False,db=db)
            assert len(choices)==1 and choices[0]['names']==['first','first_alias']
            assert 'localizedNames' not in choices[0]
            assert not any('count(' in sql.lower() for sql in statements)
        asyncio.run(scenario())
    assert not any('DISTINCT' in sql or 'FROM post_tag' in sql for sql in statements)
    engine.dispose()


def test_cleanup_filters_apply_before_pagination_and_count_real_aliases():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine, tables=[m.__table__ for m in (Tag, TagName, TagCategory, TagLocalization)])
    with engine.begin() as db:
        db.execute(TagCategory.__table__.insert(),dict(id=1,name='general',color='#aaa',default=True,order=0))
        db.execute(Tag.__table__.insert(),[dict(id=i,category_id=1,creation_time=datetime(2026,1,1),post_count=0 if i>5 else 10) for i in range(1,76)])
        names=[]
        for i in range(1,76):
            names.append(dict(tag_name_id=i*2,tag_id=i,name=f'tag/{i}',ord=10))
            if i%2: names.append(dict(tag_name_id=i*2+1,tag_id=i,name=f'alias_{i}',ord=20))
        db.execute(TagName.__table__.insert(),names)
    with Session(engine) as session:
        async def scenario():
            args=dict(localize=False,query='',category='',sort='usage',limit=60,thumbnails=False,db=AsyncReads(session))
            page=await search_tags(offset=0,cleanup='unused',**args)
            assert page.total==70 and len(page.results)==60
            assert all(r['usages']==0 for r in page.results)
            tail=await search_tags(offset=60,cleanup='unused',**args)
            assert tail.total==70 and len(tail.results)==10
            assert not ({r['id'] for r in page.results}&{r['id'] for r in tail.results})
            page=await search_tags(offset=0,cleanup='no_alias',**args)
            assert page.total==37 and all(len(r['names'])==1 for r in page.results)
            exact=await search_tags(offset=0,tag_id=2,**args)
            assert exact.total==1 and exact.results[0]['names']==['tag/2']
            # Filtering remains compatible with category and alias-name searches.
            args.update(query='alias_7',category='general')
            page=await search_tags(offset=0,cleanup='unused',**args)
            assert page.total==4
            page=await search_tags(offset=0,cleanup='no_alias',**args)
            assert page.total==0
        asyncio.run(scenario())
    engine.dispose()
