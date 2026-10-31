"""What an extension may call in core — the one module it imports helpers from.

Extensions used to reach into routers for private helpers (`pools._derive_pool_name_from_url`,
`upload._get_or_create_pool_by_source_url`, …); a rename in core then broke them silently.
These names are the declared interface (extension.json `core_api` 4): core keeps their meaning,
and core's own routers may change behind them.

Importing this module imports nothing else: a standalone script run by plain python3 (no API
virtualenv) reaches `redact` without FastAPI; the router helpers load when first called.
"""
from __future__ import annotations

CORE_API = 4


def redact(text: str | None) -> str | None:
    """Secrets removed by every registered redactor (identity when none is registered)."""
    from api.services.key_redaction import redact as _redact
    return _redact(text)


def canonical_source_url(url: str) -> str:
    """The one spelling of a source URL that keys its pool (registered source rules; else as given)."""
    from api.routers.pools import _canonical_gallery_url
    return _canonical_gallery_url(url)


def pool_name_from_url(url: str) -> str:
    """The display name core gives a pool that has only its source URL."""
    from api.routers.pools import _derive_pool_name_from_url
    return _derive_pool_name_from_url(url)


async def adopt_posts_by_source(db, pool_id: int, url: str) -> int:
    """Attach existing posts whose source matches the pool's URL; the caller owns the commit."""
    from api.routers.pools import adopt_posts_by_source as _adopt
    return await _adopt(db, pool_id, url)


async def pool_by_source_url(db, source_url: str, display_name: str | None = None):
    """The pool keyed by that source URL, created if absent."""
    from api.routers.upload import _get_or_create_pool_by_source_url
    return await _get_or_create_pool_by_source_url(db, source_url, display_name)


async def ensure_pool(db, source_url: str, *, name: str | None = None, alternates=()) -> tuple[int, bool]:
    """(pool id, created) of the pool keyed by that source URL, created if absent.

    The same find-or-create as POST /api/pools/ensure, including the title hint and
    alias URLs; the caller owns the commit.
    """
    from api.routers.pools import ensure_pool_by_source
    pool, created = await ensure_pool_by_source(db, source_url, name=name, alternates=alternates)
    return pool.pool_id, created


async def pool_view(db, pool_id: int) -> dict:
    """The pool as core serializes it (names, posts, primary source URL, sources)."""
    from api.routers.pools import _load_pool, _serialize_pool
    db.expire_all()  # read what the database holds now, not what this session loaded earlier
    return _serialize_pool(await _load_pool(str(pool_id), db))


async def upload(db, file, *, record=None, **fields):
    """`POST /api/upload`'s work, for an extension's own upload route; returns its response.

    `fields` are the core upload's form fields (tags, source, safety, pool,
    pool_source_url, collection_url, artist_tags). `record(db, ingested)` runs in
    the same transaction before the one commit; `ingested.post` is the stored post
    and `ingested.duplicate` says whether it already existed. A failure in `record`
    rolls the upload back.
    """
    from api.routers.upload import receive, store, upload_response

    async def handle(data):
        return upload_response(await store(db, data, record=record, filename=file.filename, **fields))
    return await receive(file, handle)


__all__ = ["CORE_API", "adopt_posts_by_source", "canonical_source_url", "ensure_pool",
           "pool_by_source_url", "pool_name_from_url", "pool_view", "redact", "upload"]
