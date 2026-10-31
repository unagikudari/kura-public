"""Fail-closed authentication for every HTTP command, including extensions.

Loopback reads are the internal service interface. Published reads must pass
the authenticated web ingress; never publish port 8001 or the static backend.
MCP retains its own per-user identity checks in addition to this command gate.
"""
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from api import api_key
from api.auth import get_current_user
from api.db.session import async_session
from api.errors import KuraError


class CommandAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope['type'] != 'http' or scope['method'] in {'GET', 'HEAD', 'OPTIONS'}
                or scope['path'] == '/api/auth/login'):
            return await self.app(scope, receive, send)
        request = Request(scope)
        if not api_key.key_matches(request.headers.get(api_key.HEADER_NAME)):
            try:
                async with async_session() as db:
                    user = await get_current_user(request, db)
                scope.setdefault('state', {})['kura_authenticated_user'] = user
            except (HTTPException, KuraError):
                return await JSONResponse({'detail': {
                    'error': 'authentication_required',
                    'fix': 'サインインするか、利用者トークンまたは機械用APIキーを指定してください。',
                }}, status_code=401)(scope, receive, send)
        await self.app(scope, receive, send)
