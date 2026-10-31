"""Summary deltas, stable covers and rebuild agree on unique assets, not entries."""
import asyncio
from uuid import uuid4
import pytest
from sqlalchemy import text, event
from sqlalchemy.exc import IntegrityError
from test_collector_next import database,postgres,run,image  # noqa:F401
from api.collector import service,label_service,tag_summary


async def summaries(db, owner=1):
    return (await tag_summary.catalog(db,owner,safe_mode=False))['items']


def test_deltas_replay_stable_cover_rollback_and_rebuild(database,tmp_path):
    red,_=image(tmp_path);blue,_=image(tmp_path,'blue')
    async def scenario(sessions):
        async with sessions.begin() as db:
            c=await service.create_collection(db,1,'repeated')
            a=await service.register(db,1,uuid4(),red,'a.png',c['id'])
            duplicate=await service.register(db,1,uuid4(),red,'copy.png',c['id'])
            b=await service.register(db,1,uuid4(),blue,'b.png',c['id'])
            other=await service.register(db,2,uuid4(),blue,'private.png',None)
            assert duplicate['asset_id']==a['asset_id']
            key=uuid4();args=(1,key,[(a['asset_id'],0)],['shared'],[])
            await label_service.apply(db,*args)
            initial=(await summaries(db))[0]
            assert initial['count']==1 and initial['logical_bytes']==red.size
            assert initial['representative_asset_id']==a['asset_id']
            await label_service.apply(db,*args)
            assert (await summaries(db))[0]==initial
            # New evidence confirming the same state does not rewrite the summary.
            await label_service.apply(db,1,uuid4(),[(a['asset_id'],1)],['shared'],[])
            assert (await summaries(db))[0]==initial
            await label_service.apply(db,1,uuid4(),[(b['asset_id'],0)],['shared'],[])
            row=(await summaries(db))[0]
            assert row['count']==2 and row['logical_bytes']==red.size+blue.size
            assert row['representative_asset_id']==a['asset_id']
            await label_service.apply(db,2,uuid4(),[(other['asset_id'],0)],['shared'],[])
            assert len(await summaries(db,2))==1
            # A database constraint prevents a cross-owner cover reference.
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await db.execute(text('UPDATE collector_next.tag_usage SET representative_asset_id=:a WHERE owner_id=1'),{'a':other['asset_id']})
        with pytest.raises(RuntimeError):
            async with sessions.begin() as db:
                await label_service.apply(db,1,uuid4(),[(a['asset_id'],2)],[],['shared'])
                raise RuntimeError('simulated failure after update')
        async with sessions.begin() as db:
            assert (await summaries(db))[0]==row
            await label_service.apply(db,1,uuid4(),[(a['asset_id'],2)],[],['shared'])
            row=(await summaries(db))[0]
            assert row['count']==1 and row['logical_bytes']==blue.size and row['representative_asset_id']==b['asset_id']
            await db.execute(text('UPDATE collector_next.tag_usage SET positive_count=999,logical_bytes=999,representative_asset_id=NULL WHERE owner_id=1'))
            await label_service.rebuild_asset(db,1,a['asset_id'])
            fixed=(await summaries(db))[0]
            assert (fixed['count'],fixed['logical_bytes'],fixed['representative_asset_id'])==(1,blue.size,b['asset_id'])
            await label_service.rebuild_asset(db,1,a['asset_id'])
            assert (await summaries(db))[0]==fixed
            await label_service.apply(db,1,uuid4(),[(b['asset_id'],1)],[],['shared'])
            empty=(await summaries(db))[0]
            assert (empty['count'],empty['logical_bytes'],empty['representative_asset_id'])==(0,0,None)
            assert (await tag_summary.catalog(db,1,safe_mode=True))['items'][0]['thumbnail_url'] is None
    run(database,scenario)


def test_concurrent_assets_share_one_summary_and_reads_do_not_aggregate(database,tmp_path):
    red,_=image(tmp_path);blue,_=image(tmp_path,'blue')
    async def scenario(sessions):
        async with sessions.begin() as db:
            ids=[(await service.register(db,1,uuid4(),stored,str(i),None))['asset_id'] for i,stored in enumerate((red,blue))]
        async def edit(asset):
            async with sessions.begin() as db:
                await label_service.apply(db,1,uuid4(),[(asset,0)],['t','other'],[],origin='mcp')
        await asyncio.gather(*(edit(a) for a in ids))
        statements=[];engine=sessions.kw['bind'].sync_engine
        def trace(conn,cursor,statement,*args):statements.append(statement)
        event.listen(engine,'before_cursor_execute',trace)
        try:
            async with sessions() as db:
                first=await tag_summary.catalog(db,1,limit=1,safe_mode=False)
                second=await tag_summary.catalog(db,1,after=first['next_cursor'],limit=1,safe_mode=False)
                assert len(first['items'])==len(second['items'])==1 and second['next_cursor'] is None
                assert first['items'][0]['id']!=second['items'][0]['id']
                assert all(r['count']==2 and r['logical_bytes']==red.size+blue.size for r in first['items']+second['items'])
                assert (await tag_summary.catalog(db,2))['items']==[]
                safe=await tag_summary.catalog(db,1)
                assert all(r['thumbnail_url'] is None and r['representative_asset_id'] is None for r in safe['items'])
        finally:event.remove(engine,'before_cursor_execute',trace)
        assert not any(any(t in sql for t in ('label_head','blob','count(')) for sql in statements)
        async def rebuild_first():
            async with sessions.begin() as db:
                await label_service.rebuild_asset(db,1,ids[0])
        async def remove_second():
            async with sessions.begin() as db:
                await label_service.apply(db,1,uuid4(),[(ids[1],1)],[],['t','other'])
        await asyncio.gather(rebuild_first(),remove_second())
        async with sessions.begin() as db:
            assert all(r['count']==1 and r['logical_bytes']==red.size for r in await summaries(db))
            await label_service.apply(db,1,uuid4(),[(ids[0],1)],[],['t','other'])
            assert all(r['count']==0 and r['logical_bytes']==0 and r['representative_asset_id'] is None for r in await summaries(db))
    run(database,scenario)


def test_migration_backfills_existing_membership_without_touching_evidence(database,tmp_path):
    from pathlib import Path
    import os,subprocess,sys
    from scripts.collector_next_backup import pg_environment
    stored,_=image(tmp_path)
    async def source(sessions):
        async with sessions.begin() as db:
            a=await service.register(db,1,uuid4(),stored,'existing.png',None)
            await label_service.apply(db,1,uuid4(),[(a['asset_id'],0)],['existing'],[])
            await db.execute(text('''ALTER TABLE collector_next.tag_usage
              DROP COLUMN logical_bytes CASCADE,DROP COLUMN representative_asset_id CASCADE,
              DROP COLUMN revision,DROP COLUMN representative_policy'''))
            await db.execute(text('DROP INDEX collector_next.tag_owner_cursor'))
            await db.execute(text('ALTER TABLE collector_next.schema_version DROP CONSTRAINT schema_version_version_check'))
            await db.execute(text('UPDATE collector_next.schema_version SET version=4'))
            await db.execute(text('ALTER TABLE collector_next.schema_version ADD CHECK(version=4)'))
    run(database,source)
    subprocess.run(['psql','-X','-v','ON_ERROR_STOP=1','-c',"CREATE TABLE IF NOT EXISTS public.kura_alembic_version(version_num varchar(32) PRIMARY KEY); DELETE FROM public.kura_alembic_version; INSERT INTO public.kura_alembic_version VALUES('0173')"],env=pg_environment(database),check=True,capture_output=True)
    result=subprocess.run([sys.executable,'-m','alembic','upgrade','0174'],cwd=Path(__file__).resolve().parents[1],
        env={**os.environ,'DATABASE_URL':database.replace('postgresql:','postgresql+asyncpg:')},capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    async def check(sessions):
        async with sessions() as db:
            row=(await summaries(db))[0]
            assert row['count']==1 and row['logical_bytes']==stored.size and row['representative_asset_id'] is not None
            assert (await db.execute(text('SELECT count(*) FROM collector_next.label_observation'))).scalar_one()==1
            assert (await db.execute(text('SELECT version FROM collector_next.schema_version'))).scalar_one()==5
            assert (await db.execute(text("SELECT count(*) FROM pg_publication_tables WHERE schemaname='collector_next'"))).scalar_one()==0
    run(database,check)
