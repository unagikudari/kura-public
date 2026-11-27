import asyncio
import base64
import datetime
import json
import uuid
import pytest
from sqlalchemy import text
from api.services import measurement_collection as c
from api.services import measurement_evidence as service
from api.tagger_core.qualification_evidence import QualificationEvidenceRefused
from test_measurement_evidence_live import pg, migration_tree, db_url, setup, original_state
from test_measurement_evidence import measurement, runtime


def test_cursor_is_canonical_filter_bound_and_closed():
    at = datetime.datetime(2026,11,17,tzinfo=datetime.timezone.utc)
    token = c.cursor_for(at, 'a'*64, 3)
    assert c.parse_cursor(token, 3) == (at, 'a'*64)
    for value, tag in [(token, None), (token+'=',3), ('!',3), ('e30',3), ('a'*513,3)]:
        with pytest.raises(QualificationEvidenceRefused, match='measurement_cursor_invalid'):
            c.parse_cursor(value, tag)


def test_collection_pg_auth_pagination_history_and_no_mutation(db_url, monkeypatch):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from httpx import AsyncClient, ASGITransport
    from api import access
    from api.routers import tagger_measurements as routes
    from api.db.models import User, UserToken
    from api.errors import KuraError
    async def go():
        engine, sessions = await setup(db_url)
        app = FastAPI(); app.include_router(routes.router); app.add_middleware(access.CommandAuthMiddleware)
        monkeypatch.setattr(access, 'async_session', sessions)
        @app.exception_handler(KuraError)
        async def error(request, exc):
            return JSONResponse(status_code=exc.status, content={'detail':exc.body()})
        async def database():
            async with sessions() as db:
                yield db
        app.dependency_overrides[routes.get_db] = database
        token = str(uuid.uuid4())
        try:
            async with sessions() as db:
                u=User(name='discovery-test',rank='regular',creation_time=datetime.datetime(2026,11,17))
                db.add(u);await db.flush()
                db.add(UserToken(user_id=u.user_id,token=token,creation_time=datetime.datetime(2026,11,17)))
                await db.commit()
                before=await original_state(db)
            headers={'Authorization':'Token '+base64.b64encode(('discovery-test:'+token).encode()).decode()}
            async with AsyncClient(transport=ASGITransport(app=app),base_url='http://fixture') as client:
                path='/api/tagger/measurements'
                denied=await client.get(path)
                assert denied.status_code==401
                assert denied.headers['cache-control']=='private, no-store'
                for wrong_headers in [
                    {'Authorization':'Token '+base64.b64encode(b'discovery-test:wrong').decode()},
                    {'Authorization':'Token '+base64.b64encode(('other:'+token).encode()).decode()},
                    {'X-API-Key':'not-a-user-token'}]:
                    bad_auth=await client.get(path,headers=wrong_headers)
                    assert bad_auth.status_code==401 and bad_auth.headers['cache-control']=='private, no-store'
                empty=await client.get(path,headers=headers)
                assert empty.headers['cache-control']=='private, no-store'
                assert empty.json()['state']=='available' and empty.json()['items']==[]
                async with sessions() as db:
                    refs=[]
                    # All inserts in one transaction deliberately tie received_at.
                    for i in range(4):
                        value=measurement(); value['source']['snapshot_sha256']=format(i+1,'064x')
                        result=await service.submit(db,tag_id=3,tag='fixture',measurement=value,user_id=u.user_id)
                        refs.append(result['measurement_sha256'])
                    await service.attach(db,measurement_sha256=refs[0],expected_measurement_sha256=refs[0],
                                         kind='runtime',report=runtime(),user_id=u.user_id)
                    await db.commit()
                    frozen=(await db.execute(text('SELECT measurement_sha256,canonical_body FROM tagger_measurement_anchor ORDER BY 1'))).all()
                    await db.execute(text("UPDATE tag_name SET name='renamed-fixture' WHERE tag_id=3"));await db.commit()
                seen=[];cursor=None
                while True:
                    params={'tag_id':'3','limit':'2'}
                    if cursor:params['cursor']=cursor
                    response=await client.get(path,params=params,headers=headers)
                    assert response.status_code==200,response.text
                    assert response.headers['cache-control']=='private, no-store'
                    body=response.json();assert body['state']=='available'
                    for row in body['items']:
                        assert row['tag']=='fixture' and row['admission']=='not_granted' and row['quality_granted'] is False
                        assert row['provenance']=='caller_assertion' and 'body' not in row
                        assert row['counts_partial'] is False and row['stored_count_scope']=='first_100'
                        from api.tagger_core.qualification_evidence import admission_status
                        assert row['admission']==admission_status()['admission']
                        assert row['evidence_counts']['runtime']==(1 if row['measurement_sha256']==refs[0] else 0)
                        seen.append(row['measurement_sha256'])
                    cursor=body['next_cursor']
                    if cursor is None:break
                assert seen==sorted(refs,reverse=True)
                assert (await client.get(path+'?tag_id=999',headers=headers)).json()['items']==[]
                for query in ['limit=0','limit=51','limit=1.0','tag_id=true','tag_id=9223372036854775808',
                              'limit=2&limit=3','worker_id=fake','cursor=', 'cursor=invalid']:
                    bad=await client.get(path+'?'+query,headers=headers)
                    assert bad.status_code==422,(query,bad.text)
                    assert bad.headers['cache-control']=='private, no-store'
                token_missing=c.cursor_for(datetime.datetime(2026,11,17,tzinfo=datetime.timezone.utc),'f'*64,None)
                missing=await client.get(path,params={'cursor':token_missing},headers=headers)
                assert missing.status_code==404 and missing.headers['cache-control']=='private, no-store'
                monkeypatch.setenv('KURA_DB_ROLE','replica')
                unavailable=await client.get(path,headers=headers)
                assert unavailable.status_code==503 and 'items' not in unavailable.json()
                assert unavailable.headers['cache-control']=='private, no-store'
            async with sessions() as db:
                assert (await db.execute(text('SELECT measurement_sha256,canonical_body FROM tagger_measurement_anchor ORDER BY 1'))).all()==frozen
                assert await original_state(db)==before
        finally:await engine.dispose()
    asyncio.run(go())


def test_collection_restores_timeouts_and_missing_store_is_not_empty(db_url):
    from api.routers import tagger_measurements as routes
    from api.errors import KuraError
    from types import SimpleNamespace
    from starlette.requests import Request
    from fastapi import Response
    async def go():
        engine,sessions=await setup(db_url)
        try:
            async with sessions() as db:
                await db.execute(text("SET LOCAL statement_timeout='31s'"))
                await db.execute(text("SET LOCAL lock_timeout='7s'"))
                assert (await c.collection(db))['items']==[]
                assert (await db.execute(text('SHOW statement_timeout'))).scalar_one()=='31s'
                assert (await db.execute(text('SHOW lock_timeout'))).scalar_one()=='7s'
                await db.execute(text('ALTER TABLE tagger_measurement_anchor RENAME TO hidden_anchor'))
                with pytest.raises(KuraError) as unavailable:
                    await routes.discover(Request({'type':'http','query_string':b''}),Response(),
                                          None,'20',None,db,SimpleNamespace(user_id=1))
                assert unavailable.value.status==503 and unavailable.value.error=='measurement_collection_unavailable'
                assert (await db.execute(text('SELECT 1'))).scalar_one()==1
                assert (await db.execute(text('SHOW statement_timeout'))).scalar_one()=='31s'
                assert (await db.execute(text('SHOW lock_timeout'))).scalar_one()=='7s'
                await db.rollback()
        finally:await engine.dispose()
    asyncio.run(go())


def test_collection_lock_contention_is_bounded_and_named(db_url):
    import time
    from types import SimpleNamespace
    from starlette.requests import Request
    from fastapi import Response
    from api.routers import tagger_measurements as routes
    from api.errors import KuraError
    async def go():
        engine,sessions=await setup(db_url)
        try:
            async with sessions() as locker, sessions() as reader:
                await reader.execute(text("SET LOCAL statement_timeout='31s'"))
                await reader.execute(text("SET LOCAL lock_timeout='7s'"))
                await locker.execute(text('LOCK TABLE tagger_measurement_anchor IN ACCESS EXCLUSIVE MODE'))
                start=time.monotonic()
                request=Request({'type':'http','query_string':b''})
                with pytest.raises(KuraError) as caught:
                    await routes.discover(request,Response(),None,'20',None,reader,SimpleNamespace(user_id=1))
                assert caught.value.status==503 and caught.value.error=='measurement_database_busy'
                assert time.monotonic()-start < 6
                assert (await reader.execute(text('SELECT 1'))).scalar_one()==1
                assert (await reader.execute(text('SHOW statement_timeout'))).scalar_one()=='31s'
                assert (await reader.execute(text('SHOW lock_timeout'))).scalar_one()=='7s'
                await locker.rollback()
                assert (await c.collection(reader))['items']==[]
        finally:await engine.dispose()
    asyncio.run(go())


@pytest.mark.parametrize('number',[1e16,-0.0])
def test_collection_never_recanonicalizes_original_numbers(db_url,number):
    from api.tagger_core import qualification_evidence as q
    async def go():
        engine,sessions=await setup(db_url)
        try:
            value=measurement()
            value['feature_contract']['preprocessing']['declared_numeric_metadata']=number
            value['feature_contract']['preprocessing_sha256']=q.digest(value['feature_contract']['preprocessing'])
            async with sessions() as db:
                result=await service.submit(db,tag_id=3,tag='fixture',measurement=value,user_id=1)
                await db.commit()
                before=await service.anchor(db,result['measurement_sha256'])
                assert len((await c.collection(db))['items'])==1
                after=await service.anchor(db,result['measurement_sha256'])
                assert before['canonical_body']==after['canonical_body']
                assert q.canonical(after['body'])==after['canonical_body']
        finally:await engine.dispose()
    asyncio.run(go())


def test_evidence_count_is_explicitly_bounded_per_anchor(db_url):
    async def go():
        engine,sessions=await setup(db_url)
        try:
            async with sessions() as db:
                first=await service.submit(db,tag_id=3,tag='fixture',measurement=measurement(),user_id=1)
                for i in range(102):
                    report=runtime();report['profile']=f'profile-{i}'
                    await service.attach(db,first['measurement_sha256'],expected_measurement_sha256=first['measurement_sha256'],
                                         kind='runtime',report=report,user_id=1)
                await db.commit()
                item=(await c.collection(db))['items'][0]
                assert item['counts_partial'] is True
                assert item['stored_count_scope']=='first_100'
                assert item['evidence_counts']==dict(runtime=100,human_sample=0,independent_review=0)
        finally:await engine.dispose()
    asyncio.run(go())


def test_statement_timeout_is_named_budget_not_retry_and_restores_settings(db_url,monkeypatch):
    from types import SimpleNamespace
    from starlette.requests import Request
    from fastapi import Response
    from api.routers import tagger_measurements as routes
    from api.errors import KuraError
    async def force_real_timeout(db):
        await db.execute(text("SET LOCAL statement_timeout='2ms'"))
        await db.execute(text('SELECT pg_sleep(0.1)'))
    async def go():
        engine,sessions=await setup(db_url)
        try:
            async with sessions() as db:
                await db.execute(text("SET LOCAL statement_timeout='31s'"))
                await db.execute(text("SET LOCAL lock_timeout='7s'"))
                monkeypatch.setattr(c,'limits',force_real_timeout)
                with pytest.raises(KuraError) as caught:
                    await routes.discover(Request({'type':'http','query_string':b''}),Response(),None,'20',None,db,SimpleNamespace(user_id=1))
                assert caught.value.error=='measurement_collection_read_budget_exceeded'
                assert 'do not retry automatically' in caught.value.fix
                assert (await db.execute(text('SHOW statement_timeout'))).scalar_one()=='31s'
                assert (await db.execute(text('SHOW lock_timeout'))).scalar_one()=='7s'
        finally:await engine.dispose()
    asyncio.run(go())


def test_distinct_time_pages_new_insert_timezone_and_thresholds(db_url):
    async def go():
        engine,sessions=await setup(db_url)
        try:
            async with sessions() as db:
                refs=[]
                for i,threshold in enumerate([1e-5,.30000000000000004,.5]):
                    value=measurement();value['threshold']=threshold
                    result=await service.submit(db,tag_id=3,tag='fixture',measurement=value,user_id=1)
                    refs.append(result['measurement_sha256'])
                    await db.commit()
                    await asyncio.sleep(.003)
                await db.execute(text("SET LOCAL TIME ZONE 'Asia/Tokyo'"))
                page=await c.collection(db,limit=1,tag_id=3)
                assert page['items'][0]['measurement_sha256']==refs[2]
                assert page['observed_at']>=page['items'][0]['received_at']
                assert page['items'][0]['received_at'].endswith('+00:00')
                first_cursor=page['next_cursor']
                async with sessions() as writer:
                    value=measurement();value['threshold']=.75
                    await service.submit(writer,tag_id=3,tag='fixture',measurement=value,user_id=1)
                    await writer.commit()
                page2=await c.collection(db,limit=50,tag_id=3,cursor=first_cursor)
                assert [r['measurement_sha256'] for r in page2['items']]==list(reversed(refs[:2]))
                assert page2['next_cursor'] is None
                for row in page2['items']:
                    original=await service.anchor(db,row['measurement_sha256'])
                    assert row['threshold']==json.loads(original['canonical_body'])['threshold']
                assert (await c.collection(db,limit=4))['next_cursor'] is None
                await db.execute(text("SET LOCAL statement_timeout='31s'"))
                with pytest.raises(QualificationEvidenceRefused,match='measurement_cursor_missing'):
                    await c.collection(db,cursor=c.cursor_for(datetime.datetime(2026,11,17,tzinfo=datetime.timezone.utc),'f'*64,None))
                assert (await db.execute(text('SHOW statement_timeout'))).scalar_one()=='31s'
        finally:await engine.dispose()
    asyncio.run(go())
