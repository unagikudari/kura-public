"""What happened since the viewer last asked: posts added (grouped by pool) and what the
installed extensions report (api/extension_hooks.activity).

The web polls this every 20 s while a tab is visible and shows each new event as a
notification in the bottom-right corner (web/src/lib/notifications.svelte.ts). Nothing is
stored for it: posts are read by creation time, and the client keeps which post ids and
event keys it has already shown, so the overlapping windows it asks for are not shown twice.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api import extension_hooks
from api.auth import get_current_user
from api.db.models import User
from api.db.session import get_db
from api.errors import KuraError

router = APIRouter(prefix="/api", tags=["activity"])

# A tab that slept for an hour is not shown an hour of history on waking: only this much.
WINDOW = timedelta(minutes=15)
# More than this in one answer is continued on the next (`more`, `after_id`): nothing is skipped.
MAX_POSTS = 2000

_POSTS = text("""
    SELECT p.id, p.creation_time,
           coalesce(p.primary_pool_id, (SELECT min(pp.pool_id) FROM pool_post pp WHERE pp.post_id = p.id)) AS pool_id
      FROM post p
     WHERE (p.creation_time, p.id) > (:since, :after_id) AND p.creation_time <= :now
     ORDER BY p.creation_time, p.id
     LIMIT :limit
""")
_NAMES = text("""
    SELECT DISTINCT ON (pool_id) pool_id, name FROM pool_name
     WHERE pool_id = ANY(:ids) ORDER BY pool_id, ord
""")


def posts_added(rows, names: dict[int, str]) -> list[dict]:
    """One event per pool (posts in no pool share one), each with the ids it covers."""
    pools: dict[int | None, list[int]] = {}
    for post_id, _, pool_id in rows:
        pools.setdefault(pool_id, []).append(post_id)
    return [{"key": f"posts:{pool_id or 0}:{ids[-1]}", "kind": "posts_added", "level": "info",
             "pool_id": pool_id, "pool_name": names.get(pool_id) if pool_id else None,
             "count": len(ids), "post_ids": ids}
            for pool_id, ids in pools.items()]


@router.get("/activity")
async def activity(since: datetime | None = None, after_id: int = 0,
                   user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    now = datetime.now(timezone.utc)
    if since is None:
        # The first call only fixes where the tab starts; it shows nothing from before.
        return {"now": now.isoformat(), "events": [], "more": False, "after_id": None}
    if since.tzinfo is None:
        raise KuraError(422, "since_without_timezone", context={"since": since.isoformat()},
                        fix="Send back the `now` an earlier response gave, which carries its offset.")
    if since.astimezone(timezone.utc) < now - WINDOW:
        since, after_id = now - WINDOW, 0
    since = since.astimezone(timezone.utc)
    # post.creation_time is UTC without a zone.
    bounds = {"since": since.replace(tzinfo=None), "now": now.replace(tzinfo=None)}
    rows = (await db.execute(_POSTS, {**bounds, "after_id": after_id, "limit": MAX_POSTS})).all()
    more = len(rows) == MAX_POSTS
    cursor = None
    if more:
        # Answer up to the last post returned and say where it stopped: the client asks
        # again from exactly (that time, that id), so posts sharing an instant go on too.
        now = rows[-1][1].replace(tzinfo=timezone.utc)
        cursor = rows[-1][0]
    pool_ids = sorted({pool_id for _, _, pool_id in rows if pool_id})
    names = dict((await db.execute(_NAMES, {"ids": pool_ids})).all()) if pool_ids else {}
    events = posts_added(rows, names)
    events += await extension_hooks.activity(db, since, now)
    return {"now": now.isoformat(), "events": events, "more": more, "after_id": cursor}
