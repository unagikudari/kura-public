"""Local human login; existing Token sessions remain independent of login keys."""
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import _parse_token_header, get_current_user
from api.db.models import User, UserToken
from api.db.session import get_db
from api.human_login import KeyRefused, login as local_login

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
async def login(body: dict = Body(...), db: AsyncSession = Depends(get_db)) -> dict:
    email, key = body.get("email"), body.get("key")
    if not isinstance(email, str) or not isinstance(key, str) or not email.strip() or not key.strip():
        raise HTTPException(status_code=400, detail="email and key are required")
    try:
        return await local_login(db, email.strip(), key.strip())
    except KeyRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.reason) from None
    except SQLAlchemyError:
        # Never expose SQL parameters (including verifier digests) in responses.
        await db.rollback()
        raise HTTPException(status_code=503, detail="Authentication storage unavailable") from None


@router.get("/whoami")
async def whoami(user: User = Depends(get_current_user)) -> dict:
    """Identity of the currently-authenticated user (for the SSR layout load)."""
    return {"name": user.name, "email": user.email, "rank": user.rank}


@router.post("/logout")
async def logout(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Disable the presented session token (idempotent).

    The SSR layer also clears the cookie; this revokes the underlying token so a
    leaked cookie cannot be reused."""
    creds = await _parse_token_header(request)
    if creds is not None:
        _username, token_str = creds
        await db.execute(
            UserToken.__table__.update()
            .where(UserToken.token == token_str)
            .values(enabled=False)
        )
        await db.commit()
    return {"status": "ok"}
