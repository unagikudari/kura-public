"""Exercise real multipart parsing with small synthetic limits, never production."""
import asyncio
import hashlib
import io
import tempfile

import pytest
from fastapi import FastAPI, UploadFile, HTTPException
from fastapi.testclient import TestClient
from api.services import upload_input as limits


@pytest.mark.parametrize('disk', [False,True])
def test_mapping_preserves_bytes_without_full_read_and_closes(disk, monkeypatch):
    raw=b'\x1aE\xdf\xa3'+b'x'*10000
    stream=tempfile.TemporaryFile() if disk else io.BytesIO()
    stream.write(raw);stream.seek(0)
    file=UploadFile(stream,filename='video.webm')
    async def check():
        async with limits.mapped_upload(file) as data:
            assert hashlib.sha256(data).digest()==hashlib.sha256(raw).digest()
            assert not isinstance(data,bytes)
        if disk:assert data.closed
        else:
            with pytest.raises(ValueError):len(data)
    try:asyncio.run(check())
    finally:stream.close()


def test_image_limit_checked_before_mapping(monkeypatch):
    monkeypatch.setattr(limits,'MAX_IMAGE_BYTES',8)
    async def check():
        file=UploadFile(io.BytesIO(b'\x89PNG\r\n\x1a\nxxxx'),filename='image.png')
        with pytest.raises(HTTPException) as exc:
            async with limits.mapped_upload(file):pytest.fail('oversize accepted')
        assert exc.value.status_code==413
    asyncio.run(check())


def test_chunked_multipart_stops_before_handler(monkeypatch):
    monkeypatch.setattr(limits,'MAX_REQUEST_BYTES',256)
    app=FastAPI();app.add_middleware(limits.UploadBodyLimit)
    writes=[]
    @app.post('/api/upload')
    async def upload(file:UploadFile):writes.append(True);return {'ok':True}
    with TestClient(app) as client:
        assert client.post('/api/upload',files={'file':('small.bin',b'x')}).status_code==200
        writes.clear()
        def body():
            yield b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="large.bin"\r\n\r\n'
            for _ in range(8):yield b'x'*64
            yield b'\r\n--boundary--\r\n'
        response=client.post('/api/upload',content=body(),headers={'content-type':'multipart/form-data; boundary=boundary'})
        assert response.status_code==413
        assert writes==[]


def test_cancellation_keeps_mapping_alive_until_decoder_finishes(monkeypatch):
    from api.routers import upload
    async def scenario():
        started=asyncio.Event();finish=asyncio.Event();seen=[]
        async def delayed(**kwargs):
            data=kwargs['data'];seen.append(data);started.set()
            await finish.wait()
            assert bytes(data)==b'video bytes'
            return {'ok':True}
        monkeypatch.setattr(upload,'_upload_post',delayed)
        monkeypatch.setattr(limits,'_slots',asyncio.Semaphore(2))
        with tempfile.TemporaryFile() as stream:
            stream.write(b'video bytes');stream.seek(0)
            task=asyncio.create_task(upload.upload_post(file=UploadFile(stream,filename='video.webm')))
            await started.wait();task.cancel();await asyncio.sleep(0)
            assert not task.done() and not seen[0].closed
            finish.set()
            with pytest.raises(asyncio.CancelledError):await task
            assert seen[0].closed
    asyncio.run(scenario())


def test_repeated_cancellation_waits_for_real_thread_before_unmapping(monkeypatch):
    import threading
    from api.routers import upload
    async def scenario():
        started, release = threading.Event(), threading.Event()
        seen = []
        def decode(data):
            started.set()
            assert release.wait(3)
            assert bytes(data) == b'video bytes'
            return {'ok': True}
        async def delayed(**kwargs):
            data = kwargs['data']; seen.append(data)
            return await asyncio.to_thread(decode, data)
        monkeypatch.setattr(upload, '_upload_post', delayed)
        monkeypatch.setattr(limits, '_slots', asyncio.Semaphore(2))
        with tempfile.TemporaryFile() as stream:
            stream.write(b'video bytes'); stream.seek(0)
            task = asyncio.create_task(upload.upload_post(file=UploadFile(stream, filename='video.webm')))
            try:
                for _ in range(1000):
                    if started.is_set(): break
                    await asyncio.sleep(.001)
                assert started.is_set()
                for _ in range(3):
                    task.cancel(); await asyncio.sleep(0)
                    assert not task.done() and not seen[0].closed
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError): await task
            assert seen[0].closed
    asyncio.run(scenario())


def test_upload_hashing_yields_before_any_identity_query(monkeypatch):
    import threading
    from types import SimpleNamespace
    from api.routers import upload
    raw = b'\x1aE\xdf\xa3' + b'x' * 1000
    entered, finish = threading.Event(), threading.Event()
    identity = limits.content_identity
    def slow_identity(data):
        assert threading.current_thread() is not threading.main_thread()
        entered.set(); assert finish.wait(3)
        result = identity(data)
        assert result == (hashlib.md5(raw).hexdigest(), hashlib.sha1(raw).hexdigest(), hashlib.sha256(raw).digest())
        return result
    monkeypatch.setattr(limits, 'content_identity', slow_identity)
    class StopBeforeDB(Exception): pass
    async def duplicate(db, md5, sha256): raise StopBeforeDB
    monkeypatch.setattr(upload, '_find_upload_duplicate', duplicate)
    async def scenario():
        task = asyncio.create_task(upload._upload_post(
            file=SimpleNamespace(filename='video.webm'), data=memoryview(raw),
            tags='', source='', safety='unsafe', pool='', pool_source_url='',
            collection_url='', artist_tags='', db=None, user=None))
        try:
            for _ in range(1000):
                if entered.is_set(): break
                await asyncio.sleep(.001)
            assert entered.is_set() and not task.done()
        finally: finish.set()
        with pytest.raises(StopBeforeDB): await task
    asyncio.run(scenario())


def test_a_field_upload_does_not_take_is_refused_before_the_file_is_read(monkeypatch):
    from fastapi.responses import JSONResponse
    from api.auth import get_current_user
    from api.db.session import get_db
    from api.errors import KuraError
    from api.routers import upload
    app = FastAPI(); app.include_router(upload.router)
    app.add_exception_handler(KuraError, lambda request, e: JSONResponse(e.body(), status_code=e.status))
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: None
    received = []
    async def receive(file, handle):
        received.append(file.filename); return {'ok': True}
    monkeypatch.setattr(upload, 'receive', receive)
    with TestClient(app) as client:
        stale = client.post('/api/upload', files={'file': ('a.png', b'x')},
                            data={'tags': 'a', 'item_id': '7', 'receipt_key': 'page:1'})
        assert stale.status_code == 422 and received == []
        assert stale.json()['context']['fields'] == ['item_id', 'receipt_key']
        known = client.post('/api/upload', files={'file': ('a.png', b'x')},
                            data={'tags': 'a', 'collection_url': 'https://example.test/c', 'artist_tags': 'a'})
        assert known.status_code == 200 and received == ['a.png']
