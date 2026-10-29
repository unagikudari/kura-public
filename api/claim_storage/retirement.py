"""Retire the verified checkpoint without altering the current claim authority."""
from datetime import datetime,timedelta
from sqlalchemy import text
from .service import transition_lease


def timestamp(value):
    return datetime.fromisoformat(value) if isinstance(value,str) else value


async def retire(db,observation,backup_started_at,*,observation_period=timedelta(minutes=20)):
    if observation_period<timedelta(minutes=20):raise ValueError('Checkpoint requires at least 20 minutes of observation')
    await db.execute(text("SET LOCAL lock_timeout='2s'"))
    await db.execute(text("SET LOCAL statement_timeout='10s'"))
    await transition_lease(db)
    state=(await db.execute(text('SELECT phase,updated_at,clock_timestamp() AS now FROM claim_storage.migration FOR UPDATE'))).mappings().one()
    if state['phase']!='active':raise ValueError('Normalized claims must remain authoritative')
    observed=timestamp(observation['observed_at']);activated=state['updated_at'];backup=timestamp(backup_started_at)
    if observed<activated or state['now']-observed<observation_period or not activated<=backup<=state['now']:
        raise ValueError('Observation or backup does not cover this activation')
    if timestamp(observation['migration'][0]['updated_at'])!=activated or observation['migration'][0]['phase']!='active':
        raise ValueError('Observation belongs to another activation')
    if not (await db.execute(text("SELECT to_regclass('claim_storage.legacy_claim_archive') IS NOT NULL"))).scalar_one():
        return {'phase':'active','changed':False,'checkpoint_retired':True}
    await db.execute(text('LOCK TABLE claim_storage.legacy_claim_archive IN ACCESS EXCLUSIVE MODE NOWAIT'))
    await db.execute(text('SELECT pg_stat_clear_snapshot()'))
    current=(await db.execute(text("SELECT relid,seq_scan,idx_scan FROM pg_stat_user_tables WHERE schemaname='claim_storage' AND relname='legacy_claim_archive'"))).mappings().one()
    baseline=observation['archive_reads']
    if len(baseline)!=1 or any(current[k]!=baseline[0][k] for k in ('relid','seq_scan','idx_scan')):
        raise ValueError('Legacy checkpoint readers changed since observation')
    if (await db.execute(text("SELECT count(*) FROM pg_constraint WHERE conrelid='claim_storage.claim'::regclass AND contype='f' AND convalidated"))).scalar_one()!=3:
        raise ValueError('Native claim foreign keys are not validated')
    if not (await db.execute(text("SELECT relkind='v' FROM pg_class WHERE oid='public.post_tag_actor_claim'::regclass"))).scalar_one():
        raise ValueError('Compatibility view is missing')
    size=(await db.execute(text("SELECT pg_total_relation_size('claim_storage.legacy_claim_archive')"))).scalar_one()
    # No CASCADE: unexpected dependencies abort this whole transaction.
    await db.execute(text('DROP TABLE claim_storage.legacy_claim_archive'))
    for name in ('insert','delete','update'):
        await db.execute(text(f'DROP TRIGGER claim_track_{name} ON claim_storage.claim'))
    await db.execute(text('DROP TRIGGER claim_block_truncate ON claim_storage.claim'))
    await db.execute(text('DROP TABLE claim_storage.changed_post'))
    for name in ('mirror_insert','mirror_delete','mirror_update','mirror_truncate',
                 'track_insert','track_delete','track_update','block_truncate','archive_read_only'):
        await db.execute(text(f'DROP FUNCTION claim_storage.{name}()'))
    return {'phase':'active','changed':True,'checkpoint_retired':True,'retired_relation_bytes':size}
