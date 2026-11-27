"""Private PG18 + asyncpg as the real API role; never a live endpoint.

Receipt intake does not rewrite an owner's terminal decision or approve a model.
"""
import asyncio
import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from fastapi import Response

sys.path.insert(0, str(Path(__file__).parent))
from full_schema import cluster, load
from test_qualification_evidence import report, A
from api.tagger_core import qualification_evidence as q
from api.services import qualification_evidence as service
from api.routers import tagger_qualification as routes
from api.errors import KuraError

migration = importlib.import_module('api.alembic.versions.0199_qualification_evidence')


@pytest.fixture(scope='module')
def pg():
    with cluster() as c:
        yield c


@pytest.fixture
def db_url(pg):
    url = load(*pg)
    u = urlsplit(url); query = parse_qs(u.query)
    subprocess.run(['/usr/lib/postgresql/18/bin/psql', '-h', query['host'][0],
        '-p', query['port'][0], '-U', 'postgres', '-d', u.path.lstrip('/'), '-XAtq',
        '-v', 'ON_ERROR_STOP=1', '-c', "ALTER ROLE kura LOGIN;"
        "INSERT INTO tag(id,category_id,creation_time,version) VALUES"
        "(1,(SELECT id FROM tag_category ORDER BY id LIMIT 1),now(),1);"
        "INSERT INTO tag_name(tag_name_id,tag_id,name,ord) VALUES(1,1,'fixture',0);"
        "DO $$BEGIN CREATE ROLE replicator; EXCEPTION WHEN duplicate_object THEN NULL; END$$;"
        "ALTER DEFAULT PRIVILEGES FOR ROLE kura GRANT SELECT ON TABLES TO replicator;"],
        check=True, capture_output=True)
    return url.replace('postgres@','kura@',1)


async def setup(url):
    engine = create_async_engine(url, poolclass=sa.pool.NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        assert (await db.execute(sa.text('SELECT session_user'))).scalar_one() == 'kura'
        await db.execute(sa.text("SET LOCAL lock_timeout='2s'"))
        for statement in migration.STATEMENTS:
            await db.execute(sa.text(statement))
        id = (await db.execute(sa.text("SELECT tagger_governed_submit_registration("
            "'fixture','fixture','/fixture.pt',:sha,CAST(:m AS jsonb),CAST(:g AS jsonb),NULL)"),
            {'sha':A,'m':json.dumps({'threshold':.25}),'g':json.dumps({'positive':20,'negative':30})})).scalar_one()
        await db.execute(sa.text("SELECT tagger_governed_set_registration_status(:id,'rejected','needs evidence','owner_decision')"), {'id':id})
        await db.commit()
    return engine, sessions, id


async def original(db, id):
    return (await db.execute(sa.text('SELECT to_jsonb(r) FROM tagger_registration_request r WHERE id=:id'),{'id':id})).scalar_one()


async def send(sessions, id, kind='runtime', value=None, user_id=42):
    async with sessions() as db:
        ctx = await service.registration_context(db,id)
        return await routes.attach_evidence(id, routes.EvidenceIn(expected_context_sha256=q.digest(ctx),
            kind=kind,report=value or report(kind)),Response(),db,SimpleNamespace(user_id=user_id))


@pytest.mark.parametrize('parity_state', [None, False, True])
def test_asyncpg_api_role_replay_keeps_terminal_candidate_and_original_measurement(db_url, parity_state):
    async def go():
        engine,sessions,id = await setup(db_url)
        try:
            async with sessions() as db:
                before = await original(db,id)
                assert before['status']=='rejected' and before['review_class']=='owner_decision'
            value=report('runtime'); value['parity_passed']=parity_state
            a,b = await asyncio.gather(send(sessions,id,value=value),send(sessions,id,value=value,user_id=43))
            assert a==b and a['first_submitted_by'] in (42,43)
            assert a['admission']=='not_granted' and not a['publication_eligible']
            async with sessions() as db:
                assert await original(db,id)==before
                assert (await db.execute(sa.text('SELECT count(*) FROM tagger_qualification_evidence'))).scalar_one()==1
                assert not (await db.execute(sa.text("SELECT has_table_privilege('replicator','tagger_qualification_evidence','SELECT')"))).scalar_one()
                response=Response()
                listing=await routes.evidence(id,response,db,SimpleNamespace(user_id=42))
                assert response.headers['cache-control']=='private, no-store'
                assert listing['receipts'][0]['receipt_sha256']==a['receipt_sha256']
                exact=await routes.evidence_by_ref(id,a['receipt_sha256'],Response(),db,SimpleNamespace(user_id=42))
                assert exact['receipt']['body']['provenance']=='caller_assertion'
                assert exact['receipt']['body']['report']['parity_passed'] is parity_state
                assert json.loads(exact['receipt']['canonical_body'])['report']['parity_passed'] is parity_state
                import hashlib
                assert hashlib.sha256(exact['receipt']['canonical_body'].encode()).hexdigest()==a['receipt_sha256']
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_reports_are_bound_review_references_exist_and_failure_is_not_admission(db_url):
    async def go():
        engine,sessions,id = await setup(db_url)
        try:
            async with sessions() as db:
                ctx=await service.registration_context(db,id)
                with pytest.raises(KuraError) as error:
                    await routes.attach_evidence(id,routes.EvidenceIn(expected_context_sha256='0'*64,
                        kind='runtime',report=report('runtime')),Response(),db,SimpleNamespace(user_id=42))
                assert error.value.status==409 and error.value.error=='registration_context_mismatch'
            bad=report('independent_review')
            with pytest.raises(KuraError,match='qualification_review_evidence_mismatch'):
                await send(sessions,id,'independent_review',bad)
            runtime=report('runtime');runtime.update(completed=2,oom_events=1,parity_passed=False)
            receipt=await send(sessions,id,'runtime',runtime)
            bad['evidence_sha256s']=[receipt['receipt_sha256']]
            review=await send(sessions,id,'independent_review',bad)
            uncertain=await send(sessions,id,'human_sample')
            assert all(r['admission']=='not_granted' for r in (receipt,review,uncertain))
            async with sessions() as db:
                assert (await original(db,id))['status']=='rejected'
                assert (await db.execute(sa.text('SELECT count(*) FROM tagger_qualification_evidence'))).scalar_one()==3
        finally:
            await engine.dispose()
    asyncio.run(go())


@pytest.mark.parametrize('statement',[
    'UPDATE tagger_qualification_evidence SET submitted_by=44',
    'DELETE FROM tagger_qualification_evidence',
    'TRUNCATE tagger_qualification_evidence',
])
def test_existing_evidence_is_immutable_to_api_role(db_url,statement):
    async def go():
        engine,sessions,id = await setup(db_url)
        try:
            await send(sessions,id)
            async with sessions() as db:
                with pytest.raises(sa.exc.DBAPIError,match='qualification_evidence_immutable'):
                    await db.execute(sa.text(statement))
                await db.rollback()
                assert (await db.execute(sa.text('SELECT count(*) FROM tagger_qualification_evidence'))).scalar_one()==1
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_context_swap_and_forged_digest_are_rejected_in_database(db_url):
    async def go():
        engine,sessions,id = await setup(db_url)
        try:
            async with sessions() as db:
                ctx=await service.registration_context(db,id)
                ctx['metrics']['threshold']=.5
                value=report('runtime');value['threshold']=.5
                env=q.assemble(ctx,q.digest(ctx),'runtime',value)
                sql=sa.text('INSERT INTO tagger_qualification_evidence '
                    '(receipt_sha256,registration_id,context_sha256,context_canonical,context,canonical_body,body,submitted_by) '
                    'VALUES(:ref,:id,:ctx,:c,CAST(:j AS jsonb),:wire,CAST(:b AS jsonb),42)')
                params={'ref':env['receipt_sha256'],'id':id,'ctx':env['context_sha256'],
                    'c':env['context_canonical'],'j':env['context_canonical'],
                    'wire':env['canonical_body'],'b':env['canonical_body']}
                with pytest.raises(sa.exc.DBAPIError,match='qualification_context_mismatch'):
                    await db.execute(sql,params)
                await db.rollback()
                ctx=await service.registration_context(db,id)
                env=q.assemble(ctx,q.digest(ctx),'runtime',report('runtime'))
                params.update(ref='0'*64,ctx=env['context_sha256'],c=env['context_canonical'],
                    j=env['context_canonical'],wire=env['canonical_body'],b=env['canonical_body'])
                with pytest.raises(sa.exc.IntegrityError):
                    await db.execute(sql,params)
                await db.rollback()
                assert (await db.execute(sa.text('SELECT count(*) FROM tagger_qualification_evidence'))).scalar_one()==0
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_http_auth_shape_canonical_and_submitter_boundaries(db_url,monkeypatch):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from httpx import AsyncClient, ASGITransport
    async def go():
        engine,sessions,id = await setup(db_url)
        app=FastAPI(); app.include_router(routes.router)
        @app.exception_handler(KuraError)
        async def handle(request,exc):
            return JSONResponse(status_code=exc.status,content={'error':exc.error})
        async def database():
            async with sessions() as db:
                yield db
        app.dependency_overrides[routes.get_db]=database
        base=f'/api/tagger/registration-request/{id}'
        try:
            async with AsyncClient(transport=ASGITransport(app=app),base_url='http://fixture') as client:
                for suffix in ('/qualification-context','/qualification-evidence','/qualification-evidence/'+'a'*64):
                    assert (await client.get(base+suffix)).status_code==401
                app.dependency_overrides[routes.get_current_user]=lambda: SimpleNamespace(user_id=42)
                ctx=(await client.get(base+'/qualification-context')).json()
                body=dict(expected_context_sha256=ctx['context_sha256'],kind='runtime',report=report('runtime'))
                for raw in ('{"report":NaN}', '{"report":1e999}', '{"report":[],"extra":NaN}'):
                    malformed=await client.post(base+'/qualification-evidence',content=raw,headers={'Content-Type':'application/json'})
                    assert malformed.status_code==422 and malformed.json()['error']=='qualification_request_invalid'
                forged=body|{'submitted_by':999}
                assert (await client.post(base+'/qualification-evidence',json=forged)).status_code==422
                for invalid in ('0','-1','9223372036854775808'):
                    badbase=f'/api/tagger/registration-request/{invalid}'
                    for suffix in ('/qualification-context','/qualification-evidence','/qualification-evidence/'+'a'*64):
                        assert (await client.get(badbase+suffix)).status_code==422
                    assert (await client.post(badbase+'/qualification-evidence',json=body)).status_code==422
                assert (await client.get('/api/tagger/registration-request/9223372036854775807/qualification-context')).status_code==404
                response=await client.post(base+'/qualification-evidence',json=body)
                assert response.status_code==200 and response.json()['first_submitted_by']==42
                monkeypatch.setenv('KURA_DB_ROLE','replica')
                assert (await client.post(base+'/qualification-evidence',json=body)).status_code==503
                for suffix in ('/qualification-context','/qualification-evidence','/qualification-evidence/'+'a'*64):
                    assert (await client.get(base+suffix)).status_code==503
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_publication_exclusion_matches_topology():
    policy = pytest.importorskip('replication.publication_policy')   # the replica topology is private: absent in the public tree
    CANONICAL_ONLY_TABLES = policy.CANONICAL_ONLY_TABLES
    parse_topology_excluded = policy.parse_topology_excluded
    is_published = policy.is_published
    canonical = {
        "0199_qualification_evidence": {"tagger_qualification_evidence"},
        "0202_learning_execution": {"tagger_learning_execution", "tagger_learning_execution_event"},
        "0203_review_set_registry": {
            "tagger_review_set", "tagger_review_set_member", "tagger_review_registry_activation",
        },
        "0205_candidate_evaluation_history": {"tagger_candidate_evaluation"},
        "0206_content_head_execution": {"tagger_content_head_execution"},
    }
    excluded = parse_topology_excluded()
    for module, tables in canonical.items():
        assert tables <= CANONICAL_ONLY_TABLES and tables <= excluded
        assert all(not is_published(table) for table in tables)
        assert importlib.import_module(f"api.alembic.versions.{module}").REPLICA_DDL is None


def test_pagination_finds_every_receipt_and_cross_candidate_review_is_refused(db_url):
    async def go():
        engine,sessions,id = await setup(db_url)
        try:
            async with sessions() as db:
                ctx=await service.registration_context(db,id)
                refs=set()
                for i in range(103):
                    value=report('runtime');value['elapsed_s']=i
                    r=await service.attach(db,id,expected_context_sha256=q.digest(ctx),
                        kind='runtime',report=value,user_id=42)
                    refs.add(r['receipt_sha256'])
                await db.commit()
                a=await routes.evidence(id,Response(),db,SimpleNamespace(user_id=42))
                assert len(a['receipts'])==100 and a['more_available']
                assert all('body' not in r and r['kind']=='runtime' and r['body_bytes']>0 for r in a['receipts'])
                empty=await routes.evidence(id,Response(),db,SimpleNamespace(user_id=42),kind='human_sample')
                assert empty['receipts']==[]
                b=await routes.evidence(id,Response(),db,SimpleNamespace(user_id=42),before=a['next_cursor'])
                assert len(b['receipts'])==3 and not b['more_available'] and b['next_cursor'] is None
                assert {r['receipt_sha256'] for r in a['receipts']+b['receipts']}==refs
                other=(await db.execute(sa.text("SELECT tagger_governed_submit_registration("
                    "'fixture','other','/other.pt',:sha,CAST(:m AS jsonb),NULL,NULL)"),
                    {'sha':A,'m':json.dumps({'threshold':.25})})).scalar_one()
                await db.commit()
                with pytest.raises(KuraError,match='qualification_cursor_missing'):
                    await routes.evidence(other,Response(),db,SimpleNamespace(user_id=42),before=a['next_cursor'])
            review=report('independent_review');review['evidence_sha256s']=[next(iter(refs))]
            with pytest.raises(KuraError,match='qualification_review_evidence_mismatch'):
                await send(sessions,other,'independent_review',review)
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_actual_alembic_chain_to_0199_keeps_api_role_receipt_path_usable(pg):
    """Exercise owner switches of earlier migrations then 0199 via real Alembic.

    Only the private cluster's bootstrap is privileged. Final intake is kura.
    """
    import os
    url=load(*pg)
    u=urlsplit(url);query=parse_qs(u.query)
    subprocess.run(['/usr/lib/postgresql/18/bin/psql','-h',query['host'][0],'-p',query['port'][0],
        '-U','postgres','-d',u.path.lstrip('/'),'-XAtq','-v','ON_ERROR_STOP=1',
        '-c',"ALTER ROLE kura LOGIN; DO $$BEGIN CREATE ROLE replicator; EXCEPTION WHEN duplicate_object THEN NULL; END$$;"],
        check=True,capture_output=True)
    result=subprocess.run([sys.executable,'-m','alembic','upgrade','0199'],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ,'DATABASE_URL':url,'KURA_MIGRATE_SESSION_AUTHORIZATION':'kura'},
        capture_output=True,text=True,timeout=90)
    assert result.returncode==0, result.stdout+result.stderr  # private fixture only
    async def go():
        engine=create_async_engine(url.replace('postgres@','kura@',1),poolclass=sa.pool.NullPool)
        sessions=async_sessionmaker(engine,expire_on_commit=False)
        try:
            async with sessions() as db:
                assert (await db.execute(sa.text('SELECT version_num FROM kura_alembic_version'))).scalar_one()=='0199'
                assert (await db.execute(sa.text("SELECT tableowner FROM pg_tables WHERE tablename='tagger_qualification_evidence'"))).scalar_one()=='kura'
                await db.execute(sa.text("INSERT INTO tag(id,category_id,creation_time,version) VALUES(1,(SELECT id FROM tag_category ORDER BY id LIMIT 1),now(),1)"))
                await db.execute(sa.text("INSERT INTO tag_name(tag_name_id,tag_id,name,ord) VALUES(1,1,'fixture',0)"))
                id=(await db.execute(sa.text("SELECT tagger_governed_submit_registration('fixture','test','/test',:sha,CAST(:m AS jsonb),NULL,NULL)"),
                    {'sha':A,'m':json.dumps({'threshold':.25})})).scalar_one()
                await db.commit()
            result=await send(sessions,id)
            assert result['admission']=='not_granted'
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_contended_registration_is_bounded_and_can_be_retried(db_url):
    async def go():
        engine,sessions,id=await setup(db_url)
        try:
            async with sessions() as lock:
                await lock.execute(sa.text('SELECT id FROM tagger_registration_request WHERE id=:id FOR UPDATE'),{'id':id})
                with pytest.raises(KuraError) as error:
                    await send(sessions,id)
                assert error.value.status==503 and error.value.error=='qualification_database_busy'
                await lock.rollback()
            assert (await send(sessions,id))['admission']=='not_granted'
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_real_token_and_user_model_identify_submitter_through_command_gate(db_url,monkeypatch):
    import base64,datetime,uuid
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from httpx import AsyncClient,ASGITransport
    from api import access
    from api.db.models import User,UserToken
    async def go():
        engine,sessions,id=await setup(db_url)
        app=FastAPI();app.include_router(routes.router)
        app.add_middleware(access.CommandAuthMiddleware)
        monkeypatch.setattr(access,'async_session',sessions)
        @app.exception_handler(KuraError)
        async def error(request,exc):
            return JSONResponse(status_code=exc.status,content={'error':exc.error})
        async def database():
            async with sessions() as db:yield db
        app.dependency_overrides[routes.get_db]=database
        token=str(uuid.uuid4())
        try:
            async with sessions() as db:
                user=User(name='receipt-test',rank='regular',creation_time=datetime.datetime(2026,11,17))
                db.add(user);await db.flush();uid=user.user_id
                db.add(UserToken(user_id=uid,token=token,creation_time=datetime.datetime(2026,11,17)))
                await db.commit()
            headers={'Authorization':'Token '+base64.b64encode(('receipt-test:'+token).encode()).decode()}
            base=f'/api/tagger/registration-request/{id}'
            async with AsyncClient(transport=ASGITransport(app=app),base_url='http://fixture') as client:
                ctx=await client.get(base+'/qualification-context',headers=headers)
                assert ctx.status_code==200
                body=dict(expected_context_sha256=ctx.json()['context_sha256'],kind='runtime',report=report('runtime'))
                assert (await client.post(base+'/qualification-evidence',json=body)).status_code==401
                response=await client.post(base+'/qualification-evidence',json=body,headers=headers)
                assert response.status_code==200 and response.json()['first_submitted_by']==uid
            async with sessions() as db:
                assert (await db.execute(sa.text('SELECT submitted_by FROM tagger_qualification_evidence'))).scalar_one()==uid
                grants=(await db.execute(sa.text("SELECT DISTINCT grantee::regrole::text FROM pg_class c CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) WHERE c.oid='tagger_qualification_evidence'::regclass"))).scalars().all()
                assert grants==['kura']
                assert (await db.execute(sa.text("SELECT count(*) FROM pg_publication_tables WHERE schemaname='public' AND tablename='tagger_qualification_evidence'"))).scalar_one()==0
        finally: await engine.dispose()
    asyncio.run(go())


def test_runtime_coverage_keeps_failure_and_missing_distinct(db_url):
    async def go():
        engine, sessions, registration_id = await setup(db_url)
        try:
            async with sessions() as db:
                await db.execute(sa.text("DELETE FROM tagger_expected_worker"))
                await db.execute(sa.text("""
                    INSERT INTO tagger_expected_worker(host, description, profile, expected_provider)
                    VALUES ('primary','required','full','CPUExecutionProvider'),
                           ('nodeb','required','wd14',NULL)
                """))
                context = await service.registration_context(db, registration_id)
                runtime = report("runtime") | {
                    "host": "primary", "profile": "full", "provider": "CPUExecutionProvider",
                    "parity_passed": True, "memory_limit_bytes": 1024, "peak_bytes": 512,
                    "total": 2, "completed": 2, "oom_events": 0,
                    "elapsed_s": 1, "deadline_s": 10,
                }
                await service.attach(db, registration_id, expected_context_sha256=q.digest(context),
                                     kind="runtime", report=runtime, user_id=42)
                await db.commit()
                coverage = await service.runtime_report_coverage(db, registration_id)
                assert [w["state"] for w in coverage["workers"]] == ["reported_pass", "missing"]
                assert not coverage["all_workers_report_pass"]
                fields = {"source_sha256", "inventory_sha256", "inputs_sha256", "device",
                          "provider", "memory_limit_bytes", "threads", "deadline_s", "total"}
                pinned = {field: runtime[field] for field in fields}
                plan = {("primary", "full"): pinned, ("nodeb", "wd14"): pinned}
                coverage = await service.runtime_report_coverage(db, registration_id, runtime_plan=plan)
                assert coverage["workers"][0]["state"] == "reported_pass"
                plan[("primary", "full")] = pinned | {"inputs_sha256": "f" * 64}
                coverage = await service.runtime_report_coverage(db, registration_id, runtime_plan=plan)
                assert coverage["workers"][0]["state"] == "reported_configuration_mismatch"
                with pytest.raises(ValueError, match="runtime_plan_inventory_mismatch"):
                    await service.runtime_report_coverage(db, registration_id, runtime_plan={})
                # A newer receipt from another plan does not hide this plan's evidence.
                other = await service.attach(
                    db, registration_id, expected_context_sha256=q.digest(context), kind="runtime",
                    report=runtime | {"source_sha256": "e" * 64}, user_id=42)
                await db.commit()
                plan[("primary", "full")] = pinned
                coverage = await service.runtime_report_coverage(db, registration_id, runtime_plan=plan)
                assert coverage["workers"][0]["state"] == "reported_pass"
                assert coverage["workers"][0]["receipt_sha256"] != other["receipt_sha256"]
                plan[("primary", "full")] = pinned | {"source_sha256": "e" * 64}
                coverage = await service.runtime_report_coverage(db, registration_id, runtime_plan=plan)
                assert coverage["workers"][0]["receipt_sha256"] == other["receipt_sha256"]
                plan[("primary", "full")] = pinned
                # Runtime drift on the same plan identity is never hidden.
                drift = await service.attach(
                    db, registration_id, expected_context_sha256=q.digest(context), kind="runtime",
                    report=runtime | {"provider": "OtherExecutionProvider"}, user_id=42)
                await db.commit()
                coverage = await service.runtime_report_coverage(db, registration_id, runtime_plan=plan)
                assert (coverage["workers"][0]["state"], coverage["workers"][0]["receipt_sha256"]) == (
                    "reported_configuration_mismatch", drift["receipt_sha256"])

                await service.attach(db, registration_id, expected_context_sha256=q.digest(context),
                                     kind="runtime", report=runtime | {"oom_events": 1}, user_id=42)
                await db.commit()
                coverage = await service.runtime_report_coverage(db, registration_id)
                assert [w["state"] for w in coverage["workers"]] == ["reported_failure", "missing"]
                assert not coverage["publication_eligible"]
                # A newer failure under the same pins is still the newest match.
                coverage = await service.runtime_report_coverage(db, registration_id, runtime_plan=plan)
                assert coverage["workers"][0]["state"] == "reported_failure"
        finally:
            await engine.dispose()
    asyncio.run(go())
