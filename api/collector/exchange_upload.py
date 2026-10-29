"""Bounded browser exchange adapter; the CLI and HTTP share restore semantics."""
import asyncio
import shutil
import tempfile
from pathlib import Path

from . import restore
from .export import RESERVE_BYTES

MAX_ARCHIVE_BYTES = 512 * 1024**2


class UploadLimit(ValueError):
    pass


async def receive(request, root: Path):
    """Spool without multipart parsing or a full request-body allocation."""
    length=request.headers.get('content-length')
    if length is not None:
        try:size=int(length)
        except ValueError:raise UploadLimit('Invalid archive size')
        if size<0 or size>MAX_ARCHIVE_BYTES:raise UploadLimit('Archive exceeds browser upload limit')
    stage=root/'staging';stage.mkdir(parents=True,exist_ok=True)
    archive=tempfile.TemporaryFile(mode='w+b',dir=stage)
    try:
        size=0
        async for chunk in request.stream():
            size+=len(chunk)
            if size>MAX_ARCHIVE_BYTES:raise UploadLimit('Archive exceeds browser upload limit')
            if shutil.disk_usage(stage).free<len(chunk)+RESERVE_BYTES:
                raise OSError('Storage reserve exhausted')
            write=asyncio.create_task(asyncio.to_thread(archive.write,chunk))
            try:await asyncio.shield(write)
            except asyncio.CancelledError:
                try:await write
                finally:raise
        archive.seek(0)
        return archive
    except BaseException:
        archive.close();raise


async def execute(sessions, root, archive, owner):
    """Own the transaction; keep input alive until decoding threads have stopped."""
    async with sessions.begin() as db:
        return await restore.restore(db,root,archive,owner)
