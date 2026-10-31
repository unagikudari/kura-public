#!/usr/bin/env python3
"""Claim-only snapshot replacement comparison in a private disposable PostgreSQL."""
import argparse
import asyncio
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import statistics
import subprocess
import tempfile
import time
import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from api.claim_storage import legacy,service
from api.claim_storage.migration import copy_batch,verify_batch
from api.claim_storage.cutover import activate
from api.collector.adapters.legacy_evidence import verify_snapshot
from scripts.collector_legacy_evidence import MAX_INPUT_BYTES


async def probe(socket,snapshot,samples):
    rows=verify_snapshot(snapshot)['post_tag_actor_claim']
    groups=defaultdict(list)
    for row in rows:groups[row['post_id'],row['actor_id']].append(row['tag_id'])
    pid,actor=max(groups,key=lambda key:len(groups[key]))
    tags=groups[pid,actor]
    conn=await asyncpg.connect(user='claim_probe',database='postgres',host=str(socket),port=65444)
    try:
        await conn.execute('''CREATE TABLE post(id int PRIMARY KEY);CREATE TABLE tag(id int PRIMARY KEY);
          CREATE TABLE post_tag_actor_claim(post_id int NOT NULL REFERENCES post ON DELETE CASCADE,
          tag_id int NOT NULL REFERENCES tag,actor_id text NOT NULL,claimed_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(post_id,tag_id,actor_id));
          CREATE INDEX ix_post_tag_actor_claim_actor ON post_tag_actor_claim(post_id,actor_id)''')
        await conn.copy_records_to_table('post',records=[(i,) for i in sorted({r['post_id'] for r in rows})])
        await conn.copy_records_to_table('tag',records=[(i,) for i in sorted({r['tag_id'] for r in rows})])
        await conn.copy_records_to_table('post_tag_actor_claim',records=[(r['post_id'],r['tag_id'],r['actor_id'],datetime.fromisoformat(r['claimed_at'])) for r in rows])
        await conn.execute('ANALYZE')
        engine=create_async_engine(f'postgresql+asyncpg://claim_probe@/postgres?host={socket}&port=65444',pool_size=1,max_overflow=0)
        sessions=async_sessionmaker(engine,expire_on_commit=False)
        measurements={}
        async def measure(name,normalized):
            values=[];before=None
            for i in range(samples+5):
                if i==5:before=await conn.fetchval('SELECT pg_current_wal_insert_lsn()::text')
                start=time.perf_counter()
                async with sessions.begin() as db:
                    if normalized:
                        backend=await service.backend(db,actor)
                        await service.replace(db,backend,pid,tags)
                    else:
                        await legacy.replace(db,pid,actor,tags)
                if i>=5:values.append((time.perf_counter()-start)*1000)
            wal=await conn.fetchval('SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(),$1::text::pg_lsn)::bigint',before)
            ordered=sorted(values)
            measurements[name]={'samples':samples,'median_ms':round(statistics.median(values),3),
                'p95_ms':round(ordered[min(len(ordered)-1,int(len(ordered)*0.95))],3),'wal_bytes':wal}
            result=await conn.fetch('SELECT tag_id FROM post_tag_actor_claim WHERE post_id=$1 AND actor_id=$2',pid,actor)
            if sorted(r['tag_id'] for r in result)!=sorted(tags):raise ValueError('Replacement changed the claim set')
        try:
            await measure('old_text_table',False)
            async with sessions.begin() as db:
                for statement in (Path(__file__).resolve().parents[1]/'api/claim_storage/schema.sql').read_text().split('-- statement-break'):
                    if statement.strip():await db.execute(text(statement))
            while True:
                async with sessions.begin() as db:result=await copy_batch(db,50000)
                if result['phase']!='copying':break
            while True:
                async with sessions.begin() as db:result=await verify_batch(db,10000)
                if result['phase']=='ready':break
            async with sessions.begin() as db:await activate(db)
            await conn.execute('ANALYZE claim_storage.claim;ANALYZE claim_storage.actor')
            await measure('normalized_legacy_view',False)
            await measure('normalized_native_service',True)
            return {'format':'kura-claim-workload-probe-v1','source_snapshot_sha256':snapshot['snapshot_sha256'],
                'source_rows':len(rows),'tags_per_completion':len(tags),'actor_characters':len(actor),
                'replacement_set_match':True,'measurements':measurements,
                'limitations':'Private PostgreSQL18 on explicitly selected local filesystem; claim replacement only, 5 warmups then repeated same post and actor, not API/model inference or minimum-spec guarantee.'}
        finally:await engine.dispose()
    finally:await conn.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--snapshot',type=Path,required=True);p.add_argument('--work-root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--samples',type=int,default=30)
    args=p.parse_args()
    if args.output.exists() or not 5<=args.samples<=100:p.error('Existing output or invalid sample budget')
    with args.snapshot.open('rb') as source:raw=source.read(MAX_INPUT_BYTES+1)
    if len(raw)>MAX_INPUT_BYTES:p.error('Snapshot exceeds input budget')
    snapshot=json.loads(raw);verify_snapshot(snapshot)
    binary=Path('/usr/lib/postgresql/18/bin');args.work_root.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='claim-workload-',dir=args.work_root) as temporary:
        root=Path(temporary);socket=root/'socket';socket.mkdir()
        def call(name,*params):subprocess.run([str(binary/name),*map(str,params)],check=True,capture_output=True)
        call('initdb','-D',root/'data','-A','trust','-U','claim_probe','--no-locale')
        call('pg_ctl','-D',root/'data','-l',root/'log','-o',f"-k {socket} -p 65444 -c listen_addresses='' -c shared_buffers=32MB -c max_connections=10",'-w','start')
        try:result=asyncio.run(probe(socket,snapshot,args.samples))
        finally:call('pg_ctl','-D',root/'data','-m','immediate','-w','stop')
    with args.output.open('x') as output:json.dump(result,output,indent=2);output.write('\n')
    print(json.dumps(result))


if __name__=='__main__':main()
