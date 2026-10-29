"""The one way tagger code reads a post's display image bytes.

The display WebP is leaving `post.data` for content-addressed rendition files
(docs/notes/rendition-store-ideal.md). Until every row is copied, either may
hold the bytes; after the night-time switch only the files do. Callers should
not care which, so they ask here instead of selecting `post.data` themselves.

Order of sources, per post:
  1. `post.data` while it is still populated (transition; free, already local);
  2. HTTP `GET /api/post/{id}/content` from the media authority, which serves the
     rendition file. On primary that is its own API; on a replica (trainer) it is primary.
     With KURA_DISPLAY_CACHE set (a replica that trains repeatedly), responses are
     kept on disk by post id and revalidated with the content address as ETag.
     Without it nothing is cached — primary would only be copying its own files.

"Has a display image" is `media_location = 'pg'` (identical to `data IS NOT
NULL` on 2026-10-24: 1,211,247 rows each way). Use HAS_IMAGE in SQL filters.
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HAS_IMAGE = "p.media_location = 'pg'"

Query = Callable[[str, tuple], Sequence[tuple]]

_LEGACY = "select id, data from post where id = any(%s) and media_location = 'pg'"


def _authority() -> str:
    explicit = os.environ.get("KURA_MEDIA_AUTHORITY") or os.environ.get("KURA_RENDITION_SOURCE")
    if explicit:
        return explicit.rstrip("/").removesuffix("/api")
    from tagger.api_target import api_origin
    return api_origin()


def _cache_dir() -> Path | None:
    value = os.environ.get("KURA_DISPLAY_CACHE")
    return Path(value) if value else None


def _http(post_id: int, cache: Path | None, origin: str) -> bytes | None:
    req = urllib.request.Request(f"{origin}/api/post/{post_id}/content")
    # Use the existing service credentials at the authority's private ingress;
    # unredirected headers keep the token off subsequent redirect targets.
    username, token = os.environ.get("KURA_USER"), os.environ.get("KURA_TOKEN")
    if username and token:
        from tagger.credentials import token_headers
        req.add_unredirected_header("Authorization", token_headers(username, token)["Authorization"])
    if cache is not None:
        cache.mkdir(parents=True, exist_ok=True)
        body, tag = cache / f"{post_id}.webp", cache / f"{post_id}.etag"
        if body.exists() and tag.exists():
            req.add_header("If-None-Match", tag.read_text())
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
            etag = r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304 and cache is not None:
            return body.read_bytes()
        if e.code == 404:
            return None
        raise
    if cache is None:
        return data
    tmp = body.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, body)
    if etag:
        tag.write_text(etag)
    return data


def load(query: Query, post_ids: Iterable[int], *, workers: int = 8) -> list[tuple[int, bytes]]:
    """(post_id, bytes) for each id that has a display image, in the order given."""
    want = list(dict.fromkeys(post_ids))
    if not want:
        return []
    got: dict[int, bytes] = {}
    for pid, data in query(_LEGACY, (want,)):
        if data is not None:
            got[pid] = bytes(data)
    missing = [pid for pid in want if pid not in got]
    if missing:
        # Only ids that are images at all; others have nothing to fetch.
        images = {pid for (pid,) in query(
            "select id from post where id = any(%s) and media_location = 'pg'", (missing,))}
        todo = [pid for pid in missing if pid in images]
        if todo:
            origin, cache = _authority(), _cache_dir()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for pid, data in zip(todo, pool.map(lambda p: _http(p, cache, origin), todo)):
                    if data is not None:
                        got[pid] = data
    return [(pid, got[pid]) for pid in want if pid in got]
