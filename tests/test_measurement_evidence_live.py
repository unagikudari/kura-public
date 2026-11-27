"""Private PG18/full Alembic chain + real API-role intake, never a live DB.

Before 0200 is merged, explicitly supply KURA_TEST_0200_DRAFT and its expected
SHA256. It is copied into a temporary migration tree only, never this checkout.
This draft-basis run is not the required final-baseline integration review.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from fastapi import Response

sys.path.insert(0, str(Path(__file__).parent))
from full_schema import cluster, load
from test_measurement_evidence import measurement, runtime, A, B, C
from api.tagger_core import measurement_evidence as contract
from api.tagger_core import qualification_evidence as q
from api.services import measurement_evidence as service
from api.routers import tagger_measurements as routes
from api.errors import KuraError

ROOT = Path(__file__).resolve().parents[1]
TABLES = ('tagger_measurement_anchor', 'tagger_measurement_evidence')


@pytest.fixture(scope='module')
def pg():
    with cluster() as private:
        yield private


@pytest.fixture(scope='module')
def migration_tree(tmp_path_factory):
    tree = tmp_path_factory.mktemp('measurement-alembic') / 'alembic'
    shutil.copytree(ROOT / 'api/alembic', tree, ignore=shutil.ignore_patterns('__pycache__'))
    # Earlier real migrations load SQL beside the api/alembic directory.
    shutil.copytree(ROOT / 'api/collector/migrations', tree.parent / 'collector/migrations')
    present = list((tree / 'versions').glob('0200_*.py'))
    if not present:
        source = os.environ.get('KURA_TEST_0200_DRAFT')
        expected = os.environ.get('KURA_TEST_0200_SHA256')
        assert source and expected, '0200 not merged: explicitly pin draft source and hash for private tests only'
        raw = Path(source).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == expected, '0200 draft changed; inspect before repinning'
        shutil.copyfile(source, tree / 'versions' / Path(source).name)
    return tree


@pytest.fixture
def db_url(pg, migration_tree):
    url = load(*pg)
    parsed = urlsplit(url); query = parse_qs(parsed.query)
    command = ['/usr/lib/postgresql/18/bin/psql', '-h', query['host'][0], '-p', query['port'][0],
               '-U', 'postgres', '-d', parsed.path.lstrip('/'), '-XAtq', '-v', 'ON_ERROR_STOP=1']
    subprocess.run([*command, '-c', "ALTER ROLE kura LOGIN;"
        "DO $$BEGIN CREATE ROLE replicator; EXCEPTION WHEN duplicate_object THEN NULL; END$$;"
        "ALTER DEFAULT PRIVILEGES FOR ROLE kura GRANT SELECT ON TABLES TO replicator;"],
        check=True, capture_output=True)
    code = "from alembic.config import Config; from alembic import command; import sys; c=Config(sys.argv[1]); c.set_main_option('script_location',sys.argv[2]); command.upgrade(c,'0201')"
    result = subprocess.run([sys.executable, '-c', code, str(ROOT / 'alembic.ini'), str(migration_tree)],
        cwd=ROOT, env={**os.environ, 'DATABASE_URL': url, 'KURA_MIGRATE_SESSION_AUTHORIZATION': 'kura'},
        capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    return url.replace('postgres@', 'kura@', 1)


async def setup(url):
    engine = create_async_engine(url, poolclass=sa.pool.NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        assert (await db.execute(sa.text('SELECT session_user'))).scalar_one() == 'kura'
        assert (await db.execute(sa.text('SELECT version_num FROM kura_alembic_version'))).scalar_one() == '0201'
        await db.execute(sa.text("INSERT INTO tag(id,category_id,creation_time,version) VALUES"
            "(3,(SELECT id FROM tag_category ORDER BY id LIMIT 1),now(),1)"))
        await db.execute(sa.text("INSERT INTO tag_name(tag_name_id,tag_id,name,ord) VALUES(3,3,'fixture',0)"))
        registration = (await db.execute(sa.text("SELECT tagger_governed_submit_registration("
            "'fixture','fixture','/original',:sha,CAST(:m AS jsonb),CAST(:gt AS jsonb),NULL)"),
            {'sha': B, 'm': json.dumps({'threshold': .5}), 'gt': json.dumps({'positive': 20, 'negative': 30})})).scalar_one()
        await db.execute(sa.text("SELECT tagger_governed_set_registration_status(:id,'rejected','prior insufficient labels','unmeasurable')"), {'id': registration})
        await db.execute(sa.text("SELECT tagger_governed_add_head_candidate('fixture',NULL,'prior candidate','fixture')"))
        await db.execute(sa.text("SELECT tagger_governed_hold_head_candidate('fixture','insufficient_gt',CAST(:e AS jsonb))"),
                         {'e': json.dumps({'gt_pos': 20, 'gt_neg': 30})})
        await db.commit()
    return engine, sessions


async def original_state(db):
    names = (await db.execute(sa.text("SELECT tablename FROM pg_tables WHERE schemaname='public' "
        "AND (tablename LIKE 'tagger_%' OR tablename IN ('post_tag','post_tag_pin','post_tag_audit','tag_quarantine')) "
        "AND tablename NOT IN ('tagger_measurement_anchor','tagger_measurement_evidence') ORDER BY tablename"))).scalars().all()
    values = {}
    for name in names:  # names come only from this disposable cluster's catalog
        values[name] = (await db.execute(sa.text(
            f'SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),\'[]\'::jsonb) FROM public."{name}" t'))).scalar_one()
    from tagger import reconciler
    values['reader_positive'] = await reconciler._last_registration_positive(db, 'fixture')
    values['reader_negative'] = await reconciler._last_registration_negative(db, 'fixture')
    values['reader_hold'] = await reconciler._last_registration_hold(db, 'fixture', {})
    values['open_candidates'] = await reconciler._open_candidates_by_tag(db)
    return values


async def submit(sessions, value=None, user=42, tag='fixture'):
    async with sessions() as db:
        return await routes.submit(routes.MeasurementIn(tag_id=3, tag=tag, measurement=value or measurement()),
                                   Response(), db, SimpleNamespace(user_id=user))


async def attach(sessions, anchor, value=None, user=42, kind='runtime'):
    async with sessions() as db:
        return await routes.attach(anchor, routes.EvidenceIn(expected_measurement_sha256=anchor,
            kind=kind, report=value or runtime()), Response(), db, SimpleNamespace(user_id=user))


def test_full_chain_concurrent_replay_and_lost_ack_do_not_change_any_operational_state(db_url):
    async def go():
        engine, sessions = await setup(db_url)
        try:
            async with sessions() as db:
                before = await original_state(db)
                assert before['reader_hold'][0] == 'insufficient_gt'
                assert before['reader_positive'] == 20 and before['reader_negative'] == 30
                assert before['tagger_head_candidate'][0]['status'] == 'held'
            a, b = await asyncio.gather(submit(sessions), submit(sessions, user=43))
            assert a == b and a['first_submitted_by'] in (42, 43)
            # Discarding the first reply changes nothing: the same exact request
            # recovers the content-addressed original, not another registration.
            assert await submit(sessions) == a
            ref = a['measurement_sha256']
            x, y = await asyncio.gather(attach(sessions, ref), attach(sessions, ref, user=43))
            assert x == y and await attach(sessions, ref) == x
            async with sessions() as db:
                assert await original_state(db) == before
                from tagger import reconciler
                assert await reconciler._release_held(db, 20, 50) == ([], [])
                assert await original_state(db) == before
                assert (await db.execute(sa.text('SELECT count(*) FROM tagger_measurement_anchor'))).scalar_one() == 1
                assert (await db.execute(sa.text('SELECT count(*) FROM tagger_measurement_evidence'))).scalar_one() == 1
                exact = await routes.exact(ref, x['receipt_sha256'], Response(), db, SimpleNamespace(user_id=42))
                assert exact['receipt']['body']['report']['parity_passed'] is None
                assert hashlib.sha256(exact['receipt']['canonical_body'].encode()).hexdigest() == x['receipt_sha256']
                for table in TABLES:
                    assert (await db.execute(sa.text('SELECT tableowner FROM pg_tables WHERE tablename=:t'), {'t': table})).scalar_one() == 'kura'
                    grants = (await db.execute(sa.text("SELECT DISTINCT grantee::regrole::text FROM pg_class c CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) WHERE c.oid=CAST(:t AS regclass)"), {'t': table})).scalars().all()
                    assert grants == ['kura']
                    assert (await db.execute(sa.text('SELECT count(*) FROM pg_publication_tables WHERE schemaname=\'public\' AND tablename=:t'), {'t': table})).scalar_one() == 0
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_replay_retains_historical_tag_but_new_anchor_rejects_retired_identity(db_url):
    async def go():
        engine, sessions = await setup(db_url)
        try:
            first = await submit(sessions)
            async with sessions() as db:
                await db.execute(sa.text("UPDATE tag_name SET name='renamed' WHERE tag_id=3")); await db.commit()
            assert await submit(sessions) == first
            changed = measurement(); changed['source']['training_result_sha256'] = C
            with pytest.raises(KuraError, match='measurement_tag_identity_mismatch'):
                await submit(sessions, changed)
            async with sessions() as db:
                await db.execute(sa.text('DELETE FROM tag_name WHERE tag_id=3')); await db.commit()
            assert await submit(sessions) == first
            with pytest.raises(KuraError, match='measurement_tag_identity_mismatch'):
                await submit(sessions, changed)
        finally:
            await engine.dispose()
    asyncio.run(go())


@pytest.mark.parametrize('table', TABLES)
@pytest.mark.parametrize('action', ['UPDATE', 'DELETE', 'TRUNCATE'])
def test_measurement_history_rejects_mutation_as_actual_api_owner(db_url, table, action):
    async def go():
        engine, sessions = await setup(db_url)
        try:
            anchor = await submit(sessions); await attach(sessions, anchor['measurement_sha256'])
            statement = (f'UPDATE {table} SET submitted_by=44' if action == 'UPDATE' else
                         f'DELETE FROM {table}' if action == 'DELETE' else f'TRUNCATE {table}')
            async with sessions() as db:
                with pytest.raises(sa.exc.DBAPIError):
                    await db.execute(sa.text(statement))
                await db.rollback()
                assert (await db.execute(sa.text(f'SELECT count(*) FROM {table}'))).scalar_one() == 1
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_db_guard_and_cross_anchor_review_bindings(db_url):
    async def go():
        engine, sessions = await setup(db_url)
        try:
            first = await submit(sessions); ref = first['measurement_sha256']
            one = await attach(sessions, ref)
            changed = measurement(); changed['source']['training_result_sha256'] = C
            other = (await submit(sessions, changed))['measurement_sha256']
            review = {k: runtime()[k] for k in ('artifact_sha256', 'threshold', 'observed_at')}
            review.update(evidence_sha256s=[one['receipt_sha256']], reviewer='asserted reviewer',
                          reviewed_at=runtime()['observed_at'], verdict='approve', findings=[])
            with pytest.raises(KuraError, match='measurement_review_evidence_mismatch'):
                await attach(sessions, other, review, kind='independent_review')
            assert (await attach(sessions, ref, review, kind='independent_review'))['admission'] == 'not_granted'
            original = contract.original_measurement(3, 'fixture', measurement())
            packet = contract.assemble(ref, original['body'], ref, 'runtime', runtime())
            packet['body']['report']['threshold'] = .6
            wire = q.canonical(packet['body']); digest = q.digest(packet['body'])
            async with sessions() as db:
                with pytest.raises(sa.exc.DBAPIError, match='measurement_anchor_mismatch'):
                    await db.execute(sa.text('INSERT INTO tagger_measurement_evidence '
                        '(receipt_sha256,measurement_sha256,canonical_body,body,submitted_by) '
                        'VALUES (:sha,:anchor,CAST(:wire AS text),CAST(CAST(:wire AS text) AS jsonb),42)'),
                        {'sha': digest, 'anchor': ref, 'wire': wire})
                await db.rollback()
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_authenticated_http_and_replica_refusal(db_url, monkeypatch):
    import base64, datetime, uuid
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from httpx import AsyncClient, ASGITransport
    from api import access
    from api.db.models import User, UserToken
    async def go():
        engine, sessions = await setup(db_url)
        app = FastAPI(); app.include_router(routes.router); app.add_middleware(access.CommandAuthMiddleware)
        monkeypatch.setattr(access, 'async_session', sessions)
        @app.exception_handler(KuraError)
        async def error(request, exc):
            return JSONResponse(status_code=exc.status, content={'error': exc.error})
        async def database():
            async with sessions() as db:
                yield db
        app.dependency_overrides[routes.get_db] = database
        token = str(uuid.uuid4())
        try:
            async with sessions() as db:
                user = User(name='measurement-test', rank='regular', creation_time=datetime.datetime(2026,11,17))
                db.add(user); await db.flush(); uid = user.user_id
                db.add(UserToken(user_id=uid, token=token, creation_time=datetime.datetime(2026,11,17)))
                await db.commit()
                before = await original_state(db)
            headers = {'Authorization': 'Token ' + base64.b64encode(('measurement-test:' + token).encode()).decode()}
            request = dict(tag_id=3, tag='fixture', measurement=measurement())
            async with AsyncClient(transport=ASGITransport(app=app), base_url='http://fixture') as client:
                assert (await client.post('/api/tagger/measurements', json=request)).status_code == 401
                response = await client.post('/api/tagger/measurements', json=request, headers=headers)
                assert response.status_code == 200, response.text
                assert response.json()['first_submitted_by'] == uid
                ref = response.json()['measurement_sha256']; base = f'/api/tagger/measurements/{ref}'
                read = await client.get(base, headers=headers)
                assert read.status_code == 200 and read.headers['cache-control'] == 'private, no-store'
                evidence = dict(expected_measurement_sha256=ref, kind='runtime', report=runtime())
                stored = await client.post(base + '/qualification-evidence', json=evidence, headers=headers)
                assert stored.status_code == 200 and stored.json()['first_submitted_by'] == uid
                listing = await client.get(base + '/qualification-evidence', headers=headers)
                assert len(listing.json()['receipts']) == 1
                assert 'body' not in listing.json()['receipts'][0]
                monkeypatch.setenv('KURA_DB_ROLE', 'replica')
                assert (await client.post('/api/tagger/measurements', json=request, headers=headers)).status_code == 503
                assert (await client.get(base, headers=headers)).status_code == 503
            async with sessions() as db:
                assert await original_state(db) == before
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_topology_publication_rebuild_keeps_measurements_private(db_url):
    """Execute the actual publication/GRANT loop, not only its exclusion parser."""
    policy = pytest.importorskip('replication.publication_policy')   # the replica topology is private: absent in the public tree
    CANONICAL_ONLY_TABLES = policy.CANONICAL_ONLY_TABLES
    parse_topology_excluded = policy.parse_topology_excluded
    excluded = parse_topology_excluded()
    assert set(TABLES) <= CANONICAL_ONLY_TABLES <= excluded
    sql = (ROOT / 'replication/kura_topology.sql').read_text()
    start = sql.index('DROP PUBLICATION IF EXISTS kura_mesh_pub;')
    end = sql.index("\\echo '[infra] stage complete", start)
    block = sql[start:end]
    # Before the final parent merge, use the parent's own publication exclusion
    # in this temporary test input. Never silently publish the draft 0200 table.
    if 'tagger_human_review_event' not in excluded:
        block = block.replace('excluded text[] := ARRAY[',
                              "excluded text[] := ARRAY['tagger_human_review_event',", 1)
    parsed = urlsplit(db_url); query = parse_qs(parsed.query)
    run = subprocess.run(['/usr/lib/postgresql/18/bin/psql', '-h', query['host'][0],
        '-p', query['port'][0], '-U', 'postgres', '-d', parsed.path.lstrip('/'),
        '-XAtq', '-v', 'ON_ERROR_STOP=1'], input=block, text=True, capture_output=True)
    assert run.returncode == 0, run.stderr
    async def go():
        engine = create_async_engine(db_url, poolclass=sa.pool.NullPool)
        try:
            async with engine.connect() as db:
                assert (await db.execute(sa.text("SELECT count(*) FROM pg_publication_tables WHERE pubname='kura_mesh_pub' AND tablename='tag'"))).scalar_one() == 1
                for table in (*TABLES, 'tagger_human_review_event'):
                    assert (await db.execute(sa.text("SELECT count(*) FROM pg_publication_tables WHERE tablename=:t"), {'t': table})).scalar_one() == 0
                    assert not (await db.execute(sa.text("SELECT has_table_privilege('replicator',:t,'SELECT')"), {'t': table})).scalar_one()
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_bounded_pagination_and_wrong_anchor_cursor(db_url):
    async def go():
        engine, sessions = await setup(db_url)
        try:
            first = (await submit(sessions))['measurement_sha256']
            changed = measurement(); changed['source']['training_result_sha256'] = C
            other = (await submit(sessions, changed))['measurement_sha256']
            refs = set()
            for i in range(102):
                value = runtime(); value['profile'] = f'private observation {i}'
                refs.add((await attach(sessions, first, value))['receipt_sha256'])
            async with sessions() as db:
                one = await routes.listing(first, Response(), db, SimpleNamespace(user_id=42))
                assert len(one['receipts']) == 100 and one['more_available']
                two = await routes.listing(first, Response(), db, SimpleNamespace(user_id=42), before=one['next_cursor'])
                assert len(two['receipts']) == 2 and not two['more_available'] and two['next_cursor'] is None
                assert {r['receipt_sha256'] for r in one['receipts'] + two['receipts']} == refs
                with pytest.raises(KuraError, match='measurement_cursor_missing'):
                    await routes.listing(other, Response(), db, SimpleNamespace(user_id=42), before=one['next_cursor'])
        finally:
            await engine.dispose()
    asyncio.run(go())


@pytest.mark.parametrize('number', [1e16, -0.0])
def test_jsonb_number_normalization_cannot_break_immutable_canonical_anchor(db_url, number):
    async def go():
        engine, sessions = await setup(db_url)
        try:
            value = measurement()
            value['feature_contract']['preprocessing']['declared_numeric_metadata'] = number
            value['feature_contract']['preprocessing_sha256'] = q.digest(value['feature_contract']['preprocessing'])
            if number == 0:
                value['internal_validation']['auc'] = -0.0
            first = await submit(sessions, value); ref = first['measurement_sha256']
            assert await submit(sessions, value) == first
            async with sessions() as db:
                fetched = await routes.read(ref, Response(), db, SimpleNamespace(user_id=42))
                frozen = fetched['measurement']
                assert frozen['canonical_body'] == contract.original_measurement(3, 'fixture', value)['canonical_body']
                assert q.canonical(frozen['body']) == frozen['canonical_body']
            report = runtime(); report['elapsed_s'] = -0.0
            evidence = await attach(sessions, ref, report)
            async with sessions() as db:
                exact = await routes.exact(ref, evidence['receipt_sha256'], Response(), db, SimpleNamespace(user_id=42))
                receipt = exact['receipt']
                assert q.canonical(receipt['body']) == receipt['canonical_body']
                assert hashlib.sha256(receipt['canonical_body'].encode()).hexdigest() == evidence['receipt_sha256']
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_new_anchor_uses_same_deterministic_primary_name_as_sql_guard(db_url):
    async def go():
        engine, sessions = await setup(db_url)
        try:
            async with sessions() as db:
                # Include both a secondary alias and an ordinal tie; service and
                # guard must resolve the same immutable requested primary name.
                await db.execute(sa.text("INSERT INTO tag_name(tag_name_id,tag_id,name,ord) VALUES(4,3,'alias',1),(5,3,'tie',0)"))
                await db.commit()
            assert (await submit(sessions))['outcome'] == 'succeeded'
            for name in ('alias', 'tie'):
                with pytest.raises(KuraError, match='measurement_tag_identity_mismatch'):
                    await submit(sessions, tag=name)
        finally:
            await engine.dispose()
    asyncio.run(go())
