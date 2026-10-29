"""kura#43 / G1: no route of the real app lets an unauthenticated write through.

The boundary is one middleware (api/access.py) rather than a dependency on each
route, so the question "is every mutating API authorized?" has a mechanical
answer: enumerate every surface the shipped app mounts that accepts an unsafe
method and send it a request carrying no credentials. The gate's inner app is
replaced by a sentinel for the duration, so the evidence is "nothing got past
the gate" — not "the response was 401", which a handler could return after
doing its work — and no handler, file or database is ever reached. The single
exemption is sign-in itself.
"""
import re

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.routing import Mount, WebSocketRoute

from api import access

UNSAFE = ("POST", "PUT", "PATCH", "DELETE")
EXEMPT = {("POST", "/api/auth/login")}
# Mounted sub-apps the gate is meant to cover; their own TokenAuthMiddleware
# authenticates each user in addition.
MOUNTS = {"/mcp", "/api/collector-next/mcp"}


def _app():
    from api.main import app
    return app


def _surfaces(routes):
    """(kind, effective path, methods) for every route, through FastAPI 0.137's
    included routers. A wrapped route reports its effective path on the wrapper
    and its kind on the route it wraps (starlette_route / original_route)."""
    for route in routes:
        candidates = getattr(route, "effective_candidates", None)
        if candidates is not None:
            yield from _surfaces(candidates() if callable(candidates) else candidates)
            continue
        inner = getattr(route, "starlette_route", None) or getattr(route, "original_route", None) or route
        methods = getattr(route, "methods", None) or getattr(inner, "methods", None) or set()
        # a wrapped non-API route has an empty path on the wrapper; the prefixed
        # one is on the starlette route it carries
        yield inner, route.path or inner.path, set(methods)


def _probes(app):
    out = set()
    for inner, path, methods in _surfaces(app.routes):
        if isinstance(inner, Mount):
            out |= {(m, path + "/") for m in UNSAFE}
        else:
            out |= {(m, path) for m in methods if m in UNSAFE}
    return sorted(out)


def test_the_command_gate_is_installed_on_the_shipped_app():
    assert any(m.cls is access.CommandAuthMiddleware for m in _app().user_middleware)


def test_the_app_mounts_no_surface_the_gate_cannot_see():
    """WebSocket scopes skip the gate (it guards http). None exists today; one
    must come with its own authentication and an entry here. Mounts are probed
    below, and only the declared ones may exist."""
    for inner, path, _ in _surfaces(_app().routes):
        assert not isinstance(inner, WebSocketRoute), path
        assert not isinstance(inner, Mount) or path in MOUNTS, path


def test_nested_routes_keep_their_kind_and_prefix():
    from fastapi import APIRouter, FastAPI
    from starlette.routing import Route

    async def endpoint(request): ...

    inner = APIRouter()
    inner.add_api_websocket_route("/ws", endpoint)
    inner.routes.append(Mount("/sub", app=FastAPI()))
    inner.routes.append(Route("/raw", endpoint, methods=["POST"]))
    app = FastAPI()
    app.include_router(inner, prefix="/api/x")
    kinds = {(type(i).__name__, p) for i, p, _ in _surfaces(app.routes)}
    assert ("APIWebSocketRoute", "/api/x/ws") in kinds or ("WebSocketRoute", "/api/x/ws") in kinds
    assert ("Mount", "/api/x/sub") in kinds
    assert ("POST", "/api/x/raw") in _probes(app)


@pytest.fixture
def sentinel(monkeypatch):
    """Replace whatever the gate would hand a request to; record what arrives."""
    async def nobody(request, db):
        raise HTTPException(401, "no credentials")

    class _Session:
        async def __aenter__(self): return object()
        async def __aexit__(self, *exc): return False

    monkeypatch.setattr(access, "get_current_user", nobody)
    monkeypatch.setattr(access, "async_session", _Session)
    monkeypatch.setattr(access.api_key, "key_matches", lambda presented: False)

    app = _app()
    if app.middleware_stack is None:
        app.middleware_stack = app.build_middleware_stack()
    gate = app.middleware_stack
    while gate is not None and not isinstance(gate, access.CommandAuthMiddleware):
        gate = getattr(gate, "app", None)
    assert gate is not None, "CommandAuthMiddleware is not on the request path"

    reached = []

    async def past_the_gate(scope, receive, send):
        reached.append((scope["method"], scope["path"]))
        await send({"type": "http.response.start", "status": 299, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    real, gate.app = gate.app, past_the_gate
    try:
        yield app, reached
    finally:
        gate.app = real


def test_a_request_without_credentials_never_gets_past_the_gate(sentinel):
    app, reached = sentinel
    probes = _probes(app)
    assert len(probes) > 50, "the shipped app should mount its command surface"
    client = TestClient(app, raise_server_exceptions=False)
    for method, path in probes:
        url = re.sub(r"\{[^}:]+(:path)?\}", "1", path)
        client.request(method, url, json={})
    assert set(reached) <= EXEMPT, sorted(set(reached) - EXEMPT)


def test_the_sentinel_sees_a_request_the_gate_lets_through(sentinel):
    """The probe above is only evidence if a pass-through is visible."""
    app, reached = sentinel
    TestClient(app).post("/api/auth/login", json={})
    assert reached == [("POST", "/api/auth/login")]
