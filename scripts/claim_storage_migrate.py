#!/usr/bin/env python3
"""Resume bounded claim copying/verification; never cut over implicitly."""
import argparse
import asyncio
import json
from pathlib import Path
import shutil
import time
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine,AsyncSession
from api.config import DATABASE_URL
from api.claim_storage.migration import copy_batch,verify_batch


async def migrate(args):
    engine=create_async_engine(DATABASE_URL,pool_size=1,max_overflow=0)
    started=time.monotonic();batches=0
    try:
        while time.monotonic()-started<args.max_seconds:
            if shutil.disk_usage(args.disk).free<args.min_free_gib*1024**3:
                raise ValueError('Free space reserve reached; migration remains resumable')
            try:
                async with AsyncSession(engine) as db:
                    async with db.begin():
                        await db.execute(text("SET LOCAL lock_timeout='1s'"))
                        await db.execute(text("SET LOCAL statement_timeout='30s'"))
                        phase=(await db.execute(text('SELECT phase FROM claim_storage.migration'))).scalar_one()
                        if phase in ('ready','active'):
                            print(json.dumps({'phase':phase}),flush=True);return
                        result=await (copy_batch(db,args.batch) if phase=='copying' else verify_batch(db,args.verify_span))
                batches+=1
                if batches%10==0 or result['phase'] in ('ready','verifying'):
                    print(json.dumps({**result,'batches':batches,'elapsed_seconds':round(time.monotonic()-started,1)}),flush=True)
            except DBAPIError as exc:
                code=getattr(exc.orig,'sqlstate',None)
                if code not in ('55P03','40P01','57014'):
                    raise
                print(json.dumps({'retry_sqlstate':code}),flush=True)
                await asyncio.sleep(1)
            await asyncio.sleep(args.pause)
        print(json.dumps({'paused':'time budget; resume with same command','batches':batches}),flush=True)
    finally:
        await engine.dispose()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--batch',type=int,default=10000)
    p.add_argument('--verify-span',type=int,default=1000)
    p.add_argument('--max-seconds',type=int,default=3600)
    p.add_argument('--pause',type=float,default=0.05)
    p.add_argument('--disk',type=Path,default=Path.home())
    p.add_argument('--min-free-gib',type=int,default=64)
    args=p.parse_args()
    if args.max_seconds<1 or args.pause<0 or args.min_free_gib<1:p.error('Invalid operation budget')
    asyncio.run(migrate(args))


if __name__=='__main__':main()
