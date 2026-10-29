"""Checkpoint retirement preserves current data and rejects stale or incomplete evidence."""
from datetime import timedelta
import pytest
from sqlalchemy import text
from test_collector_next import postgres,run  # noqa:F401
from test_pool_search_candidates import search_database  # noqa:F401
from test_claim_storage import initialize
from api.claim_storage.migration import copy_batch,verify_batch
from api.claim_storage.cutover import activate,rollback
from api.claim_storage.retirement import retire
from api.claim_storage import service


async def prepare(sessions):
    await initialize(sessions)
    async with sessions.begin() as db:
        await copy_batch(db,50);await copy_batch(db,50)
    while True:
        async with sessions.begin() as db:
            if (await verify_batch(db))['phase']=='ready':break
    async with sessions.begin() as db:
        await activate(db)
        await db.execute(text("UPDATE claim_storage.migration SET updated_at=clock_timestamp()-interval '30 minutes'"))
        await db.execute(text('SELECT pg_stat_force_next_flush()'))
    async with sessions.begin() as db:
        state=(await db.execute(text('SELECT phase,updated_at,clock_timestamp() AS now FROM claim_storage.migration'))).mappings().one()
        stats=(await db.execute(text("SELECT relid,seq_scan,idx_scan FROM pg_stat_user_tables WHERE schemaname='claim_storage' AND relname='legacy_claim_archive'"))).mappings().one()
        observation={'observed_at':state['now']-timedelta(minutes=25),'migration':[dict(state)],'archive_reads':[dict(stats)]}
        return observation,state['now']-timedelta(minutes=24)


def test_retirement_keeps_native_and_compatibility_writes_without_checkpoint_cost(search_database):
    async def scenario(sessions):
        observation,backup=await prepare(sessions)
        async with sessions.begin() as db:
            source=await service.backend(db,'actor-a')
            await service.replace(db,source,1,[4,5])
        async with sessions.begin() as db:
            result=await retire(db,observation,backup)
            assert result['changed'] and result['retired_relation_bytes']>0
            assert (await db.execute(text("SELECT to_regclass('claim_storage.changed_post') IS NULL"))).scalar_one()
            assert (await db.execute(text("SELECT tag_id FROM public.post_tag_actor_claim WHERE post_id=1 AND actor_id='actor-a' ORDER BY tag_id"))).scalars().all()==[4,5]
            assert (await activate(db))['changed'] is False
            assert (await retire(db,observation,backup))['changed'] is False
        async with sessions.begin() as db:
            source=await service.backend(db,'new-source')
            await service.replace(db,source,5,[4])
            await db.execute(text("UPDATE public.post_tag_actor_claim SET tag_id=5 WHERE post_id=5"))
            assert (await db.execute(text('SELECT tag_id FROM claim_storage.claim WHERE post_id=5'))).scalar_one()==5
        async with sessions.begin() as db:
            with pytest.raises(ValueError,match='checkpoint is retired'):
                await rollback(db)
    run(search_database,scenario)


def test_retirement_refuses_short_observation_reader_changes_and_old_backup(search_database):
    async def scenario(sessions):
        observation,backup=await prepare(sessions)
        cases=[({**observation,'observed_at':backup},backup,'Observation'),
               (observation,observation['migration'][0]['updated_at']-timedelta(seconds=1),'Observation'),
               ({**observation,'archive_reads':[{**observation['archive_reads'][0],'idx_scan':-1}]},backup,'readers changed')]
        # First case is a genuinely short observation, unlike the valid 25min baseline.
        async with sessions.begin() as db:now=(await db.execute(text('SELECT clock_timestamp()'))).scalar_one()
        cases[0]=({**observation,'observed_at':now},backup,'Observation')
        for obs,started,reason in cases:
            async with sessions.begin() as db:
                with pytest.raises(ValueError,match=reason):
                    async with db.begin_nested():await retire(db,obs,started)
                assert (await db.execute(text("SELECT to_regclass('claim_storage.legacy_claim_archive') IS NOT NULL"))).scalar_one()
                assert (await db.execute(text('SELECT count(*) FROM claim_storage.claim'))).scalar_one()==18
    run(search_database,scenario)
