"""Opt-in same-corpus actual API read comparison + collector label WAL budget.

Legacy ORM tables have no domain triggers/FKs. This is a read-path experiment,
not a legacy ingestion/write benchmark or a full production schema simulation.
"""
import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import uuid4
import pytest
from PIL import Image
import sqlalchemy as sa
from httpx import AsyncClient,ASGITransport
from fastapi import FastAPI
from test_collector_next import database,postgres,run  # noqa:F401
from test_collector_capacity import distribution
from api.collector import router as C,label_service
from api.db.models import Base
from api.routers import posts as P

pytestmark=pytest.mark.skipif(os.environ.get('KURA_MODEL_COMPARISON')!='1',reason='explicit isolated comparison only')


def canonical(body,legacy):
    rows=body['results'] if legacy else body['items']
    if legacy:
        return [(r['id'],r['originalFilename'],r['mimeType'],r['canvasWidth'],r['canvasHeight'],sorted(t['names'][0] for t in r['tags'])) for r in rows]
    return [(r['id'],r['original_name'],r['media_type'],r['width'],r['height'],sorted(r['tags'])) for r in rows]


async def seed(sessions,count):
    # Only the current read graph: ORM column definitions and indexes, no
    # pretended reproduction of legacy governance/aggregation triggers.
    metadata=sa.MetaData()
    for model in Base.metadata.tables.values():
        table=model.to_metadata(metadata)
        table.constraints={c for c in table.constraints if not isinstance(c,sa.ForeignKeyConstraint)}
        table.foreign_keys.clear()
        for column in table.columns:column.foreign_keys.clear()
    engine=sessions.kw['bind']
    async with engine.begin() as db:
        await db.run_sync(metadata.create_all)
    async with sessions.begin() as db:
        script='\n'.join(line for line in Path(__file__).with_name('collector_comparison_seed.sql').read_text().splitlines() if not line.lstrip().startswith('--'))
        for statement in script.split(';'):
            if statement.strip():await db.execute(sa.text(statement),{'rows':count})
    async with sessions() as db:
        mismatch=(await db.execute(sa.text("""
          SELECT EXISTS(SELECT 1 FROM public.post p FULL JOIN collector_next.media_current m ON m.asset_id=p.id
            WHERE p.id IS NULL OR m.asset_id IS NULL OR (p.original_filename,p."mime-type",p.image_width,p.image_height)
              IS DISTINCT FROM (m.original_name,m.media_type,m.width,m.height))
          OR EXISTS(SELECT 1 FROM public.post_tag p FULL JOIN collector_next.label_head h
            ON h.asset_id=p.post_id AND h.tag_id=p.tag_id WHERE p.post_id IS NULL OR h.asset_id IS NULL OR NOT h.present)
        """))).scalar_one()
        assert not mismatch,'Full synthetic corpus differs between models'
    # Make the covering index visibility map usable, like a maintained library.
    # Disable per-table autovacuum to keep the later WAL probe single-writer.
    async with engine.connect() as db:
        db=await db.execution_options(isolation_level='AUTOCOMMIT')
        for namespace in ('public','collector_next'):
            names=(await db.execute(sa.text("SELECT tablename FROM pg_tables WHERE schemaname=:schema"),{'schema':namespace})).scalars().all()
            for name in names:
                quoted=engine.dialect.identifier_preparer.quote(name)
                await db.execute(sa.text(f'ALTER TABLE {namespace}.{quoted} SET(autovacuum_enabled=false)'))
                await db.execute(sa.text(f'VACUUM ANALYZE {namespace}.{quoted}'))


async def label_budget(sessions):
    counters={'count':0}
    def count(*args):counters['count']+=1
    engine=sessions.kw['bind'];sa.event.listen(engine.sync_engine,'before_cursor_execute',count)
    results={}
    try:
        for batch in (1,10,100):
            elapsed=[];wal=[];queries=[]
            for iteration in range(22):
                async with sessions() as db:
                    targets=[(r[0],r[1]) for r in (await db.execute(sa.text(
                        'SELECT asset_id,revision FROM collector_next.asset_label_state WHERE asset_id<=:n ORDER BY asset_id'),{'n':batch})).all()]
                    before=(await db.execute(sa.text('SELECT pg_current_wal_insert_lsn()::text'))).scalar_one()
                    query_start=counters['count'];started=time.perf_counter()
                    add=['write-budget'] if iteration%2==0 else [];remove=[] if add else ['write-budget']
                    await label_service.apply(db,1,uuid4(),targets,add,remove)
                    await db.commit()
                    duration=(time.perf_counter()-started)*1000;sql_count=counters['count']-query_start
                async with sessions() as db:
                    size=int((await db.execute(sa.text('SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(),CAST(CAST(:before AS text) AS pg_lsn))'),{'before':before})).scalar_one())
                if iteration>=2:elapsed.append(duration);wal.append(size);queries.append(sql_count)
            assert len(set(queries))==1 and max(queries)<=25,queries
            results[str(batch)]={'latency':distribution(elapsed),'wal_bytes':distribution(wal),'sql_statements':sorted(set(queries))}
            # distribution labels describe ms; WAL has its own units.
            results[str(batch)]['wal_bytes']={key.replace('_ms','_bytes'):value for key,value in results[str(batch)]['wal_bytes'].items()}
    finally:sa.event.remove(engine.sync_engine,'before_cursor_execute',count)
    return results


def test_same_corpus_read_apis_and_incremental_label_writes(database,tmp_path,monkeypatch):
    count=int(os.environ.get('KURA_COMPARISON_ROWS','100000'));samples=int(os.environ.get('KURA_COMPARISON_SAMPLES','40'))
    assert 10000<=count<=1000000 and count%100==0 and 5<=samples<=100
    history_operations=int(os.environ.get('KURA_COMPARISON_HISTORY','1000'));assert 10<=history_operations<=2000 and history_operations%2==0
    async def scenario(sessions):
        await seed(sessions,count)
        app=FastAPI();app.include_router(P.router);app.include_router(C.router)
        async def get_db():
            async with sessions() as db:yield db
        app.dependency_overrides[P.get_db]=get_db
        app.dependency_overrides[P.get_optional_user]=lambda:None
        app.dependency_overrides[C.get_current_user]=lambda:SimpleNamespace(user_id=1)
        monkeypatch.setenv('KURA_COLLECTOR_NEXT','1');monkeypatch.setattr(C,'async_session',sessions)
        monkeypatch.setattr(C,'root',lambda:tmp_path/'media');monkeypatch.setattr(C,'_slots',asyncio.Semaphore(2))
        cases={}
        for key,query,tags,total,offset in [('plain','',[],count,0),('deep','',[],count,count*4//5),('common','tag_1',['tag_1'],count,0),('medium','tag_2',['tag_2'],count//10,0),('rare','tag_3',['tag_3'],count//100,0),('and','tag_2 tag_3',['tag_2','tag_3'],count//100,0)]:
            old='/api/posts?'+urlencode({'query':query,'offset':offset,'limit':40})
            new='/api/collector-next/media?'+urlencode({'after':count-offset+1,'limit':40,'tags':tags},doseq=True)
            cases[key]={'legacy':old,'collector':new,'total':total}
        baseline={key:{side:[] for side in ('legacy','collector')} for key in cases};counts={key:{} for key in cases}
        competing={key:{side:[] for side in ('legacy','collector')} for key in cases}
        expected={}
        events={'count':0}
        def sql(*args):events['count']+=1
        sa.event.listen(sessions.kw['bind'].sync_engine,'before_cursor_execute',sql)
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
            async def pair(key,order):
                case=cases[key];data={};timings={};sql_counts={}
                for side in order:
                    sql_start=events['count'];start=time.perf_counter()
                    response=await client.get(case[side]);timings[side]=(time.perf_counter()-start)*1000;sql_counts[side]=events['count']-sql_start
                    assert response.status_code==200,response.text
                    body=response.json();data[side]=canonical(body,side=='legacy')
                    if side=='legacy':assert body['total']==case['total']
                assert len(data['legacy'])==40 and data['legacy']==data['collector'],(key,data)
                expected[key]=data['collector']
                return timings,sql_counts
            for key in cases:
                for _ in range(3):await pair(key,('legacy','collector'))
                for i in range(samples):
                    timings,sql_counts=await pair(key,('legacy','collector') if i%2==0 else ('collector','legacy'))
                    for side in timings:baseline[key][side].append(timings[side])
                    counts[key]=sql_counts
            assert all(case['collector']<=3 for case in counts.values()),counts
            done=asyncio.Event();progress={'rounds':0};uploads=[]
            async def writer():
                try:
                    for i in range(2000):
                        data=BytesIO();Image.new('RGB',(512,512),(i%256,(i//256)%256,200)).save(data,'PNG')
                        start=time.perf_counter()
                        response=await client.post('/api/collector-next/upload',data={'request_key':str(uuid4())},files={'file':(f'{i}.png',data.getvalue(),'image/png')})
                        assert response.status_code==200,response.text;uploads.append((time.perf_counter()-start)*1000)
                        if len(uploads)>=100 and progress['rounds']>=20:break
                finally:done.set()
            async def reader():
                while not done.is_set():
                    for key in cases:
                        if done.is_set():break
                        timings,_=await pair(key,('legacy','collector') if progress['rounds']%2==0 else ('collector','legacy'))
                        # Keep only complete pairs while the writer was alive.
                        if not done.is_set():
                            for side,value in timings.items():competing[key][side].append(value)
                    progress['rounds']+=1
            try:
                async with asyncio.TaskGroup() as group:
                    group.create_task(writer());group.create_task(reader())
            finally:sa.event.remove(sessions.kw['bind'].sync_engine,'before_cursor_execute',sql)
        assert all(values for case in competing.values() for values in case.values())
        writes=await label_budget(sessions)
        for i in range(history_operations):
            async with sessions.begin() as db:
                targets=[tuple(row) for row in (await db.execute(sa.text(
                    'SELECT asset_id,revision FROM collector_next.asset_label_state WHERE asset_id<=100 ORDER BY asset_id'))).all()]
                add=['history-growth'] if i%2==0 else [];remove=[] if add else ['history-growth']
                await label_service.apply(db,1,uuid4(),targets,add,remove)
        after_history={key:[] for key in cases};selects=[]
        def trace(conn,cursor,statement,*args):selects.append(statement)
        sa.event.listen(sessions.kw['bind'].sync_engine,'before_cursor_execute',trace)
        try:
            async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
                for key in cases:
                    for _ in range(20):
                        started=time.perf_counter();response=await client.get(cases[key]['collector'])
                        assert response.status_code==200,response.text
                        after_history[key].append((time.perf_counter()-started)*1000)
                        assert canonical(response.json(),False)==expected[key]
        finally:sa.event.remove(sessions.kw['bind'].sync_engine,'before_cursor_execute',trace)
        assert not any('collector_next.label_observation' in query for query in selects)
        writes_after=await label_budget(sessions)
        async with sessions() as db:
            size=(await db.execute(sa.text('SELECT pg_database_size(current_database())'))).scalar_one()
        cgroup=Path('/sys/fs/cgroup')/Path('/proc/self/cgroup').read_text().strip().split('::',1)[1].lstrip('/')
        limits={k:(cgroup/k).read_text().strip() for k in ('cpu.max','memory.max','memory.swap.max','memory.peak') if (cgroup/k).exists()}
        result={'rows':count,'samples':samples,'limits':limits,'cache':'warm; storage backing must be verified by runner', 'storage_path':str(tmp_path), 'storage_device':os.stat(tmp_path).st_dev,'postgres_shared_buffers':'16MB',
            'scope':'actual read APIs; synthetic corpus; legacy ORM/FKs/triggers not a full migration; concurrent writer is collector-only',
            'baseline':{key:{side:distribution(values) for side,values in case.items()} for key,case in baseline.items()},
            'baseline_sql_statements':counts,'with_collector_ingest':{key:{side:distribution(values) for side,values in case.items()} for key,case in competing.items()},
            'uploads':distribution(uploads),'collector_label_writes':writes,
            'history_growth':{'operations':history_operations,'targets':100,'added_observations':history_operations*100,'api_after':{key:distribution(values) for key,values in after_history.items()},'read_selects_history':False,'label_writes_after':writes_after},'database_bytes_both_models':size,'result_equivalence':'Full seed corpus ID/metadata/tag joins plus API IDs, names, media type, dimensions and positive tags; exact legacy totals checked'}
        output=os.environ.get('KURA_COMPARISON_RESULT')
        if output:Path(output).write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result))
    run(database,scenario)
