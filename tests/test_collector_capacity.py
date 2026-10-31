"""Opt-in isolated capacity probe. Run the whole pytest process under a cgroup.

Synthetic metadata rows do not represent stored image bytes. This measures the
actual collector API metadata path beside real bounded PNG uploads; it is not a
browser/image-display, cold-cache, HDD or legacy API comparison.
"""
import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import uuid4
import pytest
from PIL import Image
from sqlalchemy import text
from httpx import AsyncClient,ASGITransport
from test_collector_next import database,postgres,run  # noqa:F401
from test_collector_exchange_http import application
from api.collector import router as R

pytestmark=pytest.mark.skipif(os.environ.get('KURA_CAPACITY_PROBE')!='1',reason='explicit isolated capacity run only')


def distribution(values):
    values=sorted(values)
    return {'samples':len(values),**{name:round(values[min(len(values)-1,int((len(values)-1)*p))],3) for name,p in [('p50_ms',.5),('p95_ms',.95),('p99_ms',.99)]}}


def test_metadata_browsing_beside_ingestion(database,tmp_path,monkeypatch):
    count=int(os.environ.get('KURA_CAPACITY_ROWS','100000'))
    assert 10000<=count<=1000000
    async def scenario(sessions):
        async with sessions.begin() as db:
            await db.execute(text('INSERT INTO collector_next.blob(sha256,byte_size) SELECT sha256(i::text::bytea),1 FROM generate_series(1,:n) i'),{'n':count})
            await db.execute(text('INSERT INTO collector_next.asset(owner_id,blob_id) SELECT 1,id FROM collector_next.blob'))
            await db.execute(text("INSERT INTO collector_next.media_current(asset_id,owner_id,original_name,media_type,width,height) SELECT id,1,'synthetic.png','image/png',1,1 FROM collector_next.asset"))
            await db.execute(text('ANALYZE collector_next.media_current'))
        app=application()
        async def get_db():
            async with sessions() as db:yield db
        app.dependency_overrides[R.get_db]=get_db
        app.dependency_overrides[R.get_current_user]=lambda:SimpleNamespace(user_id=1)
        monkeypatch.setenv('KURA_COLLECTOR_NEXT','1');monkeypatch.setattr(R,'async_session',sessions)
        monkeypatch.setattr(R,'root',lambda:tmp_path/'media');monkeypatch.setattr(R,'_slots',asyncio.Semaphore(2))
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
            async def sample(path):
                start=time.perf_counter();result=await client.get(path)
                assert result.status_code==200,result.text
                rows=result.json()['items'];assert len(rows)==40
                return (time.perf_counter()-start)*1000
            shallow='/api/collector-next/media?limit=40'
            deep=f'/api/collector-next/media?limit=40&after={count//5}'
            for _ in range(10):await sample(shallow);await sample(deep)
            baseline={}
            for label,path in [('shallow',shallow),('deep',deep)]:baseline[label]=distribution([await sample(path) for _ in range(100)])
            uploads=[];readings={'shallow':[],'deep':[]};started=asyncio.Event();done=asyncio.Event()
            async def writer():
                started.set()
                try:
                    for i in range(100):
                        data=BytesIO();Image.new('RGB',(512,512),(i,100,200)).save(data,'PNG')
                        start=time.perf_counter()
                        result=await client.post('/api/collector-next/upload',data={'request_key':str(uuid4())},files={'file':(f'{i}.png',data.getvalue(),'image/png')})
                        assert result.status_code==200,result.text;uploads.append((time.perf_counter()-start)*1000)
                finally:done.set()
            async def reader():
                await started.wait()
                while not done.is_set() or len(readings['deep'])<100:
                    for label,path in [('shallow',shallow),('deep',deep)]:readings[label].append(await sample(path))
            await asyncio.gather(writer(),reader())
        async with sessions.begin() as db:
            size=(await db.execute(text("SELECT pg_database_size(current_database())"))).scalar_one()
            plans={}
            for name,suffix in [('shallow','ORDER BY asset_id DESC LIMIT 40'),('deep',f'AND asset_id<{count//5} ORDER BY asset_id DESC LIMIT 40'),('offset',f'ORDER BY asset_id DESC LIMIT 40 OFFSET {count*4//5}')]:
                plan=(await db.execute(text('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT asset_id,original_name,media_type,width,height FROM collector_next.media_current WHERE owner_id=1 '+suffix))).scalar_one()[0]
                plans[name]=plan
        cgroup=Path('/sys/fs/cgroup')/Path('/proc/self/cgroup').read_text().strip().split('::',1)[1].lstrip('/')
        limits={key:(cgroup/key).read_text().strip() for key in ('cpu.max','memory.max','memory.swap.max','memory.peak') if (cgroup/key).exists()}
        result={'rows':count,'environment':{'limits':limits,'postgres_shared_buffers':'16MB','cache':'warm; verify backing filesystem of pytest basetemp', 'storage_path':str(tmp_path), 'storage_device':os.stat(tmp_path).st_dev,'transport':'in-process ASGI; actual SQL and file writes'},'baseline':baseline,'with_ingest':{k:distribution(v) for k,v in readings.items()},'uploads':distribution(uploads),'database_bytes':size,'plans':plans}
        output=os.environ.get('KURA_CAPACITY_RESULT')
        if output:Path(output).write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps({k:v for k,v in result.items() if k!='plans'}))
    run(database,scenario)
