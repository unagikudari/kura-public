# ============================================================================
# kura-api (FastAPI + asyncpg) — handles `/api/*` HTTP routes via kura-nginx.
# This is NOT szurubooru. szurubooru-api is a SEPARATE process at
# /mnt/c/Users/user/szurubooru/server-patches/, with its own search parser.
# When debugging /api/posts (or any /api/* route), the code that runs is HERE,
# not in szurubooru-api. See ARCHITECTURE.md at the repo root.
# ============================================================================
"""Info endpoint — szurubooru-compatible /api/info."""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.build_info import as_dict as _build_info_dict
from api import media_role, extension_hooks
from api.db.models import Pool, Post, Tag
from api.db.session import get_db

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["info"])


@router.get("/info")
async def get_info(db: AsyncSession = Depends(get_db)) -> dict:
    post_count = (await db.execute(select(func.count()).select_from(Post))).scalar() or 0
    tag_count = (await db.execute(select(func.count()).select_from(Tag))).scalar() or 0
    pool_count = (await db.execute(select(func.count()).select_from(Pool))).scalar() or 0

    return {
        "postCount": post_count,
        "diskUsage": 0,
        "serverTime": None,
        "config": {
            "name": "kura",
            "userNameRegex": "^[a-zA-Z0-9_-]{1,32}$",
            "passwordRegex": "^.{5,}$",
            "tagNameRegex": "^\\S+$",
            "tagCategoryNameRegex": "^[^\\s%+#/]+$",
            "defaultUserRank": "regular",
            "enableSafety": True,
            "contactEmail": None,
            "canSendMails": False,
        },
        "featuredPost": None,
        "featuringUser": None,
        "tagCount": tag_count,
        "poolCount": pool_count,
        "build": _build_info_dict(),
        "extensions": extension_hooks.versions(),
    }


@router.get("/build")
async def get_build_info() -> dict:
    """Standalone build/version info endpoint (subset of /api/info)."""
    return {**_build_info_dict(), "extensions": extension_hooks.versions()}


@router.get("/pipeline/status")
async def pipeline_status(db: AsyncSession = Depends(get_db)) -> dict:
    """Upload パイプラインのステータスサマリー。

    直近の投稿数・pool 統計と、installed extensions が足す cards を返す。
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    h1 = now - timedelta(hours=1)
    h24 = now - timedelta(hours=24)

    total = (await db.execute(select(func.count()).select_from(Post))).scalar() or 0
    last_1h = (await db.execute(
        select(func.count()).select_from(Post).where(Post.creation_time >= h1)
    )).scalar() or 0
    last_24h = (await db.execute(
        select(func.count()).select_from(Post).where(Post.creation_time >= h24)
    )).scalar() or 0

    # 最新投稿
    latest_row = (await db.execute(
        select(Post.post_id, Post.creation_time, Post.source)
        .order_by(Post.creation_time.desc()).limit(1)
    )).first()
    latest = None
    if latest_row:
        latest = {
            "id": latest_row.post_id,
            "created": latest_row.creation_time.isoformat() + "Z",
            "source": latest_row.source,
        }

    # pool 統計
    pool_count = (await db.execute(select(func.count()).select_from(Pool))).scalar() or 0
    pools_24h = (await db.execute(
        select(func.count()).select_from(Pool).where(Pool.creation_time >= h24)
    )).scalar() or 0

    return {
        "totalPosts": total,
        "uploads1h": last_1h,
        "uploads24h": last_24h,
        "latestPost": latest,
        "poolCount": pool_count,
        "newPools24h": pools_24h,
        # cards installed extensions add ({title, stats:[{label, value, color?}]})
        "extensions": await extension_hooks.status_cards(db),
    }



@router.get("/media-authority")
async def get_media_authority() -> dict:
    """Per-worker attestation (kura#50 R2 rev3): the gate compares the pid set
    observed here against the systemd cgroup's live worker pids, exact-equal."""
    return media_role.probe_payload()
