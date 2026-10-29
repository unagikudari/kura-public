"""Bounded model receipt and full verification, with no production DB/models."""
import asyncio
import hashlib
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest

from api.routers import tagger as T
from api.tagger_core import artifact_store as S


class Request:
    def __init__(self, chunks):
        self.chunks = chunks
        self.received = 0
    async def stream(self):
        for chunk in self.chunks:
            self.received += 1
            yield chunk


def setup(tmp_path, monkeypatch):
    real_temp = T.tempfile.TemporaryDirectory
    monkeypatch.setattr(T.tempfile, 'TemporaryDirectory', lambda **kwargs: real_temp(dir=tmp_path, **kwargs))
    store = S.ArtifactStore(tmp_path / 'artifacts')
    monkeypatch.setattr(S, 'default_store', lambda: store)
    monkeypatch.setattr(T, '_artifact_upload_slots', asyncio.Semaphore(1))
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    return store, db


def test_model_verification_yields_and_registers_only_complete_content(tmp_path, monkeypatch):
    store, db = setup(tmp_path, monkeypatch)
    data = b'opaque model'; sha = hashlib.sha256(data).hexdigest()
    entered, finish = threading.Event(), threading.Event()
    real = T._store_uploaded_artifact
    def slow(path, digest):
        assert threading.current_thread() is not threading.main_thread()
        assert path.read_bytes() == data
        entered.set(); assert finish.wait(3)
        return real(path, digest)
    monkeypatch.setattr(T, '_store_uploaded_artifact', slow)
    async def scenario():
        task = asyncio.create_task(T.put_artifact(sha, Request([data[:3], data[3:]]), db=db, user=None))
        try:
            for _ in range(1000):
                if entered.is_set(): break
                await asyncio.sleep(.001)
            assert entered.is_set() and not task.done()
            db.execute.assert_not_awaited()
        finally: finish.set()
        assert await task == {'content_sha256': sha, 'byte_size': len(data), 'kind': 'opaque'}
    asyncio.run(scenario())
    assert store.have(sha)
    db.commit.assert_awaited_once()
    db.rollback.assert_awaited_once()
    assert db.execute.call_args.args[1] == {'s': sha, 'n': len(data), 'k': 'opaque'}


@pytest.mark.parametrize('bad_sha', ['wrong-shape', '0' * 64])
def test_wrong_identity_never_registers_content(tmp_path, monkeypatch, bad_sha):
    store, db = setup(tmp_path, monkeypatch)
    request = Request([b'wrong bytes'])
    with pytest.raises(HTTPException) as error:
        asyncio.run(T.put_artifact(bad_sha, request, db=db, user=None))
    assert error.value.status_code == 400
    db.execute.assert_not_awaited()
    assert not store.root.exists()
    assert not list(tmp_path.iterdir())
    if bad_sha == 'wrong-shape': assert request.received == 0


def test_oversize_receipt_stops_before_consuming_remaining_body(tmp_path, monkeypatch):
    store, db = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(T, 'MAX_ARTIFACT_BYTES', 8)
    request = Request([b'1234', b'56789', b'never read'])
    with pytest.raises(HTTPException) as error:
        asyncio.run(T.put_artifact('0' * 64, request, db=db, user=None))
    assert error.value.status_code == 413
    assert request.received == 2
    db.execute.assert_not_awaited()
    assert not store.root.exists()
    assert not list(tmp_path.iterdir())


def test_cancelled_verification_finishes_before_stage_is_removed(tmp_path, monkeypatch):
    store, db = setup(tmp_path, monkeypatch)
    entered, finish = threading.Event(), threading.Event()
    stages = []
    real = T._store_uploaded_artifact
    data = b'cancelled model';sha = hashlib.sha256(data).hexdigest()
    def slow(path, digest):
        stages.append(path);entered.set();assert finish.wait(3)
        return real(path, digest)
    monkeypatch.setattr(T, '_store_uploaded_artifact', slow)
    async def scenario():
        task = asyncio.create_task(T.put_artifact(sha, Request([data]), db=db, user=None))
        try:
            for _ in range(1000):
                if entered.is_set(): break
                await asyncio.sleep(.001)
            assert entered.is_set()
            for _ in range(2):
                task.cancel();await asyncio.sleep(0)
                assert not task.done() and stages[0].exists()
        finally:finish.set()
        with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(scenario())
    assert store.have(sha)
    assert not stages[0].parent.exists()
    db.execute.assert_not_awaited()


def test_interrupted_receipt_removes_stage_without_registration(tmp_path, monkeypatch):
    _, db = setup(tmp_path, monkeypatch)
    class BrokenRequest:
        async def stream(self):
            yield b'partial model'
            raise ConnectionError('client disconnected')
    with pytest.raises(ConnectionError):
        asyncio.run(T.put_artifact('0' * 64, BrokenRequest(), db=db, user=None))
    db.execute.assert_not_awaited()
    assert not list(tmp_path.iterdir())
