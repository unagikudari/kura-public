"""When a tagger worker cannot get a post's content because of infrastructure, not the post.

The canonical kura node may read an archived original from another node
(docs/notes/kura-on-nodeb-plan.md, "Originals on another host", rev 8). While that node is
unreachable the API answers 503 `original_unavailable`; a timeout, a refused connection or a
body shorter than its Content-Length mean the same. None of these is the post's fault, so a
worker sends **no completion** for it (a failed completion would count an attempt, and enough
of them mark a taggable post `failed` for good), ends the batch, and waits: the leases expire
and the posts return to the queue uncounted, as when a worker crashes.
"""
from __future__ import annotations

import http.client
import json
import socket
import urllib.error

DEFER_SECONDS = 60


class ContentUnavailable(Exception):
    """Defer the batch: no completion, leases expire, nothing counted."""


def is_original_unavailable(status: int, body: bytes | str | None) -> bool:
    if status != 503 or not body:
        return False
    try:
        detail = json.loads(body).get("detail")
    except (ValueError, AttributeError):
        return False
    return isinstance(detail, dict) and detail.get("error") == "original_unavailable"


def classify_urllib_error(exc: BaseException) -> ContentUnavailable | None:
    """The ContentUnavailable a urllib failure means, or None if it is the post's own."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read()
        except Exception:  # noqa: BLE001
            body = None
        return ContentUnavailable(f"HTTP 503 original_unavailable") if is_original_unavailable(exc.code, body) else None
    if isinstance(exc, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError,
                        http.client.IncompleteRead)):
        return ContentUnavailable(f"{type(exc).__name__}: {exc}")
    return None
