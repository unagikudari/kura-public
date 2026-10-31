"""Paint timings measured in the viewer's own browser (back/forward and scroll).

A headless browser on primary does not see what a long-lived desktop tab sees, so the web
client measures, in the person's browser, how long a history step or a scroll stop takes
until every thumbnail in view has loaded (web/src/lib/paintTiming.ts). Each report is
one journal line `client-timing {...}` — read them with
`journalctl --user -u kura-api | grep client-timing`. Nothing is stored in the database.
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request

from api.auth import get_current_user
from api.db.models import User

router = APIRouter(prefix="/api", tags=["client-timing"])
_log = logging.getLogger("kura.client_timing")

_KINDS = {"back", "forward", "scroll", "load"}
_NUMBERS = ("first_frame_ms", "filled_ms", "visible", "unfilled", "longtask_ms", "longtask_max_ms",
            "heap_mb", "dom_nodes", "failed")
_TEXTS = {"path": 300, "build": 40, "from": 300, "nav_type": 20}
# A browser sends a handful a minute; these bound what a runaway tab (or anyone holding a
# session) can put in the journal: a small body, and per user a fixed number per minute.
MAX_BODY = 4096
PER_MINUTE = 60
_recent: dict[str, deque] = defaultdict(deque)


def _admit(user: str, now: float) -> bool:
    seen = _recent[user]
    while seen and now - seen[0] > 60:
        seen.popleft()
    if len(seen) >= PER_MINUTE:
        return False
    seen.append(now)
    return True


@router.post("/client-timing", status_code=204)
async def client_timing(request: Request, user: User = Depends(get_current_user)) -> None:
    # Count what actually arrives (a chunked body has no Content-Length to trust).
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY:
            raise HTTPException(413, f"a timing is at most {MAX_BODY} bytes")
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(422, "the body is not JSON")
    if not isinstance(body, dict):
        raise HTTPException(422, "the body is not a JSON object")
    if not _admit(user.name, time.monotonic()):
        raise HTTPException(429, f"at most {PER_MINUTE} timings a minute")
    kind = body.get("kind")
    if kind not in _KINDS:
        raise HTTPException(422, "kind must be one of " + ", ".join(sorted(_KINDS)))
    line: dict = {"kind": kind, "user": user.name}
    for key in _NUMBERS:
        value = body.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 1e9:
            line[key] = round(value, 1)
    for key, limit in _TEXTS.items():
        value = body.get(key)
        if isinstance(value, str):
            line[key] = value[:limit]
    if body.get("timed_out") is True:
        line["timed_out"] = True
    # WARNING so it reaches the journal under the API's default log level; it is a record, not an alarm.
    _log.warning("client-timing %s", json.dumps(line, ensure_ascii=False, sort_keys=True))
