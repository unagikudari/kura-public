"""Real DB/ZIP HTTP round trip and bounded streaming; never production writes."""
import asyncio
from io import BytesIO
from types import SimpleNamespace
from pathlib import Path
import threading
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from api.errors import KuraError
from httpx import AsyncClient,ASGITransport
from test_collector_next import database,postgres,run,exchange_fixture,reset_exchange_destination,exchange_semantics  # noqa:F401
from api.collector import router as R,exchange_upload as U,export


def application():
    app=FastAPI();app.include_router(R.router)
    @app.exception_handler(KuraError)
    async def errors(request,exc):return JSONResponse({'detail':exc.body()},status_code=exc.status)
    return app


def test_http_restore_preserves_export_semantics_and_rejects_nonempty(database,tmp_path,monkeypatch):
    saved=exchange_fixture(database,tmp_path/'source')
    reset_exchange_destination(database)
    async def scenario(sessions):
        app=application()
        class AuthDB:
            async def rollback(self):pass
        app.dependency_overrides[R.get_db]=lambda:AuthDB()
        app.dependency_overrides[R.get_current_user]=lambda:SimpleNamespace(user_id=7)
        monkeypatch.setenv('KURA_COLLECTOR_NEXT','1')
        monkeypatch.setattr(R,'async_session',sessions);monkeypatch.setattr(R,'root',lambda:tmp_path/'target')
        monkeypatch.setattr(R,'_restore_slots',asyncio.Semaphore(1))
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
            result=await client.post('/api/collector-next/exchange/restore',content=saved['bytes'],headers={'content-type':'application/zip'})
            assert result.status_code==200,result.text
            body=result.json();assert body['owner']==7 and body['entries']==3
            async with sessions.begin() as db:
                manifest=await export.snapshot(db,7,body['collection_id'])
            assert exchange_semantics(manifest)==exchange_semantics(saved['manifest'])
            repeat=await client.post('/api/collector-next/exchange/restore',content=saved['bytes'],headers={'content-type':'application/zip'})
            assert repeat.status_code==409 and 'exchange_target_not_empty' in repeat.text
            assert R._restore_slots._value==1
    run(database,scenario)


def test_http_invalid_input_auth_and_size_limits(database,tmp_path,monkeypatch):
    async def scenario(sessions):
        app=application()
        class DB:
            async def rollback(self):pass
        app.dependency_overrides[R.get_db]=lambda:DB()
        monkeypatch.setenv('KURA_COLLECTOR_NEXT','1')
        monkeypatch.setattr(R,'async_session',sessions);monkeypatch.setattr(R,'root',lambda:tmp_path)
        monkeypatch.setattr(R,'_restore_slots',asyncio.Semaphore(1))
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
            assert (await client.post('/api/collector-next/exchange/restore',content=b'zip')).status_code==401
            app.dependency_overrides[R.get_current_user]=lambda:SimpleNamespace(user_id=7)
            assert (await client.post('/api/collector-next/exchange/restore',content=b'zip')).status_code==415
            bad=await client.post('/api/collector-next/exchange/restore',content=b'not a zip',headers={'content-type':'application/zip'})
            assert bad.status_code==422 and str(tmp_path) not in bad.text
            monkeypatch.setattr(U,'MAX_ARCHIVE_BYTES',4)
            assert (await client.post('/api/collector-next/exchange/restore',content=b'12345',headers={'content-type':'application/zip'})).status_code==413
            assert R._restore_slots._value==1
    run(database,scenario)


def test_chunked_upload_enforces_actual_length_and_closes_spool(tmp_path,monkeypatch):
    monkeypatch.setattr(U,'MAX_ARCHIVE_BYTES',4)
    opened=[];actual=U.tempfile.TemporaryFile
    def temporary(**kw):
        result=actual(**kw);opened.append(result);return result
    monkeypatch.setattr(U.tempfile,'TemporaryFile',temporary)
    class Request:
        headers={}
        async def stream(self):
            yield b'123';yield b'45'
    async def scenario():
        with pytest.raises(U.UploadLimit):await U.receive(Request(),tmp_path)
        assert opened[0].closed
    asyncio.run(scenario())


def test_disconnect_during_restore_keeps_input_and_slot_until_transaction_finishes(tmp_path,monkeypatch):
    entered=threading.Event();release=threading.Event();archive=BytesIO(b'zip');closed=[]
    class DB:
        async def rollback(self):pass
    class Context:
        async def __aenter__(self):return DB()
        async def __aexit__(self,*args):pass
    class Sessions:
        def begin(self):return Context()
    async def check(*a,**kw):pass
    async def receive(*a):return archive
    async def execute(*a):
        def worker():entered.set();assert release.wait(3);assert not archive.closed
        await asyncio.to_thread(worker);closed.append('finished');return {}
    monkeypatch.setattr(R.collection_restore,'check_destination',check)
    monkeypatch.setattr(R.exchange_upload,'receive',receive);monkeypatch.setattr(R.exchange_upload,'execute',execute)
    monkeypatch.setattr(R,'async_session',Sessions());monkeypatch.setattr(R,'root',lambda:tmp_path)
    async def scenario():
        monkeypatch.setattr(R,'_restore_slots',asyncio.Semaphore(1))
        task=asyncio.create_task(R.restore_exchange(SimpleNamespace(headers={'content-type':'application/zip'}),SimpleNamespace(user_id=7),DB()))
        assert await asyncio.to_thread(entered.wait,2)
        task.cancel();await asyncio.sleep(.01)
        assert R._restore_slots.locked() and not archive.closed and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert closed==['finished'] and archive.closed and R._restore_slots._value==1
    try:asyncio.run(scenario())
    finally:release.set()


def test_disconnect_during_spooling_waits_for_writer_before_close(tmp_path,monkeypatch):
    entered=threading.Event();release=threading.Event()
    class Spool(BytesIO):
        def write(self,data):
            entered.set();assert release.wait(3);assert not self.closed
            return super().write(data)
    archive=Spool();monkeypatch.setattr(U.tempfile,'TemporaryFile',lambda **kw:archive)
    class Request:
        headers={}
        async def stream(self):yield b'zip'
    async def scenario():
        task=asyncio.create_task(U.receive(Request(),tmp_path))
        assert await asyncio.to_thread(entered.wait,2)
        task.cancel();await asyncio.sleep(.01)
        assert not archive.closed and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert archive.closed
    try:asyncio.run(scenario())
    finally:release.set()
