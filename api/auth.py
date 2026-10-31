"""
Token authentication compatible with szurubooru's scheme.

Header format: Authorization: Token base64(username:token)
"""

import base64
from typing import Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import User, UserToken
from api.db.session import get_db


async def _parse_token_header(request: Request) -> Optional[tuple[str, str]]:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Token "):
        return None
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        username, token = decoded.split(":", 1)
        return username, token
    except Exception:
        return None


# A token row is usable iff enabled AND (no expiry OR expiry in the future).
# Compared server-side via func.now() to avoid Python/DB timezone mismatch.
def _usable_token(token_str: str):
    return (
        select(UserToken)
        .where(
            UserToken.token == token_str,
            UserToken.enabled.is_(True),
            or_(
                UserToken.expiration_time.is_(None),
                UserToken.expiration_time > func.now(),
            ),
        )
    )


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    verified = getattr(request.state, 'kura_authenticated_user', None)
    if verified is not None:
        return verified
    creds = await _parse_token_header(request)
    if creds is None:
        # Same reasoning as the X-Api-Key refusal below: say where the credential is.
        from api.errors import missing_user_token
        raise missing_user_token()

    username, token_str = creds
    result = await db.execute(_usable_token(token_str))
    user_token = result.scalar_one_or_none()
    if user_token is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if user_token.user.name != username:
        raise HTTPException(status_code=401, detail="Token/username mismatch")

    return user_token.user


async def get_optional_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    creds = await _parse_token_header(request)
    if creds is None:
        return None
    username, token_str = creds
    result = await db.execute(_usable_token(token_str))
    user_token = result.scalar_one_or_none()
    if user_token is None or user_token.user.name != username:
        return None
    return user_token.user


async def require_api_key(request: Request) -> None:
    """Machine-to-machine gate for the unauthenticated write surfaces (kura#43
    item 1). Use as a router-level dependency: `dependencies=[Depends(require_api_key)]`.

    In observe mode (the default) a missing or wrong X-Api-Key is logged once per
    (path-prefix, condition) and allowed, so wiring this onto a router breaks no
    caller — the log names who still needs the key. In enforce mode it is a 401.

    Enforce with no key configured is a 500, not a silent open: choosing to
    enforce while nothing can be checked is a misconfiguration that must surface,
    not a gate that quietly lets everything through.
    """
    from api import api_key

    # Human commands have already passed the shared command boundary. A
    # machine key is not a second credential a signed-in human must possess.
    if getattr(getattr(request, 'state', None), 'kura_authenticated_user', None) is not None:
        return

    presented = request.headers.get(api_key.HEADER_NAME)
    if api_key.key_matches(presented):
        return
    enforce = api_key.enforcing()
    if enforce and api_key.configured_key() is None:
        # Refuse loudly rather than let enforce-mode degrade into open.
        raise HTTPException(
            status_code=500,
            detail="KURA_API_KEY is not set but KURA_API_KEY_ENFORCE=enforce",
        )
    # One line per (route prefix, reason) per process — a per-request warning would
    # bury the fact under its own volume.
    prefix = request.url.path.rsplit("/", 1)[0] or request.url.path
    reason = "missing" if presented is None else "wrong"
    _warn_api_key_once(f"{prefix}:{reason}:{'enforce' if enforce else 'observe'}",
                       prefix, reason, enforce)
    if enforce:
        # The refusal names the way IN, not just the door. Naming the header alone
        # tells a caller which word to type and leaves it hunting for the value: on
        # 2026-09-05 an agent with the key sitting in a file it never opened spent a
        # working day routing around surfaces it could not reach. A refusal is the
        # one message that is guaranteed to be read by whoever is blocked, so it is
        # the cheapest place to put the answer. The VALUE is never echoed — only
        # where it lives and how to load it without printing it.
        from api.errors import missing_api_key
        raise missing_api_key(api_key.HEADER_NAME)
    # observe: allow.


_api_key_warned: set[str] = set()


def _warn_api_key_once(dedup: str, prefix: str, reason: str, enforce: bool) -> None:
    if dedup in _api_key_warned:
        return
    _api_key_warned.add(dedup)
    import logging
    logging.getLogger(__name__).warning(
        "kura#43 X-Api-Key %s on %s* — %s. (mode=%s)",
        reason, prefix,
        "rejected" if enforce else "allowed in observe mode; teach this caller the key before enforcing",
        "enforce" if enforce else "observe",
    )
