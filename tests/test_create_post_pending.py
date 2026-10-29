"""Exercise the real creation function up to flush, with no live DB/API/files."""
from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

from api.errors import KuraError
from PIL import Image
import pytest

from api.db.models import Post


class BeforePersistence(Exception):
    pass


class Session:
    def __init__(self, duplicate=False):
        self.duplicate = duplicate
        self.added = []

    async def execute(self, statement):
        return SimpleNamespace(scalar_one_or_none=lambda: object() if self.duplicate else None)

    def add(self, post):
        self.added.append(post)

    async def flush(self):
        # The complete production function is compiled, but no persistence or
        # subsequent file helpers can execute in this isolated constructor test.
        raise BeforePersistence


def load_create(monkeypatch, tmp_path):
    from api.routers.posts import create_post
    return create_post


def content(kind):
    if kind == 'video':
        raw = b'\x00\x00\x00\x18ftypmp42synthetic'
    else:
        buf = io.BytesIO()
        im = Image.new('RGB', (2, 2), 'red')
        if kind == 'animation':
            im.save(buf, format='GIF', save_all=True, append_images=[Image.new('RGB', (2, 2), 'blue')])
        else:
            im.save(buf, format='PNG')
        raw = buf.getvalue()

    async def read():
        return raw
    return SimpleNamespace(read=read, filename='synthetic-upload')


@pytest.mark.parametrize('kind', ['image', 'animation', 'video'])
def test_new_file_backed_post_has_pending_state(monkeypatch, tmp_path, kind):
    create = load_create(monkeypatch, tmp_path)
    db = Session()
    with pytest.raises(BeforePersistence):
        asyncio.run(create(content=content(kind), file=None, metadata=None, localize=False,
                           db=db, auth_user=SimpleNamespace(user_id=17)))
    assert len(db.added) == 1
    post = db.added[0]
    assert post.status == 'pending'
    assert post.type == kind
    assert post.user_id == 17
    assert list(tmp_path.iterdir()) == []


def test_duplicate_refuses_before_adding_a_row(monkeypatch, tmp_path):
    create = load_create(monkeypatch, tmp_path)
    db = Session(duplicate=True)
    with pytest.raises(KuraError) as exc:
        asyncio.run(create(content=content('image'), file=None, metadata=None, localize=False,
                           db=db, auth_user=None))
    assert exc.value.status == 409 and exc.value.error == "post_already_exists"
    assert db.added == []
    assert list(tmp_path.iterdir()) == []
