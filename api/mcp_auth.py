"""Per-request Kura identity for internal MCP adapters; no caller-owned owner IDs."""
import logging
from starlette.requests import Request
from starlette.responses import JSONResponse
from api.auth import _parse_token_header, _usable_token
from api.db.session import async_session


async def identity(scope):
    try:creds=await _parse_token_header(Request(scope))
    except (ValueError,UnicodeError):return None
    if creds is None:return None
    username,token=creds
    async with async_session() as db:
        row=(await db.execute(_usable_token(token))).scalar_one_or_none()
        if row is not None and row.user.name==username:return row.user.user_id
    return None


class TokenAuthMiddleware:
    """Internal Token authentication, not an OAuth/public connector implementation."""
    def __init__(self,app):
        self._app=app

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self._app(scope,receive,send)
        try:owner=await identity(scope)
        except Exception as exc:
            logging.getLogger(__name__).warning('MCP authentication unavailable: %s',type(exc).__name__)
            return await JSONResponse({'detail':{'error':'authentication_unavailable','fix':'認証サービスの復旧後に再接続してください。'}},status_code=503)(scope,receive,send)
        if owner is None:
            response=JSONResponse({'detail':{'error':'missing_or_invalid_authorization','fix':'有効なKura利用者トークンをAuthorizationヘッダーで指定してください。'}},status_code=401)
            return await response(scope,receive,send)
        # Replace caller state with a new per-request mapping; no process-global
        # identity or connection/session owner cache.
        scope={**scope,'state':{**scope.get('state',{}),'kura_owner_id':owner}}
        return await self._app(scope,receive,send)
