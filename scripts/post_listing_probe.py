#!/usr/bin/env python3
"""Read-only, same-snapshot comparison of broad listing totals; no media bytes read."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from api.config import DATABASE_URL
from api.read_models.post_listing import totals


async def probe():
    engine=create_async_engine(DATABASE_URL,pool_size=1,max_overflow=0)
    result={'scope':'Warm SQL totals, current corpus, shared production host; not whole UI or minimum-machine guarantee','measurements':[]}
    try:
        async with engine.connect() as c:
            await c.execution_options(isolation_level='REPEATABLE READ')
            async with c.begin():
                await c.execute(text('SET TRANSACTION READ ONLY'))
                await c.execute(text("SET LOCAL statement_timeout='30s'"))
                for safe in (False,True):
                    query="SELECT count(*),coalesce(sum(p.file_size),0) FROM post p WHERE p.status='ok' AND NOT EXISTS (SELECT 1 FROM post_tag pt JOIN tag_name n ON n.tag_id=pt.tag_id WHERE pt.post_id=p.id AND n.name='advertisement')"+(" AND p.safety='safe'" if safe else '')
                    expected=None
                    for i in range(5):
                        start=time.monotonic();old=tuple(map(int,(await c.execute(text(query))).one()));old_ms=(time.monotonic()-start)*1000
                        start=time.monotonic();new=await totals(c,safe);new_ms=(time.monotonic()-start)*1000
                        if old!=new:raise ValueError('Listing projection differs from owning rows')
                        expected=old
                        result['measurements'].append({'safe':safe,'iteration':i,'legacy_ms':round(old_ms,3),'projection_ms':round(new_ms,3)})
                    result['safe' if safe else 'all']={'count':expected[0],'bytes':expected[1]}
                result['relation_bytes']=(await c.execute(text("SELECT pg_total_relation_size('post_listing')"))).scalar_one()
        return result
    finally:await engine.dispose()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    if args.output.exists():p.error('Output exists')
    result=asyncio.run(probe())
    with args.output.open('x') as out:
        os.fchmod(out.fileno(),0o600);json.dump(result,out,indent=2);out.write('\n')
    print(json.dumps(result))


if __name__=='__main__':main()
