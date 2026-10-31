"""Operator settings, read back from the running API (kura#102).

The values that steer a deployment live in `api/env` and in code defaults, and nothing
read them back: "which user is the owner here?" had no answer but "the default is 1".
This shows what THIS process is running with — os.environ of the API, not the file —
and where each value came from: `env` (set), `empty` (set to the empty string, which
some settings use as a value and others treat as unset — the page says only that it is
empty) or `unset` (the code's default applies). Secrets are reported as set/unset only,
never their value, and a value that could hold URL credentials (an @ or a ?) is withheld whole.

Administrators only. Read-only: changing a value is still an edit of `api/env` and a
restart, and this page is how to see that the restart took.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from api.config import OWNER_USER_ID
from api.db.models import User
from api.db.session import get_db

router = APIRouter(prefix="/api", tags=["settings"])

# Every environment variable the core API reads (tests/test_operator_settings.py fails
# when a new one is read and not listed here). True = a secret: only whether it is set.
SETTINGS: dict[str, bool] = {
    "COMFY_ROOT": False,
    "CORS_ALLOW_ORIGINS": False,
    "DATABASE_URL": True,
    "DATA_DIR": False,
    "DATA_URL": False,
    "KURA_API_KEY": True,
    "KURA_API_KEY_ENFORCE": False,
    "KURA_ARCHIVE_OBJECTS_URL": False,
    "KURA_ARCHIVE_READ_KEY": True,
    "KURA_ARTIFACT_DIR": False,
    "KURA_COLLECTOR_MCP_ORIGINS": False,
    "KURA_COLLECTOR_NEXT": False,
    "KURA_COLLECTOR_NEXT_ROOT": False,
    "KURA_DASHBOARD_KEY": True,
    "KURA_DB_ROLE": False,
    "KURA_FS_MEDIA": False,
    "KURA_FS_NAME_SALT": True,
    "KURA_FS_NAME_SALT_LEGACY": True,
    "KURA_FS_NAME_SALT_NEXT": True,
    "KURA_HUMAN_LOGIN_MODE": False,
    "KURA_LEGACY_ORIGINAL_ROOT": False,
    "KURA_MEDIA_AUTHORITY": False,
    "KURA_MEDIA_ROLE": False,
    "KURA_MIGRATE_SESSION_AUTHORIZATION": False,
    "KURA_MODELS_DIR": False,
    "KURA_ORIGINAL_ARCHIVE_ID": False,
    "KURA_ORIGINAL_ARCHIVE_ROOT": False,
    "KURA_ORIGINAL_CACHE_BYTES": False,
    "KURA_ORIGINAL_CACHE_DIR": False,
    "KURA_OWNER_USER_ID": False,
    "KURA_RENDITION_ROOT": False,
    "KURA_REQUIREMENTS": False,
    "KURA_SECRET": True,
    "KURA_STRICT_DEPS": False,
    "OLLAMA_URL": False,
}


def _may_carry_credentials(value: str) -> bool:
    """A URL can carry user:password (before an @) or a token (in a query). Picking those
    parts out of a value that may also be a list is guesswork — a password may contain a
    comma — so a value with either character is withheld whole, not partly masked."""
    return "@" in value or "?" in value


def settings_now(environ=os.environ) -> list[dict]:
    out = []
    for name, secret in SETTINGS.items():
        raw = environ.get(name)
        source = "unset" if raw is None else "empty" if raw == "" else "env"
        withheld = source == "env" and not secret and _may_carry_credentials(raw)
        out.append({"name": name, "source": source, "secret": secret, "withheld": withheld,
                    "value": raw if source == "env" and not secret and not withheld else None})
    return out


def _user(u: User) -> dict:
    return {"id": u.user_id, "name": u.name, "rank": u.rank}


@router.get("/settings/operator")
async def operator_settings(db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)) -> dict:
    if user.rank != "administrator":
        raise HTTPException(403, "administrators only")
    owner = await db.scalar(select(User).where(User.user_id == OWNER_USER_ID))
    admins = (await db.scalars(select(User).where(User.rank == "administrator")
                               .order_by(User.user_id))).all()
    return {
        "owner": {
            "id": OWNER_USER_ID,
            "source": "env" if "KURA_OWNER_USER_ID" in os.environ else "unset",
            "user": _user(owner) if owner else None,
        },
        # More than one is worth seeing: only the owner id says which of them uploads
        # are attributed to.
        "administrators": [_user(a) for a in admins],
        "settings": settings_now(),
    }
