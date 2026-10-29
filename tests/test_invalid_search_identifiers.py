"""Malformed recognized filters must not widen a read or a later bulk selection."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from api.errors import KuraError
from api.routers import posts


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(posts.router)
    db = AsyncMock()
    app.dependency_overrides[posts.get_db] = lambda: db
    app.dependency_overrides[posts.get_optional_user] = lambda: None
    app.dependency_overrides[posts.safe_mode] = lambda: False

    @app.exception_handler(KuraError)
    async def refusal(request, exc):
        return JSONResponse(status_code=exc.status, content={"detail": exc.body()})

    with TestClient(app) as session:
        yield session, db


@pytest.mark.parametrize("query", [
    "id:1,2", "id:not-a-number", "id:", "-id:1,2", "id:1.5",
    "pool:not-a-number", "pool:", "-pool:no", "pool:1,2", "id:1 pool:no",
    "locked:x id:bad", "locked:x -pool:no", "locked:x tag:id:bad",
    "id:bad locked:x", "-pool:no locked:x",
])
@pytest.mark.parametrize("path", ["/api/posts", "/api/post/1/index"])
def test_invalid_identifier_refuses_search_and_position_before_db(client, query, path):
    session, db = client
    response = session.get(path, params={"query": query, "limit": 1})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_search_identifier"
    assert detail["context"]["filter"] in ("id", "pool")
    assert f'{detail["context"]["filter"]}:123' in detail["fix"]
    db.execute.assert_not_called()


@pytest.mark.parametrize("query,operator,pool", [
    ("id:123", "=", None), ("-id:123", "!=", None),
    ("pool:123", "IN", 123), ("-pool:123", "NOT IN", None),
])
def test_valid_identifiers_keep_their_predicates(query, operator, pool):
    clauses, pool_hint = asyncio.run(posts._build_where_clauses(None, query))
    assert len(clauses) == 1
    compiled = clauses[0].compile()
    assert operator in str(compiled)
    assert 123 in compiled.params.values()
    assert pool_hint == pool


def test_empty_search_is_still_explicitly_unfiltered():
    assert asyncio.run(posts._build_where_clauses(None, "")) == ([], None)
