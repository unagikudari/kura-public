"""kura#56: a NULL-thumbnail row whose FS thumbnail is missing must 404, not 307.

The handler used to 307 unconditionally to the legacy FS URL when the thumbnail
bytea was NULL. For 1,691 rows (measured 2026-09-05) that file does not exist and
none is regenerable, so the redirect was a lie the caller could not see — a blank
image, a 404 only on the second hop. The fix looks before pointing: if the FS
thumbnail is absent, 404 here.

The handler is exercised directly (no DB) with a fake session whose two
`execute(...).scalar_one_or_none()` calls return the Post then the NULL thumbnail,
and `files.exists` monkeypatched to model the file's presence.
"""
import asyncio
import types

import pytest
from api.errors import KuraError
from fastapi.responses import RedirectResponse

from api.routers import posts as posts_mod


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """Returns the queued results in order for successive execute() calls."""
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, *_a, **_k):
        return _Result(self._results.pop(0))


def _fake_post():
    # Only the attributes the handler touches.
    return types.SimpleNamespace(post_id=999001, mime_type="image/webp", version=1)


def _call(session, request=None):
    req = request or types.SimpleNamespace(headers={})
    return asyncio.run(posts_mod.get_post_thumbnail(999001, req, db=session))


@pytest.fixture(autouse=True)
def _no_rendition_yet(monkeypatch):
    """These cases are about the legacy column/FS fallback. A post that already has
    a rendition file is served before any of this (tests/test_rendition_store.py)."""
    async def none(*_a, **_k):
        return None
    monkeypatch.setattr(posts_mod.renditions, "response_for", none)


def _stub_naming(monkeypatch):
    """Keep the salt (KURA_FS_NAME_SALT) out of these tests: stub the two naming
    calls the handler makes so the decision under test — exists? 404 : 307 — is
    isolated from filename derivation."""
    from pathlib import Path
    monkeypatch.setattr(posts_mod.files, "names",
                        lambda kind, pid, mime=None: [Path("/nonexistent/thumb.jpg")])
    monkeypatch.setattr(posts_mod, "_legacy_fs_thumbnail_url",
                        lambda post: "/data/generated-thumbnails/stub.jpg")


def test_missing_fs_thumbnail_returns_404(monkeypatch):
    monkeypatch.setattr(posts_mod.media_role, "MEDIA_ROLE", "authority", raising=False)
    _stub_naming(monkeypatch)
    monkeypatch.setattr(posts_mod.files, "exists", lambda _p: False)
    session = _FakeSession([_fake_post(), None])  # post found, thumbnail NULL
    with pytest.raises(KuraError) as ei:
        _call(session)
    assert ei.value.status == 404 and ei.value.error == "thumbnail_not_found"


def test_present_fs_thumbnail_still_307(monkeypatch):
    monkeypatch.setattr(posts_mod.media_role, "MEDIA_ROLE", "authority", raising=False)
    _stub_naming(monkeypatch)
    monkeypatch.setattr(posts_mod.files, "exists", lambda _p: True)
    session = _FakeSession([_fake_post(), None])
    resp = _call(session)
    assert isinstance(resp, RedirectResponse)
    assert resp.status_code == 307


def test_present_thumbnail_bytea_is_served_not_redirected(monkeypatch):
    monkeypatch.setattr(posts_mod.media_role, "MEDIA_ROLE", "authority", raising=False)
    # exists must never be consulted when the bytea is present.
    monkeypatch.setattr(posts_mod.files, "exists",
                        lambda _p: pytest.fail("exists() called though bytea present"))
    session = _FakeSession([_fake_post(), b"\x00webpbytes"])  # thumbnail present
    resp = _call(session)
    assert resp.status_code == 200
    assert resp.media_type == "image/webp"
