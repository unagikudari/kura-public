"""Bound upload size and decoder concurrency without copying videos into RAM."""
import asyncio
import io
import hashlib
import mmap
from contextlib import asynccontextmanager

from fastapi import HTTPException
from starlette.formparsers import MultiPartException
from starlette.exceptions import HTTPException as StarletteHTTPException

MAX_FILE_BYTES = 2 * 1024**3
MAX_IMAGE_BYTES = 64 * 1024**2
MAX_REQUEST_BYTES = MAX_FILE_BYTES + 2 * 1024**2
_slots = asyncio.Semaphore(2)


def content_identity(data) -> tuple[str, str, bytes]:
    """All original-byte identities; buffers/mmap are hashed without copying."""
    return (hashlib.md5(data).hexdigest(), hashlib.sha1(data).hexdigest(),
            hashlib.sha256(data).digest())




@asynccontextmanager
async def mapped_upload(file):
    async with _slots:
        stream = file.file
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(0)
        header = stream.read(16)
        stream.seek(0)
        from api.routers.upload import _detect_mime
        mime = _detect_mime(header, file.filename or '')
        limit = MAX_IMAGE_BYTES if mime.startswith('image/') else MAX_FILE_BYTES
        if size > limit:
            raise HTTPException(413, '画像は64MiB、その他のファイルは2GiBまでです。')
        if not size:
            yield b''
            return
        # Real multipart files are spooled to disk already; fileno rolls a small
        # spool over too. BytesIO is also accepted by embedded/private callers.
        if isinstance(stream, io.BytesIO):
            data = stream.getbuffer()
        else:
            data = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            yield data
        finally:
            if isinstance(data, memoryview): data.release()
            else: data.close()


class UploadBodyLimit:
    """Limit actual received bytes, including chunked multipart, before parsing.

    MultiPartException makes Starlette close every spool before returning its
    parser error. Translate its 400 into 413 without retaining request bytes.
    """
    def __init__(self, app): self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['path'] not in {'/api/upload', '/api/auth/login'} or scope['method'] != 'POST':
            return await self.app(scope, receive, send)
        login = scope['path'] == '/api/auth/login'
        limit = 16384 if login else MAX_REQUEST_BYTES
        count = 0
        exceeded = False
        async def bounded_receive():
            nonlocal count, exceeded
            message = await receive()
            count += len(message.get('body', b''))
            if count > limit:
                exceeded = True
                if login: raise StarletteHTTPException(413, 'ログイン要求が大きすぎます。')
                raise MultiPartException('アップロードは2GiBまでです。')
            return message
        async def bounded_send(message):
            if exceeded and message['type'] == 'http.response.start':
                message = {**message, 'status': 413}
            await send(message)
        await self.app(scope, bounded_receive, bounded_send)
