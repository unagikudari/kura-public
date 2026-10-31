"""Real file processing with temporary media and fake DB transactions."""
import asyncio
import hashlib
import io
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import UploadFile

from api.errors import KuraError
from PIL import Image
import pytest

from api.db.models import Post
from api.routers import posts as P
from api.routers import upload as U
from api.services import files, post_files as F


def png():
    out = io.BytesIO()
    Image.new('RGBA', (600, 200), (255, 0, 0, 0)).save(out, 'PNG')
    return out.getvalue()


async def materialized(db, post, data, thumb):
    post.status = 'ok'
    return True


def setup(tmp_path, monkeypatch, post=None):
    def names(kind, post_id, mime=None):
        return [tmp_path / f'{kind}-main', tmp_path / f'{kind}-alias']
    monkeypatch.setattr(files, 'names', names)
    monkeypatch.setattr(P, '_serialize_post', lambda p, *a, **kw: {'id': p.post_id})
    result = SimpleNamespace(scalar_one_or_none=lambda: post, scalar_one=lambda: post)
    db = SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock(), refresh=AsyncMock(),
                         rollback=AsyncMock())
    db.add = lambda obj: setattr(db, 'post', obj)
    async def flush():
        db.post.post_id = 17
    db.flush = flush
    return db, names


def test_preparation_preserves_dimensions_alpha_and_first_animation_frame():
    raw = png()
    media = F.prepare(raw)
    assert (media.mime, media.width, media.height, media.post_type) == ('image/png', 600, 200, 'image')
    assert F.checksums(raw) == (hashlib.sha1(raw).hexdigest(), hashlib.md5(raw).hexdigest())
    with Image.open(io.BytesIO(media.poster)) as poster:
        assert poster.size == (300, 100)
        assert min(poster.getpixel((150, 50))) > 245  # transparency on white
    out = io.BytesIO()
    Image.new('RGB', (8, 8), 'red').save(out, 'GIF', save_all=True, append_images=[Image.new('RGB', (8, 8), 'blue')])
    media = F.prepare(out.getvalue())
    assert media.post_type == 'animation'
    with Image.open(io.BytesIO(media.poster)) as poster:
        assert poster.getpixel((0, 0))[0] > 240


def _frames(fmt):
    out = io.BytesIO()
    Image.new('RGB', (8, 8), 'red').save(out, fmt, save_all=True, append_images=[Image.new('RGB', (8, 8), 'blue')])
    return out.getvalue()


@pytest.mark.parametrize('fmt', ['WEBP', 'PNG'])
def test_animated_webp_and_apng_are_animations_and_their_stills_are_images(fmt):
    assert F.prepare(_frames(fmt)).post_type == 'animation'                 # #149: not a still view
    still = io.BytesIO(); Image.new('RGB', (8, 8), 'red').save(still, fmt)
    assert F.prepare(still.getvalue()).post_type == 'image'


def _sha256_race():
    from sqlalchemy.exc import IntegrityError
    driver = SimpleNamespace(table_name='post', constraint_name='ux_post_sha256_ok')
    return IntegrityError('INSERT', {}, SimpleNamespace(sqlstate='23505', driver_exception=driver))


@pytest.mark.parametrize('failure', ['not_materialized', 'lost_the_race'])
def test_a_refused_new_post_leaves_no_row_and_no_file(tmp_path, monkeypatch, failure):
    """A post that cannot be made visible is refused, not left 'pending' with nothing to retry
    it; a request that loses the identical-content race removes the files it wrote."""
    db, names = setup(tmp_path, monkeypatch)
    async def not_materialized(db, post, data, thumb):
        return False
    monkeypatch.setattr(U, '_synchronous_materialize', materialized if failure == 'lost_the_race' else not_materialized)
    if failure == 'lost_the_race':
        db.commit = AsyncMock(side_effect=_sha256_race())
    upload = UploadFile(io.BytesIO(png()), filename='image.png')
    with pytest.raises(KuraError) as exc:
        asyncio.run(P.create_post(content=upload, file=None, metadata=None, db=db, auth_user=None, localize=False))
    assert (exc.value.status, exc.value.error) == ((422, 'media_not_processable') if failure == 'not_materialized'
                                                   else (409, 'post_already_exists'))
    db.rollback.assert_awaited_once()
    assert list(tmp_path.iterdir()) == []


def test_processing_yields_to_other_requests_and_persists_original(tmp_path, monkeypatch):
    raw = png()
    db, names = setup(tmp_path, monkeypatch, None)
    entered, release = threading.Event(), threading.Event()
    prepare = F.prepare
    def slow_prepare(data):
        assert threading.current_thread() is not threading.main_thread()
        entered.set()
        assert release.wait(3)
        return prepare(data)
    monkeypatch.setattr(F, 'prepare', slow_prepare)
    actual_store = F.store
    def store(*args):
        assert threading.current_thread() is not threading.main_thread()
        return actual_store(*args)
    monkeypatch.setattr(F, 'store', store)
    monkeypatch.setattr(U, '_synchronous_materialize', materialized)
    async def exercise():
        upload = UploadFile(io.BytesIO(raw), filename='folder/image.png')
        call = P.create_post(content=upload, file=None, metadata=None, db=db, auth_user=None, localize=False)
        task = asyncio.create_task(call)
        try:
            for _ in range(1000):
                if entered.is_set():
                    break
                await asyncio.sleep(.001)
            assert entered.is_set() and not task.done()
            db.commit.assert_not_awaited()
        finally:
            release.set()
        assert await task == {'id': 17}
    asyncio.run(exercise())
    post = db.post
    assert (post.canvas_width, post.canvas_height, post.mime_type) == (600, 200, 'image/png')
    assert post.checksum == hashlib.sha1(raw).hexdigest()
    assert post.status == 'ok' and post.safety == 'sketchy'     # visible on return
    content = names('content', 17)
    assert content[0].read_bytes() == raw and content[0].samefile(content[1])
    posters = names('thumbnail', 17)
    assert posters[0].samefile(posters[1])
    with Image.open(posters[0]) as image:
        assert image.size == (300, 100)
    db.commit.assert_awaited_once()


def test_invalid_image_fails_before_file_writes_or_commit(tmp_path, monkeypatch):
    db, _ = setup(tmp_path, monkeypatch, None)
    upload = UploadFile(io.BytesIO(b'corrupt image'), filename='bad.png')
    call = P.create_post(content=upload, file=None, metadata=None, db=db, auth_user=None, localize=False)
    with pytest.raises(KuraError) as error:
        asyncio.run(call)
    assert error.value.status == 400 and error.value.error == "unsupported_media"
    db.commit.assert_not_awaited()
    assert not list(tmp_path.iterdir())


def test_failed_video_poster_keeps_existing_poster_and_original(tmp_path, monkeypatch):
    _, names = setup(tmp_path, monkeypatch)
    poster = names('thumbnail', 17)[0]
    poster.write_bytes(b'previous poster')
    def fail(command, **kwargs):
        Path(command[-1]).write_bytes(b'partial')
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(F.subprocess, 'run', fail)
    raw = b'\x1aE\xdf\xa3video'
    assert not F.store(17, raw, F.prepare(raw))
    assert poster.read_bytes() == b'previous poster'
    assert names('content', 17)[0].read_bytes() == raw
    assert not list(tmp_path.glob('.thumbnail-*'))


def test_file_deletion_keeps_request_loop_available(tmp_path, monkeypatch):
    post = SimpleNamespace(post_id=17, mime_type='image/png')
    db, names = setup(tmp_path, monkeypatch, post)
    db.delete = AsyncMock()
    paths = names('content', 17) + names('thumbnail', 17)
    for path in paths:
        path.write_bytes(b'media')
    entered, release = threading.Event(), threading.Event()
    delete = files.delete_all_names
    def slow_delete(doomed):
        entered.set()
        assert release.wait(3)
        return delete(doomed)
    monkeypatch.setattr(files, 'delete_all_names', slow_delete)
    async def exercise():
        task = asyncio.create_task(P.delete_post(17, db=db, auth_user=None))
        try:
            for _ in range(1000):
                if entered.is_set():
                    break
                await asyncio.sleep(.001)
            assert entered.is_set() and not task.done()
        finally:
            release.set()
        await task
    asyncio.run(exercise())
    assert not any(path.exists() for path in paths)
    db.delete.assert_awaited_once_with(post)
    db.commit.assert_awaited_once()


def test_video_renderers_share_a_process_limit(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from api.routers import upload as U
    _, names = setup(tmp_path, monkeypatch)
    entered, release, second_called = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        else:
            second_called.set()
        Image.new('RGB', (20, 10), 'blue').save(command[-1], 'JPEG')
    monkeypatch.setattr(F.subprocess, 'run', run)
    raw = b'\x1aE\xdf\xa3video'
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(F.store, 17, raw, F.prepare(raw))
        assert entered.wait(3)
        second = pool.submit(U._video_thumb_at_seek, tmp_path/'source', tmp_path/'other.jpg', 1)
        try:
            assert not second_called.wait(.05)
        finally:
            release.set()
        assert first.result(timeout=3)
        second.result(timeout=3)
    assert len(calls) == 2
