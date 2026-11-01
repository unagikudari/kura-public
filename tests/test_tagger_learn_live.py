"""tagger/learn.py's measure step against the schema baseline (docs/notes/tagger-learn-local.md):
what is queued, what each kind of child result does to the database and the state file,
and that a lost reply is answered by sending the same payload again, not by measuring again.

The child (the tagger venv's vnext_runner) is replaced by a function returning its JSON line;
registrations go to the real endpoint function in the API's transaction.
"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parent))
from full_schema import cluster, load  # noqa: E402

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from api.errors import KuraError  # noqa: E402
from api.routers.tagger import RegistrationRequestIn, submit_registration_request  # noqa: E402
from api.tagger_core.transitions import acquire_lease, record_lifecycle_event, release_lease  # noqa: E402
from tagger import learn, reconciler  # noqa: E402

TAG = "boat"
ARGS = SimpleNamespace(lease_hours=8.0, tagger_python="/nonexistent/python")


@pytest.fixture(scope="module")
def pg():
    with cluster() as c:
        yield c


def _psql(url, *sql):
    u = urlsplit(url); q = parse_qs(u.query)
    cmd = ["/usr/lib/postgresql/18/bin/psql", "-h", q["host"][0], "-p", q["port"][0], "-U", "postgres",
           "-d", u.path.lstrip("/"), "-XAtq", "-v", "ON_ERROR_STOP=1"]
    for s in sql:
        cmd += ["-c", s]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def world(pg, monkeypatch, tmp_path):
    url = load(*pg)
    _psql(url, "ALTER ROLE kura LOGIN",
          "INSERT INTO tag (id, category_id, creation_time, version) "
          "VALUES (1, (SELECT id FROM tag_category ORDER BY id LIMIT 1), now(), 1)",
          f"INSERT INTO tag_name (tag_name_id, tag_id, name, ord) VALUES (1, 1, '{TAG}', 0)")
    kura = url.replace("postgres@", "kura@", 1)
    engine = create_async_engine(kura, poolclass=sa.pool.NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(learn, "async_session", sessions)
    sent = []

    def _post(payload, lease):
        """The real endpoint in its own loop and session, as the API would run it."""
        sent.append(payload)

        async def go():
            e = create_async_engine(kura, poolclass=sa.pool.NullPool)
            try:
                async with async_sessionmaker(e, expire_on_commit=False)() as db:
                    body = RegistrationRequestIn(**payload, lease_scope=lease.scope,
                                                 fencing_token=lease.fencing_token)
                    try:
                        return 200, {"id": (await submit_registration_request(body, db, None)).id}
                    except KuraError as ex:
                        return ex.status, ex.error
            finally:
                await e.dispose()
        return asyncio.run(go())

    monkeypatch.setattr(learn, "post_registration", _post)
    w = SimpleNamespace(url=url, sessions=sessions, engine=engine, sent=sent,
                        state_path=tmp_path / "learn.json")
    yield w
    asyncio.run(engine.dispose())


async def _queue(sessions) -> int:
    async with sessions() as db:
        lease = await acquire_lease(db, f"lifecycle:{TAG}", lease_seconds=30)
        try:
            await record_lifecycle_event(db, tag=TAG, from_stage=None, to_stage="candidate",
                                         reason="test", lease=lease)
            await record_lifecycle_event(db, tag=TAG, from_stage="candidate", to_stage="training",
                                         reason="test", lease=lease)
        finally:
            await release_lease(db, lease)
        return (await db.execute(sa.text(
            "SELECT max(id) FROM tagger_lifecycle_event WHERE tag = :t"), {"t": TAG})).scalar_one()


async def _queued(sessions):
    async with sessions() as db:
        return [(r["tag"], r["event_id"])
                for r in (await db.execute(sa.text(reconciler.QUEUED_TRAINING_SQL))).mappings()]


async def _stage(sessions):
    async with sessions() as db:
        return (await db.execute(sa.text(
            "SELECT to_stage FROM tagger_lifecycle_event WHERE tag = :t ORDER BY id DESC LIMIT 1"),
            {"t": TAG})).scalar_one()


def _child(result):
    return lambda tag, event_id, timeout_s, args: result


def _payload(sha="a" * 64):
    return {"tag": TAG, "source_host": "box", "artifact_ref": "/h.pt", "artifact_sha256": sha,
            "metrics": {"disposition": "hold"}, "gt": {"positive": 20, "negative": 50}}


def _measure(w, ev):
    state = learn.load_state(w.state_path)
    out = asyncio.run(learn.measure_one(TAG, ev, state, w.state_path, ARGS))
    return out, learn.load_state(w.state_path)


def test_a_result_is_registered_under_the_lease_and_leaves_the_queue(world, monkeypatch):
    ev = asyncio.run(_queue(world.sessions))
    assert asyncio.run(_queued(world.sessions)) == [(TAG, ev)]
    monkeypatch.setattr(learn, "run_child", _child({"outcome": "hold", "registration": _payload()}))
    out, state = _measure(world, ev)
    assert out == "registered"
    assert world.sent[0]["lifecycle_event_id"] == ev, "the event the lease was taken for"
    assert state["pending"] == {} and state["backoff"] == {}
    assert asyncio.run(_queued(world.sessions)) == [], "a registration for the event consumes it"


def test_a_lost_reply_is_answered_by_the_same_payload_not_a_new_measurement(world, monkeypatch):
    ev = asyncio.run(_queue(world.sessions))
    key = learn.key_of(TAG, ev)
    # The POST went through but its reply was lost: the payload is still pending.
    payload = dict(_payload(), lifecycle_event_id=ev)
    state = learn.load_state(world.state_path)
    state["pending"][key] = payload
    learn.save_state(world.state_path, state)

    async def first():
        async with world.sessions() as db:
            lease = await acquire_lease(db, f"lifecycle:{TAG}", lease_seconds=60)
        code, reply = await asyncio.to_thread(learn.post_registration, payload, lease)
        async with world.sessions() as db:
            await release_lease(db, lease)
        return code, reply
    code, reply = asyncio.run(first())
    assert code == 200
    # Queue membership is decided by the database, so the next pass sees it consumed and
    # leaves it; a pass that held the pending payload before the DB showed it re-sends it.
    monkeypatch.setattr(learn, "run_child", lambda *a: pytest.fail("measured again"))
    monkeypatch.setattr(learn, "still_queued", _always_queued)
    out, state = _measure(world, ev)
    assert out == "registered" and key not in state["pending"]
    assert world.sent[-1] == payload, "the same payload, unchanged"
    assert _psql(world.url, f"SELECT count(*) FROM tagger_registration_request "
                            f"WHERE lifecycle_event_id = {ev}") == "1"


async def _always_queued(db, tag, event_id):
    return True


def test_a_child_that_fails_backs_off_and_touches_nothing(world, monkeypatch):
    ev = asyncio.run(_queue(world.sessions))
    monkeypatch.setattr(learn, "run_child", _child(None))
    out, state = _measure(world, ev)
    assert out == "failed"
    b = state["backoff"][learn.key_of(TAG, ev)]
    assert b["n"] == 1 and b["next"] > 0
    assert asyncio.run(_stage(world.sessions)) == "training", "still queued"
    assert asyncio.run(_queued(world.sessions)) == [(TAG, ev)]
    out, state = _measure(world, ev)
    assert state["backoff"][learn.key_of(TAG, ev)]["n"] == 2, "the wait grows"


def test_a_structural_stop_defers_for_a_day_then_leaves_training(world, monkeypatch):
    ev = asyncio.run(_queue(world.sessions))
    esc = [{"code": "E0", "blocking": True, "summary": "構造前提が崩れている: release_row_present",
            "detail": {"check": "release_row_present", "why": "稼働 release 行が引けない"}}]
    monkeypatch.setattr(learn, "run_child", _child({"outcome": "escalated", "registration": None,
                                                    "escalations": esc}))
    out, _ = _measure(world, ev)
    assert out == "deferred"
    assert asyncio.run(_stage(world.sessions)) == "candidate"
    row = _psql(world.url, "SELECT disposition, actor_type, valid_until > now() + interval '23 hours' "
                           "FROM tagger_candidate_disposition_current WHERE tag = 'boat'")
    assert row == "defer|automation|t"


def test_an_unverifiable_check_is_an_infrastructure_failure_not_a_defer(world, monkeypatch):
    ev = asyncio.run(_queue(world.sessions))
    esc = [{"code": "E0", "blocking": True, "summary": "構造前提を確認できなかった: release_row_present",
            "detail": {"missing_check": "release_row_present"}}]
    monkeypatch.setattr(learn, "run_child", _child({"outcome": "escalated", "registration": None,
                                                    "escalations": esc}))
    out, state = _measure(world, ev)
    assert out == "failed" and learn.key_of(TAG, ev) in state["backoff"]
    assert _psql(world.url, "SELECT count(*) FROM tagger_candidate_disposition") == "0"


def test_a_human_decision_wins_over_the_automatic_defer(world, monkeypatch):
    ev = asyncio.run(_queue(world.sessions))
    _psql(world.url, "SET SESSION AUTHORIZATION kura",
          "SELECT tagger_governed_record_candidate_disposition('boat', 'proceed', 'owner', "
          "'the owner wants this trained', 'owner', 'human', NULL, NULL, NULL)")
    esc = [{"code": "E0", "blocking": True, "summary": "構造前提が崩れている: tag_in_vocab",
            "detail": {"check": "tag_in_vocab"}}]
    monkeypatch.setattr(learn, "run_child", _child({"outcome": "escalated", "registration": None,
                                                    "escalations": esc}))
    out, state = _measure(world, ev)
    assert out == "human"
    import time
    assert state["backoff"][learn.key_of(TAG, ev)]["next"] > time.time() + 23 * 3600, "waits a day"
    assert _psql(world.url, "SELECT disposition, actor_type FROM tagger_candidate_disposition_current "
                            "WHERE tag = 'boat'") == "proceed|human", "nothing written over it"
    assert asyncio.run(_stage(world.sessions)) == "training"


def test_unchanged_gt_settles_the_tag(world, monkeypatch):
    ev = asyncio.run(_queue(world.sessions))
    monkeypatch.setattr(learn, "run_child", _child({"outcome": "deployable(gt-unchanged)",
                                                    "registration": None}))
    out, _ = _measure(world, ev)
    assert out == "settled"
    assert asyncio.run(_stage(world.sessions)) == "candidate"


def test_a_tag_whose_lease_is_held_elsewhere_is_left_alone(world, monkeypatch):
    ev = asyncio.run(_queue(world.sessions))

    async def hold():
        async with world.sessions() as db:
            return await acquire_lease(db, f"lifecycle:{TAG}", lease_seconds=600)
    asyncio.run(hold())
    monkeypatch.setattr(learn, "run_child", lambda *a: pytest.fail("measured under another lease"))
    out, _ = _measure(world, ev)
    assert out == "busy"


def test_a_judged_registration_settles_and_an_unjudged_one_waits(world):
    ev = asyncio.run(_queue(world.sessions))
    _psql(world.url, "SET SESSION AUTHORIZATION kura",
          f"SELECT tagger_governed_submit_registration('boat', 'box', '/h.pt', '{'c' * 64}', "
          f"'{{}}'::jsonb, '{{\"positive\": 1, \"negative\": 1}}'::jsonb, {ev})")

    async def settle():
        async with world.sessions() as db:
            return await reconciler.settle_judged_training(db)
    assert asyncio.run(settle()) == [], "pending: not judged yet"
    assert asyncio.run(_stage(world.sessions)) == "training"
    _psql(world.url, "SET SESSION AUTHORIZATION kura",
          "SELECT tagger_governed_set_registration_status("
          "(SELECT id FROM tagger_registration_request WHERE tag = 'boat'), 'rejected', 'floor', NULL)")
    assert asyncio.run(settle()) == [TAG]
    assert asyncio.run(_stage(world.sessions)) == "candidate"


def test_the_state_file_is_replaced_whole(tmp_path):
    p = tmp_path / "s" / "learn.json"
    learn.save_state(p, {"backoff": {"x@1": {"n": 1}}, "pending": {}})
    assert json.loads(p.read_text())["backoff"] == {"x@1": {"n": 1}}
    assert not p.with_suffix(".tmp").exists()
    assert learn.load_state(tmp_path / "missing.json") == {"backoff": {}, "pending": {}}


def test_the_child_gets_the_apis_database_as_both_dsns():
    env = learn.child_env({"DATABASE_URL": "postgresql+asyncpg://kura:pw@127.0.0.1:5432/kura"})
    assert env["KURA_CANONICAL_DSN"] == env["KURA_READ_DSN"] == "postgresql://kura:pw@127.0.0.1:5432/kura"
    kept = learn.child_env({"DATABASE_URL": "postgresql+asyncpg://a@b/c", "KURA_READ_DSN": "host=replica"})
    assert kept["KURA_READ_DSN"] == "host=replica"


def test_the_last_json_line_is_the_result():
    assert learn.parse_child_output("noise\n{\"outcome\": \"hold\"}\n") == {"outcome": "hold"}
    assert learn.parse_child_output("Traceback ...\n") is None


def test_an_unreachable_api_keeps_the_payload_and_backs_off(world, monkeypatch):
    ev = asyncio.run(_queue(world.sessions))
    monkeypatch.setattr(learn, "run_child", _child({"outcome": "hold", "registration": _payload()}))

    def _down(payload, lease):
        raise ConnectionRefusedError("api down")
    monkeypatch.setattr(learn, "post_registration", _down)
    out, state = _measure(world, ev)
    key = learn.key_of(TAG, ev)
    assert out == "failed"
    assert state["pending"][key]["lifecycle_event_id"] == ev, "kept for the next pass"
    assert state["backoff"][key]["n"] == 1
    assert not learn.due(state, key, __import__("time").time()), "and it waits like any failure"


def test_settle_does_not_apply_an_old_verdict_to_a_newer_request(world, monkeypatch):
    """Between reading and taking the lease the tag may have been re-queued; the verdict
    read for the old event must not send the new request back (Codex 2026-11-01)."""
    ev = asyncio.run(_queue(world.sessions))
    real = reconciler._settle_rows
    calls = []

    async def stale_then_real(db, tag=None):
        calls.append(tag)
        if tag is None:   # the first read saw an older event that had a judged result
            return [{"tag": TAG, "event_id": ev - 1, "held": False, "judged": 99}]
        return await real(db, tag)
    monkeypatch.setattr(reconciler, "_settle_rows", stale_then_real)

    async def settle():
        async with world.sessions() as db:
            return await reconciler.settle_judged_training(db)
    assert asyncio.run(settle()) == []
    assert calls == [None, TAG], "re-read under the lease"
    assert asyncio.run(_stage(world.sessions)) == "training"
