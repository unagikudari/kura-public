"""Actual MCP JSON-RPC transport + real tokens + disposable PostgreSQL.

The UI router and MCP use the same DB/services. No production credentials,
network fetching, host-path tools or optional extensions are involved.
"""
import asyncio
import base64
from datetime import datetime,timedelta
from io import BytesIO
import json
from uuid import uuid4
import pytest
from fastapi import FastAPI
from httpx import AsyncClient,ASGITransport
from sqlalchemy import text
from PIL import Image
from test_collector_next import database,postgres,run  # noqa:F401
from api import mcp_auth as A
from api.collector import mcp as M,router as R
from api.db.models import User,UserToken


def auth(name):return 'Token '+base64.b64encode(f'{name}:{name}-token'.encode()).decode()


async def setup(sessions,monkeypatch,tmp_path):
    engine=sessions.kw['bind']
    async with engine.begin() as db:
        await db.run_sync(lambda conn:User.__table__.create(conn,checkfirst=True))
        await db.run_sync(lambda conn:UserToken.__table__.create(conn,checkfirst=True))
        await db.execute(text('TRUNCATE user_token,"user" RESTART IDENTITY CASCADE'))
        for id,name in [(1,'alice'),(2,'bob')]:
            await db.execute(User.__table__.insert().values(id=id,name=name,creation_time=datetime.now(),rank='regular'))
            await db.execute(UserToken.__table__.insert().values(user_id=id,token=name+'-token',enabled=True,creation_time=datetime.now()))
    monkeypatch.setenv('KURA_COLLECTOR_NEXT','1');monkeypatch.setenv('KURA_DB_ROLE','canonical')
    monkeypatch.setattr(A,'async_session',sessions);monkeypatch.setattr(M,'async_session',sessions);monkeypatch.setattr(R,'async_session',sessions)
    monkeypatch.setattr(R,'root',lambda:tmp_path/'media')
    monkeypatch.setattr(R,'_slots',asyncio.Semaphore(2))
    server=M.create_server();sub=server.streamable_http_app()
    app=FastAPI();app.include_router(R.router)
    app.mount('/api/collector-next/mcp',A.TokenAuthMiddleware(M.CollectorTransport(sub)))
    async def db_dependency():
        async with sessions() as db:yield db
    app.dependency_overrides[R.get_db]=db_dependency
    return app,server


async def rpc(client,method,arguments=None,*,who='alice',headers=None):
    body={'jsonrpc':'2.0','id':str(uuid4()),'method':method}
    if arguments is not None:body['params']=arguments
    return await client.post('/api/collector-next/mcp/',json=body,headers={
        'Authorization':auth(who),'accept':'application/json,text/event-stream','mcp-protocol-version':'2025-11-25',**(headers or {})})


async def call(client,name,args=None,*,who='alice',error=False):
    response=await rpc(client,'tools/call',{'name':name,'arguments':args or {}},who=who)
    assert response.status_code==200,response.text
    result=response.json()['result']
    if error:
        assert result.get('isError'),result;return result
    assert not result.get('isError'),result
    return result.get('structuredContent') or json.loads(result['content'][0]['text'])


def test_mcp_roundtrip_ui_import_labels_replay_and_agent_provenance(database,tmp_path,monkeypatch):
    async def scenario(sessions):
        app,server=await setup(sessions,monkeypatch,tmp_path)
        async with server.session_manager.run(),AsyncClient(transport=ASGITransport(app=app),base_url='http://127.0.0.1') as client:
            initialized=await rpc(client,'initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'fixture','version':'1'}})
            assert initialized.status_code==200 and initialized.json()['result']['serverInfo']['name']=='kura-collector'
            tools=(await rpc(client,'tools/list')).json()['result']['tools']
            by_name={t['name']:t for t in tools};assert len(tools)==13
            assert by_name['collector_search']['annotations']['readOnlyHint']
            assert by_name['collector_change_labels']['annotations']['idempotentHint']
            assert by_name['collector_change_labels']['annotations']['destructiveHint']
            assert 'owner' not in json.dumps([t['inputSchema'] for t in tools])
            key=str(uuid4());command={'title':'MCPからの作品','request_key':key}
            copies=await asyncio.gather(call(client,'collector_create_collection',{'command':command}),call(client,'collector_create_collection',{'command':command}))
            collection=copies[0];assert copies[1]==collection
            data=BytesIO();Image.new('RGB',(32,32),'red').save(data,'PNG');raw=data.getvalue()
            import hashlib
            op=str(uuid4());keys=[str(uuid4()),str(uuid4())]
            planned=await call(client,'collector_plan_import',{'command':{'request_key':op,'collection':collection['id'],'items':[
                {'request_key':k,'original_name':f'{i}.png','relative_path':f'book/{i}.png','sha256':hashlib.sha256(raw).hexdigest(),'byte_size':len(raw)} for i,k in enumerate(keys)]}})
            assert planned['state']=='awaiting_files' and planned['remaining_count']==2
            ui=await client.get('/api/collector-next/imports/'+op,headers={'Authorization':auth('alice')})
            assert ui.json()['request_key']==op and ui.json()['remaining_count']==2
            for i,k in enumerate(keys):
                response=await client.post('/api/collector-next/upload',headers={'Authorization':auth('alice')},
                    data={'request_key':k,'operation_key':op,'collection':str(collection['id']),'relative_path':f'book/{i}.png'},files={'file':(f'{i}.png',raw,'image/png')})
                assert response.status_code==200,response.text
            status=await call(client,'collector_import_status',{'request_key':op})
            assert status['state']=='completed' and status['registered_count']==2
            assert (await call(client,'collector_imports'))['items'][0]['request_key']==op
            assert len((await call(client,'collector_import_items',{'request_key':op}))['items'])==2
            assert 'not_found' in str(await call(client,'collector_import_status',{'request_key':op},who='bob',error=True))
            assert (await call(client,'collector_search'))['items']==[]  # safe default
            entries=(await call(client,'collector_entries',{'collection':collection['id'],'safe_mode':False}))['items']
            assert len(entries)==2 and entries[0]['id']==entries[1]['id'] and entries[0]['entry_id']!=entries[1]['entry_id']
            asset=entries[0]['id'];label_key=str(uuid4())
            command={'request_key':label_key,'targets':[{'asset_id':asset,'revision':0}],'add':['Agent 整理']}
            changes=await asyncio.gather(call(client,'collector_change_labels',{'command':command}),call(client,'collector_change_labels',{'command':command}))
            assert sum(bool(r.get('replayed')) for r in changes)==1
            ui_replay=await client.post('/api/collector-next/labels/change',json=command,headers={'Authorization':auth('alice')})
            assert ui_replay.status_code==200 and ui_replay.json()['replayed']
            page=(await client.get('/api/collector-next/media?tags=Agent%20整理',headers={'Authorization':auth('alice')})).json()
            assert page['items'][0]['id']==asset and page['items'][0]['tag_revision']==1
            catalog=await call(client,'collector_tags',{'safe_mode':False})
            assert catalog['items'][0]['count']==1 and catalog['items'][0]['logical_bytes']==len(raw)
            http_tags=await client.get('/api/collector-next/tags',headers={'Authorization':auth('alice')})
            assert http_tags.json()==catalog
            assert (await call(client,'collector_tags',who='bob'))['items']==[]
            assert (await call(client,'collector_tags'))['items'][0]['thumbnail_url'] is None
            history=await call(client,'collector_label_history',{'asset':asset,'safe_mode':False})
            assert len(history['items'])==1 and history['items'][0]['producer_version']=='collector-label-mcp-v1'
            assert 'not_found' in str(await call(client,'collector_label_history',{'asset':asset},error=True))
            assert 'not_found' in str(await call(client,'collector_label_operation',{'request_key':label_key},who='bob',error=True))
            other=await client.post('/api/collector-next/upload',headers={'Authorization':auth('bob')},
                data={'request_key':str(uuid4())},files={'file':('bob.png',raw,'image/png')})
            assert other.status_code==200
            forbidden={'request_key':str(uuid4()),'targets':[{'asset_id':other.json()['asset_id'],'revision':0}],'add':['unauthorized']}
            assert 'not_found' in str(await call(client,'collector_change_labels',{'command':forbidden},error=True))
            stale={'request_key':str(uuid4()),'targets':[{'asset_id':asset,'revision':0}],'remove':['Agent 整理']}
            failed=await call(client,'collector_change_labels',{'command':stale},error=True)
            assert failed['structuredContent']['error']=='conflict'
            assert (await call(client,'collector_label_operation',{'request_key':label_key}))['targets'][0]['revision']==1
    run(database,scenario)


def test_mcp_real_auth_owner_isolation_revocation_and_limits(database,tmp_path,monkeypatch):
    async def scenario(sessions):
        app,server=await setup(sessions,monkeypatch,tmp_path)
        async with server.session_manager.run(),AsyncClient(transport=ASGITransport(app=app),base_url='http://127.0.0.1') as client:
            assert (await client.post('/api/collector-next/mcp/',json={})).status_code==401
            assert (await rpc(client,'tools/list',headers={'Authorization':auth('unknown')})).status_code==401
            for who in ('alice','bob'):
                await call(client,'collector_create_collection',{'command':{'title':who,'request_key':str(uuid4())}},who=who)
            pages=await asyncio.gather(*(call(client,'collector_collections',who=who) for who in ('alice','bob')))
            assert [[c['title'] for c in p['items']] for p in pages]==[['alice'],['bob']]
            bob_id=pages[1]['items'][0]['id']
            assert 'not_found' in str(await call(client,'collector_entries',{'collection':bob_id,'safe_mode':False},error=True))
            assert (await rpc(client,'tools/list',headers={'Host':'evil.invalid'})).status_code==421
            assert (await rpc(client,'tools/list',headers={'Origin':'https://evil.invalid'})).status_code==403
            monkeypatch.setattr(M.CollectorTransport,'MAX_BYTES',16)
            assert (await rpc(client,'tools/list')).status_code==413
            async def chunked():yield b'123456789';yield b'123456789'
            response=await client.post('/api/collector-next/mcp/',content=chunked(),headers={'Authorization':auth('alice'),'content-type':'application/json','accept':'application/json,text/event-stream'})
            assert response.status_code==413
            monkeypatch.setattr(M.CollectorTransport,'MAX_BYTES',4*1024**2)
            monkeypatch.setenv('KURA_DB_ROLE','replica')
            assert (await rpc(client,'tools/list')).status_code==503
            monkeypatch.setenv('KURA_DB_ROLE','canonical')
            async with sessions.begin() as db:await db.execute(text("UPDATE user_token SET enabled=false WHERE user_id=1"))
            assert (await rpc(client,'tools/list')).status_code==401
            assert (await rpc(client,'tools/list',who='bob')).status_code==200
            too_many=await call(client,'collector_collections',{'limit':101},who='bob',error=True)
            assert too_many['isError']
            async with sessions.begin() as db:
                await db.execute(text("UPDATE user_token SET expiration_time=now()-interval '1 day' WHERE user_id=2"))
            assert (await rpc(client,'tools/list',who='bob')).status_code==401
    run(database,scenario)


def test_old_operational_toolset_remains_read_only_and_auth_failures_are_sanitized(monkeypatch):
    from api.routers import mcp_server as old
    async def scenario():
        names={tool.name for tool in await old.mcp.list_tools()}
        assert names=={'tagger_lifecycle','tagger_fleet','tagger_releases','kura_whats_happening'}
        async def broken(scope):raise RuntimeError('private token and database path')
        monkeypatch.setattr(A,'identity',broken)
        app=A.TokenAuthMiddleware(FastAPI())
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://127.0.0.1') as client:
            result=await client.post('/')
            assert result.status_code==503 and 'private token' not in result.text
    asyncio.run(scenario())


def test_official_sdk_client_initializes_lists_and_calls_stateless_server(database,tmp_path,monkeypatch):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    async def scenario(sessions):
        app,server=await setup(sessions,monkeypatch,tmp_path)
        async with server.session_manager.run(),AsyncClient(transport=ASGITransport(app=app),headers={'Authorization':auth('alice')}) as http:
            async with streamable_http_client('http://127.0.0.1/api/collector-next/mcp/',http_client=http) as (reader,writer,session_id):
                async with ClientSession(reader,writer) as client:
                    initialized=await client.initialize()
                    assert initialized.serverInfo.name=='kura-collector'
                    assert len((await client.list_tools()).tools)==13
                    result=await client.call_tool('collector_status',{})
                    assert not result.isError and result.structuredContent['safe_default']
                    assert session_id() is None
    run(database,scenario)


def test_agent_provenance_survives_exchange_restore(database,tmp_path):
    from test_collector_next import image,reset_exchange_destination,exchange_semantics
    from api.collector import service,label_service,export,restore
    stored,_=image(tmp_path/'source');saved={}
    async def source(sessions):
        async with sessions.begin() as db:
            collection=await service.create_collection(db,1,'Agent archive')
            item=await service.register(db,1,uuid4(),stored,'page.png',collection['id'])
            await label_service.apply(db,1,uuid4(),[(item['asset_id'],0)],['整理'],[],origin='mcp')
        async with sessions.begin() as db:
            saved['manifest']=await export.snapshot(db,1,collection['id'])
    run(database,source)
    archive,_=export.build_archive(tmp_path/'source',saved['manifest'])
    with archive:saved['bytes']=archive.read()
    reset_exchange_destination(database)
    async def target(sessions):
        async with sessions.begin() as db:
            result=await restore.restore(db,tmp_path/'target',BytesIO(saved['bytes']),2)
        async with sessions.begin() as db:
            manifest=await export.snapshot(db,2,result['collection_id'])
            assert exchange_semantics(manifest)==exchange_semantics(saved['manifest'])
            assert manifest['evidence'][0]['producer']['version']=='collector-label-mcp-v1'
    run(database,target)


@pytest.mark.parametrize('origin',['https://user:secret@example.test','https://example.test/path','ftp://example.test','https://example.test:bad'])
def test_origin_configuration_rejects_non_origins(monkeypatch,origin):
    monkeypatch.setenv('KURA_COLLECTOR_MCP_ORIGINS',origin)
    with pytest.raises(ValueError):M.security()
