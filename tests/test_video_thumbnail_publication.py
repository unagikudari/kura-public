"""Explicit-seek regeneration keeps serving and preserves the previous poster."""
import asyncio
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

from api.errors import KuraError
from PIL import Image
import pytest

from api.routers import posts as P, upload as U


def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(P.files, 'DATA_DIR', tmp_path)
    src = tmp_path / 'video.webm'
    src.write_bytes(b'fixture video')
    dest = tmp_path / 'poster.jpg'
    alias = tmp_path / 'alias.jpg'
    Image.new('RGB', (12, 8), 'red').save(dest)
    alias.hardlink_to(dest)
    monkeypatch.setattr(P.files, 'names', lambda kind, *args: [src] if kind == 'content' else [dest, alias])
    post = SimpleNamespace(post_id=17, mime_type='video/webm', version=4)
    result = SimpleNamespace(unique=lambda: SimpleNamespace(scalar_one_or_none=lambda: post, scalar_one=lambda: post))
    db = SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock())
    monkeypatch.setattr(P, '_serialize_post', lambda post, *a, **k: {'version': post.version})
    return src, dest, alias, db


def test_render_does_not_block_loop_or_expose_partial_output(tmp_path, monkeypatch):
    src, dest, alias, db = fixture(tmp_path, monkeypatch)
    before = dest.read_bytes()
    entered, finish = threading.Event(), threading.Event()
    def render(source, candidate, seek):
        assert source == src and seek == 3.5
        assert threading.current_thread() is not threading.main_thread()
        candidate.write_bytes(b'partial')
        entered.set()
        assert finish.wait(3)
        Image.new('RGB', (10, 6), 'blue').save(candidate)
    monkeypatch.setattr(U, '_video_thumb_at_seek', render)
    async def exercise():
        task = asyncio.create_task(P.regenerate_thumbnail(17, P.RegenerateThumbnailRequest(seek_seconds=3.5), db=db, auth_user=None, localize=False))
        try:
            for _ in range(1000):
                if entered.is_set():
                    break
                await asyncio.sleep(.001)
            assert entered.is_set() and not task.done()
            assert dest.read_bytes() == before and alias.read_bytes() == before
            assert db.commit.await_count == 0
        finally:
            finish.set()
        assert await task == {'version': 5}
    asyncio.run(exercise())
    with Image.open(dest) as image:
        assert image.size == (10, 6)
    assert dest.samefile(alias)
    db.commit.assert_awaited_once()
    assert not list(tmp_path.glob('.thumbnail-*'))


@pytest.mark.parametrize('failure', ['ffmpeg', 'timeout', 'invalid', 'missing'])
def test_failed_render_preserves_poster_alias_and_database(tmp_path, monkeypatch, failure):
    src, dest, alias, db = fixture(tmp_path, monkeypatch)
    before = dest.read_bytes()
    def render(source, candidate, seek):
        if failure == 'missing':
            return
        candidate.write_bytes(b'partial output')
        if failure == 'ffmpeg':
            raise subprocess.CalledProcessError(1, ['ffmpeg'], stderr=b'decoder failure')
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(['ffmpeg'], 30)
    monkeypatch.setattr(U, '_video_thumb_at_seek', render)
    with pytest.raises(KuraError) as error:
        asyncio.run(P.regenerate_thumbnail(17, P.RegenerateThumbnailRequest(), db=db, auth_user=None, localize=False))
    assert error.value.status == 500 and error.value.error in ("ffmpeg_failed", "thumbnail_regeneration_failed")
    assert dest.read_bytes() == before and dest.samefile(alias)
    db.commit.assert_not_awaited()
    assert not list(tmp_path.glob('.thumbnail-*'))


def test_simultaneous_regeneration_keeps_aliases_on_the_final_image(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    src, dest, alias, _ = fixture(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    second_entered = threading.Event()
    def render(source, candidate, seek):
        if seek == 1:
            entered.set()
            assert release.wait(3)
        else:
            second_entered.set()
        Image.new('RGB', (int(seek) + 10, 8), 'blue').save(candidate)
    monkeypatch.setattr(U, '_video_thumb_at_seek', render)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(U._regenerate_video_thumbnail, 17, src, [dest, alias], 1)
        assert entered.wait(3)
        second = pool.submit(U._regenerate_video_thumbnail, 17, src, [dest, alias], 2)
        try:
            assert not second_entered.wait(.05)
        finally:
            release.set()
        first.result(timeout=3)
        second.result(timeout=3)
    assert dest.samefile(alias)
    with Image.open(dest) as image:
        assert image.size == (12, 8)
