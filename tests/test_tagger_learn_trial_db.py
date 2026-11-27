"""Real CHECK, lease and lifecycle functions in an isolated full-schema PG18.

No production connection/env is used. Candidate rows remain 'candidate' while
lifecycle events move to 'training'. This is intentionally not a SQL mock.
"""
import asyncio
from pathlib import Path
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, str(Path(__file__).resolve().parent))
from full_schema import cluster, load, BIN
from api.tagger_core.transitions import acquire_lease, record_lifecycle_event, release_lease
from tagger.learn_trial import eligible

TAG = 'monochrome'


def sql(url, statement):
    u = urlsplit(url); q = parse_qs(u.query)
    result = subprocess.run([str(BIN/'psql'), '-h', q['host'][0], '-p', q['port'][0],
        '-U', 'postgres', '-d', u.path.lstrip('/'), '-XAtq', '-v', 'ON_ERROR_STOP=1',
        '-c', statement], capture_output=True, text=True)
    if result.returncode:
        print(result.stderr)
        result.check_returncode()
    return result.stdout.strip()


@pytest.fixture(scope='module')
def pg():
    with cluster() as c:
        yield c


@pytest.fixture
def world(pg):
    url = load(*pg)
    sql(url, """
        ALTER ROLE kura LOGIN;
        INSERT INTO tag (id, category_id, creation_time, version)
        VALUES (1, (SELECT id FROM tag_category ORDER BY id LIMIT 1), now(), 1);
        INSERT INTO tag_name (tag_name_id, tag_id, name, ord)
        VALUES (1, 1, 'monochrome', 0);
        SET ROLE kura_reconciler;
        INSERT INTO tagger_head_candidate(tag, reason, added_by)
        VALUES ('monochrome', 'isolated test', 'test');
    """)
    return url


async def check(url, scenario):
    engine = create_async_engine(url.replace('postgres@', 'kura@', 1), poolclass=NullPool)
    try:
        async with async_sessionmaker(engine)() as db:
            lease = await acquire_lease(db, 'lifecycle:monochrome', lease_seconds=60)
            try:
                for frm, to in [(None, 'candidate'), ('candidate', 'training')]:
                    event = await record_lifecycle_event(db, tag=TAG, from_stage=frm,
                        to_stage=to, reason='isolated test', lease=lease)
                assert await eligible(db, TAG, event)
                assert not await eligible(db, TAG, event + 1)
                assert not await eligible(db, 'other', event)
                if scenario == 'reopen':
                    await record_lifecycle_event(db, tag=TAG, from_stage='training',
                        to_stage='approved', reason='isolated test', lease=lease)
                    assert not await eligible(db, TAG, event)
                    await record_lifecycle_event(db, tag=TAG, from_stage='approved',
                        to_stage='candidate', reason='isolated test reopen', lease=lease)
                    assert not await eligible(db, TAG, event)
                    new = await record_lifecycle_event(db, tag=TAG, from_stage='candidate',
                        to_stage='training', reason='isolated test reopen', lease=lease)
                    assert new != event
                    assert not await eligible(db, TAG, event)
                    assert await eligible(db, TAG, new)
                elif scenario == 'consumed':
                    # Fixture registration consumes the exact event; no upload or API call.
                    sql(url, "SET ROLE kura_reconciler; INSERT INTO tagger_registration_request "
                        "(tag,source_host,artifact_ref,artifact_sha256,lifecycle_event_id) "
                        f"VALUES ('monochrome','test','/test/not-uploaded','{'a'*64}',{event})")
                    assert not await eligible(db, TAG, event)
                else:
                    # Test fixtures only, on this disposable cluster.
                    if scenario == 'held':
                        sql(url, "SET ROLE kura_reconciler; INSERT INTO tagger_head_candidate(tag,reason,status,hold_reason_class) "
                            "VALUES ('monochrome','test hold','held','label_definition_conflict')")
                    elif scenario == 'dismissed':
                        sql(url, "SET ROLE kura_reconciler; UPDATE tagger_head_candidate SET status='dismissed'")
                    elif scenario == 'missing':
                        sql(url, 'SET ROLE kura_reconciler; DELETE FROM tagger_head_candidate')
                    assert not await eligible(db, TAG, event)
            finally:
                await release_lease(db, lease)
    finally:
        await engine.dispose()


@pytest.mark.parametrize('scenario', ['reopen', 'consumed', 'held', 'dismissed', 'missing'])
def test_real_queue_candidate_and_lifecycle_are_separate(world, scenario):
    asyncio.run(check(world, scenario))


def test_production_check_rejects_training_candidate_status(world):
    with pytest.raises(subprocess.CalledProcessError) as exc:
        sql(world, "SET ROLE kura_reconciler; UPDATE tagger_head_candidate SET status='training'")
    assert 'tagger_head_candidate_status_check' in exc.value.stderr
    assert sql(world, 'SELECT status FROM tagger_head_candidate') == 'candidate'

@pytest.mark.parametrize('held', [False, True])
def test_single_tag_preview_reuses_gates_without_side_effects(world, monkeypatch, tmp_path, held):
    from unittest.mock import AsyncMock
    from tagger import reconciler
    engine = create_async_engine(world.replace('postgres@', 'kura@', 1), poolclass=NullPool)
    monkeypatch.setattr(reconciler, 'async_session', async_sessionmaker(engine))
    monkeypatch.setattr(reconciler, '_gt_state', AsyncMock(return_value=(100, 100, '0' * 64)))
    monkeypatch.setattr(reconciler, '_corpus_positive_count', AsyncMock(return_value=100))
    monkeypatch.setattr(reconciler, 'DISPOSITION_PATH', tmp_path/'must-not-exist.json')
    async def forbidden(*args, **kwargs):
        raise AssertionError('preview must not run housekeeping or dispatch')
    for name in ('settle_judged_training', '_reap_approved_without_head', '_release_held',
                 '_expire_due_dispositions', 'acquire_lease', 'record_lifecycle_event'):
        monkeypatch.setattr(reconciler, name, forbidden)
    if held:
        sql(world, "SET ROLE kura_reconciler; INSERT INTO tagger_head_candidate "
            "(tag,reason,status,hold_reason_class) "
            "VALUES ('monochrome','fixture','held','label_definition_conflict')")
    before = sql(world, 'SELECT count(*) FROM tagger_lifecycle_event')
    result = asyncio.run(reconciler.run_reconcile(preview_tag='monochrome'))
    assert result['preview'] and result['dispatched'] == []
    assert list(result['decisions']) == ['monochrome']
    assert result['decisions']['monochrome']['disposition'] == ('hold' if held else 'eligible')
    assert sql(world, 'SELECT count(*) FROM tagger_lifecycle_event') == before
    assert not (tmp_path/'must-not-exist.json').exists()
    asyncio.run(engine.dispose())



def test_lifecycle_publication_event_rolls_back_with_transaction(world):
    async def go():
        engine = create_async_engine(world.replace('postgres@', 'kura@', 1), poolclass=NullPool)
        try:
            async with async_sessionmaker(engine)() as db:
                lease = await acquire_lease(db, 'lifecycle:monochrome', lease_seconds=60)
                try:
                    event = await record_lifecycle_event(
                        db, tag=TAG, from_stage=None, to_stage="candidate",
                        reason="transaction-owned fixture", lease=lease, commit=False,
                    )
                    assert (await db.execute(text(
                        "SELECT id FROM tagger_lifecycle_event WHERE id=:id"
                    ), {"id": event})).scalar_one() == event
                    await db.rollback()
                    assert (await db.execute(text(
                        "SELECT id FROM tagger_lifecycle_event WHERE id=:id"
                    ), {"id": event})).scalar_one_or_none() is None
                finally:
                    await release_lease(db, lease)
        finally:
            await engine.dispose()
    asyncio.run(go())
