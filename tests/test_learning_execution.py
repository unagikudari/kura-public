"""The real full schema, application session role, and asyncpg state boundary."""
import asyncio, importlib.util, json, sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).parent))
from test_tagger_learn_live import pg, world as old_world, _queue, _psql, TAG
from sqlalchemy import text
from tagger import learning_execution as execution
from api.tagger_core.transitions import acquire_lease,release_lease

@pytest.fixture
def world(old_world):
    return old_world

def test_payload_survives_connection_loss_and_stale_writer_is_fenced(world):
    async def scenario():
        ev=await _queue(world.sessions)
        async with world.sessions() as db:
            lease=await acquire_lease(db,"lifecycle:"+TAG,lease_seconds=60)
            r=await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=None,action="start")
            payload={"tag":TAG,"lifecycle_event_id":ev,"metrics":{"x":1}}
            r=await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=r,action="payload",payload=payload)
        async with world.sessions() as db:
            recovered=await execution.read(db,ev)
            assert recovered["pending_payload"]==payload
            assert recovered["payload_sha256"]==r["payload_sha256"]
            await db.rollback()
            with pytest.raises(Exception,match="learning_revision_changed"):
                await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=None,action="start")
            await db.rollback()
            with pytest.raises(Exception,match="learning_payload_changed"):
                await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=r,action="payload",
                    payload=dict(payload,metrics={"x":2}))
            await db.rollback()
            await release_lease(db,lease)
        async with world.sessions() as db:
            with pytest.raises(Exception,match="no lease|fencing"):
                await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=r,action="registration_unavailable")
    asyncio.run(scenario())

def test_backoff_is_database_time_and_not_reset_by_new_controller(world):
    async def scenario():
        ev=await _queue(world.sessions)
        async with world.sessions() as db:
            lease=await acquire_lease(db,"lifecycle:"+TAG,lease_seconds=60)
            r=await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=None,action="start")
            r=await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=r,action="child_failed")
            assert r["failure_count"]==1
            loaded=await execution.read(db,ev)
            assert not loaded["due"]
            await db.rollback()
            with pytest.raises(Exception,match="learning_backoff_active"):
                await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=r,action="start")
            await db.rollback()
            with pytest.raises(Exception):
                await db.execute(text("UPDATE tagger_learning_execution SET failure_count=0"))
            await db.rollback()
            await release_lease(db,lease)
    asyncio.run(scenario())

@pytest.mark.parametrize("payload",[{},{"tag":"other","lifecycle_event_id":1},[],None])
def test_invalid_payload_is_not_an_outbox(world,payload):
    async def scenario():
        ev=await _queue(world.sessions)
        async with world.sessions() as db:
            lease=await acquire_lease(db,"lifecycle:"+TAG,lease_seconds=60)
            r=await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=None,action="start")
            with pytest.raises(Exception,match="learning_payload_invalid"):
                await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=r,action="payload",payload=payload)
            await db.rollback()
            assert (await execution.read(db,ev))["revision"]==1
            await db.rollback();await release_lease(db,lease)
    asyncio.run(scenario())


def test_wall_clock_expiry_even_inside_old_transaction(world):
    async def scenario():
        ev=await _queue(world.sessions)
        async with world.sessions() as db:
            lease=await acquire_lease(db,"lifecycle:"+TAG,lease_seconds=1)
            await db.execute(text("SELECT now()"))
            await db.execute(text("SELECT pg_sleep(1.1)"))
            with pytest.raises(Exception,match="learning_lease_expired|fencing"):
                await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=None,action="start")
            await db.rollback()
            assert await execution.read(db,ev) is None
    asyncio.run(scenario())

def test_atomic_legacy_import_has_no_started_without_payload_window(world):
    async def scenario():
        ev=await _queue(world.sessions)
        async with world.sessions() as db:
            lease=await acquire_lease(db,"lifecycle:"+TAG,lease_seconds=60)
            payload={"tag":TAG,"lifecycle_event_id":ev}
            r=await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=None,action="import",payload=payload)
            assert r["revision"]==1 and r["pending_payload"]==payload
            await release_lease(db,lease)
        async with world.sessions() as db:
            r=await execution.read(db,ev)
            assert r["state"]=="registration_pending"
    asyncio.run(scenario())

def test_lost_http_ack_settles_without_local_state_or_retraining(world,monkeypatch):
    from tagger import learn
    from test_tagger_learn_live import ARGS,_payload
    ev=asyncio.run(_queue(world.sessions))
    original=learn.post_registration
    def committed_but_reply_lost(payload,lease):
        status,reply=original(payload,lease)
        assert status==200
        raise ConnectionResetError("synthetic lost acknowledgement")
    monkeypatch.setattr(learn,"run_child",lambda *args:{"outcome":"hold","registration":_payload()})
    monkeypatch.setattr(learn,"post_registration",committed_but_reply_lost)
    assert asyncio.run(execution.run(TAG,ev,{},None,ARGS))=="failed"
    monkeypatch.setattr(learn,"run_child",lambda *args:pytest.fail("trained twice"))
    assert asyncio.run(execution.settle_consumed(ARGS))==1
    async def observed():
        async with world.sessions() as db: return await execution.read(db,ev)
    assert asyncio.run(observed())["state"]=="registered"
    assert not world.state_path.exists()
    assert asyncio.run(execution.settle_consumed(ARGS))==0

def test_conflicting_committed_payload_is_not_recovered_as_success(world):
    from tagger import learn
    from test_tagger_learn_live import ARGS,_payload
    async def scenario():
        ev=await _queue(world.sessions)
        async with world.sessions() as db:
            lease=await acquire_lease(db,"lifecycle:"+TAG,lease_seconds=60)
            payload=dict(_payload(),lifecycle_event_id=ev)
            await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=None,action="import",payload=payload)
        code,_=await asyncio.to_thread(learn.post_registration,dict(payload,artifact_sha256="b"*64),lease)
        assert code==200
        async with world.sessions() as db:await release_lease(db,lease)
        await execution.settle_consumed(ARGS)
        async with world.sessions() as db:
            r=await execution.read(db,ev)
            assert r["state"]=="refused" and r["pending_payload"]==payload
    asyncio.run(scenario())

def test_projection_is_database_only_and_does_not_claim_a_live_worker(world):
    from api.services.learning_execution import current
    async def scenario():
        ev=await _queue(world.sessions)
        async with world.sessions() as db:
            lease=await acquire_lease(db,"lifecycle:"+TAG,lease_seconds=60)
            await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=None,action="start")
            row=await current(db)
            assert row["event"]==ev and row["source"]=="canonical_learning_execution"
            assert row["runtime_verified"] is False and row["fresh"] is False
            await db.rollback();await release_lease(db,lease)
    asyncio.run(scenario())


@pytest.mark.parametrize("explicit_null",[False,True])
def test_null_and_omitted_registration_fields_match_the_api(world,explicit_null):
    from tagger import learn
    from test_tagger_learn_live import ARGS
    async def scenario():
        ev=await _queue(world.sessions)
        payload=dict(tag=TAG,lifecycle_event_id=ev,source_host="box",artifact_ref="/h.pt",artifact_sha256="a"*64)
        if explicit_null:payload.update(metrics=None,gt=None)
        async with world.sessions() as db:
            lease=await acquire_lease(db,"lifecycle:"+TAG,lease_seconds=60)
            await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=None,action="import",payload=payload)
        code,_=await asyncio.to_thread(learn.post_registration,payload,lease)
        assert code==200
        async with world.sessions() as db:await release_lease(db,lease)
        await execution.settle_consumed(ARGS)
        async with world.sessions() as db:
            assert (await execution.read(db,ev))["state"]=="registered"
    asyncio.run(scenario())

def test_externally_consumed_event_without_outbox_does_not_starve_recovery(world):
    from tagger import learn
    from test_tagger_learn_live import ARGS,_payload
    async def scenario():
        ev=await _queue(world.sessions)
        async with world.sessions() as db:
            lease=await acquire_lease(db,"lifecycle:"+TAG,lease_seconds=60)
            await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=None,action="start")
        code,_=await asyncio.to_thread(learn.post_registration,dict(_payload(),lifecycle_event_id=ev),lease)
        assert code==200
        async with world.sessions() as db:await release_lease(db,lease)
        assert await execution.settle_consumed(ARGS)==1
        assert await execution.settle_consumed(ARGS)==0
        async with world.sessions() as db:
            r=await execution.read(db,ev)
            assert r["state"]=="settled" and r["pending_payload"] is None
    asyncio.run(scenario())

def test_temporary_human_hold_does_not_permanently_finish_execution(world):
    from tagger import learn
    from test_tagger_learn_live import ARGS
    async def scenario():
        ev=await _queue(world.sessions)
        async with world.sessions() as db:
            lease=await acquire_lease(db,"lifecycle:"+TAG,lease_seconds=60)
            await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=None,action="start")
            await release_lease(db,lease)
    asyncio.run(scenario())
    _psql(world.url,"SET SESSION AUTHORIZATION kura",
        "SELECT tagger_governed_record_candidate_disposition('boat','defer','owner','pause','owner','human',now()+interval '1 day',NULL,NULL)")
    # The branch returns human for a standing temporary disposition.
    async def held():
        async with world.sessions() as db:
            ev=(await db.execute(text("SELECT event_id FROM tagger_learning_execution"))).scalar_one()
        assert await execution.run(TAG,ev,{},None,ARGS)=="human"
        async with world.sessions() as db:
            assert (await execution.read(db,ev))["state"]=="measuring"
    asyncio.run(held())


def test_two_controllers_cannot_advance_the_same_revision(world):
    async def scenario():
        ev=await _queue(world.sessions)
        async with world.sessions() as db:
            lease=await acquire_lease(db,"lifecycle:"+TAG,lease_seconds=60)
        async def start():
            async with world.sessions() as db:
                try:
                    await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=None,action="start")
                    return "started"
                except Exception as exc:
                    assert "learning_revision_changed" in str(exc)
                    return "stale"
        assert sorted(await asyncio.gather(start(),start()))==["stale","started"]
        async with world.sessions() as db:
            assert (await execution.read(db,ev))["revision"]==1
            assert (await db.execute(text("SELECT count(*) FROM tagger_learning_execution_event"))).scalar_one()==1
            await db.rollback();await release_lease(db,lease)
    asyncio.run(scenario())


def test_pending_event_cannot_be_settled_and_payload_cannot_bypass_backoff(world):
    async def scenario():
        ev=await _queue(world.sessions)
        async with world.sessions() as db:
            lease=await acquire_lease(db,"lifecycle:"+TAG,lease_seconds=60)
            r=await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=None,action="start")
            with pytest.raises(Exception,match="learning_event_still_pending"):
                await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=r,action="settle")
            await db.rollback()
            r=await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=r,action="child_failed")
            with pytest.raises(Exception,match="learning_backoff_active"):
                await execution.step(db,tag=TAG,event_id=ev,lease=lease,previous=r,action="payload",
                    payload={"tag":TAG,"lifecycle_event_id":ev})
            await db.rollback()
            assert (await execution.read(db,ev))["revision"]==2
            await db.rollback();await release_lease(db,lease)
    asyncio.run(scenario())


def test_migration_preserves_canonical_only_and_application_acl(world):
    policy = pytest.importorskip('replication.publication_policy')   # the replica topology is private: absent in the public tree
    CANONICAL_ONLY_TABLES = policy.CANONICAL_ONLY_TABLES
    tables=("tagger_learning_execution","tagger_learning_execution_event")
    assert set(tables)<=CANONICAL_ONLY_TABLES
    topology=(Path(__file__).parents[1]/"replication/kura_topology.sql").read_text()
    for table in tables:
        assert "'"+table+"'" in topology
        assert _psql(world.url,"SELECT tableowner FROM pg_tables WHERE tablename='"+table+"'")=="kura_reconciler"
        assert _psql(world.url,"SELECT has_table_privilege('kura','"+table+"','SELECT')")== "t"
        for privilege in ("INSERT","UPDATE","DELETE","TRUNCATE"):
            assert _psql(world.url,"SELECT has_table_privilege('kura','"+table+"','"+privilege+"')")=="f"
    assert _psql(world.url,"SELECT count(*) FROM pg_publication_tables WHERE tablename IN ('tagger_learning_execution','tagger_learning_execution_event')")=="0"


def test_replica_without_support_table_has_no_learning_observation(pg):
    from full_schema import load
    from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
    from sqlalchemy.pool import NullPool
    from api.services.learning_execution import current
    url=load(*pg)
    _psql(url,"ALTER ROLE kura LOGIN")
    async def scenario():
        engine=create_async_engine(url.replace("postgres@","kura@",1),poolclass=NullPool)
        try:
            async with async_sessionmaker(engine)() as db:
                assert await current(db) is None
                assert (await db.execute(text("SELECT 1"))).scalar_one()==1
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_parallel_worker_routing_keeps_assignment_and_lifecycle_lease(world, monkeypatch, tmp_path):
    from threading import Event
    from types import SimpleNamespace

    from tagger import dedicated_protocol, learn, remote_learning

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    nodeb = SimpleNamespace(lease_hours=8, worker_profile={"host": "nodeb"})
    p52 = SimpleNamespace(lease_hours=8, worker_profile={"host": "p52"})
    started, resume = Event(), Event()

    def measure(tag, event, timeout, args):
        assert args is nodeb
        started.set()
        assert resume.wait(10)
        return {"outcome": "failed"}

    monkeypatch.setattr(learn, "run_child", measure)

    async def scenario():
        event = await _queue(world.sessions)
        root = remote_learning.job_root(TAG, event)
        root.mkdir(parents=True)
        dedicated_protocol.write_once(root / "inputs.json", {
            "schema": "tagger-dispatch-inputs/1",
            "tag": TAG,
            "event": event,
            "worker": nodeb.worker_profile,
        })
        assert await execution.run(TAG, event, {}, None, p52) == "other_worker"
        async with world.sessions() as db:
            assert await execution.read(db, event) is None

        main = asyncio.create_task(execution.run(TAG, event, {}, None, nodeb))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            assert await execution.run(TAG, event, {}, None, p52) == "busy"
        finally:
            resume.set()
            assert await main == "failed"
        async with world.sessions() as db:
            assert (await execution.read(db, event))["state"] == "retry_wait"

    asyncio.run(scenario())
