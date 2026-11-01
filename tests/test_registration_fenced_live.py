"""A registration sent under a lease (tagger/learn.py) is accepted only inside the
transaction that sees the lease still held and the measured event still the tag's latest,
and one lifecycle event gets one result (docs/notes/tagger-learn-local.md step 3d).

Runs the endpoint against the schema baseline on a disposable PostgreSQL 18, connected as
the API's role, with two sessions racing where the guarantee is about concurrency.
"""
import asyncio
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parent))
from full_schema import cluster, load  # noqa: E402

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from api.errors import KuraError  # noqa: E402
from api.routers.tagger import RegistrationRequestIn, submit_registration_request  # noqa: E402
from api.tagger_core.transitions import acquire_lease, record_lifecycle_event, release_lease  # noqa: E402

TAG = "boat"


@pytest.fixture(scope="module")
def pg():
    with cluster() as c:
        yield c


def _as_kura(url: str) -> str:
    """The baseline's roles are NOLOGIN; the API connects as kura, and the governed
    lease functions check session_user, so the test does too."""
    u = urlsplit(url); q = parse_qs(u.query)
    subprocess.run(["/usr/lib/postgresql/18/bin/psql", "-h", q["host"][0], "-p", q["port"][0],
                    "-U", "postgres", "-d", u.path.lstrip("/"), "-XAtq", "-v", "ON_ERROR_STOP=1",
                    "-c", "ALTER ROLE kura LOGIN",
                    "-c", "INSERT INTO tag (id, category_id, creation_time, version) "
                          "VALUES (1, (SELECT id FROM tag_category ORDER BY id LIMIT 1), now(), 1)",
                    "-c", f"INSERT INTO tag_name (tag_name_id, tag_id, name, ord) VALUES (1, 1, '{TAG}', 0)"],
                   check=True, capture_output=True)
    return url.replace("postgres@", "kura@", 1)


@pytest.fixture
def db_url(pg):
    return _as_kura(load(*pg))


async def _session_factory(url):
    engine = create_async_engine(url, poolclass=sa.pool.NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _move(sessions, *steps) -> int:
    """Record lifecycle moves for TAG under a short lease; returns the last event id."""
    async with sessions() as db:
        lease = await acquire_lease(db, f"lifecycle:{TAG}", lease_seconds=30)
        try:
            for frm, to in steps:
                await record_lifecycle_event(db, tag=TAG, from_stage=frm, to_stage=to,
                                             reason="test", lease=lease)
        finally:
            await release_lease(db, lease)
        return (await db.execute(sa.text(
            "SELECT max(id) FROM tagger_lifecycle_event WHERE tag = :t"), {"t": TAG})).scalar_one()


def _body(event_id, lease=None, *, sha="a" * 64, scope=None):
    extra = {}
    if lease is not None:
        extra = {"lease_scope": scope or lease.scope, "fencing_token": lease.fencing_token}
    return RegistrationRequestIn(
        tag=TAG, source_host="box", artifact_ref=f"/heads/{sha[:8]}.pt", artifact_sha256=sha,
        metrics={"disposition": "hold"}, gt={"positive": 20, "negative": 50},
        lifecycle_event_id=event_id, **extra)


async def _send(sessions, body):
    async with sessions() as db:
        try:
            return (await submit_registration_request(body, db, None)).id
        except KuraError as e:
            return e.status


async def _count(sessions, event_id):
    async with sessions() as db:
        return (await db.execute(sa.text(
            "SELECT count(*) FROM tagger_registration_request WHERE lifecycle_event_id = :e"),
            {"e": event_id})).scalar_one()


def test_the_same_payload_twice_at_once_is_one_row(db_url):
    async def go():
        engine, sessions = await _session_factory(db_url)
        try:
            ev = await _move(sessions, (None, "candidate"), ("candidate", "training"))
            async with sessions() as db:
                lease = await acquire_lease(db, f"lifecycle:{TAG}", lease_seconds=3600)
            a, b = await asyncio.gather(_send(sessions, _body(ev, lease)), _send(sessions, _body(ev, lease)))
            assert isinstance(a, int) and a == b, (a, b)
            assert await _count(sessions, ev) == 1
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_two_results_for_one_event_at_once_leave_one(db_url):
    async def go():
        engine, sessions = await _session_factory(db_url)
        try:
            ev = await _move(sessions, (None, "candidate"), ("candidate", "training"))
            async with sessions() as db:
                lease = await acquire_lease(db, f"lifecycle:{TAG}", lease_seconds=3600)
            got = await asyncio.gather(_send(sessions, _body(ev, lease, sha="a" * 64)),
                                       _send(sessions, _body(ev, lease, sha="b" * 64)))
            assert sorted(type(g).__name__ for g in got) == ["int", "int"]
            assert 409 in got and any(g != 409 for g in got), got
            assert await _count(sessions, ev) == 1
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_a_lease_taken_over_or_expired_is_refused(db_url):
    async def go():
        engine, sessions = await _session_factory(db_url)
        try:
            ev = await _move(sessions, (None, "candidate"), ("candidate", "training"))
            async with sessions() as db:
                old = await acquire_lease(db, f"lifecycle:{TAG}", lease_seconds=1)
            await asyncio.sleep(1.5)
            assert await _send(sessions, _body(ev, old)) == 409, "an expired lease is not held"
            async with sessions() as db:
                new = await acquire_lease(db, f"lifecycle:{TAG}", lease_seconds=3600)
            assert new.fencing_token != old.fencing_token
            assert await _send(sessions, _body(ev, old)) == 409, "the old holder is fenced off"
            assert await _count(sessions, ev) == 0
            assert isinstance(await _send(sessions, _body(ev, new)), int)
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_a_measurement_of_a_superseded_event_is_refused(db_url):
    async def go():
        engine, sessions = await _session_factory(db_url)
        try:
            old_ev = await _move(sessions, (None, "candidate"), ("candidate", "training"))
            new_ev = await _move(sessions, ("training", "candidate"), ("candidate", "training"))
            assert new_ev > old_ev
            async with sessions() as db:
                lease = await acquire_lease(db, f"lifecycle:{TAG}", lease_seconds=3600)
            assert await _send(sessions, _body(old_ev, lease)) == 409
            assert await _count(sessions, old_ev) == 0
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_a_lease_on_another_scope_is_refused(db_url):
    async def go():
        engine, sessions = await _session_factory(db_url)
        try:
            ev = await _move(sessions, (None, "candidate"), ("candidate", "training"))
            async with sessions() as db:
                other = await acquire_lease(db, "lifecycle:something_else", lease_seconds=3600)
            assert await _send(sessions, _body(ev, other)) == 409
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_half_a_fence_is_a_client_error(db_url):
    async def go():
        engine, sessions = await _session_factory(db_url)
        try:
            ev = await _move(sessions, (None, "candidate"), ("candidate", "training"))
            body = _body(ev).model_copy(update={"lease_scope": f"lifecycle:{TAG}"})
            assert await _send(sessions, body) == 422
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_an_unfenced_registration_still_works(db_url):
    """Senders without a lease (older runners, backfills) keep today's behaviour."""
    async def go():
        engine, sessions = await _session_factory(db_url)
        try:
            ev = await _move(sessions, (None, "candidate"), ("candidate", "training"))
            assert isinstance(await _send(sessions, _body(ev)), int)
            assert await _count(sessions, ev) == 1
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_an_unfenced_sender_cannot_add_a_second_result_either(db_url):
    """One event, one result, whoever sends it (Codex 2026-11-01: a fenced result
    followed by an unfenced different one made two rows)."""
    async def go():
        engine, sessions = await _session_factory(db_url)
        try:
            ev = await _move(sessions, (None, "candidate"), ("candidate", "training"))
            async with sessions() as db:
                lease = await acquire_lease(db, f"lifecycle:{TAG}", lease_seconds=3600)
            first = await _send(sessions, _body(ev, lease, sha="a" * 64))
            assert isinstance(first, int) and first != 409
            assert await _send(sessions, _body(ev, sha="b" * 64)) == 409
            assert await _send(sessions, _body(ev, sha="a" * 64)) == first
            assert await _count(sessions, ev) == 1
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_a_lease_that_ends_while_waiting_for_the_event_lock_is_refused(db_url):
    """The expiry is judged after the event lock is taken, on the wall clock — not at
    the transaction's start (Codex 2026-11-01)."""
    async def go():
        engine, sessions = await _session_factory(db_url)
        try:
            ev = await _move(sessions, (None, "candidate"), ("candidate", "training"))
            async with sessions() as db:
                lease = await acquire_lease(db, f"lifecycle:{TAG}", lease_seconds=2)
            async with sessions() as blocker:
                await blocker.execute(sa.text(
                    "SELECT pg_advisory_xact_lock(hashtextextended("
                    "'tagger-registration:' || CAST(CAST(:ev AS bigint) AS text), 0))"), {"ev": ev})

                async def later():
                    await asyncio.sleep(3)
                    await blocker.rollback()
                async with sessions() as db:
                    await db.execute(sa.text("SELECT 1"))     # the transaction starts now
                    body = _body(ev, lease)
                    waiter = asyncio.create_task(later())
                    try:
                        await submit_registration_request(body, db, None)
                        got = 200
                    except KuraError as e:
                        got = e.status
                    await waiter
            assert got == 409
            assert await _count(sessions, ev) == 0
        finally:
            await engine.dispose()
    asyncio.run(go())
