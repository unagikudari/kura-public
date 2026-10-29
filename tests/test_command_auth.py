"""Use synthetic handlers: refusals must happen before any command body runs."""
import types
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI, HTTPException, Depends
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse
from api import access, auth


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('KURA_API_KEY', 'synthetic-machine-key')
    monkeypatch.setenv('KURA_API_KEY_ENFORCE', 'enforce')
    @asynccontextmanager
    async def session(): yield object()
    monkeypatch.setattr(access, 'async_session', session)
    original = auth.get_current_user
    async def user(request, db):
        if request.headers.get('authorization') != 'Token synthetic-valid':
            raise HTTPException(401, 'invalid token')
        return types.SimpleNamespace(name='person', user_id=1)
    monkeypatch.setattr(access, 'get_current_user', user)
    app = FastAPI()
    app.add_middleware(access.CommandAuthMiddleware)
    events = []
    @app.api_route('/api/future-extension/command', methods=['POST','PUT','PATCH','DELETE'])
    async def command(): events.append('write'); return {'ok':True}
    @app.post('/api/gated-command', dependencies=[Depends(auth.require_api_key)])
    async def gated(): events.append('gated'); return {'ok':True}
    @app.get('/api/posts')
    async def read(): return {'internal_read':True}
    @app.post('/api/auth/login')
    async def login(): return {'login':True}
    return TestClient(app), events


@pytest.mark.parametrize('method', ['POST','PUT','PATCH','DELETE'])
@pytest.mark.parametrize('headers', [{},{'x-api-key':'wrong'},{'authorization':'Token revoked'}])
def test_unqualified_commands_never_run(client, method, headers):
    c, events = client
    assert c.request(method,'/api/future-extension/command',headers=headers,json={}).status_code == 401
    assert events == []


@pytest.mark.parametrize('headers', [{'x-api-key':'synthetic-machine-key'}, {'authorization':'Token synthetic-valid'}])
def test_valid_human_or_machine_can_command(client, headers):
    c,events=client
    assert c.post('/api/future-extension/command',headers=headers).status_code==200
    assert events==['write']
    assert c.post('/api/gated-command',headers=headers).status_code==200


def test_explicit_internal_reads_and_login_remain_available(client):
    c,_=client
    assert c.get('/api/posts').status_code==200
    assert c.post('/api/auth/login').status_code==200
