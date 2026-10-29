"""段 3 の EXECUTE の窓を、API の再試行 (`api/services/governed_window.py`) が隠せるかを確かめる。

docs/notes/tagging-work-table-plan.md の段 2。本番の `lc_messages` は ja_JP.UTF-8 で、エラーの文は
日本語になる。再試行の判定は文を読まずに has_function_privilege を問うので、それが日本語の DB で
効くことを、本番と同じ設定の使い捨てクラスタで確かめる。

- (a) 統制関数の EXECUTE を外して戻すと、業務トランザクション全体が 1 回だけ通る。
- (b) 別の表の権限が無いときは、追加の再試行は最大 1 回で、そのあと元の 42501 で失敗する。
- (c) 期限までに窓が閉じなければ、何も commit せずに WindowExpired になり、ログに残る。
- (d) 期限は DB の待ちにも効く: 初回の attempt が行ロックを待ち続けても、期限で打ち切って巻き戻す。
- (e) commit で出た 42501 は再試行しない (commit は run() が再試行の外で 1 回だけ行う)。

統制関数は本番の本体 (`tests/fixtures/tagging_queue_governed_20261024.sql`) を使う。
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from contextlib import closing
from pathlib import Path

import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from api.services import governed_window

REPO = Path(__file__).resolve().parents[1]
BIN = Path("/usr/lib/postgresql/18/bin")
FIXTURE = REPO / "tests/fixtures/tagging_queue_governed_20261024.sql"
PORT = 65446


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    if not (BIN / "initdb").exists():
        pytest.skip("PostgreSQL 18 test binaries required")
    if "ja_JP.utf8" not in subprocess.run(["locale", "-a"], capture_output=True, text=True).stdout:
        pytest.skip("ja_JP.UTF-8 locale required (production's lc_messages)")
    root = tmp_path_factory.mktemp("governed-window-pg")
    data, sock = root / "data", root / "socket"
    sock.mkdir()
    subprocess.run([str(BIN / "initdb"), "-D", str(data), "-A", "trust", "-U", "postgres",
                    "--locale=ja_JP.UTF-8", "-E", "UTF8"], check=True, capture_output=True)
    subprocess.run([str(BIN / "pg_ctl"), "-D", str(data), "-l", str(root / "log"), "-o",
                    f"-k {sock} -p {PORT} -c listen_addresses='' -c shared_buffers=16MB "
                    "-c lc_messages=ja_JP.UTF-8", "-w", "start"], check=True, capture_output=True)
    try:
        yield str(sock)
    finally:
        subprocess.run([str(BIN / "pg_ctl"), "-D", str(data), "-m", "immediate", "-w", "stop"],
                       check=True, capture_output=True)


def _admin(sock, dbname):
    c = psycopg2.connect(host=sock, port=PORT, user="postgres", dbname=dbname)
    c.autocommit = True
    return c


@pytest.fixture
def db(cluster):
    name = f"gw_{os.urandom(4).hex()}"
    with closing(_admin(cluster, "postgres")) as c, c.cursor() as q:
        q.execute(f"CREATE DATABASE {name}")
    subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-h", cluster, "-p", str(PORT), "-U", "postgres",
                    "-d", name, "-f", str(FIXTURE)], check=True, capture_output=True)
    with closing(_admin(cluster, name)) as c, c.cursor() as q:
        q.execute("""INSERT INTO post (id, "type", status, tagging_status) VALUES (1, 'image', 'ok', 'pending')""")
        # Stands in for the tag and audit writes a completion makes before the governed call.
        q.execute("CREATE TABLE side_effect (id serial PRIMARY KEY, post_id int)")
        q.execute("GRANT INSERT ON side_effect TO kura")
        q.execute("GRANT USAGE ON SEQUENCE side_effect_id_seq TO kura")
    try:
        yield cluster, name
    finally:
        with closing(_admin(cluster, "postgres")) as c, c.cursor() as q:
            q.execute(f"DROP DATABASE {name} WITH (FORCE)")


def _session(sock, name):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"postgresql+asyncpg://kura@/{name}?host={sock}&port={PORT}")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _completion(session, calls):
    """A completion-shaped transaction: a write, then the governed call. run() commits."""
    async def attempt():
        calls.append(governed_window.now())
        await session.execute(text("INSERT INTO side_effect (post_id) VALUES (1)"))
        await session.execute(text("SELECT tagger_governed_complete_ok(1)"))
        return "done"
    return attempt


def _rows(sock, name):
    with closing(_admin(sock, name)) as c, c.cursor() as q:
        q.execute("SELECT count(*) FROM side_effect")
        effects = q.fetchone()[0]
        q.execute("SELECT tagging_status FROM post WHERE id = 1")
        return effects, q.fetchone()[0]


def _set_execute(sock, name, grant: bool):
    verb = "GRANT" if grant else "REVOKE"
    prep = "TO" if grant else "FROM"
    with closing(_admin(sock, name)) as c, c.cursor() as q:
        q.execute(f"{verb} EXECUTE ON FUNCTION {governed_window.COMPLETE_OK} {prep} kura")


def test_the_window_is_retried_through_and_the_transaction_lands_once(db):
    sock, name = db
    _set_execute(sock, name, grant=False)

    async def scenario():
        engine, sessions = _session(sock, name)
        try:
            async with sessions() as session:
                # The message really is Japanese here, as in production.
                with pytest.raises(DBAPIError) as raised:
                    await session.execute(text("SELECT tagger_governed_complete_ok(1)"))
                assert governed_window.insufficient_privilege(raised.value)
                assert "permission denied" not in str(raised.value.orig), str(raised.value.orig)
                assert "拒否" in str(raised.value.orig), str(raised.value.orig)
                await session.rollback()

                calls = []
                loop = asyncio.get_running_loop()
                loop.call_later(1.0, _set_execute, sock, name, True)
                result = await governed_window.run(session, _completion(session, calls),
                                                   governed_window.COMPLETE_OK, route="test")
                return result, calls
        finally:
            await engine.dispose()

    result, calls = asyncio.run(scenario())
    assert result == "done"
    assert len(calls) > 2
    assert _rows(sock, name) == (1, "done"), "every rolled-back attempt left nothing; the last one landed once"


def test_another_missing_privilege_fails_after_at_most_one_more_try(db):
    sock, name = db
    with closing(_admin(sock, name)) as c, c.cursor() as q:
        q.execute("REVOKE INSERT ON side_effect FROM kura")

    async def scenario():
        engine, sessions = _session(sock, name)
        try:
            async with sessions() as session:
                calls = []
                with pytest.raises(DBAPIError) as raised:
                    await governed_window.run(session, _completion(session, calls),
                                              governed_window.COMPLETE_OK, route="test")
                return raised.value, calls
        finally:
            await engine.dispose()

    error, calls = asyncio.run(scenario())
    assert governed_window.insufficient_privilege(error)
    assert len(calls) == 2
    assert _rows(sock, name) == (0, "pending")


def test_a_window_that_outlasts_the_deadline_commits_nothing(db, monkeypatch, caplog):
    sock, name = db
    _set_execute(sock, name, grant=False)
    monkeypatch.setattr(governed_window, "DEADLINE_SECONDS", 1.0)

    async def scenario():
        engine, sessions = _session(sock, name)
        try:
            async with sessions() as session:
                started = time.monotonic()
                with pytest.raises(governed_window.WindowExpired):
                    await governed_window.run(session, _completion(session, []),
                                              governed_window.COMPLETE_OK, route="complete")
                return time.monotonic() - started
        finally:
            await engine.dispose()

    with caplog.at_level(logging.WARNING, logger="kura.governed_window"):
        took = asyncio.run(scenario())
    assert took < 1.5
    assert _rows(sock, name) == (0, "pending")
    assert any(governed_window.EXPIRED_MARKER in r.getMessage() and "route=complete" in r.getMessage()
               for r in caplog.records)


def test_the_deadline_bounds_db_waits_from_the_first_attempt(db, monkeypatch):
    """A row lock held elsewhere: the attempt waits in the DB, and the deadline still ends it."""
    sock, name = db
    monkeypatch.setattr(governed_window, "DEADLINE_SECONDS", 1.0)
    holder = _admin(sock, name)
    holder.autocommit = False
    with holder.cursor() as q:
        q.execute("SELECT 1 FROM post WHERE id = 1 FOR UPDATE")

    async def scenario():
        engine, sessions = _session(sock, name)
        try:
            async with sessions() as session:
                async def attempt():
                    await session.execute(text("INSERT INTO side_effect (post_id) VALUES (1)"))
                    await session.execute(text("SELECT id FROM post WHERE id = 1 FOR NO KEY UPDATE"))
                    return "done"
                started = governed_window.now()
                with pytest.raises(governed_window.WindowExpired):
                    await governed_window.run(session, attempt, governed_window.COMPLETE_OK, route="test")
                took = governed_window.now() - started
                # The session is usable again afterwards (on a fresh connection).
                assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
                await session.rollback()
                return took
        finally:
            await engine.dispose()

    try:
        took = asyncio.run(scenario())
    finally:
        holder.rollback()
        holder.close()
    assert took < 1.5, took
    assert _rows(sock, name) == (0, "pending")
    with closing(_admin(sock, name)) as c, c.cursor() as q:
        q.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                  "AND usename = 'kura' AND state <> 'idle'")
        assert q.fetchone()[0] == 0, "the cancelled query is not left running"


def test_a_42501_at_commit_is_not_retried(db):
    """Only failures before the commit are rerun; a commit that fails is the caller's, once."""
    sock, name = db
    with closing(_admin(sock, name)) as c, c.cursor() as q:
        # A deferred trigger runs at COMMIT as kura and calls a function kura may not run.
        q.execute("CREATE FUNCTION side_effect_at_commit() RETURNS trigger LANGUAGE plpgsql AS "
                  "$$BEGIN PERFORM tagger_governed_complete_ok(NEW.post_id); RETURN NULL; END$$")
        q.execute("CREATE CONSTRAINT TRIGGER side_effect_at_commit AFTER INSERT ON side_effect "
                  "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION side_effect_at_commit()")
    _set_execute(sock, name, grant=False)

    async def scenario():
        engine, sessions = _session(sock, name)
        calls = []
        try:
            async with sessions() as session:
                async def attempt():
                    calls.append(1)
                    await session.execute(text("INSERT INTO side_effect (post_id) VALUES (1)"))
                    return "done"
                with pytest.raises(DBAPIError) as raised:
                    await governed_window.run(session, attempt, governed_window.COMPLETE_OK, route="test")
                return raised.value, calls
        finally:
            await engine.dispose()

    error, calls = asyncio.run(scenario())
    assert governed_window.insufficient_privilege(error)
    assert calls == [1]
    assert _rows(sock, name) == (0, "pending")


def test_the_routes_turn_an_expired_window_into_an_empty_claim_and_a_503(monkeypatch):
    """The worker sees "no work" for a claim and a retryable 503 for a completion, never a 500."""
    from fastapi import HTTPException

    from api.routers import tagging as routes
    from api.schemas.tagging import CompleteRequest

    seen = []

    async def expired(db, attempt, function, *, route, started=None):
        seen.append((route, function))
        raise governed_window.WindowExpired

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(governed_window, "run", expired)
    monkeypatch.setattr(routes, "report_node_state", nothing)

    async def scenario():
        claimed = await routes.claim(
            kind="auto_tag", limit=5, lease_seconds=300, host="primary", source=None, worker_id="w",
            profile="full", generation=1, release_id=1, provider=None, model_sha256=None,
            db=None, user=None)
        with pytest.raises(HTTPException) as raised:
            await routes.complete(CompleteRequest(id=1, kind="auto_tag", status="failed", error="x"),
                                  db=None, user=None)
        return claimed, raised.value

    claimed, error = asyncio.run(scenario())
    assert claimed.claimed == []
    assert error.status_code == 503
    assert seen == [("claim", governed_window.CLAIM), ("complete", governed_window.COMPLETE_RETRY)]
